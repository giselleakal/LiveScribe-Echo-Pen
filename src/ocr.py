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
import logging
import os
import shutil
import subprocess

import telemetry

log = telemetry.get_logger("ocr")

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
        with telemetry.io_call(log, "trocr.load", model=TROCR_MODEL):
            processor = TrOCRProcessor.from_pretrained(TROCR_MODEL)
            model = VisionEncoderDecoderModel.from_pretrained(TROCR_MODEL)
        model.eval()
        _trocr = (torch, processor, model)
    return _trocr


def trocr_image_to_text(image_path, max_new_tokens=64):
    """Recognize a single text-line image with TrOCR."""
    from PIL import Image

    torch, processor, model = _load_trocr()
    with telemetry.io_call(log, "trocr", ok_level=logging.DEBUG,
                           image=image_path) as call:
        image = Image.open(image_path).convert("RGB")
        call["size"] = list(image.size)
        pixel_values = processor(images=image,
                                 return_tensors="pt").pixel_values
        with torch.no_grad():
            ids = model.generate(pixel_values, max_new_tokens=max_new_tokens)
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
