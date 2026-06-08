"""
Claude + keyword classification for detection-group issue signals.
"""
from __future__ import annotations

import logging
import os
import re
from typing import List, Optional, Tuple

from . import config as _config
from .groq_client import _parse_json_object

log = logging.getLogger("lark-ops-ai")

_ISSUE_WATCH_SYSTEM = (
    "You triage Lark **detection / emergency feedback** group chat for an on-call ops bot.\n"
    "Decide if a message reports a **production or player-facing issue** that duty should know about.\n\n"
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
    "1 website_downtime — official site cannot be accessed, infinite loading, site down\n"
    "2 login_issues — site loads but login fails, credential errors, OTP send/validate failures\n"
    "3 registration_failures — new users cannot register\n"
    "4 withdrawal_issues — withdraw fails or unavailable\n"
    "5 deposit_issues — deposit fails or unavailable\n"
    "6 backend_downtime — FPMS or PMS backend unreachable or unusable\n"
    "7 gameplay_outage — all or most games unplayable / cannot enter\n"
    "8 widespread_impact — ONLY if this single message itself mentions 4+ distinct players/users "
    "reporting the same issue (rare; bot also counts across messages separately)\n\n"
    "Rules:\n"
    "- is_incident_signal=TRUE when OM/duty/staff OR players report a real symptom, e.g. "
    "\"kindly help check the CP website — continuously loading\", login OTP failing, FPMS down, "
    "withdrawal failing, all games cannot enter.\n"
    "- is_incident_signal=false for: pure greetings/thanks, jokes, meeting invites, "
    "declaring p0/p1 bridge, screenshot-only requests with no incident, status with NO problem.\n"
    "- Staff asking the team to investigate a website/login/backend/game issue = TRUE (high confidence).\n"
    "- issue_fingerprint: stable key (login_otp_failure, website_loading_cp, fpms_backend_down).\n"
    "- Multilingual input (English, Chinese, Tagalog) — classify by meaning.\n"
)

_KEYWORD_RULES: Tuple[Tuple[re.Pattern[str], List[str], str, float, str], ...] = (
    (
        re.compile(
            r"(?is)"
            r"(?:\b(?:website|web\s*site|site|cp\s*website|official\s+site)\b.{0,80}"
            r"(?:loading|load(?:ing)?\s+(?:forever|continuously|non-?stop)|"
            r"cannot\s+access|can't\s+access|not\s+(?:open|working|loading)|down|unreachable|"
            r"打不开|无法访问|一直加载|无限加载))"
            r"|"
            r"(?:\b(?:loading|load(?:ing)?\s+(?:forever|continuously))\b.{0,80}"
            r"\b(?:website|web\s*site|site|cp)\b)"
        ),
        ["website_downtime"],
        "website_loading",
        0.93,
        "Website continuously loading or inaccessible",
    ),
    (
        re.compile(
            r"(?is)"
            r"\b(?:cannot|can't|unable\s+to)\s+login\b|"
            r"\bplayers?\b.{0,100}(?:cannot|can't|unable\s+to)\s+login\b|"
            r"\blogin\b.{0,80}\b(?:on\s+)?(?:cp\s+)?(?:website|site)\b|"
            r"\blogin\s+(?:fail|error|issue|problem|broken)\b|"
            r"\botp\b.{0,40}\b(?:fail|not\s+received|invalid|error)\b|"
            r"无法登录|登录失败|验证码"
        ),
        ["login_issues"],
        "login_failure",
        0.9,
        "Players cannot login on CP website",
    ),
    (
        re.compile(r"(?is)\b(?:cannot|can't|unable\s+to)\s+register\b|注册失败|无法注册"),
        ["registration_failures"],
        "registration_failure",
        0.9,
        "Registration failure reported",
    ),
    (
        re.compile(
            r"(?is)"
            r"\b(?:cannot|can't|unable\s+to)\s+withdraw\b|"
            r"\bwithdraw(?:al)?\b.{0,60}\b(?:fail|error|issue|cannot|can't|balance|fund|money|problem)\b|"
            r"\b(\d+)\s+players?\b.{0,120}(?:cannot|can't|unable\s+to)\s+withdraw|"
            r"提款失败|无法提款|不能提款|无法提现"
        ),
        ["withdrawal_issues"],
        "withdrawal_failure",
        0.93,
        "Withdrawal issue reported",
    ),
    (
        re.compile(
            r"(?is)"
            r"\b(?:cannot|can't|unable\s+to)\s+deposit\b|"
            r"\bdeposit\b.{0,60}\b(?:fail|error|issue|cannot|can't|balance|fund|money|problem)\b|"
            r"存款失败|无法存款|充值失败"
        ),
        ["deposit_issues"],
        "deposit_failure",
        0.9,
        "Deposit issue reported",
    ),
    (
        re.compile(r"(?is)\b(?:fpms|pms)\b.{0,50}\b(?:down|unreachable|cannot|can't|not\s+working|offline)\b|后台.{0,20}(?:挂|不可用|进不去)"),
        ["backend_downtime"],
        "backend_downtime",
        0.92,
        "FPMS/PMS backend issue reported",
    ),
    (
        re.compile(
            r"(?is)\b(?:all|every)\s+games?\b.{0,40}\b(?:down|cannot|can't|unplayable|not\s+working)\b|"
            r"无法进入游戏|游戏.{0,10}(?:进不去|打不开|全部)"
        ),
        ["gameplay_outage"],
        "gameplay_outage",
        0.9,
        "Gameplay outage reported",
    ),
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


def extract_player_ids(text: str) -> List[str]:
    """
    Sorted unique player/account IDs from detection chat.

    - Default: 10-digit IDs.
    - ``Account:`` / follow-up lists often mix 7–10 digit IDs on separate lines.
    """
    t = (text or "").strip()
    if not t:
        return []
    ids_10 = set(re.findall(r"\b\d{10}\b", t))
    if ids_10 or re.search(r"(?i)\baccount\b", t):
        loose = set(re.findall(r"\b\d{7,10}\b", t))
        return sorted(loose, key=lambda x: (len(x), x))
    return sorted(ids_10)


def _extract_player_mentions(text: str) -> int:
    """``4 players`` in prose, ID list count, or plural ``players`` without a number."""
    t = (text or "").strip()
    ids = extract_player_ids(t)
    if ids:
        return len(ids)
    m = re.search(r"(?is)\b(\d+)\s+players?\b", t)
    if m:
        try:
            return max(0, int(m.group(1)))
        except ValueError:
            pass
    if re.search(r"(?is)\bplayers\b", t):
        return 0
    if re.search(r"(?is)\bplayer\b", t):
        return 1
    return 0


def _summary_with_players(
    base_summary: str,
    categories: List[str],
    players: int,
    text: str,
) -> str:
    if players >= 1:
        if "login_issues" in categories:
            if players == 1:
                return "1 player cannot login on CP website"
            return f"{players} players cannot login on CP website"
        if "withdrawal_issues" in categories:
            if players == 1:
                return "1 player cannot withdraw"
            return f"{players} players cannot withdraw"
        return f"{base_summary} ({players} player(s))"
    if re.search(r"(?is)\bplayers\b", text) and "login_issues" in categories:
        return "Players cannot login on CP website"
    return base_summary


def _keyword_classify(message_text: str) -> Optional[dict]:
    t = (message_text or "").strip()
    if not t:
        return None
    for pattern, categories, fingerprint, confidence, summary in _KEYWORD_RULES:
        if not pattern.search(t):
            continue
        players = _extract_player_mentions(t)
        player_ids = extract_player_ids(t)
        cats = list(categories)
        if players >= 4 and "widespread_impact" not in cats:
            cats.append("widespread_impact")
        summ = _summary_with_players(summary, cats, players, t)
        out = {
            "is_incident_signal": True,
            "categories": cats,
            "confidence": confidence,
            "summary": summ,
            "issue_fingerprint": fingerprint,
            "players_mentioned_in_message": players,
            "reason": "keyword rule match",
            "provider": "keyword",
        }
        if player_ids:
            out["player_ids"] = player_ids
        return out
    return None


def _classify_via_claude(message_text: str) -> Optional[dict]:
    from .anthropic_client import anthropic_chat_once

    raw = anthropic_chat_once(_ISSUE_WATCH_SYSTEM, f"MESSAGE:\n{message_text[:4500]}", max_tokens=280)
    return _parse_classification(raw, "claude")


def classify_issue_watch_message(message_text: str) -> Optional[dict]:
    """
    Keyword fast-path + Claude. Keyword wins when high-confidence; Claude refines otherwise.
    """
    t = (message_text or "").strip()
    if not t:
        return None
    kw = _keyword_classify(t)
    if kw and float(kw.get("confidence") or 0) >= 0.88:
        log.info(
            "issue_watch_ai: keyword match categories=%s fp=%s",
            kw.get("categories"),
            kw.get("issue_fingerprint"),
        )
        return kw
    provider = resolve_issue_watch_ai_provider()
    ai: Optional[dict] = None
    if provider == "claude":
        ai = _classify_via_claude(t)
    else:
        log.warning("issue_watch_ai: no ANTHROPIC_API_KEY — using keyword rules only")
    if ai and ai.get("is_incident_signal"):
        return ai
    if kw and kw.get("is_incident_signal"):
        return kw
    return ai if ai is not None else kw
