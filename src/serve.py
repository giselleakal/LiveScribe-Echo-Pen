#!/usr/bin/env python3
"""Serve the converted notebook pages over HTTP.

Serves the contents of output/ as static files plus a generated index
page that lists pages (SVG previews) and audio sessions from index.json.

Usage:  ./src/serve.py [--port 8000] [--host 0.0.0.0]
"""
import argparse
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
 audio {{ width: 100%; }}
 .sess {{ display: flex; gap: 16px; align-items: center; }}
 .sess .note {{ font-size: 13px; color: #555; min-width: 220px; }}
 h2 {{ font-size: 18px; border-bottom: 1px solid #ddd; padding-bottom: 6px; }}
</style>
</head><body>
<header><h1><a href="/">Notebook</a></h1></header>
<main>
<h2>Pages ({n_pages})</h2>
<div class="grid">
 {pages}
</div>
<h2>Recordings ({n_sessions})</h2>
{sessions}
</main>
</body></html>
"""


def build_index():
    try:
        with open(INDEX) as f:
            data = json.load(f)
    except OSError:
        return "<h2>No converted output yet. Run ./src/convert.py</h2>"

    pages = data.get("pages", [])
    cards = []
    for p in pages:
        cards.append(
            '<div class="page"><a href="{svg}"><img src="{thumb}" '
            'alt="{id}"><div class="id">{id} &middot; {strokes} strokes</div></a></div>'
            .format(**p)
        )
    sesses = []
    for s in data.get("sessions", []):
        urls = s.get("urls") or []
        if not urls:
            continue
        players = "".join('<audio controls src="%s"></audio>' % u for u in urls)
        parts = " (%d parts)" % len(urls) if len(urls) > 1 else ""
        sesses.append(
            '<div class="card sess"><div class="note">%s%s</div>%s</div>'
            % (s["id"], parts, players)
        )
    return PAGE_TPL.format(
        n_pages=len(pages),
        n_sessions=len(sesses),
        pages="\n".join(cards),
        sessions="\n".join(sesses),
    )


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=OUT, **kwargs)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/" or path == "":
            body = build_index().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
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