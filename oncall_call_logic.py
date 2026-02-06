# oncall_call_logic.py
import os
import json
import requests
from typing import List
from twilio.rest import Client

# Twilio config
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.getenv("TWILIO_FROM_NUMBER", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")

# Lark send message endpoint
LARK_SEND_URL = "https://open-sg.larksuite.com/open-apis/im/v1/messages?receive_id_type=chat_id"


def send_lark_message(chat_id: str, token: str, text: str) -> None:
    try:
        requests.post(
            LARK_SEND_URL,
            headers={"Authorization": f"Bearer {token}"},
            json={"receive_id": chat_id, "msg_type": "text", "content": json.dumps({"text": text})},
            timeout=10,
        )
    except Exception as e:
        print(f"❌ Lark send error: {e}", flush=True)


def load_oncall_numbers() -> List[str]:
    """
    Reads numbers from .env:
      ONCALL_NUMBERS=+63929xxxxxxx,+6012xxxxxxx
    """
    raw = os.getenv("ONCALL_NUMBERS", "").strip()
    if not raw:
        return []
    nums = [n.strip() for n in raw.split(",") if n.strip()]
    # Keep only E.164-looking values
    return [n for n in nums if n.startswith("+") and len(n) >= 8]


def get_verified_numbers_from_twilio(client: Client) -> List[str]:
    """
    Trial accounts can only call verified numbers.
    This fetches your Verified Caller IDs list from Twilio.
    """
    verified = []
    try:
        for item in client.outgoing_caller_ids.list():
            # item.phone_number is in E.164 (+63...)
            if getattr(item, "phone_number", None):
                verified.append(item.phone_number)
    except Exception as e:
        print(f"⚠️ Could not fetch verified caller IDs: {e}", flush=True)
    return verified


def trigger_p0_calls(incident_chat_id: str, notify_chat_id: str, token: str) -> None:
    """
    Called when P0 is declared in Group A.
    We notify Group B and call the on-call phone numbers (PSTN) via Twilio.
    """

    # 0) Basic config checks
    if not all([TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM, PUBLIC_BASE_URL]):
        send_lark_message(
            notify_chat_id,
            token,
            "❌ Calling not started: missing Twilio config (SID/TOKEN/FROM/PUBLIC_BASE_URL).",
        )
        return

    oncall_numbers = load_oncall_numbers()
    if not oncall_numbers:
        send_lark_message(
            notify_chat_id,
            token,
            "❌ Calling not started: ONCALL_NUMBERS is empty. Add numbers to .env (comma-separated).",
        )
        return

    # 1) Notify Group B
    send_lark_message(
        notify_chat_id,
        token,
        f"🚨 P0 declared in Incident GC ({incident_chat_id}). Starting phone call alerts now…",
    )

    # 2) Twilio client
    client = Client(TWILIO_SID, TWILIO_TOKEN)

    # 3) Filter by verified numbers (trial-safe)
    verified = set(get_verified_numbers_from_twilio(client))
    to_call = [n for n in oncall_numbers if n in verified]

    skipped = [n for n in oncall_numbers if n not in verified]
    if skipped:
        send_lark_message(
            notify_chat_id,
            token,
            "⚠️ Skipping unverified numbers (Twilio Trial rule):\n" + "\n".join(skipped),
        )

    if not to_call:
        send_lark_message(
            notify_chat_id,
            token,
            "❌ No calls placed: none of the ONCALL_NUMBERS are verified in Twilio.\n"
            "Go to Twilio → Phone Numbers → Verified Caller IDs and verify them.",
        )
        return

    # 4) Build voice URL (Twilio will fetch this)
    voice_url = (
        f"{PUBLIC_BASE_URL}/twilio/voice"
        f"?incident_chat_id={incident_chat_id}"
        f"&notify_chat_id={notify_chat_id}"
    )

    # 5) Place calls
    ok = 0
    for number in to_call:
        try:
            client.calls.create(
                to=number,
                from_=TWILIO_FROM,
                url=voice_url,
            )
            ok += 1
            print(f"📞 Calling {number}", flush=True)
        except Exception as e:
            print(f"❌ Failed to call {number}: {e}", flush=True)

    send_lark_message(
        notify_chat_id,
        token,
        f"📞 Call attempts finished. Placed: {ok}/{len(to_call)}",
    )
