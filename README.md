# GTA 6 Shorts pipeline (GTA VI launched Nov 19, 2026)

```
scan: Gemini writes search queries (checks what's trending, learns from past queries)
   -> YouTube search -> filter (game in title, length, views, not seen) -> top 5 by views/hour
   -> audit   (Gemini watches the URL: is it GTA 6 gameplay, how clippable?)
   -> moments (Gemini watches the URL in 40-min chunks + "most replayed" heatmap -> top windows, titles)
   -> download only those sections at 1080p (yt-dlp, padded 4 s each side)
   -> Whisper word timestamps -> snap cuts to sentence boundaries
   -> render 1080x1920: blurred bg, zoomed gameplay, word-highlighted captions, hook banner, loudness -14 LUFS
   -> output/review/  (or straight to YouTube in auto mode)
```
```
twitch: top clips of the game (Twitch API, last 48 h, most viewed) -> drop non-English, esports channels,
   duplicates of the same moment, max 2 per streamer -> download the clip -> OpenAI judges 1 frame/s + the
   transcript (twitch.provider: openai; or Gemini watches the clip itself) -> best window + title -> same render
```
Clips that aren't brand-safe (sexual content, slurs, real-money gambling) are still made, but go to
`output/flagged/` instead of `output/review/` and are never auto-uploaded. `approve` works on them.
```
```
Local recordings use the same render path, with Claude picking the window from the transcript.

## Setup (macOS)
```bash
brew install ffmpeg-full deno          # plain `ffmpeg` lacks libass (captions); deno is needed by yt-dlp for YouTube
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env                   # fill in the keys
```
- `GEMINI_API_KEY`: aistudio.google.com. Free tier: 8 h of YouTube video per day. Public videos only.
- `YOUTUBE_API_KEY`: Google Cloud, YouTube Data API v3, API key. Use a **different Cloud project** from uploads
  so discovery and uploads each get 10,000 quota units/day.
- `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET`: dev.twitch.tv/console/apps -> Register Your Application
  (redirect URL `http://localhost`, client type Confidential; Twitch requires two-factor login first),
  then Manage -> copy the Client ID, click New Secret. Without them the Twitch step is skipped with a note.
  Several Twitch apps: copy `.env` to `2.env`, `3.env`, ... and put another app's Twitch Client ID and
  secret in each (only their TWITCH_ lines are used; every other key comes from `.env`). The app in use
  switches every `twitch.key_rotation_hours` (8) by the clock, and the log says which file it is on.
- `OPENAI_API_KEY` (optional): platform.openai.com -> API keys; needs prepaid credit (a ChatGPT subscription
  doesn't include the API). Judges Twitch clips so Gemini's free quota goes to YouTube. Empty = Gemini does it.
- `ANTHROPIC_API_KEY`: only for local files without `--start/--end`.

## Use
```bash
.venv/bin/python main.py moments "https://youtu.be/VIDEO"   # cheapest test: what Gemini would cut, no downloads
.venv/bin/python main.py clip "https://youtu.be/VIDEO"      # one video -> Shorts in output/review/
.venv/bin/python main.py clip ~/Movies/my_session.mp4       # your own recording
.venv/bin/python main.py clip ~/Movies/x.mp4 --start 1:02:10 --end 1:02:45 --title "..."
.venv/bin/python main.py scan --dry-run                     # queries + the 5 videos it would pick, no Gemini video calls
.venv/bin/python main.py scan                               # queries -> top 5 videos -> Shorts in output/review/
.venv/bin/python main.py scan --queries "GTA 6 heist fails" --top 2 --hours 24
.venv/bin/python main.py scan --game "Minecraft" --top 2    # test the loop on another game
# The default profile is gta6 (config: default_profile); add --profile cs2 for the CS2 test setup.
.venv/bin/python main.py loop --top 3                       # GTA 6: YouTube every 60 min, Twitch every 5 min in between, review only
.venv/bin/python main.py loop --top 3 --every 60 --twitch-every 10   # set each clock yourself
.venv/bin/python main.py twitch --profile cs2 --dry-run    # the Twitch clips it would pick
.venv/bin/python main.py twitch --profile cs2 --top 2       # top Twitch clips -> Shorts
.venv/bin/python main.py loop --profile cs2 --top 3 --every 90   # YouTube + Twitch every 90 min, review only
.venv/bin/python main.py run                                # one round of every source + upload per config
.venv/bin/python main.py status
```
In the loop, every YouTube round searches "GTA 6" (the profile's `fixed_queries`) plus queries it has never
searched before (Gemini writes close variations of the ones that worked; exact repeats are dropped), and each Twitch round looks at a different
time window (`twitch.windows_hours`: last 3 h, 12 h, 48 h, in turn). A source that runs out of quota waits
for the next Gemini key switch or the midnight-Pacific reset; the other keeps going.

Each Short gets a `.json` sidecar (title, description, pinned comment, tags, source, rights basis) and a `.txt`
with the title, the pinned-comment blurb (context + a credit line) and the description, ready to paste into
YouTube Studio, next to the `.mp4`. YouTube's API can't pin comments, so pinning stays manual.

## Rights gate (at upload)
`scan` renders the top videos whatever their source, so you can judge the bot's picks. Each clip's
`.json` records its rights basis. **`approve` and auto-upload refuse clips without one**: your own
channels, `rights.permitted_channels` (with proof), or Creative Commons BY. Adding a creator to
`permitted_channels` (or a Twitch streamer to `twitch.streamers` with `permission: true`) later unlocks their
already-rendered clips. A Twitch clip belongs to the streamer, not the viewer who clipped it. Set `scan.only_rights_cleared: true`
to skip uncleared videos entirely. Uncleared channels also go to `output/permission_leads.csv`
(your outreach list). `clip URL` counts as "manual": only use it on videos you may reuse.

## Reliability built in
- Each Gemini step has a list of fallback models; an overloaded one (503/500/429) hands over immediately.
- Free tier is ~20 requests/day per model: a capped model is skipped until midnight Pacific. Twitch uses the
  flash-lite models, which YouTube's moments step doesn't, so the two sources don't share a budget.
- Gemini overloaded through every retry: the round stops and the video is retried next round, without
  counting as a failed attempt. A stored audit is reused on retry.
- Quota exhaustion stops the run with a clear message; the video is retried next run.
- YouTube 403s on section downloads are retried with fresh stream URLs.
- A video's moment list is saved, so re-running finishes only the failed clips, with the same picks.
- Long streams are watched in full (gta6: `gemini.max_scan_minutes: 0`), 40 min per Gemini call. Each finished
  chunk is saved in `work/moment_chunks/`, so a stream cut off by the daily quota resumes at the next chunk,
  and the next YouTube round picks it up first even if no search finds it again.

## Uploading to YouTube
1. Same Cloud project as uploads: enable YouTube Data API v3, OAuth consent screen -> **In production**
   (in Testing, tokens die after 7 days), create an OAuth *Desktop* client, save as `client_secret.json` here.
2. `.venv/bin/python main.py auth` once in a terminal.
3. Submit the YouTube API compliance audit; until it passes, every upload is forced private.
4. `python main.py approve CLIP_ID` per reviewed clip, or set `upload.mode: auto`.

## Schedule
`0 */4 * * * cd "/path/to/ClipFarm" && .venv/bin/python main.py run >> run.log 2>&1`
(The Mac must be awake. launchd survives sleep better than cron.)

## Notes
- Gemini timestamps come from ~1 fps sampling, so they are rough to a second or two. The padding plus Whisper
  refinement fixes the edges; gameplay with no speech keeps Gemini's cut.
- Free-tier Gemini: Google Search grounding (used by the query generator) has a much smaller
  quota than plain calls; when it runs out, queries are written without it.
- `gemini.processing: agentic` lets the model navigate long videos itself instead of fixed chunks. Untested here.
- YouTube's "inauthentic content" policy targets mass-produced reposts. Keep a human review step.
