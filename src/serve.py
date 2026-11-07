#!/usr/bin/env python3
"""Serve the converted notebook pages over HTTP.

Serves the contents of output/ as static files plus a generated index
page that lists pages (SVG previews) and audio sessions from index.json.

Usage:  ./src/serve.py [--port 8000] [--host 0.0.0.0]
"""
import argparse
import html
import json
import http.server
import os
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "output")
INDEX = os.path.join(OUT, "index.json")

PAGE_TPL = """<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Notebook</title>
<style>
 body {{ font-family: system-ui, sans-serif; margin: 0; background: #f4f5f7; color: #222; }}
 header {{ padding: 16px 24px; background: #1a237e; color: #fff; }}
 header a {{ color: #fff; text-decoration: none; }}
 main {{ max-width: 1080px; margin: 0 auto; padding: 24px; }}
 .card {{ background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 16px; margin-bottom: 20px; }}
 .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 16px; }}
 .page a {{ display: block; text-align: center; color: #1a237e; text-decoration: none; }}
 .page img {{ width: 100%; border: 1px solid #ccc; border-radius: 6px; }}
 .page .id {{ font-size: 13px; margin-top: 6px; }}
 .page a.download {{ display: block; width: fit-content; margin: 8px auto 0;
   padding: 6px 10px; font-size: 13px; background: #1a237e; color: #fff;
   border-radius: 6px; }}
 .page a.download:hover {{ background: #303f9f; }}
 audio {{ width: 100%; }}
 .sess {{ display: flex; gap: 16px; align-items: center; }}
 .sess .note {{ font-size: 13px; color: #555; min-width: 220px; }}
 .sess .note a {{ color: #1a237e; text-decoration: underline; }}
 h2 {{ font-size: 18px; border-bottom: 1px solid #ddd; padding-bottom: 6px; }}
 .search {{ display: flex; gap: 12px; align-items: center; margin-bottom: 20px; }}
 .search input {{ flex: 1; max-width: 480px; padding: 10px 12px; font-size: 15px;
   border: 1px solid #bbb; border-radius: 6px; }}
 .search #count {{ font-size: 13px; color: #666; }}
 .page[hidden] {{ display: none; }}
 details {{ margin-top: 8px; font-size: 13px; color: #333; }}
 details pre {{ white-space: pre-wrap; word-break: break-word; background: #fafafa;
   border: 1px solid #eee; border-radius: 4px; padding: 8px; margin: 8px 0 0; }}
</style>
</head><body>
<header><h1><a href="/">Notebook</a></h1></header>
<main>
<div class="search">
 <input id="q" type="search" placeholder="Search pages and transcriptions..." autocomplete="off">
 <span id="count"></span>
</div>
<h2>Pages ({n_pages})</h2>
<div class="grid">
 {pages}
</div>
<h2>Recordings ({n_sessions})</h2>
{sessions}
</main>
<script>{search_js}</script>
</body></html>
"""

SEARCH_JS = """
(function () {
  var q = document.getElementById('q');
  var count = document.getElementById('count');
  if (!q) return;
  var cards = Array.prototype.slice.call(document.querySelectorAll('.page'));
  function apply() {
    var term = q.value.trim().toLowerCase();
    var shown = 0;
    cards.forEach(function (card) {
      var haystack = (card.getAttribute('data-search') || '').toLowerCase();
      var match = !term || haystack.indexOf(term) !== -1;
      card.hidden = !match;
      if (match) shown++;
    });
    count.textContent = term ? shown + ' of ' + cards.length + ' pages' : '';
  }
  q.addEventListener('input', apply);
  apply();
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
        cards.append(
            '<div class="page" data-search="{search}"><a href="{svg}"><img src="{thumb}" '
            'alt="{id}"><div class="id">{id} &middot; {strokes} strokes</div></a>'
            '{download}{transcript}</div>'
            .format(
                search=html.escape(search, quote=True),
                svg=html.escape(p["svg"], quote=True),
                thumb=html.escape(p["thumb"], quote=True),
                id=html.escape(str(p["id"])),
                strokes=p["strokes"],
                download=download,
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
        sesses.append(
            '<div class="card sess"><div class="note">%s</div>%s</div>'
            % (note, players)
        )
    page = PAGE_TPL.format(
        n_pages=len(pages),
        n_sessions=len(sesses),
        pages="\n".join(cards),
        sessions="\n".join(sesses),
        search_js=SEARCH_JS,
    )
    _LAST_GOOD = page
    return page


_LAST_GOOD = None


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