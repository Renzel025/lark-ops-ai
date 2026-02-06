import os, json, base64, logging, hashlib
from typing import Any, Dict
from fastapi import FastAPI, Request, BackgroundTasks
from Crypto.Cipher import AES
import lark_oapi as lark

from lark_logic import process_message
from p0_logic import (
    get_tenant_token,
    P0_SESSIONS,
    start_p0,
    handle_p0_submit,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("lark-ops-ai")

app = FastAPI()

LARK_APP_ID = os.getenv("LARK_APP_ID", "")
LARK_APP_SECRET = os.getenv("LARK_APP_SECRET", "")
LARK_ENCRYPT_KEY = os.getenv("LARK_ENCRYPT_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

# Your client domain kept as-is (works for you)
lark_client = (
    lark.Client.builder()
    .app_id(LARK_APP_ID)
    .app_secret(LARK_APP_SECRET)
    .domain("https://open-sg.larksuite.com")
    .build()
)


def decrypt_lark_event(encrypted_b64: str, encrypt_key: str) -> Dict[str, Any]:
    """Decrypts Lark encrypted payloads."""
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    raw = base64.b64decode(encrypted_b64)
    cipher = AES.new(key, AES.MODE_CBC, iv=raw[:16])
    pt = cipher.decrypt(raw[16:])
    return json.loads(pt[:-pt[-1]].decode("utf-8"))


@app.post("/lark/webhook")
async def lark_webhook(req: Request, background: BackgroundTasks):
    body = await req.json()

    if "encrypt" in body:
        body = decrypt_lark_event(body["encrypt"], LARK_ENCRYPT_KEY)

    if body.get("type") == "url_verification":
        return {"challenge": body.get("challenge")}

    background.add_task(_process_lark_payload, body)
    return {"code": 0, "msg": "success"}


def _process_lark_payload(payload: Dict[str, Any]) -> None:
    try:
        evt = payload.get("event", {}) or {}
        header = payload.get("header", {}) or {}

        token = get_tenant_token(LARK_APP_ID, LARK_APP_SECRET)
        if not token:
            log.error("No tenant token; cannot process.")
            return

        event_type = header.get("event_type")

        # 1) Card form submission
        if event_type == "card.action.trigger":
            action = evt.get("action", {}) or {}
            value = (action.get("value") or {})
            if value.get("action") == "p0_submit":
                handle_p0_submit(evt, token)
                return

        # 2) Normal chat message
        msg = evt.get("message", {}) or {}
        if msg.get("content"):
            text = json.loads(msg["content"]).get("text", "")
            chat_id = msg.get("chat_id")
            user_id = (evt.get("sender", {}) or {}).get("sender_id", {}).get("open_id")

            # Your lark_logic decides when to trigger P0
            # If you want: call start_p0(chat_id, token) directly when detected.
            process_message(text, chat_id, user_id, token, lark_client, GROQ_API_KEY)

    except Exception as e:
        log.error(f"Background Process Error: {e}", exc_info=True)
