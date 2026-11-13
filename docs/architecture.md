# Architecture

How the pipeline fits together, what every file on disk holds, and the
contracts between the parts. Rules for changing them are in
[`AGENTS.md`](../AGENTS.md); how to test them is in
[`testing.md`](testing.md).

## Data flow

```text
pen ──usbipd──► vendor/dumpscribe (sudo, OBEX) ──► data/            raw dump
                                                     │
                     src/convert.py  (incremental, holds the run lock)
                       ├─ pages:    .stf ─► page.svg, thumb.png, [OCR: text.txt]
                       ├─ sessions: audio-N.aac ─► output/audio/, [STT: .txt]
                       └─ writes    output/index.json + .convert-state.json
                                                     │
src/serve.py ◄── browser   GET /  (page built from index.json)
     │                     POST /api/*  (delete, rename, title, transcribe, push)
     ├─ mutations take the run lock, then re-run convert.py as a child
     └─ push ─► src/scribe.py ─► Hermes webhook (HMAC-signed, queued on failure)
```

`./run` is the only entry point; it mints the `run_id` every log record
carries and starts the processes above.

## Modules

| Module | Responsibility | Depends on |
|---|---|---|
| `run` | commands, pen download/retire safety, setup, help text | bash, `src/*.py` |
| `src/paths.py` | where data lives (`ECHO_ROOT`) and where code lives | stdlib |
| `src/convert.py` | the pipeline: render, OCR/STT bookkeeping, index, state, run lock | pycairo, `ocr`, `stt`, `vendor/parsestf.py` |
| `src/ocr.py` | recognizers (TrOCR, Tesseract) and OCR profiles | torch/transformers (lazy) |
| `src/ocr_eval.py` | scores OCR against ground truth (CER/WER) | `convert`, `ocr` |
| `src/stt.py` | speech-to-text (Moonshine) and audio decoding | torch/transformers, imageio-ffmpeg (lazy) |
| `src/serve.py` | HTTP server and guarded POST API | stdlib only, `convert`, `scribe`, `webpage` |
| `src/webpage.py` | builds the page from `index.json`, escaping every value | stdlib only |
| `src/web/` | `page.html` (skeleton), `style.css`, `app.js`, inlined per request | — |
| `src/scribe.py` | Hermes push: notes, signing, chunking, durable queue, dedup | stdlib |
| `src/telemetry.py` | structured logs, correlation ids, `./run logs` | stdlib |
| `src/doctor.py` | `./run doctor` checks and `./run debug-report` | stdlib |
| `vendor/` | upstream dumpscribe (C) and the STF parser, minimal `[ECHO]` edits | openobex, libusb, … |

## Files on disk

Everything under `data/`, `output/` and `logs/`, plus the dot-files, is
generated and gitignored.

| Path | Written by | Read by | Holds |
|---|---|---|---|
| `data/data/<page>/<n>/*.stf` | dumpscribe | convert | pen strokes of one page |
| `data/userdata/<pen>/Paper Replay/<n>/<notebook>/sessions/<PRS-id>/` | dumpscribe | convert, serve (delete) | `audio-N.aac`, `session.info` (start/end), `session.pages` (linked pages) |
| `data/time_offset` | dumpscribe | convert | ms from pen clock to real time |
| `output/index.json` | convert | serve, scribe | everything the page shows (below) |
| `output/pages/<page>/` | convert | browser | `page.svg`, `thumb.png`, `text.txt`, `ocr*.png` |
| `output/audio/` | convert | browser, scribe | `<PRS-id>.<n>.aac`, `<PRS-id>.txt` |
| `output/push-queue/<id>.json` | scribe | scribe | notes waiting for Hermes: `{kind, id, sig, request_id, payload}` |
| `.convert-state.json` | convert, serve (delete) | convert, doctor | incremental state (below) |
| `.convert.lock` | convert, serve | convert, serve, doctor | the run lock: an `flock` on this file; the file only records the last holder's pid |
| `.push-state.json` | scribe | scribe | `{notes: {id: {sig, request_id, accepted, pushed_at}}}` |
| `logs/echo.jsonl` (+ `.1`) | every module | `./run logs`, doctor | one JSON record per line; `./run` rotates it at 5 MB |
| `titles.json`, `notebook_names.json` | serve (UI) | convert | user-typed names; **tracked**, see AGENTS.md |
| `training/private/eval.json`, `vocabulary.txt` | the user | ocr-eval, ocr | OCR ground truth and private words; never committed |
| `.env` | the user | `run` | `ECHO_SCRIBE_SECRET`, `ECHO_TZ`, … (never committed) |

### `output/index.json`

```text
{ "pages": [ { id, strokes, bounds: [minx, miny, maxx, maxy], svg, thumb,
               captured, captured_end, pen_tmin, pen_tmax,   # when dated
               title,                                         # when titled
               text, ocr, ocr_engine, ocr_settings } ],       # when OCR'd
  "sessions": [ { id, notebook, notebook_name, title, audio, urls,
                  pages, captured, captured_end, transcript } ] }
```

Both lists are sorted by capture time, oldest first. `ocr` is true only
when every line was read; a partial result keeps its `text` with
`ocr: false` so the next OCR run retries it.

### `.convert-state.json`

| Key | Holds |
|---|---|
| `render_version` | `RENDER_VERSION` of the last run; a bump re-renders every page once |
| `sources` | source path → `{mtime_ns, size}`; unchanged sources are skipped |
| `page_entries` | page id → its last index entry (reused for unchanged pages) |
| `session_entries` | session id → `{urls, pages, notebook, captured…, stt, stt_sig, transcript}` |
| `ocr_settings` | legacy; always null now (settings live on each page) |

The state is saved after every OCR'd page and STT'd session, so an
interrupted run keeps its finished work.

## OCR profiles

A `Profile` in `src/ocr.py` bundles every choice that changes recognized
text:
- segmentation: `max_line_aspect`, `oversize_ratio`, `lines`
- rendering: `render`, `pen_widths`, `tesseract_on_ink`
- decoding: `beams`, `no_repeat_ngram`, `vocab_margin`

The full settings, serialized, are each page's OCR reuse key, so changing a
profile re-transcribes pages once.

```text
page strokes ─► lines (vertical bands | pen order) ─► wide lines split at word gaps
            ─► line images (plain | normalized × pen widths)
            ─► TrOCR n-best per image, scored ─► choose(): best score,
               near-ties go to fewer unknown words ─► text
```

`legacy` and `beam` are frozen for comparison; new ideas are new
settings, measured with `./run ocr-eval --set`.

## Web page

```text
GET /  ─► webpage.build_index()
            reads output/index.json (last good page if it is mid-write)
            cards: page_card() / session_card()  ─► string.Template(page.html)
                                                     $style  = web/style.css
                                                     $script = web/app.js
                                                     $body   = the cards
```

- `app.js` has a few shared helpers: `toast()`, `busy()`, `post()`, and
  `run()`, which wraps a button, a request and the result handling.
- Buttons are dispatched through the `ACTIONS` table (CSS selector →
  handler). A new button adds one row.
- The search filter and multi-select listen for the `echo:filtered`
  event.
- Layout contract: every element's size is known before images, audio or
  scripts load. `./run ui-check` measures that (layout shift) and takes
  the screenshots for PRs.

## HTTP API

Every POST passes one guard before its handler runs:
- Content-Type exactly `application/json`, otherwise 415.
- An Origin matching the Host, otherwise 403.
- A loopback Host name on a loopback server, otherwise 403.
- Loopback-only unless `ECHO_ALLOW_LAN_DELETES=1`, otherwise 403.
- A JSON object body of 1 B–1 MiB, otherwise 400 or 413.

| Endpoint | Body | Does | Success / notable errors |
|---|---|---|---|
| `POST /api/delete/page`, `/api/delete/session` | `{id}` | removes raw data, output and state, then reconciles | 200 / 404 unknown id |
| `POST /api/delete` | `{items: [{kind, id}], confirm?}` | batch delete; more than 5 items need `confirm: "DELETE"` | 200 `{deleted, missing}` / 400 |
| `POST /api/rename` | `{id: notebook guid, name}` | sets or clears a notebook name | 200 |
| `POST /api/title` | `{id: page or PRS-id, name}` | sets or clears a title | 200 |
| `POST /api/transcribe/page`, `/session` | `{id}` | runs `convert.py --ocr --page` / `--stt --session` | 200 / 404 / 500 |
| `POST /api/push/page`, `/session` | `{id}` | sends one note to Hermes | 200 / 502 rejected / 503 no secret |

Every mutation holds the run lock; a running convert makes it a 409
"busy". Errors are `{ok: false, error}`.

## Logs

Records carry `ts`, `level`, `component`, `event`, `run_id`, `pid` and,
in serve, `request_id`. Note text is never logged, only lengths.

| Event | Component | Meaning |
|---|---|---|
| `convert.start` / `convert.done` / `convert.failed` | convert | one run: options; counts and `phase_ms`; crash with stack (Ctrl-C is WARNING) |
| `page.failed`, `stf.truncated` | convert | a source file is corrupt or cut short |
| `ocr.page`, `ocr.line_failed`, `ocr.page_failed` | convert | OCR per page (DEBUG) and its failures |
| `ocr.engine_fallback`, `ocr.unavailable`, `stt.unavailable` | convert | missing engines |
| `ocr.unknown_profile`, `ocr.scores_unavailable` | ocr | bad `ECHO_OCR_PROFILE`; transformers too old to score candidates |
| `stt.session`, `stt.session_failed` | convert | speech-to-text per recording |
| `audio.copy_failed`, `session.missing_page` | convert | session problems |
| `lock.wait`, `lock.busy` | convert | two runs collided; `holder_pid` |
| `io` | all | one record per boundary call: `target` (tesseract, trocr, ffmpeg, moonshine, hermes, convert, dumpscribe), `outcome`, `status`, `duration_ms` |
| `http.request` | serve | one per request (POST at INFO, GET at DEBUG) |
| `api.refused` / `api.crashed` | serve | an API said no (with `reason`) / a handler raised |
| `push.done` | push | `sent`, `parked`, `parked_classes` |
| `setup.done`, `serve.start`, `ocr_eval.done` | run, serve, ocr-eval | lifecycle |
