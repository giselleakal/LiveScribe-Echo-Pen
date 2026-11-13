#!/usr/bin/env python3
"""OCR handwritten notebook pages.

Two backends are supported:

* ``tesseract`` -- the Tesseract command-line tool (lightweight, good for
  printed text, poor on cursive handwriting).
* ``trocr`` -- Microsoft TrOCR (``transformers`` + CPU PyTorch), a
  handwriting model that reads cursive lines far more reliably. Requires
  the project's ``.venv``.

The Tesseract backend is a subprocess wrapper; the TrOCR backend imports
``torch``/``transformers`` lazily so the base pipeline still runs without
them.

How pages are segmented and decoded is chosen by a named *profile*
(``ECHO_OCR_PROFILE`` or ``--ocr-profile``). ``legacy`` reproduces the
original behaviour exactly, so ``./run ocr-eval`` can compare the two on
the same samples before a default changes.
"""
import dataclasses
import functools
import json
import logging
import os
import re
import shutil
import subprocess
import threading

import paths
import telemetry

log = telemetry.get_logger("ocr")

DEFAULT_LANG = "eng"
TROCR_MODEL = os.environ.get(
    "TROCR_MODEL", "microsoft/trocr-base-handwritten")

_trocr = None
_trocr_lock = threading.Lock()   # the model is loaded once per process


@dataclasses.dataclass(frozen=True)
class Profile:
    """Segmentation and decoding choices that affect recognized text."""
    name: str
    beams: int                 # TrOCR beam width; 1 is greedy decoding
    no_repeat_ngram: int       # block repeated n-grams (0 = off); stops loops
    max_line_aspect: "float | None"   # split wider lines at word gaps
    oversize_ratio: "float | None"    # taller strokes don't start a line
    tesseract_on_ink: bool     # render only the inked area for Tesseract
    render: str = "plain"      # "normalized": lines drawn like TrOCR's data
    lines: str = "vertical"    # "temporal": line breaks from pen order
    # pen widths (fractions of letter height) to read each normalized line
    # with; several readings need beams > 1 to be compared
    pen_widths: tuple = (0.09,)
    # prefer in-vocabulary words among candidates scoring within this much
    # (mean log-probability per token) of the best; 0 = off
    vocab_margin: float = 0.0


CHOICES = {"render": ("plain", "normalized"),
           "lines": ("vertical", "temporal")}

_BEAM = Profile("beam", beams=4, no_repeat_ngram=3, max_line_aspect=14.0,
                oversize_ratio=2.5, tesseract_on_ink=True)
PROFILES = {
    "legacy": Profile("legacy", beams=1, no_repeat_ngram=0,
                      max_line_aspect=None, oversize_ratio=None,
                      tesseract_on_ink=False),
    "beam": _BEAM,
    "default": dataclasses.replace(
        _BEAM, name="default", render="normalized", lines="temporal",
        pen_widths=(0.06, 0.09, 0.12), vocab_margin=0.15),
}


def default_profile_name():
    """ECHO_OCR_PROFILE (any case), or "default" with a warning if unknown."""
    raw = os.environ.get("ECHO_OCR_PROFILE", "default")
    name = raw.strip().lower()
    if name in PROFILES:
        return name
    telemetry.warn(log, "ocr.unknown_profile",
                   "unknown ECHO_OCR_PROFILE %r; using 'default' (choices: %s)"
                   % (raw, ", ".join(sorted(PROFILES))), requested=raw)
    return "default"


def profile(name=None):
    """The named profile (default: ECHO_OCR_PROFILE, else "default")."""
    return PROFILES[name or default_profile_name()]


def profile_key(prof):
    """Every setting of ``prof`` as one string: the OCR reuse key, so
    changing what a profile does re-transcribes pages once."""
    return json.dumps(dataclasses.asdict(prof), sort_keys=True)


def override(prof, pairs):
    """``prof`` with ``field=value`` changes, for one-at-a-time tests.

    Raises ValueError for an unknown field or a value of the wrong kind.
    """
    fields = {f.name for f in dataclasses.fields(Profile)} - {"name"}
    changes = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep or key not in fields:
            raise ValueError("unknown profile setting %r (choices: %s)"
                             % (pair, ", ".join(sorted(fields))))
        changes[key] = _parse_like(key, raw, getattr(prof, key))
    name = "+".join([prof.name] + list(pairs))
    return dataclasses.replace(prof, name=name, **changes)


def _parse_like(key, raw, current):
    """``raw`` converted to the kind of value ``current`` is."""
    if key in CHOICES:
        if raw not in CHOICES[key]:
            raise ValueError("%s must be one of %s" % (key, CHOICES[key]))
        return raw
    if isinstance(current, bool):
        if raw.lower() not in ("true", "false", "1", "0"):
            raise ValueError("%s must be true or false" % key)
        return raw.lower() in ("true", "1")
    if isinstance(current, tuple):
        return tuple(float(v) for v in raw.split(","))
    if raw.lower() == "none":
        return None
    return int(raw) if isinstance(current, int) else float(raw)


def generate_kwargs(prof, max_new_tokens=64):
    """Arguments for TrOCR's ``model.generate`` under ``prof``."""
    kwargs = {"max_new_tokens": max_new_tokens}
    if prof.beams > 1:
        kwargs.update(num_beams=prof.beams, early_stopping=True)
    if prof.no_repeat_ngram:
        kwargs["no_repeat_ngram_size"] = prof.no_repeat_ngram
    return kwargs


def binary():
    """Return the path to the tesseract binary, or None if unavailable."""
    return shutil.which("tesseract")


def available():
    return binary() is not None


def trocr_available():
    """True when the TrOCR Python dependencies can be imported."""
    try:
        import torch  # noqa: F401 # pyright: ignore[reportMissingImports]
        import transformers  # noqa: F401 # pyright: ignore[reportMissingImports]
        from PIL import Image  # noqa: F401 # pyright: ignore[reportMissingImports]
    except Exception:
        return False
    return True


def _load_trocr():
    global _trocr
    with _trocr_lock:
        if _trocr is None:
            _trocr = _build_trocr()
    return _trocr


def _build_trocr():
    import torch  # pyright: ignore[reportMissingImports]
    from transformers import TrOCRProcessor, VisionEncoderDecoderModel  # pyright: ignore[reportMissingImports]

    torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
    with telemetry.io_call(log, "trocr.load", model=TROCR_MODEL):
        processor = TrOCRProcessor.from_pretrained(TROCR_MODEL)
        model = VisionEncoderDecoderModel.from_pretrained(TROCR_MODEL)
    model.eval()
    return torch, processor, model


@dataclasses.dataclass(frozen=True)
class Candidate:
    """One reading of a line: its text and TrOCR's confidence in it
    (mean log-probability per token; 0.0 when unscored)."""
    text: str
    score: float = 0.0


def trocr_image_to_text(image_path, prof=None):
    """Recognize a single text-line image with TrOCR."""
    return recognize([image_path], prof or profile())


def recognize(image_paths, prof):
    """The best reading of one line, rendered as one or more images."""
    with telemetry.io_call(log, "trocr", ok_level=logging.DEBUG,
                           images=len(image_paths), beams=prof.beams) as call:
        candidates = trocr_candidates(image_paths, prof)
        text = choose(candidates, prof.vocab_margin,
                      vocabulary() if prof.vocab_margin else frozenset())
        call.update(candidates=len(candidates), chars=len(text),
                    top_chosen=bool(candidates) and
                    text == max(candidates, key=lambda c: c.score).text)
    return text


def _open_rgb(path):
    from PIL import Image  # pyright: ignore[reportMissingImports]
    return Image.open(path).convert("RGB")


def trocr_candidates(image_paths, prof):
    """Scored readings of a line's images: the top one per image, or the
    n-best per image when a vocabulary prior will choose among them.

    Only beam search scores its readings, so with ``beams < 2`` (or one
    image and no prior) just the first image is read, unscored.
    """
    torch, processor, model = _load_trocr()
    images = [_open_rgb(p) for p in image_paths]
    kwargs = generate_kwargs(prof)
    simple = len(images) == 1 and not prof.vocab_margin
    if simple or prof.beams < 2:
        return _top_readings(torch, processor, model, images[:1], kwargs)
    n_best = prof.beams if prof.vocab_margin else 1
    try:
        return _scored_readings(torch, processor, model, images,
                                dict(kwargs, num_return_sequences=n_best))
    except (TypeError, ValueError, AttributeError) as e:
        _warn_unscored(e)
        return _top_readings(torch, processor, model, images[:1], kwargs)


def _top_readings(torch, processor, model, images, kwargs):
    pixel_values = processor(images=images, return_tensors="pt").pixel_values
    with torch.no_grad():
        ids = model.generate(pixel_values, **kwargs)
    return [Candidate(t.strip())
            for t in processor.batch_decode(ids, skip_special_tokens=True)]


def _scored_readings(torch, processor, model, images, kwargs):
    pixel_values = processor(images=images, return_tensors="pt").pixel_values
    with torch.no_grad():
        out = model.generate(pixel_values, output_scores=True,
                             return_dict_in_generate=True, **kwargs)
    texts = processor.batch_decode(out.sequences, skip_special_tokens=True)
    return [Candidate(t.strip(), float(score))
            for t, score in zip(texts, out.sequences_scores, strict=True)]


_warned_unscored = []


def _warn_unscored(error):
    if _warned_unscored:
        return
    _warned_unscored.append(True)
    telemetry.warn(log, "ocr.scores_unavailable",
                   "this transformers version can't score candidates (%s); "
                   "reading each line once" % error,
                   exc_type=type(error).__name__)


WORD = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")


def unknown_words(text, words):
    """How many words of ``text`` are not in ``words`` (digits don't count)."""
    return sum(1 for w in WORD.findall(text) if w.lower() not in words)


def choose(candidates, margin, words):
    """The text of the best candidate.

    Highest score wins, except that among candidates scoring within
    ``margin`` of the best, the one with the fewest out-of-vocabulary
    words is preferred. The prior only breaks near-ties between readings
    the model itself proposed; it never invents text.
    """
    if not candidates:
        return ""
    best = max(candidates, key=lambda c: c.score)
    if not margin or not words:
        return best.text
    close = [c for c in candidates if best.score - c.score <= margin]
    return min(close, key=lambda c: (unknown_words(c.text, words),
                                     -c.score)).text


WORD_LISTS = ("/usr/share/dict/words", "/usr/share/dict/american-english")
USER_VOCABULARY = os.path.join(paths.REPO, "training", "private",
                               "vocabulary.txt")


@functools.lru_cache(maxsize=1)
def vocabulary():
    """Known words, lower case: the system word list plus the private
    training/private/vocabulary.txt (names, acronyms; never committed).
    Empty when neither exists, which turns the prior off."""
    words = set()
    for path in _first_existing(WORD_LISTS) + [USER_VOCABULARY]:
        words.update(_read_words(path))
    return frozenset(words)


def _first_existing(candidates):
    return [p for p in candidates if os.path.exists(p)][:1]


def _read_words(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return {w.lower() for line in f for w in WORD.findall(line)}
    except OSError:
        return set()


def image_to_text(image_path, lang=DEFAULT_LANG, psm=6, timeout=120):
    """Run tesseract on ``image_path`` and return the recognized text.

    Raises RuntimeError when tesseract is missing, fails, or times out.
    """
    exe = binary()
    if not exe:
        raise RuntimeError("tesseract not found on PATH")
    cmd = [exe, image_path, "stdout", "-l", lang, "--psm", str(psm)]
    with telemetry.io_call(log, "tesseract", ok_level=logging.DEBUG,
                           image=image_path, lang=lang, psm=psm) as call:
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            call["outcome"] = "timeout"
            raise RuntimeError("tesseract timed out after %ss" % timeout) \
                from e
        call["status"] = proc.returncode
        if proc.returncode != 0:
            call["outcome"] = "error"
            call["stderr_tail"] = telemetry.tail(proc.stderr)
            raise RuntimeError("tesseract failed (%d): %s"
                               % (proc.returncode, telemetry.tail(proc.stderr)))
        text = proc.stdout.decode("utf-8", "replace").strip()
        call["chars"] = len(text)
    return text
