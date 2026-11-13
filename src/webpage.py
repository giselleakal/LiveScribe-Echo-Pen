"""The notebook web page, built from output/index.json.

Stdlib only, like serve.py. The markup lives here; the styles and the
behaviour live in src/web/style.css and src/web/app.js, which are read on
every build and inlined, so the page is one request and edits show up on
the next reload without restarting the server.

Every value from index.json is HTML-escaped before it reaches the page.
"""
import datetime
import html
import json
import os
import re
import string

import paths

WEB = os.path.join(paths.SRC, "web")
INDEX = os.path.join(paths.OUT, "index.json")
THUMB_WIDTH = 320            # convert.py renders thumbnails this wide
STAGGER_LIMIT = 8            # cards after this many appear without delay

_last_good = None


def esc(value):
    return html.escape(str(value), quote=True)


def _read(name):
    """A page asset from src/web. Raises OSError when it is missing, which
    means a broken checkout, not a runtime condition to work around."""
    with open(os.path.join(WEB, name), encoding="utf-8") as f:
        return f.read()


def fmt_when(iso, iso_end=None):
    """Human capture-time string from an index.json ISO timestamp.

    e.g. "Nov 10, 2026, 10:16 AM", or with an end time on the same day:
    "Nov 10, 2026, 10:16 AM - 10:34 AM". Empty when missing/unparseable.
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
        return f"{start} - {end}"
    return f"{start} - {dt2.strftime('%I:%M').lstrip('0')}{dt2.strftime(' %p')}"


# ---------------------------------------------------------------------------
# small pieces
# ---------------------------------------------------------------------------

def button(label, cls, **data):
    attrs = "".join(f' data-{k}="{esc(v)}"' for k, v in data.items())
    return f'<button type="button" class="btn {cls}"{attrs}>{label}</button>'


def icon_button(cls, tip, **data):
    attrs = "".join(f' data-{k}="{esc(v)}"' for k, v in data.items())
    return (f'<button type="button" class="icon-btn {cls}"{attrs} '
            f'title="{tip}" aria-label="{tip}">&#9998;</button>')


def title_button(asset_id, current, what):
    return icon_button("titleedit", f"Title this {what}", id=asset_id,
                       cur=current)


def pick_box(kind, asset_id):
    return (f'<label class="pick" title="Select"><input type="checkbox" '
            f'class="pickbox" data-kind="{kind}" data-id="{esc(asset_id)}" '
            f'aria-label="Select {esc(asset_id)}"></label>')


def stagger(i):
    return f' style="--i:{min(i, STAGGER_LIMIT)}"'


def thumb_size(page):
    """(width, height) of a page's thumbnail, from its ink bounds.

    Lets the browser know each image's shape before it loads.
    """
    bounds = page.get("bounds") or []
    try:
        minx, miny, maxx, maxy = (float(b) for b in bounds)
    except (TypeError, ValueError):
        return THUMB_WIDTH, THUMB_WIDTH * 5 // 4
    width = max(1.0, maxx - minx)
    return THUMB_WIDTH, max(1, int((maxy - miny) * THUMB_WIDTH / width))


def details(summary, text):
    return (f"<details><summary>{summary}</summary>"
            f"<pre>{html.escape(text)}</pre></details>")


# ---------------------------------------------------------------------------
# cards
# ---------------------------------------------------------------------------

def download_link(href):
    return f'<a class="btn ghost" href="{esc(href)}" download>Download .txt</a>'


def card_actions(kind, asset_id, text_href):
    """Download (once transcribed) or Transcribe, then Send and Delete."""
    first = (download_link(text_href) if text_href else
             button("Transcribe", "primary transcribe", kind=kind, id=asset_id))
    return (first
            + button("Send to Hermes", "send push", kind=kind, id=asset_id)
            + button("Delete", "danger delete", kind=kind, id=asset_id))


def page_card(page, i):
    pid = str(page["id"])
    title = str(page.get("title") or "")
    text = page.get("text") or ""
    when = fmt_when(page.get("captured"), page.get("captured_end"))
    width, height = thumb_size(page)
    svg, thumb = esc(page["svg"]), esc(page["thumb"])
    name = (f'{esc(title)} <span class="psid">({esc(pid)})</span>'
            if title else esc(pid))
    strokes = f"{page.get('strokes', 0)} strokes"
    meta = f"{esc(when)} &middot; {strokes}" if when else strokes
    actions = card_actions("page", pid, f"pages/{pid}/text.txt" if text else "")
    search = " ".join([pid, title, when, text]).lower()
    return (
        f'<article class="card page" data-search="{esc(search)}"{stagger(i)}>'
        f'{pick_box("page", pid)}'
        f'<a class="thumb" href="{svg}"><img src="{thumb}" alt="Page {esc(pid)}"'
        f' width="{width}" height="{height}" loading="lazy" decoding="async"></a>'
        f'<div class="title"><a href="{svg}">{name}</a>'
        f'{title_button(pid, title, "page")}</div>'
        f'<div class="meta">{meta}</div>'
        f'<div class="actions">{actions}</div>'
        f'{details("Transcription", text) if text else ""}</article>')


def players(urls):
    audio = '<audio controls preload="metadata" src="{}"></audio>'
    if len(urls) == 1:
        return audio.format(esc(urls[0]))
    return "".join(f'<div class="part"><div class="plabel">Part {n}</div>'
                   f'{audio.format(esc(u))}</div>'
                   for n, u in enumerate(urls, 1))


def session_note(session):
    sid = str(session.get("id", ""))
    title = str(session.get("title") or "")
    note = (f'<span class="rtitle">{esc(title)}</span> '
            f'<span class="sid">(recording {esc(sid)})</span>'
            if title else esc(sid))
    note += title_button(sid, title, "recording")
    when = fmt_when(session.get("captured"), session.get("captured_end"))
    if when:
        note += f' &middot; <span class="stamp">{esc(when)}</span>'
    if session.get("notebook"):
        raw = str(session["notebook"])
        shown = str(session.get("notebook_name") or raw)
        note += (f" &middot; notebook {esc(shown)}"
                 + icon_button("rename", "Rename this notebook", nb=raw,
                               cur=shown))
    parts = len(session.get("urls") or [])
    if parts > 1:
        note += f" ({parts} parts)"
    linked = session.get("pages") or []
    if linked:
        links = " ".join(f'<a href="{esc(f"pages/{pid}/page.svg")}">'
                         f'{esc(pid)}</a>' for pid in linked)
        note += f'<br>Pages: <span class="pglinks">{links}</span>'
    return note


def session_card(session, i):
    sid = str(session.get("id", ""))
    transcript = (session.get("transcript") or "").strip()
    actions = card_actions("session", sid,
                           f"audio/{sid}.txt" if transcript else "")
    search = " ".join([sid, str(session.get("title") or ""),
                       fmt_when(session.get("captured"),
                                session.get("captured_end")),
                       str(session.get("notebook_name") or ""),
                       str(session.get("notebook", "")), transcript]).lower()
    return (
        f'<article class="card sess" data-search="{esc(search)}"{stagger(i)}>'
        f'{pick_box("session", sid)}'
        f'<div class="sess-head"><div class="note">{session_note(session)}'
        f'</div><div class="actions">{actions}</div></div>'
        f'<div class="players">{players(session["urls"])}</div>'
        f'{details("Transcript", transcript) if transcript else ""}</article>')


# ---------------------------------------------------------------------------
# page
# ---------------------------------------------------------------------------

SELECT_BAR = (
    '<div class="selbar">'
    '<label><input type="checkbox" id="sel-all"> Select all shown</label>'
    '<button type="button" class="btn muted" id="sel-clear">Clear</button>'
    '<button type="button" class="btn danger" id="sel-del" disabled>Delete '
    'selected (<span id="sel-count">0</span>)</button></div>')


def section(title, count_id, count, body):
    return (f'<h2><span class="bar"></span>{title} '
            f'<span class="n" id="{count_id}">{count}</span></h2>{body}')


def empty(headline, hint=""):
    return f'<div class="empty"><b>{headline}</b>{hint}</div>'


def no_match(what):
    """Shown by the search script when it hides every card of a section."""
    return f'<div class="empty nomatch" hidden>No matching {what}</div>'


def library_body(data):
    pages = data.get("pages", [])
    sessions = [s for s in data.get("sessions", []) if s.get("urls")]
    if not pages and not sessions:
        return empty("No notes yet",
                     "Plug in the pen and run <code>./run sync</code>.")
    page_cards = "".join(page_card(p, i) for i, p in enumerate(pages))
    sess_cards = "".join(session_card(s, i) for i, s in enumerate(sessions))
    pages_html = (f'<div class="grid">{page_cards}</div>{no_match("pages")}'
                  if pages else empty("No pages"))
    sessions_html = (f'<div class="list">{sess_cards}</div>'
                     f'{no_match("recordings")}'
                     if sessions else empty("No recordings"))
    return (SELECT_BAR
            + section("Pages", "n-pages", len(pages), pages_html)
            + section("Recordings", "n-sessions", len(sessions),
                      sessions_html))


def render(body):
    return string.Template(_read("page.html")).substitute(
        style=_read("style.css"), script=_read("app.js"), body=body)


def build_index():
    """The whole page, built from index.json.

    Falls back to the last successfully built page when index.json is
    missing or mid-write (a convert running while the server is up).
    """
    global _last_good
    try:
        with open(INDEX, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        if _last_good is not None:
            return _last_good
        return render(empty("No converted output yet",
                            "Run <code>./run</code> to build the library."))
    _last_good = render(library_body(data))
    return _last_good
