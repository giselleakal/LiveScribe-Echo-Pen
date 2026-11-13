"""Pen-safety rules of ./run, with lsusb, sudo and dumpscribe stubbed out."""
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

from tests import REPO

FAKE_DUMPSCRIBE = """#!/bin/sh
echo "dumpscribe $*" >> "$CALLS"
[ "$1" = "-r" ] && exit 0
[ -n "$AUDIO_OK" ] && echo "[ECHO] audio download ok"
[ -n "$PAGES_OK" ] && echo "[ECHO] pages download ok"
exit "${DUMP_STATUS:-0}"
"""
FAKE_SUDO = """#!/bin/sh
case "$1" in chown) echo "chown" >> "$CALLS"; exit 0 ;; esac
exec "$@"
"""
FAKE_LSUSB = """#!/bin/sh
echo "Bus 001 Device 002: ID 1cfb:1032 Livescribe Echo(TM) Smartpen"
"""
FAKE_CONVERT = """import os
with open(os.environ["CALLS"], "a") as f:
    f.write("convert\\n")
"""


def write_exe(path, body):
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


class RunScriptPenSafetyTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="echo-run-test-")
        self.addCleanup(shutil.rmtree, self.root, True)
        shutil.copy(os.path.join(REPO, "run"), self.root)
        for d in ("vendor", "src", "bin", "data"):
            os.makedirs(os.path.join(self.root, d))
        with open(os.path.join(self.root, "vendor", "dumpscribe.c"), "w"):
            pass
        later = time.time() + 5
        write_exe(os.path.join(self.root, "vendor", "dumpscribe"),
                  FAKE_DUMPSCRIBE)
        os.utime(os.path.join(self.root, "vendor", "dumpscribe"),
                 (later, later))
        with open(os.path.join(self.root, "src", "convert.py"), "w") as f:
            f.write(FAKE_CONVERT)
        write_exe(os.path.join(self.root, "bin", "sudo"), FAKE_SUDO)
        write_exe(os.path.join(self.root, "bin", "lsusb"), FAKE_LSUSB)
        self.calls = os.path.join(self.root, "calls.log")

    def run_cmd(self, args, answer="", **env):
        full_env = dict(os.environ)
        full_env.update({
            "PATH": os.path.join(self.root, "bin") + os.pathsep +
            os.environ["PATH"],
            "CALLS": self.calls,
        })
        full_env.update(env)
        proc = subprocess.run(
            [os.path.join(self.root, "run")] + args, input=answer,
            env=full_env, capture_output=True, text=True, timeout=60)
        with open(self.calls) as f:
            calls = f.read().split("\n")
        return proc, [c for c in calls if c]

    def test_full_backup_lets_the_user_clear_the_pen(self):
        _, calls = self.run_cmd(["sync"], answer="y\n",
                                AUDIO_OK="1", PAGES_OK="1")
        self.assertIn(f"dumpscribe -r {self.root}/data", calls)

    def test_missing_pages_marker_never_offers_to_clear(self):
        proc, calls = self.run_cmd(["sync"], answer="y\n", AUDIO_OK="1")
        self.assertFalse(any(c.startswith("dumpscribe -r") for c in calls))
        self.assertIn("Not offering", proc.stdout)

    def test_sync_without_a_terminal_leaves_the_pen_alone(self):
        proc, calls = self.run_cmd(["sync"], AUDIO_OK="1", PAGES_OK="1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(any(c.startswith("dumpscribe -r") for c in calls))

    def test_clean_pen_refuses_when_audio_did_not_download(self):
        proc, calls = self.run_cmd(["clean-pen"], answer="y\n", PAGES_OK="1")
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(any(c.startswith("dumpscribe -r") for c in calls))

    def test_clean_pen_retires_only_after_a_full_backup(self):
        proc, calls = self.run_cmd(["clean-pen"], answer="y\n",
                                   AUDIO_OK="1", PAGES_OK="1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            [c for c in calls if c.startswith("dumpscribe")],
            [f"dumpscribe {self.root}/data",
             f"dumpscribe -r {self.root}/data"])

    def test_failed_download_still_returns_data_ownership(self):
        proc, calls = self.run_cmd(["sync"], DUMP_STATUS="3")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("chown", calls)
        self.assertNotIn("convert", calls)


class RunScriptSetupTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="echo-setup-test-")
        self.addCleanup(shutil.rmtree, self.root, True)
        shutil.copy(os.path.join(REPO, "run"), self.root)
        os.makedirs(os.path.join(self.root, "bin"))
        # no dpkg-query / pkg-config: the apt and build steps are skipped
        for tool in ("dpkg-query", "pkg-config"):
            write_exe(os.path.join(self.root, "bin", tool), "#!/bin/sh\nexit 1\n")

    def run_setup(self):
        env = dict(os.environ, PATH=os.path.join(self.root, "bin") +
                   os.pathsep + os.environ["PATH"])
        return subprocess.run([os.path.join(self.root, "run"), "setup"],
                              input="", env=env, capture_output=True,
                              text=True, timeout=120)

    def test_setup_creates_a_venv_that_sees_system_packages(self):
        proc = self.run_setup()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(os.path.join(self.root, ".venv", "pyvenv.cfg")) as f:
            self.assertIn("include-system-site-packages = true", f.read())
        self.assertEqual(self.run_setup().returncode, 0)   # idempotent

    def test_setup_refuses_a_venv_without_system_packages(self):
        subprocess.run([sys.executable, "-m", "venv", "--without-pip",
                        os.path.join(self.root, ".venv")], check=True)
        proc = self.run_setup()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("rm -rf .venv", proc.stderr)


class HelpTest(unittest.TestCase):
    def test_help_lists_every_command_the_script_dispatches(self):
        with open(os.path.join(REPO, "run")) as f:
            script = f.read()
        dispatched = re.findall(r"^    ([a-z][a-z-]*)\)$", script, re.M)
        out = subprocess.run([os.path.join(REPO, "run"), "help"],
                             capture_output=True, text=True,
                             timeout=30).stdout
        for command in dispatched:
            self.assertIn("./run " + command, out, command)


if __name__ == "__main__":
    unittest.main()
