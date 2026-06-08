"""
Detection-group issue watch — Claude classification + DM alerts to overview duty recipients.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

from . import cards as _cards
from . import config as _config
from . import lark_client as _lark
from .issue_watch_ai import classify_issue_watch_message, extract_player_ids

log = logging.getLogger("lark-ops-ai")

_CATEGORY_LABELS: Dict[str, Tuple[str, int]] = {
    "website_downtime": ("Website Downtime", 1),
    "login_issues": ("Login Issues", 2),
    "registration_failures": ("Registration Failures", 3),
    "withdrawal_issues": ("Withdrawal Issues", 4),
    "deposit_issues": ("Deposit Issues", 5),
    "backend_downtime": ("Backend Downtime (FPMS/PMS)", 6),
    "gameplay_outage": ("Gameplay Outage", 7),
    "widespread_impact": ("Widespread Impact (4+ players)", 8),
}

_STORE_LOCK = threading.Lock()
_REPORTS: List[Dict[str, object]] = []
_COOLDOWN: Dict[str, float] = {}


def _now_ts() -> float:
    return time.time()


def _format_alert_time() -> str:
    """Server-local clock (ose-bot is MYT); no zoneinfo import (Python 3.8 safe)."""
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _is_player_id_list_message(text: str) -> bool:
    """Follow-up bubble that is mostly 10-digit player IDs."""
    t = (text or "").strip()
    ids = extract_player_ids(t)
    if len(ids) < 1:
        return False
    remainder = re.sub(r"\b\d{10}\b", " ", t)
    remainder = re.sub(r"[\s,;]+", "", remainder)
    return len(remainder) < 12


def _should_skip_noise(text: str) -> bool:
    t = (text or "").strip()
    if _is_player_id_list_message(t):
        return False
    if len(t) < _config.get_p0_issue_watch_min_text_len():
        return True
    low = t.lower()
    if low.startswith("p0 declared - created a meeting") or low.startswith("p1 declared - created a meeting"):
        return True
    if re.fullmatch(r"[\d\s,.:;!?+\-*/=]+", t):
        return True
    return False


def _find_recent_report(chat_id: str, sender_open_id: str, *, within_sec: float = 600) -> Optional[Dict[str, object]]:
    cutoff = _now_ts() - within_sec
    found: Optional[Dict[str, object]] = None
    for r in reversed(_REPORTS):
        if str(r.get("chat_id") or "") != chat_id:
            continue
        if str(r.get("sender_open_id") or "") != sender_open_id:
            continue
        if float(r.get("ts") or 0) < cutoff:
            continue
        found = r
        break
    return found


def _format_player_ids_md(ids: List[str], limit: int = 12) -> str:
    if not ids:
        return ""
    show = ids[:limit]
    lines = "\n".join(f"• `{pid}`" for pid in show)
    if len(ids) > limit:
        lines += f"\n• …and {len(ids) - limit} more"
    return lines


def _try_player_id_followup(
    raw: str,
    chat_id: str,
    sender_open_id: str,
    tenant_token: str,
    *,
    source_chat_name: str = "",
) -> bool:
    """Second message with player IDs — attach to recent issue from same sender."""
    if not _is_player_id_list_message(raw):
        return False
    ids = extract_player_ids(raw)
    if not ids:
        return False
    with _STORE_LOCK:
        recent = _find_recent_report(chat_id, sender_open_id)
    if not recent:
        log.info("issue_watch: player IDs without recent issue context chat_id=%s", chat_id)
        return True

    player_count = len(ids)
    categories = list(recent.get("categories") or [])
    summary = str(recent.get("summary") or "").strip()
    concern = str(recent.get("concern_text") or recent.get("summary") or "").strip()
    if "login_issues" in categories:
        summary = (
            f"{player_count} players cannot login on CP website"
            if player_count != 1
            else "1 player cannot login on CP website"
        )
    elif player_count >= 1 and "player" not in summary.lower():
        summary = f"{summary} ({player_count} player(s))"

    with _STORE_LOCK:
        recent["player_ids"] = ids
        recent["players_count"] = player_count

    cd_key = _cooldown_key(chat_id, "player_ids", str(recent.get("fingerprint") or "generic"))
    with _STORE_LOCK:
        if _cooldown_active(cd_key):
            log.info("issue_watch: player ID follow-up cooldown key=%s", cd_key)
            return True
        _set_cooldown(cd_key, max(5, _config.get_p0_issue_watch_cooldown_min() // 2))

    alert_card = _cards.build_issue_watch_alert_card(
        group_label=(source_chat_name or "").strip() or chat_id,
        categories_md=_format_categories(categories),
        summary=summary,
        concern=_quote_excerpt(concern),
        alert_time=_format_alert_time(),
        players_count=player_count,
        player_ids_md=_format_player_ids_md(ids),
    )
    n = _send_dm_alerts(tenant_token, alert_card)
    log.info(
        "issue_watch: player ID follow-up sent=%s chat_id=%s count=%s",
        n,
        chat_id,
        player_count,
    )
    return True


def _prune_store(window_sec: float) -> None:
    cutoff = _now_ts() - window_sec
    global _REPORTS
    _REPORTS = [r for r in _REPORTS if float(r.get("ts") or 0) >= cutoff]


def _count_unique_reporters(chat_id: str, fingerprint: str, window_sec: float) -> int:
    cutoff = _now_ts() - window_sec
    seen: Set[str] = set()
    for r in _REPORTS:
        if str(r.get("chat_id") or "") != chat_id:
            continue
        if str(r.get("fingerprint") or "") != fingerprint:
            continue
        if float(r.get("ts") or 0) < cutoff:
            continue
        sid = str(r.get("sender_open_id") or "").strip()
        if sid:
            seen.add(sid)
    return len(seen)


def _cooldown_key(chat_id: str, alert_kind: str, fingerprint: str) -> str:
    fp = (fingerprint or "generic")[:60]
    return f"{chat_id}:{alert_kind}:{fp}"


def _cooldown_active(key: str) -> bool:
    exp = _COOLDOWN.get(key) or 0.0
    return _now_ts() < exp


def _set_cooldown(key: str, minutes: int) -> None:
    _COOLDOWN[key] = _now_ts() + max(60, minutes * 60)


def _format_categories(keys: List[str]) -> str:
    lines: List[str] = []
    for key in keys:
        if key == "widespread_impact":
            continue
        label, num = _CATEGORY_LABELS.get(key, (key.replace("_", " ").title(), 0))
        if num:
            lines.append(f"{label} (#{num})")
        else:
            lines.append(label)
    return "\n".join(f"• {x}" for x in lines) if lines else "• (unspecified)"


def _quote_excerpt(text: str, limit: int = 320) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if len(t) <= limit:
        return t
    return t[: limit - 1].rstrip() + "…"


def _dm_recipients() -> List[str]:
    """Same route as P0/P1 overview instruction DMs."""
    return list(_config.get_dm_instruction_open_ids())


def _send_dm_alerts(token: str, card: Dict[str, object]) -> int:
    recipients = _dm_recipients()
    if not recipients:
        log.warning("issue_watch: no P0_DM_INSTRUCTION_OPEN_IDS — alert not sent")
        return 0
    tok = (token or "").strip() or _lark.get_tenant_token_primary()
    if not tok:
        log.warning("issue_watch: no tenant token — alert not sent")
        return 0
    sent = 0
    for oid in recipients:
        if not oid:
            continue
        st, body, _mid = _lark.post_card_to_open_id(oid, tok, card)
        if st == 200:
            sent += 1
        else:
            log.warning("issue_watch: DM card HTTP=%s open_id=%s body=%s", st, oid[:16], (body or "")[:200])
    return sent


def try_handle_issue_watch(
    text: str,
    chat_id: str,
    sender_open_id: str,
    tenant_token: str,
    *,
    source_chat_name: str = "",
) -> bool:
    """
    Classify detection-group chatter and DM overview duty users on incident signals.

    Returns True when watch is enabled (message was evaluated); False when disabled.
    """
    if not _config.get_p0_issue_watch_enabled():
        return False
    cid = (chat_id or "").strip()
    if not cid:
        return False
    raw = (text or "").strip()
    if not raw:
        return True
    if _try_player_id_followup(
        raw,
        cid,
        (sender_open_id or "").strip(),
        tenant_token,
        source_chat_name=source_chat_name,
    ):
        return True
    if _should_skip_noise(raw):
        return True

    recipients = _dm_recipients()
    if not recipients:
        log.warning(
            "issue_watch: P0_ISSUE_WATCH enabled but no DM recipients — set P0_DM_INSTRUCTION_OPEN_IDS "
            "(same as overview duty)"
        )

    log.info("issue_watch: evaluating chat_id=%s text_head=%r recipients=%s", cid, raw[:120], len(recipients))

    result = classify_issue_watch_message(raw)
    if not result:
        log.warning("issue_watch: no classification (check ANTHROPIC_API_KEY) chat_id=%s", cid)
        return True

    if not result.get("is_incident_signal"):
        log.info(
            "issue_watch: not a signal chat_id=%s reason=%r",
            cid,
            (result.get("reason") or "")[:120],
        )
        return True

    fingerprint = str(result.get("issue_fingerprint") or "unknown_issue")
    sender = (sender_open_id or "").strip()
    window_min = _config.get_p0_issue_watch_window_min()
    window_sec = float(window_min * 60)
    min_reports = _config.get_p0_issue_watch_min_reports()
    min_conf = _config.get_p0_issue_watch_min_confidence()
    cooldown_min = _config.get_p0_issue_watch_cooldown_min()

    with _STORE_LOCK:
        _prune_store(window_sec)
        player_ids = list(result.get("player_ids") or extract_player_ids(raw))
        _REPORTS.append(
            {
                "chat_id": cid,
                "fingerprint": fingerprint,
                "sender_open_id": sender,
                "ts": _now_ts(),
                "summary": result.get("summary") or "",
                "concern_text": raw,
                "categories": list(result.get("categories") or []),
                "player_ids": player_ids,
                "players_count": max(int(result.get("players_mentioned_in_message") or 0), len(player_ids)),
            }
        )
        reporter_count = _count_unique_reporters(cid, fingerprint, window_sec)

    categories = list(result.get("categories") or [])
    confidence = float(result.get("confidence") or 0.0)
    try:
        players_in_msg = int(result.get("players_mentioned_in_message") or 0)
    except (TypeError, ValueError):
        players_in_msg = 0
    id_count = len(set(re.findall(r"\b\d{10}\b", raw)))
    players_mentioned = max(players_in_msg, id_count)
    if players_mentioned >= min_reports and "widespread_impact" not in categories:
        categories.append("widespread_impact")
    widespread = reporter_count >= min_reports or players_mentioned >= min_reports
    high_conf = confidence >= min_conf

    if not high_conf and not widespread:
        log.info(
            "issue_watch: signal below threshold chat_id=%s conf=%.2f reporters=%s fp=%s",
            cid,
            confidence,
            reporter_count,
            fingerprint,
        )
        return True

    alert_kind = "widespread" if widespread else "category"
    cd_key = _cooldown_key(cid, alert_kind, fingerprint)
    with _STORE_LOCK:
        if _cooldown_active(cd_key):
            log.info("issue_watch: cooldown active key=%s", cd_key)
            return True
        _set_cooldown(cd_key, cooldown_min)

    player_ids = list(result.get("player_ids") or extract_player_ids(raw))
    players_count = max(players_mentioned, len(player_ids))
    alert_card = _cards.build_issue_watch_alert_card(
        group_label=(source_chat_name or "").strip() or cid,
        categories_md=_format_categories(categories),
        summary=str(result.get("summary") or ""),
        concern=_quote_excerpt(raw),
        alert_time=_format_alert_time(),
        players_count=players_count,
        player_ids_md=_format_player_ids_md(player_ids),
    )
    n = _send_dm_alerts(tenant_token, alert_card)
    log.info(
        "issue_watch: alert sent=%s chat_id=%s fp=%s reporters=%s conf=%.2f categories=%s",
        n,
        cid,
        fingerprint,
        reporter_count,
        confidence,
        categories,
    )
    return True
