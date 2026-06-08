"""
AI classification for natural-language Grafana screenshot requests (Claude or Groq).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from . import config as _config
from .groq_client import _parse_json_object, groq_chat_once

log = logging.getLogger("lark-ops-ai")

_GRAPH_SCREENSHOT_REQUEST_SYSTEM = (
    "You triage Lark incident / ops group chat for an on-call bot.\n"
    "Decide if the speaker wants a Grafana dashboard screenshot posted for a time window.\n\n"
    "Output ONLY valid JSON:\n"
    '{"intent":"request_screenshot"|"other","range":"30m"|"1h"|"2h"|"3h"|"6h"|null,"reason":"one short sentence"}\n\n'
    "request_screenshot — wants metrics/graph capture now, e.g.:\n"
    '- "please give 30 mins", "@bot send 1hr", "screenshot last 3 hours", "grafana 6h"\n'
    "other — unrelated, or time is not about Grafana (e.g. give me 30 mins to check, "
    "see you in 1 hour, status chat without asking for dashboard).\n\n"
    "Map ranges: 30 min → 30m, 1 hour → 1h, 2 hours → 2h, 3 hours → 3h, 6 hours → 6h.\n"
    "If intent is request_screenshot but range unclear, set range to null."
)

_ALLOWED_RANGES = frozenset({"30m", "1h", "2h", "3h", "6h"})


def resolve_graph_screenshot_ai_provider() -> str:
    """
    ``P0_GRAPH_SCREENSHOT_AI_PROVIDER``: ``claude`` | ``groq`` | ``auto`` (default).
    ``auto`` prefers Claude when ``ANTHROPIC_API_KEY`` is set, else Groq.
    """
    _config.reload_env_runtime()
    raw = (os.getenv("P0_GRAPH_SCREENSHOT_AI_PROVIDER") or "auto").strip().lower()
    if raw == "claude":
        return "claude" if _anthropic_key() else ""
    if raw == "groq":
        return "groq" if _groq_key() else ""
    if _anthropic_key():
        return "claude"
    if _groq_key():
        return "groq"
    return ""


def _anthropic_key() -> str:
    _config.reload_env_runtime()
    return (os.getenv("ANTHROPIC_API_KEY") or "").strip()


def _groq_key() -> str:
    _config.reload_env_runtime()
    return (os.getenv("GROQ_API_KEY") or "").strip()


def _parse_graph_screenshot_classification(raw: str, provider: str) -> Optional[dict]:
    obj = _parse_json_object(raw or "")
    if not obj:
        log.warning("classify_graph_screenshot (%s): JSON parse failed head=%s", provider, (raw or "")[:200])
        return None
    intent = str(obj.get("intent") or "").strip().lower()
    if intent not in ("request_screenshot", "other"):
        log.warning("classify_graph_screenshot (%s): unknown intent=%r", provider, intent)
        return None
    rk = str(obj.get("range") or "").strip().lower()
    if rk in ("", "null", "none"):
        rk = ""
    if rk and rk not in _ALLOWED_RANGES:
        log.warning("classify_graph_screenshot (%s): unknown range=%r", provider, rk)
        rk = ""
    reason = str(obj.get("reason") or "").strip()[:300]
    out: dict = {"intent": intent, "reason": reason, "provider": provider}
    if rk:
        out["range"] = rk
    return out


def _classify_via_claude(message_text: str) -> Optional[dict]:
    from .anthropic_client import anthropic_chat_once

    t = (message_text or "").strip()[:4500]
    raw = anthropic_chat_once(_GRAPH_SCREENSHOT_REQUEST_SYSTEM, f"MESSAGE:\n{t}", max_tokens=180)
    return _parse_graph_screenshot_classification(raw, "claude")


def _classify_via_groq(message_text: str) -> Optional[dict]:
    t = (message_text or "").strip()[:4500]
    raw = groq_chat_once(_GRAPH_SCREENSHOT_REQUEST_SYSTEM, f"MESSAGE:\n{t}", max_tokens=120)
    return _parse_graph_screenshot_classification(raw, "groq")


def classify_graph_screenshot_request(message_text: str) -> Optional[dict]:
    """
    Natural-language Grafana screenshot request vs unrelated chat.
    Uses Claude when configured (default ``auto`` → Claude if ``ANTHROPIC_API_KEY`` set).
    """
    t = (message_text or "").strip()
    if not t:
        return None
    provider = resolve_graph_screenshot_ai_provider()
    if provider == "claude":
        return _classify_via_claude(t)
    if provider == "groq":
        return _classify_via_groq(t)
    return None
