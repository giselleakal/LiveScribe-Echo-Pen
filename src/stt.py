#!/usr/bin/env python3
"""Speech-to-text transcription of Paper Replay recordings.

Transcribe the pen's ``.aac`` recordings into text using the Moonshine
family (``UsefulSensors/moonshine-base``) via ``transformers`` in the
project's ``.venv``. Decoding uses the ffmpeg binary that ships with
:mod:`imageio_ffmpeg` (no system ffmpeg needed).

Everything heavy (torch, transformers, ffmpeg, the model) is imported
lazily so the base pipeline still runs without any of it installed.

Audio is expected as mono 16 kHz float32 samples (the pen's sampling is
resampled to 16 kHz by ffmpeg). Long recordings are chunked into ~30 s
windows with 1 s overlap, matching how the model was trained; chunk texts
are stitched on word boundaries (no timestamps in v1).
"""
import logging
import os
import subprocess

import telemetry

log = telemetry.get_logger("stt")

MOONSHINE_MODEL = os.environ.get(
    "MOONSHINE_MODEL", "UsefulSensors/moonshine-base")
SAMPLE_RATE = 16000
CHUNK_S = 30
STRIDE_S = 1
MAX_NEW_TOKENS = 256

_model = None


def ffmpeg_exe():
    """Path to a usable ffmpeg binary (from imageio-ffmpeg), or None."""
    try:
        import imageio_ffmpeg  # pyright: ignore[reportMissingImports]
    except Exception:
        return None
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def available():
    """True when we can both decode audio and run the ASR model."""
    if ffmpeg_exe() is None:
        return False
    try:
        import torch  # noqa: F401 # pyright: ignore[reportMissingImports]
        import transformers  # noqa: F401 # pyright: ignore[reportMissingImports]
        import numpy  # noqa: F401
    except Exception:
        return False
    return True


def _load_model():
    """Lazily load Moonshine (CPU, capped threads) and its processor."""
    global _model
    if _model is None:
        import torch  # pyright: ignore[reportMissingImports]
        from transformers import AutoProcessor, MoonshineForConditionalGeneration  # pyright: ignore[reportMissingImports]

        torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
        with telemetry.io_call(log, "moonshine.load", model=MOONSHINE_MODEL):
            processor = AutoProcessor.from_pretrained(MOONSHINE_MODEL)
            model = MoonshineForConditionalGeneration.from_pretrained(
                MOONSHINE_MODEL)
        model.eval()
        _model = (torch, processor, model)
    return _model


def _chunks(audio):
    """Yield ~CHUNK_S windows overlapping by STRIDE_S; the last may be short."""
    size = int(SAMPLE_RATE * CHUNK_S)
    step = max(1, size - int(SAMPLE_RATE * STRIDE_S))
    start = 0
    while True:
        yield audio[start:start + size]
        if start + size >= len(audio):
            return
        start += step


MAX_OVERLAP_WORDS = 5   # words the 1 s window overlap can repeat


def _stitch(texts):
    """Join chunk texts, dropping words repeated across a chunk boundary.

    Neighbouring windows share STRIDE_S of audio, so the end of one chunk
    and the start of the next can transcribe the same few words.
    """
    words = []
    for text in texts:
        new = text.split()
        limit = min(MAX_OVERLAP_WORDS, len(words), len(new))
        overlap = 0
        for k in range(limit, 0, -1):
            tail = [w.lower() for w in words[-k:]]
            if tail == [w.lower() for w in new[:k]]:
                overlap = k
                break
        words.extend(new[overlap:])
    return " ".join(words)


def transcribe(audio):
    """Transcribe a float32 16 kHz mono ``audio`` ndarray to text.

    Empty/short audio returns "". Returns cleaned text without timestamps.
    """
    if audio is None or len(audio) < SAMPLE_RATE // 2:  # < 0.5 s of audio
        return ""
    torch, processor, model = _load_model()
    texts = []
    with telemetry.io_call(log, "moonshine",
                           audio_s=round(len(audio) / SAMPLE_RATE, 1)) as call:
        texts = _transcribe_chunks(torch, processor, model, audio)
        call["chunks"] = len(texts)
    return _stitch(texts)


def _transcribe_chunks(torch, processor, model, audio):
    texts = []
    for chunk in _chunks(audio):
        feats = processor(audio=chunk, sampling_rate=SAMPLE_RATE,
                          return_tensors="pt")
        with torch.no_grad():
            ids = model.generate(
                input_values=feats.input_values,
                attention_mask=feats.attention_mask,
                max_new_tokens=MAX_NEW_TOKENS,
            )
        text = processor.batch_decode(ids, skip_special_tokens=True)[0].strip()
        if text:
            texts.append(text)
    return texts


def seq_to_floats(paths):
    """Decode one or more audio files into one concatenated float32 array.

    Per-file failures are degraded to a warning and skipped, so a mangled
    part never aborts the whole session.
    """
    import numpy as np

    chunks = []
    for p in paths:
        try:
            chunks.append(_decode_floats(p))
        except Exception as e:
            telemetry.warn(log, "audio.decode_failed",
                           "could not decode %s: %s" % (p, e), path=p)
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks)


def _decode_floats(path, samplerate=SAMPLE_RATE):
    """Decode ``path`` to mono 16 kHz float32 samples via bundled ffmpeg."""
    ff = ffmpeg_exe()
    if not ff:
        raise RuntimeError("ffmpeg unavailable; install imageio-ffmpeg")
    with telemetry.io_call(log, "ffmpeg", ok_level=logging.DEBUG,
                           path=path) as call:
        proc = subprocess.run(
            [ff, "-nostdin", "-hide_banner", "-loglevel", "error",
             "-i", path, "-f", "f32le", "-ac", "1", "-ar", str(samplerate),
             "pipe:1"],
            capture_output=True, check=False)
        call["status"] = proc.returncode
        call["bytes"] = len(proc.stdout)
        if proc.returncode != 0:
            call["outcome"] = "error"
            call["stderr_tail"] = telemetry.tail(proc.stderr)
            raise RuntimeError("ffmpeg failed (%d): %s"
                               % (proc.returncode, telemetry.tail(proc.stderr)))
    import numpy as np
    return np.frombuffer(proc.stdout, dtype=np.float32)
