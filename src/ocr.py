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
import logging
import os
import shutil
import subprocess
import threading

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


PROFILES = {
    "default": Profile("default", beams=4, no_repeat_ngram=3,
                       max_line_aspect=14.0, oversize_ratio=2.5,
                       tesseract_on_ink=True),
    "legacy": Profile("legacy", beams=1, no_repeat_ngram=0,
                      max_line_aspect=None, oversize_ratio=None,
                      tesseract_on_ink=False),
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
        from PIL import Image  # noqa: F401
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


def trocr_image_to_text(image_path, prof=None):
    """Recognize a single text-line image with TrOCR."""
    from PIL import Image

    torch, processor, model = _load_trocr()
    kwargs = generate_kwargs(prof or profile())
    with telemetry.io_call(log, "trocr", ok_level=logging.DEBUG,
                           image=image_path, beams=kwargs.get("num_beams", 1)
                           ) as call:
        image = Image.open(image_path).convert("RGB")
        call["size"] = list(image.size)
        pixel_values = processor(images=image,
                                 return_tensors="pt").pixel_values
        with torch.no_grad():
            ids = model.generate(pixel_values, **kwargs)
        text = processor.batch_decode(ids, skip_special_tokens=True)[0].strip()
        call["chars"] = len(text)
    return text


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
