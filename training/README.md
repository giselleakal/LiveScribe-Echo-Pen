# Handwriting recognition training materials

Ground-truth samples for measuring and improving the pipeline's OCR
(`src/ocr.py`). Each sample pairs a page of handwriting with what it
*should* read as, so `./run ocr-eval` can score the OCR (character and
word error rate) before and after a change.

## Privacy (read first)

This repository is **public**. Everything committed here, including old
commits, can be read by anyone, permanently.

- Only commit samples whose content is safe to publish: write test
  sentences on purpose ("the quick brown fox…"), never real work or
  personal notes.
- Never commit names, phone numbers, addresses, account details,
  employer or customer details, or personal file paths.
- Keep sensitive samples **outside the repository** and point the
  evaluator at them: `./run ocr-eval --manifest ~/private-ocr/eval.json`.
- Nothing is committed or pushed automatically. A human reviews every
  sample before it is committed; the pre-commit PII guard
  (`scripts/check-pii.py`) refuses new images under `training/` unless
  the commit is run with `ECHO_PII_REVIEWED=1`.

## Evaluation manifest

`training/eval.json` (or any file passed to `--manifest`) lists samples:

```json
{
  "samples": [
    {"id": "fox", "page": "2208.1.12.25",
     "truth": "the quick brown fox\njumps over the lazy dog"},
    {"id": "digits", "image": "samples/digits.png",
     "truth": "0 1 2 3 4 5 6 7 8 9"}
  ]
}
```

- `page` — a page id from the library (the id shown on the page card).
  The page's strokes are read from `data/` and run through the full
  pipeline: line grouping, line rendering, recognition. Use this to
  measure real accuracy.
- `image` — a single *line* of handwriting as an image, path relative
  to the manifest. It goes straight to the recognizer, skipping line
  grouping. Use it to compare recognizers or decoding settings.
- `truth` — the correct transcription. Separate lines with `\n`.

Run it:

```sh
./run ocr-eval                        # training/eval.json, default profile
./run ocr-eval --profile legacy       # the previous OCR behaviour, to compare
./run ocr-eval --engine tesseract
./run ocr-eval --manifest ~/private-ocr/eval.json --json
```

Lower CER/WER is better. Compare profiles on the same manifest before
changing a default.

## How to add a sample (workflow)

1. **Write the sample** on Echo dot paper using publishable text, then
   `./run sync` so the page is in the library.
2. **Find its page id** on the page card in the web UI.
3. **Add an entry** to `training/eval.json` with the id and the exact
   text you wrote.
4. **Optionally add a picture** for people reading this doc: export the
   page image to `training/samples/` (PNG). Check it shows nothing
   private, then commit with `ECHO_PII_REVIEWED=1 git commit …`.
5. **Run `./run ocr-eval`** and note the score in the commit message.

## Conventions

- **Circled numbers:** transcribe a circled ⑤ as `(5)`, the same way in
  every ground truth.
- **Ground truth is what was written**, including misspellings; the OCR
  is scored against it as-is.
- **Line breaks:** one `\n` per written line, so line grouping errors
  show up in the score.

## Misreads worth targeted training (so far)

- A leading `ph` read as `h`.
- Circled digits read as letters or short words (`O`, `to`).
- Capitalised acronyms swapped for similar letters.
- TrOCR sometimes loops, repeating one short token across the line.
- Short function words just after a line break are worst; long,
  connected-cursive words survive best.
