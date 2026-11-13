# Livescribe Echo Notebook

Sync a Livescribe Echo smartpen on a WSL2 (Windows) machine and host the
written notes as a website.

The pen is detected via `usbipd` (USB/IP), bridged into WSL2, and its
contents are pulled over OBEX with a small C tool (`dumpscribe`, vendored
from the [CounterCultureLabs/dumpscribe](https://github.com/CounterCultureLabs/dumpscribe)
project, MIT license). A Python pipeline renders each written page to SVG
(+ PNG thumbnails), optionally transcribes pages with OCR (TrOCR for
handwriting, Tesseract as a lightweight fallback), and a stdlib-only HTTP
server publishes them with full-text page search.

## Handwriting recognition training

How to measure and improve the OCR against pages you know the text of:
[`training/README.md`](training/README.md). Samples and ground truth stay
in the gitignored `training/private/`; notes never go in this repository.

## Quick start

First time on a machine (WSL Ubuntu):

```sh
./run setup --ocr --stt   # system packages (asks first), .venv, OCR/STT models
./run doctor              # every line should be ✔ or an explained !
```

Every day:

```sh
# 1. pen must be physically connected (data cable) and attached into WSL:
#    run scripts/windows-connect.ps1 in an ADMIN PowerShell on Windows,
#    then confirm:  lsusb   ->  1cfb:1032 Livescribe Echo(TM) Smartpen

# 2. download notes from the pen and render pages:
./run sync

# 3. start the website (it serves output/ on port 8000):
./run
#    open http://localhost:8000 and use the search box
```

`./run` converts (incrementally: unchanged pages are skipped) and then
serves the site in the foreground; press Ctrl+C to stop. Re-run
`./run sync` any time you have unsynced notes, then refresh the page.
`./run convert` only renders, and `./run ocr` runs the OCR pass. Extra server
options go after the command: `./run serve --port 8010`. `./run help`
lists every command.

## Architecture

```text
                     pen (1cfb:1032)
                            │  micro-USB data cable
   Windows  ────────────────┘   usbipd-win service
   WSL2     usbip vhci ── /dev/bus/usb ── vendor/dumpscribe  (OBEX download)
                │                      │
                │                 data/   raw pen dump (.stf, audio, pages)
                │                      │
                │                 src/convert.py   (.stf -> SVG + thumbnails)
                │                      │
                │                 output/   index.json, pages/, audio/
                │                      │
                └────── src/serve.py ──┘  (HTTP server)
```

## Checking a machine

`./run doctor` checks everything this README assumes and prints one fix
per problem:

- Python and the `.venv`
- pycairo
- the OCR and STT engines
- the pen client build
- whether the pen is attached
- the library and incremental state
- the run lock
- push configuration
- whether logs are writable

Run it first whenever something seems off.

## First-time setup (one-time per machine)

1. **Windows**: install [usbipd-win](https://github.com/dorssel/usbipd-win).
   Bind and attach need an admin PowerShell.

2. **WSL**: `./run setup` does the rest and is safe to re-run:
   - lists missing apt packages and asks before installing them (the
     build tools for the pen client, `python3-cairo`, `python3-venv`,
     `usbutils`, and `tesseract-ocr` with `--ocr`)
   - creates `.venv` with `--system-site-packages`, so it uses the system
     pycairo
   - with `--ocr` / `--stt`, installs CPU PyTorch and `requirements.txt`
     (TrOCR and Moonshine; the models download on first use, ~1.3 GB)
   - builds the pen client (`vendor/dumpscribe`)
   - finishes with `./run doctor`

   The manual equivalent, if you prefer:

   ```sh
   sudo apt install build-essential pkg-config libglib2.0-dev libusb-1.0-0-dev \
        libopenobex2-dev libarchive-dev libxml2-dev libbluetooth-dev python3-cairo
   ./scripts/build-dumpscribe.sh
   python3 -m venv --system-site-packages .venv
   .venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch
   .venv/bin/pip install -r requirements.txt
   ```

## Daily workflow

1. **Physically connect the pen** to the PC with a micro-USB *data*
   cable (a charge-only cable will not enumerate). Pen should be on.

2. **Bridge into WSL** — in an *admin* PowerShell on Windows:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts/windows-connect.ps1
   ```
   Confirm inside WSL:
   ```sh
   lsusb        # -> 1cfb:1032 Livescribe Echo(TM) Smartpen
   ```

3. **Sync + render**:
   ```sh
   ./run sync
   ```

4. **Serve the site**:
   ```sh
   ./run
   # open http://localhost:8000
   ```
   (or combine both: `./run sync serve`)

## OCR and search

OCR is optional. `--ocr-engine auto` (the default) prefers TrOCR when the
project `.venv` provides it, then falls back to `tesseract`. Override with
`--ocr-engine tesseract` or `--ocr-engine trocr`.

- TrOCR groups ink strokes into text lines, renders each line to
  `output/pages/<page_id>/ocr-line-N.png` (very long lines are split at
  word gaps into `ocr-line-N-M.png`), and recognizes each one. Tesseract
  renders the inked area to `ocr.png` instead. Either way, the result is
  written to `output/pages/<page_id>/text.txt` and to the page's `text` in
  `output/index.json`.
- The web UI shows a "Transcription" disclosure and a "Download .txt"
  button per page, plus a search box that filters pages by id or text.

**Profiles.** `--ocr-profile` (or `ECHO_OCR_PROFILE`) chooses how lines are
segmented and decoded:

- `legacy` is the original behaviour.
- `beam` adds beam search that blocks the `to-to-to…` repetition loops,
  splits very long lines at word gaps, and stops circles and margin
  lines from making lines of their own.
- `default` adds four changes on top of `beam`:
  - draws each line the way TrOCR's training lines look
  - finds line breaks from the order the pen wrote
  - reads each line three ways and keeps the most confident reading
  - prefers real words among near-tied readings (system word list plus
    the private `training/private/vocabulary.txt`)

The profile's settings are part of each page's OCR record, so after
changing it, `./run ocr` re-transcribes once. The table in
[`training/README.md`](training/README.md) lists every setting.

**Measuring accuracy.** `./run ocr-eval` scores the OCR against pages whose
text you know: character and word error rates per sample. Compare
`--profile legacy` with `--profile default` before trusting a change.
Samples and ground truth live in the gitignored `training/private/`; see
[`training/README.md`](training/README.md).

`TROCR_MODEL=microsoft/trocr-large-handwritten` swaps in the larger, slower
model. Use `--ocr-lang eng+deu` (etc.) with Tesseract for other languages.

## Incremental conversion and deletions

`./run convert` keeps an incremental state file (`.convert-state.json`,
gitignored):

- Source files unchanged since the last run are skipped.
- Output whose source is gone from `data/` is pruned, so deleting a page
  directory or audio file from `data/` makes it disappear from the site
  on the next convert.
- `--force` re-renders everything but keeps transcriptions, transcripts
  and capture times of unchanged ink.
- The state is saved after each page OCR'd and each recording
  transcribed, so an interrupted run keeps its finished work.
- A corrupt or truncated page file is skipped with a warning, and keeps
  its last good render.

Note: the pen still re-downloads everything on `./run sync`, because the
vendored dumpscribe has no per-asset skip. So removing a page from `data/`
only sticks until the next real sync re-adds it, unless the pen is cleared
afterwards. `./run sync` offers to clear the pen, and `./run clean-pen`
does it; both refuse unless the download saved **both** pages and audio.

## Push notes to Hermes

`./run push` sends your notes to your Hermes agent's webhook
(`http://host:8644/webhooks/echo-scribe`), HMAC-signed with the Generic V2
scheme. Each page and each recording is one note:

- **page note** — `note_id` = page id, `svg_pages` = the rendered SVG, `text` = OCR.
- **recording note** — `note_id` = `PRS-…`, `.aac` audio (`audio_base64`), `transcription`,
  plus the linked pages' SVGs.

```sh
export ECHO_SCRIBE_SECRET="<secret from /opt/data/inbox/echo-scribe/WEBHOOK.md>"

./run push                       # send every new/changed note
./run push --page 2193.1.47.79   # send one page
./run push --session PRS-5197a597
./run push --force               # re-send even unchanged notes
./run push --flush-only          # just drain any queued notes
```

`./run push` exits 2 when `ECHO_SCRIBE_SECRET` is missing and 1 when some
notes were parked, so scripts notice either.

Push is rsync-style: `.push-state.json` records a content signature per
`note_id`, so notes you've already sent unchanged are skipped. Unreachable
notes are parked on disk (`output/push-queue/`, gitignored) and re-sent on a
later push; they never block conversion/transcription. Oversized notes are
chunked under the webhook's 1 MB limit automatically.

The web UI also has a "Send to Hermes" button on every page and recording
card (same signing/dedup path). Config is via environment:

| Variable | Use |
|---|---|
| `ECHO_SCRIBE_SECRET` | required HMAC signing secret |
| `ECHO_SCRIBE_URL` | webhook URL (default `http://127.0.0.1:8644/webhooks/echo-scribe`) |
| `ECHO_TZ` / `ECHO_UTC_OFFSET` | zone / offset used for `recorded_at` (fallback UTC) |

## Project layout

```text
data/      raw output of dumpscribe              (gitignored)
output/    generated site content                (gitignored)
logs/      structured logs, debug reports        (gitignored)
scripts/   windows-connect.ps1, build-dumpscribe.sh, push test harness
src/       convert.py (pipeline), serve.py (web server + API), webpage.py
           + web/ (the page: markup, style.css, app.js), ocr.py + ocr_eval.py,
           stt.py, scribe.py (Hermes push), telemetry.py (logs),
           doctor.py (checks, debug report), paths.py (ECHO_ROOT)
tests/     stdlib unittest suite + synthetic pen data (./run test)
training/  how to measure OCR; private/ holds samples (gitignored)
vendor/    dumpscribe.c + parsestf.py (MIT, from CounterCultureLabs)
.agents/   agent skills: add-feature, debug
run        the one entry point (./run help)
requirements-core.txt / requirements.txt   core / OCR+STT dep lists
future_features/   written specs for planned-but-not-built features
```

## Troubleshooting

Start with `./run doctor`. Then:

- `./run logs --errors` shows recent warnings and errors.
- `./run logs --run last` shows everything from the latest command.
- `./run debug-report` writes a shareable summary with no note content.

Agents can use the `debug` skill (`.agents/skills/debug/`).

- **`lsusb` empty in WSL**: the pen is not attached. Re-run the
  `windows-connect.ps1` step (it now reports bind/attach failures), and
  check that the pen appears in `usbipd list` first (admin PowerShell). If
  you aren't sure about the cable, try another data cable.
- **`usbipd bind` fails with a USBPcap warning**: use `--force`
  (the script already does).
- **`sudo` prompts in `./run sync`**: that is expected. The pen client
  needs read/write access to `/dev/bus/usb`, and the dataset is chowned
  back to you even when the download fails.
- **"Another convert is running"**: a convert or the web server is busy.
  `./run doctor` shows the pid. A crashed run never leaves a stale lock.
- **Pencil lead icon / HID mouse moves**: harmless; the pen's low-level
  HID interface is unused by this pipeline.

## Data formats (for future work)

- `.stf` — Anoto Stroke Format: handwritten strokes (x, y, force, time),
  parsed by `vendor/parsestf.py`.
- `page.gfx` / `page.areas` — Livescribe vector page templates
  (not yet rendered; a generated dot grid stands in).
- Paper Replay sessions — `audio-0.aac` + `session.info` + `session.pages`
  (the latter links recordings to page identifiers).