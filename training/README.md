# Handwriting recognition training materials

Ground-truth pairs for improving the pipeline's OCR (`src/ocr.py`,
tesseract): each entry pairs the original handwriting image with what
the OCR actually produced and what it *should* have produced. One
small section per sample; more samples of the same misread word are
worth more than long paragraphs.

In each section, **OCR said** is the pipeline's *raw* output (never
cleaned up — that's the training signal) and **You wrote** is the
ground truth. A `/` marks a line break.

---

## Sample 05 — from page Notes_Nov_12_05 (written 2026-11-12)

![sample 05](samples/sample_05.png)

**OCR said:**
hones are attached / a / too-to-to-to-to-to-to-to-to-to-to- / O After
that I need to determine / D. D. D. / if these switches are COR-enabled.

**You wrote:**
phones are attached to. / (5) After that I need to determine if these
switches are CER-enabled.

---

## How to add a sample (workflow)

1. **Save the handwriting image** to Windows `Downloads`
   (`C:\Users\gisel\Downloads\`). PNG preferred; any readable image works.
2. **Tell the agent** "new sample in Downloads" — it copies the image
   into `samples/`, runs the OCR to fill the "OCR said" lines, adds the
   section, and commits. The human fills in "You wrote" (the ground
   truth) either directly or by telling the agent.
3. Edit this file in Notepad via the Windows path
   `\\wsl.localhost\Ubuntu\home\giselle\echo\training` — editing here
   edits the repo copy directly; the agent commits it.

*Note:* Notepad's markdown view doesn't display images — the image
links still work in other viewers (GitHub, VS Code, Obsidian). Open the
image file itself from `samples/` if you want to see it alongside.

## Conventions

- **Sample naming:** sample number = the `NN` in the source page's
  `Notes_Mmm_DD_NN_…` filename (Nov 12 page 05 → `sample_05.png`), so
  every sample's name points straight back to the page it came from.
  Page titles in the library (`Notes_Nov_12_01`…`05`) follow the same
  scheme, so UI title ↔ sample file ↔ section all agree.
- **Circled numbers:** transcribe the circled ⑤ as `(5)` — one
  convention, used consistently in every ground truth.
- **"OCR said"** must be the OCR's *actual* output, never cleaned up.
  Corrections go under "You wrote" only.

## Misreads worth targeted training (so far)

- `ph` → `h`, `phones` → `hones`
- `5` → `O` / `to`
- `CER` → `COR` / `D.`
- Short function words after a line break are worst; connected-cursive
  phrases (determine, switches, attached) survive best.
