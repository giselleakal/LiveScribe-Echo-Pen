"""Incremental-state, lock and OCR/STT bookkeeping of convert.main()."""
import contextlib
import io
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

import convert
import tests
from tests import fixtures

PAGE_A = "2208.1.12.25"
PAGE_B = "2208.1.12.26"


def run_main(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        convert.main(list(argv))
    return out.getvalue()


def index():
    with open(os.path.join(tests.SANDBOX, "output", "index.json")) as f:
        return {p["id"]: p for p in json.load(f)["pages"]}


def rewrite_page(page_id, letters):
    path = fixtures.write_page(tests.SANDBOX, page_id,
                               fixtures.text_line(500, 600, letters))
    later = time.time() + 5
    os.utime(path, (later, later))


class LoadStateTest(unittest.TestCase):
    def setUp(self):
        fixtures.reset_sandbox()

    def test_missing_keys_get_defaults(self):
        with open(convert.STATE_PATH, "w") as f:
            json.dump({"render_version": 3}, f)
        state = convert.load_state()
        for key in ("sources", "page_entries", "session_entries"):
            self.assertEqual(state[key], {})
        self.assertEqual(state["render_version"], 3)

    def test_non_object_state_is_ignored(self):
        with open(convert.STATE_PATH, "w") as f:
            f.write("[]")
        self.assertEqual(convert.load_state()["sources"], {})


class LockTest(unittest.TestCase):
    def setUp(self):
        fixtures.reset_sandbox()

    def tearDown(self):
        convert.release_lock()

    def test_lock_left_by_a_dead_process_is_reclaimed(self):
        with open(convert.LOCK_PATH, "w") as f:
            f.write("999999")
        self.assertTrue(convert.acquire_lock(wait=0.2))

    def test_lock_held_by_another_process_blocks(self):
        holder = subprocess.Popen([sys.executable, "-c", (
            "import fcntl, os, sys, time\n"
            "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "time.sleep(30)\n"), convert.LOCK_PATH],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        assert holder.stdout is not None
        self.assertEqual(holder.stdout.readline().strip(), "held")
        self.assertTrue(convert.lock_is_held())
        with self.assertRaises(RuntimeError):
            convert.acquire_lock(wait=0.2)

    def test_lock_is_free_again_after_release(self):
        self.assertTrue(convert.acquire_lock(wait=0.2))
        convert.release_lock()
        self.assertFalse(convert.lock_is_held())

    def test_main_releases_the_lock(self):
        run_main()
        self.assertFalse(convert.lock_is_held())


@unittest.skipUnless(fixtures.has_cairo(), "pycairo not installed")
class OcrBookkeepingTest(unittest.TestCase):
    def setUp(self):
        fixtures.reset_sandbox()
        fixtures.write_page(tests.SANDBOX, PAGE_A,
                            fixtures.text_line(500, 600, 6))
        fixtures.write_page(tests.SANDBOX, PAGE_B,
                            fixtures.text_line(500, 600, 3))
        self.calls = 0
        self.fail_ocr = False
        self.fail_after = None
        self.interrupt_on = None
        self.recognized = "hello"
        patches = [
            mock.patch.object(convert.ocr, "trocr_available", lambda: True),
            mock.patch.object(convert.ocr, "trocr_image_to_text",
                              self.fake_recognize),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def fake_recognize(self, png_path, prof=None):
        self.calls += 1
        if self.interrupt_on and self.interrupt_on in png_path:
            raise KeyboardInterrupt
        if self.fail_ocr:
            raise RuntimeError("model unavailable")
        if self.fail_after is not None and self.calls > self.fail_after:
            raise RuntimeError("out of memory")
        return self.recognized

    def test_unchanged_pages_are_not_re_ocred(self):
        run_main("--ocr")
        first = self.calls
        run_main("--ocr")
        self.assertEqual(self.calls, first)

    def test_plain_convert_does_not_invalidate_ocr(self):
        run_main("--ocr")
        first = self.calls
        run_main()
        run_main("--ocr")
        self.assertEqual(self.calls, first)
        self.assertEqual(index()[PAGE_A]["text"], "hello")

    def test_renderer_upgrade_keeps_transcriptions(self):
        run_main("--ocr")
        state = convert.load_state()
        state["render_version"] = convert.RENDER_VERSION - 1
        convert.save_state(state)
        run_main()
        self.assertEqual(index()[PAGE_A].get("text"), "hello")
        self.assertTrue(os.path.exists(os.path.join(
            tests.SANDBOX, "output", "pages", PAGE_A, "text.txt")))

    def test_changed_ink_drops_stale_transcription(self):
        run_main("--ocr")
        rewrite_page(PAGE_A, 2)
        run_main()
        self.assertNotIn("text", index()[PAGE_A])

    def test_failed_re_ocr_keeps_the_previous_text(self):
        run_main("--ocr")
        self.fail_ocr = True
        run_main("--ocr", "--ocr-lang", "deu")
        self.assertEqual(index()[PAGE_A].get("text"), "hello")

    def test_force_keeps_transcriptions_of_unchanged_ink(self):
        run_main("--ocr")
        run_main("--force")
        self.assertEqual(index()[PAGE_A].get("text"), "hello")

    def test_blank_page_is_not_re_ocred_every_run(self):
        self.recognized = ""
        run_main("--ocr")
        first = self.calls
        run_main("--ocr")
        self.assertEqual(self.calls, first)

    def test_partial_line_failure_keeps_text_but_allows_retry(self):
        fixtures.write_page(tests.SANDBOX, PAGE_A,
                            fixtures.text_line(500, 600, 4) +
                            fixtures.text_line(500, 1200, 4))
        self.fail_after = 1
        run_main("--ocr")
        entry = index()[PAGE_A]
        self.assertEqual(entry.get("text"), "hello")
        self.assertFalse(entry["ocr"])

    def test_interrupted_run_keeps_finished_pages(self):
        self.interrupt_on = PAGE_B
        with self.assertRaises(KeyboardInterrupt):
            run_main("--ocr")
        [failed] = [r for r in convert.telemetry.read_records()
                    if r.get("event") == "convert.failed"]
        self.assertEqual((failed["level"], failed["interrupted"]),
                         ("WARNING", True))
        self.interrupt_on = None
        self.calls = 0
        run_main("--ocr")
        self.assertEqual(self.calls, 1)

    def test_single_page_mode_does_not_hide_other_changes(self):
        run_main()
        rewrite_page(PAGE_B, 7)
        run_main("--ocr", "--page", PAGE_A)
        run_main()
        self.assertEqual(index()[PAGE_B]["strokes"], 7)


@unittest.skipUnless(fixtures.has_cairo(), "pycairo not installed")
class CorruptSourceTest(unittest.TestCase):
    def setUp(self):
        fixtures.reset_sandbox()
        fixtures.write_page(tests.SANDBOX, PAGE_A,
                            fixtures.text_line(500, 600, 3))

    def test_corrupt_page_does_not_stop_the_others(self):
        fixtures.write_page(tests.SANDBOX, PAGE_B, [], raw=b"garbage")
        run_main()
        self.assertIn(PAGE_A, index())
        failed = [r for r in convert.telemetry.read_records()
                  if r.get("event") == "page.failed"]
        self.assertEqual([r["page"] for r in failed], [PAGE_B])

    def test_corrupted_page_keeps_its_last_good_render(self):
        fixtures.write_page(tests.SANDBOX, PAGE_B,
                            fixtures.text_line(500, 600, 2))
        run_main()
        path = fixtures.write_page(tests.SANDBOX, PAGE_B, [], raw=b"\x01")
        later = time.time() + 5
        os.utime(path, (later, later))
        run_main()
        self.assertEqual(index()[PAGE_B]["strokes"], 2)
        self.assertTrue(os.path.exists(os.path.join(
            tests.SANDBOX, "output", "pages", PAGE_B, "page.svg")))


@unittest.skipUnless(fixtures.has_cairo(), "pycairo not installed")
class SttBookkeepingTest(unittest.TestCase):
    def setUp(self):
        fixtures.reset_sandbox()
        fixtures.write_page(tests.SANDBOX, PAGE_A,
                            fixtures.text_line(500, 600, 2))
        fixtures.write_session(tests.SANDBOX, "PRS-1a", [])
        fixtures.write_session(tests.SANDBOX, "PRS-2b", [])
        patches = [
            mock.patch.object(convert.stt, "available", lambda: True),
            mock.patch.object(convert.stt, "transcribe", lambda audio: "hi"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_audio_decode_failure_still_writes_the_index(self):
        with mock.patch.object(convert.stt, "seq_to_floats",
                               side_effect=RuntimeError("ffmpeg missing")):
            run_main("--stt")
        self.assertIn(PAGE_A, index())

    def test_force_keeps_transcripts(self):
        with mock.patch.object(convert.stt, "seq_to_floats",
                               return_value=[0.0] * 16000):
            run_main("--stt")
        run_main("--force")
        with open(os.path.join(tests.SANDBOX, "output", "index.json")) as f:
            sessions = json.load(f)["sessions"]
        self.assertEqual({s["id"]: s.get("transcript") for s in sessions},
                         {"PRS-1a": "hi", "PRS-2b": "hi"})

    def test_removed_audio_part_is_dropped_from_the_session(self):
        sess = fixtures.write_session(tests.SANDBOX, "PRS-1a", [])
        with open(os.path.join(sess, "audio-1.aac"), "wb") as f:
            f.write(b"\x01" * 32)
        run_main()
        os.remove(os.path.join(sess, "audio-1.aac"))
        run_main()
        with open(os.path.join(tests.SANDBOX, "output", "index.json")) as f:
            urls = {s["id"]: s["urls"] for s in json.load(f)["sessions"]}
        self.assertEqual(urls["PRS-1a"], ["audio/PRS-1a.0.aac"])

    def test_single_session_progress_counts_one(self):
        with mock.patch.object(convert.stt, "seq_to_floats",
                               return_value=[0.0] * 16000):
            out = run_main("--stt", "--session", "PRS-1a")
        self.assertIn("transcribed (1/1)", out)


def tearDownModule():
    fixtures.reset_sandbox()


if __name__ == "__main__":
    unittest.main()
