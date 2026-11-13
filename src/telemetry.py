"""Structured logs for the Echo pipeline, written for whoever debugs it next.

Every record is one JSON line in ``logs/echo.jsonl`` (gitignored)
with a UTC timestamp, level, component, event name and the ``run_id`` of
the ``./run`` invocation that caused it; serve.py adds a ``request_id``
per HTTP request and hands both to the convert processes it starts.
Warnings and errors are also printed to stderr for the human at the
terminal.

Records describe what happened, never what was written: note text, OCR
output and transcripts are logged only as lengths, and secret-looking
fields are redacted before anything reaches a handler.

Usage as a library::

    log = telemetry.get_logger("convert")
    log.info("convert.done", extra={"pages": 12})
    with telemetry.io_call(log, "tesseract", path=png) as call:
        ...
        call.update(status=proc.returncode, bytes=len(out))

Usage as a CLI (``./run logs``)::

    python src/telemetry.py logs [--errors] [--run ID] [-n 50] [--json]
"""
import contextlib
import datetime
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
import traceback
import uuid

import paths

LOG_PATH = os.path.join(paths.LOGS, "echo.jsonl")
MAX_LOG_BYTES = 5 * 1024 * 1024   # ./run rotates to echo.jsonl.1 above this
STDERR_TAIL_CHARS = 500
SECRET_KEYS = ("secret", "token", "password", "authorization", "cookie",
               "signature", "api_key")
# record attributes owned by the logging module; anything else in a
# record's __dict__ came from ``extra=`` and is emitted as a field
_STANDARD_ATTRS = set(vars(logging.LogRecord(
    "x", logging.INFO, "x", 0, "x", None, None))) | {"message", "asctime"}
# fields every record owns; ``extra=`` values never replace them
FIXED_FIELDS = ("ts", "level", "component", "event", "run_id", "pid",
                "request_id")

# injectable clock for durations; tests replace it. Record timestamps come
# from the logging module's own wall-clock stamp (record.created).
monotonic = time.monotonic

_configured = False
_configure_lock = threading.Lock()
_run_id_lock = threading.Lock()
_context = threading.local()   # per-thread fields, e.g. serve's request_id


def run_id():
    """The correlation id of this run, minted once and inherited by children."""
    with _run_id_lock:
        rid = os.environ.get("ECHO_RUN_ID")
        if not rid:
            rid = uuid.uuid4().hex[:12]
            os.environ["ECHO_RUN_ID"] = rid
        return rid


def request_id():
    """The request being handled on this thread (or inherited), if any."""
    return getattr(_context, "request_id", None) or \
        os.environ.get("ECHO_REQUEST_ID")


@contextlib.contextmanager
def bound_request(rid):
    """Tag every record logged on this thread with ``request_id=rid``."""
    previous = getattr(_context, "request_id", None)
    _context.request_id = rid
    try:
        yield rid
    finally:
        _context.request_id = previous


def child_env(env=None):
    """Environment for a child process that carries the correlation ids."""
    out = dict(os.environ if env is None else env)
    out["ECHO_RUN_ID"] = run_id()
    rid = request_id()
    if rid:
        out["ECHO_REQUEST_ID"] = rid
    return out


def utc_stamp(epoch_seconds):
    """2025-01-15T14:23:05.123Z: fixed-width, sorts lexicographically."""
    # the pipeline supports Python 3.10, which lacks datetime.UTC
    dt = datetime.datetime.fromtimestamp(epoch_seconds, datetime.timezone.utc)
    return f"{dt:%Y-%m-%dT%H:%M:%S}.{dt.microsecond // 1000:03d}Z"


def redact(value, key=""):
    """``value`` with secret-looking keys and the push secret masked."""
    if any(word in key.lower() for word in SECRET_KEYS):
        return "[redacted]"
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, key) for v in value]
    if isinstance(value, str):
        secret = os.environ.get("ECHO_SCRIBE_SECRET")
        if secret and len(secret) >= 4 and secret in value:
            return value.replace(secret, "[redacted]")
    return value


class LogFile(logging.handlers.WatchedFileHandler):
    """The shared JSONL file: many processes append whole lines to it.

    Writers never rotate it themselves (``rotate()`` runs once per ./run
    invocation); each reopens the file when it is renamed or deleted, and
    recreates logs/ if that is gone too.
    """

    def emit(self, record):
        try:
            os.makedirs(os.path.dirname(self.baseFilename), exist_ok=True)
        except OSError:
            self.handleError(record)
            return
        super().emit(record)


def rotate(path=LOG_PATH, limit=MAX_LOG_BYTES):
    """Move a log larger than ``limit`` to ``<path>.1``; True if moved."""
    try:
        if os.path.getsize(path) <= limit:
            return False
        os.replace(path, path + ".1")
    except OSError:
        return False
    return True


class StderrHandler(logging.StreamHandler):
    """Writes to whatever ``sys.stderr`` is when the record is emitted."""

    def emit(self, record):
        self.stream = sys.stderr
        super().emit(record)


class JsonFormatter(logging.Formatter):
    """One JSON object per record with stable field names."""

    def format(self, record):
        out = {
            "ts": utc_stamp(record.created),
            "level": record.levelname,
            "component": record.name.split(".", 1)[-1],
            "event": record.getMessage(),
            "run_id": os.environ.get("ECHO_RUN_ID"),
            "pid": record.process,
        }
        rid = request_id()
        if rid:
            out["request_id"] = rid
        for key, value in vars(record).items():
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            if key not in FIXED_FIELDS:
                out[key] = value
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            out["exc_type"] = type(exc).__name__
            out["exc_msg"] = str(exc)
            out["stack"] = "".join(traceback.format_exception(
                *record.exc_info))[-4000:]
        return json.dumps(redact(out), default=str, sort_keys=False)


class HumanFormatter(logging.Formatter):
    """A terminal line: the record's ``detail`` sentence when it has one
    (``warning: page 1.2 could not be converted ...``), else
    ``warning: <event> (key=value, ...)``."""

    def format(self, record):
        fields = {k: v for k, v in vars(record).items()
                  if k not in _STANDARD_ATTRS and not k.startswith("_")}
        if "detail" in fields:
            text = f"{record.levelname.lower()}: {redact(fields['detail'])}"
            if record.exc_info and record.exc_info[1] is not None:
                text += f": {record.exc_info[1]}"
            return text
        detail = ", ".join(f"{k}={redact(v, k)}" for k, v in fields.items())
        text = f"{record.levelname.lower()}: {record.getMessage()}"
        if detail:
            text += f" ({detail})"
        if record.exc_info and record.exc_info[1] is not None:
            text += f": {record.exc_info[1]}"
        return text


def configure():
    """Attach the JSONL file and stderr handlers once per process.

    The file level comes from ECHO_LOG_LEVEL (default INFO; DEBUG adds
    per-page and per-request detail), the terminal level from
    ECHO_CONSOLE_LOG_LEVEL (default WARNING). An unwritable logs/
    directory degrades to stderr only: logging must never stop the
    pipeline.
    """
    global _configured
    with _configure_lock:
        if _configured:
            return
        _configured = True
        _attach_handlers()


def _attach_handlers():
    run_id()
    logging.raiseExceptions = False
    root = logging.getLogger("echo")
    root.setLevel(logging.DEBUG)
    root.propagate = False
    level = _level("ECHO_LOG_LEVEL", logging.INFO)
    try:
        os.makedirs(paths.LOGS, exist_ok=True)
        handler = LogFile(LOG_PATH, encoding="utf-8")
        handler.setLevel(level)
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
    except OSError as e:
        print(f"warning: logging to {LOG_PATH} disabled ({e})",
              file=sys.stderr)
    console = StderrHandler()
    console.setLevel(_level("ECHO_CONSOLE_LOG_LEVEL", logging.WARNING))
    console.setFormatter(HumanFormatter())
    root.addHandler(console)


def _level(env_name, default):
    name = os.environ.get(env_name, "").upper()
    level = logging.getLevelName(name) if name else default
    return level if isinstance(level, int) else default


def get_logger(component):
    configure()
    return logging.getLogger("echo." + component)


def warn(log, event, detail, **fields):
    """A WARNING record: ``detail`` is the sentence a human reads on the
    terminal; ``event`` and ``fields`` are what a machine filters on."""
    fields["detail"] = detail
    log.warning(event, extra=fields)


class IoCall(dict):
    """Fields of one boundary call; the caller adds outcome details."""


@contextlib.contextmanager
def io_call(log, target, ok_level=logging.INFO, **fields):
    """Emit exactly one ``io`` record for a call across a process,
    network or storage boundary, with its outcome and duration.

    A raised exception marks the call failed and propagates unchanged.
    Set ``call["outcome"]`` to override the default ok/error outcome.
    Calls made once per page or file pass ``ok_level=logging.DEBUG`` so
    successes don't flood the log; failures are always WARNING.
    """
    call = IoCall(fields)
    start = monotonic()
    try:
        yield call
    except BaseException as e:
        call.setdefault("outcome", "error")
        call["exc_type"] = type(e).__name__
        call["exc_msg"] = str(e)[:STDERR_TAIL_CHARS]
        raise
    finally:
        call.setdefault("outcome", "ok")
        call["duration_ms"] = round((monotonic() - start) * 1000, 1)
        level = ok_level if call["outcome"] == "ok" else logging.WARNING
        log.log(level, "io", extra=dict(call, target=target))


def tail(text, limit=STDERR_TAIL_CHARS):
    """Last ``limit`` characters of process output, for an io record."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    text = (text or "").strip()
    return text[-limit:]


# ---------------------------------------------------------------------------
# CLI: read the log back
# ---------------------------------------------------------------------------

def read_records(path=LOG_PATH):
    """Every parseable record, oldest first, including the rotated file."""
    yield from _records_in(path + ".1")
    yield from _records_in(path)


def _records_in(name):
    """Records of one log file; a missing file or bad line is skipped."""
    try:
        with open(name, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    for line in lines:
        try:
            yield json.loads(line)
        except ValueError:
            continue


def select(records, errors=False, run=None):
    for rec in records:
        if errors and rec.get("level") not in ("WARNING", "ERROR",
                                               "CRITICAL"):
            continue
        if run and rec.get("run_id") != run:
            continue
        yield rec


def render(rec):
    fixed = ("ts", "level", "component", "event", "run_id", "pid")
    extra = " ".join(f"{k}={json.dumps(v, default=str)}"
                     for k, v in rec.items() if k not in fixed)
    return (f"{rec.get('ts', '?')} {rec.get('level', '?'):<7} "
            f"{rec.get('component', '?'):<8} {rec.get('event', '?'):<22} "
            f"{rec.get('run_id', '-')} {extra}")


def parse_fields(pairs):
    """{"k": v} from ["k=v", ...]; integers stay integers."""
    out = {}
    for pair in pairs:
        key, _, value = pair.partition("=")
        try:
            out[key] = int(value)
        except ValueError:
            out[key] = value
    return out


def record_event(argv):
    """``event <component> <name> [key=value ...] [--level LEVEL]``.

    Parsed by hand: argparse before Python 3.12 rejects an option placed
    before trailing positional ``key=value`` words, and ./run must be
    able to write either order.
    """
    args = list(argv)
    level = "INFO"
    if "--level" in args:
        at = args.index("--level")
        level = args[at + 1] if at + 1 < len(args) else level
        del args[at:at + 2]
    if len(args) < 2:
        print("usage: telemetry.py event <component> <name> [key=value ...] "
              "[--level LEVEL]", file=sys.stderr)
        return 2
    component, name, fields = args[0], args[1], args[2:]
    get_logger(component).log(getattr(logging, level.upper(), logging.INFO),
                              name, extra=parse_fields(fields))
    return 0


def main(argv=None):
    import argparse
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["event"]:
        return record_event(argv[1:])
    if argv[:1] == ["rotate"]:
        rotate()
        return 0
    ap = argparse.ArgumentParser(description="Read or write pipeline logs.")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("event", help="record one event (for ./run): "
                                 "event <component> <name> [key=value ...]")
    sub.add_parser("rotate", help="move a large log aside (./run does this)")
    logs = sub.add_parser("logs", help="print recent log records")
    logs.add_argument("--errors", action="store_true",
                      help="only warnings and errors")
    logs.add_argument("--run", default=None,
                      help="only records of this run_id ('last' = newest run)")
    logs.add_argument("-n", type=int, default=50, help="records to show")
    logs.add_argument("--json", action="store_true",
                      help="print raw JSON lines")
    args = ap.parse_args(argv)
    if args.n < 1:
        print("-n must be at least 1", file=sys.stderr)
        return 2
    records = list(read_records())
    run = args.run
    if run == "last":
        run = next((r.get("run_id") for r in reversed(records)
                    if r.get("run_id")), None)
    chosen = list(select(records, errors=args.errors, run=run))[-args.n:]
    if not chosen:
        print(f"no matching log records in {LOG_PATH}", file=sys.stderr)
        return 1
    for rec in chosen:
        print(json.dumps(rec) if args.json else render(rec))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
