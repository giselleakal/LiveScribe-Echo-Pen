#!/usr/bin/env python3
"""Check that this machine can run the Echo pipeline, and say how to fix it.

    python src/doctor.py            human-readable checklist (./run doctor)
    python src/doctor.py --json     the same, machine-readable
    python src/doctor.py report     a sanitized debugging bundle
                                    (./run debug-report)

Each check answers one question and, when the answer is no, gives the
command that fixes it. The report contains versions, check results,
counts and recent warning/error log records. It never contains note
content (page text, transcripts, titles), and the home directory is
written as ``~``.
"""
import dataclasses
import getpass
import glob
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time

import paths

OK, WARN, FAIL = "ok", "warn", "fail"
MARK = {OK: "✔", WARN: "!", FAIL: "✘"}
PEN_IDS = ("1cfb:1030", "1cfb:1031", "1cfb:1032")
BUILD_PACKAGES = ("glib-2.0", "openobex", "libusb-1.0", "libarchive",
                  "libxml-2.0")
APT_BUILD = ("sudo apt install build-essential pkg-config libglib2.0-dev "
             "libusb-1.0-0-dev libopenobex2-dev libarchive-dev libxml2-dev "
             "libbluetooth-dev")


@dataclasses.dataclass(frozen=True)
class Result:
    name: str
    status: str
    message: str
    fix: str = ""


# ---------------------------------------------------------------------------
# checks: each returns one Result
# ---------------------------------------------------------------------------

def check_python():
    version = ".".join(str(n) for n in sys.version_info[:3])
    # runtime guard for people starting the pipeline with an old python3
    if sys.version_info < (3, 10):  # noqa: UP036
        return Result("python", FAIL, f"Python {version} is too old",
                      "install Python 3.10 or newer")
    return Result("python", OK, f"Python {version} ({sys.executable})")


def check_venv():
    cfg = os.path.join(paths.REPO, ".venv", "pyvenv.cfg")
    if not os.path.exists(cfg):
        return Result("venv", WARN, "no .venv (OCR/STT need one)",
                      "./run setup --ocr --stt")
    try:
        with open(cfg, encoding="utf-8", errors="replace") as f:
            text = f.read().replace(" ", "").lower()
    except OSError as e:
        return Result("venv", FAIL, f".venv/pyvenv.cfg unreadable ({e})",
                      "rm -rf .venv && ./run setup")
    if "include-system-site-packages=true" not in text:
        return Result("venv", FAIL, ".venv cannot see the system pycairo",
                      "rm -rf .venv && ./run setup")
    return Result("venv", OK, ".venv uses system site-packages")


def check_pycairo():
    try:
        import cairo
    except ImportError:
        return Result("pycairo", FAIL, "pycairo is missing (needed to render "
                      "thumbnails and OCR images)",
                      "sudo apt install python3-cairo")
    return Result("pycairo", OK, f"pycairo {cairo.version}")


def check_ocr():
    import ocr
    engines = []
    if ocr.trocr_available():
        engines.append("trocr")
    if ocr.available():
        engines.append("tesseract")
    if not engines:
        return Result("ocr", WARN, "no OCR engine (Transcribe will do nothing)",
                      "./run setup --ocr")
    return Result("ocr", OK, f"engines: {', '.join(engines)}; "
                  f"profile {ocr.default_profile_name()}")


def check_stt():
    import stt
    if not stt.available():
        return Result("stt", WARN, "speech-to-text unavailable",
                      "./run setup --stt")
    return Result("stt", OK, "Moonshine + bundled ffmpeg available")


def check_dumpscribe():
    binary = os.path.join(paths.VENDOR, "dumpscribe")
    source = os.path.join(paths.VENDOR, "dumpscribe.c")
    if os.path.exists(binary) and \
            os.path.getmtime(binary) >= os.path.getmtime(source):
        return Result("dumpscribe", OK, "pen client built and current")
    missing = missing_build_packages()
    if missing:
        return Result("dumpscribe", WARN, "pen client not built; build "
                      f"libraries missing: {', '.join(missing)}", APT_BUILD)
    return Result("dumpscribe", WARN, "pen client not built or older than "
                  "its source (rebuilt automatically by ./run sync)",
                  "./scripts/build-dumpscribe.sh")


def missing_build_packages():
    if not shutil.which("pkg-config"):
        return ["pkg-config"]
    return [pkg for pkg in BUILD_PACKAGES
            if subprocess.run(["pkg-config", "--exists", pkg],
                              check=False).returncode != 0]


def check_pen():
    if not shutil.which("lsusb"):
        return Result("pen", WARN, "lsusb not found", "sudo apt install usbutils")
    try:
        out = subprocess.run(["lsusb"], capture_output=True, text=True,
                             timeout=10, check=False).stdout
    except (OSError, subprocess.SubprocessError) as e:
        return Result("pen", WARN, f"lsusb failed: {e}")
    if any(pid in out.lower() for pid in PEN_IDS):
        return Result("pen", OK, "Livescribe pen attached")
    return Result("pen", WARN, "pen not attached (only needed for sync)",
                  "plug in with a DATA cable, then run "
                  "scripts/windows-connect.ps1 in an admin PowerShell")


def check_library():
    pages = glob.glob(os.path.join(paths.DATA, "data", "*", "*", "*.stf"))
    if not pages:
        return Result("library", WARN, "no pen data in data/ yet",
                      "./run sync")
    index = os.path.join(paths.OUT, "index.json")
    try:
        with open(index, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return Result("library", WARN, f"{len(pages)} pages downloaded but "
                      "no readable output/index.json", "./run convert")
    return Result("library", OK, f"{len(pages)} source pages; index lists "
                  f"{len(data.get('pages', []))} pages, "
                  f"{len(data.get('sessions', []))} recordings")


def check_state():
    import convert
    if not os.path.exists(convert.STATE_PATH):
        return Result("state", OK, "no incremental state yet (first convert "
                      "renders everything)")
    try:
        with open(convert.STATE_PATH, encoding="utf-8") as f:
            json.load(f)
    except (OSError, ValueError):
        return Result("state", WARN, ".convert-state.json is unreadable; the "
                      "next convert re-renders everything",
                      "./run convert --force")
    return Result("state", OK, ".convert-state.json readable")


def check_lock():
    import convert
    if convert.lock_is_held():
        return Result("lock", WARN, "a convert or server holds the run lock "
                      f"(pid {convert.lock_owner()})",
                      "wait for it, or stop that process")
    return Result("lock", OK, "run lock free")


def check_push():
    if not os.environ.get("ECHO_SCRIBE_SECRET"):
        return Result("push", WARN, "ECHO_SCRIBE_SECRET not set (Hermes push "
                      "disabled)", "add ECHO_SCRIBE_SECRET=... to .env")
    queued = glob.glob(os.path.join(paths.OUT, "push-queue", "*.json"))
    return Result("push", OK, f"push configured; {len(queued)} note(s) "
                  "queued")


def check_logs():
    try:
        os.makedirs(paths.LOGS, exist_ok=True)
        probe = os.path.join(paths.LOGS, ".doctor-probe")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
    except OSError as e:
        return Result("logs", WARN, f"logs/ is not writable ({e})",
                      "sudo chown -R \"$USER\" logs")
    return Result("logs", OK, "logs/echo.jsonl writable")


CHECKS = (check_python, check_venv, check_pycairo, check_ocr, check_stt,
          check_dumpscribe, check_pen, check_library, check_state,
          check_lock, check_push, check_logs)


def run_checks():
    results = []
    for check in CHECKS:
        try:
            results.append(check())
        except Exception as e:
            name = check.__name__.replace("check_", "")
            results.append(Result(name, FAIL, "check crashed: "
                                  f"{type(e).__name__}: {e}"))
    return results


def print_checklist(results):
    for r in results:
        print(f"{MARK[r.status]} {r.name:<11} {r.message}")
        if r.fix and r.status != OK:
            print(f"  {'':<11} fix: {r.fix}")
    fails = sum(r.status == FAIL for r in results)
    warns = sum(r.status == WARN for r in results)
    print(f"\n{fails} problem(s), {warns} warning(s)")


# ---------------------------------------------------------------------------
# debug report
# ---------------------------------------------------------------------------

WINDOWS_USER_DIR = re.compile(r"((?:/mnt/[a-z]|[A-Za-z]:)[\\/]+Users[\\/]+)"
                              r"[^\\/\s\"']+", re.IGNORECASE)


def scrub(text):
    """Hide who ran this, so a shared report names no user.

    The home directory becomes ~, Windows user folders (C:\\Users\\<name>,
    /mnt/c/Users/<name>) become <user>, and so does the login name itself
    wherever it appears as a whole word.
    """
    home = os.path.expanduser("~")
    if home and home != "/":
        text = text.replace(home, "~")
    text = WINDOWS_USER_DIR.sub(r"\1<user>", text)
    try:
        user = getpass.getuser()
    except Exception:
        user = ""
    if len(user) >= 3:
        text = re.sub(r"(?<![\w.-])%s(?![\w.-])" % re.escape(user), "<user>",
                      text)
    return text


def git_summary():
    def git(*args):
        try:
            return subprocess.run(["git", "-C", paths.REPO] + list(args),
                                  capture_output=True, text=True, timeout=10,
                                  check=False).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    dirty = git("status", "--porcelain")
    return {"branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "commit": git("rev-parse", "--short", "HEAD"),
            "uncommitted_files": len(dirty.splitlines()) if dirty else 0}


def state_summary():
    """Counts only: no ids, titles or text."""
    import convert
    state = convert.load_state()
    pages = state.get("page_entries", {}).values()
    sessions = state.get("session_entries", {}).values()
    return {"render_version": state.get("render_version"),
            "pages": len(state.get("page_entries", {})),
            "pages_ocr_done": sum(1 for p in pages if p.get("ocr")),
            "sessions": len(state.get("session_entries", {})),
            "sessions_stt_done": sum(1 for s in sessions
                                     if (s.get("stt") or {}).get("done"))}


def recent_records(limit=60):
    """Recent warnings/errors plus the last run's lifecycle records."""
    import telemetry
    records = list(telemetry.read_records())
    problems = [r for r in records
                if r.get("level") in ("WARNING", "ERROR", "CRITICAL")]
    lifecycle = [r for r in records
                 if str(r.get("event", "")).endswith((".start", ".done",
                                                      ".failed"))]
    keep = sorted(problems[-limit:] + lifecycle[-10:],
                  key=lambda r: r.get("ts", ""))
    return [r for i, r in enumerate(keep) if r not in keep[:i]]


def build_report():
    results = run_checks()
    generated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    profile = os.environ.get("ECHO_OCR_PROFILE", "(default)")
    lines = ["# Echo pipeline debug report", "",
             f"Generated {generated}. Contains no note content.", "",
             "## Environment", "",
             f"- platform: {platform.platform()}",
             f"- python: {sys.version.split()[0]}",
             f"- git: {json.dumps(git_summary())}",
             f"- ECHO_ROOT set: {bool(os.environ.get('ECHO_ROOT'))}",
             f"- ECHO_OCR_PROFILE: {profile}",
             "", "## Checks", ""]
    for r in results:
        fix = f" (fix: {r.fix})" if r.fix and r.status != OK else ""
        lines.append(f"- {MARK[r.status]} {r.name}: {r.message}{fix}")
    lines += ["", "## Incremental state", "",
              "```json", json.dumps(state_summary(), indent=2), "```",
              "", "## Recent log records (warnings, errors, lifecycle)", "",
              "```jsonl"]
    lines += [json.dumps(r, sort_keys=True) for r in recent_records()]
    lines += ["```", ""]
    return scrub("\n".join(lines))


def write_report():
    """Write the report under logs/; returns its path.

    Raises OSError when logs/ cannot be written (main reports it).
    """
    text = build_report()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = os.path.join(paths.LOGS, f"debug-report-{stamp}.md")
    os.makedirs(paths.LOGS, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Check the Echo pipeline setup.")
    ap.add_argument("command", nargs="?", default="check",
                    choices=("check", "report"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.command == "report":
        try:
            path = write_report()
        except OSError as e:
            print(f"could not write the debug report: {e}", file=sys.stderr)
            return 1
        print(f"debug report written to {scrub(path)}")
        print("It has no note content; read it before sharing anyway.")
        return 0
    results = run_checks()
    if args.json:
        print(json.dumps([dataclasses.asdict(r) for r in results], indent=2))
    else:
        print_checklist(results)
    return 1 if any(r.status == FAIL for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
