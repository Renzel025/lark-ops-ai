"""Fan-out duty warnings and log alerts to a monitoring Lark group."""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

from . import cards as _cards
from . import config as _config
from . import lark_client as _lark

log = logging.getLogger("lark-ops-ai")

_DEDUPE_LOCK = threading.Lock()
_LAST_SENT: Dict[str, float] = {}

# Rolling buffer of log anomalies (WARNING+/session min) for the P0 session wrap-up summary.
# Each item: (ts_epoch, levelno, levelname, logger_name, message).
_SESSION_LOG_LOCK = threading.Lock()
_SESSION_LOG_BUF: "Deque[Tuple[float, int, str, str, str]]" = deque(maxlen=4000)


def _enabled() -> bool:
    return bool(_config.get_p0_monitoring_chat_ids())


def _duty_mirror_enabled() -> bool:
    return _enabled() and _config.p0_monitoring_duty_warnings_enabled()


def _log_alerts_enabled() -> bool:
    return _enabled() and _config.p0_monitoring_log_alerts_enabled()


def _should_send_dedupe(key: str) -> bool:
    cooldown = float(_config.get_p0_monitoring_alert_cooldown_sec())
    if cooldown <= 0:
        return True
    now = time.time()
    with _DEDUPE_LOCK:
        last = _LAST_SENT.get(key, 0.0)
        if now - last < cooldown:
            return False
        _LAST_SENT[key] = now
        if len(_LAST_SENT) > 500:
            cutoff = now - max(cooldown * 2, 300)
            stale = [k for k, t in _LAST_SENT.items() if t < cutoff]
            for k in stale:
                _LAST_SENT.pop(k, None)
        return True


def post_card_to_monitoring_chats(
    tenant_token: str,
    card: Dict[str, Any],
    *,
    dedupe_key: str = "",
) -> int:
    """Post an interactive card to each ``P0_MONITORING_CHAT_IDS`` group. Returns success count."""
    tok = (tenant_token or "").strip()
    if not tok or not card or not _enabled():
        return 0
    if not dedupe_key:
        dedupe_key = hashlib.sha256(
            json.dumps(card, ensure_ascii=False, sort_keys=True).encode("utf-8", errors="replace")
        ).hexdigest()[:24]
    if not _should_send_dedupe(dedupe_key):
        log.debug("monitoring: skipped duplicate alert key=%s", dedupe_key[:12])
        return 0
    ok_n = 0
    for cid in _config.get_p0_monitoring_chat_ids():
        st, resp, _ = _lark.post_card_to_chat(cid, tok, card)
        ok, code, msg = _lark.lark_im_message_create_ok(resp)
        if st == 200 and ok:
            ok_n += 1
        else:
            log.warning(
                "monitoring: post failed chat_tail=%s HTTP=%s code=%s msg=%r",
                cid[-12:] if len(cid) > 12 else cid,
                st,
                code,
                (msg or resp or "")[:200],
            )
    if ok_n:
        log.info("monitoring: posted alert to %s group(s)", ok_n)
    return ok_n


def mirror_duty_text(
    tenant_token: str,
    text: str,
    *,
    duty_open_id: str = "",
    label: str = "duty warning",
) -> int:
    """Mirror the same text sent to a duty DM into the monitoring group."""
    if not _duty_mirror_enabled():
        return 0
    body = (text or "").strip()
    if not body:
        return 0
    card = _cards.build_monitoring_duty_card(body, duty_open_id=duty_open_id, label=label)
    dedupe = f"duty:{duty_open_id}:{hashlib.sha256(body.encode()).hexdigest()[:16]}"
    return post_card_to_monitoring_chats(tenant_token, card, dedupe_key=dedupe)


def post_log_alert(
    tenant_token: str,
    message: str,
    *,
    level: str = "ERROR",
    logger_name: str = "",
    dedupe_key: str = "",
) -> int:
    """Post a log-level alert card to the monitoring group."""
    if not _log_alerts_enabled():
        return 0
    body = (message or "").strip()
    if not body:
        return 0
    card = _cards.build_monitoring_log_card(body, level=level, logger_name=logger_name)
    key = dedupe_key or f"log:{logger_name}:{level}:{hashlib.sha256(body.encode()).hexdigest()[:16]}"
    return post_card_to_monitoring_chats(tenant_token, card, dedupe_key=key)


def post_bitable_card_failure_alert(
    tenant_token: str,
    *,
    label: str,
    http_status: int,
    lark_code: Any,
    lark_msg: str,
    dest_chat_id: str = "",
) -> int:
    """
    Alert monitoring GC when 📦/🔴 Bitable interactive card post fails (e.g. Lark 11310 element limit).

    Uses ``post_log_alert`` (not the log handler) so it fires even when failures are logged as WARNING.
    """
    lbl = (label or "bitable").strip()
    msg_s = (lark_msg or "").strip()
    dest_tail = (dest_chat_id or "")[-12:] if dest_chat_id else ""
    body_lines = [
        f"**Bitable card post failed** — `{lbl}`",
        f"HTTP={http_status} · Lark code={lark_code}",
        msg_s[:500] if msg_s else "(no Lark msg)",
    ]
    if dest_tail:
        body_lines.append(f"Incident dest: …{dest_tail}")
    if "11310" in msg_s or "element exceeds" in msg_s.lower():
        body_lines.append(
            "Hint: card too large (Lark element limit) — reduce rows/page or use compact card layout."
        )
    body = "\n".join(body_lines)
    dedupe = f"bitable_fail:{lbl}:{http_status}:{lark_code}:{hashlib.sha256(body.encode()).hexdigest()[:12]}"
    return post_log_alert(
        tenant_token,
        body,
        level="ERROR",
        logger_name="adjustment_bitable",
        dedupe_key=dedupe,
    )


def post_duty_dm(
    duty_open_id: str,
    tenant_token: str,
    text: str,
    *,
    monitor_label: str = "duty warning",
) -> Tuple[int, str]:
    """Post to duty DM and mirror to monitoring GC when configured."""
    oid = (duty_open_id or "").strip()
    tok = (tenant_token or "").strip()
    body = (text or "").strip()
    st, resp = _lark.post_text_to_open_id(oid, tok, body)
    if st == 200 and body:
        try:
            mirror_duty_text(tok, body, duty_open_id=oid, label=monitor_label)
        except Exception as e:
            log.warning("monitoring: duty mirror failed: %s", e)
    return st, resp


def _session_capture_enabled() -> bool:
    return _config.get_p0_session_log_summary_enabled() or _config.get_p0_session_error_to_group_enabled()


def _active_p0_group_ids() -> List[str]:
    """Source incident chat_ids of currently active P0/P1 sessions (lazy import avoids cycles)."""
    try:
        from features.session.session import P0_SESSIONS
    except Exception:
        return []
    out: List[str] = []
    for sess in list(P0_SESSIONS.values()):
        cid = str((sess or {}).get("source_chat") or "").strip()
        if cid.startswith("oc_") and cid not in out:
            out.append(cid)
    return out


def _capture_session_log_record(record: logging.LogRecord, msg: str) -> None:
    """
    Buffer WARNING+ (session min level) for the end-of-session wrap-up, and — while a P0 is active —
    throw the error to the incident group(s) in real time. Called from the log handler; must never
    raise (would break logging).
    """
    try:
        if record.levelno < _config.get_p0_session_log_min_level():
            return
        level = (record.levelname or "LOG").upper()
        name = (record.name or "logger").strip()
        if _config.get_p0_session_log_summary_enabled():
            with _SESSION_LOG_LOCK:
                _SESSION_LOG_BUF.append((time.time(), record.levelno, level, name, msg))
        if _config.get_p0_session_error_to_group_enabled():
            groups = _active_p0_group_ids()
            if groups:
                tok = _lark.get_tenant_token_primary()
                if tok:
                    text = f"⚠️ [{level}] {name}: {msg}"[:900]
                    for cid in groups:
                        dedupe = f"sesserr:{cid}:{level}:{hashlib.sha256(msg.encode()).hexdigest()[:16]}"
                        if _should_send_dedupe(dedupe):
                            _lark.post_text_to_chat(cid, tok, text)
    except Exception:
        pass


def _claude_summarize_session_logs(log_blob: str, priority: str, duration_text: str, count_str: str) -> str:
    try:
        from p0_logic.anthropic_client import anthropic_chat_once, has_anthropic_auth

        if not has_anthropic_auth():
            return f"(Anomalies: {count_str}. Configure Claude for a written summary.)"
        system = (
            "You are an on-call SRE summarizing the log anomalies captured during a P0 incident "
            "session for the ops group. Write a SHORT plain-English summary (2-4 sentences): what "
            "errors/warnings happened, which components, and whether they look impactful or "
            "benign/transient. Do NOT list every line, do NOT invent facts, no fluff."
        )
        user = f"Priority: {priority}\nDuration: {duration_text}\nCounts: {count_str}\n\nLOG ANOMALIES:\n{log_blob}"
        out = anthropic_chat_once(system, user, max_tokens=320)
        return (out or "").strip() or f"(Anomalies: {count_str}.)"
    except Exception as e:
        log.warning("session log summary: claude failed: %s", e)
        return f"(Anomalies: {count_str}.)"


def summarize_session_logs(
    *,
    tenant_token: str,
    source_chat_id: str,
    start_epoch: float,
    end_epoch: Optional[float] = None,
    priority: str = "P0",
    duration_text: str = "",
) -> None:
    """On P0 end: Claude-summarize the log anomalies in the session window → monitoring chat(s)."""
    if not _config.get_p0_session_log_summary_enabled():
        return
    start = float(start_epoch or 0)
    end = float(end_epoch or time.time())
    with _SESSION_LOG_LOCK:
        recs = [r for r in list(_SESSION_LOG_BUF) if start <= r[0] <= end]
    tok = (tenant_token or "").strip() or _lark.get_tenant_token_primary()
    if not tok:
        return
    pri = (priority or "P0").strip().upper()
    dur = (duration_text or "").strip()
    if not recs:
        body = (
            f"✅ **{pri} session wrap-up** — walang aberya na na-detect (no ERROR/WARNING logs) "
            f"sa buong session."
            + (f"\n🕒 Duration: {dur}" if dur else "")
        )
        card = _cards.build_monitoring_log_card(body, level="INFO", logger_name="p0-session-summary")
        post_card_to_monitoring_chats(tok, card, dedupe_key=f"sess-clean:{source_chat_id}:{int(start)}")
        return
    counts: Dict[str, int] = {}
    lines: List[str] = []
    seen = set()
    for _ts, _levelno, level, name, msg in recs:
        counts[level] = counts.get(level, 0) + 1
        k = (level, name, msg[:160])
        if k in seen:
            continue
        seen.add(k)
        lines.append(f"[{level}] {name}: {msg}"[:300])
    count_str = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    summary = _claude_summarize_session_logs("\n".join(lines[:60]), pri, dur, count_str)
    body = (
        f"🧾 **{pri} session wrap-up** — {len(recs)} anomaly log(s) during session ({count_str})."
        + (f"\n🕒 Duration: {dur}" if dur else "")
        + f"\n\n{summary}"
    )
    card = _cards.build_monitoring_log_card(body, level="WARNING", logger_name="p0-session-summary")
    post_card_to_monitoring_chats(tok, card, dedupe_key=f"sess-summary:{source_chat_id}:{int(start)}")


class LarkMonitoringLogHandler(logging.Handler):
    """Post ERROR+ (configurable) log records to ``P0_MONITORING_CHAT_IDS``."""

    _SKIP_LOGGER_NAMES = frozenset({"uvicorn.access"})

    def emit(self, record: logging.LogRecord) -> None:
        if not (_log_alerts_enabled() or _session_capture_enabled()):
            return
        try:
            logger_name = (record.name or "").strip()
            if logger_name in self._SKIP_LOGGER_NAMES:
                return
            # Skip our own monitoring posts to avoid loops.
            if logger_name.startswith("lark-ops-ai") and "monitoring:" in (record.getMessage() or ""):
                return
            msg = (record.getMessage() or "").strip()
            if not msg:
                return
            # Bitable card failures use post_bitable_card_failure_alert (formatted GC card).
            if "adjustment_bitable:" in msg and "card post failed" in msg:
                return
            # Session capture (WARNING+): buffer for the wrap-up + real-time throw to incident group.
            # Runs before the ERROR gate so WARNING-level failures are still captured.
            if _session_capture_enabled():
                _capture_session_log_record(record, msg)
            # Monitoring-chat alert (ERROR+ by default).
            if not _log_alerts_enabled():
                return
            if record.levelno < _config.get_p0_monitoring_log_min_level():
                return
            tok = _lark.get_tenant_token_primary()
            if not tok:
                return
            level = (record.levelname or "LOG").upper()
            name = (record.name or "logger").strip()
            dedupe = f"log:{name}:{level}:{hashlib.sha256(msg.encode()).hexdigest()[:16]}"
            post_log_alert(tok, msg, level=level, logger_name=name, dedupe_key=dedupe)
        except Exception:
            self.handleError(record)


def install_log_handler(*, use_root_logger: bool = True) -> None:
    """
    Attach monitoring log handler once (idempotent).

    Default: root logger so ERROR/WARNING from any module (uvicorn, handlers, …)
    reaches ``P0_MONITORING_CHAT_IDS``. ``uvicorn.access`` is skipped (too noisy).
    """
    if not (_log_alerts_enabled() or _session_capture_enabled()):
        return
    target = logging.getLogger() if use_root_logger else logging.getLogger("lark-ops-ai")
    for h in target.handlers:
        if isinstance(h, LarkMonitoringLogHandler):
            return
    handler = LarkMonitoringLogHandler()
    # Set to the LOWEST threshold in use so WARNING records still reach emit for session capture.
    lvl = _config.get_p0_monitoring_log_min_level()
    if _session_capture_enabled():
        lvl = min(lvl, _config.get_p0_session_log_min_level())
    handler.setLevel(lvl)
    target.addHandler(handler)
    chat_ids = _config.get_p0_monitoring_chat_ids()
    tails = ",".join((c[-12:] if len(c) > 12 else c) for c in chat_ids[:3])
    log.info(
        "monitoring: log alerts ON — min_level=%s chats=%s chat_tails=%s cooldown=%ss root=%s",
        logging.getLevelName(_config.get_p0_monitoring_log_min_level()),
        len(chat_ids),
        tails or "(none)",
        _config.get_p0_monitoring_alert_cooldown_sec(),
        use_root_logger,
    )
