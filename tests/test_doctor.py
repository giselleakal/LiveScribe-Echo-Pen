"""./run doctor checks and the debug report's no-note-content promise."""
import json
import os
import unittest
from unittest import mock

import convert
import doctor
import telemetry
from tests import fixtures

NOTE_TEXT = "synthetic secret note sentence"


class DoctorTest(unittest.TestCase):
    def setUp(self):
        fixtures.reset_sandbox()

    def by_name(self):
        return {r.name: r for r in doctor.run_checks()}

    def test_every_check_runs_and_problems_carry_a_fix(self):
        results = self.by_name()
        self.assertEqual(len(results), len(doctor.CHECKS))
        for r in results.values():
            if r.status != doctor.OK:
                self.assertTrue(r.fix or r.name == "pen", r)

    def test_empty_library_points_at_sync(self):
        result = self.by_name()["library"]
        self.assertEqual((result.status, result.fix), (doctor.WARN,
                                                       "./run sync"))

    def test_held_lock_is_reported_and_free_lock_is_not(self):
        self.assertEqual(self.by_name()["lock"].status, doctor.OK)
        self.assertTrue(convert.acquire_lock(wait=0.2))
        self.addCleanup(convert.release_lock)
        self.assertEqual(self.by_name()["lock"].status, doctor.WARN)

    def test_a_crashing_check_is_a_failure_not_a_crash(self):
        def boom():
            raise RuntimeError("broken probe")
        with mock.patch.object(doctor, "CHECKS", (boom,)):
            [result] = doctor.run_checks()
        self.assertEqual(result.status, doctor.FAIL)
        self.assertIn("broken probe", result.message)


class DebugReportTest(unittest.TestCase):
    def setUp(self):
        fixtures.reset_sandbox()
        state = convert.empty_state()
        state["page_entries"]["1.2.3.4"] = {"id": "1.2.3.4", "ocr": True,
                                            "text": NOTE_TEXT,
                                            "title": "Private title"}
        convert.save_state(state)
        os.makedirs(os.path.join(fixtures.tests.SANDBOX, "output"))
        with open(os.path.join(fixtures.tests.SANDBOX, "output",
                               "index.json"), "w") as f:
            json.dump({"pages": [{"id": "1.2.3.4", "text": NOTE_TEXT}],
                       "sessions": []}, f)
        telemetry.warn(telemetry.get_logger("test"), "page.failed",
                       "page 1.2.3.4 could not be converted",
                       page="1.2.3.4", chars=len(NOTE_TEXT))

    def test_report_has_counts_and_problems_but_no_note_content(self):
        report = doctor.build_report()
        self.assertIn('"pages_ocr_done": 1', report)
        self.assertIn("page.failed", report)
        self.assertNotIn(NOTE_TEXT, report)
        self.assertNotIn("Private title", report)

    def test_home_directory_is_written_as_tilde(self):
        home = os.path.expanduser("~")
        with mock.patch.object(doctor, "build_report",
                               return_value=doctor.scrub("at " + home)):
            path = doctor.write_report()
        with open(path) as f:
            self.assertEqual(f.read(), "at ~")


if __name__ == "__main__":
    unittest.main()
