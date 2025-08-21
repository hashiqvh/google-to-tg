import os, json, time, sqlite3, threading, urllib.parse
from pathlib import Path
from typing import Optional
import requests

from dotenv import load_dotenv
from fastapi import FastAPI, Request
import uvicorn

from telegram import Update, Chat, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# Load .env in current folder (adjust if your .env is elsewhere)
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
PUBLIC_BASE = os.getenv("PUBLIC_BASE_URL", "")
REDIRECT_PATH = os.getenv("REDIRECT_PATH", "/oauth/callback")
REDIRECT_URI = f"{PUBLIC_BASE}{REDIRECT_PATH}"

DB_PATH = "bot.db"
PHOTO_MAX = 10 * 1024 * 1024     # 10MB photo limit
FILE_MAX  = 2 * 1024**3          # 2GB bot API limit

PHOTOS_PICKER_BASE = "https://photospicker.googleapis.com/v1"
SCOPE = "https://www.googleapis.com/auth/photospicker.mediaitems.readonly openid email"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

# --- DB helpers ---
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    conn = db()
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        tg_id INTEGER PRIMARY KEY,
        email TEXT,
        access_token TEXT,
        refresh_token TEXT,
        token_type TEXT,
        expiry INTEGER
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS channels(
        tg_id INTEGER,
        chat_id TEXT,
        title TEXT,
        PRIMARY KEY (tg_id)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS link_codes(
        code TEXT PRIMARY KEY,
        tg_id INTEGER
    )""")
    conn.commit()
    conn.close()

def save_tokens(tg_id:int, token:dict, email:str|None=None):
    conn = db()
    expiry = int(time.time()) + int(token.get("expires_in", 3600)) - 60
    conn.execute("INSERT OR REPLACE INTO users(tg_id,email,access_token,refresh_token,token_type,expiry) VALUES(?,?,?,?,?,?)",
                 (tg_id, email, token.get("access_token"), token.get("refresh_token"),
                  token.get("token_type"), expiry))
    conn.commit(); conn.close()

def get_user(tg_id:int)->Optional[dict]:
    conn = db()
    cur = conn.execute("SELECT tg_id,email,access_token,refresh_token,token_type,expiry FROM users WHERE tg_id=?", (tg_id,))
    row = cur.fetchone(); conn.close()
    if not row: return None
    keys = ["tg_id","email","access_token","refresh_token","token_type","expiry"]
    return dict(zip(keys, row))

def save_channel(tg_id:int, chat_id:str, title:str|None):
    conn = db()
    conn.execute("INSERT OR REPLACE INTO channels(tg_id,chat_id,title) VALUES(?,?,?)", (tg_id, chat_id, title))
    conn.commit(); conn.close()

def get_channel(tg_id:int)->Optional[dict]:
    conn = db()
    cur = conn.execute("SELECT tg_id,chat_id,title FROM channels WHERE tg_id=?", (tg_id,))
    row = cur.fetchone(); conn.close()
    if not row: return None
    return {"tg_id":row[0], "chat_id":row[1], "title":row[2]}

def put_link_code(code:str, tg_id:int):
    conn = db()
    conn.execute("INSERT OR REPLACE INTO link_codes(code,tg_id) VALUES(?,?)", (code, tg_id))
    conn.commit(); conn.close()

def pop_link_code(code:str)->Optional[int]:
    conn = db()
    cur = conn.execute("SELECT tg_id FROM link_codes WHERE code=?", (code,))
    row = cur.fetchone()
    if row:
        conn.execute("DELETE FROM link_codes WHERE code=?", (code,))
        conn.commit()
    conn.close()
    return row[0] if row else None

# --- Google OAuth helpers ---
def oauth_url(state:str):
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

def exchange_code(code:str)->dict:
    r = requests.post(TOKEN_URL, data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI
    }, timeout=30)
    r.raise_for_status()
    return r.json()

def refresh_token(refresh_token:str)->dict:
    r = requests.post(TOKEN_URL, data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token
    }, timeout=30)
    r.raise_for_status()
    return r.json()

def get_access_token(u:dict)->str:
    if int(time.time()) < (u.get("expiry") or 0) and u.get("access_token"):
        return u["access_token"]
    if u.get("refresh_token"):
        t = refresh_token(u["refresh_token"])
        # sometimes refresh response omits refresh_token; keep old one
        t.setdefault("refresh_token", u["refresh_token"])
        save_tokens(u["tg_id"], t, u.get("email"))
        return t["access_token"]
    raise RuntimeError("No valid Google token; use /connect")

# --- Telegram send helpers (FIXED API) ---
def tg_send(method: str, *, chat_id: int | str, files=None, **data):
    """
    Send a Telegram API call.
    Usage: tg_send("sendMessage", chat_id=123, text="hi")
           tg_send("sendPhoto", chat_id=..., files={"photo": (..., bytes, mime)})
    """
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    payload = {"chat_id": chat_id, **data}
    r = requests.post(url, data=payload, files=files, timeout=300)
    j = r.json() if r.headers.get("content-type","").startswith("application/json") else {}
    if r.status_code != 200 or not j.get("ok", False):
        raise RuntimeError(f"Telegram error {r.status_code}: {j}")
    return j

def send_media_auto(dest_chat_id:str, name:str, content:bytes, mime:str|None):
    if len(content) > FILE_MAX:
        raise RuntimeError("File exceeds 2GB")
    if (mime or "").startswith("image/") and len(content) <= PHOTO_MAX:
        files = {"photo": (name, content, mime or "image/jpeg")}
        return tg_send("sendPhoto", chat_id=dest_chat_id, files=files)
    elif (mime or "").startswith("video/"):
        files = {"video": (name, content, mime or "video/mp4")}
        return tg_send("sendVideo", chat_id=dest_chat_id, files=files)
    else:
        files = {"document": (name, content, mime or "application/octet-stream")}
        return tg_send("sendDocument", chat_id=dest_chat_id, files=files)

# --- Picker flow ---
def create_picker_session(access_token:str)->dict:
    r = requests.post(f"{PHOTOS_PICKER_BASE}/sessions",
                      headers={"Authorization": f"Bearer {access_token}"},
                      json={}, timeout=30)
    r.raise_for_status()
    return r.json()  # {id, pickerUri, pollingConfig?}

def session_ready(access_token:str, sid:str)->dict:
    r = requests.get(f"{PHOTOS_PICKER_BASE}/sessions/{sid}",
                     headers={"Authorization": f"Bearer {access_token}"},
                     timeout=30)
    r.raise_for_status()
    return r.json()

def iter_picked(access_token:str, sid:str):
    params = {"sessionId": sid, "pageSize": 100}
    while True:
        r = requests.get(f"{PHOTOS_PICKER_BASE}/mediaItems",
                         headers={"Authorization": f"Bearer {access_token}"},
                         params=params, timeout=60)
        if r.status_code == 412 or r.status_code == 400:
            time.sleep(3); continue
        r.raise_for_status()
        data = r.json()
        for it in data.get("mediaItems", []):
            yield it
        nxt = data.get("nextPageToken")
        if not nxt: break
        params["pageToken"] = nxt

def download_item(access_token:str, item:dict)->tuple[bytes,str,str]:
    mf = item.get("mediaFile", {}) or {}
    base_url = mf.get("baseUrl") or item.get("baseUrl")
    mime = mf.get("mimeType") or item.get("mimeType") or ""
    name = item.get("filename") or mf.get("filename") or f"{item.get('id','file')}"
    if not base_url: raise RuntimeError("No baseUrl")
    url = base_url + ("=dv" if mime.startswith("video/") else "=d")
    r = requests.get(url, headers={"Authorization": f"Bearer {access_token}"}, timeout=300)
    r.raise_for_status()
    return r.content, name, mime

# --- FastAPI for OAuth callback ---
app = FastAPI()

@app.get(REDIRECT_PATH)
def oauth_cb(request: Request):
    params = dict(request.query_params)
    if "error" in params:
        return {"ok": False, "error": params["error"]}
    code = params.get("code"); state = params.get("state")
    if not code or not state: return {"ok": False, "error": "missing code/state"}
    tg_id = int(state.split(":")[0])   # simple state "TGID:nonce"
    token = exchange_code(code)

    # (Optional) fetch email
    userinfo = requests.get("https://openidconnect.googleapis.com/v1/userinfo",
                            headers={"Authorization": f"Bearer {token['access_token']}"},
                            timeout=20)
    email = userinfo.json().get("email") if userinfo.ok else None

    save_tokens(tg_id, token, email)
    # notify user in DM
    tg_send("sendMessage", chat_id=tg_id,
            text="✅ Google connected (Picker). Use /picker to select photos.")
    return {"ok": True}

def run_api():
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")

# --- Telegram bot handlers ---
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! I can move your Google Photos to a Telegram channel.\n\n"
        "1) /connect – link your Google (Picker)\n"
        "2) /setchannel – link the destination channel\n"
        "3) /picker – pick photos/videos and I'll post them there\n"
        "4) /help – tips"
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "• /connect opens Google login (Picker scope)\n"
        "• /setchannel – either forward a message from your channel here, or add me as admin and post '/link <code>' in the channel\n"
        "• /picker – I’ll send you a link to pick items; after you tap Done, I’ll upload them\n"
        "• Big images (>10MB) go as documents to preserve quality"
    )

async def connect_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    nonce = int(time.time())
    state = f"{tg_id}:{nonce}"
    url = oauth_url(state)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Connect Google", url=url)]])
    await update.message.reply_text("Connect your Google account (Picker scope):", reply_markup=kb)

async def setchannel_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    code = str(int(time.time()))[-6:]  # simple code
    put_link_code(code, update.effective_user.id)
    await update.message.reply_text(
        "Linking channel:\n"
        "• Easiest: forward any message from your channel to me, or\n"
        f"• Add me as admin to the channel and post:  /link {code}\n"
        "I’ll bind that channel to your account."
    )

async def on_forward(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.forward_from_chat: return
    ch: Chat = msg.forward_from_chat
    if ch.type != Chat.CHANNEL: return
    save_channel(update.effective_user.id, str(ch.id), ch.title or "")
    await msg.reply_text(f"✅ Linked channel: {ch.title or ch.id}")

async def on_channel_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # expects '/link CODE' inside the channel
    post = update.channel_post
    if not post or not post.text: return
    parts = post.text.strip().split()
    if len(parts) == 2 and parts[0].lower() == "/link":
        code = parts[1]
        tg_id = pop_link_code(code)
        if tg_id:
            save_channel(tg_id, str(post.chat.id), post.chat.title or "")
            try:
                tg_send("sendMessage", chat_id=tg_id,
                        text=f"✅ Linked channel: {post.chat.title or post.chat.id}")
            except Exception:
                pass

async def picker_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id)
    if not u:
        await update.message.reply_text("Please /connect Google first.")
        return
    ch = get_channel(update.effective_user.id)
    if not ch:
        await update.message.reply_text("Please /setchannel first.")
        return

    await update.message.reply_text("Creating a Picker session...")
    try:
        access = get_access_token(u)
        sess = create_picker_session(access)
    except Exception as e:
        await update.message.reply_text(f"Failed to create session: {e}")
        return

    picker_uri = sess["pickerUri"]
    await update.message.reply_text(
        "Open this link on your phone, select items, then tap Done:",
    )
    await update.message.reply_text(picker_uri)

    # background worker
    def worker(tg_id:int, chat_id:str, session_id:str):
        try:
            # wait until user finished picking
            while True:
                st = session_ready(get_access_token(get_user(tg_id)), session_id)
                if st.get("mediaItemsSet"): break
                time.sleep(int((st.get("pollingConfig", {}) or {}).get("pollInterval", 3)))
            sent = 0
            for it in iter_picked(get_access_token(get_user(tg_id)), session_id):
                try:
                    content, name, mime = download_item(get_access_token(get_user(tg_id)), it)
                    send_media_auto(chat_id, name, content, mime)
                    sent += 1
                    if sent % 10 == 0:
                        tg_send("sendMessage", chat_id=tg_id, text=f"Progress: {sent} sent…")
                    time.sleep(0.5)
                except Exception as ex:
                    tg_send("sendMessage", chat_id=tg_id, text=f"Skipped one: {ex}")
            tg_send("sendMessage", chat_id=tg_id, text=f"✅ Done. Sent {sent} item(s).")
        except Exception as ex:
            tg_send("sendMessage", chat_id=tg_id, text=f"❌ Picker job failed: {ex}")

    threading.Thread(target=worker, args=(update.effective_user.id, ch["chat_id"], sess["id"]), daemon=True).start()

def main():
    init_db()
    # Start API (OAuth callback) in a thread
    threading.Thread(target=run_api, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("connect", connect_cmd))
    app.add_handler(CommandHandler("setchannel", setchannel_cmd))
    app.add_handler(CommandHandler("picker", picker_cmd))
    app.add_handler(MessageHandler(filters.FORWARDED & filters.ChatType.PRIVATE, on_forward))
    # NOTE: This filter works in practice for channel posts on PTB v20.
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, on_channel_post))
    app.run_polling()

if __name__ == "__main__":
    if not (BOT_TOKEN and CLIENT_ID and CLIENT_SECRET and PUBLIC_BASE):
        raise SystemExit("Set BOT_TOKEN, GOOGLE_CLIENT_ID/SECRET, PUBLIC_BASE_URL in .env")
    main()
