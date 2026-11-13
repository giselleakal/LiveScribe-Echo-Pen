import datetime
import hashlib
import hmac
import json
import os
import unittest
from unittest import mock

import scribe
from tests import fixtures


def write_index(pages):
    os.makedirs(scribe.OUT, exist_ok=True)
    with open(scribe.INDEX, "w") as f:
        json.dump({"pages": pages, "sessions": []}, f)


class FakeHermes:
    """Stands in for post_body: answers each call with the next status."""

    def __init__(self, *statuses):
        self.statuses = list(statuses)
        self.bodies = []
        self.request_ids = []

    def __call__(self, body, url, secret, request_id, ts=None):
        self.bodies.append(body)
        self.request_ids.append(request_id)
        status = self.statuses.pop(0) if len(self.statuses) > 1 \
            else self.statuses[0]
        return status, b""



class SigningTest(unittest.TestCase):
    def test_signs_timestamp_dot_body(self):
        expected = hmac.new(b"s3cret", b"1700000000.{\"a\":1}",
                            hashlib.sha256).hexdigest()
        self.assertEqual(scribe.sign_v2("s3cret", "1700000000", '{"a":1}'),
                         expected)
        self.assertEqual(scribe.sign_v2("s3cret", "1700000000", b'{"a":1}'),
                         expected)


class ClassifyTest(unittest.TestCase):
    def test_status_classes(self):
        got = {s: scribe.classify(s) for s in (200, 0, 401, 413, 429, 500)}
        self.assertEqual(got, {200: "ok", 0: "net", 401: "config",
                               413: "size", 429: "rate", 500: "server"})


class ChunkingTest(unittest.TestCase):
    def test_large_payload_is_sent_in_signed_slices(self):
        bodies = []

        def fake_post(body, url, secret, request_id, ts=None):
            bodies.append(json.loads(body))
            return 200, b""

        serialized = "x" * (scribe.CHUNK_CHAR_LIMIT * 2 + 1)
        with mock.patch.object(scribe, "post_body", fake_post):
            ok, _ = scribe.send_payload(serialized, "n1", "http://h", "s",
                                        "rid")
        self.assertTrue(ok)
        self.assertEqual([b["__chunk"]["index"] for b in bodies], [0, 1, 2])
        self.assertEqual("".join(b["__chunk"]["data"] for b in bodies),
                         serialized)


class PushTest(unittest.TestCase):
    def setUp(self):
        fixtures.reset_sandbox()
        write_index([{"id": "1.2.3.4", "text": "hello"},
                     {"id": "1.2.3.5", "text": "world"}])
        env = mock.patch.dict(os.environ, {"ECHO_SCRIBE_SECRET": "s"})
        env.start()
        self.addCleanup(env.stop)
        nap = mock.patch.object(scribe.time, "sleep", lambda s: None)
        nap.start()
        self.addCleanup(nap.stop)

    def push(self, hermes, ident="1.2.3.4", force=False):
        with mock.patch.object(scribe, "post_body", hermes):
            return scribe.push_one("page", ident, force=force)

    def test_accepted_note_is_reported_sent(self):
        self.assertEqual(self.push(FakeHermes(200)),
                         (True, "sent to Hermes ✓"))

    def test_bad_signature_is_surfaced_not_queued(self):
        ok, message = self.push(FakeHermes(401))
        self.assertFalse(ok)
        self.assertIn("rejected", message)

    def test_oversized_chunk_rejection_is_surfaced(self):
        write_index([{"id": "1.2.3.4",
                      "text": "x" * (scribe.CHUNK_CHAR_LIMIT * 2)}])
        ok, message = self.push(FakeHermes(200, 413))
        self.assertFalse(ok)
        self.assertIn("size", message)

    def test_unreachable_hermes_queues_the_note(self):
        ok, message = self.push(FakeHermes(0))
        self.assertTrue(ok)
        self.assertIn("queued", message)

    def test_retry_reuses_the_queued_request_id(self):
        hermes = FakeHermes(500, 200)
        self.push(hermes)
        self.assertEqual(len(set(hermes.request_ids)), 1)
        self.assertIsNotNone(hermes.request_ids[0])

    def test_changed_note_gets_a_new_request_id(self):
        first = FakeHermes(200)
        self.push(first)
        write_index([{"id": "1.2.3.4", "text": "edited"}])
        second = FakeHermes(200)
        self.push(second)
        self.assertNotEqual(first.request_ids[0], second.request_ids[0])

    def test_unreachable_hermes_stops_the_flush_early(self):
        scribe.enqueue("1.2.3.5", json.dumps({
            "kind": "page", "id": "1.2.3.5", "sig": "x",
            "request_id": "r", "payload": "{}"}))
        hermes = FakeHermes(0)
        self.push(hermes)
        self.assertEqual(len(hermes.bodies), 1)

    def test_malformed_queue_entry_is_parked_not_fatal(self):
        scribe.enqueue("1.2.3.9", json.dumps({"id": "1.2.3.9"}))
        sent, parked, _ = scribe.flush_queue("http://h", "s")
        self.assertEqual((sent, parked), (0, ["1.2.3.9.json"]))

    def test_chunks_fit_the_byte_limit_for_non_ascii_text(self):
        write_index([{"id": "1.2.3.4",
                      "text": "é\"" * scribe.CHUNK_CHAR_LIMIT}])
        hermes = FakeHermes(200)
        self.push(hermes)
        self.assertGreater(len(hermes.bodies), 1)
        self.assertTrue(all(len(b) <= scribe.MAX_BODY_BYTES
                            for b in hermes.bodies))

    def test_cli_without_secret_fails_loudly(self):
        with mock.patch.dict(os.environ, {"ECHO_SCRIBE_SECRET": ""}):
            with mock.patch("sys.stderr") as err:
                code = scribe.main(["push"])
        self.assertEqual(code, 2)
        self.assertIn("ECHO_SCRIBE_SECRET",
                      "".join(c.args[0] for c in err.write.call_args_list))


class RecordedAtTest(unittest.TestCase):
    def test_offset_has_a_colon(self):
        with mock.patch.dict(os.environ, {"ECHO_UTC_OFFSET": "-05:00",
                                          "ECHO_TZ": ""}):
            self.assertEqual(scribe.recorded_at(1_700_000_000),
                             "2023-11-14T17:13:20-05:00")

    def test_invalid_zone_falls_back_to_the_offset(self):
        with mock.patch.dict(os.environ, {"ECHO_TZ": "Mars/Olympus",
                                          "ECHO_UTC_OFFSET": "+02:00"}):
            self.assertTrue(scribe.recorded_at(1_700_000_000)
                            .endswith("+02:00"))


class OffsetTest(unittest.TestCase):
    def test_offsets_with_and_without_colon(self):
        tz = datetime.timezone(datetime.timedelta(hours=-5))
        self.assertEqual(scribe._parse_offset("-05:00"), tz)
        self.assertEqual(scribe._parse_offset("-0500"), tz)
        self.assertIsNone(scribe._parse_offset("EST"))


if __name__ == "__main__":
    unittest.main()
