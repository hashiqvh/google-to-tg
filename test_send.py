# save as test_send.py in same folder (uses your env vars)
import os, requests
BOT=os.getenv("TG_BOT_TOKEN"); CHAT=os.getenv("TG_CHANNEL_ID")
assert BOT and CHAT, "Set TG_BOT_TOKEN and TG_CHANNEL_ID"
r = requests.post(f"https://api.telegram.org/bot{BOT}/sendMessage",
                  data={"chat_id": CHAT, "text": "Bot is live ✅"})
print(r.status_code, r.text)
