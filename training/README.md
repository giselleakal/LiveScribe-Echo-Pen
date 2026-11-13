# Handwriting recognition training materials

How to measure and improve the pipeline's OCR (`src/ocr.py`) against
pages whose correct text you know. `./run ocr-eval` scores the OCR with
character and word error rates (CER/WER) before and after a change.

## Notes never go in the repository

This repository is public, and everything committed stays in its history.
Samples, page images and ground-truth text are your notes, so they live in
`training/private/`, which git ignores. Only this README is tracked.

## Evaluation manifest

`training/private/eval.json` (or any file passed to `--manifest`) lists
the samples:

```json
{
  "samples": [
    {"id": "fox", "page": "2208.1.12.25",
     "truth": "the quick brown fox\njumps over the lazy dog"},
    {"id": "digits", "image": "digits.png",
     "truth": "0 1 2 3 4 5 6 7 8 9"}
  ]
}
```

- `page`: a page id from the library (the id shown on the page card).
  The page's strokes are read from `data/` and run through the full
  pipeline: line grouping, line rendering, recognition. Use this to
  measure real accuracy.
- `image`: a single *line* of handwriting as an image, with its path
  relative to the manifest. It goes straight to the recognizer and skips
  line grouping. Use it to compare recognizers or decoding settings.
- `truth`: the correct transcription. Separate lines with `\n`.

Run it:

```sh
./run ocr-eval                        # default profile
./run ocr-eval --profile legacy       # the previous OCR behaviour, to compare
./run ocr-eval --engine tesseract
./run ocr-eval --json                 # machine-readable
```

Lower CER/WER is better. Compare profiles on the same manifest before
changing a default.

## How to add a sample

1. Write on Echo dot paper, then `./run sync` so the page is in the library.
2. Find its page id on the page card in the web UI.
3. Add an entry to `training/private/eval.json` with the id and the exact
   text you wrote.
4. Run `./run ocr-eval`.

## Conventions

- **Circled numbers:** transcribe a circled ⑤ as `(5)`, and do it the
  same way in every ground truth.
- **Ground truth is what was written**, misspellings included. The OCR is
  scored against it as-is.
- **Line breaks:** one `\n` per written line, so line-grouping errors show
  up in the score.

## Misreads worth targeted training (so far)

- A leading `ph` read as `h`.
- Circled digits read as letters or short words (`O`, `to`).
- Capitalised acronyms swapped for similar letters.
- TrOCR sometimes loops, repeating one short token across the line.
- Short function words right after a line break are worst; long words
  in connected cursive survive best.
