"""
On-demand Grafana screenshot requests in Lark chat — natural language + optional Groq AI.

Understands OTE-AI style messages, e.g. ``@bot please give 30 mins``, ``send 1hr graph``,
``screenshot last 3 hours``.
"""
from __future__ import annotations

import logging
import re
from typing import List, Optional, Tuple

from p0_logic import config as _config
from p0_logic import lark_client as _lark

log = logging.getLogger("lark-ops-ai")

# Explicit screenshot / Grafana wording.
_SCREENSHOT_INTENT_RE = re.compile(
    r"\b(screenshot|screen\s*shot|graph\s*snap|grafana\s*snap|grafana\s*screenshot|grafana|dashboard|metrics)\b",
    re.IGNORECASE,
)

# Natural “please give / send / show …” without requiring the word screenshot.
_NATURAL_REQUEST_VERB_RE = re.compile(
    r"(?i)(?:\b(?:please|pls|kindly)\s+)?(?:can\s+you\s+)?"
    r"\b(?:give|send|show|share|post|get|pull|grab|need|want|provide)\b"
    r"(?:\s+(?:me|us))?(?:\s+(?:the|a))?\s+"
)

# ``apm metrics``, ``core metrics``, etc. — strong ops intent without the word screenshot.
_OPS_METRICS_CUE_RE = re.compile(
    r"\b(apm(?:\s+metrics?)?|core\s*metrics?|grafana|dashboard|graph|metrics)\b",
    re.IGNORECASE,
)

_RANGE_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:under|last|past|for)?\s*(6\s*h(?:ours?|rs?)?|six\s*hours?)\b", re.I), "6h"),
    (re.compile(r"\b(?:under|last|past|for)?\s*(3\s*h(?:ours?|rs?)?|three\s*hours?)\b", re.I), "3h"),
    (re.compile(r"\b(?:under|last|past|for)?\s*(2\s*h(?:ours?|rs?)?|two\s*hours?)\b", re.I), "2h"),
    (re.compile(r"\b(?:under|last|past|for)?\s*(1\s*h(?:our|r)?|one\s*hour)\b", re.I), "1h"),
    (re.compile(r"\b(?:under|last|past|for)?\s*(30\s*m(?:in(?:ute)?s?)?|half\s*(?:an?\s*)?hour)\b", re.I), "30m"),
)

_VALID_RANGE_KEYS = frozenset({"30m", "1h", "2h", "3h", "6h"})


def _strip_leading_mentions(text: str, mention_names: Optional[List[str]] = None) -> str:
    line = (text or "").strip()
    for name in mention_names or []:
        n = (name or "").strip()
        if not n:
            continue
        line = re.sub(rf"(?i)@{re.escape(n)}\b", " ", line)
    line = re.sub(r"@\S+", " ", line)
    return re.sub(r"\s+", " ", line).strip()


def parse_time_range_key(text: str) -> Optional[str]:
    """Return ``30m`` / ``1h`` / ``2h`` / ``3h`` / ``6h`` when a time window appears in ``text``."""
    raw = (text or "").strip()
    if not raw:
        return None
    for pat, key in _RANGE_PATTERNS:
        if pat.search(raw):
            return key
    return None


def has_explicit_graph_screenshot_intent(text: str) -> bool:
    """Screenshot / Grafana / dashboard wording (strict keyword path)."""
    raw = (text or "").strip()
    return bool(raw) and bool(
        re.search(
            r"\b(screenshot|screen\s*shot|graph\s*snap|grafana\s*snap|grafana\s*screenshot)\b",
            raw,
            re.I,
        )
    )


def _has_ops_metrics_cue(text: str) -> bool:
    return bool(_OPS_METRICS_CUE_RE.search((text or "").strip()))


def has_natural_graph_request_cue(text: str) -> bool:
    """``please give …`` / ``send me …`` / ``apm metrics`` style ops requests."""
    raw = (text or "").strip()
    if not raw:
        return False
    if has_explicit_graph_screenshot_intent(raw):
        return True
    if _NATURAL_REQUEST_VERB_RE.search(raw):
        return True
    if _has_ops_metrics_cue(raw):
        return True
    return bool(_SCREENSHOT_INTENT_RE.search(raw))


def has_graph_screenshot_intent(text: str) -> bool:
    """Backward-compatible: any path that could be a screenshot request."""
    return has_natural_graph_request_cue(text) and (
        parse_time_range_key(text) is not None or has_explicit_graph_screenshot_intent(text)
    )


def _mentions_our_bot(mention_names: Optional[List[str]]) -> bool:
    names = [n.strip() for n in (mention_names or []) if (n or "").strip()]
    if not names:
        return False
    hints = _config.get_p0_graph_screenshot_bot_mention_hints()
    for name in names:
        nl = name.lower()
        for h in hints:
            if h.lower() in nl:
                return True
    return False


def _chat_has_p0_context(chat_id: str) -> bool:
    cid = (chat_id or "").strip()
    if not cid:
        return False
    try:
        from features.session.session import chat_has_active_session
        from .graph_screenshot import _has_active_p0_session

        if chat_has_active_session(cid):
            return True
        return _has_active_p0_session()
    except Exception:
        return False


def _ai_classify_screenshot(text: str) -> Optional[str]:
    if not _config.get_p0_graph_screenshot_ai_enabled():
        return None
    try:
        from .graph_screenshot_ai import classify_graph_screenshot_request, resolve_graph_screenshot_ai_provider

        if not resolve_graph_screenshot_ai_provider():
            return None
        result = classify_graph_screenshot_request(text)
        if not result:
            return None
        log.info(
            "graph screenshot AI: intent=%s range=%s reason=%r text_head=%r",
            result.get("intent"),
            result.get("range"),
            result.get("reason"),
            (text or "")[:200],
        )
        if result.get("intent") != "request_screenshot":
            return None
        rk = str(result.get("range") or "").strip().lower()
        return rk if rk in _VALID_RANGE_KEYS else None
    except Exception as e:
        log.warning("graph screenshot AI classify failed: %s", e)
        return None


def resolve_graph_screenshot_range_key(
    text: str,
    *,
    chat_id: str = "",
    mention_names: Optional[List[str]] = None,
    groq_key: str = "",
) -> Optional[str]:
    """
    Resolve on-demand Grafana time range from natural or explicit phrasing.
    Returns ``None`` when not a screenshot request or range unclear.
    """
    raw = _strip_leading_mentions(text, mention_names)
    if not raw:
        return None

    time_key = parse_time_range_key(raw)
    mentions_bot = _mentions_our_bot(mention_names)
    p0_ctx = _chat_has_p0_context(chat_id)

    # Explicit: ``screenshot 30 min`` / ``grafana screenshot 3h``
    if has_explicit_graph_screenshot_intent(raw):
        if time_key:
            return time_key
        ai_rk = _ai_classify_screenshot(raw)
        return ai_rk

    # Natural: ``please give 30 mins``, ``can you send 30 mins apm metrics``, etc.
    if time_key and has_natural_graph_request_cue(raw):
        if mentions_bot or p0_ctx or _has_ops_metrics_cue(raw):
            return time_key
        cid = (chat_id or "").strip()
        if cid and _chat_allows_on_demand(cid) and _NATURAL_REQUEST_VERB_RE.search(raw):
            return time_key
        ai_rk = _ai_classify_screenshot(raw)
        if ai_rk:
            return ai_rk
        return None

    # @bot with only a time: ``@P0-bot 1hr``
    if mentions_bot and time_key and len(raw) <= 120:
        return time_key

    # AI path for ambiguous short ops asks during P0.
    if p0_ctx or mentions_bot:
        return _ai_classify_screenshot(raw)

    return None


def parse_graph_screenshot_on_demand_range(text: str) -> Optional[str]:
    """Legacy entry: explicit screenshot keywords + time range only."""
    raw = (text or "").strip()
    if not has_explicit_graph_screenshot_intent(raw):
        return None
    return parse_time_range_key(raw)


def _post_on_demand_reply(chat_id: str, token: str, text: str) -> None:
    cid = (chat_id or "").strip()
    tok = (token or "").strip()
    if not cid or not tok:
        return
    st, _ = _lark.post_text_to_chat(cid, tok, text)
    if st != 200:
        log.warning("graph screenshot on-demand: reply failed HTTP=%s", st)


def _chat_allows_on_demand(chat_id: str) -> bool:
    """
    On-demand Grafana chat replies/capture only in the screenshot hub — not every incident group.

    Allowlist order:
    1. ``P0_GRAPH_SCREENSHOT_ON_DEMAND_CHAT_IDS`` when set
    2. Else ``P0_GRAPH_SCREENSHOT_TARGET_CHAT_ID`` only
    """
    cid = (chat_id or "").strip()
    if not cid:
        return False
    allowed = _config.get_p0_graph_screenshot_on_demand_chat_ids()
    if allowed:
        return cid in allowed
    target = _config.get_p0_graph_screenshot_target_chat_id()
    return bool(target) and cid == target


def _react_to_request_message(token: str, message_id: str, emoji_type: str) -> None:
    if not _config.get_p0_graph_screenshot_react_enabled():
        return
    mid = (message_id or "").strip()
    tok = (token or "").strip()
    et = (emoji_type or "").strip()
    if not mid or not tok or not et:
        return
    st, _ = _lark.add_message_reaction(mid, tok, et)
    if st == 200:
        log.info("graph screenshot on-demand: reaction %s on request msg tail=%s", et, mid[-12:])


def try_handle_graph_screenshot_request(
    text: str,
    chat_id: str,
    tenant_token: str,
    source_chat_label: str = "",
    *,
    mention_names: Optional[List[str]] = None,
    groq_key: str = "",
    message_id: str = "",
) -> bool:
    """
    If ``text`` requests an on-demand Grafana screenshot, start capture and return True.
    Posts to ``chat_id`` (where the request was sent). ``30m`` is on-demand only (not P0 auto).
    """
    raw = (text or "").strip()
    if not raw:
        return False

    cid = (chat_id or "").strip()
    tok = (tenant_token or "").strip()
    if not cid or not tok:
        return False

    if not _chat_allows_on_demand(cid):
        log.info(
            "graph screenshot on-demand: ignored (not screenshot hub) chat_id_tail=%s text_head=%r",
            cid[-12:] if len(cid) > 12 else cid,
            raw[:80],
        )
        return False

    range_key = resolve_graph_screenshot_range_key(
        raw,
        chat_id=cid,
        mention_names=mention_names,
        groq_key=groq_key,
    )

    wants_screenshot = (
        has_explicit_graph_screenshot_intent(raw)
        or has_natural_graph_request_cue(raw)
        and parse_time_range_key(_strip_leading_mentions(raw, mention_names)) is not None
        or _mentions_our_bot(mention_names)
    )

    if not range_key:
        if has_explicit_graph_screenshot_intent(raw) or (
            wants_screenshot and _mentions_our_bot(mention_names)
        ):
            _post_on_demand_reply(
                cid,
                tok,
                "📊 Got it — which time range? **30 min**, **1h**, **2h**, **3h**, or **6h** "
                "(e.g. `please give 30 mins` or `screenshot 3 hours`).",
            )
            return True
        return False

    if not _config.p0_graph_screenshot_on_demand_enabled():
        _post_on_demand_reply(
            cid,
            tok,
            "📊 On-demand Grafana screenshots are disabled on this bot "
            "(set `P0_GRAPH_SCREENSHOT_ON_DEMAND=1` in `.env` and restart).",
        )
        return True
    if not _config.p0_graph_screenshot_enabled():
        _post_on_demand_reply(
            cid,
            tok,
            "📊 Grafana screenshots are disabled "
            "(set `P0_GRAPH_SCREENSHOT_ENABLED=1` in `.env` and restart).",
        )
        return True
    if not _config.get_p0_graph_screenshot_url():
        _post_on_demand_reply(
            cid,
            tok,
            "📊 Grafana screenshot URL is not configured (`P0_GRAPH_SCREENSHOT_URL` missing in `.env`).",
        )
        return True

    if not _config.build_p0_graph_screenshot_url_for_range(range_key):
        _post_on_demand_reply(
            cid,
            tok,
            f"📊 Grafana URL is missing or invalid for range `{range_key}` (`P0_GRAPH_SCREENSHOT_URL`).",
        )
        return True

    from .graph_screenshot import schedule_on_demand_graph_screenshot

    label = (source_chat_label or "").strip()
    range_disp = _config.get_p0_graph_screenshot_range_display(range_key)
    _post_on_demand_reply(
        cid,
        tok,
        f"📊 On it — capturing Grafana dashboard (last {range_disp}). Please wait.",
    )
    _react_to_request_message(tok, message_id, _config.get_p0_graph_screenshot_react_queued_emoji())
    try:
        outcome = schedule_on_demand_graph_screenshot(
            tok,
            cid,
            range_key,
            label,
            trigger_message_id=message_id,
        )
    except Exception as e:
        log.warning("graph screenshot on-demand: schedule failed: %s", e, exc_info=True)
        _post_on_demand_reply(
            cid,
            tok,
            f"📊 Could not start Grafana capture ({e}). Check `journalctl -u lark-ops-ai`.",
        )
        return True
    if outcome == "skipped":
        _post_on_demand_reply(
            cid,
            tok,
            "📊 Could not start Grafana capture (missing token, chat, or URL). Check server logs.",
        )
        return True
    log.info(
        "graph screenshot on-demand: queued range=%s chat_id_tail=%s",
        range_key,
        cid[-12:] if len(cid) > 12 else cid,
    )
    return True
