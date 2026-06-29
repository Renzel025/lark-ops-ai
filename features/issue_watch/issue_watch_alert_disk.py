"""
Persist Issue Watch alert snapshots when ``P0_SHARED_STATE_DIR`` is set.

Without this, ``find_latest_alert_key_for_chat`` only works in the same worker process
that sent the detection DM — multi-worker Gunicorn or ``systemctl restart`` loses the cache
before P0 declare can build the suggested overview.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional

from features.overview import draft_store as _ds

log = logging.getLogger("lark-ops-ai")

_CACHE_TTL_SEC = 7200.0


def enabled() -> bool:
    return _ds.disk_enabled()


def _safe_chat_id(chat_id: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in (chat_id or ""))[:220] or "unknown"


def _path(chat_id: str) -> str:
    base = _ds.shared_state_dir()
    return os.path.join(base, "issue_watch_alerts", f"{_safe_chat_id(chat_id)}.json")


def save_alert_snapshot(chat_id: str, alert_key: str, snapshot: Dict[str, Any]) -> None:
    cid = (chat_id or "").strip()
    key = (alert_key or "").strip()
    if not enabled() or not cid or not key:
        return
    row = dict(snapshot)
    row["alert_key"] = key
    row["chat_id"] = cid
    row["ts"] = float(row.get("ts") or time.time())
    path = _path(cid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(row, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        log.info(
            "issue_watch_alert_disk: saved chat_id_tail=%s alert_key=%s",
            cid[-12:] if len(cid) > 12 else cid,
            key[:12],
        )
    except Exception as e:
        log.warning("issue_watch_alert_disk: save failed path=%s err=%s", path, e)
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass


def load_latest_alert(chat_id: str, max_age_sec: float = _CACHE_TTL_SEC) -> Optional[Dict[str, Any]]:
    cid = (chat_id or "").strip()
    if not enabled() or not cid:
        return None
    path = _path(cid)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            row = json.load(f)
        if not isinstance(row, dict):
            return None
        ts = float(row.get("ts") or 0)
        if ts <= 0 or time.time() - ts > max_age_sec:
            return None
        return dict(row)
    except Exception as e:
        log.warning("issue_watch_alert_disk: load failed path=%s err=%s", path, e)
        return None
