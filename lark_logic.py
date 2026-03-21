import os
import re
import logging
from typing import Any, List

from wiki_ai_logic import handle_wiki_ai
from p0_logic.config import get_incident_group_chat_ids, get_p0_trigger_ignore_open_ids
from p0_logic import (
    start_p0,
    end_p0_session,
    cancel_p0_session,
    P0_SESSIONS,
    handle_dm_generate_overview,
    get_p1_prompt_pending,
    set_p1_prompt_pending,
    pop_p1_prompt_pending,
    request_p1_meeting_confirmation,
)

log = logging.getLogger("lark-ops-ai")

WIKI_GROUP_CHAT_ID = os.getenv("WIKI_GROUP_CHAT_ID", "").strip()

# Keyword anywhere in the sentence (e.g. "this is p0", "we tag this as a P0") — case-insensitive.
P0_KEYWORD_RE = re.compile(r"\bp0\b|\bpriority\s*0\b", re.IGNORECASE)
P1_KEYWORD_RE = re.compile(r"\bp1\b|\bpriority\s*1\b", re.IGNORECASE)


def _is_pasted_meeting_invite_footer(text: str) -> bool:
    """
    Ignore copy-paste of the red meeting-card footer (starts with ``P0 declared -`` / ``P1 declared -``)
    so it does not start another VC.
    """
    t = (text or "").strip().lower()
    return t.startswith("p0 declared - created a meeting") or t.startswith("p1 declared - created a meeting")

# End commands
P0_END_REGEX = re.compile(r"\b(p0\s*end|end\s*p0|close\s*p0|p0\s*resolved)\b", re.IGNORECASE)
P1_END_REGEX = re.compile(r"\b(p1\s*end|end\s*p1|close\s*p1|p1\s*resolved)\b", re.IGNORECASE)

# Cancel commands: optional free-text reason after the phrase (e.g. "cancel meeting no need yet")
# Order: longer prefixes first so "cancel meeting" wins over "cancel".
CANCEL_WITH_OPTIONAL_REASON_RE = re.compile(
    r"^\s*(cancel\s+meeting|cancel\s+p0|cancel\s+p1|cancel)\s*(.*)$",
    re.IGNORECASE,
)


def _clean_mention_names(raw_mentions: Any) -> List[str]:
    out: List[str] = []
    if not raw_mentions:
        return out

    if isinstance(raw_mentions, list):
        for m in raw_mentions:
            if isinstance(m, str):
                name = m.strip()
                if name:
                    out.append(name)
                continue

            if isinstance(m, dict):
                name = (
                    m.get("name")
                    or m.get("title")
                    or m.get("display_name")
                    or m.get("full_name")
                    or ""
                )
                name = str(name).strip()
                if name:
                    out.append(name)

    seen = set()
    deduped: List[str] = []
    for x in out:
        k = x.lower()
        if k in seen:
            continue
        seen.add(k)
        deduped.append(x)

    return deduped


def _get_active_session_chat_id() -> str:
    if not P0_SESSIONS:
        return ""
    return list(P0_SESSIONS.keys())[-1]


def process_message(
    incoming_text: str,
    chat_id: str,
    user_id: str,
    token: str,
    lark_client: Any,
    groq_key: str,
    source_chat_name: str = "",
    **kwargs: Any,
) -> None:
    text_raw = (incoming_text or "").strip()
    text_lower = text_raw.lower() if text_raw else ""

    message_type = str(kwargs.get("message_type") or "").strip().lower()
    message_id = str(kwargs.get("message_id") or "").strip()
    image_key = str(kwargs.get("image_key") or kwargs.get("file_key") or "").strip()
    mention_names = _clean_mention_names(kwargs.get("mention_names") or kwargs.get("mentions"))
    tenant_token = str(kwargs.get("tenant_token") or token or "").strip()
    chat_type = str(kwargs.get("chat_type") or "").strip().lower()

    active_chat_id = _get_active_session_chat_id()
    has_active_session = bool(active_chat_id)

    log.info(
        "process_message route: chat_id=%s chat_type=%s user_id=%s message_type=%s text=%r mentions=%s active_chat_id=%s has_active_session=%s",
        chat_id,
        chat_type,
        user_id,
        message_type,
        text_raw[:200] if text_raw else "",
        mention_names,
        active_chat_id,
        has_active_session,
    )

    # ---------------------------------------------------------
    # INCIDENT GROUP
    # ---------------------------------------------------------
    if chat_id in get_incident_group_chat_ids():
        if not text_raw:
            return

        # Cancel command (optional reason after the keyword phrase)
        cancel_m = CANCEL_WITH_OPTIONAL_REASON_RE.match(text_raw)
        if cancel_m:
            tail = (cancel_m.group(2) or "").strip()
            cancel_reason = tail if tail else "Unspecified"
            if chat_id in P0_SESSIONS:
                sess = P0_SESSIONS.get(chat_id) or {}
                priority = str(sess.get("priority") or "P0").strip().upper()
                log.info(
                    "Incident group: cancel requested chat_id=%s priority=%s reason=%r",
                    chat_id,
                    priority,
                    cancel_reason,
                )
                cancel_p0_session(chat_id, token, reason=cancel_reason)
            else:
                log.info("Incident group: cancel requested but no active session chat_id=%s", chat_id)
            return

        # End command
        if P0_END_REGEX.search(text_lower) or P1_END_REGEX.search(text_lower):
            if chat_id in P0_SESSIONS:
                log.info("Incident group: ending active session chat_id=%s", chat_id)
                end_p0_session(chat_id, token)
            else:
                log.info("Incident group: end requested but no active session chat_id=%s", chat_id)
            return

        # Trigger P0 if ``p0`` / ``priority 0`` appears anywhere (unless pasted invite footer).
        if (not _is_pasted_meeting_invite_footer(text_raw)) and P0_KEYWORD_RE.search(text_raw):
            if (user_id or "").strip() in get_p0_trigger_ignore_open_ids():
                log.info("Incident group: P0 trigger ignored (P0_TRIGGER_IGNORE_OPEN_IDS) user_id=%s", user_id)
                return
            if chat_id in P0_SESSIONS:
                log.info("Incident group: session already active chat_id=%s", chat_id)
                return

            log.info("Incident group: starting P0 session chat_id=%s user_id=%s text=%r", chat_id, user_id, text_raw[:200])
            start_p0(chat_id, token, user_id, priority="P0", source_chat_name=source_chat_name)
            return

        # Trigger P1 if ``p1`` / ``priority 1`` appears anywhere (unless pasted invite footer).
        if (not _is_pasted_meeting_invite_footer(text_raw)) and P1_KEYWORD_RE.search(text_raw):
            if (user_id or "").strip() in get_p0_trigger_ignore_open_ids():
                log.info("Incident group: P1 trigger ignored (P0_TRIGGER_IGNORE_OPEN_IDS) user_id=%s", user_id)
                return
            if chat_id in P0_SESSIONS:
                log.info("Incident group: session already active chat_id=%s", chat_id)
                return
            if get_p1_prompt_pending(chat_id):
                log.info("Incident group: P1 confirmation already pending chat_id=%s", chat_id)
                return

            log.info("Incident group: P1 keyword — posting meeting confirmation card chat_id=%s user_id=%s", chat_id, user_id)
            set_p1_prompt_pending(chat_id, user_id)
            if not request_p1_meeting_confirmation(chat_id, token, user_id):
                pop_p1_prompt_pending(chat_id)
                log.error("Incident group: failed to post P1 confirmation card chat_id=%s", chat_id)
            return

        # Ignore non P0/P1 chatter in the incident group to avoid noisy auto replies.
        log.info("Incident group: ignoring non P0/P1 message")
        return

    # ---------------------------------------------------------
    # WIKI GROUP
    # ---------------------------------------------------------
    if WIKI_GROUP_CHAT_ID and chat_id == WIKI_GROUP_CHAT_ID:
        if not text_raw:
            return

        log.info("Wiki group: routing to wiki")
        handle_wiki_ai(text_raw, chat_id, token, groq_key)
        return

    # ---------------------------------------------------------
    # DM / OTHER CHAT WHILE SESSION IS ACTIVE
    # ---------------------------------------------------------
    if has_active_session:
        if message_type == "image" and image_key:
            log.info(
                "Active session: handling image source_chat_id=%s active_session_chat_id=%s user_id=%s image_key=%s",
                chat_id,
                active_chat_id,
                user_id,
                image_key,
            )
            handle_dm_generate_overview(
                sender_open_id=user_id,
                tenant_token=tenant_token,
                image_key=image_key,
                mention_names=mention_names,
                message_id=message_id,
            )
            return

        if text_raw:
            log.info(
                "Active session: handling text source_chat_id=%s active_session_chat_id=%s user_id=%s text_head=%r",
                chat_id,
                active_chat_id,
                user_id,
                text_raw[:120],
            )
            handle_dm_generate_overview(
                sender_open_id=user_id,
                tenant_token=tenant_token,
                text=text_raw,
                mention_names=mention_names,
                message_id=message_id,
            )
            return

    log.info("Ignored message from non-allowed chat_id=%s", chat_id)
    return
