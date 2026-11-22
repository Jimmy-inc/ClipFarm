"""Gemini watches the YouTube video directly (no download) to audit it and find clip-able moments.

Uses the google-genai Interactions API: a public YouTube URL is passed as a `video` input,
sampled at a fixed fps ("static" processing). Needs env var GEMINI_API_KEY.

Docs (checked Nov 2026): YouTube URLs must be public; free tier caps at 8 h of YouTube
video per day; ~1 h fits at high media resolution or ~3 h at low resolution in 1M context.
Long videos are split into chunks with start_offset/end_offset so each call stays small.
"""
import json, os, re, time
from . import tools
from .tools import hms, parse_ts

_client = None
_active = {"key": None, "file": None}
_rotation = {"hours": 8}      # gemini.key_rotation_hours; updated via configure(cfg)
_verbose = False


def configure(cfg):
    _rotation["hours"] = cfg.get("gemini", {}).get("key_rotation_hours", 8)


def _read_env(path):
    out = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.split(" #")[0].strip().strip('"').strip("'")
    return out


def _key_sets():
    """[(file name, api key)]: the Gemini keys in .env, then in 2.env, 3.env, ... (number order).
    Only the GEMINI_API_KEY line of the numbered files is used. A file without a key, or with a key
    that's already in the list, is skipped."""
    files = [tools.ROOT / ".env"] + sorted((f for f in tools.ROOT.glob("*.env") if f.stem.isdigit()),
                                           key=lambda f: int(f.stem))
    sets, keys = [], set()
    for f in files:
        kv = _read_env(f) if f.exists() else {}
        if f.name == ".env" and not kv.get("GEMINI_API_KEY"):       # key exported in the shell instead
            kv = {"GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY", "")}
        key = kv.get("GEMINI_API_KEY")
        if key and key not in keys:
            keys.add(key)
            sets.append((f.name, key))
    return sets


def _current_key():
    """The key for the current time slot. Slots follow the clock (UTC, from midnight Jan 1 1970),
    so a restart keeps the same key, and new or edited 2.env, 3.env ... are picked up without a restart."""
    sets = _key_sets()
    if not sets:
        raise RuntimeError("no Gemini keys: fill GEMINI_API_KEY in .env")
    hours = float(_rotation["hours"] or 0)
    i = int(time.time() // (hours * 3600)) % len(sets) if hours > 0 else 0
    return sets[i]


def client():
    global _client
    name, key = _current_key()
    if _client is None or _active["key"] != key:
        from google import genai
        from google.genai import types
        if _active["key"] is not None:
            print(f"[gemini] switched to the key in {name}")
        # attempts=1: no hidden SDK retries (they made each failure take ~2 min); _ask decides
        # when to retry and when to switch models.
        _client = genai.Client(api_key=key, http_options=types.HttpOptions(
            retry_options=types.HttpRetryOptions(attempts=1)))
        _active.update(key=key, file=name)
    return _client


AUDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_target_game_gameplay": {"type": "boolean"},
        "is_original_creator_content": {"type": "boolean"},
        "clip_potential": {"type": "integer", "minimum": 1, "maximum": 10},
        "estimated_moments": {"type": "integer", "minimum": 0},
        "has_commentary": {"type": "boolean"},
        "problems": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["is_target_game_gameplay", "is_original_creator_content", "clip_potential", "estimated_moments",
                 "has_commentary", "problems", "summary"],
}

PINNED_RULE = ("pinned_comment: the comment pinned under the Short, 1-3 short sentences in plain text. Give the "
               "context the Short itself doesn't show: who is playing, what is going on or what led up to it, why it "
               "is funny or impressive. You may end with one short question that invites replies. No hashtags, no "
               "links, no emoji spam, nothing made up, and no story spoilers beyond what the clip shows.")


OOC_RULE = """TOP PRIORITY, "out of context" moments (category out_of_context): the streamer (or a friend or an NPC)
  says something that, heard on its own, sounds inappropriate, rude, suggestive, unhinged or alarming, but is
  innocent in context (they mean the game, a car, a mission, an NPC). Streamers often say lines like this on purpose
  ("clipfarming"); those count too. Listen to the audio: the exact words carry it. For these:
  - cut so the line lands WITHOUT the setup that explains it; start 1-3 s before the line, end right after the
    line or the reaction to it. {ooc_min}-15 s is ideal, shorter than the usual minimum is fine.
  - score them high when the line is clear and funny out of context; they are the most viral Shorts.
  - title: tease or quote the line without explaining it (e.g. "He did NOT mean it like that"), still truthful.
  - innuendo and accidental lines are brand-safe (is_brand_safe true) unless they contain slurs or explicit
    sexual description."""


def ooc_rule(c):
    return OOC_RULE.format(ooc_min=c.get("out_of_context_min_seconds", 5))


def min_seconds(m, c):
    """Out-of-context lines are allowed to be shorter than the usual minimum."""
    if (m or {}).get("category") == "out_of_context":
        return c.get("out_of_context_min_seconds", 5)
    return c["min_seconds"]


MOMENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "moments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "string", "description": "HH:MM:SS in the full video"},
                    "end": {"type": "string", "description": "HH:MM:SS in the full video"},
                    "score": {"type": "integer", "minimum": 1, "maximum": 10},
                    "category": {"type": "string",
                                 "enum": ["out_of_context", "funny", "chaos", "skill", "fail", "glitch",
                                          "story", "reaction", "discovery"]},
                    "is_brand_safe": {"type": "boolean"},
                    "hook": {"type": "string", "description": "what grabs attention in the first 2 s"},
                    "payoff": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "pinned_comment": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["start", "end", "score", "category", "is_brand_safe", "hook", "payoff",
                             "title", "description", "pinned_comment", "tags"],
            },
        }
    },
    "required": ["moments"],
}

AUDIT_PROMPT = """You triage YouTube videos for a {game} Shorts channel.
Video: "{title}" by {channel}, {dur}. Watch it and answer honestly:
- is_target_game_gameplay: is this real {game} gameplay (not a different game, trailers re-cut, news talk, or fake "leaks")?
- is_original_creator_content: true only if the person playing is the channel itself ("{channel}"): their own
  stream VOD, own recording, own commentary over own play. false for compilations or "best clips" of other
  streamers, fan channels cutting someone else's streams (e.g. a channel named after a pro player it isn't),
  tournament or league broadcasts, and anything reuploaded. If unsure, look for streamer overlays, watermarks or
  webcams that don't match the channel name.
- clip_potential 1-10: how many strong standalone Shorts moments (funny, chaotic, skilful, fails, glitches,
  big reactions, and especially "out of context" lines: the streamer saying something that sounds inappropriate
  or wild on its own but is innocent in context) does it contain? 1 = none, 10 = packed.
- estimated_moments: rough count of such moments.
- has_commentary: does the creator talk over the gameplay?
- problems: anything that makes clipping a bad idea: copyrighted music playing, it is itself a compilation/reupload
  of other creators, mostly menus/loading/AFK, heavy on-screen overlays covering gameplay, sexual content, etc.
- summary: one sentence."""

MOMENTS_PROMPT = """You edit YouTube Shorts for a {game} gameplay highlights channel.
Video: "{title}" by {channel}, total length {total}. You are looking at the part from {c0} to {c1}.
{heat}
Find up to {k} of the best moments in this part that work as standalone vertical Shorts:
- each {min_s}-{max_s} seconds long, one continuous window
- a hook in the first 2 seconds (start right before the action, not during the build-up)
- a clear payoff (funny, shocking, skilful, chaotic) and end right after it lands, no dead air
- skip anything that needs earlier context to make sense (out-of-context lines are the exception: they are
  meant to be heard without it), menus, loading screens, and copyrighted music
- {ooc}
- is_brand_safe: false if it shows nudity or sexual content (strip clubs, lap dances), slurs or hate speech,
    real-money gambling or case opening, or real-life graphic violence. Swearing and in-game violence are fine.
    This is only a flag: such clips are kept in a separate folder for manual review, so never lower the score
    or skip a great moment because of it.
Use timestamps of the FULL video (HH:MM:SS), not relative to this part.
title: <=70 chars, curiosity-driven, true to what happens, no ALL CAPS spam, no emoji spam.
description: 1-2 sentences. tags: up to 8, include the game name.
{pinned}
Score 1-10 for Short potential; only include moments scoring 6 or more. Return an empty list if there are none."""


def _notes(cfg, clip_rules=False):
    """Profile text appended to prompts: facts about the game (game_notes) and, for picking
    moments, extra rules such as avoiding story spoilers (clip_rules)."""
    gm = cfg["gemini"]
    out = f"\nAbout {cfg['game_name']}: {gm['game_notes']}" if gm.get("game_notes") else ""
    if clip_rules and gm.get("clip_rules"):
        out += f"\nAlso: {gm['clip_rules']}"
    return out


def _video_part(url, fps, resolution, start=None, end=None, mode="static"):
    if mode == "agentic":
        return {"type": "video", "uri": url, "resolution": resolution, "processing": "agentic"}
    proc = {"type": "static", "fps": fps}
    if start is not None:
        proc["start_offset"] = f"{int(start)}s"
    if end is not None:
        proc["end_offset"] = f"{int(end)}s"
    return {"type": "video", "uri": url, "resolution": resolution, "processing": proc}


_OVERLOAD = ("503", "high demand", "UNAVAILABLE", "overloaded", "429", "RESOURCE_EXHAUSTED",
             "Cannot fetch content", "did not respond",   # Gemini's YouTube fetch hiccups
             "Error code: 500", "Internal error")         # server-side, transient
_FATAL = ("400", "403", "404", "INVALID_ARGUMENT", "PERMISSION")


class QuotaExhausted(RuntimeError):
    pass


class GeminiBusy(RuntimeError):
    """Every model stayed overloaded (503) through all retry rounds. Not the video's fault."""


# Models that hit a daily cap, remembered until the reset: {(model, uses_tools): unix time}.
# Free tier is ~20 requests/day PER MODEL, so a capped model is skipped and the next one used.
_capped = {}
_DAILY = ("per day", "exceeded your current quota", "billing")


def all_capped(models, tools=False):
    """True if every model already hit its daily cap for the active key (known from earlier calls in this process)."""
    _, key = _current_key()
    models = [models] if isinstance(models, str) else list(models)
    return all(_capped.get((key, m, tools), 0) >= time.time() for m in models)


def _reset_time():
    import datetime as dt, zoneinfo
    pt = zoneinfo.ZoneInfo("America/Los_Angeles")
    now = dt.datetime.now(pt)
    return (now + dt.timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0).timestamp()


def _ask(models, prompt, video_part, schema, rounds=3, timeout=300, tools=None):
    """Try each model in turn. Daily cap hit -> skip that model until the reset.
    Overloaded (503/429) -> next model now. Other transient errors -> retry the same model once.
    After a full round, back off and repeat. Every model capped -> QuotaExhausted."""
    models = [models] if isinstance(models, str) else list(models)
    last = None
    for rnd in range(rounds):
        _, key = _current_key()
        live = [m for m in models if _capped.get((key, m, bool(tools)), 0) < time.time()]
        if not live:
            raise QuotaExhausted(f"Free-tier daily request cap reached on every model in {models}. "
                                 "Resets at midnight Pacific; enable billing in Google AI Studio to lift it.")
        for model in live:
            for same_model_try in range(2):
                t0 = time.time()
                try:
                    req = dict(model=model, timeout=timeout,
                               input=[{"type": "text", "text": prompt}] + ([video_part] if video_part else []),
                               response_format={"type": "text", "mime_type": "application/json",
                                                "schema": schema})
                    if tools:
                        req["tools"] = tools
                    it = client().interactions.create(**req)
                    u = getattr(it, "usage", None)
                    print(f"[gemini] {model} answered in {time.time() - t0:.0f}s"
                          + (f", usage {u}" if u and _verbose else ""))
                    return _parse_json(it.output_text or "")
                except Exception as e:
                    last, msg = e, str(e)
                    if any(k in msg for k in _DAILY) and ("429" in msg or "quota" in msg.lower()):
                        _capped[(key, model, bool(tools))] = _reset_time()
                        print(f"[gemini] {model}{' + search' if tools else ''} hit its daily cap; "
                              "skipping it until the reset")
                        break
                    print(f"[gemini] {model} failed after {time.time() - t0:.0f}s: "
                          f"{type(e).__name__}: {msg[:160]}")
                    if any(c in msg for c in _FATAL) and not any(c in msg for c in _OVERLOAD):
                        raise             # bad request, private video, bad key: retrying won't help
                    if any(c in msg for c in _OVERLOAD):
                        break             # this model is busy: try the next one now
                    time.sleep(5)
        if all(_capped.get((key, m, bool(tools)), 0) >= time.time() for m in models):
            continue                      # loop top raises QuotaExhausted
        if rnd + 1 < rounds:
            wait = 30 * 2 ** rnd
            print(f"[gemini] all models busy, waiting {wait}s")
            time.sleep(wait)
    _, key = _current_key()
    if all(_capped.get((key, m, bool(tools)), 0) >= time.time() for m in models):
        raise QuotaExhausted(f"Free-tier daily request cap reached on every model in {models}. "
                             "Resets at midnight Pacific; enable billing in Google AI Studio to lift it.")
    if last is not None and any(c in str(last) for c in _OVERLOAD):
        raise GeminiBusy(f"Gemini overloaded on {models} after {rounds} rounds: {str(last)[:200]}")
    raise RuntimeError(f"Gemini failed on {models} after {rounds} rounds: {last}")


def _parse_json(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise ValueError(f"no JSON in Gemini reply: {text[:200]!r}")
        return json.loads(m.group(0))


def audit(video, cfg):
    configure(cfg)
    g = cfg["gemini"]
    total = video["duration"]
    # Long videos (streams) start with "starting soon" screens: audit from minute 5 onwards.
    start = g.get("skip_intro_minutes", 5) * 60 if total > 45 * 60 else 0
    end = min(total, start + g["audit_minutes"] * 60) if g["audit_minutes"] else None
    part = _video_part(video["url"], g["audit_fps"], "low", start if end else None, end, g["processing"])
    prompt = AUDIT_PROMPT.format(game=cfg["game_name"], title=video["title"], channel=video["channel"],
                                 dur=hms(total))
    if end and (end < total or start):
        prompt += f"\n(You are seeing only {hms(start)}-{hms(end)}; judge from that.)"
    prompt += _notes(cfg)
    return _ask(g["audit_model"], prompt, part, AUDIT_SCHEMA)


def _heat_hint(heatmap, c0, c1, n=5):
    if not heatmap:
        return ""
    peaks = sorted((h for h in heatmap if c0 <= h["start_time"] < c1),
                   key=lambda h: h["value"], reverse=True)[:n]
    if not peaks:
        return ""
    spans = ", ".join(f"{hms(h['start_time'])}-{hms(h['end_time'])}" for h in sorted(peaks, key=lambda h: h["start_time"]))
    return f"Viewers rewatch these spans most (YouTube 'most replayed'): {spans}. Look closely there, but judge for yourself."


def scan_spans(total, heatmap, g):
    """Which parts of the video Gemini watches. Short videos: all of it, in chunk_minutes pieces.
    Longer than max_scan_minutes: the busiest window by 'most replayed' data, or else
    `samples` evenly spread windows after the intro. Keeps Gemini usage per video bounded."""
    chunk = g["chunk_minutes"] * 60
    budget = g.get("max_scan_minutes", 0) * 60
    if not budget or total <= budget:
        spans, c0 = [], 0.0
        while c0 < total:
            spans.append((c0, min(total, c0 + chunk)))
            c0 += chunk
        return spans
    if heatmap:
        best, best_heat = 0.0, -1.0
        for h in heatmap:                          # candidate windows start at each heat bucket
            s0 = min(h["start_time"], total - budget)
            heat = sum(x["value"] * (x["end_time"] - x["start_time"]) for x in heatmap
                       if s0 <= x["start_time"] < s0 + budget)
            if heat > best_heat:
                best, best_heat = s0, heat
        spans, c0 = [], best
        while c0 < best + budget - 1:
            spans.append((c0, min(best + budget, c0 + chunk)))
            c0 += chunk
        return spans
    n = max(1, g.get("samples", 3))
    piece = budget / n
    lo = min(g.get("skip_intro_minutes", 5) * 60, max(0.0, total - budget))
    step = (total - lo - piece) / max(1, n - 1)
    return [(lo + i * step, lo + i * step + piece) for i in range(n)]


def find_moments(video, cfg, heatmap=None):
    """Return moments sorted by score, with absolute float start/end seconds."""
    configure(cfg)
    g, c = cfg["gemini"], cfg["clip"]
    total = video["duration"]
    chunk = g["chunk_minutes"] * 60
    k = cfg["clip"]["max_clips_per_video"]
    out = []
    spans = scan_spans(total, heatmap, g)
    if len(spans) > 1 or spans[0] != (0.0, total):
        whole = spans[0][0] == 0.0 and spans[-1][1] >= total
        print(f"[moments] scanning {len(spans)} chunk(s): " + ", ".join(f"{hms(a)}-{hms(b)}" for a, b in spans)
              + f" of {hms(total)}"
              + (" (the whole video)" if whole else " (most replayed)" if heatmap else " (samples)"))
    # Each finished chunk is saved, so a long stream interrupted by the daily quota (or a crash)
    # resumes at the next unwatched chunk instead of starting over.
    cache = tools.WORK / "moment_chunks" / f"{video.get('video_id', 'video')}.json"
    done = {}
    if len(spans) > 1 and cache.exists():
        try:
            done = json.loads(cache.read_text())
        except ValueError:
            done = {}
        if done:
            print(f"[moments] resuming: {len(done)} of {len(spans)} chunks already watched")
    for i, (c0, c1) in enumerate(spans, 1):
        key = f"{c0:.0f}-{c1:.0f}"
        if key in done:
            out += done[key]
            continue
        if len(spans) > 1:
            print(f"[moments] chunk {i}/{len(spans)}: {hms(c0)}-{hms(c1)}")
        part = _video_part(video["url"], g["moments_fps"], g["moments_resolution"],
                           c0 if (c0, c1) != (0.0, total) else None,
                           c1 if (c0, c1) != (0.0, total) else None, g["processing"])
        prompt = MOMENTS_PROMPT.format(
            game=cfg["game_name"], title=video["title"], channel=video["channel"], total=hms(total), c0=hms(c0), c1=hms(c1),
            heat=_heat_hint(heatmap, c0, c1), k=k, min_s=c["min_seconds"], max_s=c["max_seconds"],
            pinned=PINNED_RULE, ooc=ooc_rule(c)) + _notes(cfg, True)
        data = _ask(g["moments_model"], prompt, part, MOMENTS_SCHEMA)
        found = _clean(data.get("moments", []), c0, c1, total, c)
        out += found
        if len(spans) > 1:
            done[key] = found
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(done))
    cache.unlink(missing_ok=True)
    out.sort(key=lambda m: m["score"], reverse=True)
    return _dedupe(out)[:k]


def _clean(moments, c0, c1, total, c):
    good = []
    for m in moments:
        try:
            s, e = parse_ts(m["start"]), parse_ts(m["end"])
        except (ValueError, KeyError):
            continue
        # If the model answered relative to the chunk despite the instruction, shift it.
        if c0 > 0 and e <= (c1 - c0) + 5 and s < c0 - 5:
            s, e = s + c0, e + c0
        s, e = max(0.0, s), min(total, e)
        if e - s < min_seconds(m, c) * 0.6 or e <= s:
            continue
        if e - s > c["max_seconds"]:
            e = s + c["max_seconds"]
        good.append({**m, "start": s, "end": e, "score": int(m.get("score", 0))})
    return good


def _dedupe(moments):
    kept = []
    for m in moments:                          # already best-first
        if all(min(m["end"], k["end"]) - max(m["start"], k["start"]) < 5 for k in kept):
            kept.append(m)
    return kept


# ---------- Twitch clips: Gemini can't open Twitch URLs, so the downloaded clip is sent inline ----------

CLIP_SCHEMA = {
    "type": "object",
    "properties": {
        "is_target_game_gameplay": {"type": "boolean"},
        "is_brand_safe": {"type": "boolean"},
        "score": {"type": "integer", "minimum": 1, "maximum": 10},
        "start": {"type": "number", "description": "seconds from the start of this clip"},
        "end": {"type": "number", "description": "seconds from the start of this clip"},
        "category": {"type": "string",
                     "enum": ["out_of_context", "funny", "chaos", "skill", "fail", "glitch", "story",
                              "reaction", "discovery"]},
        "hook": {"type": "string"},
        "payoff": {"type": "string"},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "pinned_comment": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "problems": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["is_target_game_gameplay", "is_brand_safe", "score", "start", "end", "category", "hook", "payoff",
                 "title", "description", "pinned_comment", "tags", "problems"],
}

CLIP_PROMPT = """You edit YouTube Shorts for a {game} gameplay highlights channel.
This is a Twitch clip from {channel}'s stream, {dur:.0f} s long. A viewer clipped it and titled it "{title}"
({views} views), so something in it was worth clipping, but the cut is rough.
Watch it and answer:
- is_target_game_gameplay: is this mostly {game} gameplay (not just a webcam, menus, another game, or a tournament broadcast)?
- is_brand_safe: false if it shows nudity or sexual content (strip clubs, lap dances), slurs or hate speech,
  real-money gambling or case opening, or real-life graphic violence. Swearing and in-game violence are fine.
  This is only a flag: such clips are kept in a separate folder for manual review, so never lower the score
  or skip a great moment because of it.
- start/end: the best single window, {min_s}-{max_s} s long (or the whole clip if it is shorter and good), in seconds
  from the start of THIS clip. Start right before the action, end right after the payoff lands, no dead air.
- score 1-10: how well that window works as a standalone vertical Short (hook in 2 s, clear payoff), i.e. how
  likely it is to go viral. Judge this independently of is_brand_safe.
- {ooc}
- title: <=70 chars, curiosity-driven, true to what happens, no ALL CAPS spam, no emoji spam. Don't just copy the viewer's title.
- description: 1-2 sentences. tags: up to 8, include the game name.
- {pinned}
- problems: copyrighted music, a different stream or broadcast being watched on screen, etc."""


def clip_prompt(cand, cfg, dur):
    c = cfg["clip"]
    return CLIP_PROMPT.format(game=cfg["game_name"], channel=cand["channel"], dur=dur, title=cand["title"],
                              views=cand.get("views", "?"), min_s=c["min_seconds"],
                              max_s=c["max_seconds"], pinned=PINNED_RULE, ooc=ooc_rule(c)) + _notes(cfg, True)


def clean_window(m, dur, cfg):
    """Clamp a judge's window to the clip and the Shorts length limits."""
    c = cfg["clip"]
    s = max(0.0, float(m.get("start", 0)))
    e = min(dur, float(m.get("end", dur)))
    if e - s < min(min_seconds(m, c), dur) * 0.6:      # nonsense window: keep the viewer's whole cut
        s, e = 0.0, dur
    if e - s > c["max_seconds"]:
        e = s + c["max_seconds"]
    return {**m, "start": s, "end": e, "score": int(m.get("score", 0))}


def _proxy(path):
    """Small 480p copy of the clip for Gemini (inline uploads must stay a few MB)."""
    from .tools import ffmpeg, run
    out = path.with_name("gemini_proxy.mp4")
    if not out.exists():
        run([ffmpeg(), "-y", "-v", "error", "-i", str(path), "-vf", "scale=-2:480", "-r", "15",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-c:a", "aac", "-b:a", "64k",
             "-ac", "1", str(out)], "ffmpeg proxy")
    return out


def judge_clip(path, cand, cfg):
    """Gemini watches a downloaded Twitch clip; returns the window (seconds), score and metadata."""
    import base64
    from .tools import duration
    configure(cfg)
    t = cfg["twitch"]
    dur = duration(path)
    data = base64.b64encode(_proxy(path).read_bytes()).decode()
    part = {"type": "video", "data": data, "mime_type": "video/mp4",
            "resolution": t.get("resolution", "medium"),
            "processing": {"type": "static", "fps": t.get("fps", 2)}}
    prompt = clip_prompt(cand, cfg, dur)
    return clean_window(_ask(t["model"], prompt, part, CLIP_SCHEMA, timeout=180), dur, cfg)
