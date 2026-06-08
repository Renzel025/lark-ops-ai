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
_MESSAGE_DEDUPE: Dict[str, float] = {}
_MESSAGE_DEDUPE_LOCK = threading.Lock()
_MESSAGE_DEDUPE_TTL_SEC = 30.0
_ALERTED_SOURCE_MESSAGE_IDS: Dict[str, float] = {}
_DEFERRED_ALERT_LOCK = threading.Lock()
_DEFERRED_ALERT_TIMERS: Dict[str, threading.Timer] = {}
_DEFERRED_ALERT_PAYLOADS: Dict[str, Dict[str, object]] = {}


def _now_ts() -> float:
    return time.time()


def _format_alert_time() -> str:
    """Server-local clock (ose-bot is MYT); no zoneinfo import (Python 3.8 safe)."""
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _format_message_create_time(create_time_ms: str) -> str:
    """Lark ``create_time`` is milliseconds since epoch."""
    raw = (create_time_ms or "").strip()
    if not raw.isdigit():
        return ""
    try:
        sec = int(raw) / 1000.0
        return datetime.fromtimestamp(sec).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return ""


def _resolve_group_label(chat_id: str, source_chat_name: str, token: str) -> str:
    label = (source_chat_name or "").strip()
    if label:
        return label
    label = _config.get_emergency_topic_for_source_chat(chat_id).strip()
    if label and not label.startswith("oc_"):
        return label
    if token:
        label = _lark.get_group_chat_name(chat_id, token).strip()
        if label:
            return label
    return chat_id


def _prune_alerted_message_ids(window_sec: float) -> None:
    cutoff = _now_ts() - window_sec
    global _ALERTED_SOURCE_MESSAGE_IDS
    _ALERTED_SOURCE_MESSAGE_IDS = {
        mid: ts for mid, ts in _ALERTED_SOURCE_MESSAGE_IDS.items() if ts >= cutoff
    }


def _already_alerted_for_message(message_id: str) -> bool:
    mid = (message_id or "").strip()
    if not mid:
        return False
    return mid in _ALERTED_SOURCE_MESSAGE_IDS


def _mark_alerted_for_message(message_id: str, *, window_min: int) -> None:
    mid = (message_id or "").strip()
    if not mid:
        return
    with _STORE_LOCK:
        _prune_alerted_message_ids(float(max(window_min, 60) * 60 * 2))
        _ALERTED_SOURCE_MESSAGE_IDS[mid] = _now_ts()


def _is_player_id_list_message(text: str) -> bool:
    """Follow-up bubble that is mostly player/account IDs (incl. ``Account:`` lists)."""
    t = (text or "").strip()
    ids = extract_player_ids(t)
    if len(ids) < 1:
        return False
    remainder = t
    for pid in ids:
        remainder = remainder.replace(pid, " ")
    remainder = re.sub(r"(?i)\baccount\b", " ", remainder)
    remainder = re.sub(r"[\s,;:+.\-]+", "", remainder)
    return len(remainder) < 12


def _expects_player_id_followup(text: str) -> bool:
    """Report mentions players but IDs likely come in the next message."""
    t = (text or "").strip()
    if extract_player_ids(t):
        return False
    return bool(re.search(r"(?is)\bplayers?\b", t))


def _defer_alert_key(chat_id: str, sender_open_id: str) -> str:
    return f"{chat_id}:{sender_open_id}"


def _cancel_deferred_alert(chat_id: str, sender_open_id: str) -> bool:
    key = _defer_alert_key(chat_id, sender_open_id)
    with _DEFERRED_ALERT_LOCK:
        timer = _DEFERRED_ALERT_TIMERS.pop(key, None)
        _DEFERRED_ALERT_PAYLOADS.pop(key, None)
    if timer:
        timer.cancel()
        return True
    return False


def _send_issue_watch_alert_card(tenant_token: str, payload: Dict[str, object]) -> int:
    from . import issue_watch_overview as _iwo

    alert_key = _iwo.prepare_alert_for_overview(payload)
    src = str(payload.get("source_incident_chat_id") or "").strip()
    tgt = str(payload.get("target_chat") or "").strip()
    if not src or not tgt:
        cid = str(payload.get("chat_id") or "").strip()
        if cid:
            src, tgt, _ = _iwo.resolve_overview_routing(cid)
    alert_card = _cards.build_issue_watch_alert_card(
        group_label=str(payload.get("group_label") or ""),
        categories_md=str(payload.get("categories_md") or ""),
        summary=str(payload.get("summary") or ""),
        concern=str(payload.get("concern") or ""),
        alert_time=str(payload.get("alert_time") or _format_alert_time()),
        player_ids_md=str(payload.get("player_ids_md") or ""),
        source_message_link=str(payload.get("source_message_link") or ""),
        source_message_time=str(payload.get("source_message_time") or ""),
        supplemental_player_ids=bool(payload.get("supplemental_player_ids")),
        issue_watch_alert_key=alert_key,
        source_incident_chat_id=src,
        target_chat=tgt,
        auto_overview_buttons=False,
    )
    return _send_dm_alerts(tenant_token, alert_card)


def _fire_deferred_issue_watch_alert(defer_key: str) -> None:
    with _DEFERRED_ALERT_LOCK:
        payload = dict(_DEFERRED_ALERT_PAYLOADS.pop(defer_key, {}) or {})
        _DEFERRED_ALERT_TIMERS.pop(defer_key, None)
    if not payload:
        return
    tok = str(payload.get("tenant_token") or "").strip()
    cid = str(payload.get("chat_id") or "").strip()
    sender = str(payload.get("sender_open_id") or "").strip()
    mid = str(payload.get("message_id") or "").strip()
    if not tok or not cid:
        return
    with _STORE_LOCK:
        for r in reversed(_REPORTS):
            if str(r.get("chat_id") or "") != cid:
                continue
            if str(r.get("sender_open_id") or "") != sender:
                continue
            if r.get("ids_followup_sent"):
                log.info("issue_watch: deferred alert skipped (IDs card already sent) chat_id=%s", cid)
                return
            break
    n = _send_issue_watch_alert_card(tok, payload)
    if n > 0:
        if mid:
            _mark_alerted_for_message(
                mid,
                window_min=int(payload.get("cooldown_min") or _config.get_p0_issue_watch_cooldown_min()),
            )
        with _STORE_LOCK:
            for r in reversed(_REPORTS):
                if str(r.get("chat_id") or "") != cid:
                    continue
                if str(r.get("sender_open_id") or "") != sender:
                    continue
                r["alert_sent"] = True
                break
    log.info(
        "issue_watch: deferred alert sent=%s chat_id=%s (no ID follow-up within wait window)",
        n,
        cid,
    )


def _schedule_deferred_issue_watch_alert(
    chat_id: str,
    sender_open_id: str,
    payload: Dict[str, object],
    *,
    wait_sec: int,
) -> None:
    key = _defer_alert_key(chat_id, sender_open_id)
    _cancel_deferred_alert(chat_id, sender_open_id)

    def _run() -> None:
        _fire_deferred_issue_watch_alert(key)

    timer = threading.Timer(max(1, wait_sec), _run)
    timer.daemon = True
    with _DEFERRED_ALERT_LOCK:
        _DEFERRED_ALERT_TIMERS[key] = timer
        _DEFERRED_ALERT_PAYLOADS[key] = dict(payload)
    timer.start()
    log.info(
        "issue_watch: deferred alert %ss — waiting for Account/player ID follow-up chat_id=%s",
        wait_sec,
        chat_id,
    )


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
    message_id: str = "",
    message_create_time: str = "",
) -> bool:
    """Second message with player IDs — merge into one alert for the recent report."""
    if not _is_player_id_list_message(raw):
        return False
    ids = extract_player_ids(raw)
    if not ids:
        return False
    _cancel_deferred_alert(chat_id, sender_open_id)
    with _STORE_LOCK:
        recent = _find_recent_report(chat_id, sender_open_id)
    if not recent:
        log.info("issue_watch: player IDs without recent issue context chat_id=%s", chat_id)
        return True
    if recent.get("ids_followup_sent"):
        log.info("issue_watch: player ID follow-up already sent chat_id=%s", chat_id)
        return True

    player_count = len(ids)
    categories = list(recent.get("categories") or [])
    summary = str(recent.get("summary") or "").strip()
    concern = str(recent.get("concern_text") or recent.get("summary") or "").strip()
    if "deposit_issues" in categories:
        summary = (
            f"{player_count} players cannot deposit on CP website"
            if player_count != 1
            else "1 player cannot deposit on CP website"
        )
    elif "login_issues" in categories:
        summary = (
            f"{player_count} players cannot login on CP website"
            if player_count != 1
            else "1 player cannot login on CP website"
        )
    elif player_count >= 1 and "player" not in summary.lower():
        summary = f"{summary} ({player_count} player(s))"

    fp = str(recent.get("fingerprint") or "generic")
    cd_key = _cooldown_key(chat_id, "player_ids", fp)
    with _STORE_LOCK:
        if _cooldown_active(cd_key):
            log.info("issue_watch: player ID follow-up cooldown chat_id=%s", chat_id)
            return True
        recent["player_ids"] = ids
        recent["players_count"] = player_count
        recent["ids_followup_sent"] = True
        _set_cooldown(cd_key, max(5, _config.get_p0_issue_watch_cooldown_min() // 2))

    src_mid = str(recent.get("message_id") or "").strip()
    src_time = _format_message_create_time(str(recent.get("message_create_time") or "")) or _format_alert_time()
    group_label = _resolve_group_label(chat_id, source_chat_name, tenant_token)
    payload = {
        "tenant_token": tenant_token,
        "chat_id": chat_id,
        "message_id": src_mid,
        "fingerprint": fp,
        "categories": categories,
        "players_count": player_count,
        "player_ids": ids,
        "concern_raw": concern,
        "group_label": group_label,
        "categories_md": _format_categories(categories, players_affected=player_count),
        "summary": summary,
        "concern": _quote_excerpt(concern),
        "alert_time": _format_alert_time(),
        "player_ids_md": _format_player_ids_md(ids),
        "source_message_link": _lark.build_message_open_applink(chat_id, src_mid)
        or _lark.build_chat_open_applink(chat_id),
        "source_message_time": src_time,
        "supplemental_player_ids": False,
    }
    n = _send_issue_watch_alert_card(tenant_token, payload)
    if n > 0 and src_mid:
        _mark_alerted_for_message(src_mid, window_min=_config.get_p0_issue_watch_cooldown_min())
    log.info(
        "issue_watch: player ID follow-up sent=%s chat_id=%s count=%s ids=%s",
        n,
        chat_id,
        player_count,
        len(ids),
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


def _normalize_concern_key(text: str) -> str:
    return " ".join((text or "").strip().lower().split())[:180]


def _issue_watch_dedupe_key(
    chat_id: str,
    sender_open_id: str,
    message_id: str,
    message_create_time: str,
    text: str,
) -> str:
    norm = _normalize_concern_key(text)
    ct = (message_create_time or "").strip()
    if ct:
        return f"iw:{chat_id}:{sender_open_id}:{ct}:{norm}"
    mid = (message_id or "").strip()
    return f"iw:{chat_id}:{sender_open_id}:mid:{mid}:{norm}"


def _try_consume_issue_watch_dedupe(key: str) -> bool:
    """True = first Lark delivery for this message; False = duplicate webhook."""
    now = time.monotonic()
    with _MESSAGE_DEDUPE_LOCK:
        for k, t in list(_MESSAGE_DEDUPE.items()):
            if now - t > _MESSAGE_DEDUPE_TTL_SEC:
                del _MESSAGE_DEDUPE[k]
        if key in _MESSAGE_DEDUPE:
            return False
        _MESSAGE_DEDUPE[key] = now
        return True


def _cooldown_key(chat_id: str, alert_kind: str, fingerprint: str) -> str:
    fp = (fingerprint or "generic")[:60]
    return f"{chat_id}:{alert_kind}:{fp}"


def _cooldown_key_for_issue(chat_id: str, categories: List[str], concern_text: str, fingerprint: str) -> str:
    """Same issue text in the same group should not DM again within cooldown."""
    primary = ""
    for cat in categories:
        if cat != "widespread_impact":
            primary = cat
            break
    if not primary:
        primary = (fingerprint or "generic")[:40]
    norm = _normalize_concern_key(concern_text)
    return f"{chat_id}:issue:{primary}:{norm}"


def _cooldown_active(key: str) -> bool:
    exp = _COOLDOWN.get(key) or 0.0
    return _now_ts() < exp


def _set_cooldown(key: str, minutes: int) -> None:
    _COOLDOWN[key] = _now_ts() + max(60, minutes * 60)


def _format_categories(keys: List[str], *, players_affected: int = 0) -> str:
    lines: List[str] = []
    for key in keys:
        if key == "widespread_impact":
            continue
        label, _num = _CATEGORY_LABELS.get(key, (key.replace("_", " ").title(), 0))
        lines.append(label)
    n = int(players_affected or 0)
    if n >= _config.get_p0_issue_watch_min_reports():
        lines.append(f"{n} players are affected")
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
    message_id: str = "",
    message_create_time: str = "",
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
        message_id=message_id,
        message_create_time=message_create_time,
    ):
        return True
    if _should_skip_noise(raw):
        return True

    dedupe_key = _issue_watch_dedupe_key(
        cid,
        (sender_open_id or "").strip(),
        message_id,
        message_create_time,
        raw,
    )
    if not _try_consume_issue_watch_dedupe(dedupe_key):
        log.info(
            "issue_watch: skipped duplicate Lark delivery chat_id=%s text_head=%r",
            cid,
            raw[:80],
        )
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
                "message_id": (message_id or "").strip(),
                "message_create_time": (message_create_time or "").strip(),
                "ids_followup_sent": False,
                "alert_sent": False,
            }
        )
        reporter_count = _count_unique_reporters(cid, fingerprint, window_sec)

    categories = list(result.get("categories") or [])
    confidence = float(result.get("confidence") or 0.0)
    try:
        players_in_msg = int(result.get("players_mentioned_in_message") or 0)
    except (TypeError, ValueError):
        players_in_msg = 0
    id_count = len(player_ids)
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

    mid = (message_id or "").strip()
    if _already_alerted_for_message(mid):
        log.info("issue_watch: skipped — alert already sent for message_id=%s", mid[:24])
        return True

    cd_key = _cooldown_key_for_issue(cid, categories, raw, fingerprint)
    with _STORE_LOCK:
        if _cooldown_active(cd_key):
            log.info(
                "issue_watch: cooldown active (same issue recently) chat_id=%s key=%s",
                cid,
                cd_key[:80],
            )
            return True
        _set_cooldown(cd_key, cooldown_min)

    player_ids = list(result.get("player_ids") or extract_player_ids(raw))
    id_count = len(player_ids)
    src_time = _format_message_create_time(message_create_time) or _format_alert_time()
    group_label = _resolve_group_label(cid, source_chat_name, tenant_token)
    payload: Dict[str, object] = {
        "tenant_token": tenant_token,
        "chat_id": cid,
        "sender_open_id": sender,
        "message_id": mid,
        "cooldown_min": cooldown_min,
        "fingerprint": fingerprint,
        "categories": categories,
        "players_count": max(players_mentioned, id_count),
        "player_ids": player_ids,
        "concern_raw": raw,
        "group_label": group_label,
        "categories_md": _format_categories(categories, players_affected=id_count),
        "summary": str(result.get("summary") or ""),
        "concern": _quote_excerpt(raw),
        "alert_time": _format_alert_time(),
        "player_ids_md": _format_player_ids_md(player_ids),
        "source_message_link": _lark.build_message_open_applink(cid, mid)
        or _lark.build_chat_open_applink(cid),
        "source_message_time": src_time,
        "supplemental_player_ids": False,
    }

    wait_sec = _config.get_p0_issue_watch_id_wait_sec()
    if id_count == 0 and _expects_player_id_followup(raw) and wait_sec > 0:
        _schedule_deferred_issue_watch_alert(cid, sender, payload, wait_sec=wait_sec)
        return True

    n = _send_issue_watch_alert_card(tenant_token, payload)
    if n > 0 and mid:
        _mark_alerted_for_message(mid, window_min=cooldown_min)
        with _STORE_LOCK:
            for r in reversed(_REPORTS):
                if str(r.get("chat_id") or "") != cid:
                    continue
                if str(r.get("sender_open_id") or "") != sender:
                    continue
                r["alert_sent"] = True
                break
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
