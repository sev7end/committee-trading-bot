"""One-time Telegram setup.

1. In Telegram, open @BotFather, send /newbot, follow the prompts, copy the token it gives you.
2. Paste the token into .env as   TELEGRAM_BOT_TOKEN=123456:ABC...
3. Open your new bot in Telegram and send it any message (e.g. "hi").
4. Run:  python scripts/telegram_setup.py
   It finds your chat id from that message, writes TELEGRAM_CHAT_ID into .env, and sends a test message.
"""
import json
import os
import re
import urllib.request

import _common  # noqa: F401
from dotenv import load_dotenv

from tradingbot.config import ROOT

ENV = ROOT / ".env"
load_dotenv(ENV)


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("TELEGRAM_BOT_TOKEN is empty in .env. Create a bot with @BotFather and paste its token first.")
        return
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20) as r:
        data = json.load(r)
    chats = {}
    for u in data.get("result", []):
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat", {})
        if chat.get("id"):
            chats[chat["id"]] = f"{chat.get('first_name', '')} {chat.get('last_name', '')} @{chat.get('username', '')}".strip()
    if not chats:
        print("No messages found. Open your bot in Telegram, send it 'hi', then run this again.")
        return
    chat_id = list(chats)[-1]
    print(f"found chat {chat_id} ({chats[chat_id]})")
    text = ENV.read_text(encoding="utf-8") if ENV.exists() else ""
    if re.search(r"^TELEGRAM_CHAT_ID=.*$", text, flags=re.M):
        text = re.sub(r"^TELEGRAM_CHAT_ID=.*$", f"TELEGRAM_CHAT_ID={chat_id}", text, flags=re.M)
    else:
        text += f"\nTELEGRAM_CHAT_ID={chat_id}\n"
    ENV.write_text(text, encoding="utf-8")
    os.environ["TELEGRAM_CHAT_ID"] = str(chat_id)
    from tradingbot import notify
    ok = notify.send("Committee bot connected. You will get a message after every trading run and every Saturday report.")
    print("test message sent" if ok else "test message FAILED")


if __name__ == "__main__":
    main()
