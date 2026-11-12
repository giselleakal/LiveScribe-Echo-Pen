#!/usr/bin/env python3
"""Push notebook notes to the Hermes webhook ("echo-scribe" route).

Implements the documented Hermes Generic V2 webhook contract for this repo:

  POST http://<host>:8644/webhooks/echo-scribe
  Headers: Content-Type: application/json
           X-Webhook-Timestamp: <unix seconds as string>
           X-Webhook-Signature-V2: <hex HMAC-SHA256 of f"{ts}.{body}">
           X-Request-ID: <uuid per logical note>       (1h dedupe)

Payload: one JSON object per finalized note. Bodies over 600k chars are
chunked into `{"__chunk": {...}}` envelopes, each slice signed separately.

This module owns: note building (page notes / session notes), payload
signing, chunking, HTTP delivery with response handling, an on-disk durable
queue (survives restart, never blocks capture/transcription), and accepted-
note dedup via `.push-state.json` so unchanged content is never re-sent.

Stdlib only. Config comes from the environment, never the source tree:
  ECHO_SCRIBE_SECRET   required signing secret (from the Hermes box, or
                       supplied by the user). No secret == config error.
  ECHO_SCRIBE_URL      default http://127.0.0.1:8644/webhooks/echo-scribe
  ECHO_TZ              IANA zone name for recorded_at, e.g. America/Chicago
  ECHO_UTC_OFFSET      alternative fixed offset, e.g. -05:00 or -0500
                       (ECHO_TZ wins if both are set)

Usage:
  ./src/scribe.py push [--page ID] [--session ID] [--force] [--flush-only]
"""
import base64
import datetime
import glob
import hashlib
import hmac
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "output")
PAGES_OUT = os.path.join(OUT, "pages")
AUDIO_OUT = os.path.join(OUT, "audio")
DATA_DIR = os.path.join(ROOT, "data")
INDEX = os.path.join(OUT, "index.json")

STATE_PATH = os.path.join(ROOT, ".push-state.json")
QUEUE_DIR = os.path.join(OUT, "push-queue")

URL_DEFAULT = os.environ.get("ECHO_SCRIBE_URL",
                             "http://127.0.0.1:8644/webhooks/echo-scribe")

CHUNK_CHAR_LIMIT = 600000   # slice serialized payloads above this
MAX_ATTEMPTS = 3            # exponential backoff ceiling for the queue
BACKOFF_BASE = 2.0          # seconds


# ---------------------------------------------------------------------------
# atomic output / state helpers (mirror convert.py conventions)
# ---------------------------------------------------------------------------

def atomic_write(path, data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(path) or ".",
        prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def load_state():
    state = {"notes": {}}
    try:
        with open(STATE_PATH) as f:
            got = json.load(f)
        if isinstance(got, dict):
            state.update(got)
        state.setdefault("notes", {})
    except (OSError, ValueError):
        pass
    return state


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    atomic_write(STATE_PATH, json.dumps(state, indent=2, sort_keys=True))


def _file_sig(path):
    try:
        st = os.stat(path)
        return [int(st.st_mtime_ns), int(st.st_size)]
    except OSError:
        return [0, 0]


def load_index():
    with open(INDEX) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# recorded_at: capture time when known, else a sane file mtime, rendered
# in the configured zone/offset
# ---------------------------------------------------------------------------

def _parse_offset(s):
    s = s.strip()
    m = re.match(r"^([+-])(\d{2}):?(\d{2})$", s)
    if not m:
        return None
    sign = 1 if m.group(1) == "+" else -1
    return datetime.timezone(
        datetime.timedelta(hours=int(m.group(2)), minutes=int(m.group(3))) * sign)


def _render_zone(dt):
    """Render a datetime in ECHO_TZ / ECHO_UTC_OFFSET (UTC fallback)."""
    tz_name = os.environ.get("ECHO_TZ")
    off = os.environ.get("ECHO_UTC_OFFSET")
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            return dt.astimezone(ZoneInfo(tz_name))
        except Exception:
            pass
    elif off and _parse_offset(off):
        return dt.astimezone(_parse_offset(off))
    return dt


def recorded_at(mtime):
    """ISO 8601 with a UTC offset, from a Unix mtime."""
    return _render_zone(datetime.datetime.fromtimestamp(
        mtime, datetime.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S%z")


def recorded_at_iso(iso):
    """Same rendering, from a capture-time ISO string (index.json).

    Returns None when the input is missing/unparseable so callers can
    fall back to file mtimes.
    """
    try:
        dt = datetime.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S%z")
    except (TypeError, ValueError):
        return None
    return _render_zone(dt).strftime("%Y-%m-%dT%H:%M:%S%z")


# ---------------------------------------------------------------------------
# note building
# ---------------------------------------------------------------------------

def _page_stf(page_id):
    m = glob.glob(os.path.join(DATA_DIR, "data", page_id, "*", "*.stf"))
    return m[0] if m else None


def _svg_markup(page_id):
    p = os.path.join(PAGES_OUT, page_id, "page.svg")
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _sane_mtime(*paths):
    """First path with a plausible mtime, else now.

    Pen-downloaded files carry a 1980 zero-time placeholder mtime, so
    anything before 1990 is treated as garbage and skipped.
    """
    for p in paths:
        if not p:
            continue
        try:
            m = os.stat(p).st_mtime
        except OSError:
            continue
        if m > 631152000:  # 1990-01-01
            return m
    return time.time()


def page_content_sig(page):
    svg = os.path.join(PAGES_OUT, page["id"], "page.svg")
    hasher = hashlib.sha256()
    payload = {
        "id": page["id"], "text": page.get("text", ""),
        "captured": page.get("captured", ""),
        "svg": _file_sig(svg),
    }
    hasher.update(json.dumps(payload, sort_keys=True).encode("utf-8"))
    return hasher.hexdigest()


def build_page_payload(page):
    out = {
        "note_id": str(page["id"]),
        "title": "page %s" % page["id"],
        "recorded_at": recorded_at_iso(page.get("captured") or "") or
        recorded_at(_sane_mtime(_page_stf(page["id"]),
                                os.path.join(PAGES_OUT, page["id"],
                                             "page.svg"))),
    }
    if page.get("text"):
        out["text"] = page["text"]
    svg = _svg_markup(page["id"])
    if svg:
        out["svg_pages"] = [svg]
    return out


def _session_audio_bytes(session):
    """Concatenated output .aac parts for a session, in part order."""
    chunks = []
    for u in session.get("urls", []):
        p = os.path.join(OUT, u)
        try:
            with open(p, "rb") as f:
                chunks.append(f.read())
        except OSError:
            continue
    return b"".join(chunks)


def session_content_sig(session, pages_by_id):
    hasher = hashlib.sha256()
    audio = [_file_sig(os.path.join(OUT, u)) for u in session.get("urls", [])]
    svgs = []
    for pid in session.get("pages", []):
        page = pages_by_id.get(pid)
        if page:
            svgs.append(_file_sig(os.path.join(PAGES_OUT, pid, "page.svg")))
    payload = {
        "id": session["id"], "transcription": session.get("transcript", ""),
        "captured": session.get("captured", ""),
        "audio": audio, "svgs": svgs,
    }
    hasher.update(json.dumps(payload, sort_keys=True).encode("utf-8"))
    return hasher.hexdigest()


def build_session_payload(session, pages_by_id):
    out = {
        "note_id": str(session["id"]),
        "title": "recording %s" % session["id"],
        "recorded_at": recorded_at_iso(session.get("captured") or "") or
        recorded_at(_sane_mtime(*[os.path.join(OUT, u)
                                  for u in session.get("urls", [])])),
    }
    if session.get("transcript"):
        out["transcription"] = session["transcript"]
    svgs = []
    for pid in session.get("pages", []):
        page = pages_by_id.get(pid)
        if page:
            markup = _svg_markup(pid)
            if markup:
                svgs.append(markup)
    if svgs:
        out["svg_pages"] = svgs
    audio = _session_audio_bytes(session)
    if audio:
        out["audio_name"] = "audio.aac"
        out["audio_base64"] = base64.b64encode(audio).decode("ascii")
    return out


# ---------------------------------------------------------------------------
# signing + HTTP delivery
# ---------------------------------------------------------------------------

def sign_v2(secret, ts, body):
    """Hex HMAC-SHA256 of f"{ts}.{body}" keyed with ``secret``.

    ``body`` may be str or bytes; the exact byte sequence
    ``b"<ts>.<body-bytes>"`` is signed (no str(bytes) repr reinterpretation).
    """
    key = secret.encode() if isinstance(secret, str) else secret
    if isinstance(body, bytes):
        msg = str(ts).encode("utf-8") + b"." + body
    else:
        msg = ("%s.%s" % (ts, body)).encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def post_body(body, url, secret, request_id, ts=None):
    """POST a single signed request; returns (http_status, resp_bytes).

    Raises urllib.error.HTTPError for non-2xx (caught by the caller) so the
    status/body are still recoverable.
    """
    if ts is None:
        ts = str(int(time.time()))
    sig = sign_v2(secret, ts, body)
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Timestamp": ts,
            "X-Webhook-Signature-V2": sig,
            "X-Request-ID": request_id,
            "User-Agent": "echo-notebook/1.0",
        })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, OSError, TimeoutError,
            ConnectionError) as e:
        # network failure / timeout: retryable; status 0 signals the caller
        return 0, str(e).encode("utf-8")


def classify(status):
    """Map an HTTP status to a result class: ok/config/size/rate/server."""
    if status == 200:
        return "ok"
    if status == 0:
        return "net"        # unreachable / timeout: retry next flush
    if status == 401:
        return "config"
    if status == 413:
        return "size"
    if status == 429:
        return "rate"
    return "server"


def send_payload(serialized, note_id, url, secret, request_id):
    """Send one serialized note, chunking if needed. Returns (ok, message).

    ``ok``      True if the receiver accepted every slice.
    ``message`` human-readable result for the CLI / UI.
    """
    if len(serialized) <= CHUNK_CHAR_LIMIT:
        status, _ = post_body(serialized.encode("utf-8"),
                              url, secret, request_id)
        return classify(status) == "ok", classify(status)
    total = (len(serialized) + CHUNK_CHAR_LIMIT - 1) // CHUNK_CHAR_LIMIT
    for i in range(total):
        sl = serialized[i * CHUNK_CHAR_LIMIT:(i + 1) * CHUNK_CHAR_LIMIT]
        chunk = {"__chunk": {"id": note_id, "index": i,
                             "total": total, "data": sl}}
        body = json.dumps(chunk, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
        status, _ = post_body(body, url, secret, request_id)
        if status != 200:
            return False, "chunk %d/%d rejected (%d)" % (i + 1, total, status)
    return True, "chunked (%d slices)" % total


# ---------------------------------------------------------------------------
# durable queue
# ---------------------------------------------------------------------------

def _queue_path(note_id):
    return os.path.join(QUEUE_DIR, "%s.json" % note_id)


def enqueue(note_id, payload):
    """Persist a serialized note to the on-disk queue (durable, atomic)."""
    os.makedirs(QUEUE_DIR, exist_ok=True)
    atomic_write(_queue_path(note_id), payload)


def flush_queue(url=URL_DEFAULT, secret=None):
    """Send every queued note. Returns (sent, parked_ids, messages).

    network-unreachable parks immediately (the next flush retries);
    5xx/429 get exponential backoff (MAX_ATTEMPTS); 401/413 are surfaced
    and left parked (no blind retry). Survives restart.
    """
    if secret is None:
        secret = os.environ.get("ECHO_SCRIBE_SECRET")
    if not secret:
        return 0, [], {"config": "ECHO_SCRIBE_SECRET is not set"}
    state = load_state()
    sent = 0
    parked = []
    messages = []
    queued = sorted(glob.glob(os.path.join(QUEUE_DIR, "*.json")),
                    key=os.path.basename)
    for path in queued:
        try:
            with open(path, encoding="utf-8") as f:
                q = json.load(f)
        except (OSError, ValueError):
            parked.append(os.path.basename(path))
            continue
        note_id = q.get("id")
        ok, cls = send_payload(q["payload"], note_id, url, secret,
                               q.get("request_id") or str(uuid.uuid4()))
        delay = BACKOFF_BASE
        attempt = 0
        while not ok and cls in ("server", "rate") and attempt < MAX_ATTEMPTS:
            time.sleep(delay)
            delay *= 2
            attempt += 1
            ok, cls = send_payload(q["payload"], note_id, url, secret,
                                   q.get("request_id"))
        if ok:
            state["notes"].setdefault(note_id, {})
            state["notes"][note_id].update(
                {"accepted": True, "request_id": q.get("request_id"),
                 "pushed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                 "sig": q.get("sig")})
            try:
                os.remove(path)
            except OSError:
                pass
            sent += 1
        else:
            parked.append(note_id)
            messages.append((note_id, "parked: %s" % cls))
        save_state(state)
    return sent, parked, messages


# ---------------------------------------------------------------------------
# prepare + push one target
# ---------------------------------------------------------------------------

def _is_configured():
    return bool(os.environ.get("ECHO_SCRIBE_SECRET"))


def push_targets(specs, force=False):
    """Build, dedup, enqueue and flush the given (kind, id) targets.

    Returns (results, summary) where results maps note_id -> message.
    Honors dedup: unchanged, already-accepted notes are skipped unless
    ``force``.
    """
    index = load_index()
    pages_by_id = {p["id"]: p for p in index.get("pages", [])}
    sessions_by_id = {s["id"]: s for s in index.get("sessions", [])}
    state = load_state()
    results = {}
    url = os.environ.get("ECHO_SCRIBE_URL", URL_DEFAULT)
    secret = os.environ.get("ECHO_SCRIBE_SECRET")
    if not secret:
        return results, {"config": "ECHO_SCRIBE_SECRET is not set"}
    for kind, ident in specs:
        if kind == "page":
            page = pages_by_id.get(ident)
            if not page:
                results[ident] = "no such page"
                continue
            sig = page_content_sig(page)
            payload = build_page_payload(page)
            title = "page %s" % ident
        else:
            session = sessions_by_id.get(ident)
            if not session:
                results[ident] = "no such session"
                continue
            sig = session_content_sig(session, pages_by_id)
            payload = build_session_payload(session, pages_by_id)
            title = "recording %s" % ident
        prev = state["notes"].get(ident)
        if not force and prev and prev.get("accepted") and prev.get("sig") == sig:
            results[ident] = "up to date (unchanged, already sent)"
            continue
        serialized = json.dumps(payload, ensure_ascii=False,
                                separators=(",", ":"))
        request_id = (prev or {}).get("request_id") or str(uuid.uuid4())
        enqueue(ident, json.dumps({
            "kind": kind, "id": ident, "sig": sig, "request_id": request_id,
            "payload": serialized,
        }))
        state["notes"][ident] = {"sig": sig, "request_id": request_id,
                                 "accepted": False}
        results[ident] = "queued for %s" % title
    save_state(state)
    sent, parked, msgs = flush_queue(url, secret)
    # flush_queue wrote its own accepted flags; reload to report accurately
    state = load_state()
    summary = {"sent": sent, "parked": parked, "messages": msgs}
    for _id in state["notes"]:
        info = state["notes"][_id]
        if info.get("accepted"):
            _mark = results.get(_id)
            if _mark and _mark != "up to date (unchanged, already sent)":
                results[_id] = "sent ✓"
    return results, summary


def push_one(kind, ident, force=False):
    """Single-target convenience for the web UI. Returns (ok, message).

    ok is True when the note is durably safe: delivered now, an unchanged
    duplicate, or queued on disk (it retries in the background / on the next
    flush). ok is only False for a nonexistent asset or a permanent receiver
    rejection (401 signature/clock, 413 size), which must be surfaced.
    """
    results, summary = push_targets([(kind, ident)], force=force)
    msg = results.get(ident, "unknown")
    cls_by_id = {nid: cls for nid, cls in summary.get("messages", [])}
    cls = cls_by_id.get(ident)
    if msg == "sent ✓":
        return True, "sent to Hermes ✓"
    if msg == "up to date (unchanged, already sent)":
        return True, "already sent (unchanged) — no new push"
    if str(msg).startswith("no such"):
        return False, msg
    if cls in ("config", "size"):
        return False, ("Hermes rejected the note (%s); check ECHO_SCRIBE_SECRET/"
                       "clock sync and retry" % cls)
    # queued: delivered is pending or Hermes is unreachable right now; the
    # note is safe on disk and will flush later.
    return True, "queued on disk; will flush to Hermes once reachable"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        description="Push notebook notes to the Hermes echo-scribe webhook.")
    ap.add_argument("command", nargs="?", default="push")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--page", default=None,
                       help="push only this page id")
    group.add_argument("--session", default=None,
                       help="push only this session id")
    ap.add_argument("--force", action="store_true",
                    help="re-send even if unchanged/already accepted")
    ap.add_argument("--flush-only", action="store_true",
                    help="just drain the pending queue, build nothing")
    args = ap.parse_args(argv)

    if args.flush_only:
        sent, parked, msgs = flush_queue()
        print("flushed: sent=%d parked=%s" % (sent, len(parked)))
        return 0

    index = load_index()
    pages = index.get("pages", [])
    sessions = index.get("sessions", [])

    specs = []
    if args.page:
        specs.append(("page", args.page))
    elif args.session:
        specs.append(("session", args.session))
    else:
        specs += [("page", p["id"]) for p in pages]
        specs += [("session", s["id"]) for s in sessions]

    results, summary = push_targets(specs, force=args.force)
    for note_id, msg in results.items():
        print("%-12s %s" % (note_id, msg))
    print("sent=%d parked=%d" % (summary.get("sent", 0),
                                  len(summary.get("parked", []))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())