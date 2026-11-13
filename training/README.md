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
    {"id": "todo", "page": "2208.1.12.31", "split": "test",
     "truth": "call the plumber\nbuy stamps"},
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
- `split`: `dev` (the default) or `test`. See "Measuring without fooling
  yourself" below.

Run it:

```sh
./run ocr-eval                              # default profile, dev samples
./run ocr-eval --profile legacy             # the original OCR
./run ocr-eval --profile beam               # the previous default
./run ocr-eval --set lines=vertical         # default minus one change
./run ocr-eval --split test                 # confirm a decision, once
./run ocr-eval --engine tesseract
./run ocr-eval --json                       # machine-readable
```

Lower CER/WER is better. Each run prints the mean and a 95% interval.

## What the profiles do

| Setting | `legacy` | `beam` | `default` | What it changes |
|---|---|---|---|---|
| `beams`, `no_repeat_ngram` | 1, 0 | 4, 3 | 4, 3 | beam search; blocks `to-to-to…` loops |
| `max_line_aspect` | off | 14 | 14 | splits very long lines at word gaps |
| `oversize_ratio` | off | 2.5 | 2.5 | circles/margin lines don't make or stretch lines |
| `render` | plain | plain | normalized | black ink, fixed letter height and pen width, like TrOCR's training lines |
| `lines` | vertical | vertical | temporal | line breaks from the order the pen wrote, so sloped lines stay apart |
| `pen_widths` | — | — | 0.06, 0.09, 0.12 | reads each line three ways, keeps the most confident |
| `vocab_margin` | 0 | 0 | 0.15 | prefers real words among near-tied readings the model proposed |

Any setting can be changed for one run with `--set key=value`, e.g.
`--set pen_widths=0.09` (one reading) or `--set vocab_margin=0` (no
word prior).

## Measuring without fooling yourself

It is easy to tune settings until they fit a handful of pages and then
read worse on everything else. To avoid that:

1. Mark about a third of the samples `"split": "test"`, choosing whole
   pages (never lines from a page that is also in dev). Write them on
   different days if you can.
2. Try ideas on dev only: compare one `--set` change at a time against
   the default.
3. Believe a difference only when it is bigger than the printed 95%
   interval. With fewer than about 30 lines, most differences are noise.
4. Run `--split test` once, to confirm the decision you already made on
   dev. If you keep adjusting until test looks good, test has become dev.
5. Prefer changes with a reason that doesn't depend on your samples
   (the rows above) over thresholds tuned to them.

## Your own words

The word prior uses the system word list (`sudo apt install wamerican`)
plus, if it exists, `training/private/vocabulary.txt`: one word per line,
for names, acronyms and jargon you write often. It lives in the private
folder because it says what your notes are about. The prior only breaks
near-ties between readings the model itself proposed, so a long
vocabulary list cannot make it invent words.

## How to add a sample

1. Write on Echo dot paper, then `./run sync` so the page is in the library.
2. Find its page id on the page card in the web UI.
3. Add an entry to `training/private/eval.json` with the id, the exact
   text you wrote, and `"split": "test"` for roughly every third page.
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
