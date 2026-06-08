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

from . import config as _config
from . import lark_client as _lark
from .issue_watch_ai import classify_issue_watch_message

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


def _should_skip_noise(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < _config.get_p0_issue_watch_min_text_len():
        return True
    low = t.lower()
    if low.startswith("p0 declared - created a meeting") or low.startswith("p1 declared - created a meeting"):
        return True
    if re.fullmatch(r"[\d\s,.:;!?+\-*/=]+", t):
        return True
    return False


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


def _format_categories(keys: List[str], *, include_widespread: bool) -> str:
    lines: List[str] = []
    for key in keys:
        label, num = _CATEGORY_LABELS.get(key, (key.replace("_", " ").title(), 0))
        if num:
            lines.append(f"{label} (#{num})")
        else:
            lines.append(label)
    if include_widespread and "widespread_impact" not in keys:
        label, num = _CATEGORY_LABELS["widespread_impact"]
        lines.append(f"{label} (#{num})")
    return "\n".join(f"• {x}" for x in lines) if lines else "• (unspecified)"


def _quote_excerpt(text: str, limit: int = 320) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if len(t) <= limit:
        return t
    return t[: limit - 1].rstrip() + "…"


def _build_dm_text(
    *,
    group_label: str,
    chat_id: str,
    categories: List[str],
    confidence: float,
    summary: str,
    excerpt: str,
    widespread_count: int,
    widespread_threshold: int,
    window_min: int,
) -> str:
    widespread = widespread_count >= widespread_threshold
    title_group = (group_label or chat_id).strip()
    body = [
        f"🚨 Major P0 Detection alert — {title_group}",
        "",
        "Category:",
        _format_categories(categories, include_widespread=widespread),
    ]
    if summary:
        body.extend(["", f"Summary: {summary}"])
    if widespread:
        body.extend(
            [
                "",
                f"Widespread: {widespread_count} players reported the same issue "
                f"(last {window_min} min)",
            ]
        )
    body.extend(
        [
            "",
            f"Concern: 「{_quote_excerpt(excerpt)}」",
            "",
            f"Time: {_format_alert_time()}",
        ]
    )
    return "\n".join(body)


def _dm_recipients() -> List[str]:
    """Same route as P0/P1 overview instruction DMs."""
    return list(_config.get_dm_instruction_open_ids())


def _send_dm_alerts(token: str, text: str) -> int:
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
        st, body = _lark.post_text_to_open_id(oid, tok, text)
        if st == 200:
            sent += 1
        else:
            log.warning("issue_watch: DM HTTP=%s open_id=%s body=%s", st, oid[:16], (body or "")[:200])
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
    if not raw or _should_skip_noise(raw):
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
        _REPORTS.append(
            {
                "chat_id": cid,
                "fingerprint": fingerprint,
                "sender_open_id": sender,
                "ts": _now_ts(),
                "summary": result.get("summary") or "",
                "categories": list(result.get("categories") or []),
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

    dm_text = _build_dm_text(
        group_label=(source_chat_name or "").strip(),
        chat_id=cid,
        categories=categories,
        confidence=confidence,
        summary=str(result.get("summary") or ""),
        excerpt=raw,
        widespread_count=max(reporter_count, players_mentioned),
        widespread_threshold=min_reports,
        window_min=window_min,
    )
    n = _send_dm_alerts(tenant_token, dm_text)
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
