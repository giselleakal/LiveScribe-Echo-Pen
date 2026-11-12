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

## Quick start

If everything is already set up (see "Current machine state"):

```sh
# 1. pen must be physically connected (data cable) and attached into WSL:
#    run scripts/windows-connect.ps1 in an ADMIN PowerShell on Windows,
#    then confirm:  lsusb   ->  1cfb:1032 Livescribe Echo(TM) Smartpen

# 2. download notes from the pen and render pages (+ OCR if installed):
./run sync

# 3. start the website (it serves output/ on port 8000):
./run
#    open http://localhost:8000 and use the search box
```

`./run` converts (incrementally — unchanged pages are skipped) and then
serves the site in the foreground; press Ctrl+C to stop. Re-run
`./run sync` anytime you have unsynced notes, then refresh the page;
`./run convert` only renders, `./run ocr` forces the OCR pass again.
Extra server options go after the command: `./run serve --port 8010`.

## Architecture

```
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

## Current machine state

Everything on this machine is already installed and verified working:

- Windows: `usbipd-win` service running (the bridge).
- WSL2 kernel: `vhci_hcd` loaded, `usbip` tools present.
- Ubuntu packages: `obexftp`, `libopenobex2-dev`, `build-essential`,
  `pkg-config`, `libglib2.0-dev`, `libusb-1.0-0-dev`, `libarchive-dev`,
  `libxml2-dev`, `libbluetooth-dev`, `python3-cairo`.
- The pen has been synced once: `data/` holds 7 written pages (.stf),
  Paper Replay audio, and page templates; `output/` holds rendered pages.
- `python3-cairo` renderer deps are installed. Start the site with
  `./run` (converts, then binds localhost:8000; pass
  `./run serve --host 0.0.0.0` to expose on the LAN).
- OCR is ready: `tesseract` 5.3.4 is available via
  `~/.local/bin/tesseract`, and `.venv` has TrOCR (CPU PyTorch +
  transformers) with the `trocr-base-handwritten` model cached in
  `~/.cache/huggingface`. `./run` uses TrOCR automatically.

## First-time setup (one-time per machine)

1. **Windows**: install [usbipd-win](https://github.com/dorssel/usbipd-win)
   (already done here). Admin PowerShell is needed for bind/attach.

2. **WSL**: install build/runtime deps:
   ```sh
   sudo apt install build-essential pkg-config libglib2.0-dev libusb-1.0-0-dev \
        libopenobex2-dev libarchive-dev libxml2-dev libbluetooth-dev python3-cairo
   ```

3. **Build the pen client**:
   ```sh
   ./scripts/build-dumpscribe.sh
   ```

4. **Optional — install OCR** (for page transcriptions and search):

   Tesseract is small but poor at cursive handwriting:
   ```sh
   sudo apt install tesseract-ocr
   ```

   For accurate handwriting, install the TrOCR backend in a project venv
   (`--system-site-packages` keeps the system `pycairo`):
   ```sh
   python3 -m venv --system-site-packages .venv
   .venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch
   .venv/bin/pip install -r requirements.txt
   ```
   The model downloads on first use (~1.3 GB). `./run` prefers the venv
   automatically when it exists. To re-run just the conversion:
   ```sh
   .venv/bin/python src/convert.py --ocr
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
  `output/pages/<page_id>/ocr-line-N.png`, and recognizes it. Tesseract
  instead renders the whole page to `ocr.png`. Either way, the result is
  written to `output/pages/<page_id>/text.txt`.
- Recognized text is stored under the page's `text` key in
  `output/index.json`.
- The web UI shows a "Transcription" disclosure and a "Download .txt"
  button per page, plus a search box that filters pages by id or text.

Handwriting accuracy varies; the TrOCR backend handles cursive much
better than Tesseract. Use `--ocr-lang eng+deu` (etc.) with Tesseract to
recognize other languages.

## Incremental conversion and deletions

`./run convert` keeps an incremental state file (`.convert-state.json`,
gitignored): source files unchanged since the last run are skipped, and
output whose source is gone from `data/` is pruned. Deleting a page
directory or audio file from `data/` therefore makes it disappear from
the site on the next convert.

Note: the pen itself still re-downloads everything on `./run sync` (the
vendored dumpscribe has no per-asset skip), so removing a page from
`data/` only sticks until the next real pen sync re-adds it. Deleting
assets from the pen hardware is a `dumpscribe -c` bulk operation for
whole notebooks, not per page/session (see `future_features/` for where
this is headed).

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

```
data/      raw output of dumpscribe  (gitignored)
output/    generated site content    (gitignored)
scripts/   windows-connect.ps1, build-dumpscribe.sh
src/       convert.py, ocr.py, serve.py (project code)
vendor/    dumpscribe.c + parsestf.py (MIT, from CounterCultureLabs)
run        one-shot entry point: convert + serve (./run, ./run sync)
requirements-core.txt / requirements.txt   core / OCR dep lists
.venv/     optional TrOCR backend + CPU PyTorch (gitignored)
future_features/   written specs for planned-but-not-built features
```

## Troubleshooting

- **`lsusb` empty in WSL**: the pen is not attached. Re-run the
  `windows-connect.ps1` step; verify it appears in `usbipd list` first
  (admin PowerShell). If the cable is unknown, try another data cable.
- **`usbipd bind` fails with a USBPcap warning**: use `--force`
  (the script already does).
- **`sudo` prompts in `./run sync`**: that is expected — the pen client needs
  read/write access to `/dev/bus/usb`. The dataset is chowned back to you.
- **Pencil lead icon / HID mouse moves**: harmless; the pen's low-level
  HID interface is unused by this pipeline.

## Data formats (for future work)

- `.stf` — Anoto Stroke Format: handwritten strokes (x, y, force, time),
  parsed by `vendor/parsestf.py`.
- `page.gfx` / `page.areas` — Livescribe vector page templates
  (not yet rendered; a generated dot grid stands in).
- Paper Replay sessions — `audio-0.aac` + `session.info` + `session.pages`
  (the latter links recordings to page identifiers).