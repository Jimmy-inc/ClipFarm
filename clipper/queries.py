"""Gemini writes fresh YouTube search queries each run.

It can look up what is trending first (Google Search grounding) and sees how well previous
queries worked (videos found, Shorts made), so queries that keep finding nothing get dropped.
"""
import datetime as dt
from . import gemini, state

SCHEMA = {
    "type": "object",
    "properties": {
        "queries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"q": {"type": "string"}, "why": {"type": "string"}},
                "required": ["q", "why"],
            },
        }
    },
    "required": ["queries"],
}

PROMPT = """You write YouTube search queries for a bot that finds long-form {game} gameplay videos
(streams, playthroughs, challenges, funny-moments compilations of the creator's own play) uploaded in the
last {hours} hours, then cuts them into Shorts. Today is {today}.
{trend}
Good queries find videos that contain many short, standalone, high-energy moments: chaos, fails, glitches,
skill, big reactions, new missions or features people are excited about. Avoid queries that mostly find
news, rumours, trailers, reaction-to-trailer videos, tier lists or tutorials.

Past queries (most recent first) with how many usable videos they found and how many Shorts came out:
{history}
Every query must be new: never reuse a past query word for word. For past queries that worked, write a
close variation (a different angle, activity, place or wording); drop the angles that found nothing.

Write {n} different queries, 2-6 words each, in English, mixing trending topics with evergreen gameplay terms.
Every query must make sense on its own on YouTube (include the game name)."""


def generate(cfg, n=None):
    s, g = cfg["scan"], cfg["discovery"]
    n = n or s["num_queries"]
    kws = [k.lower() for k in g.get("keywords", [])]
    hist = [h for h in state.query_history(300) if not kws or any(k in h["q"].lower() for k in kws)][:30]
    history = "\n".join(f'- "{h["q"]}": {h["found"]} videos, {h["clips"]} Shorts' for h in hist) or "(none yet)"
    trend = (f"First use Google Search to check what {cfg['game_name']} topics, missions, updates, events, "
             f"glitches and creators are trending this week, and use that." if s.get("use_google_search") else "")
    prompt = PROMPT.format(game=cfg["game_name"], hours=g["lookback_hours"], today=dt.date.today().isoformat(),
                           trend=trend, history=history, n=n)
    if s.get("query_notes"):
        prompt += f"\n{s['query_notes']}"
    tools = [{"type": "google_search"}] if s.get("use_google_search") else None
    gemini.configure(cfg)
    try:
        data = gemini._ask(s["query_model"], prompt, None, SCHEMA, tools=tools)
    except Exception as e:
        if not tools:
            raise
        print("[queries] Google Search grounding unavailable (its free-tier quota is small); "
              "writing queries without it")
        data = gemini._ask(s["query_model"], prompt, None, SCHEMA)
    past = {" ".join(h["q"].lower().split()) for h in state.query_history(5000)}
    past |= {q.lower() for q in s.get("fixed_queries", [])}     # already searched every round
    out, repeats = [], []
    for item in data.get("queries", []):
        q = " ".join(str(item.get("q", "")).split())
        if q and q.lower() not in {x["q"].lower() for x in out}:
            (repeats if q.lower() in past else out).append({"q": q, "why": item.get("why", "")})
    if repeats:
        print(f"[queries] dropped {len(repeats)} already-searched quer{'y' if len(repeats) == 1 else 'ies'}: "
              + ", ".join(f'"{r["q"]}"' for r in repeats))
    if not out:                   # everything was a repeat: search them anyway rather than nothing
        out = repeats
    fixed = [{"q": q, "why": "fixed in config"} for q in s.get("fixed_queries", [])]
    return (fixed + out)[: len(fixed) + n]
