import os
import requests
import logging

log = logging.getLogger("lark-ops-ai")

REQ_TIMEOUT = 10

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()

def send_telegram(text: str) -> None:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log.warning("Telegram not configured: TG_BOT_TOKEN/TG_CHAT_ID missing.")
        return

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text, "disable_web_page_preview": True}

    try:
        r = requests.post(url, json=payload, timeout=REQ_TIMEOUT)
        if r.status_code != 200:
            log.error("Telegram send failed: %s - %s", r.status_code, r.text[:300])
        else:
            log.info("Telegram send OK.")
    except Exception as e:
        log.error("Telegram send exception: %s", e)
