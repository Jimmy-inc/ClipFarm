"""Ask Claude to pick the best <=max_seconds window and write Shorts metadata."""
import json, re
import anthropic
from .tools import duration
from . import gemini

PROMPT = """You edit YouTube Shorts for a {game} gameplay highlights channel.
Source: a video from "{streamer}" titled "{title}", {dur:.0f}s long.
Timestamped transcript (seconds):
{transcript}

Pick the single best continuous window between {min_s} and {max_s} seconds that works as a standalone Short:
strong hook in the first 2 seconds, a clear payoff (funny, shocking, skillful), no dead air at start/end.
If the whole clip is within the limit and good, use all of it.
Return ONLY JSON:
{{"start": float, "end": float, "score": 1-10 (Short potential),
 "title": "<=70 chars, curiosity-driven, no clickbait lies, no ALL CAPS spam",
 "description": "1-2 sentences about what happens",
 "pinned_comment": "see below",
 "tags": [up to 8, include the game name]}}
{pinned}"""

def score(cand, words, video, cfg):
    dur = duration(video)
    lines, cur, t0 = [], [], None
    for w in words:
        if t0 is None: t0 = w["s"]
        cur.append(w["w"])
        if w["e"] - t0 > 4:
            lines.append(f"[{t0:.1f}] {' '.join(cur)}"); cur, t0 = [], None
    if cur: lines.append(f"[{t0:.1f}] {' '.join(cur)}")
    prompt = PROMPT.format(game=cfg["game_name"], streamer=cand["streamer"], title=cand["title"], dur=dur,
                           transcript="\n".join(lines) or "(no speech)",
                           min_s=cfg["clip"]["min_seconds"], max_s=cfg["clip"]["max_seconds"],
                           pinned=gemini.PINNED_RULE)
    msg = anthropic.Anthropic().messages.create(
        model=cfg["claude"]["model"], max_tokens=600,
        messages=[{"role": "user", "content": prompt}])
    data = json.loads(re.search(r"\{.*\}", msg.content[0].text, re.S).group(0))
    data["start"] = max(0.0, float(data["start"]))
    data["end"] = min(dur, float(data["end"]))
    if data["end"] <= data["start"]:
        raise ValueError(f"Claude returned an empty window: {data}")
    if data["end"] - data["start"] > cfg["clip"]["max_seconds"]:
        data["end"] = data["start"] + cfg["clip"]["max_seconds"]
    data["score"] = float(data.get("score", 0))
    return data
