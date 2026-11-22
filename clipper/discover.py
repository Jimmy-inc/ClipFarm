"""Find Twitch clips via the official Twitch Helix API (env TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET,
free at dev.twitch.tv).

Viewer-made clips are a built-in highlight signal: someone already thought the moment was worth
clipping, and view_count ranks them. Two sources:
  - the game's top clips across Twitch (discovery; review only unless the streamer gave permission)
  - clips from streamers listed in twitch.streamers (permission: true makes them uploadable)
"""
import os, re, time, datetime as dt, requests
from . import state, tools

API = "https://api.twitch.tv/helix"
_tok = {"value": None, "exp": 0.0, "client_id": None, "file": None}
_rotation = {"hours": 8}      # twitch.key_rotation_hours; set by find_candidates
_window = {"n": 0}            # which of twitch.windows_hours this round uses (rotates every round)


def _read_env(path):
    out = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.split(" #")[0].strip().strip('"').strip("'")
    return out


def _key_sets():
    """[(file name, client id, secret)]: the Twitch keys in .env, then in 2.env, 3.env, ... (number order).
    Only the TWITCH_* lines of the numbered files are used. A file without both keys, or with an app
    that's already in the list, is skipped."""
    files = [tools.ROOT / ".env"] + sorted((f for f in tools.ROOT.glob("*.env") if f.stem.isdigit()),
                                           key=lambda f: int(f.stem))
    sets, ids = [], set()
    for f in files:
        kv = _read_env(f) if f.exists() else {}
        if f.name == ".env" and not kv.get("TWITCH_CLIENT_ID"):       # keys exported in the shell instead
            kv = {k: os.environ.get(k, "") for k in ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET")}
        cid, secret = kv.get("TWITCH_CLIENT_ID"), kv.get("TWITCH_CLIENT_SECRET")
        if cid and secret and cid not in ids:
            ids.add(cid)
            sets.append((f.name, cid, secret))
    return sets


def configured():
    return bool(_key_sets())


def _creds():
    """The key set for the current time slot. Slots follow the clock (UTC, from midnight Jan 1 1970),
    so a restart keeps the same set, and new or edited 2.env, 3.env ... are picked up without a restart."""
    sets = _key_sets()
    if not sets:
        raise RuntimeError("no Twitch keys: fill TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET in .env")
    hours = float(_rotation["hours"] or 0)
    i = int(time.time() // (hours * 3600)) % len(sets) if hours > 0 else 0
    name, cid, secret = sets[i]
    if _tok["client_id"] != cid:              # new slot: a token only works with its own client id
        if _tok["client_id"] is not None:
            print(f"[twitch] switched to the keys in {name}")
        _tok.update(value=None, exp=0.0, client_id=cid, file=name)
    return name, cid, secret


def _token():
    name, cid, secret = _creds()
    if _tok["value"] and time.time() < _tok["exp"] - 300:
        return _tok["value"]
    r = requests.post("https://id.twitch.tv/oauth2/token", data={
        "client_id": cid, "client_secret": secret,
        "grant_type": "client_credentials"}, timeout=30)
    if r.status_code in (400, 403):
        raise RuntimeError(f"Twitch rejected TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET in {name} "
                           f"({r.status_code}: {r.text[:120]}). Re-copy them from dev.twitch.tv/console/apps.")
    r.raise_for_status()
    j = r.json()
    _tok.update(value=j["access_token"], exp=time.time() + j.get("expires_in", 3600))
    return _tok["value"]


def _get(path, **params):
    """One Helix call. Returns (data, next_cursor)."""
    for attempt in range(3):
        token = _token()                          # picks this slot's key set first
        r = requests.get(f"{API}/{path}", params=params, timeout=30, headers={
            "Client-Id": _tok["client_id"], "Authorization": f"Bearer {token}"})
        if r.status_code == 401:                     # token expired early
            _tok["value"] = None
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(5 * (attempt + 1))
            continue
        r.raise_for_status()
        j = r.json()
        return j["data"], j.get("pagination", {}).get("cursor")
    r.raise_for_status()
    raise RuntimeError(f"Twitch {path} failed: {r.status_code}")


def _game_id(name):
    data, _ = _get("games", name=name)
    return data[0]["id"] if data else None


def _clips(pages=1, **params):
    """Up to pages x 100 clips. Twitch often returns near-empty pages (a handful of clips plus a cursor),
    so keep following the cursor until that many arrive, within 10 requests per wanted page."""
    out, cursor = [], None
    for _ in range(pages * 10):
        data, cursor = _get("clips", first=100, **({"after": cursor} if cursor else {}), **params)
        out += data
        if not cursor or len(out) >= pages * 100:
            break
    return out


def _permitted(t):
    return {s["login"].lower(): s for s in t.get("streamers", []) if s.get("permission")}


def _drop_reason(c, t, cfg, game_id, excl):
    if c.get("game_id") != game_id:
        return "other game"
    if t.get("require_english", True) and not (c.get("language") or "").startswith("en"):
        return "not English"
    if any(re.search(p, c["broadcaster_name"], re.I) for p in excl):
        return "excluded channel"
    if c["view_count"] < t["min_clip_views"]:
        return f"under {t['min_clip_views']} views"
    if c["duration"] < t.get("min_clip_seconds", 12):
        return "too short"
    if state.video_seen("tw_" + c["id"], cfg["retry"]["max_attempts"]):
        return "already done"
    return None


def find_candidates(cfg, top_n=None):
    t = cfg["twitch"]
    _rotation["hours"] = t.get("key_rotation_hours", 8)
    name = t.get("game_name") or cfg["game_name"]
    game_id = _game_id(name)
    sets = _key_sets()
    if len(sets) > 1:
        print(f"[twitch] using the keys in {_tok['file']} ({len(sets)} sets, switching every {_rotation['hours']} h)")
    if not game_id:
        print(f"[twitch] category '{name}' not found on Twitch (set twitch.game_name to its exact Twitch name)")
        return []
    from .yt_discover import search_since
    # In the loop each round looks at a different time window, so fresh clips get a turn instead of the
    # same 48-hour top list every time (Twitch has no text search for clips). One-off runs use the widest.
    windows = t.get("windows_hours") or [t["lookback_hours"]]
    if t.get("rotate_windows"):
        hours = windows[_window["n"] % len(windows)]
        _window["n"] += 1
    else:
        hours = max(windows)
    since = search_since({"lookback_hours": hours,
                          "not_before": cfg["discovery"].get("not_before")})
    permitted = _permitted(t)

    raw = _clips(pages=t.get("pages", 2), game_id=game_id, started_at=since) if t.get("discover", True) else []
    permitted_ids = set()
    for login in permitted:
        users, _ = _get("users", login=login)
        if users:
            permitted_ids.add(users[0]["id"])
            # broadcaster_id and game_id are mutually exclusive in Get Clips: the game is filtered below.
            raw += _clips(broadcaster_id=users[0]["id"], started_at=since)

    excl = list(cfg["discovery"].get("exclude_channel_patterns", [])) + list(t.get("exclude_broadcaster_patterns", []))
    drops, kept, seen_ids = {}, [], set()
    for c in raw:
        if c["id"] in seen_ids:
            continue
        seen_ids.add(c["id"])
        why = _drop_reason(c, t, cfg, game_id, excl)
        if why:
            drops[why] = drops.get(why, 0) + 1
        else:
            kept.append(c)

    # Several viewers often clip the same moment: keep the most-viewed clip per spot in a VOD.
    kept.sort(key=lambda c: c["view_count"], reverse=True)
    uniq = []
    for c in kept:
        dup = any(c["broadcaster_id"] == u["broadcaster_id"] and c.get("video_id") and c["video_id"] == u.get("video_id")
                  and c.get("vod_offset") is not None and u.get("vod_offset") is not None
                  and abs(c["vod_offset"] - u["vod_offset"]) < 45 for u in uniq)
        if dup:
            drops["same moment as a bigger clip"] = drops.get("same moment as a bigger clip", 0) + 1
        else:
            uniq.append(c)

    # Spread picks across streamers; permitted streamers first.
    per, picked = {}, []
    cap = t.get("max_per_broadcaster", 2)
    for c in sorted(uniq, key=lambda c: (c["broadcaster_id"] not in permitted_ids, -c["view_count"])):
        b = c["broadcaster_id"]
        if per.get(b, 0) >= cap:
            continue
        per[b] = per.get(b, 0) + 1
        picked.append(c)
    picked = picked[: top_n or t["max_clips_per_run"]]

    logins = {}
    if picked:      # Get Clips has no login field; one call maps ids -> logins for credits and permission
        users, _ = _get("users", id=[c["broadcaster_id"] for c in picked])
        logins = {u["id"]: u["login"] for u in users}
    print(f"[twitch] {len(raw)} clips in the last {hours} h; dropped: "
          + (", ".join(f"{k} {v}" for k, v in sorted(drops.items(), key=lambda kv: -kv[1])) or "none"))
    out = []
    for c in picked:
        login = logins.get(c["broadcaster_id"], c["broadcaster_name"].lower())
        p = permitted.get(login)
        out.append({"source_id": "tw_" + c["id"], "url": c["url"], "title": c["title"],
                    "channel": c["broadcaster_name"], "channel_id": "",
                    "channel_url": f"https://www.twitch.tv/{login}", "twitch_login": login,
                    "clipped_by": c.get("creator_name"), "views": c["view_count"],
                    "duration": c["duration"], "created_at": c["created_at"],
                    "platform": "twitch", "found_by": "twitch: permitted streamer" if p else "twitch: top clips",
                    "rights": f"permission: {p.get('proof') or 'recorded'}" if p else "none"})
    return out
