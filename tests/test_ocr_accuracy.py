"""Normalized rendering, pen-order lines, several readings, vocabulary
prior, and the evaluation controls that keep them honest."""
import contextlib
import io
import json
import os
import tempfile
import types
import unittest
from unittest import mock

import convert
import ocr
import ocr_eval
from tests import fixtures


def strokes_of(raw):
    return [[(x, y, 20) for x, y in s] for s in raw]


def sloped_line(x, y, letters, rise, spacing=80):
    """``letters`` glyphs whose baseline climbs ``rise`` units in total."""
    step = rise / max(1, letters - 1)
    return [fixtures.glyph(x + i * spacing, round(y - i * step))
            for i in range(letters)]


# ---------------------------------------------------------------------------
# profiles and overrides
# ---------------------------------------------------------------------------

class ProfileSetTest(unittest.TestCase):
    def test_beam_is_the_previous_default(self):
        beam = ocr.profile("beam")
        self.assertEqual((beam.render, beam.lines, beam.pen_widths,
                          beam.vocab_margin),
                         ("plain", "vertical", (0.09,), 0.0))
        self.assertEqual(ocr.generate_kwargs(beam)["num_beams"], 4)

    def test_default_turns_on_all_four_improvements(self):
        prof = ocr.profile("default")
        self.assertEqual((prof.render, prof.lines), ("normalized", "temporal"))
        self.assertEqual(len(prof.pen_widths), 3)
        self.assertGreater(prof.vocab_margin, 0)

    def test_override_changes_one_setting(self):
        prof = ocr.override(ocr.profile("default"),
                            ["lines=vertical", "pen_widths=0.09",
                             "vocab_margin=0", "max_line_aspect=none"])
        self.assertEqual((prof.lines, prof.pen_widths, prof.vocab_margin,
                          prof.max_line_aspect),
                         ("vertical", (0.09,), 0.0, None))
        self.assertEqual(prof.render, "normalized")
        self.assertTrue(prof.name.startswith("default+lines=vertical"))

    def test_override_rejects_unknown_and_invalid_values(self):
        base = ocr.profile("default")
        for bad in (["colour=red"], ["render=fancy"], ["beams"],
                    ["tesseract_on_ink=maybe"], ["beams=many"]):
            with self.assertRaises(ValueError, msg=bad):
                ocr.override(base, bad)

    def test_any_setting_change_changes_the_reuse_key(self):
        base = ocr.profile("default")
        changed = ocr.override(base, ["vocab_margin=0.2"])
        self.assertNotEqual(ocr.profile_key(base), ocr.profile_key(changed))
        self.assertEqual(ocr.profile_key(base),
                         ocr.profile_key(ocr.profile("default")))


# ---------------------------------------------------------------------------
# option 2: normalized rendering
# ---------------------------------------------------------------------------

@unittest.skipUnless(fixtures.has_cairo(), "pycairo not installed")
class NormalizedRenderTest(unittest.TestCase):
    def render(self, raw, pen_width=0.09):
        import cairo  # pyright: ignore[reportMissingImports]
        line = strokes_of(raw)
        path = os.path.join(tempfile.mkdtemp(dir=fixtures.tests.SANDBOX),
                            "line.png")
        convert.render_line_normalized(line, path, line, pen_width)
        return cairo.ImageSurface.create_from_png(path)

    def test_letters_are_a_fixed_height_in_pixels(self):
        image = self.render(fixtures.text_line(100, 400, 6))
        expected = convert.LETTER_PX * (1 + 2 * convert.PAD_LETTERS)
        self.assertAlmostEqual(image.get_height(), expected, delta=2)

    def test_handwriting_size_does_not_change_the_image(self):
        small = self.render(fixtures.text_line(100, 400, 6))
        big = [[(x * 2, y * 2) for x, y in s]
               for s in fixtures.text_line(100, 400, 6)]
        large = self.render(big)
        self.assertAlmostEqual(small.get_width(), large.get_width(), delta=2)
        self.assertAlmostEqual(small.get_height(), large.get_height(),
                               delta=2)

    def test_ink_is_black(self):
        image = self.render(fixtures.text_line(100, 400, 3))
        self.assertIn(b"\x00\x00\x00", bytes(image.get_data()))

    def test_one_image_per_pen_width_and_all_are_read(self):
        page_dir = tempfile.mkdtemp(dir=fixtures.tests.SANDBOX)
        prof = ocr.profile("default")
        job = convert.LineJob(0, strokes_of(fixtures.text_line(100, 400, 5)),
                              page_dir, prof, float("inf"))
        seen = []
        with mock.patch.object(convert.ocr, "recognize",
                               lambda paths, p: seen.append(paths) or "hi"):
            result = convert.read_line(job)
        self.assertEqual(result.images, ["ocr-line-0-v0.png",
                                         "ocr-line-0-v1.png",
                                         "ocr-line-0-v2.png"])
        self.assertEqual(len(seen[0]), 3)
        self.assertEqual((result.text, result.chunks, result.failed),
                         ("hi", 1, 0))


# ---------------------------------------------------------------------------
# option 3: line breaks from pen order
# ---------------------------------------------------------------------------

class TemporalLinesTest(unittest.TestCase):
    def counts(self, raw, grouper):
        return [len(line) for line in grouper(strokes_of(raw))]

    def test_sloped_lines_are_kept_apart(self):
        raw = (sloped_line(100, 450, 20, rise=270) +
               sloped_line(100, 750, 20, rise=270))
        self.assertEqual(self.counts(raw, convert.group_lines_temporal),
                         [20, 20])
        self.assertNotEqual(self.counts(raw, convert.group_lines), [20, 20])

    def test_a_word_added_to_an_earlier_line_joins_it(self):
        raw = (fixtures.text_line(100, 400, 5) +
               fixtures.text_line(100, 700, 5) +
               fixtures.text_line(900, 400, 3))      # back up to line 1
        self.assertEqual(self.counts(raw, convert.group_lines_temporal),
                         [8, 5])

    def test_dots_go_with_the_letter_written_before_them(self):
        dot = [(150, 300), (152, 302)]
        raw = (fixtures.text_line(100, 400, 3) + [dot] +
               fixtures.text_line(100, 700, 3))
        lines = convert.group_lines_temporal(strokes_of(raw))
        self.assertEqual([len(line) for line in lines], [4, 3])

    def test_straight_lines_match_vertical_grouping(self):
        raw = fixtures.text_line(100, 400, 6) + fixtures.text_line(100, 700, 4)
        self.assertEqual(self.counts(raw, convert.group_lines_temporal),
                         self.counts(raw, convert.group_lines))


# ---------------------------------------------------------------------------
# options 4 and 5: candidates, scores and the vocabulary prior
# ---------------------------------------------------------------------------

class ChooseTest(unittest.TestCase):
    WORDS = frozenset({"brown", "fox", "jumps", "to"})

    def test_highest_score_wins_without_a_prior(self):
        cands = [ocr.Candidate("brawn fox", -0.2),
                 ocr.Candidate("brown fox", -0.3)]
        self.assertEqual(ocr.choose(cands, 0.0, self.WORDS), "brawn fox")

    def test_near_tie_prefers_known_words(self):
        cands = [ocr.Candidate("brawn fox jumps", -0.20),
                 ocr.Candidate("brown fox jumps", -0.28)]
        self.assertEqual(ocr.choose(cands, 0.15, self.WORDS),
                         "brown fox jumps")

    def test_prior_never_overrules_a_clear_winner(self):
        cands = [ocr.Candidate("brawn fox jumps", -0.10),
                 ocr.Candidate("brown fox jumps", -0.90)]
        self.assertEqual(ocr.choose(cands, 0.15, self.WORDS),
                         "brawn fox jumps")

    def test_digits_are_not_unknown_words(self):
        cands = [ocr.Candidate("(5)", -0.20), ocr.Candidate("to", -0.25)]
        self.assertEqual(ocr.choose(cands, 0.15, self.WORDS), "(5)")

    def test_no_candidates_is_empty_text(self):
        self.assertEqual(ocr.choose([], 0.15, self.WORDS), "")


class FakeTrOCR:
    """Stands in for torch + the TrOCR processor and model."""

    def __init__(self, scores, scored_ok=True):
        self.scores = scores           # reading text -> score
        self.scored_ok = scored_ok
        self.calls = []

    def processor(self, images, return_tensors):
        return types.SimpleNamespace(pixel_values=list(images))

    def batch_decode(self, ids, skip_special_tokens):
        return list(ids)

    def generate(self, pixel_values, **kwargs):
        self.calls.append(kwargs)
        n = kwargs.get("num_return_sequences", 1)
        texts = [f"{img} r{k}" for img in pixel_values for k in range(n)]
        if not kwargs.get("return_dict_in_generate"):
            return texts
        if not self.scored_ok:
            raise TypeError("unexpected keyword")
        return types.SimpleNamespace(
            sequences=texts,
            sequences_scores=[self.scores.get(t, -1.0) for t in texts])

    def patch(self, test):
        processor = types.SimpleNamespace(batch_decode=self.batch_decode)
        processor_call = self.processor

        class Processor:
            batch_decode = staticmethod(processor.batch_decode)

            def __call__(self, images, return_tensors):
                return processor_call(images, return_tensors)

        model = types.SimpleNamespace(generate=self.generate)
        torch = types.SimpleNamespace(no_grad=contextlib.nullcontext)
        for target, value in (("_load_trocr", lambda: (torch, Processor(),
                                                       model)),
                              ("_open_rgb", lambda path: path)):
            patcher = mock.patch.object(ocr, target, value)
            patcher.start()
            test.addCleanup(patcher.stop)


class CandidatesTest(unittest.TestCase):
    def test_single_plain_image_is_read_once(self):
        fake = FakeTrOCR({})
        fake.patch(self)
        cands = ocr.trocr_candidates(["a.png"], ocr.profile("beam"))
        self.assertEqual([c.text for c in cands], ["a.png r0"])
        self.assertNotIn("return_dict_in_generate", fake.calls[0])

    def test_most_confident_image_wins(self):
        fake = FakeTrOCR({"b.png r0": -0.1})
        fake.patch(self)
        prof = ocr.override(ocr.profile("default"), ["vocab_margin=0"])
        self.assertEqual(ocr.recognize(["a.png", "b.png", "c.png"], prof),
                         "b.png r0")
        self.assertEqual(fake.calls[0]["num_return_sequences"], 1)

    def test_vocabulary_prior_asks_for_the_n_best(self):
        fake = FakeTrOCR({})
        fake.patch(self)
        with mock.patch.object(ocr, "vocabulary", lambda: frozenset({"x"})):
            ocr.recognize(["a.png"], ocr.profile("default"))
        self.assertEqual(fake.calls[0]["num_return_sequences"],
                         ocr.profile("default").beams)

    def test_unscored_transformers_falls_back_to_one_reading(self):
        fake = FakeTrOCR({}, scored_ok=False)
        fake.patch(self)
        with mock.patch.object(ocr.telemetry, "warn") as warn, \
                mock.patch.object(ocr, "_warned_unscored", []):
            text = ocr.recognize(["a.png", "b.png"], ocr.override(
                ocr.profile("default"), ["vocab_margin=0"]))
            ocr.recognize(["a.png", "b.png"], ocr.override(
                ocr.profile("default"), ["vocab_margin=0"]))
        self.assertEqual(text, "a.png r0")
        self.assertEqual(warn.call_count, 1)


class VocabularyTest(unittest.TestCase):
    def tearDown(self):
        ocr.vocabulary.cache_clear()

    def test_system_list_and_private_words_are_merged(self):
        folder = tempfile.mkdtemp(dir=fixtures.tests.SANDBOX)
        system = os.path.join(folder, "words")
        private = os.path.join(folder, "vocabulary.txt")
        with open(system, "w") as f:
            f.write("Apple\nbanana's\n")
        with open(private, "w") as f:
            f.write("CER\n")
        with mock.patch.object(ocr, "WORD_LISTS", (system,)), \
                mock.patch.object(ocr, "USER_VOCABULARY", private):
            ocr.vocabulary.cache_clear()
            self.assertEqual(ocr.vocabulary(),
                             frozenset({"apple", "banana's", "cer"}))

    def test_no_word_list_means_no_prior(self):
        with mock.patch.object(ocr, "WORD_LISTS", ("/nonexistent",)), \
                mock.patch.object(ocr, "USER_VOCABULARY", "/nonexistent"):
            ocr.vocabulary.cache_clear()
            self.assertEqual(ocr.vocabulary(), frozenset())


# ---------------------------------------------------------------------------
# evaluation controls
# ---------------------------------------------------------------------------

class SplitAndIntervalTest(unittest.TestCase):
    def test_split_defaults_to_dev(self):
        samples = [{"truth": "a"}, {"truth": "b", "split": "test"}]
        self.assertEqual(ocr_eval.in_split(samples, "dev"), [samples[0]])
        self.assertEqual(ocr_eval.in_split(samples, "test"), [samples[1]])
        self.assertEqual(len(ocr_eval.in_split(samples, "all")), 2)

    def test_unknown_split_is_rejected(self):
        path = os.path.join(tempfile.mkdtemp(dir=fixtures.tests.SANDBOX),
                            "m.json")
        with open(path, "w") as f:
            json.dump({"samples": [{"page": "1", "truth": "x",
                                    "split": "train"}]}, f)
        with self.assertRaises(ValueError):
            ocr_eval.load_manifest(path)

    def test_interval_is_repeatable_and_brackets_the_mean(self):
        values = [0.1, 0.2, 0.4, 0.05, 0.3, 0.25]
        low, high = ocr_eval.interval(values)
        self.assertEqual((low, high), ocr_eval.interval(values))
        self.assertLessEqual(low, ocr_eval.mean(values))
        self.assertGreaterEqual(high, ocr_eval.mean(values))

    def test_one_sample_has_no_spread(self):
        self.assertEqual(ocr_eval.interval([0.3]), (0.3, 0.3))


@unittest.skipUnless(fixtures.has_cairo(), "pycairo not installed")
class EvalSetAndSplitTest(unittest.TestCase):
    def setUp(self):
        fixtures.reset_sandbox()
        fixtures.write_page(fixtures.tests.SANDBOX, "1.2.3.4",
                            fixtures.text_line(500, 600, 4))
        self.manifest = os.path.join(fixtures.tests.SANDBOX, "eval.json")
        with open(self.manifest, "w") as f:
            json.dump({"samples": [
                {"id": "a", "page": "1.2.3.4", "truth": "hello"},
                {"id": "b", "page": "1.2.3.4", "truth": "hello",
                 "split": "test"}]}, f)
        self.profiles = []
        for target, value in (
                ("trocr_available", lambda: True),
                ("recognize", lambda paths, prof:
                 self.profiles.append(prof) or "hello")):
            patcher = mock.patch.object(ocr, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_eval(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ocr_eval.main(["--manifest", self.manifest, "--json"] +
                                 list(args))
        return code, json.loads(out.getvalue())

    def test_dev_is_scored_by_default(self):
        code, result = self.run_eval()
        self.assertEqual((code, result["split"]), (0, "dev"))
        self.assertEqual([s["sample"] for s in result["samples"]], ["a"])
        self.assertEqual(result["cer_95"], [0.0, 0.0])

    def test_set_changes_the_profile_that_is_evaluated(self):
        _, result = self.run_eval("--set", "lines=vertical", "--split", "test")
        self.assertEqual(result["profile"], "default+lines=vertical")
        self.assertEqual(self.profiles[-1].lines, "vertical")

    def test_bad_set_is_a_usage_error(self):
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            code = ocr_eval.main(["--manifest", self.manifest,
                                  "--set", "colour=red"])
        self.assertEqual(code, 2)
        self.assertIn("unknown profile setting", out.getvalue())


if __name__ == "__main__":
    unittest.main()
