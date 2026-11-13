# Handwriting recognition training materials

Ground-truth pairs for improving the pipeline's OCR (`src/ocr.py`,
tesseract): each row pairs the original handwriting image with what the
OCR actually produced and what it *should* have produced. Keep one row
per sample; more samples of the same misread word are worth more than
long paragraphs.

| Original | Transcription (OCR output) | Correction (ground truth) |
|---|---|---|
| ![sample 05](samples/sample_05.png) | hones are attached<br>a<br>too-to-to-to-to-to-to-to-to-to-to-<br>O After that I need to determine<br>D. D. D.<br>if these switches are COR-enabled. | phones are attached to.<br><br>(5) After that I need to determine if these switches are CER-enabled. |

## Notes

- **Sample 05** — source: `Notes_Nov_12_05_Image_OCR.png` (Windows
  Downloads), written on Echo dot paper, 2026-11-12.
- **Naming convention:** sample number = the `NN` in the source page's
  `Notes_Mmm_DD_NN_…` filename (Nov 12 page 05 → sample 05), so every
  sample's name points straight back to the page it came from.
- Ground-truth nuance: the ink shows a **circled ⑤**; the correction
  writes it as `(5)`. For training data, decide one convention and use
  it consistently (e.g. always transcribe circled numbers as `(N)`).
- Recurring misreads worth targeted training: `ph`→`h`, `phones`→
  `hones`, `5`→`O`/`to`, `CER`→`COR`/`D.`. The connected-cursive
  phrases (determine, switches, attached) survive OCR best; short
  function words after line breaks are worst.
