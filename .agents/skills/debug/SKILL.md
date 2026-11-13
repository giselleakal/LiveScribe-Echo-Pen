---
name: debug
description: Walk a person through debugging the Livescribe Echo pipeline, from the cheapest fail-fast checks (cable, pen attached, ./run doctor) through structured logs and state files to isolating one component and writing a deterministic reproduction. Use when sync, convert, the web page, OCR, speech-to-text or Hermes push misbehaves, or someone says "it's broken" or "nothing works".
---

# Debug the Echo pipeline

Climb the ladder one rung at a time, **cheapest first**. Most problems
here are a cable, a missing attach, or a missing package, and they are
found in the first minute. At each rung: run the check, say what it means
in one sentence, and either fix it or climb. Ask the person one question at
a time, and never guess past a rung you skipped.

```text
L0 physical ─► L1 doctor ─► L2 logs ─► L3 state ─► L4 isolate ─► L5 reproduce ─► L6 deep
   seconds        seconds      a minute    minutes      minutes        an hour        hours
```

Rules while debugging (from `AGENTS.md`):

- Never paste note content (page text, transcripts, titles) into an
  issue, PR, commit or chat log outside this machine. `./run debug-report`
  is safe to share. Raw `data/` and `output/` are not.
- Never run `./run clean-pen` or delete `data/` to "start fresh". The pen
  may hold the only copy.
- Experiment in a sandbox: `ECHO_ROOT=$(mktemp -d)` points every module
  at an empty data root.

## First: what exactly is wrong?

Get the symptom in one sentence, plus the exact command and its output:

- "What did you run, and what did you expect to happen?"
- "What happened instead? Please paste the last lines of output."

Then jump to the rung that matches the symptom:

| Symptom | Start at |
|---|---|
| `./run sync` says the pen is not attached; `lsusb` shows no pen | L0 |
| `ModuleNotFoundError`, "not available", Transcribe does nothing | L1 |
| a page, recording or transcript is missing or wrong | L2 |
| "Another convert is running"; nothing re-renders; stale page | L3 |
| the web page errors, or a button fails | L2, then L4 |
| OCR text is poor | L4 (OCR), then `./run ocr-eval` |
| Hermes push fails or notes are "parked" | L2, then L4 (push) |

## L0: physical (seconds)

1. Is the pen **on**, in its cradle or plugged in, with a **data** cable?
   Charge-only cables never enumerate.
2. On Windows, in an **admin** PowerShell: run
   `scripts/windows-connect.ps1`. It now stops with a clear message if
   `usbipd` bind or attach fails.
3. In WSL: `lsusb` must show `1cfb:1032 Livescribe Echo(TM) Smartpen`.
   If it doesn't, the problem is the cable or the bridge, not the code.
4. The `sudo` password prompt during sync is expected.

## L1: `./run doctor` (seconds)

Run `./run doctor`. Every `!` or `✘` line has a `fix:`. Apply the fix,
re-run doctor, and only climb once the line relevant to the symptom shows
`✔`. Common ones:

| Line | Meaning | Fix |
|---|---|---|
| `venv` ✘ | `.venv` can't see the system pycairo | `rm -rf .venv && ./run setup` |
| `ocr` ! | no OCR engine installed | `./run setup --ocr` |
| `stt` ! | no speech model | `./run setup --stt` |
| `dumpscribe` ! | pen client not built, or older than its source | the apt line shown, then `./run sync` |
| `lock` ! | another convert or server is running | wait, or stop that process |
| `push` ! | no `ECHO_SCRIBE_SECRET` | add it to `.env` |

## L2: structured logs (a minute)

Every `./run` invocation has a `run_id`; every web request has a
`request_id`; every boundary call (tesseract, TrOCR, ffmpeg, Moonshine,
Hermes, dumpscribe, convert subprocess) writes one `io` record with
`outcome`, `status`, `duration_ms` and a short `stderr_tail`.

```sh
./run logs --errors                 # recent warnings and errors
./run logs --run last               # everything from the latest run
./run logs --run last --json | jq 'select(.event == "io")'
./run logs -n 200 --json | jq 'select(.request_id == "<id>")'
```

What to look for:

- `convert.failed` with `exc_type` and `stack`: the run crashed. Start from
  the stack.
- `page.failed`: one source file is corrupt. The page keeps its last
  render. Note the `source` path.
- `io` with `outcome != "ok"`: which external tool failed, and its
  `stderr_tail`.
- `lock.busy` / `lock.wait`: two runs collided. `holder_pid` says who.
- `push.done` with `parked_classes`: `config` means a bad secret or clock
  skew; `size` means the note is too large; `net` means Hermes is
  unreachable.
- `api.refused`: the web API said no, and `reason` says why. A 415 or 403
  from the browser means the request did not come from the page itself.

Set `ECHO_LOG_LEVEL=DEBUG` and re-run to get per-page and per-request
detail.

What each file on disk holds, and every log event, is listed in
`docs/architecture.md`.

## L3: state files (minutes)

The pipeline is incremental. "Nothing changes" usually means the state
thinks nothing changed.

| File | Holds | Safe reset |
|---|---|---|
| `.convert-state.json` | source signatures, page/session entries, OCR settings | `./run convert --force` (keeps transcriptions) |
| `.convert.lock` | holder pid; an `flock`, so a leftover file is not a held lock | none needed (`./run doctor` shows held vs leftover) |
| `.push-state.json` | which notes Hermes accepted | `./run push --force` re-sends |
| `output/push-queue/` | notes waiting to be sent | `./run push --flush-only` |
| `output/index.json` | what the web page shows | `./run convert` rebuilds it |

Check counts without exposing content:

```sh
jq '{render_version, pages: (.page_entries|length), sessions: (.session_entries|length)}' .convert-state.json
```

## L4: isolate one component (minutes)

Run each stage alone, so the failure points at one module:

| Stage | Command | Healthy result |
|---|---|---|
| download | `./run sync` | `[ECHO] audio download ok` and `[ECHO] pages download ok` |
| render | `./run convert` | `rendered N pages (...)` |
| one page OCR | `./run ocr --page <id>` | `ocr.page` record at DEBUG; text in the page card |
| OCR quality | `./run ocr-eval --profile default` vs `--profile legacy` | lower CER is better |
| speech | `./run stt --session PRS-…` | `stt.session` record with `chars > 0` |
| web server | `./run serve`, then `curl -s localhost:8000/ \| head` | HTML, not an error |
| push | `ECHO_SCRIBE_SECRET=x python3 scripts/test-push-sender.py` | `ALL SENDER TESTS PASSED` (sandboxed, no real data) |

To test against a copy rather than the real library:

```sh
SANDBOX=$(mktemp -d); cp -r data "$SANDBOX/"
ECHO_ROOT="$SANDBOX" ./run convert --force
```

## L5: deterministic reproduction (an hour)

Turn the bug into a failing test before fixing it:

1. Build the input with `tests/fixtures.py`:
   - `write_page(root, id, strokes)` for a page, or `raw=b"..."` for a
     corrupt file
   - `write_session(...)` for a recording
   - `text_line(...)` for handwriting-like strokes

   Never use real pages.
2. Run the code path:
   - convert: in-process `convert.main([...])` (see
     `tests/test_convert_state.py`), or in a sandbox root with
     `fixtures.run_convert(root, ...)`
   - API: the in-process server in `tests/test_serve_api.py`
   - `run` script: the stubbed `lsusb`/`sudo`/`dumpscribe` in
     `tests/test_run_script.py`
3. Watch it fail for the reason you expect: `./run test` (or
   `python3 -m unittest tests.test_<file>`).
4. Fix, watch it pass, run the whole suite, and commit test and fix
   together.

## L6: deep (hours)

Only here once the rungs above point at the formats or the pen itself:

- **`.stf` strokes** (`vendor/parsestf.py`): bit-level Huffman-coded
  stream. A truncated file raises `EOFError` and keeps its complete
  strokes. `python3 vendor/parsestf.py <file.stf>` prints points.
- **`session.pages` / `session.info`**: binary headers decoded by
  `parse_session_pages` / `parse_session_info` in `src/convert.py`. Wrong
  capture times usually mean `data/time_offset` (the pen clock); see
  `paper/README.md` for the clock-fix sheet.
- **OBEX / USB** (`vendor/dumpscribe.c`): rebuild with
  `./scripts/build-dumpscribe.sh`, then run `sudo vendor/dumpscribe data`
  by hand to see its raw output.

## Finish

Tell the person, in a short table: the symptom, the cause, the fix, and
how it was verified. If the cause is code, the fix comes with the failing
test from L5. If the problem was environmental (cable, package, secret),
say which doctor line or log record would have shown it fastest.
