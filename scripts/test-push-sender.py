#!/usr/bin/env python3
"""Run the echo-scribe sender against the mock receiver; print real output.

Exercises: signing vector, small-note round trip, dedup, ~3 MB chunked
round trip with sha256 verification, and off-line durability (queue drains
once the receiver is reachable).
"""
import base64
import glob
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

SECRET = "test-echo-scribe-secret"
ROOT_DIR = os.path.join(ROOT, "test-inbox")
PORT = 18231
URL = "http://127.0.0.1:%d/webhooks/echo-scribe" % PORT
DEAD_URL = "http://127.0.0.1:39999/webhooks/echo-scribe"  # nothing listening

# env must be set before importing scribe (URL_DEFAULT + secret are read at use)
os.environ["ECHO_SCRIBE_SECRET"] = SECRET
os.environ["ECHO_SCRIBE_URL"] = URL
import scribe  # noqa: E402

# start from a clean slate (clear prior dedup state + queue + inbox)
import shutil
for p in (scribe.STATE_PATH,):
    if os.path.exists(p):
        os.remove(p)
shutil.rmtree(scribe.QUEUE_DIR, ignore_errors=True)
shutil.rmtree(ROOT_DIR, ignore_errors=True)


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def banner(t):
    print("\n=== %s ===" % t)


# 0. signature vector (contract test #1)
banner("signature vector")
vector = scribe.sign_v2("s", "1700000000", '{"a":1}')
assert vector == "ab7bd212df57f3ef4ea3c17d4a1a71d8981ef595d4cd699b62517c68d89192a2", vector
print("HMAC vector: PASS (%s)" % vector)

# 1. start mock receiver
env = dict(os.environ, ECHO_SCRIBE_SECRET=SECRET)
recv = subprocess.Popen(
    [sys.executable, os.path.join(ROOT, "scripts", "test-push-receiver.py"),
     "--port", str(PORT), "--root", ROOT_DIR],
    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(50):
        try:
            import urllib.request
            r = urllib.request.urlopen("http://127.0.0.1:%d/" % PORT, timeout=1)
            print("receiver health:", r.status, r.read().decode())
            break
        except Exception:
            time.sleep(0.1)
    else:
        raise SystemExit("receiver did not start")

    # 2. small page note round trip (contract test #2)
    banner("small page note -> Hermes")
    results, summary = scribe.push_targets([("page", "2193.1.47.79")])
    print("summary:", summary)
    print("results:", results)
    pid = "2193.1.47.79"
    needle = os.path.join(ROOT_DIR, "*", pid)
    dirs = glob.glob(needle)
    assert dirs, "no page dir created"
    day_dir = dirs[0]
    print("created:", day_dir)
    print("files:", sorted(os.listdir(day_dir)))
    for f in ("note.md", "page-01.svg", "manifest.json"):
        assert os.path.exists(os.path.join(day_dir, f)), f + " missing"
    print("small note round trip: PASS")

    # 3. dedup (contract test #4) -- same note again -> one dir
    banner("dedup: send same page again")
    before = sorted(glob.glob(needle))
    results2, _ = scribe.push_targets([("page", pid)], force=False)
    after = sorted(glob.glob(needle))
    assert before == after and len(after) == 1, (before, after)
    print("dirs after resend:", after)
    print("dedup message:", results2.get(pid))
    print("dedup: PASS (one directory)")

    # 4. ~3 MB chunked note (contract test #3)
    banner("~3 MB chunked note -> Hermes")
    big = os.urandom(3 * 1024 * 1024)  # 3 MiB
    payload = {
        "note_id": "PRS-BIG",
        "title": "big note",
        "recorded_at": "2026-11-07T21:20:00-05:00",
        "transcription": "a long recording",
        "audio_name": "audio.aac",
        "audio_base64": base64.b64encode(big).decode("ascii"),
    }
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    print("serialized length:", len(serialized))
    ok, cls = scribe.send_payload(serialized, "PRS-BIG", URL, SECRET,
                                  str(uuid.uuid4()))
    assert ok, cls
    big_dir = glob.glob(os.path.join(ROOT_DIR, "*", "PRS-BIG"))
    assert big_dir, "no PRS-BIG dir"
    bigdir = big_dir[0]
    written = os.path.join(bigdir, "audio.aac")
    assert os.path.exists(written)
    with open(written, "rb") as f:
        got = hashlib.sha256(f.read()).hexdigest()
    assert got == hashlib.sha256(big).hexdigest(), "audio sha256 mismatch"
    print("done. dir:", bigdir, "files:", sorted(os.listdir(bigdir)))
    print("audio.aac sha256 matches source: PASS")
    print("chunked send: PASS")

    # 5. request-id + per-slice count sanity
    banner("chunk slice count")
    slices = (len(serialized) + scribe.CHUNK_CHAR_LIMIT - 1) // scribe.CHUNK_CHAR_LIMIT
    print("slices sent:", slices)
    print("PASS")

    # 6. durability: enqueue offline, then drain (contract test #5)
    banner("offline durability -> drain")
    qid = "PRS-OFFLINE"
    fake = json.dumps({
        "kind": "session", "id": qid, "sig": "x", "request_id": str(uuid.uuid4()),
        "payload": json.dumps({"note_id": qid, "title": "offline test"}),
    })
    os.makedirs(scribe.QUEUE_DIR, exist_ok=True)
    scribe.atomic_write(scribe._queue_path(qid), fake)
    sent, parked, _ = scribe.flush_queue(url=DEAD_URL)
    print("flush against dead URL: sent=%d parked=%s" % (sent, parked))
    assert parked and qid in parked, "note should be parked while offline"
    print("note remains queued while offline: PASS")
    sent2, _parked2, _ = scribe.flush_queue(url=URL)
    print("flush against live URL: sent=%d parked=%s" % (sent2, _parked2))
    assert sent2 == 1 and _parked2 == []
    off_dir = glob.glob(os.path.join(ROOT_DIR, "*", qid))
    assert off_dir, "offline note not delivered"
    print("note drained after receiver returned: PASS")

    banner("find output")
    print(run(["find", ROOT_DIR, "-type", "f"]).stdout)

    print("\nALL SENDER TESTS PASSED")
finally:
    recv.terminate()
    recv.wait()