"""YouTube Data API v3 upload with scheduling.

One-time setup: create an OAuth "Desktop app" client in Google Cloud Console, enable
YouTube Data API v3, save it as client_secret.json next to main.py, then run
`python main.py auth` once in a terminal.

Two things that silently break automation if skipped:
  - Set the OAuth consent screen's publishing status to "In production". In "Testing",
    refresh tokens expire after 7 days.
  - Uploads from an unaudited API project are locked to private. Submit the YouTube API
    compliance audit before expecting public or scheduled videos.
"""
import datetime as dt, sys
from pathlib import Path
from .tools import ROOT, WORK

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
TOKEN = WORK / "token.json"


def authorize():
    from google_auth_oauthlib.flow import InstalledAppFlow
    secret = ROOT / "client_secret.json"
    if not secret.exists():
        sys.exit(f"Missing {secret}. Download your OAuth Desktop client JSON there.")
    creds = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES).run_local_server(port=0)
    WORK.mkdir(exist_ok=True)
    TOKEN.write_text(creds.to_json())
    print(f"Saved {TOKEN}")


def _service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google.auth.exceptions import RefreshError
    from googleapiclient.discovery import build
    if not TOKEN.exists():
        raise RuntimeError("Not authorised with YouTube. Run `python main.py auth` once in a terminal.")
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds.valid:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise RuntimeError("YouTube token expired or revoked. If this happens every ~7 days, set the "
                               "OAuth consent screen to 'In production'. Then run `python main.py auth`.") from e
        TOKEN.write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def build_title(title: str) -> str:
    tag = " #Shorts"
    title = " ".join(title.split())
    if "#shorts" in title.lower():
        return title[:100]
    return title[: 100 - len(tag)].rstrip() + tag


def credit_block(src: dict) -> str:
    lines = [f"\n\nClip from {src['channel']}: {src['url']}"]
    if src.get("channel_url"):
        lines.append(f"Watch {src['channel']} live: {src['channel_url']}")
    basis = src.get("rights", "")
    if basis.startswith("permission"):
        lines.append("Posted with the creator's permission.")
    elif "Creative Commons" in basis:
        lines.append("Original licensed CC BY (https://creativecommons.org/licenses/by/3.0/).")
    return "\n".join(lines)


def upload(path: Path, meta: dict, src: dict, cfg, publish_at: float | None = None) -> str:
    from googleapiclient.http import MediaFileUpload
    up = cfg["upload"]
    tags = list(dict.fromkeys(meta.get("tags", []) + up.get("extra_tags", [])))[:15]
    body = {
        "snippet": {"title": build_title(meta["title"]),
                    "description": meta.get("description", "") + credit_block(src) + "\n\n" + up.get("hashtags", "#Shorts"),
                    "tags": tags, "categoryId": up["category_id"]},
        "status": {"privacyStatus": up["privacy"], "selfDeclaredMadeForKids": up["made_for_kids"]},
    }
    if publish_at and up["privacy"] == "private" and up.get("schedule", True):
        body["status"]["publishAt"] = dt.datetime.fromtimestamp(publish_at, dt.timezone.utc
                                                                ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    req = _service().videos().insert(part="snippet,status", body=body,
                                     media_body=MediaFileUpload(str(path), resumable=True,
                                                                chunksize=8 * 1024 * 1024))
    resp = None
    while resp is None:
        _, resp = req.next_chunk(num_retries=3)
    return resp["id"]
