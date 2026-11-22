"""Video -> Shorts. One YouTube URL (or local file) in, finished vertical clips out."""
import json, shutil
from pathlib import Path
from . import download, gemini, refine, render, state, transcribe, upload
from .tools import OUTPUT, WORK, duration

REVIEW = OUTPUT / "review"
FLAGGED = OUTPUT / "flagged"     # not brand-safe (sexual content, slurs, gambling): review by hand, never auto-uploaded


def youtube_video(url_or_cand, cfg, do_audit=True, force=False):
    """Process one YouTube video. Returns list of rendered clip paths."""
    cand = url_or_cand if isinstance(url_or_cand, dict) else {"url": url_or_cand}
    meta = download.info(cand["url"])
    v = {**meta, **{k: x for k, x in cand.items() if x not in (None, "")}}
    v["url"] = f"https://www.youtube.com/watch?v={meta['video_id']}"   # canonical: no &t=, playlists, etc.
    vid = v["video_id"]
    if v["live"] in ("is_live", "is_upcoming", "post_live"):
        raise RuntimeError(f"{vid} is {v['live']}; wait until the VOD is processed")
    if v.get("availability") not in (None, "public"):
        raise RuntimeError(f"{vid} is {v['availability']}; Gemini can only watch public videos")
    v.setdefault("rights", "manual")
    state.set_video(vid, "processing", url=v["url"], channel=v["channel"], title=v["title"],
                    rights=v["rights"])
    print(f"[video] {vid} '{v['title'][:60]}' ({v['duration'] / 60:.0f} min, {v['channel']})")
    try:
        if do_audit:
            a = None if force else state.video_audit(vid)
            if a:
                print("[audit] reusing the audit from the previous attempt")
            else:
                a = gemini.audit(v, cfg)
            print(f"[audit] potential {a['clip_potential']}/10, ~{a['estimated_moments']} moments, "
                  f"is {cfg['game_name']} gameplay={a['is_target_game_gameplay']} problems={a['problems']}")
            original = a.get("is_original_creator_content", True)
            print(f"[audit] original creator content={original}")
            # A long stream is mostly ordinary play, so its moment *density* scores low (3/10) even when
            # it has several 8/10 moments. For stream VODs the moments step (min_moment_score) judges quality.
            min_score = cfg["gemini"]["min_audit_score_stream"] if v.get("was_live") or v.get("live") == "was_live" or v["duration"] > 60 * 60 \
                else cfg["gemini"]["min_audit_score"]
            ok = (a["is_target_game_gameplay"] and a["clip_potential"] >= min_score
                  and (original or not cfg["gemini"].get("require_original_content", True)))
            if not ok:
                state.set_video(vid, "rejected", audit=a)
                return []
            state.set_video(vid, "audited", audit=a)

        # Reuse the moment list from an earlier attempt so a retry finishes the same clips
        # (Gemini gives slightly different answers each call) and costs nothing.
        moments = None if force else state.video_moments(vid)
        if moments:
            print(f"[moments] reusing {len(moments)} moments from the previous attempt")
        else:
            moments = gemini.find_moments(v, cfg, heatmap=v.get("heatmap"))
            moments = [m for m in moments if m["score"] >= cfg["gemini"]["min_moment_score"]]
            state.set_video(vid, "moments", moments=moments)
        print(f"[moments] {len(moments)} kept: " +
              ", ".join(f"{m['start']:.0f}-{m['end']:.0f}s ({m['score']})" for m in moments))
        outs, failed = [], 0
        for m in moments:
            cid = f"{vid}_{int(m['start'])}"
            try:
                p = _youtube_clip(v, m, cid, cfg, force)
                if p:
                    outs.append(p)
            except Exception as e:
                failed += 1
                state.set_clip(cid, "error", video_id=vid, error=str(e)[:800])
                print(f"[error] clip {cid}: {str(e).splitlines()[0][:200]}")
        if failed:   # 'error' => `run` retries this video later; finished clips are skipped then
            state.set_video(vid, "error", error=f"{failed} clip(s) failed")
            print(f"[video] {failed} clip(s) failed; re-run the same command to retry just those")
        else:
            state.set_video(vid, "done")
        return outs
    finally:
        if cfg["cleanup_work"]:
            shutil.rmtree(WORK / vid, ignore_errors=True)


def _youtube_clip(v, m, cid, cfg, force):
    prev = state.clip(cid)
    if prev and prev["status"] in ("rendered", "uploaded") and not force:
        print(f"[skip] {cid} already {prev['status']}")
        return None
    c = cfg["clip"]
    s0 = max(0.0, m["start"] - c["pad_seconds"])
    s1 = min(v["duration"], m["end"] + c["pad_seconds"])
    sec = download.section(v["url"], s0, s1, WORK / v["video_id"] / f"{cid}.mp4")
    sec_len = duration(sec)
    words = transcribe.transcribe(sec, cfg)
    a, b = refine.refine(words, m["start"] - s0, m["end"] - s0, sec_len, gemini.min_seconds(m, c), c["max_seconds"])
    meta = {k: m.get(k) for k in ("title", "description", "pinned_comment", "tags", "score", "category",
                                  "hook", "payoff")}
    meta["is_brand_safe"] = m.get("is_brand_safe", True)      # moment lists saved before the flag existed: safe
    meta.update(start=round(s0 + a, 2), end=round(s0 + b, 2))
    return _finish(cid, sec, words, a, b, meta, v, cfg)


def local_file(path: Path, cfg, start=None, end=None, title=None):
    """Local video (e.g. your own recording). Claude picks the window unless start/end are given."""
    from . import score
    path = Path(path).expanduser().resolve()
    cid = render.safe_name(path.stem)
    work = WORK / cid
    work.mkdir(parents=True, exist_ok=True)
    src_copy = work / "source.mp4"
    if not src_copy.exists():
        src_copy.symlink_to(path)
    words = transcribe.transcribe(src_copy, cfg)
    src = {"url": str(path), "channel": cfg.get("own_channel_name", "me"), "rights": "own footage"}
    if start is not None and end is not None:
        meta = {"title": title or path.stem, "description": "", "tags": [cfg["game_name"].lower()], "score": None,
                "start": float(start), "end": float(end)}
    else:
        meta = score.score({"streamer": src["channel"], "title": title or path.stem}, words, src_copy, cfg)
    a, b = refine.refine(words, meta["start"], meta["end"], duration(src_copy),
                         cfg["clip"]["min_seconds"], cfg["clip"]["max_seconds"])
    meta.update(start=a, end=b)
    return [_finish(cid, src_copy, words, a, b, meta, src, cfg)]


_openai_note = {"shown": False}


def _judge_twitch(video, words, cand, cfg):
    """twitch.provider: openai (frames + transcript, paid, saves Gemini's free quota) or gemini."""
    from . import openai_judge
    if cfg["twitch"].get("provider", "gemini") == "openai" and not _openai_note.get("off"):
        if openai_judge.configured():
            try:
                return openai_judge.judge_clip(video, words, cand, cfg)
            except Exception as e:
                msg = str(e)
                print(f"[openai] failed: {type(e).__name__}: {msg[:160]}; using Gemini for this clip")
                if any(k in msg for k in ("insufficient_quota", "invalid_api_key", "401", "model_not_found")):
                    _openai_note["off"] = True     # won't fix itself this session: stop trying
                    print("[openai] OpenAI disabled for the rest of this run (check the key / billing)")
        elif not _openai_note["shown"]:
            print("[twitch] OPENAI_API_KEY is empty in .env; using Gemini for Twitch clips instead")
            _openai_note["shown"] = True
    return gemini.judge_clip(video, cand, cfg)


def twitch_clip(cand, cfg, force=False):
    """Twitch clips are already short and viewer-picked: download the whole clip; OpenAI (frames +
    transcript) or Gemini (the clip itself) picks the best window and writes the title."""
    cid = cand["source_id"]
    prev = state.clip(cid)
    if prev and prev["status"] in ("rendered", "uploaded") and not force:
        print(f"[skip] {cid} already {prev['status']}")
        return []
    state.set_video(cid, "processing", url=cand["url"], channel=cand["channel"],
                    title=cand["title"], rights=cand["rights"])
    print(f"[twitch] {cid} '{cand['title'][:60]}' ({cand['duration']:.0f}s, {cand['channel']}, "
          f"{cand['views']} views)")
    try:
        video = download.whole(cand["url"], cid)
        words = transcribe.transcribe(video, cfg)
        meta = _judge_twitch(video, words, cand, cfg)
        print(f"[twitch] score {meta['score']}/10, gameplay={meta['is_target_game_gameplay']}, "
              f"brand-safe={meta.get('is_brand_safe')}, "
              f"{meta['start']:.0f}-{meta['end']:.0f}s, problems={meta.get('problems')}")
        if not meta["is_target_game_gameplay"] or meta["score"] < cfg["twitch"]["min_score"]:
            state.set_video(cid, "rejected", audit=meta)
            return []
        a, b = refine.refine(words, meta["start"], meta["end"], duration(video),
                             gemini.min_seconds(meta, cfg["clip"]), cfg["clip"]["max_seconds"])
        meta.update(start=a, end=b)
        out = _finish(cid, video, words, a, b, meta, {**cand, "video_id": cid}, cfg)
        state.set_video(cid, "done")
        return [out]
    finally:
        if cfg["cleanup_work"]:
            shutil.rmtree(WORK / cid, ignore_errors=True)


def _finish(cid, video, words, a, b, meta, src, cfg):
    flagged = meta.get("is_brand_safe") is False
    out = (FLAGGED if flagged else REVIEW) / f"{cid}.mp4"
    hook = meta["title"] if cfg["render"].get("hook_from_title", True) else ""
    render.render(video, words, a, b, out, cfg, hook_text=hook)
    meta["pinned_comment"] = pinned_comment(meta, src)
    copy = (f"TITLE\n{upload.build_title(meta['title'])}\n\nPINNED COMMENT\n{meta['pinned_comment']}\n\n"
            f"DESCRIPTION\n{meta.get('description') or ''}{upload.credit_block(src)}\n")
    out.with_suffix(".txt").write_text(copy)       # ready to copy-paste into YouTube Studio
    sidecar = {"clip_id": cid, "meta": meta, "flagged": flagged,
               "source": {k: src.get(k) for k in ("url", "channel", "channel_id", "title", "rights", "license",
                                                  "found_by", "platform", "twitch_login", "channel_url",
                                                  "clipped_by")}}
    out.with_suffix(".json").write_text(json.dumps(sidecar, indent=2))
    state.set_clip(cid, "rendered", video_id=src.get("video_id", cid), url=src.get("url"),
                   start=meta["start"], end=meta["end"], score=meta.get("score"),
                   title=meta["title"], out_path=str(out), meta=sidecar)
    print(f"[rendered] {out}  ({b - a:.1f}s) '{meta['title']}'" + ("  [FLAGGED: not brand-safe]" if flagged else ""))
    if cfg["upload"]["mode"] == "auto" and not flagged:
        maybe_upload(cid, out, sidecar, cfg)
    else:
        print(f"           approve with: python main.py approve {cid}")
    return out


def pinned_comment(meta, src):
    """The judge's context blurb (or the description, for moment lists saved before blurbs existed),
    plus a credit line for someone else's footage."""
    text = " ".join((meta.get("pinned_comment") or meta.get("description") or "").split())
    if src.get("rights") != "own footage" and src.get("channel") and src.get("url"):
        text += f"\n\nClip from {src['channel']}: {src.get('channel_url') or src['url']}"
    return text.strip()


def rights_ok(src, cfg):
    """Upload gate. 'manual' = you ran `clip URL` yourself; 'none' = found by scan with no permission.
    Re-checked against the current config, so adding a channel to permitted_channels unlocks it."""
    r = src.get("rights") or "none"
    if r != "none":
        return r
    if src.get("platform") == "twitch":
        for st in cfg.get("twitch", {}).get("streamers", []):
            if st.get("permission") and st["login"].lower() == (src.get("twitch_login") or "").lower():
                return f"permission: {st.get('proof') or 'recorded'}"
        return None
    from .yt_discover import rights_basis
    return rights_basis({"channel_id": src.get("channel_id") or "", "license": src.get("license") or ""}, cfg)


def maybe_upload(cid, out, sidecar, cfg):
    up = cfg["upload"]
    if not rights_ok(sidecar["source"], cfg):
        print(f"[upload] {cid} stays in review: no permission recorded for {sidecar['source']['channel']}")
        return None
    if state.uploads_last_24h() >= up["max_per_day"]:
        print(f"[upload] daily cap {up['max_per_day']} reached; {cid} stays in review")
        return None
    when = state.next_publish_slot(up["schedule_gap_hours"])
    yt = upload.upload(out, sidecar["meta"], sidecar["source"], cfg, publish_at=when)
    state.set_clip(cid, "uploaded", youtube_id=yt, publish_at=when)
    print(f"[upload] {cid} -> https://youtu.be/{yt}")
    return yt


def approve(cid, cfg):
    out = REVIEW / f"{cid}.mp4"
    if not out.exists() and (FLAGGED / f"{cid}.mp4").exists():
        out = FLAGGED / f"{cid}.mp4"      # your call: YouTube may age-restrict or demonetise it
    sidecar = json.loads(out.with_suffix(".json").read_text())
    if not rights_ok(sidecar["source"], cfg):
        src = sidecar["source"]
        raise RuntimeError(f"No permission recorded for {src['channel']} ({src.get('channel_id')}). "
                           "Add them to rights.permitted_channels in config.yaml once they say yes.")
    when = state.next_publish_slot(cfg["upload"]["schedule_gap_hours"])
    yt = upload.upload(out, sidecar["meta"], sidecar["source"], cfg, publish_at=when)
    state.set_clip(cid, "uploaded", youtube_id=yt, publish_at=when)
    print(f"https://youtu.be/{yt}")
