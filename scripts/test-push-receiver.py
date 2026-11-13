#!/usr/bin/env python3
"""Minimal mock of the Hermes "echo-scribe" webhook receiver.

Implements the contract the real receiver documents, so the sender's
signing, chunking, response handling and durability can be tested against
something faithful:

  * validates the Generic V2 HMAC signature (X-Webhook-Signature-V2) with a
    +-300s timestamp check, returning 401 on mismatch;
  * enforces a ~1 MB body cap -> 413;
  * reassembles `__chunk` envelopes by note_id;
  * writes files under <root>/<date>/<note_id>/ as note.md, page-NN.svg,
    audio.aac and manifest.json (by extension), deriving date from the
    current day;
  * skips re-writing an existing note_id directory (dedup) but still 200s;
  * responds 429 after more than RATE_LIMIT requests in a minute (off by
    default for deterministic tests).

Usage:
  ECHO_SCRIBE_SECRET=<secret> python3 scripts/test-push-receiver.py \
      --port 18044 [--root <dir>] [--rate-limit N]
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECRET = os.environ.get("ECHO_SCRIBE_SECRET", "test-secret")


def verify_v2(secret, headers, body):
    ts = headers.get("X-Webhook-Timestamp", "")
    sig = headers.get("X-Webhook-Signature-V2", "")
    if not ts or not sig:
        return False, "missing signature headers"
    try:
        now = int(time.time())
        if abs(now - int(ts)) > 300:
            return False, "clock skew"
    except ValueError:
        return False, "bad timestamp"
    expected = hmac.new(
        secret.encode(), str(ts).encode() + b"." + body,
        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return False, "bad signature"
    return True, "ok"


class ReceiverServer(ThreadingHTTPServer):
    """The mock's shared state: inbox root, rate limit, pending chunks."""

    def __init__(self, address, root, rate_limit=0):
        super().__init__(address, Receiver)
        self.root = os.path.abspath(root)
        self.rate_limit = rate_limit
        self.req_times = []
        self.chunks = {}


class Receiver(BaseHTTPRequestHandler):
    server: ReceiverServer
    server_version = "MockEchoScribe/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass  # quiet by default; our test prints its own status lines

    def send_json(self, status, payload, headers=None):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path.rstrip("/") != "/webhooks/echo-scribe":
            self.send_json(404, {"status": "unknown_route"})
            return
        if self.server.rate_limit:
            self.server.req_times.append(time.time())
            recent = [t for t in self.server.req_times
                      if time.time() - t < 60]
            if len(recent) > self.server.rate_limit:
                self.send_json(429, {"status": "rate_limited"})
                return
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1000000:  # ~1 MB cap
            self.send_json(413, {"status": "too_large"})
            return
        body = self.rfile.read(length).decode("utf-8", "replace")
        ok, why = verify_v2(SECRET, self.headers, body.encode())
        if not ok:
            self.send_json(401, {"status": "ignored", "reason": why})
            return
        # replay protection via X-Request-ID (1h) -- not the focus here
        payload = json.loads(body)
        try:
            if "__chunk" in payload:
                chunk = payload["__chunk"]
                self.server.chunks.setdefault(chunk["id"], {})[chunk["index"]] = \
                    chunk["data"]
                if len(self.server.chunks[chunk["id"]]) == chunk["total"]:
                    serialized = "".join(
                        self.server.chunks[chunk["id"]][i]
                        for i in range(chunk["total"]))
                    del self.server.chunks[chunk["id"]]
                    deliver(chunk["id"], json.loads(serialized),
                            self.server.root)
                self.send_json(200, {"status": "delivered"})
                return
            note_id = payload.get("note_id")
            deliver(note_id, payload, self.server.root)
            self.send_json(200, {"status": "delivered"})
        except Exception:
            import traceback
            traceback.print_exc()
            self.send_json(500, {"status": "error", "reason": "deliver failed"})

    def do_GET(self):
        self.send_json(200, {"status": "ok", "platform": "webhook"})


def deliver(note_id, note, root):
    day = time.strftime("%Y-%m-%d")
    dst = os.path.join(root, day, str(note_id))
    if os.path.isdir(dst):
        return  # dedup: already present
    os.makedirs(dst, exist_ok=True)
    title = note.get("title") or note_id
    text = note.get("text") or ""
    transcript = note.get("transcription") or ""
    parts = []
    components: list = [("note.md",
                   "# %s\n\n## Text\n%s\n\n## Transcript\n%s\n"
                   % (title, text, transcript))]
    for i, svg in enumerate(note.get("svg_pages", []), 1):
        if isinstance(svg, dict):
            name, svg = svg.get("name"), svg.get("data", "")
        else:
            name = "page-%02d.svg" % i
        components.append((name, svg))
    if note.get("audio_base64"):
        raw = base64.b64decode(note["audio_base64"])
        name = note.get("audio_name", "audio.aac")
        components.append((name, raw))
    for name, content in components:
        if isinstance(content, bytes):
            data = content
        else:
            data = content.encode("utf-8")
        with open(os.path.join(dst, name), "wb") as f:
            f.write(data)
        parts.append({
            "name": name,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    with open(os.path.join(dst, "manifest.json"), "w") as f:
        json.dump({
            "note_id": note_id, "title": title,
            "received_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "files": parts,
        }, f, indent=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18044)
    ap.add_argument("--root", default="test-inbox")
    ap.add_argument("--rate-limit", type=int, default=0)
    args = ap.parse_args()
    srv = ReceiverServer(("127.0.0.1", args.port), args.root,
                         rate_limit=args.rate_limit)
    print("mock receiver on http://127.0.0.1:%d (root=%s)" % (args.port, srv.root))
    srv.serve_forever()


if __name__ == "__main__":
    main()
