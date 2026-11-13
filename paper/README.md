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

| notebook_4.pdf | 25    | 5,897,978 | ec93cc892a75       | web.archive.org/web/20171011id_/…         |
| settings_sheet.pdf | 1 | 16,951,264 | 57701c2f29f2    | calculator & time/date settings (Echo)    |

## Fixing the pen's clock (it reads ~1972 — factory default)

The pipeline does NOT need this: dumpscribe rewrites `data/time_offset`
(systime - pentime) at every sync, so all app timestamps are correct
even with the pen clock wrong. The only things wrong are the pen's own
OLED display and the PRS- ids (cosmetic).

If you want the pen display correct, use the official settings sheet:
print `settings_sheet.pdf` (same dot-paper rules — color laser, actual
size), then on the sheet fill in the current date and time and tap the
"send"/submit control with the pen. The pen's clock is then right, and
the next `./run sync` simply computes a new (small) offset — older
stamps stay correct because they were computed with the old offset.
Alternative: connect the pen once to official Echo Desktop on Windows,
which syncs the clock on connect — but its driver may claim the USB
device and interfere with the WSL usbipd attach used by `./run sync`;
remove it afterwards if you go that route.

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
