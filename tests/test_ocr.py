"""OCR profiles, line segmentation, and the evaluation harness."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import convert
import ocr
import ocr_eval

from tests import fixtures


def strokes_of(raw):
    return [[(x, y, 20) for x, y in s] for s in raw]


class ProfileTest(unittest.TestCase):
    def test_default_decodes_with_beams_and_blocks_repeats(self):
        kwargs = ocr.generate_kwargs(ocr.profile("default"))
        self.assertEqual(kwargs["num_beams"], 4)
        self.assertEqual(kwargs["no_repeat_ngram_size"], 3)

    def test_legacy_is_plain_greedy_decoding(self):
        self.assertEqual(ocr.generate_kwargs(ocr.profile("legacy")),
                         {"max_new_tokens": 64})

    def test_profile_comes_from_the_environment(self):
        with mock.patch.dict(os.environ, {"ECHO_OCR_PROFILE": "legacy"}):
            self.assertEqual(ocr.profile().name, "legacy")
        with mock.patch.dict(os.environ, {"ECHO_OCR_PROFILE": "bogus"}):
            self.assertEqual(ocr.profile().name, "default")

    def test_profile_is_part_of_the_reuse_key(self):
        a = convert.OcrJob("trocr", "eng", 6, "default").settings()
        b = convert.OcrJob("trocr", "eng", 6, "legacy").settings()
        self.assertNotEqual(a, b)


class OversizeStrokeTest(unittest.TestCase):
    def setUp(self):
        top = fixtures.text_line(100, 400, 5)
        bottom = fixtures.text_line(100, 700, 5)
        # a circle-like stroke straddling the gap between the two lines
        ring = [(700, 330), (820, 330), (820, 870), (700, 870), (700, 330)]
        self.strokes = strokes_of(top + bottom + [ring])

    def test_default_does_not_invent_a_line_for_a_tall_stroke(self):
        lines = convert.group_lines(self.strokes, oversize_ratio=2.5)
        self.assertEqual(len(lines), 2)

    def test_legacy_grouping_is_unchanged(self):
        self.assertEqual(len(convert.group_lines(self.strokes)), 3)


class SplitWideLineTest(unittest.TestCase):
    def test_long_line_is_cut_at_the_word_gaps(self):
        words = []
        for w in range(6):
            words += fixtures.text_line(100 + w * 900, 400, 5)
        line = strokes_of(words)
        width = 5 * 900 + 4 * 80 + 60          # six 5-glyph words
        expected = -(-width // (100 * 14))     # ceil(aspect / 14)
        chunks = convert.split_wide_line(line, max_aspect=14.0)
        self.assertEqual(len(chunks), expected)
        self.assertEqual([len(c) % 5 for c in chunks], [0] * expected)
        self.assertEqual(sum(len(c) for c in chunks), len(line))

    def test_short_line_is_kept_whole(self):
        line = strokes_of(fixtures.text_line(100, 400, 5))
        self.assertEqual(convert.split_wide_line(line, 14.0), [line])

    def test_line_without_gaps_is_not_cut(self):
        line = strokes_of([[(x, 400), (x + 120, 500)]
                           for x in range(0, 6000, 100)])
        self.assertEqual(len(convert.split_wide_line(line, 14.0)), 1)

    def test_legacy_never_splits(self):
        line = strokes_of(fixtures.text_line(100, 400, 200))
        self.assertEqual(convert.split_wide_line(line, None), [line])


class ScoringTest(unittest.TestCase):
    def test_perfect_and_empty(self):
        self.assertEqual(ocr_eval.score_text("Hello  World", "hello world"),
                         (0.0, 0.0))
        self.assertEqual(ocr_eval.score_text("abc", ""), (1.0, 1.0))

    def test_one_substitution(self):
        cer, wer = ocr_eval.score_text("phones are", "hones are")
        self.assertAlmostEqual(cer, 1 / 10)
        self.assertAlmostEqual(wer, 1 / 2)

    def test_manifest_validation(self):
        path = os.path.join(tempfile.mkdtemp(dir=fixtures.tests.SANDBOX),
                            "m.json")
        with open(path, "w") as f:
            json.dump({"samples": [{"truth": "x"}]}, f)
        with self.assertRaises(ValueError):
            ocr_eval.load_manifest(path)


@unittest.skipUnless(fixtures.has_cairo(), "pycairo not installed")
class EvalHarnessTest(unittest.TestCase):
    def setUp(self):
        fixtures.reset_sandbox()
        fixtures.write_page(fixtures.tests.SANDBOX, "1.2.3.4",
                            fixtures.text_line(500, 600, 4) +
                            fixtures.text_line(500, 1200, 4))
        self.manifest = os.path.join(fixtures.tests.SANDBOX, "eval.json")
        with open(self.manifest, "w") as f:
            json.dump({"samples": [
                {"id": "two-lines", "page": "1.2.3.4",
                 "truth": "hello\nhello"},
                {"id": "missing", "page": "9.9.9.9", "truth": "x"}]}, f)
        patches = [
            mock.patch.object(ocr, "trocr_available", lambda: True),
            mock.patch.object(ocr, "trocr_image_to_text",
                              lambda png, prof=None: "hello"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_eval(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ocr_eval.main(["--manifest", self.manifest, "--json"] +
                                 list(args))
        return code, json.loads(out.getvalue())

    def test_scores_pages_and_reports_broken_samples(self):
        code, result = self.run_eval()
        by_id = {s["sample"]: s for s in result["samples"]}
        self.assertEqual((by_id["two-lines"]["cer"],
                          by_id["two-lines"]["found_lines"]), (0.0, 2))
        self.assertIn("FileNotFoundError", by_id["missing"]["error"])
        self.assertEqual(code, 1)

    def test_eval_never_writes_to_output(self):
        self.run_eval("--profile", "legacy")
        self.assertFalse(os.path.exists(os.path.join(fixtures.tests.SANDBOX,
                                                     "output")))


if __name__ == "__main__":
    unittest.main()
