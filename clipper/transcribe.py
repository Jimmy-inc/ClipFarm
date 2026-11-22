"""Local speech-to-text with word timestamps (faster-whisper).

Audio is decoded with our own ffmpeg into a 16 kHz mono float array, which avoids
faster-whisper's PyAV decoder (it breaks whenever PyAV changes its API).
"""
import json, subprocess
from pathlib import Path
import numpy as np
from .tools import ffmpeg

_model = None


def _audio(video: Path) -> np.ndarray:
    r = subprocess.run([ffmpeg(), "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
                        "-f", "f32le", "-"], capture_output=True)
    if r.returncode != 0:
        if b"does not contain any stream" in r.stderr or b"Output file #0 does not contain" in r.stderr:
            return np.zeros(0, dtype=np.float32)
        raise RuntimeError(f"ffmpeg audio decode failed: {r.stderr.decode()[-500:]}")
    return np.frombuffer(r.stdout, dtype=np.float32)


def transcribe(video: Path, cfg):
    global _model
    cache = video.with_suffix(".words.json")
    if cache.exists():
        return json.loads(cache.read_text())
    audio = _audio(video)
    words = []
    if audio.size > 16000 // 2:
        from faster_whisper import WhisperModel
        if _model is None:
            _model = WhisperModel(cfg["whisper"]["model"], device=cfg["whisper"]["device"],
                                  compute_type="auto")
        segs, _ = _model.transcribe(audio, word_timestamps=True, vad_filter=True)
        words = [{"w": w.word.strip(), "s": round(w.start, 2), "e": round(w.end, 2)}
                 for seg in segs for w in (seg.words or [])]
    cache.write_text(json.dumps(words))
    return words
