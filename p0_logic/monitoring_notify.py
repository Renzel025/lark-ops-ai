"""Fan-out duty warnings and log alerts to a monitoring Lark group."""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Dict, List, Optional, Tuple

from . import config as _config
from . import lark_client as _lark

log = logging.getLogger("lark-ops-ai")

_DEDUPE_LOCK = threading.Lock()
_LAST_SENT: Dict[str, float] = {}


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


def _format_duty_body(text: str, *, duty_open_id: str = "", label: str = "") -> str:
    body = (text or "").strip()
    if not body:
        return ""
    oid = (duty_open_id or "").strip()
    who = f"To: `{oid[-12:]}`" if oid else "To: duty DM"
    kind = (label or "duty warning").strip()
    return f"🔔 **Ops monitor · {kind}**\n{who}\n\n---\n\n{body}"


def _format_log_body(record: logging.LogRecord) -> str:
    msg = (record.getMessage() or "").strip()
    if not msg:
        return ""
    level = (record.levelname or "LOG").upper()
    name = (record.name or "logger").strip()
    return f"🔴 **Ops monitor · log {level}**\n`{name}`\n\n---\n\n{msg}"


def post_to_monitoring_chats(
    tenant_token: str,
    text: str,
    *,
    dedupe_key: str = "",
) -> int:
    """Post plain text to each ``P0_MONITORING_CHAT_IDS`` group. Returns success count."""
    tok = (tenant_token or "").strip()
    body = (text or "").strip()
    if not tok or not body or not _enabled():
        return 0
    key = (dedupe_key or hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest()[:24])
    if not _should_send_dedupe(key):
        log.debug("monitoring: skipped duplicate alert key=%s", key[:12])
        return 0
    ok_n = 0
    for cid in _config.get_p0_monitoring_chat_ids():
        st, resp = _lark.post_text_to_chat(cid, tok, body)
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
    body = _format_duty_body(text, duty_open_id=duty_open_id, label=label)
    if not body:
        return 0
    dedupe = f"duty:{duty_open_id}:{hashlib.sha256(text.encode()).hexdigest()[:16]}"
    return post_to_monitoring_chats(tenant_token, body, dedupe_key=dedupe)


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


class LarkMonitoringLogHandler(logging.Handler):
    """Post ERROR+ (configurable) log records to ``P0_MONITORING_CHAT_IDS``."""

    def emit(self, record: logging.LogRecord) -> None:
        if not _log_alerts_enabled():
            return
        try:
            min_level = _config.get_p0_monitoring_log_min_level()
            if record.levelno < min_level:
                return
            # Skip our own monitoring posts to avoid loops.
            if (record.name or "").startswith("lark-ops-ai") and "monitoring:" in (record.getMessage() or ""):
                return
            body = _format_log_body(record)
            if not body:
                return
            tok = _lark.get_tenant_token_primary()
            if not tok:
                return
            dedupe = f"log:{record.name}:{record.levelname}:{hashlib.sha256(body.encode()).hexdigest()[:16]}"
            post_to_monitoring_chats(tok, body, dedupe_key=dedupe)
        except Exception:
            self.handleError(record)


def install_log_handler(*, logger_name: str = "lark-ops-ai") -> None:
    """Attach monitoring log handler once (idempotent)."""
    if not _log_alerts_enabled():
        return
    root = logging.getLogger(logger_name)
    for h in root.handlers:
        if isinstance(h, LarkMonitoringLogHandler):
            return
    handler = LarkMonitoringLogHandler()
    handler.setLevel(_config.get_p0_monitoring_log_min_level())
    root.addHandler(handler)
    log.info(
        "monitoring: log alerts ON — min_level=%s chats=%s cooldown=%ss",
        logging.getLevelName(_config.get_p0_monitoring_log_min_level()),
        len(_config.get_p0_monitoring_chat_ids()),
        _config.get_p0_monitoring_alert_cooldown_sec(),
    )
