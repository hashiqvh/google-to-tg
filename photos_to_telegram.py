import os, json, time, mimetypes
from pathlib import Path
import requests
from tqdm import tqdm

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import AuthorizedSession, Request

# ── EDIT THESE ────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.getenv("TG_BOT_TOKEN",   "YOUR_TELEGRAM_BOT_TOKEN")
CHANNEL_ID  = os.getenv("TG_CHANNEL_ID",  "@yourchannelusername")  # or -1001234567890
PAGE_SIZE   = 100   # Photos API max page size
BATCH_SLEEP = 0.5   # seconds between Telegram uploads
# ─────────────────────────────────────────────────────────────────────────────

# IMPORTANT: These legacy scopes are no longer available for most new apps (Mar 31, 2025).
# This will only work if your project is allowlisted/verified for full-library access.
SCOPES = ["https://www.googleapis.com/auth/photoslibrary.readonly"]

TOKEN_FILE = "token.json"
PROGRESS_FILE = "processed_ids.jsonl"

PHOTOS_BASE = "https://photoslibrary.googleapis.com/v1/mediaItems"

def get_creds():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            # Spins up localhost receiver for OAuth
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds

def load_done_ids():
    done = set()
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE) as f:
            for line in f:
                try:
                    done.add(json.loads(line)["id"])
                except Exception:
                    pass
    return done

def mark_done(item_id):
    with open(PROGRESS_FILE, "a") as f:
        f.write(json.dumps({"id": item_id}) + "\n")

def download_bytes(session: AuthorizedSession, item: dict) -> tuple[bytes,str,str]:
    """Returns (content_bytes, filename, mime_type). Uses =d for photos, =dv for videos."""
    base_url = item.get("baseUrl")
    mime_type = item.get("mimeType", "")
    filename  = item.get("filename") or f'{item["id"]}'

    if not base_url:
        raise RuntimeError("No baseUrl for media item")

    if mime_type.startswith("video/"):
        url = base_url + "=dv"   # high-quality transcoded video bytes
    else:
        url = base_url + "=d"    # download with EXIF (no location data)
    # Auth header required; baseUrl is NOT public
    r = session.get(url, stream=True)
    r.raise_for_status()
    content = r.content
    return content, filename, mime_type

def send_to_telegram(content: bytes, filename: str, mime_type: str):
    api = f"https://api.telegram.org/bot{BOT_TOKEN}"
    files = None
    data = {"chat_id": CHANNEL_ID, "caption": filename}

    # Prefer type-specific methods for better UX in channel
    try:
        if mime_type.startswith("image/"):
            files = {"photo": (filename, content, mime_type)}
            url = f"{api}/sendPhoto"
        elif mime_type.startswith("video/"):
            files = {"video": (filename, content, mime_type)}
            url = f"{api}/sendVideo"
        else:
            # Fallback as document for other types (e.g., HEIC, RAW)
            files = {"document": (filename, content, mime_type or "application/octet-stream")}
            url = f"{api}/sendDocument"

        resp = requests.post(url, data=data, files=files, timeout=300)
        j = resp.json() if resp.headers.get("content-type","").startswith("application/json") else {}
        if resp.status_code != 200 or not j.get("ok", False):
            raise RuntimeError(f"Telegram error: {resp.status_code} {j}")
    finally:
        files = None

def main():
    # Basic checks
    if "YOUR_TELEGRAM_BOT_TOKEN" in BOT_TOKEN:
        raise SystemExit("Please set BOT_TOKEN / TG_BOT_TOKEN.")

    creds = get_creds()
    session = AuthorizedSession(creds)

    done_ids = load_done_ids()
    page_token = None
    total_sent = 0

    with tqdm(desc="Exporting", unit="item") as bar:
        while True:
            params = {"pageSize": PAGE_SIZE}
            if page_token:
                params["pageToken"] = page_token

            # List media items (requires legacy read scope)
            r = session.get(PHOTOS_BASE, params=params)
            if r.status_code == 403:
                raise SystemExit(
                    "403 Insufficient scopes from Google Photos API.\n"
                    "Google removed general library read scopes in 2025. "
                    "Unless your project is approved/allowlisted, you can't enumerate the full library."
                )
            r.raise_for_status()
            data = r.json()
            items = data.get("mediaItems", []) or []

            if not items:
                break

            for it in items:
                if it["id"] in done_ids:
                    continue
                try:
                    content, filename, mime_type = download_bytes(session, it)
                    # If filename has no extension, guess one
                    if "." not in filename:
                        ext = mimetypes.guess_extension(mime_type or "") or ""
                        filename = filename + ext
                    send_to_telegram(content, filename, mime_type)
                    mark_done(it["id"])
                    total_sent += 1
                    bar.update(1)
                    time.sleep(BATCH_SLEEP)
                except Exception as e:
                    print(f"⚠️  Skipped {it.get('id')} ({it.get('filename')}): {e}")

            page_token = data.get("nextPageToken")
            if not page_token:
                break

    print(f"✅ Done. Posted {total_sent} items to {CHANNEL_ID}.")

if __name__ == "__main__":
    main()
