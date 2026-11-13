# Upgrading an install

What to do when new code is pulled onto a machine that already has a
library. Newest change last; do the steps for every change between the
code you had and the code you pulled.

## Every upgrade

1. **Stop the running server** (`./run` in its terminal: Ctrl+C) *before*
   pulling. A server started from older code keeps that code in memory
   and can misread files the new code writes.
2. `git pull` (or merge the change).
3. `./run doctor`, and fix every `✘` line. `./run setup` is safe to re-run.
4. Do the one-time steps below for each change you pulled.
5. Start it again: `./run`.

## Repo hardening (#4)

Verified by pulling this change onto a copy of an install made by the
code before it.

| Step | Why |
|---|---|
| **Before pulling**, copy any images you want to keep from `training/samples/` to `training/private/` | the pull deletes `training/samples/` (samples are notes and no longer tracked). If it already happened, recover one with `git log --diff-filter=D --name-only -- training/samples` and `git show <commit>^:<path> > training/private/<name>` |
| Stop any running server before pulling (above) | the old server treats any existing `.convert.lock` file as a held lock; the new code keeps that file, so an old server would refuse every change as "busy" |
| `./run doctor` | it is new: checks the venv, OCR/STT engines, the pen client, the library and the lock |
| Expect the first `./run ocr` to re-transcribe every page once | each page now records the OCR settings it was read with, and older pages have none |
| Expect the next `./run sync` to rebuild `vendor/dumpscribe` | its source changed (pages-download marker, safer clean mode); needs the build packages, which doctor checks |
| If a script or cron job runs `./run push`, handle its exit codes | 2 = `ECHO_SCRIBE_SECRET` missing, 1 = some notes were parked (queued), 0 = all sent |
| In Pi, run `/reload` | picks up the new `add-feature` and `debug` skills |

Nothing else needs migrating:
- `.convert-state.json` from older code loads as-is, and unchanged pages
  are not re-rendered.
- A `.convert.lock` left behind by a killed process is ignored.
- `.push-state.json`, `titles.json` and `notebook_names.json` are
  unchanged.
- `logs/` is created on first use.
