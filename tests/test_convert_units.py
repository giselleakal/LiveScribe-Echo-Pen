import datetime
import os
import tempfile
import unittest

import tests
from tests import fixtures

import convert


class StfFixtureTest(unittest.TestCase):
    def test_round_trip_through_vendored_parser(self):
        strokes = [[(100, 200), (150, 300), (200, 200)], [(400, 500), (420, 520)]]
        parsed = fixtures.parse_points(fixtures.stf_bytes(strokes))
        self.assertEqual([[(x, y) for x, y, _ in s] for s in parsed], strokes)


class GroupLinesTest(unittest.TestCase):
    def test_two_lines_are_split_and_ordered_top_to_bottom(self):
        bottom = fixtures.text_line(100, 900, 5)
        top = fixtures.text_line(100, 400, 4)
        lines = convert.group_lines([[(x, y, 20) for x, y in s]
                                     for s in bottom + top])
        self.assertEqual([len(line) for line in lines], [4, 5])

    def test_strokes_in_a_line_are_sorted_left_to_right(self):
        line = fixtures.text_line(100, 400, 4)
        shuffled = [line[2], line[0], line[3], line[1]]
        [got] = convert.group_lines([[(x, y, 20) for x, y in s]
                                     for s in shuffled])
        self.assertEqual([s[0][0] for s in got], [100, 180, 260, 340])

    def test_empty_page_has_no_lines(self):
        self.assertEqual(convert.group_lines([]), [])


class CleanOcrTextTest(unittest.TestCase):
    def test_collapses_spaces_and_drops_blank_lines(self):
        self.assertEqual(convert.clean_ocr_text("a  b \n\n  c ,d\t\te ."),
                         "a b\nc,d e.")


class PageAddressTest(unittest.TestCase):
    def test_four_part_id(self):
        raw = fixtures.page_address(2208, 1, 12, 25).to_bytes(8, "big")
        self.assertEqual(convert.decode_page_address(raw), "2208.1.12.25")

    def test_section_prefix_gives_five_parts(self):
        raw = fixtures.page_address(2208, 1, 12, 25, section=3).to_bytes(8, "big")
        self.assertEqual(convert.decode_page_address(raw), "3.2208.1.12.25")

    def test_session_pages_file(self):
        root = tempfile.mkdtemp(dir=tests.SANDBOX)
        sess = fixtures.write_session(
            root, "PRS-1", [fixtures.page_address(2208, 1, 12, 25),
                            fixtures.page_address(2208, 1, 12, 26)])
        self.assertEqual(
            convert.parse_session_pages(os.path.join(sess, "session.pages")),
            ["2208.1.12.25", "2208.1.12.26"])

    def test_session_pages_rejects_bad_magic(self):
        path = os.path.join(tempfile.mkdtemp(dir=tests.SANDBOX), "session.pages")
        with open(path, "wb") as f:
            f.write(b"\x00\x00\x01\x00\x00\x01" + b"\x00" * 16)
        self.assertEqual(convert.parse_session_pages(path), [])


class SessionInfoTest(unittest.TestCase):
    def _write(self, start, end):
        path = os.path.join(tempfile.mkdtemp(dir=tests.SANDBOX), "session.info")
        raw = bytearray(32)
        raw[0:2] = b"\xfa\xce"
        raw[11:16] = start.to_bytes(5, "big")
        raw[19:24] = end.to_bytes(5, "big")
        with open(path, "wb") as f:
            f.write(bytes(raw))
        return path

    def test_start_and_end_are_decoded(self):
        path = self._write(0x5197A597, 0x5197A597 + 60000)
        self.assertEqual(convert.parse_session_info(path, "PRS-5197a597"),
                         (0x5197A597, 0x5197A597 + 60000))

    def test_start_must_match_the_session_id(self):
        path = self._write(0x5197A597, 0x5197A597 + 60000)
        self.assertIsNone(convert.parse_session_info(path, "PRS-1"))


class InkBoundsTest(unittest.TestCase):
    def test_single_dot_gets_the_minimum_canvas_centred_on_it(self):
        minx, miny, maxx, maxy = convert.ink_bounds([[(1000, 2000, 5)]])
        self.assertAlmostEqual(maxx - minx, convert.MIN_CANVAS)
        self.assertAlmostEqual((minx + maxx) / 2, 1000)
        self.assertAlmostEqual((miny + maxy) / 2, 2000)

    def test_empty_page_falls_back_to_full_page(self):
        self.assertEqual(convert.ink_bounds([]),
                         (0, 0, convert.PAGE_W, convert.PAGE_H))


class MiscTest(unittest.TestCase):
    def test_audio_parts_sort_naturally(self):
        names = ["audio-10.aac", "audio-2.aac", "audio-0.aac"]
        self.assertEqual(sorted(names, key=convert.audio_part_key),
                         ["audio-0.aac", "audio-2.aac", "audio-10.aac"])

    def test_pen_time_to_iso_uses_the_sync_offset(self):
        iso = convert.pen_ms_to_iso(1000, 1_700_000_000_000 - 1000)
        parsed = datetime.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S%z")
        self.assertEqual(parsed.timestamp(), 1_700_000_000)


if __name__ == "__main__":
    unittest.main()
