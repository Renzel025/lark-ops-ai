"""
On-demand Grafana screenshot requests in Lark chat (typed message).
"""
from __future__ import annotations

import logging
import re
from typing import Optional, Tuple

from . import config as _config
from . import lark_client as _lark

log = logging.getLogger("lark-ops-ai")

# Must mention screenshot / graph / grafana; range parsed from the same line.
_SCREENSHOT_INTENT_RE = re.compile(
    r"\b(screenshot|screen\s*shot|graph\s*snap|grafana\s*snap|grafana\s*screenshot)\b",
    re.IGNORECASE,
)

_RANGE_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:under|last|past|for)?\s*(6\s*h(?:ours?|rs?)?|six\s*hours?)\b", re.I), "6h"),
    (re.compile(r"\b(?:under|last|past|for)?\s*(3\s*h(?:ours?|rs?)?|three\s*hours?)\b", re.I), "3h"),
    (re.compile(r"\b(?:under|last|past|for)?\s*(1\s*h(?:our|r)?|one\s*hour)\b", re.I), "1h"),
    (re.compile(r"\b(?:under|last|past|for)?\s*(30\s*m(?:in(?:ute)?s?)?|half\s*(?:an?\s*)?hour)\b", re.I), "30m"),
)


def parse_graph_screenshot_on_demand_range(text: str) -> Optional[str]:
    """
    Return ``6h`` / ``3h`` / ``1h`` / ``30m`` when the message asks for a Grafana screenshot
    with that time window. ``None`` if not a screenshot request or range unclear.
    """
    raw = (text or "").strip()
    if not raw or not _SCREENSHOT_INTENT_RE.search(raw):
        return None
    for pat, key in _RANGE_PATTERNS:
        if pat.search(raw):
            return key
    return None


def _chat_allows_on_demand(chat_id: str) -> bool:
    cid = (chat_id or "").strip()
    if not cid:
        return False
    allowed = _config.get_p0_graph_screenshot_on_demand_chat_ids()
    if allowed:
        return cid in allowed
    target = _config.get_p0_graph_screenshot_target_chat_id()
    if target and cid == target:
        return True
    return cid in _config.get_incident_group_chat_ids()


def try_handle_graph_screenshot_request(
    text: str,
    chat_id: str,
    tenant_token: str,
    source_chat_label: str = "",
) -> bool:
    """
    If ``text`` requests an on-demand Grafana screenshot, start capture and return True.
    Posts to ``chat_id`` (where the request was sent). ``30m`` is on-demand only (not P0 auto).
    """
    if not _config.p0_graph_screenshot_on_demand_enabled():
        return False
    if not _config.p0_graph_screenshot_enabled():
        return False
    if not _config.get_p0_graph_screenshot_url():
        return False
    range_key = parse_graph_screenshot_on_demand_range(text)
    if not range_key:
        return False
    cid = (chat_id or "").strip()
    tok = (tenant_token or "").strip()
    if not cid or not tok:
        return False
    if not _chat_allows_on_demand(cid):
        log.info(
            "graph screenshot on-demand: ignored (chat not allowed) chat_id_tail=%s range=%s",
            cid[-12:] if len(cid) > 12 else cid,
            range_key,
        )
        return False

    from .graph_screenshot import schedule_on_demand_graph_screenshot

    label = (source_chat_label or "").strip()
    schedule_on_demand_graph_screenshot(tok, cid, range_key, label, post_chat_id=cid)
    log.info(
        "graph screenshot on-demand: queued range=%s chat_id_tail=%s",
        range_key,
        cid[-12:] if len(cid) > 12 else cid,
    )
    return True
