"""Judge a downloaded Twitch clip with the OpenAI API (env OPENAI_API_KEY) instead of Gemini.

OpenAI models take images, not video: the clip goes in as one small frame per second, each labelled
with its timestamp, plus the Whisper transcript we already made for the captions. Same prompt and
same answer shape as gemini.judge_clip, so the rest of the pipeline doesn't care which one ran.
Paid per token (no free tier); a 60 s clip is ~60 low-detail frames, well under a cent on a small model.
"""
import base64, json, os, shutil
from . import gemini
from .tools import duration, ffmpeg, run

_client = None


def configured():
    return bool(os.environ.get("OPENAI_API_KEY"))


def client():
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(max_retries=2)          # reads OPENAI_API_KEY
    return _client


def _strict(schema):
    """OpenAI strict structured output: every property required, no extra keys, no numeric bounds."""
    if schema.get("type") == "object":
        props = {k: _strict(v) for k, v in schema["properties"].items()}
        return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}
    if schema.get("type") == "array":
        return {"type": "array", "items": _strict(schema["items"])}
    return {k: v for k, v in schema.items() if k not in ("minimum", "maximum")}


def _frames(path, fps, width):
    d = path.with_name("openai_frames")
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir()
    run([ffmpeg(), "-v", "error", "-i", str(path), "-vf", f"fps={fps},scale={width}:-2",
         "-q:v", "5", str(d / "f%04d.jpg")], "ffmpeg frames")
    return sorted(d.glob("f*.jpg"))


def _transcript(words):
    lines, cur, t0 = [], [], None
    for w in words:
        if t0 is None:
            t0 = w["s"]
        cur.append(w["w"])
        if w["e"] - t0 > 4:
            lines.append(f"[{t0:.1f}s] {' '.join(cur)}")
            cur, t0 = [], None
    if cur:
        lines.append(f"[{t0:.1f}s] {' '.join(cur)}")
    return "\n".join(lines) or "(no speech)"


def judge_clip(path, words, cand, cfg):
    t = cfg["twitch"]
    dur = duration(path)
    fps = min(t.get("openai_fps", 1), t.get("openai_max_frames", 60) / max(dur, 1))
    frames = _frames(path, fps, t.get("openai_frame_width", 512))
    content = [{"type": "input_text", "text": gemini.clip_prompt(cand, cfg, dur) +
                "\n\nYou get the clip as still frames (timestamp before each) and the speech transcript, "
                "not as video. Infer the action from how consecutive frames change.\n\nTranscript:\n"
                + _transcript(words)}]
    for i, f in enumerate(frames):
        content.append({"type": "input_text", "text": f"t={i / fps:.1f}s"})
        content.append({"type": "input_image", "detail": t.get("openai_detail", "low"),
                        "image_url": "data:image/jpeg;base64," + base64.b64encode(f.read_bytes()).decode()})
    r = client().responses.create(
        model=t.get("openai_model", "gpt-6-luna"), timeout=180,
        input=[{"role": "user", "content": content}],
        text={"format": {"type": "json_schema", "name": "twitch_clip", "strict": True,
                         "schema": _strict(gemini.CLIP_SCHEMA)}})
    u = getattr(r, "usage", None)
    print(f"[openai] {r.model} judged {len(frames)} frames"
          + (f", {u.input_tokens} in / {u.output_tokens} out tokens" if u else ""))
    return gemini.clean_window(json.loads(r.output_text), dur, cfg)
