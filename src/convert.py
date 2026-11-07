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
"""
import argparse
import atexit
import glob
import json
import os
import re
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "vendor"))
import parsestf
import ocr

PAGE_W, PAGE_H = 4963, 6278  # STF "dot" units; page size used by stf2pdf
GRID = 83                     # visible dot-grid spacing in STF units
INK = (12, 22, 90)            # Livescribe ink blue

STATE_PATH = os.path.join(ROOT, ".convert-state.json")
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

    def handle_point(self, x, y, force, time):
        self.cur.append((x, y, force))

    def handle_stroke_end(self, time):
        self.strokes.append(self.cur)
        self.cur = []


def load_strokes(stf_path):
    with open(stf_path, "rb") as f:
        sc = StrokeCollector(f)
        sc.parse()
    return [s for s in sc.strokes if s]


def dot_grid():
    """Yield (cx, cy) grid points across the page."""
    r = GRID * 0.06
    for gy in range(0, PAGE_H + GRID, GRID):
        for gx in range(0, PAGE_W + GRID, GRID):
            yield gx, gy, r


def render_svg(strokes, path):
    parts = []
    parts.append('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d"'
                 % (PAGE_W, PAGE_H))
    parts.append(' width="100%" height="100%">\n')
    parts.append('<rect width="%d" height="%d" fill="#ffffff"/>\n' % (PAGE_W, PAGE_H))
    parts.append('<g fill="#c9d4e8">\n')
    for gx, gy, r in dot_grid():
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
        for gx, gy, r in dot_grid():
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


def render_thumb(strokes, png_path, size=320):
    render_png(strokes, png_path, width=size, grid=True)


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


def collect_sessions(data_dir):
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
            sessions.append({
                "id": sid,
                "notebook": notebook,
                "audio": [os.path.relpath(p, data_dir) for p in aac_parts],
                "pages": pages,
            })
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

    if acquire_lock():
        # released on process exit, so an early exception can't leak it
        atexit.register(release_lock)

    data_dir = os.path.join(ROOT, "data")
    out_dir = os.path.join(ROOT, "output")
    pages_out = os.path.join(out_dir, "pages")
    audio_out = os.path.join(out_dir, "audio")
    os.makedirs(pages_out, exist_ok=True)
    os.makedirs(audio_out, exist_ok=True)

    state = load_state()
    if args.force:
        state = {"sources": {}, "page_entries": {}, "session_entries": {},
                 "ocr_settings": None}

    pages = []
    skipped_pages = 0
    for stf in sorted(glob.glob(os.path.join(data_dir, "data", "*", "*", "*.stf"))):
        page_id = os.path.basename(os.path.dirname(os.path.dirname(stf)))
        src_rel = os.path.relpath(stf, ROOT)
        sig = file_sig(stf)
        prev = state.get("page_entries", {}).get(page_id)
        reuse = (not args.force) and \
            state.get("sources", {}).get(src_rel) == sig and prev is not None and \
            os.path.exists(os.path.join(pages_out, page_id, "page.svg")) and \
            ((not do_ocr) or
             (state.get("ocr_settings") == settings and prev.get("ocr")))
        if reuse:
            state["sources"][src_rel] = sig
            pages.append(dict(prev))
            skipped_pages += 1
            print("page %s: unchanged, skipping" % page_id)
            continue

        strokes = load_strokes(stf)
        page_dir = os.path.join(pages_out, page_id)
        os.makedirs(page_dir, exist_ok=True)
        render_svg(strokes, os.path.join(page_dir, "page.svg"))
        render_thumb(strokes, os.path.join(page_dir, "thumb.png"))
        entry = {
            "id": page_id,
            "strokes": len(strokes),
            "svg": "pages/%s/page.svg" % page_id,
            "thumb": "pages/%s/thumb.png" % page_id,
        }
        if do_ocr:
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
        state["session_entries"][s["id"]] = {
            "urls": s["urls"], "pages": s["pages"], "notebook": s["notebook"],
        }
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

    state["ocr_settings"] = settings
    atomic_write(os.path.join(out_dir, "index.json"),
                 json.dumps({"pages": pages, "sessions": sessions}, indent=2))
    save_state(state)
    print("rendered %d pages (%d unchanged, %d sessions (%d unchanged))"
          % (len(pages), skipped_pages, len(sessions), skipped_sessions))


if __name__ == "__main__":
    main()
