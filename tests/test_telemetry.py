"""Shape, redaction and correlation of the structured logs."""
import json
import os
import re
import subprocess
import sys
import threading
import unittest
from unittest import mock

import telemetry
from tests import REPO, fixtures

STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")


def records():
    return list(telemetry.read_records())


def last(event=None):
    for rec in reversed(records()):
        if event is None or rec.get("event") == event:
            return rec
    raise AssertionError(f"no {event} record")


class RecordShapeTest(unittest.TestCase):
    def setUp(self):
        fixtures.reset_sandbox()
        self.log = telemetry.get_logger("test")

    def test_record_has_the_stable_fields(self):
        self.log.info("thing.done", extra={"pages": 3})
        rec = last("thing.done")
        self.assertRegex(rec["ts"], STAMP)
        self.assertEqual((rec["level"], rec["component"], rec["pages"]),
                         ("INFO", "test", 3))
        self.assertEqual(rec["run_id"], telemetry.run_id())

    def test_exceptions_carry_type_message_and_stack(self):
        try:
            raise ValueError("bad page")
        except ValueError:
            self.log.warning("page.failed", exc_info=True)
        rec = last("page.failed")
        self.assertEqual((rec["exc_type"], rec["exc_msg"]),
                         ("ValueError", "bad page"))
        self.assertIn("Traceback", rec["stack"])

    def test_secrets_are_redacted(self):
        with mock.patch.dict(os.environ, {"ECHO_SCRIBE_SECRET": "hunter22"}):
            self.log.info("push", extra={
                "secret": "x", "headers": {"Authorization": "Bearer y"},
                "url": "http://h/?k=hunter22"})
        rec = last("push")
        self.assertEqual(rec["secret"], "[redacted]")
        self.assertEqual(rec["headers"]["Authorization"], "[redacted]")
        self.assertNotIn("hunter22", json.dumps(rec))

    def test_request_id_is_per_thread(self):
        seen = {}

        def work(rid):
            with telemetry.bound_request(rid):
                self.log.info("req", extra={"tag": rid})

        threads = [threading.Thread(target=work, args=(f"r{i}",))
                   for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for rec in records():
            if rec.get("event") == "req":
                seen[rec["tag"]] = rec["request_id"]
        self.assertEqual(seen, {f"r{i}": f"r{i}" for i in range(5)})


class IoCallTest(unittest.TestCase):
    def setUp(self):
        fixtures.reset_sandbox()
        self.log = telemetry.get_logger("test")
        ticks = iter([10.0, 10.25])
        patcher = mock.patch.object(telemetry, "monotonic",
                                    lambda: next(ticks))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_one_record_with_duration_and_outcome(self):
        with telemetry.io_call(self.log, "tesseract", page="1.2") as call:
            call["status"] = 0
        rec = last("io")
        self.assertEqual((rec["target"], rec["outcome"], rec["status"],
                          rec["duration_ms"], rec["page"]),
                         ("tesseract", "ok", 0, 250.0, "1.2"))

    def test_failure_is_recorded_and_reraised(self):
        with self.assertRaises(OSError), telemetry.io_call(self.log, "ffmpeg"):
            raise OSError("no such file")
        rec = last("io")
        self.assertEqual((rec["outcome"], rec["level"], rec["exc_type"]),
                         ("error", "WARNING", "OSError"))


class CliTest(unittest.TestCase):
    def setUp(self):
        fixtures.reset_sandbox()

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, os.path.join(REPO, "src", "telemetry.py")] +
            list(args), capture_output=True, text=True, timeout=60,
            env=dict(os.environ, ECHO_RUN_ID="cli-run"))

    def test_event_then_logs_round_trip(self):
        self.cli("event", "run", "pen.download", "status=3", "target=x",
                 "--level", "WARNING")
        self.cli("event", "run", "pen.ok")
        out = self.cli("logs", "--errors", "--json").stdout.splitlines()
        [rec] = [json.loads(line) for line in out]
        self.assertEqual((rec["event"], rec["status"], rec["run_id"]),
                         ("pen.download", 3, "cli-run"))

    def test_empty_log_says_so(self):
        proc = self.cli("logs")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no matching log records", proc.stderr)


class ServeRecordTest(unittest.TestCase):
    def test_each_request_gets_one_record_with_its_own_id(self):
        import http.client

        import serve
        from tests.test_serve_api import start_server
        fixtures.reset_sandbox()
        httpd = start_server("127.0.0.1")
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        for _ in range(2):
            conn = http.client.HTTPConnection("127.0.0.1",
                                              httpd.server_address[1])
            conn.request("POST", "/api/title", body=b"[]",
                         headers={"Content-Type": "application/json"})
            conn.getresponse().read()
            conn.close()
        recs = [r for r in records() if r.get("event") == "http.request"]
        self.assertEqual([(r["status"], r["level"]) for r in recs],
                         [(400, "INFO"), (400, "INFO")])
        self.assertEqual(len({r["request_id"] for r in recs}), 2)
        refused = [r for r in records() if r.get("event") == "api.refused"]
        self.assertEqual({r["request_id"] for r in refused},
                         {r["request_id"] for r in recs})
        self.assertTrue(serve.log)


@unittest.skipUnless(fixtures.has_cairo(), "pycairo not installed")
class CorrelationTest(unittest.TestCase):
    def test_convert_child_logs_under_the_parent_run_id(self):
        root = fixtures.fresh_root(fixtures.tests.SANDBOX, "corr")
        fixtures.write_page(root, "2208.1.12.25",
                            fixtures.text_line(500, 600, 3))
        proc = fixtures.run_convert(root, env={"ECHO_RUN_ID": "parent-run"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(os.path.join(root, "logs", "echo.jsonl")) as f:
            recs = [json.loads(line) for line in f]
        done = [r for r in recs if r["event"] == "convert.done"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["run_id"], "parent-run")
        self.assertEqual(done[0]["pages"], 1)


if __name__ == "__main__":
    unittest.main()
