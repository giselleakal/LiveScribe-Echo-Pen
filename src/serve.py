#!/usr/bin/env python3
"""Serve the converted notebook pages over HTTP.

Serves the contents of output/ as static files plus a generated index
page that lists pages (SVG previews) and audio sessions from index.json.

Usage:  ./src/serve.py [--port 8000] [--host 0.0.0.0]
"""
import argparse
import contextlib
import datetime
import glob
import html
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
 .sess-head .note .rtitle {{ font-weight:650; color:#111827; }}
 .sess-head .note .sid {{ color:var(--muted); font-size:12px; }}
 .page .id .psid {{ color:var(--muted); font-size:11px; font-weight:500; }}
 .rename, .titleedit {{ background:none; border:none; cursor:pointer; padding:0 2px 0 6px;
    font:inherit; font-size:12px; color:var(--muted); opacity:.55; }}
 .rename:hover, .titleedit:hover {{ opacity:1; color:var(--accent); }}
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
<script>{rename_js}</script>
<script>{title_js}</script>
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
    document.dispatchEvent(new Event('echo:filtered'));
  }
  q.addEventListener('input', apply);
  apply();
})();
"""

DEL_JS = """
(function () {
  document.addEventListener('click', function (e) {
    var btn = e.target.closest ? e.target.closest('.delete[data-id]') : null;
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

RENAME_JS = """
(function () {
  document.addEventListener('click', function (e) {
    var btn = e.target.closest ? e.target.closest('.rename') : null;
    if (!btn) return;
    e.preventDefault();
    var id = btn.getAttribute('data-nb');
    var cur = btn.getAttribute('data-cur') || id;
    var name = prompt('Rename notebook ' + cur + ' to:', cur);
    if (name === null) return;
    name = name.trim();
    if (!name) {
      if (!confirm('Clear the notebook name and show the raw id again?')) return;
    }
    var req = fetch('/api/rename', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: id, name: name })
    });
    req.then(function (r) { return r.json(); }).then(function (res) {
      if (res.ok) {
        window.location.reload();
      } else {
        alert(res.error || 'Rename failed.');
      }
    }).catch(function () { alert('Rename failed.'); });
  });
})();
"""

TITLE_JS = """
(function () {
  document.addEventListener('click', function (e) {
    var btn = e.target.closest ? e.target.closest('.titleedit') : null;
    if (!btn) return;
    e.preventDefault();
    var id = btn.getAttribute('data-id');
    var cur = btn.getAttribute('data-cur') || '';
    var what = /^PRS-/.test(id) ? 'recording' : 'page';
    var ask = cur
      ? 'Change the title of ' + what + ' ' + id + ' to:'
      : 'Give ' + what + ' ' + id + ' a title:';
    var name = prompt(ask, cur);
    if (name === null) return;
    name = name.trim();
    if (!name && cur) {
      if (!confirm('Clear the title?')) return;
    }
    if (!name && !cur) return;
    var req = fetch('/api/title', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: id, name: name })
    });
    req.then(function (r) { return r.json(); }).then(function (res) {
      if (res.ok) {
        window.location.reload();
      } else {
        alert(res.error || 'Title failed.');
      }
    }).catch(function () { alert('Title failed.'); });
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
    return boxes.filter(function (b) {
      return b.checked && document.contains(b) && !b.closest('[hidden]');
    });
  }
  document.addEventListener('echo:filtered', function () { refresh(); });
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
        title = str(p.get("title") or "")
        pid_txt = html.escape(str(p["id"]))
        title_btn = ('<button class="titleedit" data-id="%s" data-cur="%s" '
                     'title="Title this page">&#9998;</button>'
                     % (pid_txt, html.escape(title, quote=True)))
        if title:
            id_html = ('%s <span class="psid">(%s)</span>%s'
                       % (html.escape(title), pid_txt, title_btn))
        else:
            id_html = pid_txt + title_btn
        search = ("%s %s %s %s" % (p.get("id", ""), title, when, text)).lower()
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
            '<div class="id">{id_html}</div>'
            '<div class="meta">{when}{stroke_meta}</div></a>'
            '<div class="actions">{download}{transcribe}{delete}{push}</div>'
            '{transcript}</div>'
            .format(
                search=html.escape(search, quote=True),
                svg=html.escape(p["svg"], quote=True),
                thumb=html.escape(p["thumb"], quote=True),
                id=pid_txt,
                id_html=id_html,
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
        sid_txt = html.escape(str(s.get("id", "")))
        title = str(s.get("title") or "")
        title_btn = ('<button class="titleedit" data-id="%s" data-cur="%s" '
                     'title="Title this recording">&#9998;</button>'
                     % (sid_txt,
                        html.escape(title, quote=True)))
        if title:
            note = ('<span class="rtitle">%s</span> <span class="sid">'
                    '(recording %s)</span>%s'
                    % (html.escape(title), sid_txt, title_btn))
        else:
            note = sid_txt + title_btn
        if when:
            note += " &middot; <span class=\"stamp\">" + html.escape(when) + "</span>"
        if s.get("notebook"):
            nb_raw = str(s["notebook"])
            nb_disp = str(s.get("notebook_name") or nb_raw)
            note += ' &middot; notebook %s<button class="rename" data-nb="%s" data-cur="%s" title="Rename this notebook">&#9998;</button>' % (
                html.escape(nb_disp),
                html.escape(nb_raw, quote=True),
                html.escape(nb_disp, quote=True))
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
        search = ("%s %s %s %s %s%s"
                  % (s.get("id", ""), title, when,
                     s.get("notebook_name") or "",
                     s.get("notebook", ""), tx_search)).lower()
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
        rename_js=RENAME_JS,
        title_js=TITLE_JS,
        sel_js=SEL_JS,
        transcribe_js=TRANSCRIBE_JS,
        push_js=PUSH_JS,
    )
    _LAST_GOOD = page
    return page


_LAST_GOOD = None


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
        browsing does not flood the log. Errors are always WARNING.
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
