# Testing

Every check this project has, when to run it, and how to add tests.
Rules (test first for behaviour changes, synthetic data only) are in
[`AGENTS.md`](../AGENTS.md).

## Checks

| Check | Command | Run it when |
|---|---|---|
| unit + integration tests | `./run test` | before every commit |
| Python 3.10 (the oldest supported) | `uv run --python 3.10 python -m unittest discover -s tests -t .` | syntax or stdlib changes |
| shell scripts | `bash -n run` and `uvx --from shellcheck-py shellcheck run scripts/*.sh` | editing `run` or `scripts/*.sh` |
| pen client (C) | `scripts/check-dumpscribe.sh` | editing `vendor/dumpscribe.c` |
| Hermes push end to end | `python3 scripts/test-push-sender.py` | editing `src/scribe.py` |
| setup of this machine | `./run doctor` | before starting, and after an upgrade |
| OCR accuracy | `./run ocr-eval` (see [`training/README.md`](../training/README.md)) | OCR changes, on real samples |

Read the result, not just the exit code:

- **Skipped tests.** `./run test` uses `.venv/bin/python`, else the first
  `python3` on `PATH`. If that interpreter has no pycairo, every
  rendering test is *skipped*, and the run still says OK. A count after
  `skipped=` means the pipeline tests did not run; use an interpreter
  with pycairo (the system `python3`, or `./run setup`).
- **`check-dumpscribe.sh`** prints `full build OK` only when openobex is
  installed. `syntax OK` means it was checked against
  `scripts/obex-stub/` only; say "syntax-checked, not built" in the PR.
- **Things this repository cannot test** on a machine without the
  hardware or models: a real pen sync or wipe, a real dumpscribe build,
  TrOCR/Moonshine inference, and OCR accuracy. Name them as unverified.

## Where the tests are

| File | Covers | Style |
|---|---|---|
| `tests/fixtures.py` | synthetic `.stf` writer, sessions, `reset_sandbox()` | helpers |
| `tests/__init__.py` | points `ECHO_ROOT` at a throwaway sandbox before any import | — |
| `test_convert_units.py` | pure helpers: line grouping, page addresses, session headers, STF parsing | direct calls |
| `test_convert_pipeline.py` | a whole convert run against synthetic data | subprocess in its own root |
| `test_convert_state.py` | incremental state, lock, OCR/STT reuse, corrupt sources, interruptions | in-process `convert.main()` |
| `test_ocr.py` | profiles, engine choice, line splitting, `ocr-eval` scoring | direct calls, mocked recognizer |
| `test_stt.py` | audio windows and stitching | direct calls |
| `test_serve_api.py` | every API guard and handler | in-process server on a random loopback port |
| `test_scribe.py` | signing, chunking, push results, queue, CLI | fake `post_body` |
| `test_telemetry.py` | record shape, redaction, correlation, rotation, CLI | reads `logs/echo.jsonl` |
| `test_doctor.py` | doctor checks, debug report without note content | direct calls |
| `test_run_script.py` | `run`: pen-safety rules, setup, help | copies `run` with stub `lsusb`/`sudo`/`dumpscribe` |
| `test_docs.py` | paths and commands named in the docs exist | file checks |

## Writing a test

1. **Sandbox only.** Importing `tests` sets `ECHO_ROOT` to a temporary
   directory. Call `fixtures.reset_sandbox()` in `setUp` so tests can't see
   each other's output. Never point a test at the real checkout.
2. **Synthetic data only.**
   - `fixtures.write_page(root, id, fixtures.text_line(x, y, n))` writes
     a page; pass `raw=b"..."` for a corrupt one.
   - `fixtures.write_session(...)` writes a recording.
   - Text is made up ("the quick brown fox"), never from a note.
3. **Pick the layer that shows the bug:**
   - a pure function: call it directly
   - a convert run: `convert.main([...])` with stdout captured (see
     `run_main` in `test_convert_state.py`)
   - an API: the in-process server in `test_serve_api.py`
   - `run`: the stubbed copy in `test_run_script.py`
4. **Mock at the boundary,** not inside the logic:
   - `ocr.recognize` / `ocr.trocr_available` for OCR
   - `stt.transcribe` / `stt.seq_to_floats` for STT
   - `scribe.post_body` for Hermes
   - `telemetry.monotonic` for durations
5. **Watch it fail first,** then fix, then run the whole suite.
6. Tests needing pycairo use
   `@unittest.skipUnless(fixtures.has_cairo(), ...)`.
