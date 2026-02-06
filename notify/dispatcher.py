import logging
from notify.telegram_bot import send_telegram

log = logging.getLogger("lark-ops-ai")

def notify_all(message: str) -> None:
    try:
        send_telegram(message)
    except Exception as e:
        log.error("notify_all error: %s", e)
