#!/usr/bin/env python3
"""Measure the web page's layout shift and take screenshots of it.

    ./run ui-check [--out DIR] [--budget 0.05] [--delay 0.6]

Builds a synthetic library (generated strokes, made-up titles: safe to
publish), serves it with images and audio delayed like a phone on the
LAN, and uses headless Chromium to:

* measure Cumulative Layout Shift on a desktop and a phone width, from
  the browser's own layout-shift records, and name what moved;
* save screenshots (overview, busy/selected/toast states, search) for
  pull requests.

Exits 1 when CLS exceeds --budget, 2 when Chromium or pycairo is missing.
Screenshots go to logs/ui-check/ by default (gitignored).
"""
import argparse
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import tests  # noqa: E402  (sets ECHO_ROOT to a throwaway sandbox)
from tests import fixtures  # noqa: E402

sys.path.insert(0, os.path.join(REPO, "src"))
import serve  # noqa: E402

VIEWS = {"desktop": 1280, "mobile": 390}
BROWSERS = ("chromium", "chromium-browser", "google-chrome",
            "google-chrome-stable")

CLS_PAGE = """<!doctype html><html><body style="margin:0">
<iframe id="f" src="/" style="width:%(width)spx;height:2400px;border:0"></iframe>
<pre id="out">pending</pre>
<script>
var f = document.getElementById('f'), shifts = [];
function name(n) {
  if (!n || !n.tagName) return 'unknown';
  var c = typeof n.className === 'string' ? n.className.trim() : '';
  return n.tagName.toLowerCase() + (c ? '.' + c.split(/\\s+/).join('.') : '');
}
f.addEventListener('load', function () {
  new f.contentWindow.PerformanceObserver(function (list) {
    list.getEntries().forEach(function (e) {
      if (!e.hadRecentInput) shifts.push({value: e.value,
        moved: e.sources.map(function (s) { return name(s.node); })});
    });
  }).observe({type: 'layout-shift', buffered: true});
  setTimeout(function () {
    document.getElementById('out').textContent = JSON.stringify(shifts);
  }, 4000);
});
</script></body></html>"""

# Drives the page into the states a PR reviewer wants to see. Headless
# Chromium's virtual clock does not run CSS transitions, so they are
# switched off to capture end states; transcription never returns, so its
# spinner stays on screen.
DEMO_PAGE = """<!doctype html><html><body style="margin:0">
<iframe id="f" src="/" style="width:%(width)spx;height:100vh;border:0;display:block"></iframe>
<script>
var f = document.getElementById('f'), scenario = '%(scenario)s';
f.addEventListener('load', function () {
  var d = f.contentDocument, w = f.contentWindow;
  w.confirm = function () { return true; };
  var still = d.createElement('style');
  still.textContent = '*, *::before { transition: none !important; }';
  d.head.appendChild(still);
  var realFetch = w.fetch;
  w.fetch = function (url, opts) {
    if (String(url).indexOf('/api/transcribe/') === 0) return new w.Promise(function () {});
    return realFetch.call(w, url, opts);
  };
  setTimeout(function () {
    if (scenario === 'search') {
      var q = d.getElementById('q');
      q.value = 'planning';
      q.dispatchEvent(new w.Event('input'));
      return;
    }
    var box = d.querySelectorAll('.pickbox')[2];
    box.checked = true;
    box.dispatchEvent(new w.Event('change', {bubbles: true}));
    d.querySelector('.card.page .transcribe').click();
    d.querySelector('.card.page .push').click();
  }, 900);
});
</script></body></html>"""


def build_library(root):
    """Pages of different shapes, two recordings, titles and text.

    Raises OSError when the sandbox can't be written (main reports it).
    """
    shapes = [
        ("2208.1.12.25", [(500, 600 + 300 * r, 12) for r in range(14)]),
        ("2208.1.12.26", [(500, 600, 30)]),
        ("2208.1.12.27", [(800, 900, 3)]),
        ("2208.1.12.28", [(300, 400 + 250 * r, 6) for r in range(4)]),
        ("2208.1.12.29", [(400, 500 + 300 * r, 20) for r in range(8)]),
        ("2208.1.12.30", [(600, 700, 8)]),
    ]
    for page_id, rows in shapes:
        strokes = [s for x, y, n in rows for s in fixtures.text_line(x, y, n)]
        fixtures.write_page(root, page_id, strokes)
    fixtures.write_session(root, "PRS-1a2b",
                           [fixtures.page_address(2208, 1, 12, 25)])
    sess = fixtures.write_session(root, "PRS-3c4d", [
        fixtures.page_address(2208, 1, 12, 28),
        fixtures.page_address(2208, 1, 12, 29)])
    with open(os.path.join(sess, "audio-1.aac"), "wb") as f:
        f.write(b"\x00" * 64)
    with open(os.path.join(root, "titles.json"), "w") as f:
        json.dump({"2208.1.12.25": "Grocery list", "PRS-3c4d": "Planning call"}, f)
    with open(os.path.join(root, "notebook_names.json"), "w") as f:
        json.dump({"0123456789abcdef": "Field notebook"}, f)
    proc = fixtures.run_convert(root)
    if proc.returncode != 0:
        raise SystemExit("convert failed:\n" + proc.stderr)
    add_text(os.path.join(root, "output", "index.json"))


def add_text(index_path):
    """Synthetic transcriptions, so those parts of the page show too."""
    with open(index_path) as f:
        index = json.load(f)
    for page in index["pages"]:
        if page["id"] in ("2208.1.12.25", "2208.1.12.28"):
            page.update(text="the quick brown fox\njumps over the lazy dog",
                        ocr=True)
    for session in index["sessions"]:
        if session["id"] == "PRS-3c4d":
            session["transcript"] = "lorem ipsum dolor sit amet " * 12
    with open(index_path, "w") as f:
        json.dump(index, f)


def make_handler(delay):
    class Handler(serve.Handler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            page = self.harness_page()
            if page is not None:
                self.send_html(page)
                return
            if self.path.endswith((".png", ".svg", ".aac")):
                time.sleep(delay)
            super().do_GET()

        def harness_page(self):
            view = "mobile" if "mobile" in self.path else "desktop"
            width = VIEWS[view]
            if self.path.startswith("/__cls"):
                return CLS_PAGE % {"width": width}
            if self.path.startswith("/__demo"):
                scenario = "search" if "search" in self.path else "actions"
                return DEMO_PAGE % {"width": width, "scenario": scenario}
            return None

        def send_html(self, page):
            body = page.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    return Handler


def browser():
    return next((shutil.which(b) for b in BROWSERS if shutil.which(b)), None)


def chromium(exe, *args):
    cmd = [exe, "--headless=new", "--disable-gpu", "--hide-scrollbars"]
    return subprocess.run(cmd + list(args), capture_output=True, text=True,
                          timeout=120).stdout


def measure_cls(exe, base, view):
    dom = chromium(exe, "--virtual-time-budget=15000", "--dump-dom",
                   f"{base}/__cls?{view}")
    match = re.search(r'<pre id="out">(.*?)</pre>', dom, re.S)
    failure = f"could not read layout shifts for {view} from Chromium"
    if match is None:
        raise SystemExit(failure)
    try:
        shifts = json.loads(match.group(1).replace("&quot;", '"'))
    except ValueError:
        raise SystemExit(failure) from None
    return sum(s["value"] for s in shifts), shifts


def screenshot(exe, url, path, width, budget_ms):
    chromium(exe, f"--window-size={width},1700" if "__demo" not in url
             else f"--window-size={width},1000",
             f"--virtual-time-budget={budget_ms}", f"--screenshot={path}", url)
    return os.path.exists(path)


def take_screenshots(exe, base, out):
    shots = []
    for view, width in VIEWS.items():
        for name, url, ms in (("overview", f"{base}/", 8000),
                              ("actions", f"{base}/__demo?actions-{view}", 4000),
                              ("search", f"{base}/__demo?search-{view}", 4000)):
            path = os.path.join(out, f"{name}-{view}.png")
            if screenshot(exe, url, path, width, ms):
                shots.append(path)
    return shots


def main(argv=None):
    ap = argparse.ArgumentParser(description="Measure layout shift and "
                                             "screenshot the web page.")
    ap.add_argument("--out", default=os.path.join(REPO, "logs", "ui-check"))
    ap.add_argument("--budget", type=float, default=0.05,
                    help="largest acceptable CLS (default 0.05; 0.1 is "
                         "Google's 'good')")
    ap.add_argument("--delay", type=float, default=0.6,
                    help="seconds to delay each image/audio response")
    args = ap.parse_args(argv)
    exe = browser()
    if not exe or not fixtures.has_cairo():
        missing = "chromium" if not exe else "pycairo"
        print(f"ui-check needs Chromium and pycairo (missing: {missing})",
              file=sys.stderr)
        return 2
    try:
        build_library(tests.SANDBOX)
        os.makedirs(args.out, exist_ok=True)
    except OSError as e:
        print(f"ui-check could not prepare its files: {e}", file=sys.stderr)
        return 2
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                            make_handler(args.delay))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    worst = 0.0
    try:
        for view in VIEWS:
            cls, shifts = measure_cls(exe, base, view)
            worst = max(worst, cls)
            moved = sorted({m for s in shifts for m in s["moved"]})
            print(f"CLS {view:<8} {cls:.4f}" + (f"  moved: {', '.join(moved)}"
                                                if moved else ""))
        for path in take_screenshots(exe, base, args.out):
            print("screenshot", os.path.relpath(path, REPO))
    finally:
        httpd.shutdown()
    if worst > args.budget:
        print(f"layout shift {worst:.4f} is over the budget of {args.budget}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
