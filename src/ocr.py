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
"""
import os
import shutil
import subprocess

DEFAULT_LANG = "eng"
TROCR_MODEL = os.environ.get(
    "TROCR_MODEL", "microsoft/trocr-base-handwritten")

_trocr = None


def binary():
    """Return the path to the tesseract binary, or None if unavailable."""
    return shutil.which("tesseract")


def available():
    return binary() is not None


def trocr_available():
    """True when the TrOCR Python dependencies can be imported."""
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception:
        return False
    return True


def _load_trocr():
    global _trocr
    if _trocr is None:
        import torch
        from transformers import TrOCRProcessor, VisionEncoderDecoderModel

        torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
        processor = TrOCRProcessor.from_pretrained(TROCR_MODEL)
        model = VisionEncoderDecoderModel.from_pretrained(TROCR_MODEL)
        model.eval()
        _trocr = (torch, processor, model)
    return _trocr


def trocr_image_to_text(image_path, max_new_tokens=64):
    """Recognize a single text-line image with TrOCR."""
    from PIL import Image

    torch, processor, model = _load_trocr()
    image = Image.open(image_path).convert("RGB")
    pixel_values = processor(images=image, return_tensors="pt").pixel_values
    with torch.no_grad():
        ids = model.generate(pixel_values, max_new_tokens=max_new_tokens)
    return processor.batch_decode(ids, skip_special_tokens=True)[0].strip()


def image_to_text(image_path, lang=DEFAULT_LANG, psm=6, timeout=120):
    """Run tesseract on ``image_path`` and return the recognized text.

    Raises RuntimeError when tesseract is missing, fails, or times out.
    """
    exe = binary()
    if not exe:
        raise RuntimeError("tesseract not found on PATH")
    cmd = [exe, image_path, "stdout", "-l", lang, "--psm", str(psm)]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("tesseract timed out after %ss" % timeout)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError("tesseract failed (%d): %s" % (proc.returncode, err))
    return proc.stdout.decode("utf-8", "replace").strip()
