#!/usr/bin/env python3
"""Score OCR against ground truth: character and word error rates.

Reads a manifest (default ``training/private/eval.json``, gitignored)::

    {"samples": [
        {"id": "fox", "page": "2208.1.12.25", "truth": "the quick ..."},
        {"id": "digits", "image": "digits.png", "truth": "0 1 2"}
    ]}

``page`` samples run the full pipeline (strokes from data/, line
grouping, line rendering, recognition); ``image`` samples are single text
lines sent straight to the recognizer. Rendered line images go to a
temporary directory, never to output/.

Scores are computed on normalized text (lower case, whitespace collapsed),
so they measure reading rather than formatting. Lower is better; 0.0 is
perfect. Compare profiles on the same manifest before changing a default:

    ./run ocr-eval --profile legacy
    ./run ocr-eval --profile default
"""
import argparse
import dataclasses
import glob
import json
import os
import sys
import tempfile

import convert
import ocr
import paths
import telemetry

log = telemetry.get_logger("ocr-eval")

SUMMARY = "Score OCR against ground truth: character and word error rates."

DEFAULT_MANIFEST = os.path.join(paths.REPO, "training", "private",
                                "eval.json")


@dataclasses.dataclass(frozen=True)
class Score:
    sample: str
    cer: float
    wer: float
    truth_lines: int
    found_lines: int
    seconds: float
    error: "str | None" = None


def edit_distance(a, b):
    """Levenshtein distance between two sequences."""
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1,
                               previous[j - 1] + (x != y)))
        previous = current
    return previous[-1]


def normalize(text):
    return " ".join(text.lower().split())


def error_rate(truth, found):
    """Edit distance per truth unit; 1.0+ means mostly wrong."""
    if not truth:
        return 0.0 if not found else 1.0
    return edit_distance(truth, found) / len(truth)


def score_text(truth, found):
    """(CER, WER) of ``found`` against ``truth``."""
    t, f = normalize(truth), normalize(found)
    return error_rate(t, f), error_rate(t.split(), f.split())


def load_manifest(path):
    """The manifest's samples; raises ValueError when malformed."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    samples = data.get("samples") if isinstance(data, dict) else None
    if not isinstance(samples, list):
        raise ValueError(f'{path}: expected {{"samples": [...]}}')
    for i, sample in enumerate(samples):
        if not isinstance(sample, dict) or \
                not isinstance(sample.get("truth"), str) or \
                ("page" in sample) == ("image" in sample):
            raise ValueError(f'{path}: sample {i} needs "truth" and exactly '
                             'one of "page" or "image"')
    return samples


def page_strokes(page_id, data_dir=paths.DATA):
    """All strokes of a library page, read from its .stf files."""
    files = sorted(glob.glob(os.path.join(data_dir, "data", page_id, "*",
                                          "*.stf")))
    if not files:
        raise FileNotFoundError(f"page {page_id} is not in {data_dir}")
    strokes = []
    for stf in files:
        strokes += convert.load_strokes(stf)[0]
    return strokes


def recognize_sample(sample, job, base_dir, work_dir):
    """Recognized text of one manifest sample under ``job``."""
    if "page" in sample:
        strokes = page_strokes(sample["page"])
        page_dir = tempfile.mkdtemp(dir=work_dir)
        return convert.ocr_page(strokes, page_dir, job).text
    image = os.path.join(base_dir, sample["image"])
    if job.engine == "trocr":
        return ocr.trocr_image_to_text(image, ocr.profile(job.profile))
    return ocr.image_to_text(image, lang=job.lang, psm=7)


def evaluate(samples, job, base_dir):
    """Score every sample; a failing sample is reported, not fatal."""
    scores = []
    with tempfile.TemporaryDirectory(prefix="ocr-eval-") as work_dir:
        for sample in samples:
            sid = str(sample.get("id") or sample.get("page")
                      or sample.get("image"))
            started = telemetry.monotonic()
            truth = sample["truth"]
            try:
                found = recognize_sample(sample, job, base_dir, work_dir)
                error = None
            except Exception as e:
                found, error = "", f"{type(e).__name__}: {e}"
            cer, wer = score_text(truth, found)
            scores.append(Score(
                sid, round(cer, 4), round(wer, 4),
                len(truth.splitlines()), len(found.splitlines()),
                round(telemetry.monotonic() - started, 2), error))
    return scores


def mean(values):
    values = list(values)
    return round(sum(values) / len(values), 4) if values else 0.0


def report(scores, job):
    print(f"engine={job.engine} profile={job.profile} samples={len(scores)}")
    print(f"{'sample':<24} {'CER':>7} {'WER':>7} {'lines':>6} {'seconds':>7}")
    for s in scores:
        lines = f"{s.found_lines}/{s.truth_lines}"
        error = f"  ERROR {s.error}" if s.error else ""
        print(f"{s.sample[:24]:<24} {s.cer:7.3f} {s.wer:7.3f} {lines:>6} "
              f"{s.seconds:7.2f}{error}")
    cer = mean(s.cer for s in scores)
    wer = mean(s.wer for s in scores)
    print(f"{'mean':<24} {cer:7.3f} {wer:7.3f}")


def choose_engine(requested):
    """The engine to evaluate, or None with a reason printed."""
    have = {"trocr": ocr.trocr_available(), "tesseract": ocr.available()}
    if requested == "auto":
        requested = "trocr" if have["trocr"] else "tesseract"
    if have.get(requested):
        return requested
    print(f"error: OCR engine {requested!r} is not available here; install "
          "it with './run setup --ocr' or check './run doctor'",
          file=sys.stderr)
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=SUMMARY)
    ap.add_argument("--manifest", default=DEFAULT_MANIFEST,
                    help="samples file (default: "
                         "training/private/eval.json)")
    ap.add_argument("--engine", choices=("auto", "trocr", "tesseract"),
                    default="auto")
    ap.add_argument("--profile", choices=sorted(ocr.PROFILES),
                    default=ocr.default_profile_name())
    ap.add_argument("--lang", default=ocr.DEFAULT_LANG)
    ap.add_argument("--json", action="store_true",
                    help="print machine-readable results")
    args = ap.parse_args(argv)
    try:
        samples = load_manifest(args.manifest)
    except FileNotFoundError:
        print(f"no manifest at {args.manifest}; see training/README.md to "
              "create one", file=sys.stderr)
        return 2
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not samples:
        print(f"no samples in {args.manifest}; see training/README.md to "
              "add some", file=sys.stderr)
        return 2
    engine = choose_engine(args.engine)
    if engine is None:
        return 2
    job = convert.OcrJob(engine, args.lang, 6, args.profile)
    scores = evaluate(samples, job, os.path.dirname(
        os.path.abspath(args.manifest)))
    log.info("ocr_eval.done", extra={
        "engine": engine, "profile": args.profile, "samples": len(scores),
        "cer": mean(s.cer for s in scores), "wer": mean(s.wer for s in scores),
        "errors": sum(1 for s in scores if s.error)})
    if args.json:
        print(json.dumps({"engine": engine, "profile": args.profile,
                          "cer": mean(s.cer for s in scores),
                          "wer": mean(s.wer for s in scores),
                          "samples": [dataclasses.asdict(s) for s in scores]},
                         indent=2))
    else:
        report(scores, job)
    return 1 if any(s.error for s in scores) else 0


if __name__ == "__main__":
    raise SystemExit(main())
