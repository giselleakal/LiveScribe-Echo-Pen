# Printable Echo dot-paper notebooks — inventory

Free official Livescribe printable notebooks (25 pages each, US Letter).
Printed pages work with the Echo pen exactly like a real notebook: ink
is captured, and `./run sync` treats them like the ERT Notes pages.

Source: Livescribe support article "Printing free Livescribe dot paper
and controls online" (livescribe.helpscoutdocs.com article 2338).
The live livescribe.com links 504'd (Nov 2026), so these were
recovered from Internet Archive snapshots of the /en-us/ paths.

| File           | Pages | Size      | SHA-256 (first 12) | Archive snapshot                          |
|----------------|-------|-----------|--------------------|-------------------------------------------|
| notebook_1.pdf | 25    | 5,897,434 | 06271fdb9477       | web.archive.org/web/20171011145327id_/…   |
| notebook_2.pdf | 25    | 5,897,617 | f1c865a0348f       | (captured via /en-us/, gzip; gunzipped)   |
| notebook_3.pdf | 25    | 5,897,802 | 642607eed025       | (captured via /en-us/, gzip; gunzipped)   |
| notebook_4.pdf | 25    | 5,897,978 | ec93cc892a75       | web.archive.org/web/20171011id_/…         |

Verify before printing: `sha256sum notebook_*.pdf` and
`pdfinfo notebook_1.pdf` (expect 25 pages, 612x792 pts, not encrypted).

## Printing rules (violating these wastes the paper)

- **Color laser printer, 600+ DPI.** Inkjet smears the dots; B&W fails
  (the pattern is built from overlapping CMY dots). A print shop works.
- **"Actual size" / no scaling.** Any scale shifts the dot grid.
- **Duplex is fine** (front and back are different page addresses) —
  one sheet = two writing pages.
- **Never print the same page of the same notebook twice.** Two copies
  share one page address, so ink from the second merges into the first.
- Print only the pages you need (e.g. `pdfseparate`/`pdfjam` or printer
  page ranges); unused page addresses keep for later.

## Page usage log (print each page at most once)

| Notebook  | Pages printed | Date       | Notes |
|-----------|---------------|------------|-------|
| 1         | —             | —          |       |
| 2         | —             | —          |       |
| 3         | —             | —          |       |
| 4         | —             | —          |       |

## Environment notes

- Prefer the blank side of used office paper (manual feed) — the dots
  print on fresh toner, the paper is diverted waste.
- Plain paper without dots still works for **audio + narrated notes**
  (the pen records audio; `./run stt` transcribes). Silent writing on
  plain paper is NOT captured — verified 2026-11-11 with "Plain Paper
  Test" (PRS-fa0858196, audio only, no strokes).

Also available (not stored here): the pen's Time/Date settings sheet,
https://www.livescribe.com/media/pdf/print_notebook/calculator_time_date_settings_%20sheet.pdf
— useful for keeping the pen clock (and thus capture-time stamps) right.
