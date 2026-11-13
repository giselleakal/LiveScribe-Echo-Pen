"""HTTP API guards of serve.py, on an in-process server bound to loopback."""
import http.client
import json
import os
import threading
import unittest
from typing import Any
from unittest import mock

import convert
import serve
from tests import fixtures


class QuietHandler(serve.Handler):
    def log_message(self, format, *args):
        pass


def start_server(host):
    httpd = serve.http.server.ThreadingHTTPServer((host, 0), QuietHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def write_index(pages):
    os.makedirs(serve.OUT, exist_ok=True)
    with open(serve.INDEX, "w") as f:
        json.dump({"pages": pages, "sessions": []}, f)


class ServeApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = start_server("127.0.0.1")
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        fixtures.reset_sandbox()
        os.makedirs(serve.OUT, exist_ok=True)
        patcher = mock.patch.object(serve, "LOCK_WAIT", 0.2)
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, method, path, body=None, headers=None, raw=None,
                port=None) -> Any:
        conn = http.client.HTTPConnection("127.0.0.1", port or self.port,
                                          timeout=30)
        hdrs = {"Content-Type": "application/json"}
        hdrs.update(headers or {})
        data = json.dumps(body).encode() if body is not None else raw
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        payload: Any
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = raw.decode("utf-8", "replace")
        return resp.status, payload

    def test_index_without_output_explains_what_to_run(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith("<h2>No converted output yet"),
                        body[:80])

    def test_cross_origin_delete_is_refused(self):
        status, body = self.request(
            "POST", "/api/delete/page", {"id": "1.2.3.4"},
            {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertFalse(body["ok"])

    def test_non_json_content_type_is_refused(self):
        status, _ = self.request("POST", "/api/delete/page", {"id": "1.2.3.4"},
                                 {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)

    def test_simple_request_disguised_as_json_is_refused(self):
        for path in ("/api/title", "/api/rename", "/api/delete",
                     "/api/transcribe/page", "/api/push/page"):
            status, _ = self.request(
                "POST", path, {"id": "1", "name": "x"},
                {"Content-Type": "text/plain;x=application/json"})
            self.assertEqual(status, 415, path)

    def test_null_origin_is_refused(self):
        status, _ = self.request("POST", "/api/title",
                                 {"id": "1.2.3.4", "name": "x"},
                                 {"Origin": "null"})
        self.assertEqual(status, 403)
        self.assertFalse(os.path.exists(convert.TITLES_PATH))

    def test_foreign_host_header_is_refused(self):
        evil = "evil.example:%d" % self.port
        status, _ = self.request("GET", "/", headers={"Host": evil})
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/api/title",
                                 {"id": "1.2.3.4", "name": "x"},
                                 {"Host": evil, "Origin": "http://" + evil})
        self.assertEqual(status, 403)

    def test_localhost_names_are_accepted(self):
        for host in ("localhost", "127.0.0.1", "LOCALHOST"):
            status, _ = self.request(
                "GET", "/", headers={"Host": "%s:%d" % (host, self.port)})
            self.assertEqual(status, 200, host)

    def test_non_object_body_is_a_400(self):
        for body in ([], "x", 5, {"id": 5}, {"id": None}):
            status, _ = self.request("POST", "/api/delete/page", body)
            self.assertEqual(status, 400, body)

    def test_bad_content_length_is_rejected(self):
        status, _ = self.request("POST", "/api/title", raw=b"{}",
                                 headers={"Content-Length": "-1"})
        self.assertEqual(status, 400)
        status, _ = self.request(
            "POST", "/api/title", raw=b"{}",
            headers={"Content-Length": str(serve.MAX_BODY_BYTES + 1)})
        self.assertEqual(status, 413)

    def test_ids_with_trailing_newline_or_unicode_digits_are_rejected(self):
        for ident in ("1.2.3.4\n", "\u0661.2"):
            status, _ = self.request("POST", "/api/title",
                                     {"id": ident, "name": "x"})
            self.assertEqual(status, 400, repr(ident))

    def test_busy_lock_is_a_409(self):
        self.assertTrue(convert.acquire_lock(wait=0.2))
        self.addCleanup(convert.release_lock)
        status, body = self.request("POST", "/api/title",
                                    {"id": "1.2.3.4", "name": "x"})
        self.assertEqual(status, 409)
        self.assertIn("busy", body["error"])

    def test_unknown_page_delete_is_a_404(self):
        with mock.patch.object(serve, "_reconcile"):
            status, _ = self.request("POST", "/api/delete/page",
                                     {"id": "1.2.3.4"})
        self.assertEqual(status, 404)

    def test_failed_batch_delete_still_reconciles(self):
        for pid in ("1.2.3.4", "1.2.3.5"):
            os.makedirs(os.path.join(serve.OUT, "pages", pid))
        with mock.patch.object(serve, "delete_page",
                               side_effect=[True, OSError("disk")]), \
                mock.patch.object(serve, "_reconcile") as reconcile:
            status, _ = self.request("POST", "/api/delete", {"items": [
                {"kind": "page", "id": "1.2.3.4"},
                {"kind": "page", "id": "1.2.3.5"}]})
        self.assertEqual(status, 500)
        reconcile.assert_called_once()

    def test_push_failure_reason_reaches_the_ui(self):
        write_index([{"id": "1.2.3.4"}])
        with mock.patch.dict(os.environ, {"ECHO_SCRIBE_SECRET": "s"}), \
                mock.patch.object(serve.scribe, "push_one",
                                  return_value=(False, "Hermes rejected")):
            status, body = self.request("POST", "/api/push/page",
                                        {"id": "1.2.3.4"})
        self.assertEqual(status, 502)
        self.assertEqual(body["error"], "Hermes rejected")

    def test_push_without_index_is_a_404(self):
        with mock.patch.dict(os.environ, {"ECHO_SCRIBE_SECRET": "s"}):
            status, _ = self.request("POST", "/api/push/page",
                                     {"id": "1.2.3.4"})
        self.assertEqual(status, 404)

    def test_bulk_delete_button_is_not_bound_to_single_delete(self):
        self.assertIn(".delete[data-id]", serve.DEL_JS)

    def test_traversal_ids_are_rejected(self):
        for ident in ("../../etc", "1.2/../3", ".hidden", "1.2.3.4\x00"):
            status, _ = self.request("POST", "/api/delete/page", {"id": ident})
            self.assertEqual(status, 400, ident)

    def test_bulk_delete_needs_typed_confirmation(self):
        items = [{"kind": "page", "id": "1.2.3.%d" % i}
                 for i in range(serve.BULK_DELETE_LIMIT + 1)]
        status, body = self.request("POST", "/api/delete", {"items": items})
        self.assertEqual(status, 400)
        self.assertIn("DELETE", body["error"])

    def test_malformed_batch_item_removes_nothing(self):
        status, _ = self.request("POST", "/api/delete", {
            "items": [{"kind": "page", "id": "1.2.3.4"},
                      {"kind": "page", "id": "../x"}]})
        self.assertEqual(status, 400)

    def test_rename_rejects_bad_notebook_id(self):
        status, _ = self.request("POST", "/api/rename",
                                 {"id": "../nb", "name": "x"})
        self.assertEqual(status, 400)

    def test_title_length_is_capped(self):
        status, body = self.request("POST", "/api/title",
                                    {"id": "1.2.3.4", "name": "x" * 81})
        self.assertEqual(status, 400)
        self.assertIn("too long", body["error"])

    def test_push_without_secret_reports_not_configured(self):
        status, body = self.request("POST", "/api/push/page", {"id": "1.2.3.4"})
        self.assertEqual(status, 503)
        self.assertIn("ECHO_SCRIBE_SECRET", body["error"])

    def test_unknown_api_is_404(self):
        status, _ = self.request("POST", "/api/nope", {})
        self.assertEqual(status, 404)


class LanServerTest(unittest.TestCase):
    """A server bound to all interfaces refuses mutations by default."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = start_server("0.0.0.0")
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_transcribe_and_push_are_loopback_only(self):
        for path in ("/api/transcribe/page", "/api/push/page"):
            conn = http.client.HTTPConnection("127.0.0.1", self.port,
                                              timeout=30)
            conn.request("POST", path, body=b'{"id": "1.2.3.4"}',
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            body = json.loads(resp.read())
            conn.close()
            self.assertEqual(resp.status, 403, path)
            self.assertIn("ECHO_ALLOW_LAN_DELETES", body["error"])


class FormatWhenTest(unittest.TestCase):
    def test_same_day_range_is_abbreviated(self):
        self.assertEqual(
            serve._fmt_when("2026-11-10T10:16:00-0600",
                            "2026-11-10T10:34:00-0600"),
            "Nov 10, 2026, 10:16 AM - 10:34 AM")

    def test_unparseable_is_empty(self):
        self.assertEqual(serve._fmt_when("yesterday"), "")


if __name__ == "__main__":
    unittest.main()
