import os
import re
import time
import secrets
import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

from wiki_ai_logic import handle_wiki_ai
from p0_logic.config import (
    get_incident_group_chat_ids,
    get_p0_keyword_groq_gate,
    get_p0_keyword_supplemental_skip_regex,
    get_p0_keyword_use_builtin_context_filters,
    get_p0_keyword_ai_triage,
    resolve_priority_keyword_ai_provider,
    get_session_meeting_card_post_chat_id,
    get_p0_trigger_ignore_open_ids,
    get_p0_redeclare_supersedes_active,
    get_p0_multi_meeting_per_group,
    get_p0_issue_watch_enabled,
    get_p0_keyword_confirm_dm_enabled,
    get_dm_instruction_open_ids,
    p0_group_typed_meeting_commands_enabled,
    HELP_RE,
    RING_CMD_RE,
)
from p0_logic.groq_client import classify_priority_keyword, groq_p0_keyword_declares_new_bridge
from features.session.session import handle_p1_meeting_confirm_no, handle_p1_meeting_confirm_yes
from p0_logic.cards import build_help_commands_card, build_p0_keyword_confirm_dm_card
from p0_logic.lark_client import post_card_to_chat, post_text_to_chat, post_card_to_open_id
from features.screenshot.graph_screenshot_request import (
    try_handle_graph_screenshot_request,
    _strip_leading_mentions,
    _mentions_our_bot,
)
from features.issue_watch.issue_watch import try_handle_issue_watch
from p0_logic import (
    start_p0,
    cancel_p0_session,
    end_p0_session,
    clear_p0_cooldown,
    P0_SESSIONS,
    chat_has_active_session,
    handle_dm_generate_overview,
    get_p1_prompt_pending,
    set_p1_prompt_pending,
    pop_p1_prompt_pending,
    request_p1_meeting_confirmation,
    resolve_source_incident_chat_for_session_command,
)

log = logging.getLogger("lark-ops-ai")

WIKI_GROUP_CHAT_ID = os.getenv("WIKI_GROUP_CHAT_ID", "").strip()

# Lark may deliver ``im.message.receive_v1`` twice for one user send (e.g. main feed + thread copy)
# with different ``message_id`` but the same ``create_time`` and text — dedupe keyword VC triggers.
_KEYWORD_TRIGGER_DEDUPE: Dict[str, float] = {}
_KEYWORD_TRIGGER_DEDUPE_LOCK = threading.Lock()
_KEYWORD_TRIGGER_DEDUPE_TTL_SEC = 25.0


def _keyword_trigger_dedupe_key(
    chat_id: str,
    user_open_id: str,
    message_id: str,
    message_create_time: str,
    text_raw: str,
) -> str:
    norm = " ".join((text_raw or "").split())
    ct = (message_create_time or "").strip()
    if ct:
        return f"kw:{chat_id}:{user_open_id}:{ct}:{norm}"
    mid = (message_id or "").strip()
    return f"kw:{chat_id}:{user_open_id}:mid:{mid}:{norm}"


def _try_consume_keyword_trigger_dedupe(key: str) -> bool:
    """
    True = first delivery for this key within TTL (proceed with P0/P1 keyword action).
    False = duplicate webhook (skip — avoids double ``start_p0`` / double P1 card).
    """
    now = time.monotonic()
    with _KEYWORD_TRIGGER_DEDUPE_LOCK:
        for k, t in list(_KEYWORD_TRIGGER_DEDUPE.items()):
            if now - t > _KEYWORD_TRIGGER_DEDUPE_TTL_SEC:
                del _KEYWORD_TRIGGER_DEDUPE[k]
        if key in _KEYWORD_TRIGGER_DEDUPE:
            return False
        _KEYWORD_TRIGGER_DEDUPE[key] = now
        return True


# --- P0 keyword confirm DM (P0_KEYWORD_CONFIRM_DM_ENABLED) ---------------------------------------
# When a group ``p0`` mention is NOT auto-declared (AI/Groq says it is not a fresh declaration),
# instead of silently dropping it we DM the duty a Yes/No card. The pending entry is keyed by a
# generated ``nonce`` and looked up / consumed when the duty clicks Yes or No (in handlers.py).
_P0_KEYWORD_CONFIRM_LOCK = threading.RLock()
# nonce -> { source_incident_chat_id, trigger_open_id, trigger_lark_user_id, source_chat_name,
#            phrase, created_at (epoch float) }
_P0_KEYWORD_CONFIRM_PENDING: Dict[str, Dict[str, Any]] = {}
_P0_KEYWORD_CONFIRM_TTL_SEC = 3600.0  # prune entries older than 1h
# Dedupe the DM itself: at most one confirm DM per source ``message_id`` (Lark redelivers events).
_P0_KEYWORD_CONFIRM_DM_DEDUPE: Dict[str, float] = {}
_P0_KEYWORD_CONFIRM_DM_DEDUPE_TTL_SEC = 600.0


def _p0_keyword_confirm_prune_locked() -> None:
    now = time.time()
    for k, v in list(_P0_KEYWORD_CONFIRM_PENDING.items()):
        if now - float(v.get("created_at") or 0) > _P0_KEYWORD_CONFIRM_TTL_SEC:
            _P0_KEYWORD_CONFIRM_PENDING.pop(k, None)


def _try_consume_p0_keyword_confirm_dm_dedupe(message_id: str) -> bool:
    """True = first DM for this source message_id; False = duplicate webhook delivery (skip DM)."""
    mid = (message_id or "").strip()
    if not mid:
        return True
    now = time.monotonic()
    with _P0_KEYWORD_CONFIRM_LOCK:
        for k, t in list(_P0_KEYWORD_CONFIRM_DM_DEDUPE.items()):
            if now - t > _P0_KEYWORD_CONFIRM_DM_DEDUPE_TTL_SEC:
                del _P0_KEYWORD_CONFIRM_DM_DEDUPE[k]
        if mid in _P0_KEYWORD_CONFIRM_DM_DEDUPE:
            return False
        _P0_KEYWORD_CONFIRM_DM_DEDUPE[mid] = now
        return True


def p0_keyword_confirm_lookup(nonce: str) -> Optional[Dict[str, Any]]:
    """Return a copy of the pending entry (None if missing/expired). Does NOT consume it."""
    key = (nonce or "").strip()
    if not key:
        return None
    with _P0_KEYWORD_CONFIRM_LOCK:
        _p0_keyword_confirm_prune_locked()
        v = _P0_KEYWORD_CONFIRM_PENDING.get(key)
        return dict(v) if v else None


def p0_keyword_confirm_consume(nonce: str) -> Optional[Dict[str, Any]]:
    """Atomically pop the pending entry (idempotent: a second Yes/No click returns None)."""
    key = (nonce or "").strip()
    if not key:
        return None
    with _P0_KEYWORD_CONFIRM_LOCK:
        _p0_keyword_confirm_prune_locked()
        v = _P0_KEYWORD_CONFIRM_PENDING.pop(key, None)
        return dict(v) if v else None


def _send_p0_keyword_confirm_dm(
    *,
    source_incident_chat_id: str,
    trigger_open_id: str,
    trigger_lark_user_id: str,
    source_chat_name: str,
    phrase: str,
    token: str,
    message_id: str,
) -> None:
    """
    DM the duty a Yes/No "create a P0 meeting?" card for a ``p0`` mention that was NOT auto-declared.
    Stores the pending entry keyed by a fresh nonce BEFORE sending; deduped per source ``message_id``.
    """
    if not _try_consume_p0_keyword_confirm_dm_dedupe(message_id):
        log.info(
            "Incident group: P0 keyword confirm DM skipped (duplicate Lark delivery) chat_id=%s message_id=%s",
            source_incident_chat_id,
            message_id,
        )
        return
    recipients = [x for x in (get_dm_instruction_open_ids() or []) if x]
    if not recipients:
        log.warning(
            "Incident group: P0 keyword confirm DM has no recipients (P0_DM_INSTRUCTION_OPEN_IDS unset) chat_id=%s",
            source_incident_chat_id,
        )
        return
    nonce = secrets.token_hex(16)
    entry: Dict[str, Any] = {
        "source_incident_chat_id": (source_incident_chat_id or "").strip(),
        "trigger_open_id": (trigger_open_id or "").strip(),
        "trigger_lark_user_id": (trigger_lark_user_id or "").strip(),
        "source_chat_name": (source_chat_name or "").strip(),
        "phrase": (phrase or "").strip()[:300],
        "created_at": time.time(),
    }
    with _P0_KEYWORD_CONFIRM_LOCK:
        _p0_keyword_confirm_prune_locked()
        _P0_KEYWORD_CONFIRM_PENDING[nonce] = entry
    card = build_p0_keyword_confirm_dm_card(nonce, entry["phrase"], entry["source_chat_name"])
    tails: List[str] = []
    for oid in recipients:
        st, body, _mid = post_card_to_open_id(oid, token, card)
        tails.append(oid[-8:] if len(oid) > 8 else oid)
        if st != 200:
            log.warning(
                "Incident group: P0 keyword confirm DM post HTTP=%s oid_tail=%s body=%r",
                st,
                oid[-8:] if len(oid) > 8 else oid,
                (body or "")[:200],
            )
    log.info(
        "Incident group: P0 keyword confirm DM sent recipients=%s nonce=%s chat_id=%s",
        tails,
        nonce,
        source_incident_chat_id,
    )


def _maybe_p0_keyword_confirm_dm(
    *,
    chat_id: str,
    token: str,
    user_id: str,
    sender_lark_user_id: str,
    source_chat_name: str,
    text_raw: str,
    message_id: str,
) -> None:
    """
    Gate + guards for the not-auto-declared P0 mention DM. No-op unless the toggle is ON;
    skipped when a meeting is already active in the source group.
    """
    if not get_p0_keyword_confirm_dm_enabled():
        return
    # Rule: anything that is NOT a firm auto-declaration must still ASK the duty — questions,
    # hedges ("possible p0"), handoffs, and past/informational references ("the p0 yesterday",
    # "may we ask ... p0") all get the Yes/No DM. Only an EXPLICIT negation ("this is not p0")
    # stays silent, since asking "create a meeting?" right after "this is NOT p0" is nonsensical.
    if _is_explicit_p0_negation(text_raw):
        log.info(
            "Incident group: P0 keyword confirm DM skipped (explicit negation) chat_id=%s",
            chat_id,
        )
        return
    if chat_has_active_session(chat_id):
        log.info(
            "Incident group: P0 keyword confirm DM skipped (session already active) chat_id=%s",
            chat_id,
        )
        return
    _send_p0_keyword_confirm_dm(
        source_incident_chat_id=chat_id,
        trigger_open_id=user_id,
        trigger_lark_user_id=sender_lark_user_id,
        source_chat_name=source_chat_name,
        phrase=text_raw,
        token=token,
        message_id=message_id,
    )


# Keyword anywhere in the sentence (e.g. "this is p0", "we tag this as a P0") — case-insensitive.
# Questions ("is this p0?", "can this be a p1?") are ignored via _is_question_about_priority().
P0_KEYWORD_RE = re.compile(r"\bp0\b|\bpriority\s*0\b", re.IGNORECASE)
P1_KEYWORD_RE = re.compile(r"\bp1\b|\bpriority\s*1\b", re.IGNORECASE)

# Shared subject: "it", "this", "this one", "this issue", "that outage", …
_P0_SUBJECT = r"(?:it|(?:this|that)(?:\s+(?:one|issue|incident|outage|problem|ticket|case))?)"
_PRIO01 = r"(?:p0|p1|priority\s*0|priority\s*1)"
_P0_ONLY = r"(?:p0|priority\s*0)"

# Do not start VC when the user is *asking* about P0/P1 (vs declaring). See _is_question_about_priority().
_QUESTION_PRIORITY_PHRASE_RE = re.compile(
    rf"(?is)(?:"
    rf"is\s+this\s+(?:an?\s+)?{_PRIO01}\b|"
    rf"is\s+that\s+(?:an?\s+)?{_PRIO01}\b|"
    rf"is\s+it\s+(?:an?\s+)?{_PRIO01}\b|"
    rf"are\s+we\s+(?:in\s+)?(?:a\s+)?{_PRIO01}\b|"
    rf"is\s+this\s+possible\s+as\s+(?:a\s+)?{_PRIO01}\b|"
    rf"is\s+that\s+possible\s+as\s+(?:a\s+)?{_PRIO01}\b|"
    rf"can\s+we\s+refer\s+(?:{_P0_SUBJECT}\s+)?as\s+(?:a\s+)?{_PRIO01}\b|"
    rf"could\s+we\s+refer\s+(?:{_P0_SUBJECT}\s+)?as\s+(?:a\s+)?{_PRIO01}\b|"
    rf"can\s+we\s+tag\s+(?:{_P0_SUBJECT}\s+)?as\s+(?:a\s+)?{_PRIO01}\b|"
    rf"could\s+we\s+tag\s+(?:{_P0_SUBJECT}\s+)?as\s+(?:a\s+)?{_PRIO01}\b|"
    rf"shall\s+we\s+tag\s+{_P0_SUBJECT}\s+as\s+(?:a\s+)?{_PRIO01}\b|"
    rf"should\s+we\s+declare\s+{_P0_SUBJECT}\s+as\s+(?:a\s+)?{_PRIO01}\b|"
    rf"should\s+i\s+declare\s+{_P0_SUBJECT}\s+as\s+(?:a\s+)?{_PRIO01}\b|"
    rf"can\s+we\s+declare\s+{_P0_SUBJECT}\s+as\s+(?:a\s+)?{_PRIO01}\b|"
    rf"could\s+we\s+declare\s+{_P0_SUBJECT}\s+as\s+(?:a\s+)?{_PRIO01}\b|"
    rf"can\s+we\s+consider\s+{_P0_SUBJECT}\s+as\s+(?:a\s+)?{_PRIO01}\b|"
    rf"could\s+we\s+consider\s+{_P0_SUBJECT}\s+as\s+(?:a\s+)?{_PRIO01}\b|"
    rf"shall\s+we\s+consider\s+{_P0_SUBJECT}\s+as\s+(?:a\s+)?{_PRIO01}\b|"
    rf"can\s+this\s+be\s+(?:an?\s+)?{_PRIO01}\b|"
    rf"could\s+this\s+be\s+(?:an?\s+)?{_PRIO01}\b|"
    rf"should\s+this\s+be\s+(?:an?\s+)?{_PRIO01}\b|"
    rf"would\s+this\s+be\s+(?:an?\s+)?{_PRIO01}\b|"
    rf"will\s+this\s+be\s+(?:an?\s+)?{_PRIO01}\b|"
    rf"does\s+this\s+(?:count|qualify)\s+(?:as\s+)?(?:a\s+)?{_PRIO01}\b|"
    rf"what\s+is\s+(?:a\s+)?{_PRIO01}\b|"
    rf"how\s+(?:do|can|to)\s+(?:i|we)\s+(?:know|tell|declare)\s+.*\b(?:p0|p1|priority\s*[01])\b|"
    rf"any(?:thing|one)\s+.*\b(?:p0|p1|priority\s*[01])\b"
    rf")"
)

# Broken-English asks: "is this issue is p0" (extra words between "is … is p0") don't match phrases above.
_BROKEN_ENGLISH_DOUBLE_IS_PRIORITY_RE = re.compile(
    r"(?is)\bis\s+.+?\bis\s+(?:an?\s+)?(?:p0|p1|priority\s*0|priority\s*1)\b"
)

# Embedded if/whether clause: "please confirm if issue is p0", "need to check if this is p0".
_IF_OR_WHETHER_PRIORITY_CLAUSE_RE = re.compile(
    r"(?is)\b(?:if|whether)\s+.{1,220}?\bis\s+(?:an?\s+)?(?:p0|p1|priority\s*0|priority\s*1)\b"
)

# Explicit **declaration** ("this is p0", "it's a p1", "declaring p0") — a real statement, not a
# question — so a stray '?' from an *unrelated* sentence does not suppress a genuine P0 (e.g.
# "This is P0. @cs is there players reaching out?"). The (?!\s*\?) lookahead keeps a directly
# questioned "this is p0?" out (that stays a question). "is this p0" won't match (needs "this is").
_EXPLICIT_PRIORITY_DECLARATION_RE = re.compile(
    rf"(?is)"
    rf"\b(?:this|that)\s+is\s+(?:now\s+|already\s+|indeed\s+|a\s+|an\s+)*{_PRIO01}\b(?!\s*\?)"
    rf"|\bit'?s\s+(?:now\s+|already\s+|a\s+|an\s+)*{_PRIO01}\b(?!\s*\?)"
    rf"|\b(?:declaring|declare|raising|raise)\s+(?:this\s+)?(?:as\s+)?(?:a\s+)?{_PRIO01}\b(?!\s*\?)"
    rf"|\b{_PRIO01}\s+(?:confirmed|declared)\b(?!\s*\?)"
)


def _is_question_about_priority(text: str) -> bool:
    """
    True if the message looks like a question *about* P0/P1 rather than a declaration.
    Declarations like "this is p0" (statement) still trigger; "is this p0?" does not.
    A stray '?' from an unrelated sentence no longer suppresses an explicit "this is p0" declaration.
    """
    t = (text or "").strip()
    if not t:
        return False
    if not (P0_KEYWORD_RE.search(t) or P1_KEYWORD_RE.search(t)):
        return False
    # Explicit question forms always win (asking, not declaring) — checked before the declaration
    # override and before the blanket '?' so a real "is this p0?" / "if this is p0" stays a question.
    if _is_p0_thread_confirm_question(t):
        return True
    if _QUESTION_PRIORITY_PHRASE_RE.search(t):
        return True
    if _BROKEN_ENGLISH_DOUBLE_IS_PRIORITY_RE.search(t):
        return True
    if _IF_OR_WHETHER_PRIORITY_CLAUSE_RE.search(t):
        return True
    # A clear declaration ("this is p0") is NOT a question even if a stray '?' appears elsewhere.
    if _EXPLICIT_PRIORITY_DECLARATION_RE.search(t):
        return False
    # Bare '?' with a priority keyword and none of the above → treat as a question.
    if "?" in t:
        return True
    return False


# Polite asks often omit ``?`` on Lark (e.g. "may we know what are the findings of p0 last tuesday").
_P0_POLITE_INFO_ASK_RE = re.compile(
    r"(?is)\b(?:may|could)\s+we\s+(?:know|ask|see|confirm|clarify|understand)(?:\s+more)?\b"
    r"|\bcan\s+we\s+(?:know|ask|see|confirm|clarify|understand)(?:\s+more)?\b"
)

# ``p0`` / ``priority 0`` with a **past** calendar anchor (RCA / "last week's bridge"), not a fresh declaration.
_P0_RELATIVE_DAY = (
    r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)\b"
)
_P0_WITH_PAST_TIME_ANCHOR_RE = re.compile(
    r"(?is)(?:\b(?:p0|priority\s*0)\b.{0,120}?\b(?:last|past)\s+(?:"
    + _P0_RELATIVE_DAY
    + r"|week|month)\b"
    r"|\b(?:last|past)\s+(?:"
    + _P0_RELATIVE_DAY
    + r"|week|month)\b.{0,120}?\b(?:p0|priority\s*0)\b"
    r"|\b(?:p0|priority\s*0)\b.{0,120}?\byesterday\b"
    r"|\byesterday\b.{0,120}?\b(?:p0|priority\s*0)\b)"
)


def _is_p0_informational_ask_or_past_reference(text: str) -> bool:
    """
    True when ``p0`` / ``priority 0`` appears in an **informational** question (no trailing ``?``)
    or next to a **past** date/week/month anchor — not a request to open a new bridge.

    Skipped when ``_P0_MEETING_OR_DECLARE_HINT_RE`` matches (escalate / declare / meeting intent).
    """
    t = (text or "").strip()
    if not t or not P0_KEYWORD_RE.search(t):
        return False
    if _P0_MEETING_OR_DECLARE_HINT_RE.search(t):
        return False
    if _P0_POLITE_INFO_ASK_RE.search(t):
        return True
    if _P0_WITH_PAST_TIME_ANCHOR_RE.search(t):
        return True
    return False


def _is_pasted_meeting_invite_footer(text: str) -> bool:
    """
    Ignore copy-paste of the red meeting-card footer (starts with ``P0 declared -`` / ``P1 declared -``)
    so it does not start another VC.
    """
    t = (text or "").strip().lower()
    return t.startswith("p0 declared - created a meeting") or t.startswith("p1 declared - created a meeting")


# Lark mobile/desktop composer may append ``Message <chat display name>`` to im.message text.
# e.g. deposit paste + ``Message p0 detection dev`` falsely matches ``\\bp0\\b`` keyword trigger.
# Footer may be on its own line OR same line as the last account id (no leading newline).
_LARK_COMPOSER_MESSAGE_FOOTER_RE = re.compile(r"(?is)\s*Message\s+.+?\s*$")


def _strip_lark_composer_message_footer(text: str, *, chat_label: str = "") -> str:
    """Remove trailing ``Message <group name>`` Lark UI suffix before keyword / triage."""
    t = (text or "").strip()
    label = (chat_label or "").strip()
    if label:
        labeled = re.compile(rf"(?is)\s*Message\s+{re.escape(label)}\s*$")
        t = labeled.sub("", t).rstrip()
    m = _LARK_COMPOSER_MESSAGE_FOOTER_RE.search(t)
    if m:
        t = t[: m.start()].rstrip()
    return t


def _text_for_priority_keyword_trigger(text: str, *, chat_label: str = "") -> str:
    return _strip_lark_composer_message_footer(text, chat_label=chat_label)


_OVERVIEW_TEMPLATE_MARKERS = (
    "incident overview",
    "事故概览",
    "incident start",
    "事故开始",
    "impact scope",
    "影响范围",
    "support request",
    "支援请求",
    "reported time",
    "reporter",
    "affected players",
    "affected user",
)


def _is_manual_p0_incident_overview_template(text: str) -> bool:
    """
    Humans often paste the bilingual **P0 Incident Overview** block (Send overview / manual share).
    Originally we only checked the first line for ``P0 Incident Overview`` / ``P0 事故概览`` — but
    real pastes vary (leading emoji like ``📍``, ``**`` markdown, quoted-reply prefixes, custom
    templates with ``🕐`` instead of ``🕒``, etc.) so we use two layers:

    Layer 1 — first-line title check (tolerant to leading emoji / decoration / markdown).
    Layer 2 — multi-marker heuristic: if the body contains 2+ overview field markers
              (``Issue / Impact Scope / Support Request / Reported Time / 事故概览 / 影响范围 / …``),
              treat as a manual overview paste even if the title line is malformed or missing.

    Either layer matching → skip the keyword trigger silently.
    """
    t = (text or "").strip()
    if not t:
        return False

    first = t.split("\n")[0].strip()
    first_clean = re.sub(r"^[^A-Za-z\u4e00-\u9fff]+", "", first).strip()
    if first_clean:
        if re.match(r"(?is)(?:P0|P1)\s+Incident\s+Overview\b", first_clean):
            return True
        if re.match(r"(?is)(?:P0|P1)\s*事故概览", first_clean):
            return True

    body_lc = t.lower()
    marker_hits = sum(1 for m in _OVERVIEW_TEMPLATE_MARKERS if m in body_lc)
    if marker_hits >= 2:
        return True
    return False


# Incident keyword: message contains ``p0`` but explicitly says no / not P0 / no escalation (no Groq).
_P0_NEGATION_SUBSTRINGS = (
    "no p0 escalation",
    "not a p0",
    "not p0",
    "is not p0",
    "is not a p0",
    "not in p0",
    "non-p0",
    "non p0",
    "without p0 escalation",
    "without a p0",
    "without p0",
    "p0 escalation is not required",
    "p0 escalation is not needed",
    "no need for p0",
    "no need for a p0",
    "no p0 needed",
    "no p0 required",
)


def _is_explicit_p0_negation(text: str) -> bool:
    """
    True when the text contains ``p0`` but clearly *declines* P0 / escalation (e.g. "No P0 escalation is required").
    Skips the naive ``\\bp0\\b`` keyword VC trigger — no LLM required.
    """
    t = (text or "").strip()
    if not t:
        return False
    if not P0_KEYWORD_RE.search(t):
        return False
    low = " ".join(t.lower().split())
    for s in _P0_NEGATION_SUBSTRINGS:
        if s in low:
            return True
    if re.search(r"(?is)\bno\s+p0\s+escalation\b", t):
        return True
    if re.search(r"(?is)\b(?:not|without)\s+(?:a\s+)?p0\b", t):
        return True
    # "will not be consider(ed) as p0" — ``not`` and ``p0`` are not adjacent (old pattern missed this).
    if re.search(
        r"(?is)\b(?:will|would)\s+not\s+(?:be\s+)?consider\w*\s+as\s+(?:a\s+)?p0\b",
        t,
    ):
        return True
    if re.search(r"(?is)\bnot\s+consider\w*\s+as\s+(?:a\s+)?p0\b", t):
        return True
    if re.search(
        r"(?is)\b(?:does|do|did)\s+not\s+(?:qualify|count)\s+(?:as\s+)?(?:a\s+)?p0\b",
        t,
    ):
        return True
    return False


# RCA / postmortem or ticket handoff: "P0 issue(s)", "this P0 case", meegle links — not a VC declaration.
_P0_ISSUE_PROSE_PHRASE_RE = re.compile(
    r"(?is)"
    r"\b(?:a|an|the|this|that|on)\s+p0\s+issues?\b|"
    r"\bp0\s+issues?\b|"
    r"\b(?:a|an|the|this|that|on)\s+p0\s+cases?\b|"
    r"\bp0\s+cases?\b|"
    r"\b(?:a|an|the)\s+priority\s*0\s+issues?\b|"
    r"\bpriority\s*0\s+issues?\b"
)

# Ticket / meegle share to @Duty — informational handoff, not "start bridge now".
_P0_TICKET_HANDOFF_RE = re.compile(
    r"(?is)\bhere\s+(?:is|are)\s+(?:the\s+)?(?:meegle|ticket|story|link|detail)\b"
)

# If any of these appear, treat message as possible real escalation even when it also says "P0 issue".
_P0_MEETING_OR_DECLARE_HINT_RE = re.compile(
    r"(?is)\b(?:"
    r"declar\w*|"
    r"escalat\w*|"
    r"start(?:ing)?\s+(?:a\s+)?(?:the\s+)?(?:p0\s+)?(?:bridge\s+)?meeting\b|"
    r"create\s+(?:a\s+)?(?:p0\s+)?meeting\b|"
    r"open(?:ing)?\s+(?:a\s+)?p0(?:\s+meeting|\s+bridge)?\b|"
    r"need\s+(?:a\s+)?p0\s+meeting\b|"
    r"p0\s+meeting\b|"
    r"p0\s+bridge\b|"
    r"(?:we(?:'re|\s+are)|i(?:'m|s))\s+(?:on|in)\s+p0\b|"
    r"going\s+(?:to\s+)?p0\b|"
    r"treat(?:ed|ing)?\s+(?:this|that|it)\s+as\s+(?:a\s+)?p0\b|"
    r"tag(?:ged|ging)?\s+(?:this|that|it)\s+as\s+(?:a\s+)?p0\b"
    r")\b"
)


def _is_p0_issue_prose_without_meeting_intent(text: str) -> bool:
    """
    True when ``p0`` appears mainly as a **severity label** ("P0 issue") in explanatory text, not as
    instruction to open the emergency bridge.
    """
    t = (text or "").strip()
    if not t or not P0_KEYWORD_RE.search(t):
        return False
    if not _P0_ISSUE_PROSE_PHRASE_RE.search(t):
        return False
    if _P0_MEETING_OR_DECLARE_HINT_RE.search(t):
        return False
    return True


def _is_p0_ticket_handoff_not_declaration(text: str) -> bool:
    """
    True when the line shares a ticket/meegle and mentions P0 as **case context** — not a bridge request.
    Example (skip): "Hi @Duty here is the meegle on this P0 case …"
    """
    t = (text or "").strip()
    if not t or not P0_KEYWORD_RE.search(t):
        return False
    if _is_explicit_direct_p0_declaration(t):
        return False
    if _P0_TICKET_HANDOFF_RE.search(t):
        return True
    if _is_p0_issue_prose_without_meeting_intent(t):
        return True
    return False


def _regex_priority_keyword_intent_override(text_raw: str) -> Optional[str]:
    """
    Deterministic declare vs question when phrasing clearly matches regex (overrides Groq mislabels).
    Returns ``declare_p0``, ``question``, or None (defer to Groq / legacy).
    """
    t = (text_raw or "").strip()
    if not t or not P0_KEYWORD_RE.search(t):
        return None
    if _is_explicit_p0_negation(t):
        return "question"
    if _is_p0_thread_confirm_question(t):
        return "question"
    if _is_explicit_direct_p0_declaration(t):
        return "declare_p0"
    # NOTE: the softer _is_question_about_priority (is-this-p0?, a stray '?') is deliberately NOT a
    # hard override here. When P0_KEYWORD_AI_TRIAGE is on we want the LLM to classify these — not
    # pre-label them "question" and skip the model. (Explicit negation / thread-confirm / explicit
    # declaration stay deterministic above; the legacy no-AI path still uses the regex directly.)
    return None


def _sanitize_priority_keyword_ai_triage(text_raw: str, result: Dict[str, str]) -> Dict[str, str]:
    """Correct common Groq mislabels when only P0 or only P1 appears in the text."""
    override = _regex_priority_keyword_intent_override(text_raw)
    if override:
        groq_intent = str((result or {}).get("intent") or "").strip().lower()
        if groq_intent != override:
            log.info(
                "Priority keyword AI triage: regex override groq=%s -> %s text_head=%r",
                groq_intent or "(none)",
                override,
                (text_raw or "")[:200],
            )
        return {
            **(result or {}),
            "intent": override,
            "reason": f"regex_override:{override}",
            "provider": (result or {}).get("provider") or "regex",
        }
    intent = str(result.get("intent") or "").strip().lower()
    has_p0 = bool(P0_KEYWORD_RE.search(text_raw or ""))
    has_p1 = bool(P1_KEYWORD_RE.search(text_raw or ""))
    if has_p0 and not has_p1 and intent == "declare_p1":
        log.warning(
            "Priority keyword AI triage: Groq intent=declare_p1 but text is P0-only — remapping to question text_head=%r",
            (text_raw or "")[:200],
        )
        return {**result, "intent": "question", "reason": "P0-only text; declare_p1 mislabel"}
    if has_p1 and not has_p0 and intent == "declare_p0":
        log.warning(
            "Priority keyword AI triage: Groq intent=declare_p0 but text is P1-only — remapping to question text_head=%r",
            (text_raw or "")[:200],
        )
        return {**result, "intent": "question", "reason": "P1-only text; declare_p0 mislabel"}
    return result


def _priority_keyword_ai_triage(text_raw: str, groq_key: str) -> Optional[Dict[str, str]]:
    """Run LLM classifier (Claude → Gemini → Groq) when ``P0_KEYWORD_AI_TRIAGE`` is on."""
    if not get_p0_keyword_ai_triage():
        return None
    override = _regex_priority_keyword_intent_override(text_raw)
    if override:
        result = {
            "intent": override,
            "reason": f"regex_override:{override}",
            "provider": "regex",
        }
        log.info(
            "Priority keyword AI triage provider=regex intent=%s reason=%r text_head=%r",
            override,
            result["reason"],
            (text_raw or "")[:200],
        )
        return result
    if not resolve_priority_keyword_ai_provider():
        return None
    try:
        result = classify_priority_keyword(text_raw, provider=None)
        if result:
            result = _sanitize_priority_keyword_ai_triage(text_raw, result)
            log.info(
                "Priority keyword AI triage provider=%s intent=%s reason=%r text_head=%r",
                result.get("provider") or "-",
                result.get("intent"),
                result.get("reason"),
                (text_raw or "")[:200],
            )
        else:
            override = _regex_priority_keyword_intent_override(text_raw)
            if override:
                result = {
                    "intent": override,
                    "reason": f"regex_override:{override}",
                    "provider": "regex",
                }
                log.info(
                    "Priority keyword AI triage: LLM empty — regex override intent=%s text_head=%r",
                    override,
                    (text_raw or "")[:200],
                )
            else:
                log.info(
                    "Priority keyword AI triage: no usable Groq result (legacy/GROQ_GATE path may apply) text_head=%r",
                    (text_raw or "")[:200],
                )
        return result
    except Exception as e:
        log.warning("Priority keyword AI triage failed: %s", e)
        return None


def _legacy_p0_keyword_blocked(text_raw: str) -> bool:
    """Regex/heuristic path when AI triage is off or unavailable."""
    if _is_question_about_priority(text_raw):
        log.info("Incident group: P0 trigger ignored (question about priority) text=%r", text_raw[:200])
        return True
    if _is_p0_ticket_handoff_not_declaration(text_raw):
        log.info(
            "Incident group: P0 trigger ignored (P0 case / ticket handoff, not a declaration) text_head=%r",
            text_raw[:200],
        )
        return True
    if get_p0_keyword_use_builtin_context_filters():
        if _is_p0_informational_ask_or_past_reference(text_raw):
            log.info(
                "Incident group: P0 trigger ignored (informational ask or past P0 reference, not new bridge) "
                "text_head=%r",
                text_raw[:200],
            )
            return True
        if _is_p0_issue_prose_without_meeting_intent(text_raw):
            log.info(
                "Incident group: P0 trigger ignored (narrative 'P0 issue' / severity label, "
                "no declare/meeting intent) text_head=%r",
                text_raw[:200],
            )
            return True
        if _is_p0_inside_existing_meeting_context(text_raw):
            log.info(
                "Incident group: P0 trigger ignored (status inside existing P0 meeting/call, not new bridge) "
                "text_head=%r",
                text_raw[:200],
            )
            return True
    sup_re = get_p0_keyword_supplemental_skip_regex()
    if sup_re is not None and sup_re.search(text_raw):
        log.info(
            "Incident group: P0 trigger ignored (P0_KEYWORD_SUPPLEMENTAL_SKIP_REGEX match) text_head=%r",
            text_raw[:200],
        )
        return True
    return False


# Status line: "... in / during the P0 meeting" refers to activity inside an **existing** bridge — not a new VC request.
_P0_IN_EXISTING_MEETING_PHRASE_RE = re.compile(
    r"(?is)\b(?:into|in|during|at|on|within|for|inside)\s+(?:the\s+|a\s+|our\s+)?(?:p0|priority\s*0)\s+meeting\b"
    r"|(?:in|during|at|on|within)\s+(?:the\s+|a\s+|our\s+)?(?:p0|priority\s*0)\s+(?:call|bridge|huddle)\b"
)
# Do not skip when the same line clearly requests opening a **new** P0 meeting.
_P0_IN_MEETING_CONTEXT_OVERRIDE_RE = re.compile(
    r"(?is)\b(?:"
    r"start(?:ing)?\s+(?:a\s+|the\s+|our\s+)?(?:new\s+)?p0\s+meeting\b|"
    r"open(?:ing)?\s+(?:a\s+|the\s+)?p0\s+meeting\b|"
    r"create\s+(?:a\s+|the\s+)?p0\s+meeting\b|"
    r"need\s+(?:a\s+|the\s+)?(?:new\s+)?p0\s+meeting\b|"
    r"declar\w*\s+(?:a\s+|the\s+)?p0\b|"
    r"escalat\w*\s+(?:to\s+)?(?:a\s+|the\s+)?p0\b"
    r")\b"
)


def _is_p0_inside_existing_meeting_context(text: str) -> bool:
    """True when P0 appears only as *where* work happens (existing meeting/call), not a new bridge request."""
    t = (text or "").strip()
    if not t or not P0_KEYWORD_RE.search(t):
        return False
    if not _P0_IN_EXISTING_MEETING_PHRASE_RE.search(t):
        return False
    if _P0_IN_MEETING_CONTEXT_OVERRIDE_RE.search(t):
        return False
    return True


def _is_explicit_direct_p0_declaration(text: str) -> bool:
    """
    Short, unmistakable **declarations** (not questions). Groq gate often false-negatives these.

    Examples: "this is p0", "it's p0", whole-line "p0", "we tag this issue as p0".
    """
    t = (text or "").strip()
    if not t or not P0_KEYWORD_RE.search(t):
        return False
    if _is_explicit_p0_negation(t):
        return False
    if re.search(r"(?is)\bthis\s+is\s+(?:a\s+)?(?:p0|priority\s*0)\b", t):
        return True
    if re.search(r"(?is)\bthat\s+is\s+(?:a\s+)?(?:p0|priority\s*0)\b", t):
        return True
    if re.search(r"(?is)\bit(?:'s|\s+is)\s+(?:a\s+)?(?:p0|priority\s*0)\b", t):
        return True
    # "yes team this issue is p0" — Groq often misses; not "is this issue p0?" (question path).
    if re.search(
        r"(?is)\b(?:this|that|the)\s+issue\s+is\s+(?:a\s+)?(?:p0|priority\s*0)\b",
        t,
    ):
        return True
    if re.match(r"(?is)^(?:p0|priority\s*0)\s*[!?.…]*\s*$", t):
        return True
    # "we tag … as p0" — modal + we + tag stays with Groq / thread-confirm ("can we tag…").
    if not re.search(r"(?is)\b(?:can|could|should|may|would|shall)\s+we\s+(?:tag|treat|consider|declare)", t):
        if re.search(
            rf"(?is)\b(?:i|we)\s+(?:will\s+)?consider(?:ed|ing)?\s+{_P0_SUBJECT}\s+(?:as\s+)?(?:a\s+)?(?:p0|priority\s*0)\b",
            t,
        ):
            return True
        if re.search(
            rf"(?is)\b(?:confirm|confirmed)\s+(?:as\s+)?(?:a\s+)?(?:p0|priority\s*0)\b",
            t,
        ):
            return True
        if re.search(
            r"(?is)\b(?:i|we)\s+treat(?:ed|ing)?\s+(?:this|that|it|the\s+issue|this\s+issue)\s+as\s+(?:a\s+)?(?:p0|priority\s*0)\b",
            t,
        ):
            return True
        if re.search(
            r"(?is)\b(?:i|we)\s+tag(?:ged|ging)?\s+(?:this|that|it|the\s+issue|this\s+issue)\s+as\s+(?:a\s+)?(?:p0|priority\s*0)\b",
            t,
        ):
            return True
        # Passive / subjectless tag: "this issue tagged as p0", "tagged as p0", "marked as p0"
        # (no I/we subject). These are real declarations the LLM triage keeps mislabeling as
        # "handoff" — make them deterministic so a declaration never depends on the classifier.
        # Modal questions ("should we tag … as p0") are already excluded by the guard above.
        if re.search(
            r"(?is)\b(?:tagged|treated|marked|flagged|classified|labell?ed)\s+as\s+(?:a\s+)?(?:p0|priority\s*0)\b",
            t,
        ):
            return True
    return False


# Cancel commands: optional free-text reason after the phrase (e.g. "cancel meeting no need yet")
# Order: longer prefixes first so "cancel meeting" wins over "cancel".
# The keyword must be a WHOLE word — the ``(?=\s|$)`` lookahead stops arbitrary letters gluing onto
# the short ``cm`` / ``cancel`` triggers (e.g. a screenshot caption ``cmsdb`` must NOT cancel the P0
# meeting; only ``cm``, ``cm <reason>``, ``cancel``, ``cancel meeting <reason>`` should).
CANCEL_WITH_OPTIONAL_REASON_RE = re.compile(
    r"^\s*(cancel\s+meeting|cancel\s+p0|cancel\s+p1|cancel|cm)(?=\s|$)\s*(.*)$",
    re.IGNORECASE,
)


def _matches_typed_end_meeting_command(text_raw: str) -> bool:
    """Operator guide: whole-line **end meeting**; **p0 end** / **end p0** / … may appear in prose."""
    t = (text_raw or "").strip()
    if not t:
        return False
    if re.match(r"(?is)^\s*(?:em|end\s+meeting|close\s+meeting)\s*$", t):
        return True
    if re.match(r"(?is)^\s*(?:pe|p0e)\s*$", t):
        return True
    if re.match(r"(?is)^\s*(?:p1e|1e)\s*$", t):
        return True
    if re.search(r"(?is)\b(?:p0|p1)\s+end\b", t):
        return True
    if re.search(r"(?is)\bend\s+(?:p0|p1)\b", t):
        return True
    if re.search(r"(?is)\bclose\s+p0\b", t):
        return True
    if re.search(r"(?is)\bp0\s+resolved\b", t):
        return True
    return False


# Clear cooldown only (no new VC). Whole line only. / 仅清除冷却，不新建会议
COOLDOWN_RESET_RE = re.compile(
    r"^\s*(p0\s+cooldown\s+reset|cooldown\s+reset|reset\s+cooldown|clear\s+cooldown|cr)\s*$",
    re.IGNORECASE,
)

# While P1 "create meeting?" is pending — typed confirm / decline (card has **Create meeting** / **Don't need**).
# Strict whole-line pattern kept for reference; see _matches_p1_pending_create_reply() for handling @mentions + "yes, because …".
P1_PENDING_CREATE_RE = re.compile(
    r"^\s*(create\s+meeting|p1\s+create|yes)\s*$",
    re.IGNORECASE,
)
P1_PENDING_DECLINE_RE = re.compile(
    r"^\s*(not\s+needed|don'?t\s+need|no)\s*$",
    re.IGNORECASE,
)

# Detects a *question* about P0 ("is this p0", "can we tag this as p0") vs a declaration.
# Used only by _is_p0_thread_confirm_question / _is_question_about_priority to keep questions
# from auto-declaring. (The old designated-asker thread-confirm flow was removed.)
# ``?`` optional (not only questions with ``?``); phrase may follow @mentions ("@QA is this P0?").
P0_THREAD_CONFIRM_QUESTION_RE = re.compile(
    rf"(?is)(?:"
    rf"is\s+this\s+(?:an?\s+)?{_P0_ONLY}\b|"
    rf"is\s+that\s+(?:an?\s+)?{_P0_ONLY}\b|"
    rf"is\s+it\s+(?:an?\s+)?{_P0_ONLY}\b|"
    rf"is\s+this\s+[^\n?]+\s+is\s+(?:a\s+)?{_P0_ONLY}\b|"
    rf"is\s+this\s+[^\n?]+\s+(?:a\s+)?{_P0_ONLY}\b|"
    rf"if\s+this\s+[^\n?]+\s+is\s+(?:a\s+)?{_P0_ONLY}\b|"
    rf"if\s+that\s+[^\n?]+\s+is\s+(?:a\s+)?{_P0_ONLY}\b|"
    rf"is\s+this\s+[^\n?]+\s+as\s+(?:a\s+)?{_P0_ONLY}\b|"
    rf"is\s+that\s+[^\n?]+\s+as\s+(?:a\s+)?{_P0_ONLY}\b|"
    rf"can\s+we\s+tag\s+(?:{_P0_SUBJECT}\s+)?as\s+(?:a\s+)?{_P0_ONLY}\b|"
    rf"could\s+we\s+tag\s+(?:{_P0_SUBJECT}\s+)?as\s+(?:a\s+)?{_P0_ONLY}\b|"
    rf"shall\s+we\s+tag\s+{_P0_SUBJECT}\s+as\s+(?:a\s+)?{_P0_ONLY}\b|"
    rf"can\s+this\s+be\s+tagged\s+as\s+(?:a\s+)?{_P0_ONLY}\b|"
    rf"can\s+that\s+be\s+tagged\s+as\s+(?:a\s+)?{_P0_ONLY}\b|"
    rf"can\s+we\s+refer\s+(?:{_P0_SUBJECT}\s+)?as\s+(?:a\s+)?{_P0_ONLY}\b|"
    rf"could\s+we\s+refer\s+(?:{_P0_SUBJECT}\s+)?as\s+(?:a\s+)?{_P0_ONLY}\b|"
    rf"is\s+this\s+possible\s+as\s+(?:a\s+)?{_P0_ONLY}\b|"
    rf"is\s+that\s+possible\s+as\s+(?:a\s+)?{_P0_ONLY}\b|"
    rf"should\s+we\s+declare\s+{_P0_SUBJECT}\s+as\s+(?:a\s+)?{_P0_ONLY}\b|"
    rf"should\s+i\s+declare\s+{_P0_SUBJECT}\s+as\s+(?:a\s+)?{_P0_ONLY}\b|"
    rf"can\s+we\s+declare\s+{_P0_SUBJECT}\s+as\s+(?:a\s+)?{_P0_ONLY}\b|"
    rf"could\s+we\s+declare\s+{_P0_SUBJECT}\s+as\s+(?:a\s+)?{_P0_ONLY}\b|"
    rf"can\s+we\s+consider\s+{_P0_SUBJECT}\s+as\s+(?:a\s+)?{_P0_ONLY}\b|"
    rf"could\s+we\s+consider\s+{_P0_SUBJECT}\s+as\s+(?:a\s+)?{_P0_ONLY}\b|"
    rf"shall\s+we\s+consider\s+{_P0_SUBJECT}\s+as\s+(?:a\s+)?{_P0_ONLY}\b"
    rf")"
)


def _is_p0_thread_confirm_question(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return bool(P0_THREAD_CONFIRM_QUESTION_RE.search(t))


def _strip_leading_at_mentions_for_confirm(
    line: str, mention_names: Optional[List[str]] = None
) -> str:
    """
    Lark text may use ``@_user_1`` (single token) or UI-style ``@CP OM Duty`` (spaces in the label).
    Strip **longest** ``@displayName`` first using webhook ``mentions[].name``, then ``@\\S+`` tokens.
    """
    line = (line or "").strip()
    while True:
        changed = False
        names = [n.strip() for n in (mention_names or []) if (n or "").strip()]
        names.sort(key=len, reverse=True)
        for n in names:
            prefix = "@" + n
            if line.startswith(prefix):
                line = line[len(prefix) :].lstrip()
                changed = True
                break
        if changed:
            continue
        nxt = re.sub(r"^\s*@\S+\s+", "", line, count=1)
        if nxt != line:
            line = nxt.strip()
            continue
        break
    return line


def _matches_p1_pending_create_reply(
    text_raw: str, mention_names: Optional[List[str]] = None
) -> bool:
    """P1 card typed confirm: allow @mentions and short explanations after **yes** / **create meeting**."""
    t = (text_raw or "").strip()
    if not t:
        return False
    line = t.split("\n")[0].strip()
    line = re.sub(r"<[^>]+>", "", line).strip()
    line = _strip_leading_at_mentions_for_confirm(line, mention_names)
    s = line.strip()
    if not s:
        return False
    if P1_PENDING_CREATE_RE.match(s):
        return True
    return bool(
        re.match(r"^\s*(?:create\s+meeting|p1\s+create)\b", s, re.IGNORECASE)
        or re.match(r"^\s*yes\b", s, re.IGNORECASE)
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


def _parse_mixed_commands(ring_raw: str) -> Tuple[List[str], List[str]]:
    """Ordered, deduped commands from ONE message, allowing a MIX in a single line — e.g.
    ``/srebac sfpms cpms`` or ``/scpms /fpms /c @Juan``. Returns ``(game_cmds, ring_cmds)``: game_cmds
    are SRE-game escalation commands (srebac …); ring_cmds are duty/direct ring commands (scpms, fpms,
    dba, c, …), fired into the active meeting.

    Only ONE leading slash is needed — bare (slashless) command tokens are accepted, BUT only when the
    whole message is a PURE command list (every token is a known command). If ANY token is prose
    (e.g. "@bot pms is down"), only slash-prefixed tokens count, so casual chat can never page.
    (@mentions are already stripped from ``ring_raw``; tagged targets come from mention_open_ids.)
    """
    from features.recording.sre_game import is_sre_game_command

    toks = [t for t in (ring_raw or "").split() if t]

    def _cmd(tok: str) -> str:
        return tok.lstrip("/").strip().lower()

    def _known(c: str) -> bool:
        return bool(is_sre_game_command(c) or c == "c" or RING_CMD_RE.match(c))

    has_prose = any(not _known(_cmd(t)) for t in toks)
    game: List[str] = []
    ring: List[str] = []
    gseen: set = set()
    rseen: set = set()
    for tok in toks:
        if has_prose and not tok.startswith("/"):
            continue  # with prose present, require an explicit slash per command
        c = _cmd(tok)
        if not c:
            continue
        if is_sre_game_command(c):
            if c not in gseen:
                gseen.add(c)
                game.append(c)
        elif (c == "c" or RING_CMD_RE.match(c)) and c not in rseen:
            rseen.add(c)
            ring.append(c)
    return game, ring


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

    message_type = str(kwargs.get("message_type") or "").strip().lower()
    message_id = str(kwargs.get("message_id") or "").strip()
    message_create_time = str(kwargs.get("message_create_time") or "").strip()
    parent_id = str(kwargs.get("parent_id") or "").strip()
    root_id = str(kwargs.get("root_id") or "").strip()
    thread_id = str(kwargs.get("thread_id") or "").strip()
    image_key = str(kwargs.get("image_key") or kwargs.get("file_key") or "").strip()
    mention_names = _clean_mention_names(kwargs.get("mention_names") or kwargs.get("mentions"))
    tenant_token = str(kwargs.get("tenant_token") or token or "").strip()
    chat_type = str(kwargs.get("chat_type") or "").strip().lower()
    sender_lark_user_id = str(kwargs.get("sender_lark_user_id") or "").strip()

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
    # INCIDENT GROUP + prompt/mirror session commands
    # ---------------------------------------------------------
    is_detection = chat_id in get_incident_group_chat_ids()
    mirror_session_source = (
        "" if is_detection else resolve_source_incident_chat_for_session_command(chat_id)
    )
    if is_detection or mirror_session_source:
        if not text_raw:
            return

        if is_detection:
            session_source = chat_id
        else:
            session_source = mirror_session_source

        # Bot replies from typed commands: same destination as meeting cards for this incident row.
        notify_chat = get_session_meeting_card_post_chat_id(session_source) or chat_id

        if HELP_RE.match(text_raw.strip()):
            if token:
                st, body, _ = post_card_to_chat(notify_chat, token, build_help_commands_card())
                if st != 200:
                    log.warning("incident group help card failed HTTP=%s body=%s", st, (body or "")[:300])
            return

        # Ring commands page duty/escalation into the already-active meeting. Trigger via a leading
        # slash (/m /e /fe /fpms /cpms /pms /scpms …) OR by @mentioning the bot (@bot m). A bare
        # "m"/"e" with NEITHER must not trigger, so stray single letters in normal chat can't page.
        # Checked before the screenshot handler so these short commands aren't swallowed.
        _ring_raw = _strip_leading_mentions(text_raw, mention_names).strip()
        _ring_is_slash = _ring_raw.startswith("/")
        _ring_cmd = _ring_raw.lstrip("/").strip().lower()
        _ring_first = _ring_cmd.split()[0] if _ring_cmd.split() else ""

        # SRE Game escalation. First: a no/yes REPLY inside an active escalation thread advances/stops
        # it (scoped — a non-yes/no reply is ignored here and falls through to normal routing, so this
        # never swallows unrelated chatter). Then: a /sre<game> command starts a new escalation.
        _esc_keys = [k for k in (root_id, parent_id, thread_id) if k]
        if _esc_keys:
            from features.recording.sre_game import maybe_handle_sre_game_reply

            # Pass ALL thread identifiers (root/parent/thread) + the mention-stripped text so "@bot /n"
            # and a bare "/n" both parse to "n" and match whichever key the escalation was stored under.
            if maybe_handle_sre_game_reply(
                _esc_keys,
                _ring_raw,
                token,
                tenant_token=tenant_token or token,
                operator_open_id=user_id,
                tagged_open_ids=[x for x in (kwargs.get("mention_open_ids") or []) if x],
            ):
                return
        # Mixed commands in ONE message: "/srebac sfpms cpms" or "/scpms /fpms /c @Juan @Maria" — fire
        # EACH into the active meeting. SRE-game commands (srebac …) start an escalation; duty/direct
        # commands (scpms, fpms, dba, /c …) page their people. Only ONE leading slash is needed (bare
        # tokens are honored when the whole message is a pure command list); /c consumes the message
        # @mentions and needs @bot. A single command (/srebac, /fpms, @bot m) also flows through here.
        _game_cmds, _ring_cmds = _parse_mixed_commands(_ring_raw)
        if (_game_cmds or _ring_cmds) and (_ring_is_slash or _mentions_our_bot(mention_names)):
            _bot_mentioned = _mentions_our_bot(mention_names)
            _direct = [x for x in (kwargs.get("mention_open_ids") or []) if x]
            log.info(
                "mixed cmds detected game=%s ring=%s slash=%s bot_mentioned=%s tagged=%s chat_tail=%s",
                _game_cmds,
                _ring_cmds,
                _ring_is_slash,
                _bot_mentioned,
                len(_direct),
                chat_id[-8:] if chat_id else "",
            )
            from features.recording.sre_game import start_sre_game_escalation
            from features.recording.vc_ring import handle_ring_command

            for _gc in _game_cmds:
                start_sre_game_escalation(
                    _gc,
                    session_source,
                    notify_chat,
                    token,
                    command_message_id=message_id,
                    thread_root=root_id,
                    operator_open_id=user_id,
                    tenant_token=tenant_token or token,
                )
            for _c in _ring_cmds:
                if _c == "c":
                    if not _bot_mentioned:
                        # /c needs @bot so the @mentions are unambiguously the targets.
                        continue
                    handle_ring_command(
                        "c",
                        session_source,
                        notify_chat,
                        token,
                        operator_open_id=user_id,
                        tenant_token=tenant_token or token,
                        direct_open_ids=_direct,
                        reply_to_message_id=message_id,
                    )
                else:
                    handle_ring_command(
                        _c,
                        session_source,
                        notify_chat,
                        token,
                        operator_open_id=user_id,
                        tenant_token=tenant_token or token,
                        reply_to_message_id=message_id,
                    )
            return

        # @bot /c @person… — ad-hoc DIRECT call (Model A): ring the TAGGED people straight from the
        # message @mentions. REQUIRES @mentioning the bot (so the user @mentions are unambiguously the
        # targets, and the bot is dropped from them). Bare "/c @a" without @bot does NOT trigger.
        if _ring_first == "c" and _mentions_our_bot(mention_names):
            _direct = [x for x in (kwargs.get("mention_open_ids") or []) if x]
            log.info(
                "direct ring /c slash=%s tagged=%s chat_tail=%s",
                _ring_is_slash,
                len(_direct),
                chat_id[-8:] if chat_id else "",
            )
            from features.recording.vc_ring import handle_ring_command

            handle_ring_command(
                "c",
                session_source,
                notify_chat,
                token,
                operator_open_id=user_id,
                tenant_token=tenant_token or token,
                direct_open_ids=_direct,
            )
            return

        if RING_CMD_RE.match(_ring_cmd):
            _bot_mentioned = _mentions_our_bot(mention_names)
            log.info(
                "ring cmd detected cmd=%r slash=%s bot_mentioned=%s mentions=%s chat_tail=%s session_source_tail=%s",
                _ring_cmd,
                _ring_is_slash,
                _bot_mentioned,
                mention_names,
                chat_id[-8:] if chat_id else "",
                session_source[-8:] if session_source else "",
            )
            if _ring_is_slash or _bot_mentioned:
                from features.recording.vc_ring import handle_ring_command

                handle_ring_command(
                    _ring_cmd,
                    session_source,
                    notify_chat,
                    token,
                    operator_open_id=user_id,
                    tenant_token=tenant_token or token,
                )
                return

        if try_handle_graph_screenshot_request(
            text_raw,
            chat_id,
            tenant_token or token,
            source_chat_name,
            mention_names=mention_names,
            groq_key=groq_key,
            message_id=message_id,
        ):
            return

        # Typed P1 prompt reply (before cancel so "no" does not collide with other routes)
        pend = get_p1_prompt_pending(session_source)
        if pend:
            nonce = str(pend.get("nonce") or "").strip()
            if _matches_p1_pending_create_reply(text_raw, mention_names):
                err = handle_p1_meeting_confirm_yes(session_source, token, user_id, nonce)
                if err == "session_active":
                    post_text_to_chat(
                        notify_chat,
                        token,
                        "ℹ️ A meeting session is already active in this chat.",
                    )
                elif err == "stale":
                    post_text_to_chat(
                        notify_chat,
                        token,
                        "ℹ️ This P1 confirmation is out of date or was already answered.",
                    )
                return
            if P1_PENDING_DECLINE_RE.match(text_raw.strip()):
                err = handle_p1_meeting_confirm_no(session_source, token, nonce)
                if err == "session_active":
                    post_text_to_chat(
                        notify_chat,
                        token,
                        "ℹ️ A meeting is already active in this chat. Just type **cancel meeting** if you want to end it.",
                    )
                elif err == "stale":
                    post_text_to_chat(
                        notify_chat,
                        token,
                        "ℹ️ This P1 confirmation is out of date or was already answered.",
                    )
                return

        if p0_group_typed_meeting_commands_enabled() and _matches_typed_end_meeting_command(text_raw):
            if chat_has_active_session(session_source):
                sess = P0_SESSIONS.get(session_source) or {}
                priority = str(sess.get("priority") or "P0").strip().upper()
                log.info(
                    "Incident UX: end meeting requested message_chat=%s session_source=%s priority=%s",
                    chat_id,
                    session_source,
                    priority,
                )
                end_p0_session(session_source, token)
            else:
                log.info(
                    "Incident UX: end requested but no active session message_chat=%s session_source=%s",
                    chat_id,
                    session_source,
                )
                if token:
                    post_text_to_chat(
                        notify_chat,
                        token,
                        "ℹ️ No active P0/P1 meeting in this chat to end.",
                    )
            return

        # Cancel command (optional reason after the keyword phrase).
        # Gated by P0_GROUP_TYPED_MEETING_COMMANDS so typed cancel can be turned off in the group.
        cancel_m = (
            CANCEL_WITH_OPTIONAL_REASON_RE.match(text_raw)
            if p0_group_typed_meeting_commands_enabled()
            else None
        )
        if cancel_m:
            tail = (cancel_m.group(2) or "").strip()
            cancel_reason = tail if tail else "Unspecified"
            if chat_has_active_session(session_source):
                sess = P0_SESSIONS.get(session_source) or {}
                priority = str(sess.get("priority") or "P0").strip().upper()
                log.info(
                    "Incident UX: cancel requested message_chat=%s session_source=%s priority=%s reason=%r",
                    chat_id,
                    session_source,
                    priority,
                    cancel_reason,
                )
                cancel_p0_session(session_source, token, reason=cancel_reason)
            else:
                log.info(
                    "Incident UX: cancel requested but no active session message_chat=%s session_source=%s",
                    chat_id,
                    session_source,
                )
                if token:
                    post_text_to_chat(
                        notify_chat,
                        token,
                        "ℹ️ No active P0/P1 meeting in this chat to cancel.",
                    )
            return

        # Cooldown reset only — no new VC. / 只清冷却，不创建会议
        if COOLDOWN_RESET_RE.match(text_raw.strip()):
            clear_p0_cooldown(session_source)
            if token:
                post_text_to_chat(
                    notify_chat,
                    token,
                    "ℹ️ Cooldown cleared for this group. The next **p0** or **p1** declaration in this chat will no longer be blocked by cooldown.",
                )
            return

        if is_detection:
            kw_text = _text_for_priority_keyword_trigger(text_raw, chat_label=source_chat_name)
            if P0_KEYWORD_RE.search(text_raw or "") and not P0_KEYWORD_RE.search(kw_text or ""):
                log.info(
                    "Incident group: P0 keyword ignored (Lark composer footer only) chat_id=%s footer_tail=%r",
                    chat_id,
                    (text_raw or "")[-60:],
                )
            # Trigger P0 if ``p0`` / ``priority 0`` appears anywhere (unless pasted invite footer).
            # When Issue Watch is on, only *explicit* P0 declarations start a meeting — player
            # reports (incl. Lark footer ``Message p0 detection dev``) go to Issue Watch DM first.
            _p0_kw_hit = (not _is_pasted_meeting_invite_footer(text_raw)) and P0_KEYWORD_RE.search(kw_text)
            _p0_skip_for_issue_watch = (
                _p0_kw_hit
                and get_p0_issue_watch_enabled()
                and not _is_explicit_direct_p0_declaration(kw_text)
            )
            if _p0_skip_for_issue_watch:
                log.info(
                    "Incident group: P0 keyword deferred to Issue Watch (not explicit declare) chat_id=%s text_tail=%r",
                    chat_id,
                    (text_raw or "")[-80:],
                )
                # Also offer the duty a Yes/No "create meeting?" DM (P0_KEYWORD_CONFIRM_DM_ENABLED):
                # Issue Watch only auto-declares on multi-report/high-confidence, so a single p0
                # mention would otherwise pass silently. Falls through to Issue Watch after.
                _maybe_p0_keyword_confirm_dm(
                    chat_id=chat_id,
                    token=token,
                    user_id=user_id,
                    sender_lark_user_id=sender_lark_user_id,
                    source_chat_name=source_chat_name,
                    text_raw=text_raw,
                    message_id=message_id,
                )
            if _p0_kw_hit and not _p0_skip_for_issue_watch:
                if _is_manual_p0_incident_overview_template(text_raw):
                    log.info(
                        "Incident group: P0 trigger ignored (manual P0 Incident Overview template) text_head=%r",
                        text_raw[:200],
                    )
                    return
                if _is_explicit_p0_negation(text_raw):
                    log.info(
                        "Incident group: P0 trigger ignored (explicit not/no p0 or no escalation) text_head=%r",
                        text_raw[:200],
                    )
                    return
                # When AI triage is ON, do NOT pre-ignore on the blunt regex — let the message reach
                # the LLM below so Claude decides declare-vs-question. Only the no-AI legacy path
                # short-circuits here. (Explicit negation is already handled hard, above.)
                if not get_p0_keyword_ai_triage() and _is_question_about_priority(text_raw):
                    log.info(
                        "Incident group: P0 trigger ignored (question about priority) text=%r",
                        text_raw[:200],
                    )
                    _maybe_p0_keyword_confirm_dm(
                        chat_id=chat_id,
                        token=token,
                        user_id=user_id,
                        sender_lark_user_id=sender_lark_user_id,
                        source_chat_name=source_chat_name,
                        text_raw=text_raw,
                        message_id=message_id,
                    )
                    return
                if (user_id or "").strip() in get_p0_trigger_ignore_open_ids():
                    log.info("Incident group: P0 trigger ignored (P0_TRIGGER_IGNORE_OPEN_IDS) user_id=%s", user_id)
                    return
                if chat_has_active_session(chat_id):
                    if get_p0_multi_meeting_per_group():
                        log.info(
                            "Incident group: multi-meeting mode — starting an additional concurrent P0 chat_id=%s",
                            chat_id,
                        )
                        # fall through: start_p0 creates a new coexisting meeting + session
                    elif get_p0_redeclare_supersedes_active():
                        log.info(
                            "Incident group: re-declare supersedes active session — cancelling then starting new chat_id=%s",
                            chat_id,
                        )
                        cancel_p0_session(chat_id, token, reason="Superseded by a new P0 declaration")
                        clear_p0_cooldown(chat_id)
                        # fall through to start a fresh P0 below
                    else:
                        log.info("Incident group: session already active chat_id=%s", chat_id)
                        return

                ai = _priority_keyword_ai_triage(kw_text, groq_key)
                if ai is not None:
                    if ai.get("intent") != "declare_p0":
                        log.info(
                            "Incident group: P0 AI triage — no meeting (intent=%s) text_head=%r",
                            ai.get("intent"),
                            kw_text[:200],
                        )
                        _maybe_p0_keyword_confirm_dm(
                            chat_id=chat_id,
                            token=token,
                            user_id=user_id,
                            sender_lark_user_id=sender_lark_user_id,
                            source_chat_name=source_chat_name,
                            text_raw=text_raw,
                            message_id=message_id,
                        )
                        return
                elif _legacy_p0_keyword_blocked(kw_text):
                    # Rule: a P0 keyword must NEVER be dropped silently — the legacy regex/heuristic
                    # path only blocks AUTO-declare, so still offer the duty a Yes/No confirm DM.
                    # (_maybe_p0_keyword_confirm_dm self-skips explicit negations / past references.)
                    _maybe_p0_keyword_confirm_dm(
                        chat_id=chat_id,
                        token=token,
                        user_id=user_id,
                        sender_lark_user_id=sender_lark_user_id,
                        source_chat_name=source_chat_name,
                        text_raw=text_raw,
                        message_id=message_id,
                    )
                    return
                elif get_p0_keyword_groq_gate():
                    if _is_explicit_direct_p0_declaration(kw_text):
                        log.info(
                            "Incident group: P0_KEYWORD_GROQ_GATE bypass (explicit direct declaration) text_head=%r",
                            kw_text[:200],
                        )
                    else:
                        gv = groq_p0_keyword_declares_new_bridge(kw_text)
                        if gv is False:
                            log.info(
                                "Incident group: P0 trigger ignored (P0_KEYWORD_GROQ_GATE: Groq says not a P0 declaration) "
                                "text_head=%r",
                                text_raw[:200],
                            )
                            _maybe_p0_keyword_confirm_dm(
                                chat_id=chat_id,
                                token=token,
                                user_id=user_id,
                                sender_lark_user_id=sender_lark_user_id,
                                source_chat_name=source_chat_name,
                                text_raw=text_raw,
                                message_id=message_id,
                            )
                            return
                        if gv is None:
                            log.warning(
                                "Incident group: P0_KEYWORD_GROQ_GATE inconclusive (fail-open proceed) text_head=%r",
                                text_raw[:200],
                            )

                kw_dedupe = _keyword_trigger_dedupe_key(
                    chat_id, user_id, message_id, message_create_time, text_raw
                )
                if not _try_consume_keyword_trigger_dedupe(kw_dedupe):
                    log.info(
                        "Incident group: P0 keyword skipped (duplicate Lark delivery, same create_time+text) chat_id=%s",
                        chat_id,
                    )
                    return

                log.info("Incident group: starting P0 session chat_id=%s user_id=%s text=%r", chat_id, user_id, text_raw[:200])
                start_p0(
                    chat_id,
                    token,
                    user_id,
                    priority="P0",
                    source_chat_name=source_chat_name,
                    trigger_lark_user_id=sender_lark_user_id,
                )
                return

            # Trigger P1 if ``p1`` / ``priority 1`` appears anywhere (unless pasted invite footer).
            if (not _is_pasted_meeting_invite_footer(text_raw)) and P1_KEYWORD_RE.search(kw_text):
                if _is_explicit_p0_negation(text_raw):
                    log.info(
                        "Incident group: P1 trigger ignored (explicit negation) text_head=%r",
                        text_raw[:200],
                    )
                    return
                if (user_id or "").strip() in get_p0_trigger_ignore_open_ids():
                    log.info("Incident group: P1 trigger ignored (P0_TRIGGER_IGNORE_OPEN_IDS) user_id=%s", user_id)
                    return
                if chat_has_active_session(chat_id):
                    log.info("Incident group: session already active chat_id=%s", chat_id)
                    return
                if get_p1_prompt_pending(chat_id):
                    log.info("Incident group: P1 confirmation already pending chat_id=%s", chat_id)
                    return

                ai = _priority_keyword_ai_triage(text_raw, groq_key)
                if ai is not None:
                    if ai.get("intent") != "declare_p1":
                        log.info(
                            "Incident group: P1 AI triage — no prompt (intent=%s) text_head=%r",
                            ai.get("intent"),
                            text_raw[:200],
                        )
                        return
                elif _is_question_about_priority(text_raw):
                    log.info(
                        "Incident group: P1 trigger ignored (question about priority) text=%r",
                        text_raw[:200],
                    )
                    return

                kw_dedupe = _keyword_trigger_dedupe_key(
                    chat_id, user_id, message_id, message_create_time, text_raw
                )
                if not _try_consume_keyword_trigger_dedupe(kw_dedupe):
                    log.info(
                        "Incident group: P1 keyword skipped (duplicate Lark delivery, same create_time+text) chat_id=%s",
                        chat_id,
                    )
                    return

                log.info("Incident group: P1 keyword — posting meeting confirmation card chat_id=%s user_id=%s", chat_id, user_id)
                set_p1_prompt_pending(chat_id, user_id)
                if not request_p1_meeting_confirmation(chat_id, token, user_id):
                    pop_p1_prompt_pending(chat_id)
                    log.error("Incident group: failed to post P1 confirmation card chat_id=%s", chat_id)
                return

            if try_handle_issue_watch(
                _strip_lark_composer_message_footer(text_raw, chat_label=source_chat_name),
                chat_id,
                user_id,
                tenant_token or token,
                source_chat_name=source_chat_name,
                message_id=message_id,
                message_create_time=message_create_time,
            ):
                return

            # Ignore non P0/P1 chatter in the incident group to avoid noisy auto replies.
            log.info("Incident group: ignoring non P0/P1 message")
            return

        log.info(
            "Prompt/mirror session UX: ignoring message (use detection group to type p0/p1) message_chat=%s",
            chat_id,
        )
        return

    # ---------------------------------------------------------
    # On-demand Grafana screenshot (hub / allowed chats)
    # ---------------------------------------------------------
    if text_raw and try_handle_graph_screenshot_request(
        text_raw,
        chat_id,
        tenant_token or token,
        source_chat_name,
        mention_names=mention_names,
        groq_key=groq_key,
        message_id=message_id,
    ):
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
