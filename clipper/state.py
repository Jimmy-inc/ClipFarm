"""SQLite state: each source video is audited once and each clip is rendered/uploaded once.

Errors are retried on later runs until retry.max_attempts is reached.
"""
import json, sqlite3, time
from .tools import WORK

DB = WORK / "state.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos(
    video_id TEXT PRIMARY KEY, url TEXT, channel TEXT, title TEXT, rights TEXT,
    status TEXT, audit TEXT, attempts INTEGER DEFAULT 0, error TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS queries(
    id INTEGER PRIMARY KEY AUTOINCREMENT, q TEXT, ts REAL, found INTEGER DEFAULT 0,
    clips INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS clips(
    clip_id TEXT PRIMARY KEY, video_id TEXT, url TEXT, start REAL, "end" REAL,
    score REAL, title TEXT, status TEXT, out_path TEXT, meta TEXT,
    youtube_id TEXT, publish_at REAL, attempts INTEGER DEFAULT 0, error TEXT, updated REAL);
"""


def conn():
    DB.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    cols = {r[1] for r in c.execute("PRAGMA table_info(videos)")}
    if "moments" not in cols:                       # added after the first release
        c.execute("ALTER TABLE videos ADD COLUMN moments TEXT")
    return c


def _upsert(table, key, key_val, status, **fields):
    with conn() as c:
        c.execute(f"INSERT OR IGNORE INTO {table}({key}, status, updated) VALUES(?,?,?)",
                  (key_val, status, time.time()))
        if status == "error":
            fields["attempts"] = (c.execute(f"SELECT attempts FROM {table} WHERE {key}=?",
                                            (key_val,)).fetchone()[0] or 0) + 1
        for k, v in list(fields.items()):
            if isinstance(v, (dict, list)):
                fields[k] = json.dumps(v)
        sets = ", ".join(['status=?', 'updated=?'] + [f'"{k}"=?' for k in fields])
        c.execute(f"UPDATE {table} SET {sets} WHERE {key}=?",
                  [status, time.time(), *fields.values(), key_val])


def set_video(video_id, status, **fields):
    _upsert("videos", "video_id", video_id, status, **fields)


def set_clip(clip_id, status, **fields):
    _upsert("clips", "clip_id", clip_id, status, **fields)


def video_seen(video_id, max_attempts=3):
    with conn() as c:
        r = c.execute("SELECT status, attempts FROM videos WHERE video_id=?", (video_id,)).fetchone()
    if r is None:
        return False
    # Finished for good: done, rejected, or failed too often. Anything else (queued, processing,
    # audited, moments, a recent error) is unfinished and gets picked up again.
    return r["status"] in ("done", "rejected") or (r["status"] == "error" and r["attempts"] >= max_attempts)


def video_moments(video_id):
    with conn() as c:
        r = c.execute("SELECT moments FROM videos WHERE video_id=?", (video_id,)).fetchone()
    return json.loads(r["moments"]) if r and r["moments"] else None


def video_audit(video_id):
    """A stored audit, so a retry (after Gemini was busy) doesn't spend another request on it."""
    with conn() as c:
        r = c.execute("SELECT audit FROM videos WHERE video_id=?", (video_id,)).fetchone()
    return json.loads(r["audit"]) if r and r["audit"] else None


def add_query(q, found):
    with conn() as c:
        return c.execute("INSERT INTO queries(q, ts, found) VALUES(?,?,?)", (q, time.time(), found)).lastrowid


def credit_query(query_id, clips):
    with conn() as c:
        c.execute("UPDATE queries SET clips = clips + ? WHERE id=?", (clips, query_id))


def query_history(n=30):
    with conn() as c:
        return c.execute("SELECT q, found, clips FROM queries ORDER BY ts DESC LIMIT ?", (n,)).fetchall()


def clip(clip_id):
    with conn() as c:
        return c.execute("SELECT * FROM clips WHERE clip_id=?", (clip_id,)).fetchone()


def uploads_last_24h():
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM clips WHERE status='uploaded' AND updated>?",
                         (time.time() - 86400,)).fetchone()[0]


def next_publish_slot(gap_hours):
    """Space scheduled videos gap_hours apart, continuing after the latest one already scheduled."""
    with conn() as c:
        last = c.execute("SELECT MAX(publish_at) FROM clips WHERE status='uploaded'").fetchone()[0]
    now = time.time() + 15 * 60                     # YouTube needs publishAt in the future
    return max(now, (last or 0) + gap_hours * 3600)


def summary():
    with conn() as c:
        v = c.execute("SELECT status, COUNT(*) n FROM videos GROUP BY status").fetchall()
        k = c.execute("SELECT status, COUNT(*) n FROM clips GROUP BY status").fetchall()
        recent = c.execute("SELECT clip_id, status, score, title, out_path, youtube_id FROM clips "
                           "ORDER BY updated DESC LIMIT 15").fetchall()
    return v, k, recent
