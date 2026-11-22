#!/usr/bin/env python3
"""GTA 6 Shorts pipeline.

  python main.py clip URL_OR_FILE      one video -> finished Shorts in output/review/
        [--no-audit] [--force]         skip the Gemini triage pass / re-render existing clips
        [--start S --end E --title T]  (local files) skip AI and cut exactly this window
  python main.py audit URL             Gemini triage only, prints JSON
  python main.py moments URL           Gemini moment list only, prints JSON (no downloads)
  python main.py scan                  Gemini writes search queries -> top 5 videos -> Shorts
        [--dry-run]                    only show the queries and the videos it would process
        [--queries "q1" "q2"] [--top N] [--hours H] [--game G]
  python main.py discover              same as: scan --dry-run
  python main.py twitch --profile cs2  top Twitch clips for the game -> Shorts  [--dry-run] [--top N] [--hours H]
  python main.py run                   one round of every source in `sources` + upload per config
  python main.py loop                  forever: YouTube every 60 min, Twitch every 5 min in between,
        [--every MIN] [--twitch-every MIN] [--top N] [--upload]     review only, logs to logs/
  python main.py stop                  ask a running loop to stop after the current video
  python main.py approve CLIP_ID       upload a reviewed clip
  python main.py auth                  one-time YouTube OAuth in a browser
  python main.py status                what has been processed
"""
import argparse, json, os, sys, time, traceback
from pathlib import Path
import yaml
from clipper import pipeline, state, tools

cfg = yaml.safe_load((tools.ROOT / "config.yaml").read_text())

# Load KEY=value lines from .env (so cron/launchd runs get the API keys too).
_env = tools.ROOT / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def cmd_clip(a):
    _apply_overrides(a)          # default profile (game notes, no-spoiler rule) or --game
    if Path(a.target).expanduser().exists():
        outs = pipeline.local_file(Path(a.target), cfg, a.start, a.end, a.title)
    else:
        outs = pipeline.youtube_video(a.target, cfg, do_audit=not a.no_audit, force=a.force)
    print(f"\n{len(outs)} Short(s) ready:" if outs else "\nNo Shorts produced.")
    for o in outs:
        print("  ", o)


def _video(url):
    from clipper import download
    return download.info(url)


def cmd_audit(a):
    _apply_overrides(a)          # default profile (game notes, no-spoiler rule) or --game
    from clipper import gemini
    print(json.dumps(gemini.audit(_video(a.url), cfg), indent=2))


def cmd_moments(a):
    _apply_overrides(a)          # default profile (game notes, no-spoiler rule) or --game
    from clipper import gemini
    v = _video(a.url)
    ms = gemini.find_moments(v, cfg, heatmap=v["heatmap"])
    for m in ms:
        m["at"] = f"{tools.hms(m['start'])}-{tools.hms(m['end'])}"
    print(json.dumps(ms, indent=2))


def _apply_overrides(a):
    prof = getattr(a, "profile", None) or (None if getattr(a, "game", None) else cfg.get("default_profile"))
    if prof:
        if prof not in cfg.get("profiles", {}):
            raise RuntimeError(f"unknown profile {prof!r}; known: {', '.join(cfg.get('profiles', {}))}")
        pr = cfg["profiles"][prof]
        cfg["game_name"] = pr["game_name"]
        cfg["discovery"]["keywords"] = pr["keywords"]
        cfg["discovery"]["exclude_title_patterns"] = pr.get("exclude_title_patterns", [])
        for section in ("discovery", "scan", "gemini", "clip", "twitch", "upload"):   # optional per-game overrides
            cfg[section].update(pr.get(section, {}))
        if "sources" in pr:
            cfg["sources"] = pr["sources"]
    if getattr(a, "game", None):
        cfg["game_name"] = a.game
        cfg["discovery"]["keywords"] = [a.game.lower()]      # GTA keywords don't apply to other games
        cfg["discovery"]["exclude_title_patterns"] = []
    if getattr(a, "max_minutes", None):
        cfg["discovery"]["max_video_minutes"] = a.max_minutes
    if getattr(a, "hours", None):
        cfg["discovery"]["lookback_hours"] = a.hours
        cfg["twitch"]["lookback_hours"] = a.hours
        cfg["twitch"]["windows_hours"] = [a.hours]


def cmd_scan(a, dry_run=None):
    """Search prompt generator -> top N YouTube URLs -> URL-to-Short pipeline (-> upload per config)."""
    from clipper import queries, yt_discover
    _apply_overrides(a)
    dry = a.dry_run if dry_run is None else dry_run
    given = getattr(a, "queries", None)
    qs = [{"q": q, "why": "given on the command line"} for q in given] if given else queries.generate(cfg)
    print("[scan] search queries:")
    for q in qs:
        print(f"   - {q['q']}   ({q['why'][:90]})")
    cands = yt_discover.scan(cfg, qs, getattr(a, "top", None), record=not dry)
    print(f"[scan] top {len(cands)} videos:")
    for i, v in enumerate(cands, 1):
        print(f"  {i}. {v['views_per_hour']:>8.0f} v/h  {v['duration'] / 60:5.1f} min  "
              f"{'[stream VOD] ' if v.get('was_live') else ''}"
              f"[{'OK to upload: ' + v['rights'] if v['rights'] != 'none' else 'review only, no permission'}]")
        print(f"     {v['channel']}: {v['title'][:70]}")
        print(f"     {v['url']}   (found by: {v['found_by']})")
    if dry or not cands:
        return 0
    qid = {q["q"]: q["id"] for q in qs}
    made = 0
    for i, v in enumerate(cands, 1):
        if STOP.exists():
            print("[scan] stop requested, not starting more videos")
            break
        print(f"\n[scan] video {i}/{len(cands)}")
        try:
            outs = pipeline.youtube_video(v, cfg)
            made += len(outs)
            if v["found_by"] in qid:
                state.credit_query(qid[v["found_by"]], len(outs))
        except Exception as e:
            from clipper.gemini import GeminiBusy, QuotaExhausted
            if isinstance(e, QuotaExhausted):
                state.set_video(v["video_id"], "queued")      # not its fault; retry next run
                raise
            if isinstance(e, GeminiBusy):                      # overloaded for minutes: don't burn
                state.set_video(v["video_id"], "queued")      # a retry attempt or the next videos
                print(f"[scan] Gemini is overloaded; stopping this round. {v['video_id']} is retried next round.")
                break
            state.set_video(v["video_id"], "error", error=str(e)[:800])
            print(f"[error] {v['video_id']}: {str(e).splitlines()[0][:200]}")
            if a.verbose:
                traceback.print_exc()
    print(f"\n[scan] done: {made} Short(s) from {len(cands)} videos in output/review/")
    return made


def cmd_discover(a):
    a.dry_run = True
    cmd_scan(a)


def twitch_round(a, dry_run=False):
    """Top Twitch clips for the game -> Gemini picks the window -> Shorts. Returns Shorts made."""
    from clipper import discover
    from clipper.gemini import GeminiBusy, QuotaExhausted
    if not discover.configured():
        print("[twitch] skipped: no TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET in .env")
        return 0
    cands = discover.find_candidates(cfg, getattr(a, "twitch_top", None) or (getattr(a, "top", None) if a.cmd == "twitch" else None))
    print(f"[twitch] {len(cands)} clips to process:")
    for i, c in enumerate(cands, 1):
        print(f"  {i}. {c['views']:>6} views  {c['duration']:4.0f}s  "
              f"[{'OK to upload: ' + c['rights'] if c['rights'] != 'none' else 'review only, no permission'}]")
        print(f"     {c['channel']}: {c['title'][:70]}")
        print(f"     {c['url']}")
    if dry_run or getattr(a, "dry_run", False):
        return 0
    made = 0
    for c in cands:
        if STOP.exists():
            print("[twitch] stop requested, not starting more clips")
            break
        try:
            made += len(pipeline.twitch_clip(c, cfg))
        except QuotaExhausted:
            state.set_video(c["source_id"], "queued")
            raise
        except GeminiBusy:
            state.set_video(c["source_id"], "queued")
            print("[twitch] Gemini is overloaded; stopping this round, the clip is retried next round.")
            break
        except Exception as e:
            state.set_video(c["source_id"], "error", error=str(e)[:800])
            print(f"[error] {c['source_id']}: {str(e).splitlines()[0][:200]}")
            if a.verbose:
                traceback.print_exc()
    print(f"[twitch] done: {made} Short(s) from {len(cands)} clips")
    return made


def cmd_twitch(a):
    _apply_overrides(a)
    twitch_round(a)


def _sources():
    """The enabled sources that can run, in order."""
    from clipper import discover
    out = []
    if "youtube" in cfg["sources"]:
        out.append("YouTube")
    if "twitch" in cfg["sources"] and discover.configured():
        out.append("Twitch")
    elif "twitch" in cfg["sources"]:
        print("[twitch] skipped: no TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET in .env")
    return out


def _twitch_needs_gemini():
    from clipper import openai_judge
    return cfg["twitch"].get("provider", "gemini") != "openai" or not openai_judge.configured()


def _run_source(name, a):
    """One round of one source. Returns (Shorts made, out of quota). Never raises."""
    from clipper.gemini import QuotaExhausted, all_capped
    if name == "YouTube" and all_capped(cfg["gemini"]["moments_model"]):
        # Don't spend YouTube search quota on videos Gemini can't watch until the reset.
        print("[youtube] skipped: every moments model is at its daily cap until the reset")
        return 0, True
    if name == "Twitch" and _twitch_needs_gemini() and all_capped(cfg["twitch"]["model"]):
        print("[twitch] skipped: every Twitch model is at its daily cap until the reset")
        return 0, True
    try:
        return (cmd_scan(a, dry_run=False) if name == "YouTube" else twitch_round(a)), False
    except QuotaExhausted as e:
        print(f"[{name.lower()}] out of quota for today: {e}")
        return 0, True
    except Exception as e:              # one source crashing shouldn't stop the other
        print(f"[{name.lower()}] round crashed: {type(e).__name__}: {e}")
        traceback.print_exc(file=sys.stdout)
        return 0, False


def _round(a):
    """One run round over every enabled source. Returns (Shorts made, sources out of quota, active)."""
    made, out_of_quota, active = 0, [], []
    for name in _sources():
        if STOP.exists():
            break
        active.append(name)
        n, capped = _run_source(name, a)
        made += n
        if capped:
            out_of_quota.append(name)
    return made, out_of_quota, active


def cmd_run(a):
    _apply_overrides(a)
    made, _, _ = _round(a)
    print(f"[run] done, {made} new Short(s)")


STOP = tools.WORK / "STOP"


class _Tee:
    """Print to the terminal and append timestamped lines to the log file."""
    def __init__(self, term, log):
        self.term, self.log, self.buf = term, log, ""

    def write(self, text):
        self.term.write(text)
        self.buf += text
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.log.write(f"{time.strftime('%H:%M:%S')} {line}\n")
        self.log.flush()
        return len(text)

    def flush(self):
        self.term.flush()


def _sleep_until(ts):
    """Sleep until ts, waking every 10 s to check for a stop request. True = stop."""
    while time.time() < ts:
        if STOP.exists():
            return True
        time.sleep(min(10, max(0.1, ts - time.time())))
    return STOP.exists()


def _next_quota_reset():
    import datetime as dt, zoneinfo
    pt = zoneinfo.ZoneInfo("America/Los_Angeles")
    now = dt.datetime.now(pt)
    reset = (now + dt.timedelta(days=1)).replace(hour=0, minute=10, second=0, microsecond=0)
    return reset.timestamp()


def _next_retry():
    """When a source that ran out of quota tries again: the daily reset, or the next Gemini key
    switch if that comes first (each key in .env, 2.env, 3.env ... has its own daily quota)."""
    from clipper import gemini
    wake = _next_quota_reset()
    hours = float(cfg["gemini"].get("key_rotation_hours") or 0)
    if hours > 0 and len(gemini._key_sets()) > 1:
        slot = hours * 3600
        wake = min(wake, (time.time() // slot + 1) * slot + 60)
    return wake


def cmd_loop(a):
    """YouTube on a fixed clock (--every), Twitch in between, every --twitch-every minutes."""
    _apply_overrides(a)
    if not a.upload:
        cfg["upload"]["mode"] = "review"          # nothing leaves this machine unless --upload
    cfg["twitch"]["rotate_windows"] = True        # each Twitch round: a different time window
    logdir = tools.ROOT / "logs"
    logdir.mkdir(exist_ok=True)
    log = open(logdir / f"loop-{time.strftime('%Y-%m-%d_%H%M')}.log", "a")
    sys.stdout = sys.stderr = _Tee(sys.__stdout__, log)
    STOP.unlink(missing_ok=True)
    sources = _sources()
    every = {"YouTube": a.every, "Twitch": a.twitch_every}
    print(f"[loop] {cfg['game_name']}: " + ", ".join(f"{n} every {every[n]} min" for n in sources)
          + f"; top {a.top or cfg['scan']['top_n']} YouTube videos per round, "
          f"uploads {'per config (' + cfg['upload']['mode'] + ')' if a.upload else 'OFF (review only)'}")
    print(f"[loop] log: {log.name}")
    print("[loop] stop: Ctrl+C here, or run `python main.py stop` (finishes the current video first)")
    if not sources:
        print("[loop] no source can run; check `sources` in config.yaml and the keys in .env")
        return
    due = {n: time.time() for n in sources}      # both start right away, YouTube first
    rounds = {n: 0 for n in sources}
    total = 0
    while not STOP.exists():
        name = min(sources, key=lambda n: (due[n], n != "YouTube"))
        if _sleep_until(due[name]):
            break
        rounds[name] += 1
        t0 = time.time()
        print(f"\n[loop] ===== {name} round {rounds[name]}, {time.strftime('%a %H:%M')} =====")
        made, capped = _run_source(name, a)
        total += made
        print(f"[loop] {name} round {rounds[name]}: {made} new Short(s) in {(time.time() - t0) / 60:.1f} min "
              f"({total} this session)")
        if capped:
            due[name] = _next_retry()
            print(f"[loop] {name} is out of quota; next {name} round at the next key switch or reset "
                  f"({time.strftime('%a %H:%M', time.localtime(due[name]))})")
        else:
            due[name] = t0 + every[name] * 60
        if STOP.exists():
            break
        _, clips, _ = state.summary()
        print("[loop] clips so far:", {r["status"]: r["n"] for r in clips})
        print("[loop] next: " + ", ".join(f"{n} at {time.strftime('%H:%M', time.localtime(max(due[n], time.time())))}"
                                          for n in sorted(sources, key=lambda n: due[n])))
    STOP.unlink(missing_ok=True)
    print("[loop] stopped: " + ", ".join(f"{rounds[n]} {n} round(s)" for n in sources)
          + f", {total} Short(s) this session")


def cmd_stop(a):
    tools.WORK.mkdir(exist_ok=True)
    STOP.touch()
    print("Stop requested: the loop finishes its current video, then exits (checks every 10 s while waiting).")


def cmd_status(a):
    videos, clips, recent = state.summary()
    print("videos:", {r["status"]: r["n"] for r in videos})
    print("clips: ", {r["status"]: r["n"] for r in clips})
    for r in recent:
        yt = f"https://youtu.be/{r['youtube_id']}" if r["youtube_id"] else (r["out_path"] or "")
        print(f"  {r['status']:9} {r['score'] or '':>3}  {r['clip_id']}  {(r['title'] or '')[:50]}  {yt}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("clip"); c.add_argument("target")
    c.add_argument("--no-audit", action="store_true"); c.add_argument("--force", action="store_true")
    c.add_argument("--start", type=tools.parse_ts); c.add_argument("--end", type=tools.parse_ts)
    c.add_argument("--title")
    c.add_argument("--game", help='override game_name for this run, e.g. "PUBG"')
    for name in ("audit", "moments"):
        sp = sub.add_parser(name); sp.add_argument("url"); sp.add_argument("--game")
    sub.add_parser("approve").add_argument("clip_id")
    for name in ("scan", "discover", "loop", "twitch", "run"):
        sp = sub.add_parser(name)
        sp.add_argument("--profile", help="game profile from config.yaml, e.g. cs2 or gta6")
        sp.add_argument("--dry-run", action="store_true")
        sp.add_argument("--queries", nargs="+", help="skip the generator and search these")
        sp.add_argument("--top", type=int, help="how many videos to process (default scan.top_n)")
        sp.add_argument("--hours", type=int, help="only videos uploaded in the last H hours")
        sp.add_argument("--max-minutes", type=int, help="skip source videos longer than this")
        sp.add_argument("--game")
    lp = sub.choices["loop"]
    lp.add_argument("--every", type=int, default=60, help="minutes between YouTube round starts (default 60)")
    lp.add_argument("--twitch-every", type=int, default=5,
                    help="minutes between Twitch round starts; runs between YouTube rounds (default 5)")
    lp.add_argument("--upload", action="store_true", help="allow uploads per config (default: review only)")
    lp.set_defaults(top=None)
    for name in ("auth", "status", "stop"):
        sub.add_parser(name)
    a = p.parse_args()
    if a.cmd == "approve":
        return pipeline.approve(a.clip_id, cfg)
    if a.cmd == "auth":
        from clipper import upload
        return upload.authorize()
    {"clip": cmd_clip, "audit": cmd_audit, "moments": cmd_moments, "discover": cmd_discover, "scan": cmd_scan, "loop": cmd_loop, "stop": cmd_stop,
     "run": cmd_run, "twitch": cmd_twitch, "status": cmd_status}[a.cmd](a)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped (Ctrl+C). Unfinished videos are picked up again next time.")
    except RuntimeError as e:
        sys.exit(f"error: {e}")
