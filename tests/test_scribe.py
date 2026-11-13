import datetime
import hashlib
import hmac
import json
import unittest
from unittest import mock

import tests  # noqa: F401  (sandbox ECHO_ROOT before importing scribe)

import scribe


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


class OffsetTest(unittest.TestCase):
    def test_offsets_with_and_without_colon(self):
        tz = datetime.timezone(datetime.timedelta(hours=-5))
        self.assertEqual(scribe._parse_offset("-05:00"), tz)
        self.assertEqual(scribe._parse_offset("-0500"), tz)
        self.assertIsNone(scribe._parse_offset("EST"))


if __name__ == "__main__":
    unittest.main()
