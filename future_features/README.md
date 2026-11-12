# Future features (not yet implemented)

Written-up specs for the planned features, gathered so they can be
implemented without re-deriving the research. Each section is written to
be actionable: decisions are made, the file/line touchpoints are listed,
and acceptance criteria are spelled out.

The current milestone (all P0/P1/P2 bug fixes + the `./run` entry point)
was completed first; these are the next features.

---

## Feature A — Delete assets (SVG pages and audio recordings)

**Goal.** A delete button next to every asset (page card and recording
card) in the web UI that removes the asset from the local pipeline —
the SVG, its thumbnail, its OCR output, and the audio recording — and
keeps it deleted across re-syncs. Optionally, a bulk pen wipe.

### Current state after the P0/P1/P2 milestone

- `src/serve.py` is read-only: only `do_GET`; no POST endpoints, no auth.
- `src/convert.py` is now incremental (`.convert-state.json`) and prunes
  output whose source vanished from `data/` — this is the mechanism the
  delete feature must plug into (deleting a file from `data/` makes the
  asset disappear on the next convert, correctly and automatically).
- `vendor/dumpscribe -c` can already wipe a *whole notebook* (pages +
  audio) from the pen after a successful download (`delete_notebook_pages`
  / `delete_notebook_audio`, dumpscribe.c ~`569-590`). There is no
  per-page/per-session pen delete protocol: granularity is per-notebook,
  wired in `main()` (~`936`) behind the `-c` flag.

### Design

1. **New POST API in `src/serve.py`** (keep the stdlib-only approach):

   - `POST /api/delete/page` — body `{"id": "<page_id>"}`:
     - remove `data/data/<id>/` (raw .stf dump) and `output/pages/<id>/`
       (svg, thumb, text.txt, ocr-*.png);
     - remove the page from `.convert-state.json` (`page_entries` +
       `sources` key matching its .stf relpath) so the incremental
       state does not resurrect it;
     - respond `{"ok": true}`.
   - `POST /api/delete/session` — body `{"id": "<session_id>"}`:
     - delete the raw session dir: `data/userdata/*/Paper\ Replay/251/*/*/sessions/<id>/`,
       which removes `audio-*.aac`, `session.info`, `session.pages`;
     - remove `output/audio/<id>.*.aac` and the matching
       `session_entries` / `sources` state keys.

   Both must re-write `index.json` (subtract the deleted asset) **using
   the same atomic write pattern as `convert.py`** (`tempfile + os.replace`)
   so a concurrently-running convert or a concurrent HTTP GET can never
   observe a torn file. Simplest correct route: perform the deletion
   through the same helpers (`atomic_write`, `file_sig`) by importing
   them from `convert.py`, then telling `convert` to reconcile via
   `subprocess.run([sys.executable, ROOT/src/convert.py], check=False)`
   so all pruning/linking logic stays in exactly one place.

2. **Concurrency guard.** A lock file (e.g. `.convert.lock` created with
   `O_CREAT | O_EXCL`) must be honored by:
   - `convert.py main()` — fail fast with a clear message if locked;
   - the delete endpoints — retry/wait briefly.
   `ThreadingHTTPServer` serves each request in its own thread, so
   deletes themselves must serialize against each other too (the lock
   covers this).

3. **UI.** In `serve.py`'s `PAGE_TPL`:
   - add a small "Delete" button to every `.page` card (next to
     "Download .txt") and every `.sess` card;
   - JS: `fetch('/api/delete/page', {method:'POST', body: JSON.stringify({id})})`
     → on ok, remove the card from the DOM (and its audio files).
   - confirm dialog before firing (`confirm()` is fine at this fidelity).
   - add a repo-root `run` passthrough if needed: none required.

4. **Security (small but real).** The server has no auth. Even bound to
   `127.0.0.1`, any other local process or a browser CSRF-style request
   from another tab could hit the delete endpoint. Minimum bar:
   - validate `Content-Type: application/json`, and `"Origin"` matches
     the request's `Host` (i.e. same-origin check) for `/api/*`;
   - path-traversal guard: never accept an `id` containing `/`, `\`,
     `..`, or leading `.`; regex-validate the id shape
     (`page: ^\d+(\.\d+)*$`, `session: ^PRS-[0-9a-f]+$`);
   - `./run serve --host 0.0.0.0` + deletes should warn loudly (a
     single env var, e.g. `ECHO_ALLOW_LAN_DELETES=1`, gates the API
     when the host isn't loopback).

5. **Optional: pen wipe (explicit, destructive).** The pen can't delete
   per-asset. Two cheap options:

   - **`./run clean-pen`**: prompts for confirmation, then runs the
     existing `sudo vendor/dumpscribe -c data` path — download-then-retire
     mode already supported by the vendored binary (every notebook that
     downloads successfully is retired from the pen). This is the safest
     thing that requires zero new C code.
   - Per-notebook/not-per-page deletion would require extending
     `dumpscribe.c` (the retire command is a per-notebook lspcommand;
     per-page artillery is not in the vendor protocol). Out of scope
     unless we decide to fork dumpscribe more deeply.

   **Recommendation:** ship `./run clean-pen` + local tombstone deletes;
   revisit per-page pen deletion only if bulk-wipe isn't enough.

### Acceptance checklist

- [ ] Delete button renders for every page and recording card.
- [ ] Clicking delete (after confirm) removes the asset from the DOM
      immediately; a page reload keeps it gone (state file + data dir +
      output dir are all consistent — grep for the id in all three).
- [ ] `convert.py` reruns do not resurrect a deleted asset.
- [ ] Two rapid deletes of different assets don't corrupt `index.json`
      (atomic writes; no torn file ever seen by the server).
- [ ] Deleting a referenced page leaves recordings intact (they simply
      lose the page link, printed as before as a conversion warning).
- [ ] A request with a malicious id (`../..`, NUL, unicode tricks) is
      rejected with 400 and touches nothing.

---

## Feature B — Speech-to-text transcription of pen recordings

**Goal.** Transcribe the Paper Replay `.aac` recordings into text,
stored per-session and shown in the web UI in the same pattern the page
OCR already uses (transcript disclosure + `Download .txt` button),
searchable alongside page text.

### Current state after the P0/P1/P2 milestone

- `output/audio/<session_id>.<n>.aac` exists (36 sessions on this pen).
- `index.json` sessions already carry `urls` and, linked, `pages`; the
  UI renders `<audio controls>` players per part.
- No `ffmpeg` on this machine — needed to decode AAC. No GPU — this WSL2
  box runs CPU-only PyTorch already (TrOCR), and `.venv` has
  `torch/transformers` installed, so any transformers-supported ASR
  model is nearly free to add dependency-wise.
- The incremental machinery in `convert.py` (`.convert-state.json`,
  `file_sig`, `atomic_write`, prune) is exactly the pattern to copy.

### Model pick (researched Nov 2026 — "the new hot small one")

| Candidate | Size | CPU story | Notes |
|---|---|---|---|
| **Moonshine (Useful Sensors)** — `UsefulSensors/moonshine-base` (or `-tiny`) | 27M–61M params | Designed for edge/CPU; beats Whisper tiny/base on accuracy per parameter; transformers has native support (`moonshine` model class) | Best fit: new + hot + small + already-compatible stack. English-only. |
| NVIDIA Parakeet TDT 0.6B v2/v3 | ~600M | Great accuracy/speed on CPU only via ONNX int8 (`onnx-asr`) | Hot, but brings a new runtime dep (onnxruntime); heavier. |
| distil-whisper / faster-whisper large-v3-turbo | 756M–809M | CTranslate2 int8, realtime-ish on CPU | Boring but solid fallback. |

**Decision: Moonshine (base v2, fallback tiny)** via the existing
`.venv` (transformers). Falls back to a documented non-vendored English
model; `MOONSHINE_MODEL` env var overrides, mirroring `ocr.py`'s
`TROCR_MODEL` pattern.

### Design

1. **New module `src/stt.py`**, deliberately mirroring `src/ocr.py`:
   - `available()` → True when transformers (and a bundled ffmpeg, below)
     can be imported/found;
   - `transcribe(path)` → lazy-load `pipeline("automatic-speech-recognition", model=...)`
     or the manual `model.generate()` loop if transformers lacks a
     pipeline for Moonshine; enforce `torch.set_num_threads` like
     `_load_trocr` does; 16 kHz mono resampled float32 waveform in.

2. **Audio decode.** No system ffmpeg; don't need sudo:
   - add `imageio-ffmpeg` to `requirements.txt` — it ships a static
     ffmpeg binary via pip;
   - `stt.py` uses `imageio_ffmpeg.get_ffmpeg_exe()` and drives
     `ffmpeg -i file -f f32le -ac 1 -ar 16000 pipe:` to get raw floats.
   - Multi-part sessions are stitched in part order before decoding
     (`audio-2` before `audio-10` — the natural-sort helper in
     `convert.py` already exists and is exported).

3. **Chunking.** Pen recordings can be long; model is short-form:
   - chunk to ~30 s windows with 1 s overlap; concatenate texts with a
     simple "word-boundary stitching" (drop duplicated boundary token).
     No timestamps v1; add timestamps later if desired.

4. **Storage + surfacing** (per-page OCR mirror pattern):
   - outputs `output/audio/<session_id>.txt` (atomically) and puts the
     transcript in `index.json` under `sessions[n]["transcript"]`;
   - `serve.py` renders a "Transcript" `<details>` + "Download .txt"
     link per recording, same CSS as the page transcription; the
     client-side search gets the transcript folded into
     `data-search` so `moonshine ht2000` finds the right recording.

5. **Incremental state**: `convert.py`'s
   `session_entries[sid]["stt"] = {"engine/model": ..., "done": true}`
   plus existing aac sig reuse — skip transcription when unchanged,
   same way page OCR entries are reused. A `--force` reruns.

6. **Trigger points**:
   - `./run stt [--session PRS-…]` — explicit, only path that transcribes;
   - NOT automatic in `./run`/`./run sync` (recordings are ~8 MB here,
     but a used pen could hold hours of audio; don't surprise the user
     with a CPU-hour sync).

7. **Failure/edge handling**:
   - audio that is actually silence/room noise → empty transcript,
     UI shows "no speech detected" rather than nothing;
   - a session without `session.info` still transcribes (the aac is
     self-contained);
   - per-part failures degrade to partial transcript with a warning.

### Acceptance checklist

- [ ] `./run stt` transcribes all 36 sessions on this box without sudo
      (bundled ffmpeg) and finishes in bounded, logged progress.
- [ ] Transcripts appear in the UI under each recording and are
      downloadable; search matches transcript words.
- [ ] Second `./run stt` is a no-op for unchanged sessions.
- [ ] A mangled/part AAC file yields a warning, not a crash.
- [ ] `MOONSHINE_MODEL=...` override takes effect (hits HF cache dir).

---

## Feature D — Send notes to Hermes (echo-scribe webhook)

**Goal.** An export that pushes each note from the notebook to the user's
Hermes agent via its **webhook adapter** (`http://host:8644/webhooks/echo-scribe`),
using the documented **Generic V2 signing scheme**. Delivered from the CLI
(`./run push`) and a per-card "Send to Hermes" button in the web UI, with
rsync-style dedup, an on-disk durable queue, and chunking for oversized notes.

### Why the webhook (not the API server)

Hermes (Nous Research's self-hosted agent) exposes two inbound surfaces. The
user's contract pins the **webhook adapter** (route `echo-scribe`, port 8644,
HMAC V2), which is event-driven and fire-and-forget — right for an export
that "just delivers a note". The OpenAI-compatible API server (`/v1/chat/...`)
was scoped out because the contract dictates webhook semantics.

### Contract (implement exactly; do not redesign)

- **POST** `http://localhost:8644/webhooks/echo-scribe`; no bearer token.
- **Signing (Generic V2):** `ts = unix seconds as str`; `sig = hex HMAC-SHA256`
  of `f"{ts}.{body}"` keyed with the shared secret, where `body` is the exact
  bytes sent (serialize first, sign those bytes). Headers:
  `Content-Type: application/json`, `X-Webhook-Timestamp: <ts>`,
  `X-Webhook-Signature-V2: <sig>`, optional `X-Request-ID` (uuid per logical
  note; 1h dedupe).
- **Secret:** read at runtime from `ECHO_SCRIBE_SECRET` env; never hardcode/
  commit. Value on the Hermes machine lives at
  `/opt/data/inbox/echo-scribe/WEBHOOK.md`.
- **Payload** (one JSON object per finalized note, omit empty fields):
  `note_id` (stable+unique per note), `title`, `recorded_at` (ISO 8601 with
  the pen's local UTC offset), `text` (page OCR), `transcription` (session
  transcript), `svg_pages` (raw SVG markup strings in order),
  `audio_name`/`audio_base64` (raw `.aac`, no `data:` prefix; omit if none),
  `assets[]` (future extras: `{"name", "text"|"base64"}`).
- **Chunking:** serialized body > 600,000 chars → slice the exact serialized
  string into ≤600k-char slices; send sequentially as
  `{"__chunk": {"id": <note_id>, "index": <0-based>, "total": <n>, "data": <slice>}}`;
  sign each slice separately; wait for each 200 before the next.
- **Responses:** 200 = stored (do not parse status); 401 = surface config
  error (signature/clock >300s skew), no blind retry; 413 = surface;
  429 = retry after delay; 5xx/network/timeout = exponential backoff
  (≥3 attempts) then park.
- **Durability:** failed/offline uploads never block capture/transcription;
  unsent notes queue on disk, flush in the background-ish, survive restart;
  accepted `note_id`s are persisted so a restart does not re-upload; never
  drop a note while the network is down.

### Current state / design decisions

- Repo assets: **pages** (`output/pages/<id>/page.svg` + OCR `text` in
  `output/index.json`) and **sessions** (`output/audio/<id>.n.aac` + `transcript`).
  One note each: page note (`note_id` = page id) and session note
  (`note_id` = `PRS-…`). Audio stays `.aac` (the feel agent accepted `.aac`),
  named `audio.aac`; multi-part sessions concatenate their `.aac` parts in
  order.
- `recorded_at` sourced from the pen data's file mtime, rendered in
  `ECHO_TZ` (IANA) or `ECHO_UTC_OFFSET` (e.g. `-05:00`); fallback UTC.
- Dedup mirrors the repo idiom (`.convert-state.json`/`file_sig`): a
  `.push-state.json` records per `note_id` a content `sig` (sha256 over the
  payload's metadata: text/transcription + svg/audio size+mtime). Unchanged +
  accepted → skip; content changed → re-push; `--force` overrides.
- Delivery runs synchronously on request, but a note that can't be delivered
  is parked in `output/push-queue/` (atomic writes) and drained on the next
  push / `./run push --flush-only`.

### Files / touchpoints

- `src/scribe.py` (new, stdlib-only): `sign_v2`, `recorded_at`,
  `build_page_payload` / `build_session_payload`, `page_content_sig` /
  `session_content_sig`, `post_body`, `send_payload` (chunking), queue
  (`enqueue` / `flush_queue`), `push_targets` / `push_one`, CLI `main`.
- `run`: new `push` command → `scribe.py push` (`--page|--session|--force|--flush-only`).
- `serve.py`: `import scribe`; `.push` button CSS + `PUSH_JS` + per-card
  "Send to Hermes" buttons (pages + sessions); `_do_push` handler routed under
  `POST /api/push/<kind>` (content-type + same-origin guard, id validation,
  `503` when `ECHO_SCRIBE_SECRET` unset).
- `.gitignore`: `.push-state.json`, `test-inbox/`.
- `scripts/test-push-receiver.py` (mock Hermes receiver: HMAC-verify, 1MB cap,
  chunk reassembly, writes `<root>/<date>/<note_id>/{note.md,page-NN.svg,
  audio.aac,manifest.json}`, dedup by dir) and `scripts/test-push-sender.py`
  (acceptance driver).

### Acceptance (all passing via the mock receiver)

- [x] Signature unit test vs `secret "s" / ts 1700000000 / body {"a":1}` →
      `ab7bd212…92a2` (also verified for the raw-bytes path).
- [x] Small page note → 200; files under `<root>/<date>/<note_id>/` as
      `note.md`, `page-01.svg`, `manifest.json`.
- [x] ~3 MB note (forces chunking, 7 slices) → all slices 200; reassembled
      `audio.aac` sha256 == source.
- [x] Same `note_id` sent twice → one directory.
- [x] Receiver offline → note parked on disk; flush drains it when reachable.
- [x] Real status codes + `find` output captured in the test run.

---

## Feature C — Pages clipped to the fixed page rectangle (bug)

**Goal.** Render each page to the bounds of its actual ink instead of the
nominal Livescribe page rectangle, so notes written past the page edge are
not cut off. The SVG and thumbnail should both be sized to the writing
(plus a small padding), not to `PAGE_W × PAGE_H`.

### Current state / root cause

`src/convert.py` renders every page against a fixed rectangle:

- `PAGE_W, PAGE_H = 4963, 6278` (`src/convert.py:38`).
- `render_svg()` hardcodes `viewBox="0 0 4963 6278"` and a full-page
  background rect (`src/convert.py:162-180`).
- `render_thumb()` → `render_png()` defaults `bounds=(0, 0, PAGE_W, PAGE_H)`
  (`src/convert.py:183-219`).
- `dot_grid()` only walks the nominal page (`src/convert.py:154-159`).

Any stroke outside that rectangle is clipped. This is real on the current
pen data:

| page | ink x range | ink y range | clipped? |
|---|---|---|---|
| `2193.1.47.79` | 1359 → **6249** | 1034 → 2015 | right edge (page max 4963) |
| `2208.0.26.96` | 368 → 5176 | 898 → **6595** | right + bottom (page max 6278) |
| `2208.0.30.37` | 5354 → 5532 | 6672 → **6958** | bottom |

The pen is not losing the ink — the renderer is cropping it to a page size
the writing exceeds.

### Design

1. **`ink_bounds(strokes, padding=...)`** (new helper near `stroke_bounds`,
   `src/convert.py:222`): flatten all points and return
   `(minx-pad, miny-pad, maxx+pad, maxy+pad)`. Empty page falls back to
   `(0, 0, PAGE_W, PAGE_H)`. Padding ~`GRID` (83) or `120` STF units so
   stroke caps/round joins are not flush against the edge.

2. **Minimum canvas (decision: yes).** A page with a single dot would
   otherwise zoom enormously. Enforce a minimum width/height (e.g. ~600
   STF units, roughly 2 grid cells) by expanding the bounds symmetrically
   around the ink centre. This keeps tiny marks readable without turning
   them into giant blobs.

3. **`dot_grid(bounds=None)`** (`src/convert.py:154`): iterate grid points
   from `floor(minx/GRID)*GRID` to `maxx` (same for y), keeping the global
   grid origin so the paper dots stay aligned with the original page.

4. **`render_svg(strokes, path, bounds=None)`** (`src/convert.py:162`):
   default `bounds = ink_bounds(strokes)`; use `viewBox="minx miny w h"`,
   draw the background rect at those bounds, and draw the grid over those
   bounds. Keep `width="100%" height="100%"` so the card scales it.

5. **`render_thumb()`** (`src/convert.py:218`): compute `ink_bounds(strokes)`
   and pass it to `render_png(..., bounds=bounds)`. `render_png` already
   accepts `bounds`; make its grid loop use the same bounds.

6. **Invalidate old renders.** Add `RENDER_VERSION = 2`, store it in
   `.convert-state.json`, and include it in the `reuse` check at
   `src/convert.py:496`. Without this, unchanged `.stf` files keep serving
   the old clipped SVGs until someone runs `--force`; with it, the fix
   applies on the next normal `./run` and then goes back to incremental.

7. **Optional — OCR path.** The Tesseract whole-page path (`ocr_page`,
   `src/convert.py:377`) also renders with full-page bounds, so off-page
   ink is clipped before OCR. Passing `ink_bounds` there would improve
   recognition. TrOCR is unaffected (it already crops per line via
   `render_line_png`). If changed, bump the OCR settings/version so text is
   regenerated.

8. **Optional — index metadata.** Store
   `"bounds": [minx, miny, maxx, maxy]` in each page entry for debugging;
   harmless.

`src/serve.py` needs no change: it already displays the SVG/thumbnail at
`width: 100%`, so a tighter `viewBox` simply fills the card.

### Acceptance checklist

- [ ] Every page's SVG `viewBox` contains all stroke coordinates.
- [ ] Thumbnails show the same ink as the SVG (no clipping).
- [ ] `2193.1.47.79`, `2208.0.26.96`, and `2208.0.30.37` render fully.
- [ ] A normal in-bounds page still looks like a page (grid + padding, not
      a degenerate crop).
- [ ] A plain `./run` (no `--force`) re-renders old pages once, then is
      incremental again.
- [ ] Empty/1-stroke pages don't produce absurd zoom (minimum canvas
      guard).
