"""Synthetic pen data for tests: .stf pages and Paper Replay sessions.

The STF writer emits only the subset of the format the parser needs:
one absolute-coordinate point per sample, constant force, fixed timing.
"""
import os
import shutil
import subprocess
import sys

import tests
from tests import REPO

sys.path.insert(0, os.path.join(REPO, "vendor"))
import parsestf  # noqa: E402

_FORCE_TABLE = [
    [1, [0]],
    [6, [1, 4, 7, 9, 10, 11, 13, 15, 17, 20, 21, 22, 23, 24, 25, 26, 27, 28,
         30]],
    [7, [2, 3, 5, 6, 8, 12, 14, 16, 18, 19, 29, 31, 32, 33, 34, 35, 36, 49,
         52]],
]
_HEADER_TABLE = [[1, [0]], [2, [1, 2]]]
_HEADER2_TABLE = [[1, [0]], [2, [1, 3]]]
_TIME_TABLE = [[1, [1]], [2, [2]], [4, [0, 3, 4]]]
_DFORCE_TABLE = [[1, [0]]]


def _code(table, value):
    """Bits (as a string) that ``BitReader.decode`` maps to ``value``."""
    codeacc = 0
    have = 0
    for length, values in table:
        codeacc = (codeacc << (length - have)) + len(values)
        have = length
        if value in values:
            stream = codeacc - len(values) + values.index(value)
            return format(stream, "0%db" % length)
    raise ValueError("value %r not encodable" % (value,))


class _Bits:
    def __init__(self):
        self.bits = []

    def put(self, value, n):
        if not 0 <= value < (1 << n):
            raise ValueError("%d does not fit in %d bits" % (value, n))
        self.bits.append(format(value, "0%db" % n))

    def code(self, table, value):
        self.bits.append(_code(table, value))

    def to_bytes(self):
        joined = "".join(self.bits)
        joined += "0" * (-len(joined) % 8)
        return bytes(int(joined[i:i + 8], 2) for i in range(0, len(joined), 8))


def stf_bytes(strokes, force=20, start_ms=1000, long_times=False):
    """Encode ``strokes`` (lists of (x, y) points) as an Anoto STF file.

    ``long_times`` stores each sample's time in the 32-bit field instead
    of the short Huffman code, as the pen does after a long pause.
    """
    out = bytearray(b"\x01\x00" + b"Anoto STF v1.0" + b"\x00\x64")
    for i, stroke in enumerate(strokes):
        bits = _Bits()
        bits.put(0x10, 8)                       # stroke header: 32-bit time
        bits.put(start_ms if i == 0 else 10, 32)
        x0, y0 = stroke[0]
        bits.put(x0, 16)
        bits.put(y0, 16)
        bits.code(_FORCE_TABLE, force)
        for x, y in stroke[1:]:
            if long_times:
                bits.code(_HEADER_TABLE, 2)
                bits.code(_HEADER2_TABLE, 3)     # 32-bit sample time
                bits.put(1, 32)
            else:
                bits.code(_HEADER_TABLE, 1)      # timed sample
                bits.code(_TIME_TABLE, 1)
            bits.put(1, 1)                       # absolute coordinates
            bits.put(x, 16)
            bits.put(y, 16)
            bits.code(_DFORCE_TABLE, 0)
        bits.code(_HEADER_TABLE, 0)
        bits.code(_TIME_TABLE, 0)                # time 0 ends the stroke
        out += bits.to_bytes()
    out += b"\x80"
    return bytes(out)


def glyph(x, y, w=60, h=100):
    """A small zig-zag stroke shaped like one handwritten letter."""
    return [(x, y), (x + w // 2, y + h), (x + w, y), (x + w, y + h // 2)]


def text_line(x, y, letters, spacing=80):
    """Strokes for ``letters`` glyphs written left to right at (x, y)."""
    return [glyph(x + i * spacing, y) for i in range(letters)]


def write_page(root, page_id, strokes, raw=None):
    """Store a synthetic page where dumpscribe would put it.

    ``raw`` replaces the encoded strokes with arbitrary bytes, to model a
    corrupt or truncated download.
    """
    page_dir = os.path.join(root, "data", "data", page_id, "0")
    os.makedirs(page_dir, exist_ok=True)
    path = os.path.join(page_dir, "page.stf")
    with open(path, "wb") as f:
        f.write(stf_bytes(strokes) if raw is None else raw)
    return path


def write_session(root, session_id, page_addresses, audio=b"\x00" * 64,
                  notebook="0123456789abcdef"):
    """Store a synthetic Paper Replay session (audio + session.pages)."""
    sess = os.path.join(root, "data", "userdata", "pen", "Paper Replay",
                        "251", notebook, "sessions", session_id)
    os.makedirs(sess, exist_ok=True)
    with open(os.path.join(sess, "audio-0.aac"), "wb") as f:
        f.write(audio)
    raw = bytearray(b"\xca\xbe\x01\x00")
    raw += len(page_addresses).to_bytes(2, "big")
    for addr in page_addresses:
        raw += addr.to_bytes(8, "big") + (0).to_bytes(8, "big")
    with open(os.path.join(sess, "session.pages"), "wb") as f:
        f.write(bytes(raw))
    return sess


def page_address(segment, shelf, book, page, section=0):
    """Pack a page id the way session.pages stores it."""
    return ((section << 52) | (segment << 40) | (shelf << 24) |
            (book << 12) | page)


def parse_points(data):
    """Decode STF bytes back to [(x, y, force)] strokes with parsestf."""
    import io

    class _Collector(parsestf.STFParser):
        def __init__(self, stream):
            super().__init__(stream)
            self.strokes, self.cur = [], []

        def handle_point(self, x, y, force, time):
            self.cur.append((x, y, force))

        def handle_stroke_end(self, time):
            self.strokes.append(self.cur)
            self.cur = []

    c = _Collector(io.BytesIO(data))
    c.parse()
    return c.strokes


def reset_sandbox():
    """Empty the shared test ECHO_ROOT so tests don't see each other's output."""
    for name in os.listdir(tests.SANDBOX):
        path = os.path.join(tests.SANDBOX, name)
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)


def fresh_root(base, name):
    """An empty ECHO_ROOT directory under ``base``."""
    root = os.path.join(base, name)
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root)
    return root


def run_convert(root, *args, env=None):
    """Run ``src/convert.py`` against ``root``; returns CompletedProcess."""
    full_env = dict(os.environ)
    full_env["ECHO_ROOT"] = root
    full_env.pop("ECHO_LOCK_HELD", None)
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, os.path.join(REPO, "src", "convert.py")] + list(args),
        env=full_env, capture_output=True, text=True, timeout=120)


def has_cairo():
    try:
        import cairo  # noqa: F401
    except ImportError:
        return False
    return True
