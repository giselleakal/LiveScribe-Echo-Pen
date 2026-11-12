#!/usr/bin/env python3
"""Serve the converted notebook pages over HTTP.

Serves the contents of output/ as static files plus a generated index
page that lists pages (SVG previews) and audio sessions from index.json.

Usage:  ./src/serve.py [--port 8000] [--host 0.0.0.0]
"""
import argparse
import datetime
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
import scribe

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "output")
INDEX = os.path.join(OUT, "index.json")

# Path-traversal-safe id shapes (reject / \ .. leading '.', NUL, unicode.)
PAGE_ID_RE = re.compile(r"^\d+(\.\d+)*$")
SESSION_ID_RE = re.compile(r"^PRS-[0-9a-fA-F]+$")
# Batches larger than this require {"confirm": "DELETE"} in the request
# body (and the UI asks the user to type DELETE first) — so a stray
# "select all" + click can never wipe the whole library in one shot.
BULK_DELETE_LIMIT = 5
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
 header h1 {{ margin:0; font-size:clamp(22px, 4vw, 28px); letter-spacing:-0.02em; }}
 header h1 a {{ color:#fff; text-decoration:none; }}
 header p {{ margin:6px 0 0; opacity:.92; font-size:clamp(12px, 2vw, 14px); }}
 main {{ max-width:1080px; margin:0 auto; padding:0 clamp(14px, 4vw, 20px) 56px; }}
 img {{ max-width:100%; }}
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
 a.download, .transcribe, .delete, .push {{ font:inherit; cursor:pointer; border:none;
    border-radius:9px; padding:7px 13px; font-size:12px; font-weight:600;
    text-decoration:none; display:inline-flex; align-items:center; }}
 a.download, .transcribe {{ background:linear-gradient(135deg,var(--ink),var(--accent));
    color:#fff; }}
 a.download:hover, .transcribe:hover {{ filter:brightness(1.08); }}
 .transcribe:disabled {{ opacity:.65; cursor:progress; }}
 .delete {{ background:#fff; color:var(--danger); border:1px solid var(--danger); }}
 .delete:hover {{ background:var(--danger); color:#fff; }}
 .card {{ position:relative; }}
 .pick {{ position:absolute; top:10px; left:10px; z-index:3; width:18px; height:18px;
    cursor:pointer; accent-color:var(--accent); }}
 .card.selected {{ box-shadow:0 0 0 2px var(--accent); }}
 .selbar {{ display:flex; gap:10px; align-items:center; margin:16px 0 -4px;
    flex-wrap:wrap; font-size:13px; }}
 .selbar label {{ display:inline-flex; gap:6px; align-items:center; cursor:pointer;
    font-weight:600; color:var(--ink); }}
 .selbar .muted {{ background:#fff; border:1px solid var(--line); border-radius:9px;
    padding:6px 12px; font:inherit; font-size:12px; font-weight:600; cursor:pointer;
    color:var(--muted); }}
 .selbar .delete:disabled {{ opacity:.5; cursor:not-allowed; }}
 .push {{ background:linear-gradient(135deg,#047857,#0d9488); color:#fff; }}
 .push:hover {{ filter:brightness(1.08); }}
 .push:disabled {{ opacity:.65; cursor:progress; }}
 details, details.transcript {{ margin-top:12px; font-size:13px; color:#374151;
    border:1px solid var(--line); border-radius:10px; padding:0 12px; background:#fafbff; }}
 details summary {{ cursor:pointer; font-weight:650; padding:8px 0; color:var(--ink);
    user-select:none; }}
 details pre, details.transcript pre {{ white-space:pre-wrap; word-break:break-word;
    background:#fff; border:1px solid var(--line); border-radius:8px; padding:10px;
    margin:0 0 10px; }}
 audio {{ width:100%; margin-top:4px; border-radius:9px; }}
 .sess {{ display:block; }}
 .sess-head {{ display:flex; align-items:flex-start; gap:16px; flex-wrap:wrap; }}
 .sess-head .note {{ flex:1; min-width:220px; font-size:13px; color:#4b5563; }}
 .sess-head .note b {{ font-size:14px; color:#111827; }}
 .sess-head .note a {{ color:var(--ink); text-decoration:none; font-weight:600; }}
 .sess-head .note a:hover {{ text-decoration:underline; }}
 .sess-head .note .stamp {{ font-weight:600; color:#111827; }}
 .sess-head .actions {{ margin-left:auto; }}
 .players {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(250px,1fr));
    gap:8px; margin-top:12px; }}
 .players audio {{ width:100%; }}
 .part .plabel {{ font-size:11px; color:var(--muted); margin-bottom:3px; }}
 .page .actions, .sess-head .actions {{ display:flex; gap:8px; margin-top:0; flex-wrap:wrap; }}
 .page[hidden] {{ display:none; }}
 @media (max-width:760px) {{ .search {{ max-width:100%; margin-top:-20px; }}
   .grid {{ gap:16px; }} }}
 @media (max-width:680px) {{ header {{ padding:26px 18px 40px; }}
   .sess-head {{ flex-direction:column; align-items:stretch; }}
   .sess-head .actions {{ margin-left:0; }}
   .players {{ grid-template-columns:1fr; }} }}
 @media (max-width:460px) {{ header {{ padding:22px 16px 36px; }}
   .search {{ padding:6px; }} .search input {{ font-size:14px; }}
   .grid {{ grid-template-columns:1fr; gap:14px; }}
   .card {{ padding:14px; }}
   .page .actions, .sess-head .actions {{ flex-direction:column; align-items:stretch; }}
   a.download, .transcribe, .delete, .push {{ width:100%; justify-content:center; }}
   h2 {{ margin:24px 0 10px; }} }}
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
<div class="selbar">
 <label><input type="checkbox" id="sel-all"> Select all shown</label>
 <button class="muted" id="sel-clear">Clear</button>
 <button class="delete" id="sel-del" disabled>Delete selected (<span id="sel-count">0</span>)</button>
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
<script>{sel_js}</script>
<script>{transcribe_js}</script>
<script>{push_js}</script>
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

SEL_JS = """
(function () {
  var selAll = document.getElementById('sel-all');
  var selClear = document.getElementById('sel-clear');
  var selDel = document.getElementById('sel-del');
  var selCount = document.getElementById('sel-count');
  if (!selAll || !selDel) return;
  var boxes = Array.prototype.slice.call(
      document.querySelectorAll('.pickbox'));

  function selected() {
    return boxes.filter(function (b) { return b.checked && !b.closest('[hidden]'); });
  }
  function refresh() {
    var n = selected().length;
    selCount.textContent = n;
    selDel.disabled = (n === 0);
  }
  boxes.forEach(function (b) {
    b.addEventListener('change', function () {
      var card = b.closest('.page') || b.closest('.sess');
      if (card) card.classList.toggle('selected', b.checked);
      refresh();
    });
  });
  selAll.addEventListener('change', function () {
    boxes.forEach(function (b) {
      var card = b.closest('.page') || b.closest('.sess');
      var shown = card && !card.hidden;
      if (shown) b.checked = selAll.checked;
      if (card) card.classList.toggle('selected', b.checked);
    });
    refresh();
  });
  selClear.addEventListener('click', function () {
    boxes.forEach(function (b) { b.checked = false; });
    document.querySelectorAll('.card.selected').forEach(function (c) {
      c.classList.remove('selected');
    });
    selAll.checked = false;
    refresh();
  });
  selDel.addEventListener('click', function () {
    var items = selected().map(function (b) {
      return { kind: b.getAttribute('data-kind'), id: b.getAttribute('data-id') };
    });
    if (!items.length) return;
    var kinds = {};
    items.forEach(function (i) { kinds[i.kind] = (kinds[i.kind] || 0) + 1; });
    var desc = Object.keys(kinds).map(function (k) {
      return kinds[k] + ' ' + (k === 'page' ? 'page(s)' : 'recording(s)');
    }).join(', ');
    var body = { items: items };
    if (items.length > 5) {
      // matches BULK_DELETE_LIMIT in serve.py: big selections need a
      // typed confirmation so the library can't be wiped by a slip
      var typed = prompt('You selected ' + items.length + ' items.\\n' +
        'This permanently deletes them from the notebook AND the raw pen\\n' +
        'data - it cannot be undone.\\n\\nType DELETE (all caps) to proceed:');
      if (typed !== 'DELETE') return;
      body.confirm = 'DELETE';
    } else if (!confirm('Delete ' + desc + '? This cannot be undone.')) {
      return;
    }
    selDel.disabled = true;
    var req = fetch('/api/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    req.then(function (r) { return r.json(); }).then(function (res) {
      if (res.ok) { location.reload(); return; }
      selDel.disabled = false;
      alert(res.error || 'Delete failed.');
    }).catch(function () {
      selDel.disabled = false;
      alert('Delete failed (network error).');
    });
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

PUSH_JS = """
(function () {
  document.addEventListener('click', function (e) {
    var btn = e.target.closest ? e.target.closest('.push') : null;
    if (!btn) return;
    e.preventDefault();
    var kind = btn.getAttribute('data-kind');
    var id = btn.getAttribute('data-id');
    var old = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Sending\u2026';
    fetch('/api/push/' + kind, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: id })
    }).then(function (r) { return r.json(); }).then(function (res) {
      btn.disabled = false;
      btn.textContent = old;
      if (res.ok) { alert(res.message || 'Sent to Hermes.'); }
      else { alert(res.error || 'Push failed.'); }
    }).catch(function () {
      btn.disabled = false;
      btn.textContent = old;
      alert('Push failed (network error).');
    });
  });
})();
"""


def _fmt_when(iso, iso_end=None):
    """Human capture-time string from an index.json ISO timestamp.

    e.g. "Nov 10, 2026, 10:16 AM", or with an end time on the same day:
    "Nov 10, 2026, 10:16 - 10:34 AM". Empty string when missing/unparseable.
    """
    if not iso:
        return ""
    try:
        dt = datetime.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return ""
    start = re.sub(r" 0(\d)", r" \1", dt.strftime("%b %d, %Y, %I:%M %p"))
    if not iso_end:
        return start
    try:
        dt2 = datetime.datetime.strptime(iso_end, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return start
    if dt.date() != dt2.date():
        end = re.sub(r" 0(\d)", r" \1", dt2.strftime("%b %d, %Y, %I:%M %p"))
        return "%s - %s" % (start, end)
    # same day: abbreviate to just the clock range
    ampm = dt2.strftime(" %p")
    return "%s - %s%s" % (start, dt2.strftime("%I:%M").lstrip("0"), ampm)


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
        when = _fmt_when(p.get("captured"), p.get("captured_end"))
        search = ("%s %s %s" % (p.get("id", ""), when, text)).lower()
        delete = ('<button class="delete" data-kind="page" data-id="%s">Delete</button>'
                  % html.escape(str(p["id"]), quote=True))
        push = ('<button class="push" data-kind="page" data-id="%s">Send to Hermes</button>'
                % html.escape(str(p["id"]), quote=True))
        transcribe = ""
        if not p.get("ocr"):
            transcribe = ('<button class="transcribe" data-kind="page" '
                          'data-id="%s">Transcribe</button>'
                          % html.escape(str(p["id"]), quote=True))
        cards.append(
            '<div class="card page" data-search="{search}"><label class="pick">'
            '<input type="checkbox" class="pickbox" data-kind="page" data-id="{id}"></label>'
            '<a class="thumb" '
            'href="{svg}"><img src="{thumb}" alt="{id}">'
            '<div class="id">{id}</div>'
            '<div class="meta">{when}{stroke_meta}</div></a>'
            '<div class="actions">{download}{transcribe}{delete}{push}</div>'
            '{transcript}</div>'
            .format(
                search=html.escape(search, quote=True),
                svg=html.escape(p["svg"], quote=True),
                thumb=html.escape(p["thumb"], quote=True),
                id=html.escape(str(p["id"])),
                when=(html.escape(when) + " &middot; ") if when else "",
                stroke_meta=("%d strokes" % p["strokes"]),
                download=download,
                transcribe=transcribe,
                delete=delete,
                push=push,
                transcript=transcript,
            )
        )
    sesses = []
    for s in data.get("sessions", []):
        urls = s.get("urls") or []
        if not urls:
            continue
        if len(urls) == 1:
            players = ('<audio controls src="%s"></audio>'
                       % html.escape(urls[0], quote=True))
        else:
            parts = []
            for i, u in enumerate(urls, 1):
                parts.append(
                    '<div class="part"><div class="plabel">Part %d</div>'
                    '<audio controls src="%s"></audio></div>'
                    % (i, html.escape(u, quote=True)))
            players = "".join(parts)
        parts = " (%d parts)" % len(urls) if len(urls) > 1 else ""
        when = _fmt_when(s.get("captured"), s.get("captured_end"))
        note = html.escape(str(s.get("id", "")))
        if when:
            note += " &middot; <span class=\"stamp\">" + html.escape(when) + "</span>"
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
        push = ('<button class="push" data-kind="session" data-id="%s">Send to Hermes</button>'
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
        search = ("%s %s %s%s"
                  % (s.get("id", ""), when, s.get("notebook", ""),
                     tx_search)).lower()
        transcribe = ""
        if not transcript:
            transcribe = ('<button class="transcribe" data-kind="session" '
                          'data-id="%s">Transcribe</button>'
                          % html.escape(str(s.get("id", "")), quote=True))
        sesses.append(
            '<div class="card sess" data-search="{search}">'
            '<label class="pick"><input type="checkbox" class="pickbox" '
            'data-kind="session" data-id="{sid}"></label>'
            '<div class="sess-head"><div class="note">{note}</div>'
            '<div class="actions">{delete}{transcribe}{push}</div></div>'
            '<div class="players">{players}</div>{tx}</div>'
            .format(
                search=html.escape(search, quote=True),
                note=note,
                sid=html.escape(str(s.get("id", "")), quote=True),
                delete=delete,
                transcribe=transcribe,
                push=push,
                players=players,
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
        sel_js=SEL_JS,
        transcribe_js=TRANSCRIBE_JS,
        push_js=PUSH_JS,
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

    def _do_push(self):
        """Push a page/session note to the Hermes echo-scribe webhook."""
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
        if not scribe._is_configured():
            send_json(self, 503,
                      {"ok": False,
                       "error": "Hermes push is not configured "
                                "(ECHO_SCRIBE_SECRET is not set)"})
            return
        kind = self.path[len("/api/push/"):]
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
        # cheap existence check before doing work
        index = scribe.load_index()
        exists = any(p.get("id") == ident for p in index.get("pages", [])) \
            if kind == "page" else \
            any(s.get("id") == ident for s in index.get("sessions", []))
        if not exists:
            send_json(self, 404, {"ok": False, "error": "no such asset"})
            return
        try:
            ok, message = scribe.push_one(kind, ident)
        except Exception as e:
            send_json(self, 500,
                      {"ok": False, "error": "push failed: %s" % e})
            return
        send_json(self, 200 if ok else 502,
                  {"ok": ok, "message": message})

    def _post_guards(self):
        """Common POST guards for mutating APIs.

        Returns an error dict to send, or None when the request may proceed.
        """
        if "application/json" not in self.headers.get("Content-Type", ""):
            return {"ok": False,
                    "error": "Content-Type must be application/json"}
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if origin and origin not in ("http://" + host, "https://" + host,
                                     "null"):
            return {"ok": False, "error": "cross-origin request denied"}
        if self.server.server_address[0] not in LOOPBACK_HOSTS and \
                not os.environ.get("ECHO_ALLOW_LAN_DELETES"):
            return {"ok": False,
                    "error": "Deletes disabled on a network host. Set "
                             "ECHO_ALLOW_LAN_DELETES=1 to enable."}
        return None

    def do_POST(self):
        if self.path.startswith("/api/push/"):
            return self._do_push()
        if self.path.startswith("/api/transcribe/"):
            return self._do_transcribe()
        if self.path == "/api/delete":
            return self._do_delete_batch()
        if not self.path.startswith("/api/delete/"):
            send_json(self, 404, {"ok": False, "error": "not found"})
            return

        guard = self._post_guards()
        if guard:
            send_json(self, 403, guard)
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

    def _do_delete_batch(self):
        """Delete many pages/sessions in one locked, reconciled pass.

        Body: {"items": [{"kind": "page"|"session", "id": "..."}, ...]}
        All ids are validated before anything is removed; state and output
        are reconciled once at the end.
        """
        guard = self._post_guards()
        if guard:
            send_json(self, 403, guard)
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
            items = payload["items"]
        except (ValueError, KeyError, IndexError) as e:
            send_json(self, 400,
                      {"ok": False, "error": "bad JSON body: %s" % e})
            return
        if not isinstance(items, list) or not items:
            send_json(self, 400, {"ok": False, "error": "items must be a "
                                                        "non-empty list"})
            return
        # bulk guard: large batches (e.g. "select all shown") need the
        # explicit typed confirmation the UI collects before sending
        if len(items) > BULK_DELETE_LIMIT and \
                payload.get("confirm") != "DELETE":
            send_json(self, 400,
                      {"ok": False,
                       "error": "bulk delete of %d items requires "
                                "{\"confirm\": \"DELETE\"} in the body "
                                "(type DELETE in the dialog to proceed)"
                                % len(items)})
            return

        # validate every item up front so a malformed batch removes nothing
        for item in items:
            if not isinstance(item, dict):
                send_json(self, 400, {"ok": False, "error": "each item must "
                                                            "be an object"})
                return
            ident = item.get("id")
            kind = item.get("kind")
            if kind not in ("page", "session") or not isinstance(ident, str):
                send_json(self, 400, {"ok": False, "error": "invalid item: "
                                                            "%r" % (item,)})
                return
            valid = PAGE_ID_RE.match(ident) if kind == "page" \
                else SESSION_ID_RE.match(ident)
            if not valid:
                send_json(self, 400, {"ok": False, "error": "invalid id "
                                                            "format: %r" % ident})
                return

        did_lock = convert.acquire_lock(wait=10.0)
        try:
            try:
                for item in items:
                    if item["kind"] == "page":
                        delete_page(item["id"])
                    else:
                        delete_session(item["id"])
            except OSError as e:
                send_json(self, 500,
                          {"ok": False, "error": "delete failed: %s" % e})
                return
            _reconcile()
        finally:
            if did_lock:
                convert.release_lock()
        send_json(self, 200, {"ok": True, "deleted": len(items)})


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