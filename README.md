# Livescribe Echo Notebook

Sync a Livescribe Echo smartpen on a WSL2 (Windows) machine and host the
written notes as a website.

The pen is detected via `usbipd` (USB/IP), bridged into WSL2, and its
contents are pulled over OBEX with a small C tool (`dumpscribe`, vendored
from the [CounterCultureLabs/dumpscribe](https://github.com/CounterCultureLabs/dumpscribe)
project, MIT license). A Python pipeline renders each written page to SVG
(+ PNG thumbnails) and a stdlib-only HTTP server publishes them.

## Quick start

If everything is already set up (see "Current machine state"):

```sh
# 1. pen must be physically connected (data cable) and attached into WSL:
#    run scripts/windows-connect.ps1 in an ADMIN PowerShell on Windows,
#    then confirm:  lsusb   ->  1cfb:1032 Livescribe Echo(TM) Smartpen

# 2. download notes from the pen and render pages:
./scripts/sync.sh

# 3. start the website (it serves output/ on port 8000):
python3 src/serve.py --port 8000
#    open http://localhost:8000
```

The server stays in the foreground; press Ctrl+C to stop. If you have
unsynced notes, run step 2 again anytime and refresh the page.

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
  `python3 src/serve.py --port 8000` (binds localhost by default; pass
  `--host 0.0.0.0` to expose on the LAN).

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
   ./scripts/sync.sh
   ```

4. **Serve the site**:
   ```sh
   python3 src/serve.py --port 8000
   # open http://localhost:8000
   ```

## Project layout

```
data/      raw output of dumpscribe  (gitignored)
output/    generated site content    (gitignored)
scripts/   windows-connect.ps1, sync.sh, build-dumpscribe.sh
src/       convert.py, serve.py      (project code)
vendor/    dumpscribe.c + parsestf.py (MIT, from CounterCultureLabs)
```

## Troubleshooting

- **`lsusb` empty in WSL**: the pen is not attached. Re-run the
  `windows-connect.ps1` step; verify it appears in `usbipd list` first
  (admin PowerShell). If the cable is unknown, try another data cable.
- **`usbipd bind` fails with a USBPcap warning**: use `--force`
  (the script already does).
- **`sudo` prompts in `sync.sh`**: that is expected — the pen client needs
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