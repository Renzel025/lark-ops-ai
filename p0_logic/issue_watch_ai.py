"""
Claude classification for detection-group player issue signals.
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional

from . import config as _config
from .groq_client import _parse_json_object

log = logging.getLogger("lark-ops-ai")

_ISSUE_WATCH_SYSTEM = (
    "You triage Lark incident / player feedback group chat for an on-call ops bot.\n"
    "Decide if a message reports a **player-facing production issue** (not general chat, jokes, "
    "meeting invites, P0/P1 bridge declarations, or staff coordination).\n\n"
    "Output ONLY valid JSON:\n"
    "{\n"
    '  "is_incident_signal": true|false,\n'
    '  "categories": ["login_issues"],\n'
    '  "confidence": 0.0-1.0,\n'
    '  "summary": "one concise English sentence",\n'
    '  "issue_fingerprint": "short_snake_case_cluster_key",\n'
    '  "players_mentioned_in_message": 0,\n'
    '  "reason": "one short sentence"\n'
    "}\n\n"
    "Categories (use exact keys, one or more):\n"
    "1 website_downtime — official site cannot be accessed\n"
    "2 login_issues — site loads but login fails, credential errors, OTP send/validate failures\n"
    "3 registration_failures — new users cannot register\n"
    "4 withdrawal_issues — withdraw fails or unavailable\n"
    "5 deposit_issues — deposit fails or unavailable\n"
    "6 backend_downtime — FPMS or PMS backend unreachable or unusable\n"
    "7 gameplay_outage — all or most games unplayable / cannot enter\n"
    "8 widespread_impact — ONLY if this single message itself mentions 4+ distinct players/users "
    "reporting the same issue (rare; bot also counts across messages separately)\n\n"
    "Rules:\n"
    "- is_incident_signal=false for: greetings, thanks, staff-only notes, questions without a problem, "
    "status updates with no user impact, screenshot requests, declaring p0/p1 meetings.\n"
    "- issue_fingerprint: stable key for clustering same root cause (e.g. login_otp_failure, "
    "website_down_main, fpms_backend_down).\n"
    "- players_mentioned_in_message: count distinct players/users explicitly mentioned in THIS message.\n"
    "- confidence: how sure this is a real player-facing incident signal (not noise).\n"
    "- Multilingual input (English, Chinese, Tagalog) — classify by meaning.\n"
)

_ALLOWED_CATEGORIES = frozenset(
    {
        "website_downtime",
        "login_issues",
        "registration_failures",
        "withdrawal_issues",
        "deposit_issues",
        "backend_downtime",
        "gameplay_outage",
        "widespread_impact",
    }
)


def _anthropic_key() -> str:
    _config.reload_env_runtime()
    return (os.getenv("ANTHROPIC_API_KEY") or "").strip()


def resolve_issue_watch_ai_provider() -> str:
    """
    ``P0_ISSUE_WATCH_AI_PROVIDER``: ``claude`` (default) | ``auto``.
    """
    _config.reload_env_runtime()
    raw = (os.getenv("P0_ISSUE_WATCH_AI_PROVIDER") or "claude").strip().lower()
    if raw == "claude":
        return "claude" if _anthropic_key() else ""
    if raw == "auto":
        return "claude" if _anthropic_key() else ""
    return "claude" if _anthropic_key() else ""


def _norm_confidence(raw: object) -> float:
    try:
        c = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if c > 1.0:
        c = c / 100.0
    return max(0.0, min(1.0, c))


def _norm_categories(raw: object) -> List[str]:
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for item in raw:
        key = str(item or "").strip().lower()
        if key in _ALLOWED_CATEGORIES and key not in out:
            out.append(key)
    return out


def _parse_classification(raw: str, provider: str) -> Optional[dict]:
    obj = _parse_json_object(raw or "")
    if not obj:
        log.warning("classify_issue_watch (%s): JSON parse failed head=%s", provider, (raw or "")[:200])
        return None
    signal = bool(obj.get("is_incident_signal"))
    categories = _norm_categories(obj.get("categories"))
    confidence = _norm_confidence(obj.get("confidence"))
    summary = str(obj.get("summary") or "").strip()[:400]
    fingerprint = str(obj.get("issue_fingerprint") or "").strip().lower()[:80]
    fingerprint = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in fingerprint).strip("_")
    try:
        players = int(obj.get("players_mentioned_in_message") or 0)
    except (TypeError, ValueError):
        players = 0
    players = max(0, min(players, 50))
    reason = str(obj.get("reason") or "").strip()[:300]
    if signal and not categories:
        log.warning("classify_issue_watch (%s): signal=true but no categories", provider)
        return None
    if signal and not fingerprint:
        fingerprint = categories[0] if categories else "unknown_issue"
    return {
        "is_incident_signal": signal,
        "categories": categories,
        "confidence": confidence,
        "summary": summary,
        "issue_fingerprint": fingerprint,
        "players_mentioned_in_message": players,
        "reason": reason,
        "provider": provider,
    }


def classify_issue_watch_message(message_text: str) -> Optional[dict]:
    """
    Claude classifies a detection-group message. Returns None when AI unavailable or parse fails.
    """
    t = (message_text or "").strip()
    if not t:
        return None
    provider = resolve_issue_watch_ai_provider()
    if provider != "claude":
        log.warning("issue_watch_ai: no ANTHROPIC_API_KEY — classification skipped")
        return None
    from .anthropic_client import anthropic_chat_once

    raw = anthropic_chat_once(_ISSUE_WATCH_SYSTEM, f"MESSAGE:\n{t[:4500]}", max_tokens=280)
    return _parse_classification(raw, "claude")
