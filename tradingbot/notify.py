"""Telegram notifications. Plain HTTPS to the Bot API, no extra packages.

Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env (see scripts/telegram_setup.py).
If either is missing every call is a silent no-op, so the bot keeps working without Telegram.
"""
from __future__ import annotations
import json
import os
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

from .config import ROOT

load_dotenv(ROOT / ".env")


def configured() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN")) and bool(os.getenv("TELEGRAM_CHAT_ID"))


def send(text: str, silent: bool = False) -> bool:
    """Send a plain-text message (auto-split at 4000 chars). Returns True if delivered."""
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        return False
    ok = True
    for i in range(0, max(len(text), 1), 4000):
        body = json.dumps({"chat_id": chat, "text": text[i:i + 4000], "disable_notification": silent,
                           "disable_web_page_preview": True}).encode("utf-8")
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                ok = ok and r.status == 200
        except Exception as e:  # noqa: BLE001 - notifications must never crash the bot
            print(f"[notify] telegram send failed: {e}")
            ok = False
    return ok


def send_file(path: Path, caption: str = "") -> bool:
    """Send a small file (e.g. the weekly report) as a document."""
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat or not Path(path).exists():
        return False
    boundary = "----tradingbot"
    data = Path(path).read_bytes()
    parts = [f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat}\r\n",
             f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption[:1000]}\r\n",
             f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{Path(path).name}\"\r\nContent-Type: text/plain\r\n\r\n"]
    body = "".join(parts).encode("utf-8") + data + f"\r\n--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendDocument", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200
    except Exception as e:  # noqa: BLE001
        print(f"[notify] telegram file send failed: {e}")
        return False
