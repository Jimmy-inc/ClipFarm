"""Locate external binaries and run them with useful error messages."""
import re
import os, shutil, subprocess, sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "work"
OUTPUT = ROOT / "output"

# Homebrew's plain `ffmpeg` formula is built without libass, so the `subtitles`
# filter (burned-in captions) is missing. `ffmpeg-full` has it but is keg-only.
_FFMPEG_CANDIDATES = [
    "/opt/homebrew/opt/ffmpeg-full/bin",
    "/usr/local/opt/ffmpeg-full/bin",
]


@lru_cache
def ffmpeg_dir() -> str:
    env = os.environ.get("FFMPEG_DIR")
    for d in ([env] if env else []) + _FFMPEG_CANDIDATES:
        if d and (Path(d) / "ffmpeg").exists():
            return d
    found = shutil.which("ffmpeg")
    if not found:
        sys.exit("ffmpeg not found. On macOS: brew install ffmpeg-full")
    return str(Path(found).parent)


def ffmpeg() -> str:
    return str(Path(ffmpeg_dir()) / "ffmpeg")


def ffprobe() -> str:
    return str(Path(ffmpeg_dir()) / "ffprobe")


def run(cmd, what: str, **kw):
    """subprocess.run that raises with the tail of stderr instead of hiding it."""
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)
    if r.returncode != 0:
        tail = "\n".join((r.stderr or r.stdout or "").strip().splitlines()[-15:])
        raise RuntimeError(f"{what} failed (exit {r.returncode}):\n{tail}")
    return r


def duration(path) -> float:
    # Container duration is "N/A" for some cut sections; fall back to the streams' own durations.
    r = run([ffprobe(), "-v", "error", "-show_entries", "format=duration:stream=duration",
             "-of", "default=nw=1:nk=1", str(path)], "ffprobe")
    vals = [float(x) for x in r.stdout.split() if re.fullmatch(r"[\d.]+", x)]
    if not vals:
        raise RuntimeError(f"ffprobe found no duration in {path}")
    return max(vals)


def yt_dlp_base():
    return [sys.executable, "-m", "yt_dlp", "--no-warnings", "--ffmpeg-location", ffmpeg_dir()]


def hms(t: float) -> str:
    t = max(0, int(round(t)))
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def parse_ts(v) -> float:
    """Accept 83, 83.5, "83", "1:23", "01:23", "1:01:23", "83s"."""
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().rstrip("s")
    parts = s.split(":")
    if len(parts) > 3:
        raise ValueError(f"bad timestamp {v!r}")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return total
