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

Each sample may carry ``"split": "dev"`` (the default) or ``"test"``.
Try ideas on dev; look at test only to confirm a decision, so the
settings are not fitted to the samples used to judge them.

Scores are computed on normalized text (lower case, whitespace collapsed),
so they measure reading rather than formatting. Lower is better; 0.0 is
perfect. The 95% interval comes from resampling the samples: a change
whose gain is smaller than that interval is not shown to be real.

    ./run ocr-eval --profile legacy
    ./run ocr-eval --profile default
    ./run ocr-eval --profile beam --set render=normalized   # one change
    ./run ocr-eval --split test                             # confirm once
"""
import argparse
import dataclasses
import glob
import json
import os
import random
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
SPLITS = ("dev", "test")
BOOTSTRAP_ROUNDS = 1000


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
        if sample.get("split", "dev") not in SPLITS:
            raise ValueError(f'{path}: sample {i} "split" must be "dev" or '
                             '"test"')
    return samples


def in_split(samples, split):
    """The samples of one split ("all" keeps every sample)."""
    if split == "all":
        return list(samples)
    return [s for s in samples if s.get("split", "dev") == split]


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
        return ocr.recognize([image], job.profile)
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


def interval(values, rounds=BOOTSTRAP_ROUNDS, seed=0):
    """95% bootstrap interval of the mean of ``values`` (seeded, so the
    same scores always give the same interval)."""
    values = list(values)
    if len(values) < 2:
        return mean(values), mean(values)
    rng = random.Random(seed)
    means = sorted(sum(rng.choices(values, k=len(values))) / len(values)
                   for _ in range(rounds))
    return (round(means[int(0.025 * rounds)], 4),
            round(means[int(0.975 * rounds) - 1], 4))


def summary(scores, job, split):
    """Mean scores and their intervals, as one dict."""
    cer = [s.cer for s in scores]
    wer = [s.wer for s in scores]
    return {"engine": job.engine, "profile": job.profile.name,
            "split": split, "samples": len(scores),
            "cer": mean(cer), "cer_95": interval(cer),
            "wer": mean(wer), "wer_95": interval(wer),
            "errors": sum(1 for s in scores if s.error)}


def report(scores, job, split):
    print(f"engine={job.engine} profile={job.profile.name} split={split} "
          f"samples={len(scores)}")
    print(f"{'sample':<24} {'CER':>7} {'WER':>7} {'lines':>6} {'seconds':>7}")
    for s in scores:
        lines = f"{s.found_lines}/{s.truth_lines}"
        error = f"  ERROR {s.error}" if s.error else ""
        print(f"{s.sample[:24]:<24} {s.cer:7.3f} {s.wer:7.3f} {lines:>6} "
              f"{s.seconds:7.2f}{error}")
    total = summary(scores, job, split)
    print(f"{'mean':<24} {total['cer']:7.3f} {total['wer']:7.3f}")
    print(f"95% interval: CER {total['cer_95'][0]:.3f}-{total['cer_95'][1]:.3f}"
          f", WER {total['wer_95'][0]:.3f}-{total['wer_95'][1]:.3f}")


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


def parse_args(argv):
    ap = argparse.ArgumentParser(description=SUMMARY)
    ap.add_argument("--manifest", default=DEFAULT_MANIFEST,
                    help="samples file (default: "
                         "training/private/eval.json)")
    ap.add_argument("--engine", choices=("auto", "trocr", "tesseract"),
                    default="auto")
    ap.add_argument("--profile", choices=sorted(ocr.PROFILES),
                    default=ocr.default_profile_name())
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="change one profile setting, e.g. render=plain "
                         "(repeatable)")
    ap.add_argument("--split", choices=SPLITS + ("all",), default="dev",
                    help="which samples to score (default: dev)")
    ap.add_argument("--lang", default=ocr.DEFAULT_LANG)
    ap.add_argument("--json", action="store_true",
                    help="print machine-readable results")
    return ap.parse_args(argv)


def load_samples(args):
    """The selected samples, or None with the reason printed."""
    try:
        samples = in_split(load_manifest(args.manifest), args.split)
    except FileNotFoundError:
        print(f"no manifest at {args.manifest}; see training/README.md to "
              "create one", file=sys.stderr)
        return None
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return None
    if not samples:
        print(f"no {args.split} samples in {args.manifest}; see "
              "training/README.md to add some", file=sys.stderr)
        return None
    return samples


def main(argv=None):
    args = parse_args(argv)
    samples = load_samples(args)
    engine = choose_engine(args.engine) if samples else None
    if engine is None:
        return 2
    try:
        prof = ocr.override(ocr.profile(args.profile), args.set)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    job = convert.OcrJob(engine, args.lang, 6, prof)
    scores = evaluate(samples, job,
                      os.path.dirname(os.path.abspath(args.manifest)))
    total = summary(scores, job, args.split)
    log.info("ocr_eval.done", extra=total)
    if args.json:
        print(json.dumps(dict(total, samples=[dataclasses.asdict(s)
                                              for s in scores]), indent=2))
    else:
        report(scores, job, args.split)
    return 1 if total["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
