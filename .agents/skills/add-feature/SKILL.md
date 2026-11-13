---
name: add-feature
description: Guide a person, step by step and in plain language, through adding a feature to this Livescribe Echo pipeline. Covers understanding the want, scoping, planning, building on a feature branch, an agent self-review for bugs and cleanup, verification, and the pull request. Use when someone asks to add, build, change or extend something in this repository.
---

# Add a feature to the Echo pipeline

You are pairing with a person who may not be a programmer. Lead them
through the stages below, in order. Speak plainly, ask **one question at a
time**, and explain any term you have to use ("a branch is a separate copy
of the code where we can work safely"). Never skip a stage. Small features
go through stages quickly; they still go through all of them.

```text
1 understand ─► 2 scope ─► 3 plan ─► [human OK] ─► 4 build ─► 5 review ─► 6 verify ─► 7 ship ─► [human OK]
                    ▲                                              │            │
                    └──────────── new information changes scope ───┴────────────┘
```

Read `AGENTS.md` before stage 2. Its rules override anything here, and the
first one matters most: **notes never go in the repository.**

## 0. Before anything

1. Run `git status`. If there are uncommitted changes, ask what they are
   before touching anything.
2. Run `./run doctor` and `./run test`, so you know what "working" looks
   like on this machine. If tests already fail, tell the person and fix that
   (or agree to work around it) before starting.

## 1. Understand the want

Find out what problem the person has, not just what they asked for.

- "What would you like the notebook to do that it doesn't today?"
- "Walk me through the last time you wished it could do this."
- "How will you know it's working: what would you see?"

Restate it in one or two sentences and ask "Did I get that right?" Keep
going until the answer is yes.

## 2. Scope it

1. Check `future_features/README.md`. If the feature is already specced
   there, follow that spec, and tell the person.
2. Find where it lives, using the map below, and read those files.
3. Agree on **non-goals**: what this feature will deliberately not do.
4. Name the risks out loud if any apply:

| Risk | Why it matters here |
|---|---|
| deletes anything from the pen or `data/` | not undoable; always behind a typed or y/N confirmation |
| touches a POST API | must go through `Handler._route` and `ApiError` in `serve.py` |
| writes files | atomically (`atomic_write`), or the web server can serve a torn file |
| shows, stores or sends note content | must never reach git, logs or the PR |
| changes OCR | new profile or knob in `src/ocr.py`; measure with `./run ocr-eval` |
| edits `vendor/` | minimal edits, marked `[ECHO]`; C cannot be built without openobex |

Where things live:

| Area | File |
|---|---|
| commands (`./run …`) and their help text | `run` (help = comment block at the top) |
| pen data → SVG/PNG, index, state, lock | `src/convert.py` |
| web page markup | `src/webpage.py`; styles, script, skeleton in `src/web/` |
| web APIs | `src/serve.py` (stdlib only) |
| handwriting recognition, profiles | `src/ocr.py`, `src/ocr_eval.py` |
| speech-to-text | `src/stt.py` |
| sending notes to Hermes | `src/scribe.py` |
| logs and correlation ids | `src/telemetry.py` |
| setup checks, debug report | `src/doctor.py` |
| data root (`ECHO_ROOT`) | `src/paths.py` |
| tests and synthetic pen data | `tests/`, `tests/fixtures.py` |

Write the scope as a short list (goal, non-goals, files, risks) and show it.

## 3. Plan

Turn the scope into numbered steps. Each step is small enough to check on
its own and says how it will be checked:

```text
1. Add a failing test for <behaviour> in tests/test_<area>.py   — check: ./run test fails for the right reason
2. Implement <change> in src/<file>.py                           — check: that test passes
3. Add the button / command / help line                          — check: ./run help, or the page in a browser
```

Ask for the branch name (suggest `feature/<short-name>`), then ask
**"Shall I start?"** Do not write code before a clear yes.

## 4. Build

1. `git switch -c feature/<name>` from an up-to-date `master`.
2. For each plan step: write or update the test first, make it pass, then
   commit with an imperative subject ("Add page export button").
3. Follow `AGENTS.md`:
   - small functions and early returns
   - parameter objects over long argument lists
   - `telemetry` for warnings and every IO call
   - stdlib-only `serve.py`
   - code that runs on Python 3.10
4. Tests use `tests/fixtures.py` (synthetic strokes and sessions) and the
   `ECHO_ROOT` sandbox. Never use real pages or real note text.
5. Tell the person what you are doing between steps, in one sentence each.

If you learn something that changes the scope, stop, explain it, and go
back to stage 2.

## 5. Review your own work (required)

Before you call anything done, review the whole change for bugs and
cleanup as if someone else wrote it. Read `git diff master...HEAD`
end to end.

1. **Get a second opinion.**
   - If subagents are available, ask a fresh `reviewer` for a read-only
     review of the diff.
   - If `jev_review` is available, run it on each changed file with
     `reviewType: "correctness"`, then `"quality"`.
   - Treat every flag as a lead, not a verdict: check it against the code.
2. **Hunt for bugs.** Look for each of these:
   - error paths that crash instead of reporting
   - empty or missing inputs
   - off-by-one boundaries
   - two requests or processes racing on the same file (is the run lock
     held?)
   - a partial failure that leaves state half-written
   - behaviour that contradicts a name or docstring
3. **Clean up.** Remove:
   - dead code, debug prints and unused imports
   - duplicated logic (import the existing helper instead)
   - unclear names and stale or "history" comments
   - functions that grew past about 40 lines or two levels of nesting
4. **Check the rules.**
   - no note content, personal path or secret in the diff, a log record
     or a commit message
   - atomic writes; POST APIs routed through the guard
   - help text, `README.md`, `CHEATSHEET.md` and `AGENTS.md` updated if
     commands changed
5. **Fix what you found**, with a test for each real bug, and re-run
   `./run test`.
6. Tell the person what the review found and what you changed, as a short
   table (finding → fix). If you chose not to fix something, say why.

## 6. Verify

Run these, and report the actual results:

| Check | Command |
|---|---|
| tests | `./run test` |
| Python 3.10 | `uv run --python 3.10 python -m unittest discover -s tests -t .` (if uv exists) |
| shell scripts, if changed | `bash -n run`; `uvx --from shellcheck-py shellcheck run` |
| pipeline in a sandbox | `ECHO_ROOT=$(mktemp -d) ./run convert` with fixture data, or the person's real `./run convert` |
| web page, if changed | `./run`, then the person clicks through it; or `curl -s localhost:8000/` |
| OCR, if changed | `./run ocr-eval --profile legacy` vs the new profile |
| setup still fine | `./run doctor` |

Ask the person to try the feature themselves and confirm it does what
stage 1 said. Something you could not verify (no pen, no TrOCR) is
stated, not implied.

## 7. Ship

1. Show `git log --oneline master..HEAD` and `git diff --stat master...HEAD`.
2. Draft the PR body:

   ```markdown
   ## What and why
   <the stage-1 sentence>

   ## Changes
   - <one bullet per commit or area>

   ## Review findings fixed
   | Finding | Fix |

   ## Verification
   - [x] ./run test (N tests)
   - [ ] <anything not verified, and why>
   ```

3. Check the PR body itself for note content.
4. Ask **"Shall I push the branch and open the pull request?"** Push and run
   `gh pr create` only after a clear yes. Never push to `master` and never
   force-push.
