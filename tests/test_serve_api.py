"""HTTP API guards of serve.py, on an in-process server bound to loopback."""
import http.client
import json
import os
import threading
import unittest
from typing import Any

import tests  # noqa: F401  (sandbox ECHO_ROOT before importing serve)

import serve


class QuietHandler(serve.Handler):
    def log_message(self, format, *args):
        pass


class ServeApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(serve.OUT, exist_ok=True)
        cls.httpd = serve.http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), QuietHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def request(self, method, path, body=None, headers=None) -> Any:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        hdrs = {"Content-Type": "application/json"}
        hdrs.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
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
        self.assertIn("./run", body)

    def test_cross_origin_delete_is_refused(self):
        status, body = self.request(
            "POST", "/api/delete/page", {"id": "1.2.3.4"},
            {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertFalse(body["ok"])

    def test_non_json_content_type_is_refused(self):
        status, _ = self.request("POST", "/api/delete/page", {"id": "1.2.3.4"},
                                 {"Content-Type": "text/plain"})
        self.assertEqual(status, 403)

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
