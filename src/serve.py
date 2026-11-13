#!/usr/bin/env python3
"""Serve the converted notebook pages over HTTP.

Serves the contents of output/ as static files plus a generated index
page that lists pages (SVG previews) and audio sessions from index.json.

Usage:  ./src/serve.py [--port 8000] [--host 0.0.0.0]
"""
import argparse
import contextlib
import glob
import json
import http.server
import logging
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import uuid

# Core pipeline helpers (state load/save, the run lock). This imports only
# stdlib + the vendored parsestf/ocr at module load; cairo and torch stay
# lazy inside convert.py/ocr.py, so serving stays dependency-light.
import convert
import paths
import scribe
import telemetry
import webpage

log = telemetry.get_logger("serve")

ROOT = paths.ROOT
OUT = paths.OUT
CONVERT_PY = os.path.join(paths.SRC, "convert.py")
INDEX = os.path.join(OUT, "index.json")

# Path-traversal-safe id shapes, matched with fullmatch: ASCII digits and
# hex only, so / \ .. leading '.', NUL, newlines and unicode digits fail.
PAGE_ID_RE = re.compile(r"[0-9]+(?:\.[0-9]+)*")
SESSION_ID_RE = re.compile(r"PRS-[0-9a-fA-F]+")
# pen notebook guid (hex, from the dump directory name) or legacy ids
NOTEBOOK_ID_RE = re.compile(r"[0-9a-fA-F]{8,32}|0|251")
ID_PATTERNS = {"page": PAGE_ID_RE, "session": SESSION_ID_RE}
# Batches larger than this require {"confirm": "DELETE"} in the request
# body (and the UI asks the user to type DELETE first) — so a stray
# "select all" + click can never wipe the whole library in one shot.
BULK_DELETE_LIMIT = 5
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
MAX_BODY_BYTES = 1 << 20     # API bodies are tiny JSON objects
LOCK_WAIT = 10.0             # seconds an API call waits for a running convert

# ---------------------------------------------------------------------------
# mutating APIs
# ---------------------------------------------------------------------------

class ApiError(Exception):
    """A request the API refuses; becomes a JSON error response."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def find_session_dirs(session_id):
    """Return every raw pen session directory for ``session_id``."""
    replay = os.path.join(paths.DATA, "userdata")
    return glob.glob(os.path.join(
        replay, "*", "Paper Replay", "*", "*", "sessions", session_id))


def _reconcile():
    """Re-run convert so index.json, output pruning, and state stay correct.

    Runs with ECHO_LOCK_HELD so the child skips locking (the caller holds
    the run lock). Output is regenerated atomically, so a concurrent GET
    never sees a torn file. Returns True when convert succeeded.
    """
    return run_convert([])


def run_convert(argv):
    """Run convert.py as a child that inherits the lock and correlation ids.

    The caller holds the run lock (ECHO_LOCK_HELD tells the child to skip
    it). Returns True when convert exited 0.
    """
    env = telemetry.child_env()
    env["ECHO_LOCK_HELD"] = "1"
    with telemetry.io_call(log, "convert", argv=argv) as call:
        proc = subprocess.run([sys.executable, CONVERT_PY] + argv,
                              env=env, check=False)
        call["status"] = proc.returncode
        if proc.returncode != 0:
            call["outcome"] = "error"
    return proc.returncode == 0


def delete_page(page_id):
    """Remove a page everywhere: raw dump, output, and incremental state.

    Returns False when nothing of the page existed.
    """
    out_dir = os.path.join(OUT, "pages", page_id)
    raw_dir = os.path.join(paths.DATA, "data", page_id)
    existed = os.path.isdir(out_dir) or os.path.isdir(raw_dir)
    shutil.rmtree(out_dir, ignore_errors=True)
    shutil.rmtree(raw_dir, ignore_errors=True)
    state = convert.load_state()
    state["page_entries"].pop(page_id, None)
    state["sources"] = {
        k: v for k, v in state["sources"].items()
        if not k.startswith("data/data/%s/" % page_id)}
    convert.save_state(state)
    return existed


def delete_session(session_id):
    """Remove a recording everywhere: raw session dir, output audio, state.

    Returns False when nothing of the recording existed.
    """
    raw_dirs = find_session_dirs(session_id)
    audio = glob.glob(os.path.join(OUT, "audio", session_id + ".*.aac"))
    existed = bool(raw_dirs or audio)
    for d in raw_dirs:
        shutil.rmtree(d, ignore_errors=True)
    for f in audio + [os.path.join(OUT, "audio", session_id + ".txt")]:
        try:
            os.remove(f)
        except OSError:
            pass
    state = convert.load_state()
    state["session_entries"].pop(session_id, None)
    state["sources"] = {
        k: v for k, v in state["sources"].items()
        if ("/sessions/%s/" % session_id) not in k}
    convert.save_state(state)
    return existed


def delete_asset(kind, ident):
    """Delete one page or session; False when it did not exist."""
    if kind == "page":
        return delete_page(ident)
    return delete_session(ident)


@contextlib.contextmanager
def run_lock():
    """Hold the convert run lock, or refuse the request as busy (409)."""
    try:
        took = convert.acquire_lock(wait=LOCK_WAIT)
    except RuntimeError as e:
        raise ApiError(409, "busy: %s" % e) from e
    try:
        yield
    finally:
        if took:
            convert.release_lock()


def _run_convert_locked(argv):
    """Run ``convert.py`` with ``argv`` under the run lock; True on success."""
    with run_lock():
        return run_convert(argv)


def send_json(handler, status, payload):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def asset_id(payload, kind):
    """The validated ``id`` of a page or session request body."""
    ident = payload.get("id")
    if not isinstance(ident, str) or not ID_PATTERNS[kind].fullmatch(ident):
        raise ApiError(400, "invalid id format")
    return ident


def any_asset_id(payload):
    """A validated page or session id (titles apply to both)."""
    ident = payload.get("id")
    if not isinstance(ident, str):
        raise ApiError(400, "invalid page/session id")
    if not any(p.fullmatch(ident) for p in ID_PATTERNS.values()):
        raise ApiError(400, "invalid page/session id")
    return ident


def display_name(payload, limit):
    name = payload.get("name")
    if not isinstance(name, str):
        raise ApiError(400, "name must be a string")
    name = name.strip()
    if len(name) > limit:
        raise ApiError(400, "name too long (max %d chars)" % limit)
    return name


def host_name(host_header):
    """Lower-case host name from a Host header, without the port."""
    try:
        return (urllib.parse.urlsplit("//" + host_header).hostname or "")
    except ValueError:
        return ""


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=OUT, **kwargs)

    # -- request records ----------------------------------------------------

    def send_response(self, code, message=None):
        self._status = code
        super().send_response(code, message)

    def handle_one_request(self):
        """Handle one request with its own request_id and one http record.

        POSTs log at INFO; GETs (page loads, thumbnails, audio) at DEBUG so
        browsing does not flood the log. Server errors (5xx) are WARNING;
        refused requests (4xx) are expected and keep the normal level.
        """
        self._status = None
        started = telemetry.monotonic()
        with telemetry.bound_request(uuid.uuid4().hex[:12]):
            try:
                super().handle_one_request()
            except Exception:
                self._status = self._status or 500
                raise
            finally:
                self._log_request(started)

    def _log_request(self, started):
        """The one http.request record of the request being finished."""
        if not getattr(self, "command", None):
            return   # connection closed without a request
        status = self._status or 0
        level = logging.INFO if self.command == "POST" else logging.DEBUG
        if status >= 500:
            level = logging.WARNING
        log.log(level, "http.request", extra={
            "method": self.command,
            "path": urllib.parse.urlsplit(self.path).path,
            "status": status,
            "client": self.client_address[0],
            "duration_ms": round((telemetry.monotonic() - started) * 1000, 1)})

    # -- request guards ----------------------------------------------------

    def _bound_to_loopback(self):
        address = self.server.server_address
        host = address[0] if isinstance(address, tuple) else address
        return str(host) in LOOPBACK_HOSTS

    def _host_allowed(self):
        """Refuse DNS-rebinding: a loopback server answers loopback names only.

        A page on another site can point its own host name at 127.0.0.1;
        the browser then treats this server as same-origin with that site.
        """
        if not self._bound_to_loopback():
            return True
        return host_name(self.headers.get("Host", "")) in LOOPBACK_HOSTS

    def _check_mutation(self):
        """Guards shared by every POST API; raises ApiError to refuse."""
        media_type = self.headers.get("Content-Type", "").split(";")[0]
        if media_type.strip().lower() != "application/json":
            raise ApiError(415, "Content-Type must be application/json")
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if origin is not None and origin not in ("http://" + host,
                                                 "https://" + host):
            raise ApiError(403, "cross-origin request denied")
        if not self._bound_to_loopback() and \
                not os.environ.get("ECHO_ALLOW_LAN_DELETES"):
            raise ApiError(403, "Changes are disabled on a network host. "
                                "Set ECHO_ALLOW_LAN_DELETES=1 to enable.")

    def _read_json(self):
        """The request body as a JSON object; raises ApiError otherwise."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            raise ApiError(400, "empty request body")
        if length > MAX_BODY_BYTES:
            raise ApiError(413, "request body too large")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise ApiError(400, "bad JSON body: %s" % e) from e
        if not isinstance(payload, dict):
            raise ApiError(400, "JSON body must be an object")
        return payload

    # -- dispatch ------------------------------------------------------------

    def do_GET(self):
        if not self._host_allowed():
            send_json(self, 403, {"ok": False, "error": "unexpected Host"})
            return
        path = urllib.parse.urlparse(self.path).path
        if path == "/" or path == "":
            body = webpage.build_index().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            # the index is rebuilt from index.json on every request while
            # runs may be updating it; never cache it
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()

    def do_HEAD(self):
        if not self._host_allowed():
            send_json(self, 403, {"ok": False, "error": "unexpected Host"})
            return
        super().do_HEAD()

    def do_POST(self):
        route = self._route()
        if route is None:
            send_json(self, 404, {"ok": False, "error": "not found"})
            return
        handler, arg = route
        try:
            if not self._host_allowed():
                raise ApiError(403, "unexpected Host")
            self._check_mutation()
            status, payload = handler(self._read_json(), *arg)
        except ApiError as e:
            status, payload = e.status, {"ok": False, "error": e.message}
            log.log(logging.WARNING if e.status >= 500 else logging.INFO,
                    "api.refused", extra={"path": self.path,
                                          "status": e.status,
                                          "reason": e.message})
        except Exception as e:
            log.error("api.crashed", exc_info=True, extra={"path": self.path})
            status = 500
            payload = {"ok": False,
                       "error": "internal error: %s: %s" % (type(e).__name__,
                                                            e)}
        send_json(self, status, payload)

    def _route(self):
        """(handler, extra args) for this POST path, or None."""
        exact = {
            "/api/delete": (self._do_delete_batch, ()),
            "/api/rename": (self._do_rename, ()),
            "/api/title": (self._do_title, ()),
        }
        if self.path in exact:
            return exact[self.path]
        for prefix, handler in (("/api/delete/", self._do_delete_one),
                                ("/api/transcribe/", self._do_transcribe),
                                ("/api/push/", self._do_push)):
            kind = self.path[len(prefix):]
            if self.path.startswith(prefix) and kind in ID_PATTERNS:
                return handler, (kind,)
        return None

    # -- handlers: (payload, *args) -> (status, response) ---------------------

    def _do_transcribe(self, payload, kind):
        """On-demand transcription: OCR a page or STT a session."""
        ident = asset_id(payload, kind)
        # the id must actually exist before we spend a long transcription
        if kind == "page":
            exists = os.path.isdir(os.path.join(paths.DATA, "data", ident))
        else:
            exists = bool(find_session_dirs(ident))
        if not exists:
            raise ApiError(404, "no such %s" % kind)
        argv = ["--ocr", "--page", ident] if kind == "page" \
            else ["--stt", "--session", ident]
        if not _run_convert_locked(argv):
            raise ApiError(500, "transcription failed")
        return 200, {"ok": True}

    def _do_push(self, payload, kind):
        """Push a page/session note to the Hermes echo-scribe webhook."""
        if not scribe._is_configured():
            raise ApiError(503, "Hermes push is not configured "
                                "(ECHO_SCRIBE_SECRET is not set)")
        ident = asset_id(payload, kind)
        try:
            index = scribe.load_index()
        except (OSError, ValueError) as e:
            raise ApiError(404, "no converted output yet; run ./run convert") \
                from e
        listed = index.get("pages" if kind == "page" else "sessions", [])
        if not any(a.get("id") == ident for a in listed):
            raise ApiError(404, "no such asset")
        try:
            ok, message = scribe.push_one(kind, ident)
        except Exception as e:
            raise ApiError(500, "push failed: %s" % e) from e
        if not ok:
            raise ApiError(502, message)
        return 200, {"ok": True, "message": message}

    def _do_delete_one(self, payload, kind):
        ident = asset_id(payload, kind)
        with run_lock():
            try:
                existed = delete_asset(kind, ident)
            finally:
                _reconcile()
        if not existed:
            raise ApiError(404, "no such %s" % kind)
        return 200, {"ok": True}

    def _do_rename(self, payload):
        """Assign a friendly display name to a notebook guid.

        Body: {"id": "<guid>", "name": "Field Notes"}. Stored in the
        repo-root notebook_names.json (atomic write), then a locked
        convert rebuilds index.json so every session card picks it up.
        Send name:"" to clear the alias back to the raw guid.
        """
        ident = payload.get("id")
        if not isinstance(ident, str) or not NOTEBOOK_ID_RE.fullmatch(ident):
            raise ApiError(400, "invalid notebook id")
        name = display_name(payload, 60)
        with run_lock():
            names = convert.load_notebook_names()
            if name:
                names[ident] = name
            else:
                names.pop(ident, None)
            write_json_file(convert.NOTEBOOK_NAMES_PATH, names)
            _reconcile()
        return 200, {"ok": True, "name": name}

    def _do_title(self, payload):
        """Assign a title to a single asset (page or recording).

        Body: {"id": "<page id or PRS-…>", "name": "Field sketch"} — the
        kind is inferred from the id format (both live in titles.json).
        Atomic write, then a locked convert rebuilds index.json.
        Send name:"" to clear the title.
        """
        ident = any_asset_id(payload)
        name = display_name(payload, 80)
        with run_lock():
            titles = convert.load_titles()
            if name:
                titles[ident] = name
            else:
                titles.pop(ident, None)
            write_json_file(convert.TITLES_PATH, titles)
            _reconcile()
        return 200, {"ok": True, "name": name}

    def _do_delete_batch(self, payload):
        """Delete many pages/sessions in one locked, reconciled pass.

        Body: {"items": [{"kind": "page"|"session", "id": "..."}, ...]}
        All ids are validated before anything is removed; state and output
        are reconciled once at the end, even when a delete fails midway.
        """
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            raise ApiError(400, "items must be a non-empty list")
        # bulk guard: large batches (e.g. "select all shown") need the
        # explicit typed confirmation the UI collects before sending
        if len(items) > BULK_DELETE_LIMIT and \
                payload.get("confirm") != "DELETE":
            raise ApiError(400, "bulk delete of %d items requires "
                                "{\"confirm\": \"DELETE\"} in the body "
                                "(type DELETE in the dialog to proceed)"
                                % len(items))
        targets = [batch_target(item) for item in items]
        deleted = 0
        with run_lock():
            try:
                for kind, ident in targets:
                    deleted += bool(delete_asset(kind, ident))
            except OSError as e:
                raise ApiError(500, "delete failed after %d item(s): %s"
                                    % (deleted, e)) from e
            finally:
                _reconcile()
        return 200, {"ok": True, "deleted": deleted,
                     "missing": len(targets) - deleted}


def batch_target(item):
    """(kind, id) of one batch-delete item; raises ApiError when invalid."""
    if not isinstance(item, dict) or item.get("kind") not in ID_PATTERNS:
        raise ApiError(400, "invalid item: %r" % (item,))
    return item["kind"], asset_id(item, item["kind"])


def write_json_file(path, data):
    try:
        convert.atomic_write(path, json.dumps(data, indent=2, sort_keys=True)
                             + "\n")
    except OSError as e:
        raise ApiError(500, "saving failed: %s" % e) from e


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (use 0.0.0.0 to expose on the LAN)")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    with http.server.ThreadingHTTPServer((args.host, args.port), Handler) as httpd:
        host, port = httpd.server_address[:2]
        log.info("serve.start", extra={"host": host, "port": port,
                                       "out": OUT,
                                       "lan_changes": bool(os.environ.get(
                                           "ECHO_ALLOW_LAN_DELETES"))})
        print("Serving notebook at http://%s:%d/  (output dir: %s)"
              % (host, port, OUT))
        httpd.serve_forever()


if __name__ == "__main__":
    main()
