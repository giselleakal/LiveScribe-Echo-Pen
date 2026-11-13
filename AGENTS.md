# AGENTS.md

Personal WSL2 pipeline for a Livescribe Echo smartpen: pull notes from the
pen (vendored C tool), render pages to SVG + OCR, serve a static site.
Verification is `./run test` (stdlib unittest, no network, no pen) plus
running the pipeline.

## Where to find things

Everything needed to run, change and verify this project is in the
repository. Read the doc for the area before changing it.

| Need | Read |
|---|---|
| rules for working here (this file) | `AGENTS.md` |
| how the parts fit, files on disk, API, log events | [`docs/architecture.md`](docs/architecture.md) |
| every check, the test map, writing tests | [`docs/testing.md`](docs/testing.md) |
| upgrading an existing install, one-time steps per change | [`docs/upgrading.md`](docs/upgrading.md) |
| install, daily use, troubleshooting (for people) | [`README.md`](README.md), [`CHEATSHEET.md`](CHEATSHEET.md) |
| measuring and improving OCR | [`training/README.md`](training/README.md) |
| planned features | [`future_features/README.md`](future_features/README.md) |
| guided feature work / debugging | `.agents/skills/add-feature/`, `.agents/skills/debug/` |

## Never put notes in the repository

**This repository is public. Everything committed, including commit
messages and old commits, can be read by anyone, permanently.**

Never add any of these to a tracked file, a commit message, an issue or
a PR description:

- pen data or anything derived from it: `.stf` pages, page SVG/PNG, audio,
  OCR text, transcripts, screenshots or photos of the notebook
- handwriting samples and OCR ground truth: they go in the gitignored
  `training/private/`, never in `training/` itself
- what a note says, even quoted as an example or a test string; tests
  use synthetic strokes from `tests/fixtures.py`
- names, phone numbers, emails, addresses, employer or customer details
- usernames and personal paths (`C:\Users\<name>`, `/home/<name>`); write
  placeholders like `~/echo` or `%USERPROFILE%`

`titles.json` and `notebook_names.json` are tracked, but they hold names the
user typed in the web UI. Do not commit changes to them unless the user
explicitly asks for that commit.

Before every commit, read `git diff --cached`. If anything above shows up,
stop and ask. Do not "clean it up later": removing data from published
history needs the repository owner and a force-push.

## Starting the application

Do these in order whenever you start (or restart) the application:

1. `git status` and `git log -1 --oneline`: know what is checked out, and
   don't touch uncommitted work you didn't make.
2. If the code changed since the server was last started, stop the
   running `./run` first (Ctrl+C in its terminal). Then follow
   [`docs/upgrading.md`](docs/upgrading.md) once for each change pulled.
3. `./run doctor`. Fix every `✘`; a `!` only matters if you need that
   feature (pen, OCR, STT, push).
4. `./run test`. Check that nothing is skipped unexpectedly (see
   [`docs/testing.md`](docs/testing.md)).
5. Start the server. `./run` converts, then serves in the foreground
   until Ctrl+C, so run it in its own terminal, or in the background:
   `nohup ./run > logs/serve.out 2>&1 &`. It is up when
   `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/` prints
   `200`.
6. Pen work only when the user says the pen is attached: `./run sync`
   asks for sudo. Never answer the clear-the-pen prompt on the user's
   behalf.
7. If anything misbehaves, use the `debug` skill and start from
   `./run logs --errors --run last`.

## Entry point

- `run` is the only entry point; don't invoke `src/*.py` or `vendor/dumpscribe` directly unless debugging:
  - `./run`: incremental convert + serve on http://127.0.0.1:8000 (foreground)
  - `./run setup [--ocr] [--stt]`: create `.venv` and install dependencies;
    idempotent. `./run doctor`: check the toolchain, pen and state, with one
    fix line per problem.
  - `./run convert` / `./run ocr`: render only (`ocr` forces the OCR pass).
    `--ocr-profile legacy` restores the original OCR behaviour.
  - `./run ocr-eval`: score OCR (CER/WER) against
    `training/private/eval.json`; see `training/README.md`.
  - `./run stt`: transcribe session audio (Moonshine) explicitly; pass
    `--session PRS-…` to do one recording. It never runs automatically, and it
    uses the pip `imageio-ffmpeg` static binary (no system ffmpeg needed).
  - `./run push`: send notes to the Hermes echo-scribe webhook
    (`src/scribe.py`). Needs `ECHO_SCRIBE_SECRET` (put it in a gitignored
    `.env`); exits 2 without it and 1 when notes were parked.
  - `./run sync`: pen download (+convert); needs the pen attached. Then it
    offers to wipe the pen, only when both `[ECHO] audio download ok` and
    `[ECHO] pages download ok` were printed (y/N; no terminal means no).
  - `./run clean-pen`: download, then retire the pen, only after a
    complete download (bulk, destructive; prompts first).
  - `./run test`: the test suite. `./run logs`: recent structured log
    records. `./run debug-report`: a sanitized bundle for debugging.
  - Extra args go to `src/serve.py`: `./run serve --port 8010 --host 0.0.0.0`

- Python: `run` uses `.venv/bin/python` when it exists, else system `python3`.
  The venv is created with `--system-site-packages` so system `pycairo` is used:
  do not pip-install pycairo into the venv, and do not recreate the venv without
  that flag (`./run setup` does it correctly). Code must run on Python 3.10.

- Hardware prerequisites, outside this repo's control. Check these before
  debugging everything else:
  - The pen must be physically connected with a micro-USB *data* cable and
    attached into WSL: run `scripts/windows-connect.ps1` in an ADMIN
    PowerShell on Windows, then `lsusb` must show `1cfb:1032`.
  - `./run sync` runs `dumpscribe` under `sudo` (raw USB access). The sudo
    prompt is expected, and `data/` is chowned back even when the download
    fails. `run` rebuilds `vendor/dumpscribe` when `dumpscribe.c` is newer.

## Data invariants

- `data/` (raw pen dump), `output/` (generated site) and `logs/` are
  **gitignored generated artifacts**. Never hand-edit them; re-run convert
  instead.
- `.convert-state.json` is the incremental-convert state file: unchanged
  sources are skipped, and output whose source vanished is pruned. Use
  `./run convert --force` to re-render everything; transcriptions, transcripts
  and capture stamps of unchanged ink survive it. The state is saved after
  each OCR'd page and STT'd session, so an interrupted run keeps its work.
- `.convert.lock` is an `flock`: the kernel releases it when its holder
  dies, so a leftover file is not a held lock (`./run doctor` tells the two
  apart). serve.py takes the same lock for every mutation.
- Pen sync always re-downloads everything (dumpscribe has no per-asset skip), so
  deletions made directly in `data/` only survive until the next real sync,
  unless you clear the pen afterwards (the `./run sync` offer, or
  `./run clean-pen`). With the pen retired, curated deletions stay deleted.
- `ECHO_ROOT` points every module at another data root (see `src/paths.py`);
  tests use it to run against a sandbox. Never point a test at the real
  checkout.

## Code conventions

- `src/serve.py` is **stdlib-only**:
  - It serves `output/` statically (GET) and exposes guarded POST APIs:
    per-asset and batch delete, on-demand transcription, Hermes push, rename
    and title.
  - A batch of more than 5 deletes needs a typed `{"confirm": "DELETE"}`, so
    the library can't be wiped by a slip.
  - Every POST goes through one guard: Content-Type exactly
    `application/json`, an Origin matching the Host, and loopback-only
    unless `ECHO_ALLOW_LAN_DELETES=1`. A loopback server only answers
    loopback Host names (DNS rebinding).
  - New endpoints go through `Handler._route` and raise `ApiError`. Never
    hand-roll a check.
- Notebook display names live in `notebook_names.json` (guid -> friendly
  name, edited via `/api/rename`). Per-asset titles live in `titles.json`
  (asset id -> title, via `/api/title`).
- `src/convert.py` holds the core pipeline, split into phases:
  `convert_pages`, `convert_sessions`, `link_sessions`, `prune_outputs`,
  `transcribe_sessions`, `write_index`. Its only third-party dependency is
  pycairo; the OCR/STT dependencies are lazy imports in `src/ocr.py` and
  `src/stt.py`.
- All output files are written **atomically** (tempfile + `os.replace`) so a
  concurrently running web server never serves a torn file. Preserve this
  pattern for any new file writes.
- `vendor/` is vendored upstream code (dumpscribe.c, parsestf.py, MIT);
  minimal edits only, each marked `[ECHO]` in a comment. The
  `vendor/dumpscribe` binary is gitignored and built by
  `./scripts/build-dumpscribe.sh`.
- Planned features are specced in `future_features/README.md`, with exact
  file touchpoints and design constraints. Read it before implementing them,
  and use the `add-feature` skill (`.agents/skills/add-feature/`).

## Logging and telemetry

- Use `src/telemetry.py`; never `print` a warning or hand-roll a logger:
  - `log = telemetry.get_logger("<component>")`, then
    `log.info("<event.name>", extra={...})`.
  - `telemetry.warn(log, event, sentence, **fields)` for anything a human
    should see on the terminal.
  - `with telemetry.io_call(log, "<target>") as call:` around every call
    that crosses a process, network or storage boundary. It emits exactly
    one record with outcome and duration; pass `ok_level=logging.DEBUG`
    for per-page calls.
- Records go to `logs/echo.jsonl`, one JSON object per line, carrying a UTC
  `ts`, `level`, `component`, `event`, `run_id` (one per `./run`) and, in
  serve, a `request_id`.
- Child processes inherit the correlation ids through
  `telemetry.child_env()`.
- **Never log note content**: OCR text, transcripts and note payloads are
  logged as lengths only. Secret-looking keys and the push secret are
  redacted by the formatter; don't route around it.
- INFO records lifecycle (start, done, counts); per-item detail is DEBUG.
  ERROR means a human must act; an expected refusal is INFO.
- Print to stdout only for the user-facing progress lines of a CLI.

## Verification

The full list of checks, the test map and how to write tests are in
[`docs/testing.md`](docs/testing.md). The rules:

- `./run test` must pass before every commit; add or update a focused test
  first for behaviour changes. Tests are stdlib `unittest`, deterministic,
  with no network or pen. Use `tests/fixtures.py` for synthetic `.stf`
  pages and sessions.
- Run the checks for what you touched: Python 3.10 for syntax, shellcheck
  for `run` and `scripts/*.sh`, `scripts/check-dumpscribe.sh` for the C.
- A change that alters files on disk, the API or a command adds its
  one-time steps to [`docs/upgrading.md`](docs/upgrading.md), and its
  contract changes to [`docs/architecture.md`](docs/architecture.md).
- OCR changes: add a profile or knob in `src/ocr.py` instead of changing
  `legacy`, and compare with `./run ocr-eval --profile ...` on real samples.
- `vendor/dumpscribe.c` cannot be built without openobex:
  `scripts/check-dumpscribe.sh` then only syntax-checks it, and you must
  say so.

## Debugging

Use the `debug` skill (`.agents/skills/debug/`). It escalates from the
cheapest checks (cable, `lsusb`, `./run doctor`) through `./run logs
--errors --run last` and state files to deterministic reproductions. Start
from `./run debug-report` when someone else's machine is involved; it
contains no note content.

## Working style

### Presenting information

Choose the shape that makes the relationship visible; prose is the fallback.

| Content                                      | Shape                                   |
| -------------------------------------------- | --------------------------------------- |
| items with two or more comparable attributes | table                                   |
| options with trade-offs                      | table: one row per option               |
| ordered steps                                | numbered list                           |
| branching flow, retries, state changes       | ASCII diagram (never Mermaid)           |
| file layout or data shape                    | ASCII tree                              |
| before/after or change summary               | two-column table                        |
| a single fact                                | prose                                   |

Lead with the conclusion, then the table or diagram that supports it.

### Autonomy

- Act on your best judgment at **70% confidence or more**. State the choice
  in one line.
- Below that, ask one batched question with 2–4 concrete options.
- Always ask before anything destructive or irreversible: wiping the pen,
  deleting data, force-pushing, rewriting history, publishing, or committing
  user content (see "Never put notes in the repository").
- Prefer a cheap reversible experiment over a question. For pipeline
  changes, an `ECHO_ROOT` sandbox is that experiment.
- A missing requirement is asked about regardless of confidence.

### Command-line tools

Prefer `rg`, `fd`, `bat`, `jq`, `gh` when they exist; this machine may only
have the GNU defaults (`grep`, `find`, `cat`), so fall back instead of
failing. Use `jq` for any JSON, including `logs/echo.jsonl`. Add
`--plain`/`-p` when piping colorized tools.

### Coding

- Functional core, imperative shell: decisions in small pure functions
  (`page_reusable`, `session_reusable`, `split_wide_line`); file and process
  effects at the edges.
- Small, single-purpose functions. Nest at most two levels; handle
  invalid, empty and error cases first. Cyclomatic complexity ≤ 10 per
  function, 15 at most.
- Prefer data-driven dispatch over `if`/`elif` chains of like branches.
- Parameter objects (frozen dataclasses such as `OcrJob`, `PageInk`) over
  long argument lists. Avoid flag parameters.
- No catch-all `utils`/`helpers` modules. Shared knowledge lives in one
  module and is imported, never copied. Move misplaced code as you work,
  and update every call site in the same change.
- Make it work, then correct, then fast. Profile before optimizing.
- Match the repository's style: `%`-formatting in existing modules is fine.
  Don't reformat code you are not changing.

### Comments

- Comment only what the code cannot say: a business rule, a constraint, an
  invariant ("the pen may only be wiped when every notebook is on disk").
- No history in comments ("used to", "instead of", "TODO refactor"); that
  belongs in the commit message.
- Never let another file stand in for the explanation; state the rule.
- Update or delete a comment in the same change that invalidates it.

### Git

- Work on a `feature/<name>` branch and open a PR. The PR body lists what
  changed, how it was verified, and what was not.
- Small, focused commits with an imperative subject line, matching the
  existing history.
- Never force-push, rewrite shared history, or push to `master` without the
  owner's explicit go.
- Never commit secrets: `.env`, `ECHO_SCRIBE_SECRET`, tokens.
- Say what you ran to verify; if you couldn't verify something (no pen, no
  TrOCR, no openobex), say so instead of implying success.
