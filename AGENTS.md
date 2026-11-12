# AGENTS.md

Personal WSL2 pipeline for a Livescribe Echo smartpen: pull notes from the
pen (vendored C tool), render pages to SVG + OCR, serve a static site.
No tests, no CI, no lint config — verification is running the pipeline.

## Entry point

- `run` is the only entry point; don't invoke `src/*.py` or `vendor/dumpscribe` directly unless debugging:
  - `./run` — incremental convert + serve on http://127.0.0.1:8000 (foreground)
  - `./run convert` / `./run ocr` — render only (ocr forces the OCR pass)
  - `./run stt` — transcribe session audio (Moonshine) explicitly; pass
    `--session PRS-…` to do one recording. Never auto-runs; uses the pip
    `imageio-ffmpeg` static binary (no system ffmpeg needed).
  - `./run push` — send notes to the Hermes echo-scribe webhook
    (`src/scribe.py`); needs `ECHO_SCRIBE_SECRET` (put it in a gitignored
    `.env`). `./run sync` downloads first and then offers to wipe the pen
    (gated on a successful audio download; y/N prompt).
  - `./run clean-pen` — download the notebook, then delete it from the pen
    (bulk, destructive; prompts for confirmation).
  - `./run sync` — pen download (+convert); needs the pen attached
  - Extra args go to `src/serve.py`: `./run serve --port 8010 --host 0.0.0.0`

- Python: `run` uses `.venv/bin/python` when it exists, else system `python3`.
  The venv is created with `--system-site-packages` so system `pycairo` is used —
  do not pip-install pycairo into the venv or recreate it without that flag.

- Hardware prerequisites (outside this repo's control — check before debugging
  everything else):
  - Pen must be physically connected with a micro-USB *data* cable and
    attached into WSL: `scripts/windows-connect.ps1` in an ADMIN PowerShell
    on Windows, then `lsusb` must show `1cfb:1032`.
  - `./run sync` runs `dumpscribe` under `sudo` (raw USB access); the sudo
    prompt is expected, and it chowns `data/` back afterwards.

## Data invariants

- `data/` (raw pen dump) and `output/` (generated site) are **gitignored
  generated artifacts** — never hand-edit files in either; re-run convert instead.
- `.convert-state.json` is the incremental-convert state file: unchanged
  sources are skipped and output whose source vanished is pruned. Delete it
  (or use `src/convert.py --force`) when testing conversion changes, or you'll
  wonder why nothing re-rendered.
- Pen sync always re-downloads everything (no per-asset skip in dumpscribe), so
  deletions made directly in `data/` only survive until the next real sync —
  unless you clear the pen afterwards (the `./run sync` offer, or
  `./run clean-pen`); with the pen retired, curated deletions stay deleted.

## Code conventions

- `src/serve.py` is **stdlib-only**. It serves `output/` statically (GET)
  and exposes guarded POST APIs: per-asset and batch delete (batch > 5
  items requires a typed `{"confirm": "DELETE"}` guard so the library can't
  be wiped by a slip), on-demand transcription, and Hermes push. Mutating
  APIs are loopback-only unless `ECHO_ALLOW_LAN_DELETES=1` and enforce a
  same-origin check. `src/convert.py` has the core logic; its only
  third-party dep is pycairo (OCR/STT deps are lazy imports in `src/ocr.py`
  / `src/scribe.py`).
- All output files are written **atomically** (tempfile + `os.replace`) so a
  concurrently running web server never serves a torn file — preserve this
  pattern for any new file writes.
- `vendor/` is vendored upstream code (dumpscribe.c, parsestf.py, MIT);
  minimal edits only. `vendor/dumpscribe` binary is gitignored and built by
  `./scripts/build-dumpscribe.sh` (run does this automatically when missing).
- Planned features are specced in `future_features/README.md`, including exact
  file touchpoints and design constraints — read it before implementing them.
