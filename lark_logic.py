import re
from typing import Any
from wiki_ai_logic import handle_wiki_ai
from p0_logic import start_p0, end_p0_session, P0_SESSIONS

# This remains your specific incident group
INCIDENT_GROUP_ID = "yourlarkgroupchat"

# P0 trigger regex
P0_REGEX = re.compile(r"\b(p0|priority\s*0)\b", re.IGNORECASE)
P0_END_REGEX = re.compile(r"\b(p0\s*end|end\s*p0|resolved|close\s*p0|p0\s*resolved)\b", re.IGNORECASE)
NEGATION_REGEX = re.compile(r"\bnot\b.*\bp0\b", re.IGNORECASE)


def process_message(
    incoming_text: str,
    chat_id: str,
    user_id: str,
    token: str,
    lark_client: Any,
    groq_key: str,
    **kwargs: Any,
) -> None:
    text_raw = (incoming_text or "").strip()
    if not text_raw:
        return

    # keep raw + lower
    text_lower = text_raw.lower()

    # Only special logic inside incident group
    if chat_id == INCIDENT_GROUP_ID:

        # 1) End / resolved commands
        if P0_END_REGEX.search(text_lower):
            end_p0_session(chat_id)
            # optional confirmation:
            # from p0_logic import _post_text
            # _post_text(chat_id, token, "✅ P0 session ended.")
            return

        # 2) Prevent "not p0" triggers
        if NEGATION_REGEX.search(text_lower):
            return

        # 3) Trigger P0
        if P0_REGEX.search(text_lower):
            # prevent re-trigger spam if already active
            if chat_id in P0_SESSIONS:
                # optional: ignore silently
                return

            print(f"DEBUG: P0 Trigger detected from user {user_id}")
            start_p0(chat_id, token)
            return

        # 4) Wiki AI fallback only if question
        if "?" in text_raw:
            handle_wiki_ai(text_raw, chat_id, token, groq_key)
        return

    # Outside incident group: Wiki AI
    handle_wiki_ai(text_raw, chat_id, token, groq_key)
