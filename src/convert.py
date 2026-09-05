#!/usr/bin/env python3
"""Convert raw dumpscribe output into web-viewable page images.

Reads:  data/                     -- directory produced by dumpscribe
Writes: output/index.json
        output/pages/<page_id>/page.svg   (vector, full fidelity)
        output/pages/<page_id>/thumb.png  (raster thumbnail)
        output/audio/<session_id>.aac     (copied audio)

Usage:  ./src/convert.py
"""
import glob
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "vendor"))
import parsestf

PAGE_W, PAGE_H = 4963, 6278  # STF "dot" units; page size used by stf2pdf
GRID = 83                     # visible dot-grid spacing in STF units
INK = (12, 22, 90)            # Livescribe ink blue


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
    with open(path, "w") as f:
        f.write('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d"' % (PAGE_W, PAGE_H))
        f.write(' width="100%" height="100%">\n')
        f.write('<rect width="%d" height="%d" fill="#ffffff"/>\n' % (PAGE_W, PAGE_H))
        f.write('<g fill="#c9d4e8">\n')
        for gx, gy, r in dot_grid():
            f.write('<circle cx="%d" cy="%d" r="%.2f"/>\n' % (gx, gy, r))
        f.write('</g>\n')
        f.write('<g stroke="#%02x%02x%02x" fill="none" stroke-linecap="round" stroke-linejoin="round">\n'
                % INK)
        for stroke in strokes:
            avg_f = sum(p[2] for p in stroke) / len(stroke)
            width = max(1.5, (avg_f ** 0.3) * 3)
            d = "M" + " L".join("%.1f %.1f" % (x, y) for x, y, _ in stroke)
            f.write('<path d="%s" stroke-width="%.2f"/>\n' % (d, width))
        f.write('</g>\n</svg>\n')


def render_thumb(strokes, png_path, size=320):
    import cairo
    scale = size / float(PAGE_W)
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, size, int(PAGE_H * scale))
    ctx = cairo.Context(surface)
    ctx.set_source_rgb(1, 1, 1)
    ctx.paint()
    ctx.scale(scale, scale)
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
    surface.write_to_png(png_path)


def parse_session_pages(path):
    """Decode the binary session.pages (CA BE header, packed page addresses).

    Layout (big-endian): magic(2) version(1) reserved(1) num_pages(2),
    then per page: page-address(8) + pen-time(8).
    """
    raw = open(path, "rb").read()
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


def collect_sessions(data_dir):
    sessions = []
    replay_root = os.path.join(data_dir, "userdata")
    for pen in glob.glob(os.path.join(replay_root, "*", "Paper Replay", "*", "*")):
        notebook = os.path.basename(pen)
        for sess in glob.glob(os.path.join(pen, "sessions", "*")):
            sid = os.path.basename(sess)
            aac_parts = sorted(glob.glob(os.path.join(sess, "audio-*.aac")))
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


def main():
    data_dir = os.path.join(ROOT, "data")
    out_dir = os.path.join(ROOT, "output")
    pages_out = os.path.join(out_dir, "pages")
    audio_out = os.path.join(out_dir, "audio")
    os.makedirs(pages_out, exist_ok=True)
    os.makedirs(audio_out, exist_ok=True)

    pages = []
    for stf in sorted(glob.glob(os.path.join(data_dir, "data", "*", "*", "*.stf"))):
        page_id = os.path.basename(os.path.dirname(os.path.dirname(stf)))
        strokes = load_strokes(stf)
        page_dir = os.path.join(pages_out, page_id)
        os.makedirs(page_dir, exist_ok=True)
        render_svg(strokes, os.path.join(page_dir, "page.svg"))
        render_thumb(strokes, os.path.join(page_dir, "thumb.png"))
        pages.append({
            "id": page_id,
            "strokes": len(strokes),
            "svg": "pages/%s/page.svg" % page_id,
            "thumb": "pages/%s/thumb.png" % page_id,
        })

    sessions = collect_sessions(data_dir)
    for s in sessions:
        urls = []
        for i, aac in enumerate(s["audio"]):
            src = os.path.join(data_dir, aac)
            dst = os.path.join(audio_out, s["id"] + ".%d.aac" % i)
            try:
                shutil.copyfile(src, dst)
                urls.append("audio/" + os.path.basename(dst))
            except OSError as e:
                print("audio copy failed for %s: %s" % (s["id"], e), file=sys.stderr)
        s["urls"] = urls

    with open(os.path.join(out_dir, "index.json"), "w") as f:
        json.dump({"pages": pages, "sessions": sessions}, f, indent=2)
    print("rendered %d pages, found %d audio sessions" % (len(pages), len(sessions)))


if __name__ == "__main__":
    main()