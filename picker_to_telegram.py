import os, time, json, mimetypes
from pathlib import Path
import requests
from dotenv import load_dotenv
from tqdm import tqdm

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import AuthorizedSession, Request

load_dotenv()

BOT_TOKEN  = os.getenv("TG_BOT_TOKEN")
CHANNEL_ID = os.getenv("TG_CHANNEL_ID")

SCOPE  = ["https://www.googleapis.com/auth/photospicker.mediaitems.readonly"]
BASE   = "https://photospicker.googleapis.com/v1"

def get_creds():
    creds = None
    if Path("token_picker.json").exists():
        creds = Credentials.from_authorized_user_file("token_picker.json", SCOPE)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPE)
            creds = flow.run_local_server(port=0)
        Path("token_picker.json").write_text(creds.to_json())
    return creds

def create_session(sess: AuthorizedSession) -> dict:
    r = sess.post(f"{BASE}/sessions", json={})
    r.raise_for_status()
    return r.json()  # includes id and pickerUri

def get_session(sess: AuthorizedSession, sid: str) -> dict:
    r = sess.get(f"{BASE}/sessions/{sid}")
    r.raise_for_status()
    return r.json()

def list_picked(sess: AuthorizedSession, sid: str, page_size=100):
    params = {"sessionId": sid, "pageSize": page_size}
    while True:
        r = sess.get(f"{BASE}/mediaItems", params=params)
        if r.status_code == 412 or r.status_code == 400:
            # user hasn't tapped Done yet (FAILED_PRECONDITION via guides)
            time.sleep(3); continue
        r.raise_for_status()
        data = r.json()
        for item in data.get("mediaItems", []):
            yield item
        nxt = data.get("nextPageToken")
        if not nxt: break
        params["pageToken"] = nxt

def dl_bytes(sess: AuthorizedSession, item: dict):
    # PickedMediaItem structure can carry media file under mediaFile; be defensive:
    mf = item.get("mediaFile", {})
    base_url = mf.get("baseUrl") or item.get("baseUrl")
    mime     = mf.get("mimeType") or item.get("mimeType", "")
    fn       = item.get("filename") or mf.get("filename") or f"{item.get('id','file')}"
    if not base_url: raise RuntimeError("No baseUrl in picked item")
    url = base_url + ("=dv" if mime.startswith("video/") else "=d")
    r = sess.get(url)
    r.raise_for_status()
    return r.content, fn, mime

def tg_send(content: bytes, filename: str, mime: str):
    api = f"https://api.telegram.org/bot{BOT_TOKEN}"
    data = {"chat_id": CHANNEL_ID}
    if mime.startswith("image/"):
        files = {"photo": (filename, content, mime or "image/jpeg")}
        url = f"{api}/sendPhoto"
    elif mime.startswith("video/"):
        files = {"video": (filename, content, mime or "video/mp4")}
        url = f"{api}/sendVideo"
    else:
        files = {"document": (filename, content, mime or "application/octet-stream")}
        url = f"{api}/sendDocument"
    resp = requests.post(url, data=data, files=files, timeout=300)
    j = resp.json()
    if resp.status_code != 200 or not j.get("ok", False):
        raise RuntimeError(f"Telegram error {resp.status_code}: {j}")

def main():
    if not BOT_TOKEN or not CHANNEL_ID:
        raise SystemExit("Set TG_BOT_TOKEN and TG_CHANNEL_ID in .env")

    creds = get_creds()
    sess  = AuthorizedSession(creds)

    # 1) create picking session
    s = create_session(sess)
    sid = s["id"]; picker_uri = s["pickerUri"]
    print("\nOpen this Picker URL on your phone and select items, then tap Done:\n")
    print(picker_uri, "\n")

    # 2) poll until user finished
    while True:
        st = get_session(sess, sid)
        if st.get("mediaItemsSet"):
            break
        poll = (st.get("pollingConfig", {}) or {}).get("pollInterval", 3)
        try: poll = int(poll)
        except: poll = 3
        time.sleep(max(2, poll))

    # 3) list picked items and forward to Telegram
    count = 0
    for it in tqdm(list_picked(sess, sid), desc="Uploading to Telegram", unit="item"):
        try:
            content, fn, mime = dl_bytes(sess, it)
            tg_send(content, fn, mime)
            count += 1
            time.sleep(0.5)  # be gentle with rate limits
        except Exception as e:
            print("⚠️ Skipped:", e)

    # 4) optional cleanup
    try:
        sess.delete(f"{BASE}/sessions/{sid}")
    except Exception:
        pass

    print(f"Done. Posted {count} items.")

if __name__ == "__main__":
    main()
