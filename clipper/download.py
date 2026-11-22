"""yt-dlp wrappers: video metadata, padded section downloads, and whole-clip downloads.

YouTube needs a JavaScript runtime for yt-dlp (brew install deno) since late 2025.
"""
import json, time
from pathlib import Path
from .tools import WORK, run, yt_dlp_base

FORMAT = "bv*[height<=1080][vcodec^=avc1]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b"


def info(url: str) -> dict:
    """Metadata without downloading: duration, channel, license, heatmap ('most replayed')."""
    r = run(yt_dlp_base() + ["-J", "--skip-download", url], "yt-dlp metadata")
    j = json.loads(r.stdout)
    return {
        "video_id": j["id"], "url": j.get("webpage_url", url), "title": j.get("title", ""),
        "channel": j.get("channel") or j.get("uploader", ""), "channel_id": j.get("channel_id", ""),
        "duration": float(j.get("duration") or 0), "license": j.get("license") or "",
        "live": j.get("live_status") or "not_live", "availability": j.get("availability"),
        "views": j.get("view_count") or 0, "heatmap": j.get("heatmap") or [],
        "description": (j.get("description") or "")[:500], "tags": j.get("tags") or [],
    }


def section(url: str, start: float, end: float, out: Path) -> Path:
    """Download only [start, end] at up to 1080p. Cuts are re-encoded so t=0 is exactly `start`."""
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        return out
    tmpl = str(out.with_suffix("")) + ".%(ext)s"
    cmd = yt_dlp_base() + ["-f", FORMAT, "--merge-output-format", "mp4",
                           "--download-sections", f"*{start:.2f}-{end:.2f}", "--force-keyframes-at-cuts",
                           "-o", tmpl, url]
    for attempt in range(3):
        try:
            run(cmd, "yt-dlp section download")
            break
        except RuntimeError as e:
            # YouTube sometimes 403s a stream URL (expired/throttled). Re-running makes yt-dlp
            # fetch fresh URLs, which almost always works.
            for f in out.parent.glob(out.stem + ".*"):
                f.unlink(missing_ok=True)
            if attempt == 2:
                raise
            print(f"[download] section failed ({str(e).splitlines()[-1][:120]}), retrying")
            time.sleep(10 * (attempt + 1))
    if not out.exists():
        got = sorted(out.parent.glob(out.stem + ".*"))
        raise RuntimeError(f"yt-dlp finished but {out.name} is missing (found {[g.name for g in got]})")
    return out


def whole(url: str, source_id: str) -> Path:
    """Short sources (Twitch clips): download the whole thing."""
    d = WORK / source_id
    d.mkdir(parents=True, exist_ok=True)
    out = d / "source.mp4"
    if not out.exists():
        run(yt_dlp_base() + ["-f", FORMAT, "--merge-output-format", "mp4", "-o", str(out), url],
            "yt-dlp download")
    return out
