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
import dataclasses
import datetime
import fcntl
import glob
import json
import logging
import math
import os
import re
import shutil
import sys
import tempfile
import threading
import time

import paths
sys.path.insert(0, paths.VENDOR)
import parsestf
import ocr
import stt
import telemetry

log = telemetry.get_logger("convert")

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
_held = threading.local()   # the calling thread's lock fd, if it holds one


# ---------------------------------------------------------------------------
# run lock
# ---------------------------------------------------------------------------

def acquire_lock(wait=3.0):
    """Take the run lock: an exclusive flock on ``.convert.lock``.

    Returns True if this thread took the lock. Returns False without
    locking when ``ECHO_LOCK_HELD`` is set: serve.py sets that before
    launching a reconcile ``convert`` while it already holds the lock, so
    the child can't deadlock against its parent.

    The kernel drops the lock when its holder exits, however it dies, so a
    killed convert or server never leaves a stale lock behind. The file
    itself stays; it records the holder's pid for diagnostics only.

    Blocks/retries up to ``wait`` seconds; raises RuntimeError if the
    lock stays held that long.
    """
    if os.environ.get("ECHO_LOCK_HELD"):
        return False
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    started = time.monotonic()
    deadline = started + wait
    waited = False
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if not waited:
                waited = True
                log.info("lock.wait", extra={"holder_pid": lock_owner()})
            if time.monotonic() >= deadline:
                os.close(fd)
                telemetry.warn(log, "lock.busy",
                               "run lock still held after %.1fs" % wait,
                               holder_pid=lock_owner(), wait_s=wait)
                raise RuntimeError(
                    "Another convert is running (lock file: %s, pid %s). "
                    "Try again shortly." % (LOCK_PATH, lock_owner()))
            time.sleep(0.1)
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode("ascii"))
    _held.fd = fd
    return True


def release_lock():
    fd = getattr(_held, "fd", None)
    if fd is None:
        return
    _held.fd = None
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def lock_owner():
    """Pid recorded by the last lock holder, or None when unknown."""
    try:
        with open(LOCK_PATH) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def lock_is_held():
    """True when some process currently holds the run lock."""
    try:
        fd = os.open(LOCK_PATH, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


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
    """Parse a .stf into (strokes, tmin, tmax) with pen-time ms bounds.

    A file cut off mid-stroke (pen unplugged during sync) keeps every
    complete stroke; only the unfinished one is dropped.
    """
    with open(stf_path, "rb") as f:
        sc = StrokeCollector(f)
        try:
            sc.parse()
        except EOFError:
            telemetry.warn(log, "stf.truncated",
                           "%s is truncated; keeping %d complete strokes"
                           % (stf_path, len(sc.strokes)),
                           path=stf_path, strokes=len(sc.strokes))
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


def stroke_width(avg_force):
    """Rendered line width (STF units) for a stroke's mean pen force."""
    return max(1.5, (max(avg_force, 0) ** 0.3) * 3)


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
        width = stroke_width(avg_f)
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
            ctx.arc(gx, gy, r, 0, math.tau)
            ctx.fill()
    # ink
    ctx.set_source_rgb(*[c / 255.0 for c in INK])
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    ctx.set_line_join(cairo.LINE_JOIN_ROUND)
    for stroke in strokes:
        avg_f = sum(p[2] for p in stroke) / len(stroke)
        ctx.set_line_width(stroke_width(avg_f))
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


def group_lines(strokes, oversize_ratio=None):
    """Split strokes into text lines using their vertical position.

    Line bands are found from letter-sized strokes; small strokes (dots,
    accents) and, with ``oversize_ratio``, strokes taller than that many
    median heights (circles, brackets, margin lines) are then assigned to
    the nearest band instead of starting a line of their own.
    """
    if not strokes:
        return []
    info = []
    for stroke in strokes:
        _, y0, _, y1 = stroke_bounds(stroke)
        info.append((stroke, y0, y1, (y0 + y1) / 2.0, y1 - y0))
    heights = sorted(i[4] for i in info)
    median_h = heights[len(heights) // 2] or 1.0
    ceiling = oversize_ceiling(strokes, oversize_ratio)
    tall = [i for i in info if 0.35 * median_h <= i[4] <= ceiling] or info

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


def oversize_ceiling(strokes, ratio):
    """Height above which a stroke counts as oversize on this page.

    ``ratio`` times the median stroke height; infinite when ``ratio`` is
    None (no stroke is oversize).
    """
    if not ratio or not strokes:
        return float("inf")
    heights = sorted(stroke_bounds(s)[3] - stroke_bounds(s)[1]
                     for s in strokes)
    return ratio * (heights[len(heights) // 2] or 1.0)


def line_frame(strokes, ceiling):
    """The strokes that define a line's extent: all but the oversize ones.

    A margin line or a bracket assigned to a text line must not stretch
    that line's image or its height.
    """
    frame = [s for s in strokes
             if stroke_bounds(s)[3] - stroke_bounds(s)[1] <= ceiling]
    return frame or strokes


def split_wide_line(strokes, max_aspect, ceiling=float("inf")):
    """Split one line into chunks no wider than ``max_aspect`` × its height.

    TrOCR squeezes every image into a 384×384 square, so a very long line
    loses most of its horizontal detail. Cuts go at the widest gaps
    between strokes (word spaces), never through a stroke; a line with no
    gaps, or a short one, is returned whole. The height ignores strokes
    taller than ``ceiling``.
    """
    if not max_aspect or len(strokes) < 2:
        return [strokes]
    bounds = [stroke_bounds(s) for s in strokes]
    frame = [stroke_bounds(s) for s in line_frame(strokes, ceiling)]
    width = max(b[2] for b in bounds) - min(b[0] for b in bounds)
    height = max(b[3] for b in frame) - min(b[1] for b in frame)
    pieces = math.ceil(width / max(1.0, height) / max_aspect)
    if pieces < 2:
        return [strokes]
    order = sorted(range(len(strokes)), key=lambda i: bounds[i][0])
    gaps = []
    right = bounds[order[0]][2]
    for pos in range(1, len(order)):
        gap = bounds[order[pos]][0] - right
        if gap > 0:
            gaps.append((gap, pos))
        right = max(right, bounds[order[pos]][2])
    cuts = sorted(pos for _, pos in sorted(gaps, reverse=True)[:pieces - 1])
    chunks = []
    start = 0
    for cut in cuts + [len(order)]:
        chunks.append([strokes[i] for i in order[start:cut]])
        start = cut
    return chunks


def render_line_png(strokes, png_path, width=2400, max_height=1200,
                    padding=60, frame=None):
    """Render one text line for TrOCR, cropped to ``frame`` (default: all
    strokes); ink outside the crop is clipped."""
    pts = [p for s in (frame or strokes) for p in s]
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

@dataclasses.dataclass(frozen=True)
class OcrJob:
    """Which recognizer runs, with which options; part of the reuse key."""
    engine: str
    lang: str
    psm: int
    profile: str = "default"

    def settings(self):
        return [self.engine, self.lang, self.psm, self.profile]


@dataclasses.dataclass(frozen=True)
class PageInk:
    page_id: str
    strokes: list
    page_dir: str


@dataclasses.dataclass(frozen=True)
class OcrResult:
    """Recognized text, the ocr*.png files it came from, and whether every
    line was read (False when some lines failed and should be retried)."""
    text: str
    kept: list
    complete: bool


OCR_ENTRY_KEYS = ("text", "ocr", "ocr_engine", "ocr_settings")


def ocr_page(strokes, page_dir, job):
    """OCR one page with the selected backend.

    Files are published atomically; the caller prunes any older ocr*.png
    that are not in ``OcrResult.kept``. Raises RuntimeError when the
    recognizer failed on every line, so a broken model is not mistaken
    for a blank page.
    """
    prof = ocr.profile(job.profile)
    if job.engine != "trocr":
        ocr_png = os.path.join(page_dir, "ocr.png")
        bounds = ink_bounds(strokes) if prof.tesseract_on_ink else None
        render_png(strokes, ocr_png, width=2000, grid=False, bounds=bounds)
        text = ocr.image_to_text(ocr_png, lang=job.lang, psm=job.psm)
        return OcrResult(clean_ocr_text(text), ["ocr.png"], True)
    texts, kept = [], []
    lines = group_lines(strokes, oversize_ratio=prof.oversize_ratio)
    ceiling = oversize_ceiling(strokes, prof.oversize_ratio)
    failures = 0
    for i, line in enumerate(lines):
        chunks = split_wide_line(line, prof.max_line_aspect, ceiling)
        words = []
        for j, chunk in enumerate(chunks):
            name = ("ocr-line-%d.png" % i if len(chunks) == 1
                    else "ocr-line-%d-%d.png" % (i, j))
            line_png = os.path.join(page_dir, name)
            render_line_png(chunk, line_png,
                            frame=line_frame(chunk, ceiling))
            kept.append(name)
            try:
                words.append(ocr.trocr_image_to_text(line_png, prof))
            except Exception as e:
                failures += 1
                telemetry.warn(log, "ocr.line_failed",
                               "TrOCR failed on line %d: %s" % (i, e),
                               line=i, chunk=j, png=line_png,
                               exc_type=type(e).__name__)
        texts.append(" ".join(w for w in words if w))
    if kept and failures == len(kept):
        raise RuntimeError("TrOCR failed on all %d lines" % failures)
    text = clean_ocr_text("\n".join(t for t in texts if t))
    return OcrResult(text, kept, failures == 0)


def has_transcription(entry):
    return bool(entry.get("ocr") or entry.get("text"))


def page_reusable(prev, source_unchanged, has_svg, job, state):
    """True when a page's previous render (and OCR, if wanted) still holds."""
    if prev is None or not source_unchanged or not has_svg:
        return False
    if job is None:
        return True
    done_with = prev.get("ocr_settings", state.get("ocr_settings"))
    return bool(prev.get("ocr")) and done_with == job.settings()


def carry_ocr(entry, last):
    """Copy a still-valid transcription from the previous index entry."""
    for key in OCR_ENTRY_KEYS:
        if key in last:
            entry[key] = last[key]


def drop_ocr_files(page_dir):
    """Remove text.txt and ocr*.png left by an outdated transcription."""
    stale = [os.path.join(page_dir, "text.txt")]
    stale += glob.glob(os.path.join(page_dir, "ocr*.png"))
    for path in stale:
        try:
            os.remove(path)
        except OSError:
            pass


def transcribe_page(entry, ink, job, last_good):
    """OCR ``ink`` into ``entry``.

    ``last_good`` is the previous entry when the ink is unchanged (else
    empty): if the recognizer itself fails, that transcription is kept
    rather than replaced by nothing. ``entry["ocr"]`` is True only when
    every line was read, so a partial result is retried by the next OCR
    run; a page with no recognizable text counts as done.
    """
    try:
        result = ocr_page(ink.strokes, ink.page_dir, job)
    except Exception as e:
        telemetry.warn(log, "ocr.page_failed",
                       "OCR failed for %s: %s" % (ink.page_id, e),
                       page=ink.page_id, engine=job.engine,
                       exc_type=type(e).__name__,
                       kept_previous=has_transcription(last_good))
        if has_transcription(last_good):
            carry_ocr(entry, last_good)
            return
        drop_ocr_files(ink.page_dir)
        entry["ocr"] = False
        return
    prune_ocr_pngs(ink.page_dir, result.kept)
    log.debug("ocr.page", extra={"page": ink.page_id, "engine": job.engine,
                                 "lines": len(result.kept),
                                 "complete": result.complete,
                                 "chars": len(result.text)})
    entry.update({"ocr": result.complete, "ocr_engine": job.engine,
                  "ocr_settings": job.settings()})
    text_path = os.path.join(ink.page_dir, "text.txt")
    if result.text:
        atomic_write(text_path, result.text + "\n")
        entry["text"] = result.text
        return
    try:
        os.remove(text_path)
    except OSError:
        pass


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


def empty_state():
    return {
        "render_version": None,     # RENDER_VERSION of the last render
        "sources": {},          # src relpath -> {"mtime_ns", "size"}
        "page_entries": {},     # page_id -> last index entry written
        "session_entries": {},  # session_id -> {"urls","pages","notebook"}
        "ocr_settings": None,   # OCR settings of pages lacking their own
    }


def load_state():
    state = empty_state()
    try:
        with open(STATE_PATH) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return state
    if not isinstance(saved, dict):
        return state
    for key, value in saved.items():
        if key in state and isinstance(state[key], dict) and \
                not isinstance(value, dict):
            continue
        state[key] = value
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
    ap.add_argument("--ocr-profile", choices=sorted(ocr.PROFILES),
                    default=ocr.default_profile_name(),
                    help="segmentation/decoding profile; 'legacy' is the "
                         "original behaviour (default: $ECHO_OCR_PROFILE "
                         "or 'default')")
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
            telemetry.warn(log, "ocr.engine_fallback",
                           "TrOCR is not available (use the project .venv); "
                           "falling back to Tesseract",
                           wanted="trocr", tesseract=ts_ok)
            engine = "tesseract" if ts_ok else None
        elif engine == "tesseract" and not ts_ok:
            telemetry.warn(log, "ocr.engine_fallback",
                           "tesseract is not installed; falling back to TrOCR",
                           wanted="tesseract", trocr=tr_ok)
            engine = "trocr" if tr_ok else None
        if engine is None:
            telemetry.warn(log, "ocr.unavailable",
                           "--ocr requested but no OCR engine is available; "
                           "skipping OCR (run ./run doctor)")
            do_ocr = False
        else:
            print("OCR engine: %s" % engine)

    ocr_job = None
    if do_ocr and engine:
        ocr_job = OcrJob(engine, args.ocr_lang, args.ocr_psm,
                         args.ocr_profile)

    do_stt = args.stt
    if do_stt and not stt.available():
        telemetry.warn(log, "stt.unavailable",
                       "--stt requested but STT is not available (need the "
                       "project .venv with transformers + imageio-ffmpeg); "
                       "skipping (run ./run doctor)")
        do_stt = False

    started = telemetry.monotonic()
    log.info("convert.start", extra={
        "root": ROOT, "force": args.force, "page": args.page,
        "session": args.session, "stt": do_stt,
        "ocr_engine": ocr_job.engine if ocr_job else None,
        "ocr_profile": ocr_job.profile if ocr_job else None})
    took_lock = acquire_lock()
    try:
        convert_library(args, ocr_job, do_stt)
    except BaseException as e:
        interrupted = isinstance(e, KeyboardInterrupt)
        # Ctrl-C is a choice, not a fault: WARNING, so ERROR keeps meaning
        # that a human must act
        log.log(logging.WARNING if interrupted else logging.ERROR,
                "convert.failed", exc_info=not interrupted, extra={
                    "duration_ms": round(
                        (telemetry.monotonic() - started) * 1000, 1),
                    "interrupted": interrupted})
        raise
    finally:
        if took_lock:
            release_lock()


@dataclasses.dataclass
class Library:
    """One convert run: options, incremental state and output locations."""
    args: argparse.Namespace
    ocr_job: "OcrJob | None"
    do_stt: bool
    state: dict
    # source signatures recorded by the previous run, kept even when
    # --force or a renderer upgrade discards them from ``state``: they tell
    # whether a re-rendered page still has the same ink
    last_sources: dict
    titles: dict
    offset: "int | None"
    data_dir: str = paths.DATA
    out_dir: str = paths.OUT

    @property
    def pages_out(self):
        return os.path.join(self.out_dir, "pages")

    @property
    def audio_out(self):
        return os.path.join(self.out_dir, "audio")


def open_library(args, ocr_job, do_stt):
    state = load_state()
    last_sources = dict(state["sources"])
    if args.force:
        # re-render and re-copy everything, but keep the per-asset entries:
        # transcriptions, transcripts and frozen capture stamps stay valid
        state["sources"] = {}
    elif state.get("render_version") != RENDER_VERSION:
        print("renderer changed (v%s -> v%s); re-rendering all pages"
              % (state.get("render_version"), RENDER_VERSION))
        state["sources"] = {}
    # pages not re-rendered yet have no source signature, so recording the
    # version now is safe even if this run is interrupted
    state["render_version"] = RENDER_VERSION
    lib = Library(args=args, ocr_job=ocr_job, do_stt=do_stt, state=state,
                  last_sources=last_sources, titles=load_titles(),
                  offset=pen_offset_ms(paths.DATA))
    os.makedirs(lib.pages_out, exist_ok=True)
    os.makedirs(lib.audio_out, exist_ok=True)
    return lib


class PhaseTimer:
    """Wall time per pipeline phase, for the convert.done record."""

    def __init__(self):
        self.ms = {}
        self._start = telemetry.monotonic()

    def lap(self, phase):
        now = telemetry.monotonic()
        self.ms[phase] = round((now - self._start) * 1000, 1)
        self._start = now


def convert_library(args, ocr_job, do_stt):
    """Render, transcribe and index everything under data/ (lock held)."""
    timer = PhaseTimer()
    lib = open_library(args, ocr_job, do_stt)
    pages, skipped_pages = convert_pages(lib)
    timer.lap("pages")
    sessions, kept_audio, skipped_sessions = convert_sessions(lib)
    link_sessions(sessions, {p["id"] for p in pages})
    timer.lap("sessions")
    pruned = prune_outputs(lib, {p["id"] for p in pages}, kept_audio)
    if lib.do_stt:
        transcribe_sessions(lib, sessions)
        timer.lap("stt")
    write_index(lib, pages, sessions)
    timer.lap("index")
    log.info("convert.done", extra={
        "pages": len(pages), "pages_unchanged": skipped_pages,
        "pages_ocr": sum(1 for p in pages if p.get("ocr")),
        "sessions": len(sessions), "sessions_unchanged": skipped_sessions,
        "pruned": pruned, "phase_ms": timer.ms,
        "duration_ms": round(sum(timer.ms.values()), 1)})
    print("rendered %d pages (%d unchanged, %d sessions (%d unchanged))"
          % (len(pages), skipped_pages, len(sessions), skipped_sessions))


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------

def convert_pages(lib):
    """Render (and optionally OCR) every .stf; returns (entries, skipped)."""
    pages = []
    skipped = 0
    pattern = os.path.join(lib.data_dir, "data", "*", "*", "*.stf")
    for stf in sorted(glob.glob(pattern)):
        entry, reused = convert_page(lib, stf)
        if entry is None:
            continue
        pages.append(entry)
        skipped += reused
    return pages, skipped


def convert_page(lib, stf):
    """One page's index entry, and whether it was reused unchanged."""
    page_id = os.path.basename(os.path.dirname(os.path.dirname(stf)))
    src_rel = os.path.relpath(stf, ROOT)
    sig = file_sig(stf)
    state = lib.state
    prev = state["page_entries"].get(page_id)
    single_page = lib.args.page is not None
    if single_page and page_id != lib.args.page and prev is not None:
        # on-demand OCR of another page: keep this one as it was, and leave
        # its recorded signature alone so an edit is still picked up later
        return reuse_page(lib, page_id, prev), True
    job = lib.ocr_job if not single_page or page_id == lib.args.page else None
    has_svg = os.path.exists(os.path.join(lib.pages_out, page_id, "page.svg"))
    if page_reusable(prev, state["sources"].get(src_rel) == sig, has_svg,
                     job, state):
        state["sources"][src_rel] = sig
        return reuse_page(lib, page_id, prev), True
    try:
        entry, ink = render_page(lib, page_id, stf, prev)
    except Exception as e:
        telemetry.warn(log, "page.failed",
                       "page %s could not be converted (%s: %s); %s"
                       % (page_id, type(e).__name__, e,
                          "keeping its last render" if prev
                          else "skipping it"),
                       page=page_id, source=src_rel,
                       exc_type=type(e).__name__, kept_previous=bool(prev))
        return (dict(prev), True) if prev else (None, False)
    last = prev or {}
    same_ink = lib.last_sources.get(src_rel) == sig
    if job is not None:
        transcribe_page(entry, ink, job, last if same_ink else {})
    elif same_ink and has_transcription(last):
        carry_ocr(entry, last)
    else:
        drop_ocr_files(ink.page_dir)
    state["sources"][src_rel] = sig
    state["page_entries"][page_id] = entry
    if job is not None:
        save_state(state)   # an interrupted OCR run keeps finished pages
    return entry, False


def reuse_page(lib, page_id, prev):
    entry = dict(prev)
    apply_title(entry, page_id, lib.titles)
    lib.state["page_entries"][page_id] = entry
    print("page %s: unchanged, skipping" % page_id)
    return entry


def render_page(lib, page_id, stf, prev):
    """Parse and render one page; returns (index entry, PageInk)."""
    strokes, tmin, tmax = load_strokes(stf)
    page_dir = os.path.join(lib.pages_out, page_id)
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
    if lib.offset is not None and tmin is not None:
        stamp_page(entry, prev or {}, (tmin, tmax), lib.offset)
    title = lib.titles.get(page_id)
    if title:
        entry["title"] = title
    return entry, PageInk(page_id, strokes, page_dir)


def stamp_page(entry, prev, pen_span, offset):
    """Capture start/end stamps, frozen from ``prev`` when its ink matches.

    Stamps are frozen at the offset in effect when the page was synced: a
    later pen-clock correction rewrites the global offset, which would
    otherwise mis-stamp pages written before the fix.
    """
    tmin, tmax = pen_span
    if prev.get("captured") and prev.get("pen_tmin") == tmin:
        entry["captured"] = prev["captured"]
        if tmax == prev.get("pen_tmax"):
            entry["captured_end"] = prev.get("captured_end")
        else:
            entry["captured_end"] = pen_ms_to_iso(tmax, offset)
    else:
        entry["captured"] = pen_ms_to_iso(tmin, offset)
        entry["captured_end"] = pen_ms_to_iso(tmax, offset)
    entry["pen_tmin"] = tmin
    entry["pen_tmax"] = tmax


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------

SESSION_CARRY_KEYS = ("stt", "stt_sig", "transcript")


def convert_sessions(lib):
    """Copy session audio; returns (sessions, kept audio names, skipped)."""
    sessions = collect_sessions(lib.data_dir)
    kept_audio = set()
    skipped = 0
    for s in sessions:
        sigs = {os.path.relpath(os.path.join(lib.data_dir, aac), ROOT): None
                for aac in s["audio"]}
        sigs = {rel: file_sig(os.path.join(ROOT, rel)) for rel in sigs}
        prev = lib.state["session_entries"].get(s["id"])
        freeze_session_stamps(s, prev)
        if session_reusable(lib, s, prev, sigs):
            s["urls"] = list(prev["urls"])
            skipped += 1
            print("session %s: unchanged, skipping" % s["id"])
        else:
            s["urls"] = copy_session_audio(lib, s)
        lib.state["sources"].update(sigs)
        lib.state["session_entries"][s["id"]] = session_entry(s, prev)
        if isinstance(prev, dict) and prev.get("transcript") is not None:
            s["transcript"] = prev["transcript"]
        kept_audio.update(os.path.basename(u) for u in s["urls"])
    return sessions, kept_audio, skipped


def freeze_session_stamps(s, prev):
    """Keep capture stamps computed under the offset valid at download.

    A pen-clock correction rewrites the global offset, which would
    otherwise mis-stamp every recording made before the fix. When the end
    grew (an audio part was appended) the fresh end is kept.
    """
    if not prev or not prev.get("captured"):
        return
    if prev.get("pen_tstart") != s.get("pen_tstart"):
        return
    s["captured"] = prev["captured"]
    if prev.get("pen_tend") == s.get("pen_tend"):
        s["captured_end"] = prev.get("captured_end")


def session_audio_urls(s):
    return ["audio/%s.%d.aac" % (s["id"], i) for i in range(len(s["audio"]))]


def session_reusable(lib, s, prev, sigs):
    """True when the session's copied audio matches its sources exactly."""
    if lib.args.force or not sigs or prev is None:
        return False
    if prev.get("pages") != s["pages"] or prev.get("notebook") != s["notebook"]:
        return False
    if any(lib.state["sources"].get(rel) != sig for rel, sig in sigs.items()):
        return False
    if prev.get("urls") != session_audio_urls(s):
        return False
    return all(os.path.exists(os.path.join(lib.out_dir, u))
               for u in prev["urls"])


def copy_session_audio(lib, s):
    """Copy every audio part into output/audio atomically; returns urls."""
    urls = []
    for i, aac in enumerate(s["audio"]):
        src = os.path.join(lib.data_dir, aac)
        dst = os.path.join(lib.audio_out, "%s.%d.aac" % (s["id"], i))
        fd, tmp = tempfile.mkstemp(
            dir=lib.audio_out, prefix=s["id"] + ".", suffix=".aac.tmp")
        try:
            with open(src, "rb") as fi, os.fdopen(fd, "wb") as fo:
                shutil.copyfileobj(fi, fo)
            os.replace(tmp, dst)
            urls.append("audio/" + os.path.basename(dst))
        except OSError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            telemetry.warn(log, "audio.copy_failed",
                           "audio copy failed for %s: %s" % (s["id"], e),
                           session=s["id"], part=i, source=src)
    return urls


def session_entry(s, prev):
    """State entry for a session; carries STT results over from ``prev``."""
    entry = {"urls": s["urls"], "pages": s["pages"], "notebook": s["notebook"]}
    for k in ("captured", "captured_end", "pen_tstart", "pen_tend"):
        if s.get(k) is not None:
            entry[k] = s[k]
    if not isinstance(prev, dict):
        return entry
    for k in SESSION_CARRY_KEYS:
        if prev.get(k) is not None:
            entry[k] = prev[k]
    return entry


def link_sessions(sessions, page_ids):
    """Resolve session page addresses to page ids that exist.

    Session addresses can carry a leading section component (5 parts)
    that .stf page ids (4 parts) lack.
    """
    for s in sessions:
        linked = []
        for raw in s.get("pages", []):
            short = ".".join(raw.split(".")[1:])
            if raw in page_ids:
                linked.append(raw)
            elif raw.count(".") == 4 and short in page_ids:
                linked.append(short)
            else:
                telemetry.warn(log, "session.missing_page",
                               "recording %s references page %s which is "
                               "not present in the notebook" % (s["id"], raw),
                               session=s["id"], page=raw)
        s["pages"] = linked


def prune_outputs(lib, page_ids, kept_audio):
    """Remove output whose source is gone, so deletions from data/ stick.

    Returns how many page directories and audio files were removed.
    """
    pruned = 0
    for d in sorted(glob.glob(os.path.join(lib.pages_out, "*"))):
        if os.path.isdir(d) and os.path.basename(d) not in page_ids:
            shutil.rmtree(d, ignore_errors=True)
            pruned += 1
            print("pruned output/pages/%s (no matching source)"
                  % os.path.basename(d))
    for f in sorted(glob.glob(os.path.join(lib.audio_out, "*.aac"))):
        if os.path.basename(f) in kept_audio:
            continue
        try:
            os.remove(f)
            pruned += 1
            print("pruned output/audio/%s (no matching source)"
                  % os.path.basename(f))
        except OSError:
            pass
    return pruned


# ---------------------------------------------------------------------------
# speech-to-text
# ---------------------------------------------------------------------------

def transcribe_sessions(lib, sessions):
    """Transcribe each selected session whose audio changed since last time."""
    wanted = [s for s in sessions
              if not lib.args.session or s["id"] == lib.args.session]
    done = 0
    for s in wanted:
        if transcribe_session(lib, s):
            done += 1
            print("session %s: transcribed (%d/%d)" % (s["id"], done,
                                                      len(wanted)))


def transcript_current(lib, s, prev, sig):
    """True when ``prev`` already holds this audio's transcript."""
    prev_stt = prev.get("stt") or {}
    if lib.args.force or not prev_stt.get("done"):
        return False
    if prev_stt.get("model") != stt.MOONSHINE_MODEL:
        return False
    # JSON stores tuples as lists; normalize before comparing
    if (prev.get("stt_sig") or []) != [list(t) for t in sig]:
        return False
    tx_path = os.path.join(lib.audio_out, s["id"] + ".txt")
    return os.path.exists(tx_path) or not prev.get("transcript")


def transcribe_session(lib, s):
    """Run STT for one session; True when a new transcript was produced."""
    sid = s["id"]
    urls = s.get("urls") or []
    if not urls:
        s["transcript"] = ""
        return False
    sig = _audio_sig(urls, lib.out_dir)
    prev = lib.state["session_entries"].get(sid) or {}
    if transcript_current(lib, s, prev, sig):
        s["transcript"] = prev.get("transcript", "")
        print("session %s: transcript unchanged, skipping" % sid)
        return False
    try:
        text = stt.transcribe(stt.seq_to_floats(
            [os.path.join(lib.out_dir, u) for u in urls]))
    except Exception as e:
        telemetry.warn(log, "stt.session_failed",
                       "STT failed for %s: %s" % (sid, e),
                       session=sid, exc_type=type(e).__name__)
        return False
    s["transcript"] = text or ""
    tx_path = os.path.join(lib.audio_out, sid + ".txt")
    if text:
        atomic_write(tx_path, text + "\n")
    else:
        try:
            os.remove(tx_path)
        except OSError:
            pass
    entry = lib.state["session_entries"].setdefault(sid, {})
    entry["stt"] = {"done": True, "model": stt.MOONSHINE_MODEL}
    entry["stt_sig"] = sig
    entry["transcript"] = s["transcript"]
    save_state(lib.state)   # an interrupted STT run keeps finished sessions
    log.info("stt.session", extra={"session": sid, "parts": len(urls),
                                   "chars": len(s["transcript"])})
    return True


def write_index(lib, pages, sessions):
    """Publish index.json and the state that produced it."""
    lib.state["ocr_settings"] = None
    # chronological order (oldest first); undated assets fall to the end
    pages.sort(key=lambda p: (p.get("captured") or "9999", p["id"]))
    sessions.sort(key=lambda s: (s.get("captured") or "9999", s["id"]))
    atomic_write(os.path.join(lib.out_dir, "index.json"),
                 json.dumps({"pages": pages, "sessions": sessions}, indent=2))
    save_state(lib.state)


if __name__ == "__main__":
    main()
