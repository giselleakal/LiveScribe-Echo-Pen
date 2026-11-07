# Future features (not yet implemented)

Written-up specs for the two planned features, gathered so they can be
implemented without re-deriving the research. Each section is written to
be actionable: decisions are made, the file/line touchpoints are listed,
and acceptance criteria are spelled out.

The current milestone (all P0/P1/P2 bug fixes + the `./run` entry point)
was completed first; these are the next two features.

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
