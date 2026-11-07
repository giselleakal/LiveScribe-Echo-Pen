#!/usr/bin/env python3
"""Serve the converted notebook pages over HTTP.

Serves the contents of output/ as static files plus a generated index
page that lists pages (SVG previews) and audio sessions from index.json.

Usage:  ./src/serve.py [--port 8000] [--host 0.0.0.0]
"""
import argparse
import glob
import html
import json
import http.server
import os
import re
import shutil
import subprocess
import sys
import urllib.parse

# Core pipeline helpers (state load/save, the run lock). This imports only
# stdlib + the vendored parsestf/ocr at module load; cairo and torch stay
# lazy inside convert.py/ocr.py, so serving stays dependency-light.
import convert

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "output")
INDEX = os.path.join(OUT, "index.json")

# Path-traversal-safe id shapes (reject / \ .. leading '.', NUL, unicode.)
PAGE_ID_RE = re.compile(r"^\d+(\.\d+)*$")
SESSION_ID_RE = re.compile(r"^PRS-[0-9a-fA-F]+$")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

PAGE_TPL = """<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Notebook</title>
<style>
 :root {{ --ink:#1a237e; --accent:#7c3aed; --bg:#f3f4f7; --card:#fff;
    --muted:#6b7280; --line:#e5e7eb; --danger:#b3261e; }}
 * {{ box-sizing: border-box; }}
 body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica,
    Arial, sans-serif; margin:0; background: var(--bg); color:#1f2937; line-height:1.55; }}
 header {{ padding:34px 24px 46px; color:#fff;
    background: linear-gradient(135deg, #1a237e 0%, #3949ab 48%, #7c3aed 100%); }}
 header .inner {{ max-width:1080px; margin:0 auto; }}
 header h1 {{ margin:0; font-size:26px; letter-spacing:-0.02em; }}
 header h1 a {{ color:#fff; text-decoration:none; }}
 header p {{ margin:6px 0 0; opacity:.92; font-size:14px; }}
 main {{ max-width:1080px; margin:0 auto; padding:0 20px 56px; }}
 .search {{ display:flex; gap:12px; align-items:center; margin:-22px auto 0; max-width:620px;
    background: var(--card); border:1px solid var(--line); border-radius:14px;
    padding:6px 8px 6px 16px; box-shadow:0 6px 20px rgba(30,41,59,.10);
    position:relative; z-index:2; }}
 .search input {{ flex:1; border:none; outline:none; font-size:15px; padding:8px 0;
    background:transparent; color:#1f2937; }}
 .search input::placeholder {{ color:var(--muted); }}
 .search #count {{ font-size:12px; color:var(--accent); font-weight:600; white-space:nowrap;
    background:#f1efff; border-radius:999px; padding:5px 11px; }}
 h2 {{ font-size:16px; font-weight:700; margin:32px 0 14px; color:#111827;
    display:flex; align-items:center; gap:10px; }}
 h2 .bar {{ width:4px; height:18px; border-radius:3px; background:var(--accent); }}
 h2 .n {{ color:var(--muted); font-weight:600; font-size:12px; background:#eceffd;
    border-radius:999px; padding:3px 10px; }}
 .grid {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(230px,1fr)); gap:22px;
    margin-top:16px; }}
 .card {{ background:var(--card); border:1px solid var(--line); border-radius:16px;
    padding:18px; box-shadow:0 1px 2px rgba(30,41,59,.05);
    transition: transform .16s ease, box-shadow .16s ease; }}
 .card:hover {{ transform: translateY(-3px); box-shadow:0 14px 28px rgba(30,41,59,.12); }}
 .page a.thumb {{ display:block; text-align:center; }}
 .page img {{ width:100%; border-radius:10px; border:1px solid var(--line); }}
 .page .id {{ font-size:13.5px; font-weight:650; color:#111827; margin-top:11px; }}
 .page .meta {{ font-size:12px; color:var(--muted); margin-top:2px; }}
 .page .actions, .sess .actions {{ display:flex; gap:8px; margin-top:12px; flex-wrap:wrap; }}
 a.download, .transcribe, .delete {{ font:inherit; cursor:pointer; border:none;
    border-radius:9px; padding:7px 13px; font-size:12px; font-weight:600;
    text-decoration:none; display:inline-flex; align-items:center; }}
 a.download, .transcribe {{ background:linear-gradient(135deg,var(--ink),var(--accent));
    color:#fff; }}
 a.download:hover, .transcribe:hover {{ filter:brightness(1.08); }}
 .transcribe:disabled {{ opacity:.65; cursor:progress; }}
 .delete {{ background:#fff; color:var(--danger); border:1px solid var(--danger); }}
 .delete:hover {{ background:var(--danger); color:#fff; }}
 details, details.transcript {{ margin-top:12px; font-size:13px; color:#374151;
    border:1px solid var(--line); border-radius:10px; padding:0 12px; background:#fafbff; }}
 details summary {{ cursor:pointer; font-weight:650; padding:8px 0; color:var(--ink);
    user-select:none; }}
 details pre, details.transcript pre {{ white-space:pre-wrap; word-break:break-word;
    background:#fff; border:1px solid var(--line); border-radius:8px; padding:10px;
    margin:0 0 10px; }}
 audio {{ width:100%; margin-top:4px; border-radius:9px; }}
 .sess {{ display:flex; gap:18px; align-items:center; }}
 .sess .note {{ flex:1; min-width:0; font-size:13px; color:#4b5563; }}
 .sess .note b {{ font-size:14px; color:#111827; }}
 .sess .note a {{ color:var(--ink); text-decoration:none; font-weight:600; }}
 .sess .note a:hover {{ text-decoration:underline; }}
 .sess audio {{ max-width:420px; }}
 .sess .actions {{ margin-left:auto; margin-top:0; }}
 .page[hidden] {{ display:none; }}
 @media (max-width:680px) {{ header {{ padding:26px 18px 40px; }}
   .sess {{ flex-direction:column; align-items:stretch; }}
   .sess audio {{ max-width:100%; }} .sess .actions {{ margin-left:0; margin-top:10px; }} }}
</style>
</head><body>
<header><div class="inner">
 <h1><a href="/">Notebook</a></h1>
 <p>Livescribe Echo &mdash; notes &amp; recordings</p>
</div></header>
<main>
<div class="search">
 <input id="q" type="search" placeholder="Search pages and transcriptions..." autocomplete="off">
 <span id="count"></span>
</div>
<h2><span class="bar"></span>Pages <span class="n">{n_pages}</span></h2>
<div class="grid">
 {pages}
</div>
<h2><span class="bar"></span>Recordings <span class="n">{n_sessions}</span></h2>
{sessions}
</main>
<script>{search_js}</script>
<script>{del_js}</script>
<script>{transcribe_js}</script>
</body></html>
"""

SEARCH_JS = """
(function () {
  var q = document.getElementById('q');
  var count = document.getElementById('count');
  if (!q) return;
  var cards = Array.prototype.slice.call(
      document.querySelectorAll('.page, .sess'));
  function apply() {
    var term = q.value.trim().toLowerCase();
    var shown = 0;
    cards.forEach(function (card) {
      var haystack = (card.getAttribute('data-search') || '').toLowerCase();
      var match = !term || haystack.indexOf(term) !== -1;
      card.hidden = !match;
      if (match) shown++;
    });
    count.textContent = term ? shown + ' of ' + cards.length + ' matches' : '';
  }
  q.addEventListener('input', apply);
  apply();
})();
"""

DEL_JS = """
(function () {
  document.addEventListener('click', function (e) {
    var btn = e.target.closest ? e.target.closest('.delete') : null;
    if (!btn) return;
    e.preventDefault();
    var kind = btn.getAttribute('data-kind');
    var id = btn.getAttribute('data-id');
    var what = kind === 'session' ? 'recording' : 'page';
    if (!confirm('Delete this ' + what + ' (' + id + ')? This cannot be undone.')) return;
    var req = fetch('/api/delete/' + kind, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: id })
    });
    req.then(function (r) { return r.json(); }).then(function (res) {
      if (res.ok) {
        var card = btn.closest('.page') || btn.closest('.sess');
        if (card) card.parentNode.removeChild(card);
      } else {
        alert(res.error || 'Delete failed.');
      }
    }).catch(function () { alert('Delete failed.'); });
  });
})();
"""

TRANSCRIBE_JS = """
(function () {
  document.addEventListener('click', function (e) {
    var btn = e.target.closest ? e.target.closest('.transcribe') : null;
    if (!btn) return;
    e.preventDefault();
    var kind = btn.getAttribute('data-kind');
    var id = btn.getAttribute('data-id');
    if (!confirm('Transcribe this ' + (kind === 'session' ? 'recording'
        : 'page') + ' (' + id + ')? This may take a little while.')) return;
    var old = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Transcribing\u2026';
    fetch('/api/transcribe/' + kind, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: id })
    }).then(function (r) { return r.json(); }).then(function (res) {
      if (res.ok) { location.reload(); return; }
      btn.disabled = false;
      btn.textContent = old;
      alert(res.error || 'Transcription failed.');
    }).catch(function () {
      btn.disabled = false;
      btn.textContent = old;
      alert('Transcription failed.');
    });
  });
})();
"""


def build_index():
    """Build the index page.

    Falls back to the last successfully built page when index.json is
    missing or mid-write (concurrent ./run sync while ./run serve is up).
    """
    global _LAST_GOOD
    try:
        with open(INDEX) as f:
            data = json.load(f)
    except (OSError, ValueError):
        if _LAST_GOOD is not None:
            return _LAST_GOOD
        return "<h2>No converted output yet. Run ./run</h2>"

    pages = data.get("pages", [])
    cards = []
    for p in pages:
        text = p.get("text") or ""
        transcript = ""
        if text:
            transcript = (
                '<details><summary>Transcription</summary><pre>%s</pre></details>'
                % html.escape(text)
            )
        download = ""
        if text:
            download = (
                '<a class="download" href="{txt}" download>Download .txt</a>'
                .format(txt=html.escape(
                    "pages/%s/text.txt" % p["id"], quote=True))
            )
        search = ("%s %s" % (p.get("id", ""), text)).lower()
        delete = ('<button class="delete" data-kind="page" data-id="%s">Delete</button>'
                  % html.escape(str(p["id"]), quote=True))
        transcribe = ""
        if not p.get("ocr"):
            transcribe = ('<button class="transcribe" data-kind="page" '
                          'data-id="%s">Transcribe</button>'
                          % html.escape(str(p["id"]), quote=True))
        cards.append(
            '<div class="page" data-search="{search}"><a class="thumb" '
            'href="{svg}"><img src="{thumb}" alt="{id}">'
            '<div class="id">{id}</div><div class="meta">{strokes} strokes</div></a>'
            '<div class="actions">{download}{transcribe}{delete}</div>'
            '{transcript}</div>'
            .format(
                search=html.escape(search, quote=True),
                svg=html.escape(p["svg"], quote=True),
                thumb=html.escape(p["thumb"], quote=True),
                id=html.escape(str(p["id"])),
                strokes=p["strokes"],
                download=download,
                transcribe=transcribe,
                delete=delete,
                transcript=transcript,
            )
        )
    sesses = []
    for s in data.get("sessions", []):
        urls = s.get("urls") or []
        if not urls:
            continue
        players = "".join('<audio controls src="%s"></audio>'
                          % html.escape(u, quote=True) for u in urls)
        parts = " (%d parts)" % len(urls) if len(urls) > 1 else ""
        note = html.escape(str(s.get("id", "")))
        if s.get("notebook"):
            note += " &middot; notebook " + html.escape(str(s["notebook"]))
        note += html.escape(parts)
        linked = s.get("pages") or []
        if linked:
            links = " ".join(
                '<a href="%s">%s</a>' % (
                    html.escape("pages/%s/page.svg" % pid, quote=True),
                    html.escape(str(pid)))
                for pid in linked)
            note += '<br>Pages: <span class="pglinks">%s</span>' % links
        delete = ('<button class="delete" data-kind="session" data-id="%s">Delete</button>'
                  % html.escape(str(s.get("id", "")), quote=True))
        transcript = (s.get("transcript") or "").strip()
        tx_block = ""
        tx_search = ""
        if transcript:
            tx_block = (
                '<details class="transcript"><summary>Transcript</summary>'
                '<pre>%s</pre></details>'
                '<a class="download" href="%s" download>Download .txt</a>'
                % (html.escape(transcript),
                   html.escape("audio/%s.txt" % s.get("id", ""), quote=True))
            )
            tx_search = " " + transcript
        search = ("%s %s%s"
                  % (s.get("id", ""), s.get("notebook", ""), tx_search)).lower()
        transcribe = ""
        if not transcript:
            transcribe = ('<button class="transcribe" data-kind="session" '
                          'data-id="%s">Transcribe</button>'
                          % html.escape(str(s.get("id", "")), quote=True))
        sesses.append(
            '<div class="card sess" data-search="{search}">'
            '<div class="note">{note}</div>{players}'
            '<div class="actions">{delete}{transcribe}</div>{tx}</div>'
            .format(
                search=html.escape(search, quote=True),
                note=note,
                players=players,
                delete=delete,
                transcribe=transcribe,
                tx=tx_block,
            )
        )
    page = PAGE_TPL.format(
        n_pages=len(pages),
        n_sessions=len(sesses),
        pages="\n".join(cards),
        sessions="\n".join(sesses),
        search_js=SEARCH_JS,
        del_js=DEL_JS,
        transcribe_js=TRANSCRIBE_JS,
    )
    _LAST_GOOD = page
    return page


_LAST_GOOD = None


# ---------------------------------------------------------------------------
# delete API
# ---------------------------------------------------------------------------

def find_session_dirs(session_id):
    """Return every raw pen session directory for ``session_id``."""
    replay = os.path.join(ROOT, "data", "userdata")
    return glob.glob(os.path.join(
        replay, "*", "Paper Replay", "*", "*", "sessions", session_id))


def _reconcile():
    """Re-run convert so index.json, output pruning, and state stay correct.

    Runs with ECHO_LOCK_HELD so the child skips locking (the caller holds
    the run lock). Output is regenerated atomically, so a concurrent GET
    never sees a torn file.
    """
    env = dict(os.environ)
    env["ECHO_LOCK_HELD"] = "1"
    subprocess.run(
        [sys.executable, os.path.join(ROOT, "src", "convert.py")],
        env=env, check=False)


def delete_page(page_id):
    """Remove a page everywhere: raw dump, output, and incremental state."""
    shutil.rmtree(os.path.join(OUT, "pages", page_id), ignore_errors=True)
    shutil.rmtree(os.path.join(ROOT, "data", "data", page_id),
                  ignore_errors=True)
    state = convert.load_state()
    state["page_entries"].pop(page_id, None)
    state["sources"] = {
        k: v for k, v in state["sources"].items()
        if not k.startswith("data/data/%s/" % page_id)}
    convert.save_state(state)


def delete_session(session_id):
    """Remove a recording everywhere: raw session dir, output audio, state."""
    for d in find_session_dirs(session_id):
        shutil.rmtree(d, ignore_errors=True)
    for f in glob.glob(os.path.join(OUT, "audio", session_id + ".*.aac")):
        try:
            os.remove(f)
        except OSError:
            pass
    try:
        os.remove(os.path.join(OUT, "audio", session_id + ".txt"))
    except OSError:
        pass
    state = convert.load_state()
    state["session_entries"].pop(session_id, None)
    state["sources"] = {
        k: v for k, v in state["sources"].items()
        if ("/sessions/%s/" % session_id) not in k}
    convert.save_state(state)


def _run_convert_locked(argv):
    """Run ``convert.py`` with ``argv`` under the run lock.

    Sets ``ECHO_LOCK_HELD`` so the child skips locking (the caller holds
    it). Returns True on success.
    """
    did_lock = convert.acquire_lock(wait=10.0)
    try:
        env = dict(os.environ)
        env["ECHO_LOCK_HELD"] = "1"
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "src", "convert.py")] + argv,
            env=env, check=False)
        return proc.returncode == 0
    finally:
        if did_lock:
            convert.release_lock()


def send_json(handler, status, payload):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=OUT, **kwargs)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/" or path == "":
            body = build_index().encode("utf-8")
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

    def _do_transcribe(self):
        """On-demand transcription: OCR a page or STT a session."""
        if "application/json" not in self.headers.get("Content-Type", ""):
            send_json(self, 400,
                      {"ok": False, "error": "Content-Type must be "
                                             "application/json"})
            return
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if origin and origin not in ("http://" + host, "https://" + host,
                                     "null"):
            send_json(self, 403,
                      {"ok": False, "error": "cross-origin request denied"})
            return
        kind = self.path[len("/api/transcribe/"):]
        if kind not in ("page", "session"):
            send_json(self, 404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            send_json(self, 400, {"ok": False, "error": "empty request body"})
            return
        try:
            ident = json.loads(
                self.rfile.read(length).decode("utf-8"))["id"]
        except (ValueError, KeyError, IndexError) as e:
            send_json(self, 400,
                      {"ok": False, "error": "bad JSON body: %s" % e})
            return
        valid = PAGE_ID_RE.match(ident) if kind == "page" \
            else SESSION_ID_RE.match(ident)
        if not valid:
            send_json(self, 400,
                      {"ok": False, "error": "invalid id format"})
            return
        # the id must actually exist before we spend a long transcription
        if kind == "page":
            if not os.path.isdir(os.path.join(ROOT, "data", "data", ident)):
                send_json(self, 404,
                          {"ok": False, "error": "no such page"})
                return
        else:
            if not find_session_dirs(ident):
                send_json(self, 404,
                          {"ok": False, "error": "no such session"})
                return
        argv = ["--ocr", "--page", ident] if kind == "page" \
            else ["--stt", "--session", ident]
        if not _run_convert_locked(argv):
            send_json(self, 500,
                      {"ok": False, "error": "transcription failed"})
            return
        send_json(self, 200, {"ok": True})

    def do_POST(self):
        if self.path.startswith("/api/transcribe/"):
            return self._do_transcribe()
        if not self.path.startswith("/api/delete/"):
            send_json(self, 404, {"ok": False, "error": "not found"})
            return

        # content-type must be JSON
        if "application/json" not in self.headers.get("Content-Type", ""):
            send_json(self, 400,
                      {"ok": False, "error": "Content-Type must be "
                                             "application/json"})
            return
        # same-origin guard against CSRF (Origin, when present, must be us)
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if origin and origin not in ("http://" + host, "https://" + host,
                                     "null"):
            send_json(self, 403,
                      {"ok": False, "error": "cross-origin request denied"})
            return
        # deletes are disabled on non-loopback hosts unless explicitly enabled
        if self.server.server_address[0] not in LOOPBACK_HOSTS and \
                not os.environ.get("ECHO_ALLOW_LAN_DELETES"):
            send_json(self, 403, {"ok": False, "error":
                                  "Deletes disabled on a network host. Set "
                                  "ECHO_ALLOW_LAN_DELETES=1 to enable."})
            return

        kind = self.path[len("/api/delete/"):]
        if kind not in ("page", "session"):
            send_json(self, 404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            send_json(self, 400, {"ok": False, "error": "empty request body"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            ident = payload["id"]
        except (ValueError, KeyError, IndexError) as e:
            send_json(self, 400,
                      {"ok": False, "error": "bad JSON body: %s" % e})
            return

        # validate the id strictly; rejects traversal, NUL, unicode tricks
        valid = PAGE_ID_RE.match(ident) if kind == "page" \
            else SESSION_ID_RE.match(ident)
        if not valid:
            send_json(self, 400,
                      {"ok": False, "error": "invalid id format"})
            return

        did_lock = convert.acquire_lock(wait=10.0)
        try:
            try:
                if kind == "page":
                    delete_page(ident)
                else:
                    delete_session(ident)
            except OSError as e:
                send_json(self, 500,
                          {"ok": False, "error": "delete failed: %s" % e})
                return
            _reconcile()
        finally:
            if did_lock:
                convert.release_lock()
        send_json(self, 200, {"ok": True})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (use 0.0.0.0 to expose on the LAN)")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    with http.server.ThreadingHTTPServer((args.host, args.port), Handler) as httpd:
        print("Serving notebook at http://%s:%d/  (output dir: %s)"
              % (args.host, args.port, OUT))
        httpd.serve_forever()


if __name__ == "__main__":
    main()