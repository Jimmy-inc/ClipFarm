"""Cut a window and reframe to 1080x1920 with word-highlighted captions and a hook title.

Layout: blurred, zoomed copy of the frame as background; gameplay scaled to `fg_height`
pixels tall and cropped to 1080 wide around `anchor` (left|center|right). fg_height 608
shows the whole 16:9 frame (small); 1000 shows the middle ~60% (bigger, usually better).
Vertical sources (9:16) are simply scaled to fill.
"""
import json, re
from pathlib import Path
from .tools import ffmpeg, ffprobe, run

YELLOW = r"{\c&H00E5FF&}"      # ASS colours are &HBBGGRR
RESET = r"{\c}"


def _ts(t):
    t = max(0.0, t)
    h, m, s = int(t // 3600), int(t % 3600 // 60), t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _clean(text):
    return text.replace("{", "(").replace("}", ")").replace("\\", "/")


def _wrap(text, width=22):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur); cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return r"\N".join(lines[:3])


def _cards(ws, max_words=3, max_chars=18, max_gap=0.6):
    cards, cur = [], []
    for w in ws:
        if cur and (len(cur) >= max_words or w["s"] - cur[-1]["e"] > max_gap
                    or len(" ".join(x["w"] for x in cur + [w])) > max_chars):
            cards.append(cur); cur = []
        cur.append(w)
    if cur:
        cards.append(cur)
    return cards


def build_ass(words, start, end, path: Path, style, hook_text="", hook_seconds=0.0):
    font, size = style.get("font", "Arial Black"), style.get("font_size", 82)
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\n\n"
            "[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,"
            "BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,"
            "Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
            f"Style: Cap,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,"
            "100,100,0,0,1,7,2,2,60,60,560,1\n"
            f"Style: Hook,{font},62,&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,"
            "100,100,0,0,3,18,0,8,90,90,300,1\n\n"
            "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n")
    lines = []
    if hook_text and hook_seconds > 0:
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(min(hook_seconds, end - start))},Hook,,0,0,0,,"
                     f"{_wrap(_clean(hook_text))}")
    ws = [{**w, "w": _clean(w["w"]).upper()} for w in words
          if w["e"] > start and w["s"] < end and w["w"].strip()]
    for card in _cards(ws):
        for i, w in enumerate(card):
            s = max(w["s"], start) - start
            e = (card[i + 1]["s"] if i + 1 < len(card) else min(w["e"], end)) - start
            if e <= s:
                continue
            text = " ".join(f"{YELLOW}{x['w']}{RESET}" if j == i else x["w"] for j, x in enumerate(card))
            lines.append(f"Dialogue: 0,{_ts(s)},{_ts(e)},Cap,,0,0,0,,{text}")
    path.write_text(head + "\n".join(lines) + "\n")


def _probe(video):
    r = run([ffprobe(), "-v", "error", "-show_entries", "stream=codec_type,width,height",
             "-of", "json", video], "ffprobe")
    streams = json.loads(r.stdout)["streams"]
    v = next(s for s in streams if s["codec_type"] == "video")
    return v["width"], v["height"], any(s["codec_type"] == "audio" for s in streams)


def render(video: Path, words, start, end, out: Path, cfg, hook_text=""):
    r = cfg["render"]
    out.parent.mkdir(parents=True, exist_ok=True)
    ass = out.with_suffix(".ass")
    build_ass(words, start, end, ass, r, hook_text, r.get("hook_seconds", 0))
    w, h, has_audio = _probe(video)

    if w / h <= 9 / 16 + 0.02:                       # already vertical
        fg = "[b]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920[fg];"
        y = "(H-h)/2"
    else:
        fh = max(608, int(r.get("fg_height", 1000)))
        fh -= fh % 2
        sw = int(round(w * fh / h / 2)) * 2
        if sw < 1080:                                 # narrower than 16:9: fit width instead
            fg, y = "[b]scale=1080:-2[fg];", "(H-h)/2-120"
        else:
            x = {"left": "0", "right": "iw-1080"}.get(r.get("anchor", "center"), "(iw-1080)/2")
            fg, y = f"[b]scale={sw}:{fh},crop=1080:{fh}:{x}:0[fg];", "(H-h)/2-120"
    fc = ("[0:v]split=2[a][b];"
          "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=30:6[bg];"
          + fg +
          f"[bg][fg]overlay=(W-w)/2:{y}[v];"
          f"[v]subtitles={ass.name}[out]")
    cmd = [ffmpeg(), "-y", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", video.resolve(),
           "-filter_complex", fc, "-map", "[out]"]
    if has_audio:
        cmd += ["-map", "0:a:0", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-ar", "48000",
                "-c:a", "aac", "-b:a", "160k"]
    cmd += ["-c:v", "libx264", "-preset", r.get("preset", "medium"), "-crf", str(r.get("crf", 20)),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", out.name]
    run(cmd, "ffmpeg render", cwd=out.parent)       # cwd so the .ass path needs no escaping
    return out


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", s)
