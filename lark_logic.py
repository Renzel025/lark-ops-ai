import os
import re
import time
import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

from wiki_ai_logic import handle_wiki_ai
from p0_logic.config import (
    get_incident_group_chat_ids,
    get_overview_target_chat_id_for_source_incident,
    get_p0_thread_confirm_allow_toplevel_yes,
    get_p0_thread_confirm_allow_asker_self_yes,
    get_p0_thread_confirm_asker_open_ids,
    get_p0_thread_confirm_target_open_ids,
    get_p0_thread_confirm_responder_open_ids,
    get_p0_thread_confirm_toplevel_grace_sec,
    get_p0_thread_confirm_ttl_sec,
    get_p0_thread_confirm_use_groq,
    get_p0_trigger_ignore_open_ids,
)
from p0_logic.groq_client import groq_thread_confirm_affirms_p0
from p0_logic.session import handle_p1_meeting_confirm_no, handle_p1_meeting_confirm_yes
from p0_logic.cards import build_no_active_p0_session_card
from p0_logic.lark_client import post_card_to_chat, post_text_to_chat
from p0_logic import (
    start_p0,
    cancel_p0_session,
    clear_p0_cooldown,
    P0_SESSIONS,
    chat_has_active_session,
    handle_dm_generate_overview,
    get_p1_prompt_pending,
    set_p1_prompt_pending,
    pop_p1_prompt_pending,
    request_p1_meeting_confirmation,
)

log = logging.getLogger("lark-ops-ai")

WIKI_GROUP_CHAT_ID = os.getenv("WIKI_GROUP_CHAT_ID", "").strip()

# Keyword anywhere in the sentence (e.g. "this is p0", "we tag this as a P0") — case-insensitive.
# Questions ("is this p0?", "can this be a p1?") are ignored via _is_question_about_priority().
P0_KEYWORD_RE = re.compile(r"\bp0\b|\bpriority\s*0\b", re.IGNORECASE)
P1_KEYWORD_RE = re.compile(r"\bp1\b|\bpriority\s*1\b", re.IGNORECASE)

# Do not start VC when the user is *asking* about P0/P1 (vs declaring). See _is_question_about_priority().
_QUESTION_PRIORITY_PHRASE_RE = re.compile(
    r"(?is)"
    r"(?:"
    r"is\s+this\s+(?:an?\s+)?(?:p0|p1|priority\s*0|priority\s*1)\b|"
    r"is\s+that\s+(?:an?\s+)?(?:p0|p1|priority\s*0|priority\s*1)\b|"
    r"is\s+it\s+(?:an?\s+)?(?:p0|p1|priority\s*0|priority\s*1)\b|"
    r"are\s+we\s+(?:in\s+)?(?:a\s+)?(?:p0|p1|priority\s*0|priority\s*1)\b|"
    r"is\s+this\s+possible\s+as\s+(?:a\s+)?(?:p0|p1|priority\s*0|priority\s*1)\b|"
    r"is\s+that\s+possible\s+as\s+(?:a\s+)?(?:p0|p1|priority\s*0|priority\s*1)\b|"
    r"can\s+we\s+refer\s+(?:this|that|it)\s+as\s+(?:a\s+)?(?:p0|p1|priority\s*0|priority\s*1)\b|"
    r"could\s+we\s+refer\s+(?:this|that|it)\s+as\s+(?:a\s+)?(?:p0|p1|priority\s*0|priority\s*1)\b|"
    r"can\s+we\s+tag\s+(?:(?:this|that|it)\s+)?as\s+(?:a\s+)?(?:p0|p1|priority\s*0|priority\s*1)\b|"
    r"could\s+we\s+tag\s+(?:(?:this|that|it)\s+)?as\s+(?:a\s+)?(?:p0|p1|priority\s*0|priority\s*1)\b|"
    r"should\s+we\s+declare\s+(?:it|this|that)\s+as\s+(?:a\s+)?(?:p0|p1|priority\s*0|priority\s*1)\b|"
    r"should\s+i\s+declare\s+(?:it|this|that)\s+as\s+(?:a\s+)?(?:p0|p1|priority\s*0|priority\s*1)\b|"
    r"can\s+we\s+declare\s+(?:it|this|that)\s+as\s+(?:a\s+)?(?:p0|p1|priority\s*0|priority\s*1)\b|"
    r"could\s+we\s+declare\s+(?:it|this|that)\s+as\s+(?:a\s+)?(?:p0|p1|priority\s*0|priority\s*1)\b|"
    r"can\s+this\s+be\s+(?:an?\s+)?(?:p0|p1|priority\s*0|priority\s*1)\b|"
    r"could\s+this\s+be\s+(?:an?\s+)?(?:p0|p1|priority\s*0|priority\s*1)\b|"
    r"should\s+this\s+be\s+(?:an?\s+)?(?:p0|p1|priority\s*0|priority\s*1)\b|"
    r"would\s+this\s+be\s+(?:an?\s+)?(?:p0|p1|priority\s*0|priority\s*1)\b|"
    r"will\s+this\s+be\s+(?:an?\s+)?(?:p0|p1|priority\s*0|priority\s*1)\b|"
    r"does\s+this\s+(?:count|qualify)\s+(?:as\s+)?(?:a\s+)?(?:p0|p1|priority\s*0|priority\s*1)\b|"
    r"what\s+is\s+(?:a\s+)?(?:p0|p1|priority\s*0|priority\s*1)\b|"
    r"how\s+(?:do|can|to)\s+(?:i|we)\s+(?:know|tell|declare)\s+.*\b(?:p0|p1|priority\s*[01])\b|"
    r"any(?:thing|one)\s+.*\b(?:p0|p1|priority\s*[01])\b"
    r")"
)

# Broken-English asks: "is this issue is p0" (extra words between "is … is p0") don't match phrases above.
_BROKEN_ENGLISH_DOUBLE_IS_PRIORITY_RE = re.compile(
    r"(?is)\bis\s+.+?\bis\s+(?:an?\s+)?(?:p0|p1|priority\s*0|priority\s*1)\b"
)

# Embedded if/whether clause: "please confirm if issue is p0", "need to check if this is p0".
_IF_OR_WHETHER_PRIORITY_CLAUSE_RE = re.compile(
    r"(?is)\b(?:if|whether)\s+.{1,220}?\bis\s+(?:an?\s+)?(?:p0|p1|priority\s*0|priority\s*1)\b"
)


def _is_question_about_priority(text: str) -> bool:
    """
    True if the message looks like a question *about* P0/P1 rather than a declaration.
    Declarations like "this is p0" (statement) still trigger; "is this p0?" does not.
    """
    t = (text or "").strip()
    if not t:
        return False
    if not (P0_KEYWORD_RE.search(t) or P1_KEYWORD_RE.search(t)):
        return False
    # Phrases that arm **thread confirm** are never keyword declarations (e.g. "can we tag it as p0" without `?`).
    if _is_p0_thread_confirm_question(t):
        return True
    # Question mark: treat as non-declaration for incident keyword triggers.
    if "?" in t:
        return True
    if _BROKEN_ENGLISH_DOUBLE_IS_PRIORITY_RE.search(t):
        return True
    if _IF_OR_WHETHER_PRIORITY_CLAUSE_RE.search(t):
        return True
    return bool(_QUESTION_PRIORITY_PHRASE_RE.search(t))


def _is_pasted_meeting_invite_footer(text: str) -> bool:
    """
    Ignore copy-paste of the red meeting-card footer (starts with ``P0 declared -`` / ``P1 declared -``)
    so it does not start another VC.
    """
    t = (text or "").strip().lower()
    return t.startswith("p0 declared - created a meeting") or t.startswith("p1 declared - created a meeting")


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
    return False


# Cancel commands: optional free-text reason after the phrase (e.g. "cancel meeting no need yet")
# Order: longer prefixes first so "cancel meeting" wins over "cancel".
CANCEL_WITH_OPTIONAL_REASON_RE = re.compile(
    r"^\s*(cancel\s+meeting|cancel\s+p0|cancel\s+p1|cancel)\s*(.*)$",
    re.IGNORECASE,
)

# Clear cooldown only (no new VC). Whole line only. / 仅清除冷却，不新建会议
COOLDOWN_RESET_RE = re.compile(
    r"^\s*(p0\s+cooldown\s+reset|cooldown\s+reset|reset\s+cooldown|clear\s+cooldown)\s*$",
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

# --- Thread: designated asker posts "is this P0?" → someone else replies "yes" → start P0 ---
# Arming still requires ``P0_THREAD_CONFIRM_ASKER_OPEN_IDS`` / ``TARGET_OPEN_IDS`` — see config.
# ``?`` optional: "is this p0" / "can we tag this as p0" (not only questions with ``?``).
# Phrase may appear after @mentions (e.g. "@QA Team is this P0?").
P0_THREAD_CONFIRM_QUESTION_RE = re.compile(
    r"(?is)"
    r"(?:is\s+this\s+(?:an?\s+)?(?:p0|priority\s*0)\b"
    r"|is\s+that\s+(?:an?\s+)?(?:p0|priority\s*0)\b"
    r"|is\s+it\s+(?:an?\s+)?(?:p0|priority\s*0)\b"
    r"|is\s+this\s+[^\n?]+\s+is\s+(?:a\s+)?(?:p0|priority\s*0)\b"
    r"|is\s+this\s+[^\n?]+\s+(?:a\s+)?(?:p0|priority\s*0)\b"
    r"|if\s+this\s+[^\n?]+\s+is\s+(?:a\s+)?(?:p0|priority\s*0)\b"
    r"|if\s+that\s+[^\n?]+\s+is\s+(?:a\s+)?(?:p0|priority\s*0)\b"
    r"|is\s+this\s+[^\n?]+\s+as\s+(?:a\s+)?(?:p0|priority\s*0)\b"
    r"|is\s+that\s+[^\n?]+\s+as\s+(?:a\s+)?(?:p0|priority\s*0)\b"
    r"|can\s+we\s+tag\s+(?:(?:this|that|it)\s+)?as\s+(?:a\s+)?(?:p0|priority\s*0)\b"
    r"|could\s+we\s+tag\s+(?:(?:this|that|it)\s+)?as\s+(?:a\s+)?(?:p0|priority\s*0)\b"
    r"|shall\s+we\s+tag\s+(?:this|that|it)\s+as\s+(?:a\s+)?(?:p0|priority\s*0)\b"
    r"|can\s+this\s+be\s+tagged\s+as\s+(?:a\s+)?(?:p0|priority\s*0)\b"
    r"|can\s+that\s+be\s+tagged\s+as\s+(?:a\s+)?(?:p0|priority\s*0)\b"
    r"|can\s+we\s+tag\s+this\s+issue\s+as\s+(?:a\s+)?(?:p0|priority\s*0)\b"
    r"|can\s+we\s+refer\s+(?:(?:this|that|it)\s+)?as\s+(?:a\s+)?(?:p0|priority\s*0)\b"
    r"|could\s+we\s+refer\s+(?:(?:this|that|it)\s+)?as\s+(?:a\s+)?(?:p0|priority\s*0)\b"
    r"|is\s+this\s+possible\s+as\s+(?:a\s+)?(?:p0|priority\s*0)\b"
    r"|is\s+that\s+possible\s+as\s+(?:a\s+)?(?:p0|priority\s*0)\b"
    r"|should\s+we\s+declare\s+(?:it|this|that)\s+as\s+(?:a\s+)?(?:p0|priority\s*0)\b"
    r"|should\s+i\s+declare\s+(?:it|this|that)\s+as\s+(?:a\s+)?(?:p0|priority\s*0)\b"
    r"|can\s+we\s+declare\s+(?:it|this|that)\s+as\s+(?:a\s+)?(?:p0|priority\s*0)\b"
    r"|could\s+we\s+declare\s+(?:it|this|that)\s+as\s+(?:a\s+)?(?:p0|priority\s*0)\b"
    r")"
)
# Reply must read like **P0 approval** — phrase-prefix match (not full NLP).
P0_THREAD_CONFIRM_YES_RE = re.compile(
    r"(?is)^(?:"
    r"(?:yes|yep|yeah|sure|ok|okay|agreed|agree|confirm|confirmed|是|对的|确认)\b|"
    r"yes\s*,?\s*this\s+is\s+(?:a\s+)?(?:p0|priority\s*0)\b|"
    r"we\s+(?:will\s+)?consider\s+(?:it|this|that)\s+(?:as\s+)?(?:a\s+)?(?:p0|priority\s*0)\b|"
    r"we\s+consider\s+it\s+(?:as\s+)?(?:a\s+)?(?:p0|priority\s*0)\b|"
    r"(?:we\s+)?(?:can|could)\s+tag\s+(?:(?:this|that|it)\s+)?as\s+(?:a\s+)?(?:p0|priority\s*0)\b|"
    r"can\s+tag\s+(?:this|that|it)\s+as\s+(?:a\s+)?(?:p0|priority\s*0)\b|"
    r"treat(?:ing)?\s+(?:this|that|it)\s+as\s+(?:a\s+)?(?:p0|priority\s*0)\b|"
    r"(?:this|the)\s+issue\s+is\s+(?:a\s+)?(?:p0|priority\s*0)\b|"
    r"(?:confirm|confirmed)\s+(?:as\s+)?(?:a\s+)?(?:p0|priority\s*0)\b|"
    r"go ahead|sounds good|approved\b|proceed\b|"
    r"we\s+will\s+(?:consider|proceed)\b|"
    r"\+\+"
    r")"
)

_P0_THREAD_LOCK = threading.RLock()
# chat_id -> { "question_message_id", "asker_open_id", "exp" }
_P0_THREAD_PENDING: Dict[str, Dict[str, Any]] = {}
# pending value: question_message_id, asker_open_id, exp, armed_at (epoch float)


def _p0_thread_clear_pending_dict(pend: Dict[str, Any]) -> None:
    """Remove all chat_id keys that reference the same pending object (source + mirrored prompt chat)."""
    with _P0_THREAD_LOCK:
        keys = [k for k, v in _P0_THREAD_PENDING.items() if v is pend]
        for k in keys:
            _P0_THREAD_PENDING.pop(k, None)


def _p0_thread_prune_expired(chat_id: str) -> None:
    with _P0_THREAD_LOCK:
        p = _P0_THREAD_PENDING.get(chat_id)
        if not p:
            return
        if time.time() > float(p.get("exp") or 0):
            _p0_thread_clear_pending_dict(p)


def _thread_reply_targets_question(parent_id: str, root_id: str, question_message_id: str) -> bool:
    q = (question_message_id or "").strip()
    if not q:
        return False
    p = (parent_id or "").strip()
    r = (root_id or "").strip()
    return p == q or r == q


def _is_p0_thread_confirm_question(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return bool(P0_THREAD_CONFIRM_QUESTION_RE.search(t))


def _toplevel_yes_context_ok(
    pend: Dict[str, Any],
    asker_open_id: str,
    mention_open_ids: List[str],
) -> bool:
    """
    Top-level (non-thread) yes: accept if @asker is in Lark ``mentions`` **or** still within
    grace seconds after ``armed_at`` (same conversation window).
    """
    asker = (asker_open_id or "").strip()
    if not asker:
        return False
    mids = [x.strip() for x in (mention_open_ids or []) if (x or "").strip()]
    if asker in mids:
        return True
    grace = float(get_p0_thread_confirm_toplevel_grace_sec())
    if grace <= 0:
        return False
    armed = float((pend or {}).get("armed_at") or 0)
    if armed <= 0:
        return False
    return (time.time() - armed) <= grace


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


def _is_p0_thread_confirm_yes(
    text: str, mention_names: Optional[List[str]] = None
) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    line = t.split("\n")[0].strip()
    line = re.sub(r"<[^>]+>", "", line).strip()
    line = _strip_leading_at_mentions_for_confirm(line, mention_names)
    return bool(P0_THREAD_CONFIRM_YES_RE.match(line))


def _p0_thread_reply_looks_like_p0_question_not_answer(
    text_raw: str, mention_names: Optional[List[str]] = None
) -> bool:
    """
    True if the message reads like **another** arming-style P0 question, not an approval.

    Prevents: armed \"is this P0?\" + follow-up **\"can we tag it as P0\"** from being
    treated as *toplevel yes* + Groq true. Call only after ``_is_p0_thread_confirm_yes`` is false.
    """
    t = (text_raw or "").strip()
    if not t:
        return False
    line = t.split("\n")[0].strip()
    line = re.sub(r"<[^>]+>", "", line).strip()
    line = _strip_leading_at_mentions_for_confirm(line, mention_names)
    if not line:
        return False
    if P0_THREAD_CONFIRM_QUESTION_RE.match(line):
        return True
    if "?" in line and P0_THREAD_CONFIRM_QUESTION_RE.search(line):
        return True
    if P0_THREAD_CONFIRM_QUESTION_RE.search(line) and re.match(
        r"(?is)^\s*(?:can|could|shall|may|are\s+we|do\s+we|should\s+we|would\s+we)\b",
        line,
    ):
        return True
    return False


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


def _mirror_prompt_chat_situation_ok(
    message_chat_id: str,
    source_incident_chat_id: str,
    parent_id: str,
    root_id: str,
    pend: Dict[str, Any],
    mention_open_ids: List[str],
    asker_open_id: str,
) -> bool:
    """
    Same placement rules as legacy mirror confirm, but **without** judging reply text.

    True when the message is in the **prompt / overview** chat (not source incident id),
    and either in a **thread** or allowed **toplevel** yes (grace / @asker) per config.
    """
    if (message_chat_id or "").strip() == (source_incident_chat_id or "").strip():
        return False
    p, r = (parent_id or "").strip(), (root_id or "").strip()
    if p or r:
        return True
    if not get_p0_thread_confirm_allow_toplevel_yes():
        return False
    return _toplevel_yes_context_ok(pend, asker_open_id, mention_open_ids)


def _p0_thread_reply_affirms(
    pend: Dict[str, Any],
    text_raw: str,
    mention_names: Optional[List[str]],
) -> Tuple[bool, str]:
    """Returns (affirms, how) where how is regex | groq | no_match."""
    if _is_p0_thread_confirm_yes(text_raw, mention_names):
        return True, "regex"
    if _p0_thread_reply_looks_like_p0_question_not_answer(text_raw, mention_names):
        log.info(
            "P0 thread confirm: reply looks like a P0 question (not an approval) — skipping confirm/Groq"
        )
        return False, "no_match"
    if not get_p0_thread_confirm_use_groq():
        return False, "no_match"
    q = str(pend.get("question_text") or "").strip()
    if not q:
        log.info("P0 thread confirm: Groq skipped (no question_text on pending arm)")
        return False, "no_match"
    g = groq_thread_confirm_affirms_p0(q, text_raw)
    if g is True:
        return True, "groq"
    if g is False:
        log.info("P0 thread confirm: Groq classified reply as not affirming P0")
    else:
        log.warning("P0 thread confirm: Groq uncertain or failed parse — not starting P0")
    return False, "no_match"


def _try_handle_p0_thread_confirm(
    chat_id: str,
    user_id: str,
    text_raw: str,
    message_id: str,
    parent_id: str,
    root_id: str,
    token: str,
    source_chat_name: str,
    sender_lark_user_id: str,
    mention_open_ids: Optional[List[str]] = None,
    mention_names: Optional[List[str]] = None,
) -> bool:
    """
    Returns True if this message was fully handled (armed pending or started P0).
    """
    askers = get_p0_thread_confirm_asker_open_ids()
    targets = get_p0_thread_confirm_target_open_ids()
    if not askers and not targets:
        return False

    _p0_thread_prune_expired(chat_id)
    oid = (user_id or "").strip()
    mention_oids = {x.strip() for x in (mention_open_ids or []) if (x or "").strip()}

    with _P0_THREAD_LOCK:
        pend = _P0_THREAD_PENDING.get(chat_id)

    if pend:
        source_incident = str(pend.get("source_incident_chat_id") or "").strip() or chat_id
        qmid = str(pend.get("question_message_id") or "").strip()
        asker = str(pend.get("asker_open_id") or "").strip()
        p = (parent_id or "").strip()
        r = (root_id or "").strip()
        in_source = chat_id == source_incident
        thread_ok = bool(
            in_source and qmid and _thread_reply_targets_question(parent_id, root_id, qmid)
        )
        toplevel_raw = bool(
            in_source
            and qmid
            and get_p0_thread_confirm_allow_toplevel_yes()
            and not p
            and not r
        )
        toplevel_ok = bool(
            toplevel_raw
            and _toplevel_yes_context_ok(pend, asker, list(mention_open_ids or []))
        )
        mirror_situation = _mirror_prompt_chat_situation_ok(
            chat_id,
            source_incident,
            parent_id,
            root_id,
            pend,
            list(mention_open_ids or []),
            asker,
        )
        if (
            in_source
            and toplevel_raw
            and not thread_ok
            and not toplevel_ok
            and _is_p0_thread_confirm_yes(text_raw, mention_names)
        ):
            log.info(
                "Incident group: P0 thread toplevel yes ignored (outside grace or @asker not in mentions) "
                "chat_id=%s grace_sec=%s",
                chat_id,
                get_p0_thread_confirm_toplevel_grace_sec(),
            )
        if thread_ok or toplevel_ok or mirror_situation:
            responders = get_p0_thread_confirm_responder_open_ids()
            if oid == asker and not get_p0_thread_confirm_allow_asker_self_yes():
                log.info(
                    "Incident group: P0 thread confirm ignored (asker replied to own question) chat_id=%s",
                    chat_id,
                )
                return True
            if responders and oid not in responders:
                log.info(
                    "Incident group: P0 thread confirm ignored (responder not in allowlist) chat_id=%s",
                    chat_id,
                )
                return False
            affirms, affirm_how = _p0_thread_reply_affirms(pend, text_raw, mention_names)
            if not affirms:
                # Fully handled: do not fall through to ``\bp0\b`` keyword (e.g. "should we declare it as p0").
                log.info(
                    "Incident group: P0 thread confirm reply did not affirm — ignoring keyword for this message "
                    "chat_id=%s",
                    chat_id,
                )
                return True
            _p0_thread_clear_pending_dict(pend)
            if chat_has_active_session(source_incident):
                log.info(
                    "Incident group: P0 thread confirm skipped (session already active) source_chat=%s",
                    source_incident,
                )
                return True
            if mirror_situation:
                mode = "prompt/mirror chat (INCIDENT_OVERVIEW_TARGET_MAP)"
            elif thread_ok:
                mode = "thread reply"
            else:
                mode = "toplevel yes (P0_THREAD_CONFIRM_ALLOW_TOPLEVEL_YES=1)"
            if affirm_how == "groq":
                mode = f"{mode} groq_classify"
            log.info(
                "Incident group: P0 thread confirm — starting P0 source_chat=%s message_chat=%s confirmer=%s via %s",
                source_incident,
                chat_id,
                oid,
                mode,
            )
            start_p0(
                source_incident,
                token,
                oid,
                priority="P0",
                source_chat_name=source_chat_name if in_source else "",
                trigger_lark_user_id=sender_lark_user_id,
            )
            return True

    if _is_p0_thread_confirm_question(text_raw):
        arm_via_asker = bool(askers and oid in askers)
        arm_via_target = bool(targets and (mention_oids & targets))
        if arm_via_asker or arm_via_target:
            mid = (message_id or "").strip()
            if not mid:
                return False
            ttl = float(get_p0_thread_confirm_ttl_sec())
            tgt = get_overview_target_chat_id_for_source_incident(chat_id)
            entry = {
                "question_message_id": mid,
                "asker_open_id": oid,
                "exp": time.time() + ttl,
                "armed_at": time.time(),
                "source_incident_chat_id": chat_id,
                "question_text": (text_raw or "").strip()[:8000],
            }
            with _P0_THREAD_LOCK:
                _P0_THREAD_PENDING[chat_id] = entry
                if tgt and tgt != chat_id:
                    _P0_THREAD_PENDING[tgt] = entry
            mode = []
            if arm_via_asker:
                mode.append("designated_asker")
            if arm_via_target:
                mode.append("target_mention")
            log.info(
                "Incident group: P0 thread confirm armed asker=%s question_message_id=%s chat_id=%s ttl_sec=%s "
                "mirror_prompt_chat=%s mode=%s",
                oid[-12:] if oid else "",
                mid,
                chat_id,
                int(ttl),
                tgt if tgt and tgt != chat_id else "",
                "+".join(mode) if mode else "",
            )
            return True

    return False


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

    message_type = str(kwargs.get("message_type") or "").strip().lower()
    message_id = str(kwargs.get("message_id") or "").strip()
    parent_id = str(kwargs.get("parent_id") or "").strip()
    root_id = str(kwargs.get("root_id") or "").strip()
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
    # INCIDENT GROUP
    # ---------------------------------------------------------
    if chat_id in get_incident_group_chat_ids():
        if not text_raw:
            return

        # All bot prompts/warnings/replies in the incident-group flow should land in the prompt
        # / target chat (e.g. emergency-test group), not in the production source chat. Resolves
        # via INCIDENT_OVERVIEW_TARGET_MAP / OVERVIEW_TARGET_GROUP_CHAT_ID; falls back to source
        # if no target is configured.
        notify_chat = (
            get_overview_target_chat_id_for_source_incident(chat_id) or chat_id
        )

        # Typed P1 prompt reply (before cancel so "no" does not collide with other routes)
        pend = get_p1_prompt_pending(chat_id)
        if pend:
            nonce = str(pend.get("nonce") or "").strip()
            if _matches_p1_pending_create_reply(text_raw, mention_names):
                err = handle_p1_meeting_confirm_yes(chat_id, token, user_id, nonce)
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
                err = handle_p1_meeting_confirm_no(chat_id, token, nonce)
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

        if _try_handle_p0_thread_confirm(
            chat_id,
            user_id,
            text_raw,
            message_id,
            parent_id,
            root_id,
            token,
            source_chat_name,
            sender_lark_user_id,
            mention_open_ids=kwargs.get("mention_open_ids"),
            mention_names=mention_names,
        ):
            return

        # Cancel command (optional reason after the keyword phrase)
        cancel_m = CANCEL_WITH_OPTIONAL_REASON_RE.match(text_raw)
        if cancel_m:
            tail = (cancel_m.group(2) or "").strip()
            cancel_reason = tail if tail else "Unspecified"
            if chat_has_active_session(chat_id):
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
                if token:
                    st, body, _ = post_card_to_chat(
                        notify_chat, token, build_no_active_p0_session_card("cancel")
                    )
                    if st != 200:
                        log.warning("no-session cancel prompt card failed HTTP=%s body=%s", st, (body or "")[:300])
            return

        # Cooldown reset only — no new VC. / 只清冷却，不创建会议
        if COOLDOWN_RESET_RE.match(text_raw.strip()):
            clear_p0_cooldown(chat_id)
            if token:
                post_text_to_chat(
                    notify_chat,
                    token,
                    "ℹ️ Cooldown cleared for this group. The next **p0** or **p1** declaration in this chat will no longer be blocked by cooldown.",
                )
            return

        # Trigger P0 if ``p0`` / ``priority 0`` appears anywhere (unless pasted invite footer).
        if (not _is_pasted_meeting_invite_footer(text_raw)) and P0_KEYWORD_RE.search(text_raw):
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
            if _is_question_about_priority(text_raw):
                log.info(
                    "Incident group: P0 trigger ignored (question about priority) text=%r",
                    text_raw[:200],
                )
                return
            if (user_id or "").strip() in get_p0_trigger_ignore_open_ids():
                log.info("Incident group: P0 trigger ignored (P0_TRIGGER_IGNORE_OPEN_IDS) user_id=%s", user_id)
                return
            if chat_has_active_session(chat_id):
                log.info("Incident group: session already active chat_id=%s", chat_id)
                return

            log.info("Incident group: starting P0 session chat_id=%s user_id=%s text=%r", chat_id, user_id, text_raw[:200])
            # Do NOT pass silent_when_blocked=True here. The template detector
            # (_is_manual_p0_incident_overview_template, layers 1 + 2 above) already
            # filters out manual overview re-pastes BEFORE we get here, so any keyword
            # match that survives is a legitimate trigger attempt — the user expects to
            # see a cooldown / "session active" warning in the prompt/target chat when
            # blocked, not a silent no-op.
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
        if (not _is_pasted_meeting_invite_footer(text_raw)) and P1_KEYWORD_RE.search(text_raw):
            if _is_question_about_priority(text_raw):
                log.info(
                    "Incident group: P1 trigger ignored (question about priority) text=%r",
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
