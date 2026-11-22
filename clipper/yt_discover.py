"""Find candidate GTA 6 videos on YouTube with the official Data API v3.

Needs env var YOUTUBE_API_KEY (Google Cloud -> APIs & Services -> Credentials -> API key,
with YouTube Data API v3 enabled). Use a *separate* Cloud project from the one that
uploads, so discovery and uploads each get their own 10,000 units/day.

Quota cost per call: search.list = 100, videos/channels/playlistItems.list = 1.

Rights gate (this is deliberate, not a TODO): a video is only *processable* when
  - its channel is in rights.permitted_channels (you have written permission), or
  - it is one of your own channels (rights.own_channel_ids), or
  - it is licensed Creative Commons BY and rights.allow_creative_commons is true.
Everything else popular is written to output/permission_leads.csv so you know whom to ask.
"""
import csv, datetime as dt, os, re
import requests
from . import state, tools
from .tools import OUTPUT

API = "https://www.googleapis.com/youtube/v3"


def _get(path, **params):
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        raise RuntimeError("YOUTUBE_API_KEY is not set")
    r = requests.get(f"{API}/{path}", params={**params, "key": key}, timeout=30)
    if (r.status_code == 403 and "quotaExceeded" in r.text) or (r.status_code == 429 and "Quota exceeded" in r.text):
        from .gemini import QuotaExhausted
        raise QuotaExhausted("YouTube Data API daily quota (10,000 units) used up; it resets at midnight Pacific.")
    if r.status_code != 200:
        raise RuntimeError(f"YouTube API {path} {r.status_code}: {r.text[:300]}")
    return r.json()


def iso_duration(s: str) -> float:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0.0
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


def _video_details(ids):
    out = []
    for i in range(0, len(ids), 50):
        data = _get("videos", part="snippet,contentDetails,statistics,status,liveStreamingDetails",
                    id=",".join(ids[i:i + 50]))
        out.extend(data.get("items", []))
    return out


def _normalise(v):
    sn, cd, st, stt = v["snippet"], v["contentDetails"], v.get("statistics", {}), v.get("status", {})
    published = dt.datetime.fromisoformat(sn["publishedAt"].replace("Z", "+00:00"))
    age_h = max(1.0, (dt.datetime.now(dt.timezone.utc) - published).total_seconds() / 3600)
    views = int(st.get("viewCount", 0))
    return {
        "video_id": v["id"], "url": f"https://www.youtube.com/watch?v={v['id']}",
        "title": sn["title"], "description": sn.get("description", "")[:500],
        "tags": sn.get("tags", []), "channel_id": sn["channelId"], "channel": sn["channelTitle"],
        "published": sn["publishedAt"], "age_hours": round(age_h, 1),
        "duration": iso_duration(cd.get("duration", "")), "views": views,
        "views_per_hour": round(views / age_h, 1), "license": stt.get("license", "youtube"),
        "privacy": stt.get("privacyStatus"), "live": sn.get("liveBroadcastContent", "none"),
        "lang": (sn.get("defaultAudioLanguage") or sn.get("defaultLanguage") or "").lower(),
        "was_live": "liveStreamingDetails" in v and sn.get("liveBroadcastContent", "none") == "none",
    }


def _is_english(v):
    """Title mostly Latin script AND (if the uploader set one) an English audio language.
    The language field alone isn't enough: many channels leave it on the default "en"."""
    letters = [c for c in re.sub(r"#\S+", "", v["title"]) if c.isalpha()]
    non_latin = sum(1 for c in letters if ord(c) > 0x24F)
    if letters and non_latin / len(letters) >= 0.3:
        return False
    return v["lang"].startswith("en") if v.get("lang") else True


def rights_basis(v, cfg):
    r = cfg["rights"]
    if v["channel_id"] in r.get("own_channel_ids", []):
        return "own channel"
    for c in r.get("permitted_channels", []):
        if c.get("id") == v["channel_id"]:
            return f"permission: {c.get('proof', 'recorded')}"
    lic = (v.get("license") or "").lower()          # API: "creativeCommon"; yt-dlp: "Creative Commons ..."
    if r.get("allow_creative_commons") and ("creativecommon" in lic or "creative commons" in lic):
        return "Creative Commons BY"
    return None


def _is_gta6(v, keywords, title_only=True, exclude=()):
    # Title-only by default: tags/descriptions are keyword-stuffed ("GTA 5 LIVE" tagged "gta 6"),
    # and hashtags are stripped because titles stuff them too ("Gta v live #gta6").
    title = re.sub(r"#[^\W\d]\S*", " ", v["title"]).lower()
    if any(re.search(p, title) for p in exclude):
        return False
    text = title if title_only else " ".join([title, v["description"], " ".join(v["tags"])]).lower()
    return any(k.lower() in text for k in keywords)


def _clean_title(v):
    return re.sub(r"#[^\W\d]\S*", " ", v["title"]).lower()


def _drop_reason(v, d):
    title = _clean_title(v)
    if any(re.search(p, title) for p in d.get("exclude_title_patterns", [])):
        return "excluded title (other game)"
    if d.get("skip_compilations", True) and any(re.search(p, title) for p in d.get("compilation_title_patterns", [])):
        return "compilation title"
    if any(re.search(p, v.get("channel", "").lower()) for p in d.get("exclude_channel_patterns", [])):
        return "tournament/league channel"
    # keyword_match: off = trust the search (audit checks the game), title = game named in title,
    # any = title, description or tags
    mode = d.get("keyword_match", "off")
    if mode != "off" and not _is_gta6(v, d["keywords"], mode == "title"):
        return "game not in title"
    if v["privacy"] != "public" or v["live"] != "none":
        return "live/upcoming"
    if d.get("require_english", True) and not _is_english(v):
        return "not English"
    if v["duration"] < d["min_video_minutes"] * 60:
        return "too short"
    if v["duration"] > d["max_video_minutes"] * 60:
        return "too long"
    if v["views"] < d["min_views"]:
        return f"under {d['min_views']} views"
    return None


def _passes_filters(v, d):
    if d.get("require_english", True) and not _is_english(v):
        return False
    return (v["privacy"] == "public" and v["live"] == "none"
            and d["min_video_minutes"] * 60 <= v["duration"] <= d["max_video_minutes"] * 60
            and v["views"] >= d["min_views"])


def _uploads_of(channel_id, n=25):
    ch = _get("channels", part="contentDetails", id=channel_id).get("items", [])
    if not ch:
        return []
    pl = ch[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    items = _get("playlistItems", part="contentDetails", playlistId=pl, maxResults=n).get("items", [])
    return [i["contentDetails"]["videoId"] for i in items]


def _search(q, since, n, license_=None, order="viewCount", duration=None):
    params = dict(part="id", q=q, type="video", order=order, publishedAfter=since,
                  maxResults=n, safeSearch="none", relevanceLanguage="en")
    if duration:                 # "medium" = 4-20 min, "long" = over 20 min (sorted by views,
        params["videoDuration"] = duration   # unfiltered results are almost all Shorts)
    if license_:
        params["videoLicense"] = license_
    return [i["id"]["videoId"] for i in _get("search", **params).get("items", [])]


def search_since(d):
    """Start of the search window: lookback_hours ago, but never before discovery.not_before
    (e.g. a game's release, so pre-launch trailers, leaks and predictions aren't searched)."""
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=d["lookback_hours"])
    if d.get("not_before"):
        floor = dt.datetime.fromisoformat(str(d["not_before"]).replace("Z", "+00:00"))
        since = max(since, floor)
    return since.strftime("%Y-%m-%dT%H:%M:%SZ")


def find_candidates(cfg):
    """Return processable candidates (rights-checked, filtered, unseen), best first."""
    d = cfg["discovery"]
    since = search_since(d)
    ids = []
    # 1) Channels you are allowed to clip: cheap uploads-playlist reads (2 units each).
    for c in cfg["rights"].get("permitted_channels", []) + [
            {"id": i} for i in cfg["rights"].get("own_channel_ids", [])]:
        if c.get("id"):
            ids += _uploads_of(c["id"])
    # 2) Creative Commons search (100 units per query).
    if cfg["rights"].get("allow_creative_commons"):
        for q in d["queries"]:
            ids += _search(q, since, d["results_per_query"], "creativeCommon")
    # 3) Open search, only to collect permission leads (100 units per query).
    lead_ids = []
    if d.get("collect_leads"):
        for q in d["queries"][: d.get("lead_queries", 1)]:
            lead_ids += _search(q, since, d["results_per_query"])

    ids = list(dict.fromkeys(ids))
    all_ids = list(dict.fromkeys(ids + lead_ids))
    details = {v["id"]: _normalise(v) for v in _video_details(all_ids)} if all_ids else {}

    cands, leads = [], []
    for vid, v in details.items():
        if _drop_reason(v, d):
            continue
        basis = rights_basis(v, cfg)
        if basis:
            if not state.video_seen(vid, cfg["retry"]["max_attempts"]):
                v["rights"] = basis
                cands.append(v)
        else:
            leads.append(v)
    _write_leads(leads)
    cands.sort(key=lambda x: x["views_per_hour"], reverse=True)
    return cands[: d["max_videos_per_run"]]


def _write_leads(leads):
    if not leads:
        return
    OUTPUT.mkdir(exist_ok=True)
    path = OUTPUT / "permission_leads.csv"
    known = set()
    if path.exists():
        with path.open() as f:
            known = {row["video_id"] for row in csv.DictReader(f)}
    new = [v for v in leads if v["video_id"] not in known]
    if not new:
        return
    write_header = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["found", "channel", "channel_id", "video_id", "url",
                                          "title", "views", "views_per_hour"])
        if write_header:
            w.writeheader()
        now = dt.date.today().isoformat()
        for v in sorted(new, key=lambda x: x["views_per_hour"], reverse=True):
            w.writerow({"found": now, **{k: v[k] for k in w.fieldnames if k != "found"}})
    print(f"[discover] {len(new)} popular videos from channels without permission -> {path}")


def scan(cfg, queries, top_n=None, record=True):
    """Search each query, merge, filter, rank by views/hour, return the top_n unseen videos.

    Unlike find_candidates, videos WITHOUT a rights basis are kept (rights="none") so you can
    review what the bot would make; the upload step refuses them. Set scan.only_rights_cleared
    to skip them entirely.
    """
    d, sc = cfg["discovery"], cfg["scan"]
    top_n = top_n or sc["top_n"]
    since = search_since(d)
    found_by, query_ids = {}, {}
    for q in queries:
        ids = []
        for dur in sc.get("search_durations", ["long"]):     # each costs 100 quota units
            ids += _search(q["q"], since, d["results_per_query"], order=sc.get("order", "viewCount"),
                           duration=dur)
        for vid in ids:
            found_by.setdefault(vid, q["q"])
        q["ids"] = ids
    # Channels you're cleared for are always checked too (2 quota units each).
    for c in cfg["rights"].get("permitted_channels", []) + [
            {"id": i} for i in cfg["rights"].get("own_channel_ids", [])]:
        if c.get("id"):
            for vid in _uploads_of(c["id"]):
                found_by.setdefault(vid, "permitted channel")

    # Long streams whose full watch was cut short (daily quota, crash) resume first, even if no
    # search found them this round: each one's finished chunks are in work/moment_chunks/.
    resume = {f.stem for f in (tools.WORK / "moment_chunks").glob("*.json")}
    for vid in resume:
        found_by[vid] = "resuming a half-watched video"
    details = [_normalise(v) for v in _video_details(list(found_by))] if found_by else []
    kept, leads = [], []
    dropped = {}
    for v in details:
        v["found_by"] = found_by[v["video_id"]]
        why = _drop_reason(v, d)
        if not why and state.video_seen(v["video_id"], cfg["retry"]["max_attempts"]):
            why = "already done"
            (tools.WORK / "moment_chunks" / f"{v['video_id']}.json").unlink(missing_ok=True)
        if why:
            dropped[why] = dropped.get(why, 0) + 1
            continue
        v["rights"] = rights_basis(v, cfg) or "none"
        if v["rights"] == "none":
            leads.append(v)
            if sc.get("only_rights_cleared"):
                continue
        kept.append(v)
    print(f"[scan] {len(details)} search results; dropped: "
          + (", ".join(f"{k} {n}" for k, n in sorted(dropped.items(), key=lambda x: -x[1])) or "none"))
    _write_leads(leads)
    for q in queries:                      # how many usable videos each query produced
        q["found"] = sum(1 for v in kept if v["found_by"] == q["q"])
        q["id"] = state.add_query(q["q"], q["found"]) if record else None
    # Prefer original streamers: a past livestream (VOD) is almost always the streamer's own channel.
    boost = sc.get("stream_vod_boost", 3)
    for v in kept:
        v["rank"] = v["views_per_hour"] * (boost if v.get("was_live") else 1)
    if sc.get("stream_vods_first", True):          # every stream VOD before any other video
        kept.sort(key=lambda x: (bool(x.get("was_live")), x["rank"]), reverse=True)
    else:
        kept.sort(key=lambda x: x["rank"], reverse=True)
    kept.sort(key=lambda x: x["video_id"] not in resume)      # stable: half-watched videos first
    return kept[:top_n]
