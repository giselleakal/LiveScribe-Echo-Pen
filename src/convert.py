#!/usr/bin/env python3
"""Convert raw dumpscribe output into web-viewable page images.

Reads:  data/                     -- directory produced by dumpscribe
Writes: output/index.json         (atomically re-written each run)
        output/pages/<page_id>/page.svg   (vector, full fidelity)
        output/pages/<page_id>/thumb.png  (raster thumbnail)
        output/pages/<page_id>/text.txt   (with --ocr)
        output/audio/<session_id>.<n>.aac (copies of session audio)
        .convert-state.json      (incremental-rendering bookkeeping)

All output files are written atomically (temp file + rename) so the web
server never serves a half-written file while a conversion is running.

The pipeline is incremental: sources whose size/mtime are unchanged since
the previous run are skipped, and output for sources that no longer exist
is pruned. Use --force to re-render everything.

Usage:  ./src/convert.py [--ocr] [--ocr-engine auto|tesseract|trocr] [--force]
                     [--stt] [--session <id>]
"""
import argparse
import atexit
import datetime
import glob
import json
import os
import re
import shutil
import sys
import tempfile
import time

import paths
sys.path.insert(0, paths.VENDOR)
import parsestf
import ocr
import stt

PAGE_W, PAGE_H = 4963, 6278  # STF "dot" units; page size used by stf2pdf
GRID = 83                     # visible dot-grid spacing in STF units
INK = (12, 22, 90)            # Livescribe ink blue

RENDER_VERSION = 4          # bump to force re-render on next ./run (see main)
PAGE_PADDING = 120          # STF units of margin around ink in each render
MIN_CANVAS = 600            # minimum page size in STF units (~2 grid cells)

ROOT = paths.ROOT
STATE_PATH = os.path.join(ROOT, ".convert-state.json")
# user-assigned friendly names for pen notebook guids; written by the
# site's rename button, read here so index.json carries the alias
NOTEBOOK_NAMES_PATH = os.path.join(ROOT, "notebook_names.json")
# user-assigned titles for individual assets, keyed by id (PRS- session
# ids and page ids never collide, so one file serves both)
TITLES_PATH = os.path.join(ROOT, "titles.json")
LOCK_PATH = os.path.join(ROOT, ".convert.lock")


# ---------------------------------------------------------------------------
# run lock
# ---------------------------------------------------------------------------

def acquire_lock(wait=3.0):
    """Create the convert lock file exclusively (O_CREAT | O_EXCL).

    Returns True if this process took the lock. Returns False without
    creating anything when ``ECHO_LOCK_HELD`` is set: serve.py sets that
    before launching a reconcile ``convert`` while it already holds the
    lock, so the child skips locking and can't deadlock against its parent.

    Blocks/retries up to ``wait`` seconds; raises RuntimeError if the
    lock stays held that long.
    """
    if os.environ.get("ECHO_LOCK_HELD"):
        return False
    deadline = time.time() + wait
    while True:
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            return True
        except FileExistsError:
            if time.time() >= deadline:
                raise RuntimeError(
                    "Another convert is running (lock file: %s). "
                    "Try again shortly." % LOCK_PATH)
            time.sleep(0.1)


def release_lock():
    try:
        os.remove(LOCK_PATH)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# atomic output helpers
# ---------------------------------------------------------------------------

def atomic_write(path, data):
    """Write ``data`` (str or bytes) to ``path`` atomically.

    Writes to a temp file in the same directory, then renames it over the
    destination, so concurrent readers never see a torn file.
    """
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


def publish_png(surface, path):
    """Write a cairo surface to ``path`` atomically."""
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(path) or ".",
        prefix=os.path.basename(path) + ".", suffix=".png.tmp")
    os.close(fd)
    try:
        surface.write_to_png(tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# STF parsing / rendering
# ---------------------------------------------------------------------------

class StrokeCollector(parsestf.STFParser):
    def __init__(self, stream):
        super().__init__(stream)
        self.strokes = []   # list of strokes; each a list of (x, y, force)
        self.cur = []
        self.tmin = None    # earliest pen-time ms seen (capture start)
        self.tmax = None    # latest pen-time ms seen (capture end)

    def handle_point(self, x, y, force, time):
        self.cur.append((x, y, force))
        if self.tmin is None or time < self.tmin:
            self.tmin = time
        if self.tmax is None or time > self.tmax:
            self.tmax = time

    def handle_stroke_end(self, time):
        self.strokes.append(self.cur)
        self.cur = []


def load_strokes(stf_path):
    """Parse a .stf into (strokes, tmin, tmax) with pen-time ms bounds."""
    with open(stf_path, "rb") as f:
        sc = StrokeCollector(f)
        sc.parse()
    return [s for s in sc.strokes if s], sc.tmin, sc.tmax


# ---------------------------------------------------------------------------
# capture timestamps (pen time -> real time via data/time_offset)
# ---------------------------------------------------------------------------

def pen_offset_ms(data_dir):
    """Milliseconds from the pen's internal epoch to real time.

    dumpscribe computes systime - pentime at sync time and stores it in
    data/time_offset. Pen-downloaded file mtimes are a 1980 zero-time
    placeholder, so this is the only reliable way to date pen data.
    """
    try:
        with open(os.path.join(data_dir, "time_offset")) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def pen_ms_to_iso(pen_ms, offset_ms):
    """Real time of a pen-time ms value, as local ISO 8601 with offset."""
    dt = datetime.datetime.fromtimestamp((pen_ms + offset_ms) / 1000.0,
                                         datetime.timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def parse_session_info(path, session_id):
    """Decode session.info (CA-BE v3 header).

    Layout: magic 0xFACE, version, 7 zero bytes, then 5-byte big-endian
    pen-ms fields: session start at offset 11 (the PRS id IS that start
    time, which we use as a sanity check), end at offset 19; per audio
    part start/end pairs follow after a length-prefixed name each.

    Returns (start_ms, end_ms), or None when unparseable.
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    if len(raw) < 24 or raw[0:2] != b"\xfa\xce":
        return None
    start = int.from_bytes(raw[11:16], "big")
    end = int.from_bytes(raw[19:24], "big")
    try:
        if start != int(session_id[4:], 16):
            return None
    except ValueError:
        return None
    if start <= 0 or end < start:
        return None
    return start, end


def dot_grid(bounds=None):
    """Yield (cx, cy, r) grid points across a page region.

    Keeps the global GRID lattice so paper dots stay aligned with the
    original page, but only walks the region in ``bounds``. The default
    region is the full nominal page.
    """
    if bounds is None:
        bounds = (0, 0, PAGE_W, PAGE_H)
    minx, miny, maxx, maxy = bounds
    r = GRID * 0.06
    startx = int(minx // GRID) * GRID
    starty = int(miny // GRID) * GRID
    for gy in range(starty, int(maxy) + 1, GRID):
        for gx in range(startx, int(maxx) + 1, GRID):
            yield gx, gy, r


def ink_bounds(strokes, padding=PAGE_PADDING, min_canvas=MIN_CANVAS):
    """Bounds of the actual ink, padded, with a minimum canvas size.

    Returns (minx, miny, maxx, maxy). An empty page falls back to the full
    nominal page rectangle. The minimum canvas is enforced symmetrically
    around the ink centre so a lone stroke (or a single dot) doesn't zoom
    to fill the whole card.
    """
    pts = [p for s in strokes for p in s]
    if not pts:
        return (0, 0, PAGE_W, PAGE_H)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    minx, maxx = min(xs) - padding, max(xs) + padding
    miny, maxy = min(ys) - padding, max(ys) + padding
    w = maxx - minx
    h = maxy - miny
    if w < min_canvas:
        half, cx = min_canvas / 2.0, (minx + maxx) / 2.0
        minx, maxx = cx - half, cx + half
    if h < min_canvas:
        half, cy = min_canvas / 2.0, (miny + maxy) / 2.0
        miny, maxy = cy - half, cy + half
    return (minx, miny, maxx, maxy)


def render_svg(strokes, path, bounds=None):
    if bounds is None:
        bounds = ink_bounds(strokes)
    minx, miny, maxx, maxy = bounds
    w, h = maxx - minx, maxy - miny
    parts = []
    parts.append('<svg xmlns="http://www.w3.org/2000/svg" viewBox="%.1f %.1f %.1f %.1f"'
                 % (minx, miny, w, h))
    parts.append(' width="100%" height="100%">\n')
    parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="#ffffff"/>\n'
                 % (minx, miny, w, h))
    parts.append('<g fill="#c9d4e8">\n')
    for gx, gy, r in dot_grid(bounds):
        parts.append('<circle cx="%d" cy="%d" r="%.2f"/>\n' % (gx, gy, r))
    parts.append('</g>\n')
    parts.append('<g stroke="#%02x%02x%02x" fill="none" stroke-linecap="round" '
                 'stroke-linejoin="round">\n' % INK)
    for stroke in strokes:
        avg_f = sum(p[2] for p in stroke) / len(stroke)
        width = max(1.5, (avg_f ** 0.3) * 3)
        d = "M" + " L".join("%.1f %.1f" % (x, y) for x, y, _ in stroke)
        parts.append('<path d="%s" stroke-width="%.2f"/>\n' % (d, width))
    parts.append('</g>\n</svg>\n')
    atomic_write(path, "".join(parts))


def render_png(strokes, png_path, width=320, grid=True, bounds=None):
    import cairo
    if bounds is None:
        bounds = (0, 0, PAGE_W, PAGE_H)
    minx, miny, maxx, maxy = bounds
    bw = max(1.0, maxx - minx)
    bh = max(1.0, maxy - miny)
    scale = width / bw
    surface = cairo.ImageSurface(
        cairo.FORMAT_RGB24, width, max(1, int(bh * scale)))
    ctx = cairo.Context(surface)
    ctx.set_source_rgb(1, 1, 1)
    ctx.paint()
    ctx.scale(scale, scale)
    ctx.translate(-minx, -miny)
    if grid:
        # dot grid
        ctx.set_source_rgb(0.79, 0.83, 0.91)
        for gx, gy, r in dot_grid(bounds):
            ctx.arc(gx, gy, r, 0, 2 * 3.14159)
            ctx.fill()
    # ink
    ctx.set_source_rgb(*[c / 255.0 for c in INK])
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    ctx.set_line_join(cairo.LINE_JOIN_ROUND)
    for stroke in strokes:
        avg_f = sum(p[2] for p in stroke) / len(stroke)
        ctx.set_line_width(max(1.5, (avg_f ** 0.3) * 3))
        ctx.move_to(stroke[0][0], stroke[0][1])
        for x, y, _ in stroke[1:]:
            ctx.line_to(x, y)
        ctx.stroke()
    publish_png(surface, png_path)


def render_thumb(strokes, png_path, size=320, bounds=None):
    if bounds is None:
        bounds = ink_bounds(strokes)
    render_png(strokes, png_path, width=size, grid=True, bounds=bounds)


def stroke_bounds(stroke):
    xs = [p[0] for p in stroke]
    ys = [p[1] for p in stroke]
    return min(xs), min(ys), max(xs), max(ys)


def group_lines(strokes):
    """Split strokes into text lines using their vertical position.

    Small strokes (dots, accents) are assigned to the nearest line after
    the main line bands are found from the taller strokes.
    """
    if not strokes:
        return []
    info = []
    for stroke in strokes:
        x0, y0, x1, y1 = stroke_bounds(stroke)
        info.append((stroke, y0, y1, (y0 + y1) / 2.0, y1 - y0))
    heights = sorted(i[4] for i in info)
    median_h = heights[len(heights) // 2] or 1.0
    tall = [i for i in info if i[4] >= 0.35 * median_h] or info

    clusters = []
    for item in sorted(tall, key=lambda i: i[3]):
        center = item[3]
        if clusters:
            mean = sum(clusters[-1]) / len(clusters[-1])
            if abs(center - mean) <= 0.75 * median_h:
                clusters[-1].append(center)
                continue
        clusters.append([center])
    centers = [sum(c) / len(c) for c in clusters]

    lines = [[] for _ in centers]
    for stroke, _, _, center, _ in info:
        idx = min(range(len(centers)), key=lambda j: abs(center - centers[j]))
        lines[idx].append(stroke)

    ordered = [ln for _, ln in sorted(zip(centers, lines), key=lambda t: t[0])]
    for line in ordered:
        line.sort(key=lambda s: stroke_bounds(s)[0])
    return [ln for ln in ordered if ln]


def render_line_png(strokes, png_path, width=2400, max_height=1200,
                    padding=60):
    pts = [p for s in strokes for p in s]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    bounds = (min(xs) - padding, min(ys) - padding,
              max(xs) + padding, max(ys) + padding)
    bw = bounds[2] - bounds[0]
    bh = bounds[3] - bounds[1]
    if bh > 0 and width * (bh / bw) > max_height:
        width = max(100, int(max_height * (bw / bh)))
    render_png(strokes, png_path, width=width, grid=False, bounds=bounds)


def clean_ocr_text(text):
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s+([?.!,;:])", r"\1", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


# ---------------------------------------------------------------------------
# session parsing
# ---------------------------------------------------------------------------

def parse_session_pages(path):
    """Decode the binary session.pages (CA BE header, packed page addresses).

    Layout (big-endian): magic(2) version(1) reserved(1) num_pages(2),
    then per page: page-address(8) + pen-time(8).
    """
    with open(path, "rb") as f:
        raw = f.read()
    if len(raw) < 6 or raw[0:2] != b"\xca\xbe":
        return []
    num = int.from_bytes(raw[4:6], "big")
    pages = []
    off = 6
    for _ in range(num):
        if off + 16 > len(raw):
            break
        pages.append(decode_page_address(raw[off:off + 8]))
        off += 16
    return pages


def decode_page_address(b):
    v = int.from_bytes(b, "big")
    section = (v >> 52) & 0xFFF
    segment = (v >> 40) & 0xFFF
    shelf = (v >> 24) & 0xFFFF
    book = (v >> 12) & 0xFFF
    page = v & 0xFFF
    if section:
        return "%d.%d.%d.%d.%d" % (section, segment, shelf, book, page)
    return "%d.%d.%d.%d" % (segment, shelf, book, page)


def audio_part_key(path):
    """Natural sort key so audio-2.aac comes before audio-10.aac."""
    base = os.path.basename(path)
    m = re.search(r"audio-(\d+)\.aac$", base)
    return (int(m.group(1)) if m else -1, base)


def load_notebook_names():
    """Friendly display names for notebook guids (notebook_names.json)."""
    try:
        with open(NOTEBOOK_NAMES_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if str(v).strip()}


def load_titles():
    """User-assigned titles for assets, keyed by id (titles.json)."""
    try:
        with open(TITLES_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if str(v).strip()}


def apply_title(entry, asset_id, titles):
    """Set or clear entry["title"] for a reused (not re-rendered) entry."""
    t = titles.get(asset_id)
    if t:
        entry["title"] = t
    else:
        entry.pop("title", None)


def collect_sessions(data_dir):
    offset = pen_offset_ms(data_dir)
    names = load_notebook_names()
    titles = load_titles()
    sessions = []
    replay_root = os.path.join(data_dir, "userdata")
    for pen in glob.glob(os.path.join(replay_root, "*", "Paper Replay", "*", "*")):
        notebook = os.path.basename(pen)
        for sess in glob.glob(os.path.join(pen, "sessions", "*")):
            sid = os.path.basename(sess)
            aac_parts = sorted(glob.glob(os.path.join(sess, "audio-*.aac")),
                               key=audio_part_key)
            if not aac_parts:
                continue
            sp = os.path.join(sess, "session.pages")
            pages = parse_session_pages(sp) if os.path.exists(sp) else []
            s = {
                "id": sid,
                "notebook": notebook,
                "audio": [os.path.relpath(p, data_dir) for p in aac_parts],
                "pages": pages,
            }
            info = os.path.join(sess, "session.info")
            if os.path.exists(info) and offset is not None:
                times = parse_session_info(info, sid)
                if times:
                    s["captured"] = pen_ms_to_iso(times[0], offset)
                    s["captured_end"] = pen_ms_to_iso(times[1], offset)
                    s["pen_tstart"] = times[0]
                    s["pen_tend"] = times[1]
            alias = names.get(notebook)
            if alias:
                s["notebook_name"] = alias
            title = titles.get(sid)
            if title:
                s["title"] = title
            sessions.append(s)
    # chronological order so the site lists recordings oldest-first
    sessions.sort(key=lambda s: (s.get("captured") or "9999", s["id"]))
    return sessions


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------

def ocr_page(strokes, page_dir, engine, lang, psm):
    """OCR one page with the selected backend.

    Returns (text, kept_pngs) where kept_pngs lists the ocr*.png filenames
    that are still part of the result. Files are published atomically; the
    caller prunes any older ocr*.png that are no longer in the set.
    """
    kept = []
    if engine == "trocr":
        texts = []
        for i, line in enumerate(group_lines(strokes)):
            name = "ocr-line-%d.png" % i
            line_png = os.path.join(page_dir, name)
            render_line_png(line, line_png)
            kept.append(name)
            try:
                texts.append(ocr.trocr_image_to_text(line_png))
            except Exception as e:
                print("warning: TrOCR failed on line %d: %s" % (i, e),
                      file=sys.stderr)
        return clean_ocr_text("\n".join(t for t in texts if t)), kept
    name = "ocr.png"
    ocr_png = os.path.join(page_dir, name)
    render_png(strokes, ocr_png, width=2000, grid=False)
    kept.append(name)
    return clean_ocr_text(ocr.image_to_text(ocr_png, lang=lang, psm=psm)), kept


def prune_ocr_pngs(page_dir, kept_names):
    """Remove ocr*.png files that a previous run left but this run did not."""
    for name in sorted(glob.glob(os.path.join(page_dir, "ocr*.png"))):
        if os.path.basename(name) not in kept_names:
            try:
                os.remove(name)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# incremental state
# ---------------------------------------------------------------------------

def file_sig(path):
    st = os.stat(path)
    return {"mtime_ns": int(st.st_mtime_ns), "size": int(st.st_size)}


def load_state():
    state = {
        "render_version": None,     # RENDER_VERSION of the last render
        "sources": {},          # src relpath -> {"mtime_ns", "size"}
        "page_entries": {},     # page_id -> last index entry written
        "session_entries": {},  # session_id -> {"urls","pages","notebook"}
        "ocr_settings": None,   # [engine, lang, psm] of last OCR pass
    }
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return state


def save_state(state):
    atomic_write(STATE_PATH, json.dumps(state, indent=2, sort_keys=True))


def source_unchanged(state, src_rel):
    """True when ``src_rel`` has the same size/mtime as the last run."""
    try:
        return state.get("sources", {}).get(src_rel) == file_sig(
            os.path.join(ROOT, src_rel))
    except OSError:
        return False


def _audio_sig(urls, out_dir):
    """(url, mtime_ns, size) per output audio file, for STT reuse keys."""
    out = []
    for u in sorted(urls):
        try:
            st = os.stat(os.path.join(out_dir, u))
            out.append((u, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((u, 0, 0))
    return out


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="Convert dumpscribe data into web output.")
    ap.add_argument("--ocr", action="store_true",
                    help="run OCR on each page and store its transcription")
    ap.add_argument("--ocr-engine", choices=("auto", "tesseract", "trocr"),
                    default="auto",
                    help="OCR backend; auto prefers TrOCR when installed (default)")
    ap.add_argument("--ocr-lang", default=ocr.DEFAULT_LANG,
                    help="Tesseract language(s), e.g. eng or eng+deu (default: eng)")
    ap.add_argument("--ocr-psm", type=int, default=6,
                    help="Tesseract page segmentation mode (default: 6)")
    ap.add_argument("--force", action="store_true",
                    help="re-render everything, ignoring the incremental state")
    ap.add_argument("--stt", action="store_true",
                    help="transcribe session audio (Moonshine) into transcripts")
    ap.add_argument("--session", default=None,
                    help="with --stt, transcribe only this session id")
    ap.add_argument("--page", default=None,
                    help="only OCR/render this one page id; others are reused")
    args = ap.parse_args(argv)

    do_ocr = args.ocr
    engine = args.ocr_engine
    if do_ocr:
        tr_ok = ocr.trocr_available()
        ts_ok = ocr.available()
        if engine == "auto":
            engine = "trocr" if tr_ok else ("tesseract" if ts_ok else None)
        elif engine == "trocr" and not tr_ok:
            print("warning: TrOCR is not available (use the project .venv); "
                  "falling back to Tesseract", file=sys.stderr)
            engine = "tesseract" if ts_ok else None
        elif engine == "tesseract" and not ts_ok:
            print("warning: tesseract is not installed; falling back to TrOCR",
                  file=sys.stderr)
            engine = "trocr" if tr_ok else None
        if engine is None:
            print("warning: --ocr requested but no OCR engine is available; "
                  "skipping OCR", file=sys.stderr)
            do_ocr = False
        else:
            print("OCR engine: %s" % engine)

    settings = [engine, args.ocr_lang, args.ocr_psm] if do_ocr else None

    do_stt = args.stt
    if do_stt and not stt.available():
        print("warning: --stt requested but STT is not available (need the "
              "project .venv with transformers + imageio-ffmpeg); skipping",
              file=sys.stderr)
        do_stt = False

    if acquire_lock():
        # released on process exit, so an early exception can't leak it
        atexit.register(release_lock)

    data_dir = paths.DATA
    out_dir = paths.OUT
    pages_out = os.path.join(out_dir, "pages")
    audio_out = os.path.join(out_dir, "audio")
    os.makedirs(pages_out, exist_ok=True)
    os.makedirs(audio_out, exist_ok=True)

    state = load_state()
    if args.force:
        state = {"render_version": None, "sources": {}, "page_entries": {},
                 "session_entries": {},
                 "ocr_settings": None}
    elif state.get("render_version") != RENDER_VERSION:
        # Renderer changed (e.g. ink-bounds clipping): ignore the sources
        # signature cache so every page is re-rendered once, then go back to
        # incremental. Keep page_entries (rect/svg/thumb paths are stable).
        # v4: seeds pen_tmin/pen_tmax capture bounds into page_entries so
        # capture stamps can be frozen against future offset changes.
        print("renderer changed (v%s -> v%s); re-rendering all pages"
              % (state.get("render_version"), RENDER_VERSION))
        state["sources"] = {}

    pages = []
    skipped_pages = 0
    offset = pen_offset_ms(data_dir)   # pen-epoch -> real-time conversion
    titles = load_titles()
    for stf in sorted(glob.glob(os.path.join(data_dir, "data", "*", "*", "*.stf"))):
        page_id = os.path.basename(os.path.dirname(os.path.dirname(stf)))
        src_rel = os.path.relpath(stf, ROOT)
        sig = file_sig(stf)
        prev = state.get("page_entries", {}).get(page_id)
        if args.page is not None and page_id != args.page:
            # single-page mode (on-demand OCR): reuse other pages so
            # index.json stays complete without re-touching them.
            if prev is not None:
                state["sources"][src_rel] = file_sig(stf)
                entry = dict(prev)
                apply_title(entry, page_id, titles)
                pages.append(entry)
                skipped_pages += 1
                print("page %s: unchanged, skipping" % page_id)
                continue
            this_ocr = False  # never encoded before; render below, no OCR
        else:
            this_ocr = do_ocr
        reuse = (not args.force) and \
            state.get("sources", {}).get(src_rel) == sig and prev is not None and \
            os.path.exists(os.path.join(pages_out, page_id, "page.svg")) and \
            ((not do_ocr) or
             (state.get("ocr_settings") == settings and prev.get("ocr")))
        if reuse:
            state["sources"][src_rel] = sig
            entry = dict(prev)
            apply_title(entry, page_id, titles)
            state["page_entries"][page_id] = entry
            pages.append(entry)
            skipped_pages += 1
            print("page %s: unchanged, skipping" % page_id)
            continue

        strokes, tmin, tmax = load_strokes(stf)
        page_dir = os.path.join(pages_out, page_id)
        os.makedirs(page_dir, exist_ok=True)
        bounds = ink_bounds(strokes)
        render_svg(strokes, os.path.join(page_dir, "page.svg"), bounds)
        render_thumb(strokes, os.path.join(page_dir, "thumb.png"), bounds=bounds)
        entry = {
            "id": page_id,
            "strokes": len(strokes),
            "bounds": [round(b, 1) for b in bounds],
            "svg": "pages/%s/page.svg" % page_id,
            "thumb": "pages/%s/thumb.png" % page_id,
        }
        if offset is not None and tmin is not None:
            pt = prev or {}
            if pt.get("captured") and pt.get("pen_tmin") == tmin:
                # freeze: keep stamps from the offset in effect when the
                # page was synced (see the session-freeze note below)
                entry["captured"] = pt["captured"]
                if tmax == pt.get("pen_tmax"):
                    entry["captured_end"] = pt.get("captured_end")
                else:
                    entry["captured_end"] = pen_ms_to_iso(tmax, offset)
            else:
                entry["captured"] = pen_ms_to_iso(tmin, offset)
                entry["captured_end"] = pen_ms_to_iso(tmax, offset)
            entry["pen_tmin"] = tmin
            entry["pen_tmax"] = tmax
        title = titles.get(page_id)
        if title:
            entry["title"] = title
        if this_ocr:
            try:
                text, kept = ocr_page(strokes, page_dir, engine,
                                      args.ocr_lang, args.ocr_psm)
                prune_ocr_pngs(page_dir, kept)
            except Exception as e:
                print("warning: OCR failed for %s: %s" % (page_id, e),
                      file=sys.stderr)
                text = None
            if text:
                atomic_write(os.path.join(page_dir, "text.txt"), text + "\n")
                entry["text"] = text
                entry["ocr"] = True
                entry["ocr_engine"] = engine
            else:
                # OCR failed on previously valid transcription: drop it
                try:
                    os.remove(os.path.join(page_dir, "text.txt"))
                except OSError:
                    pass
                entry["ocr"] = False
        else:
            # Re-rendered without OCR against changed ink: any text/ocr
            # images from the previous run are invalid, drop them.
            for stale in (["text.txt"] +
                          [os.path.basename(x) for x in
                           glob.glob(os.path.join(page_dir, "ocr*.png"))]):
                try:
                    os.remove(os.path.join(page_dir, stale))
                except OSError:
                    pass
        state["sources"][src_rel] = sig
        state["page_entries"][page_id] = entry
        pages.append(entry)

    sessions = collect_sessions(data_dir)
    kept_audio = set()
    skipped_sessions = 0
    for s in sessions:
        sigs = {os.path.relpath(os.path.join(data_dir, aac), ROOT): None
                for aac in s["audio"]}
        sigs = {rel: file_sig(os.path.join(ROOT, rel)) for rel in sigs}
        prev = state.get("session_entries", {}).get(s["id"])
        # Freeze stamps computed under the offset in effect when the
        # session was downloaded: a pen-clock correction rewrites the
        # global offset, which would otherwise mis-stamp every asset
        # recorded before the fix (their pen times encode the old clock).
        if prev and prev.get("captured") and \
                prev.get("pen_tstart") == s.get("pen_tstart"):
            s["captured"] = prev["captured"]
            if prev.get("pen_tend") == s.get("pen_tend"):
                s["captured_end"] = prev.get("captured_end")
            # else: end grew (appended audio part); keep the fresh end,
            # which was converted with the offset valid for that part
        reuse = (not args.force) and bool(sigs) and prev is not None and \
            prev.get("pages") == s["pages"] and \
            prev.get("notebook") == s["notebook"]
        if reuse:
            if any(state.get("sources", {}).get(rel) != sig
                   for rel, sig in sigs.items()):
                reuse = False
        if reuse:
            for u in prev.get("urls", []):
                if not os.path.exists(os.path.join(out_dir, u)):
                    reuse = False
                    break
        if reuse:
            reused_urls = prev["urls"]
            # keep new parts found at the end of an existing recording
            for i, aac in enumerate(s["audio"]):
                expected = "audio/%s.%d.aac" % (s["id"], i)
                if expected not in reused_urls:
                    reused_urls.append(expected)
                    reuse = False  # a new part was appended, copy it below
            s["urls"] = reused_urls
        if not reuse:
            urls = []
            for i, aac in enumerate(s["audio"]):
                src = os.path.join(data_dir, aac)
                dst = os.path.join(audio_out, "%s.%d.aac" % (s["id"], i))
                fd, tmp = tempfile.mkstemp(
                    dir=audio_out, prefix=s["id"] + ".", suffix=".aac.tmp")
                try:
                    with open(src, "rb") as fi, os.fdopen(fd, "wb") as fo:
                        shutil.copyfileobj(fi, fo)
                    os.replace(tmp, dst)
                    urls.append("audio/" + os.path.basename(dst))
                except (OSError, KeyError) as e:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    print("audio copy failed for %s: %s" % (s["id"], e),
                          file=sys.stderr)
            s["urls"] = urls
        else:
            skipped_sessions += 1
            print("session %s: unchanged, skipping" % s["id"])
        state["sources"].update(sigs)
        entry = {
            "urls": s["urls"], "pages": s["pages"], "notebook": s["notebook"],
        }
        for k in ("captured", "captured_end", "pen_tstart", "pen_tend"):
            if s.get(k) is not None:
                entry[k] = s[k]
        # carry forward transcripts/stt markers from a previous run so a
        # non-stt convert doesn't drop them; sessions rebuilt every run
        if isinstance(prev, dict):
            for k in ("stt", "stt_sig", "transcript"):
                if prev.get(k) is not None:
                    entry[k] = prev[k]
                    if k == "transcript":
                        s["transcript"] = prev[k]
        state["session_entries"][s["id"]] = entry
        for u in s["urls"]:
            kept_audio.add(os.path.basename(u))

    # Session -> page linking. Session page addresses can carry a leading
    # section component (5 parts) that .stf page ids (4 parts) lack.
    page_ids = {p["id"] for p in pages}
    for s in sessions:
        linked = []
        for raw in s.get("pages", []):
            if raw in page_ids:
                linked.append(raw)
                continue
            parts = raw.split(".")
            if len(parts) == 5 and ".".join(parts[1:]) in page_ids:
                linked.append(".".join(parts[1:]))
                continue
            print("warning: recording %s references page %s which is not "
                  "present in the notebook" % (s["id"], raw), file=sys.stderr)
        s["pages"] = linked

    # Prune output for sources that no longer exist (fixes accumulation of
    # stale assets, and makes deletions from the pen/data dir stick).
    current_pages = {p["id"] for p in pages}
    for d in sorted(glob.glob(os.path.join(pages_out, "*"))):
        if os.path.isdir(d) and os.path.basename(d) not in current_pages:
            shutil.rmtree(d, ignore_errors=True)
            print("pruned output/pages/%s (no matching source)"
                  % os.path.basename(d))
    for f in sorted(glob.glob(os.path.join(audio_out, "*.aac"))):
        if os.path.basename(f) not in kept_audio:
            try:
                os.remove(f)
                print("pruned output/audio/%s (no matching source)"
                      % os.path.basename(f))
            except OSError:
                pass

    # Speech-to-text: transcribe each session's audio incrementally.
    if do_stt:
        total = len(sessions) - (1 if args.session else 0)
        done = 0
        for s in sessions:
            sid = s["id"]
            if args.session and sid != args.session:
                continue
            urls = s.get("urls") or []
            if not urls:
                s["transcript"] = ""
                continue
            sig = _audio_sig(urls, out_dir)
            prev = state.get("session_entries", {}).get(sid, {}) or {}
            prev_stt = prev.get("stt") or {}
            # JSON stores tuples as lists; normalize both sides before comparing
            cur_sig = [list(t) for t in sig]
            prev_sig = prev.get("stt_sig") or []
            tx_path = os.path.join(audio_out, sid + ".txt")
            if (not args.force) and prev_stt.get("done") and \
                    prev_stt.get("model") == stt.MOONSHINE_MODEL and \
                    prev_sig == cur_sig and os.path.exists(tx_path):
                s["transcript"] = prev.get("transcript", "")
                print("session %s: transcript unchanged, skipping" % sid)
                continue
            aac_paths = [os.path.join(out_dir, u) for u in urls]
            audio = stt.seq_to_floats(aac_paths)
            try:
                text = stt.transcribe(audio)
            except Exception as e:
                print("warning: STT failed for %s: %s" % (sid, e),
                      file=sys.stderr)
                continue
            s["transcript"] = text or ""
            if text:
                atomic_write(tx_path, text + "\n")
            else:
                try:
                    os.remove(tx_path)
                except OSError:
                    pass
            entry = state["session_entries"].setdefault(sid, {})
            entry["stt"] = {"done": True, "model": stt.MOONSHINE_MODEL}
            entry["stt_sig"] = sig
            entry["transcript"] = s["transcript"]
            done += 1
            print("session %s: transcribed (%d/%d)" % (sid, done, total))

    state["ocr_settings"] = settings
    state["render_version"] = RENDER_VERSION
    # chronological order (oldest first) for both lists; assets without a
    # capture time fall to the end
    pages.sort(key=lambda p: (p.get("captured") or "9999", p["id"]))
    sessions.sort(key=lambda s: (s.get("captured") or "9999", s["id"]))
    atomic_write(os.path.join(out_dir, "index.json"),
                 json.dumps({"pages": pages, "sessions": sessions}, indent=2))
    save_state(state)
    print("rendered %d pages (%d unchanged, %d sessions (%d unchanged))"
          % (len(pages), skipped_pages, len(sessions), skipped_sessions))


if __name__ == "__main__":
    main()
