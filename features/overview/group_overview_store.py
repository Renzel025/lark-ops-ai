"""
In-memory store for overviews posted to Lark groups (enables **Edit** on the group card).

Keyed by ``chat_id`` + ``message_id`` so the primary bot can PATCH the same message after Save in DM.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("lark-ops-ai")

_LOCK = threading.Lock()
_BY_KEY: Dict[str, Dict[str, Any]] = {}
_TTL_SEC = 24 * 3600
_MAX_ROWS = 400


def _key(chat_id: str, message_id: str) -> str:
    return f"{(chat_id or '').strip()}:{(message_id or '').strip()}"


def save_group_overview(
    *,
    group_chat_id: str,
    group_message_id: str,
    md: str,
    issue: str,
    impact: str,
    support: str,
    combined_text: str,
    mention_names: list,
    start_epoch: int,
    priority: str,
    target_chat: str,
    source_incident_chat_id: str,
    source_chat_label: str,
    sent_by_open_id: str = "",
    broadcast_chat_id: str = "",
    broadcast_message_id: str = "",
) -> None:
    cid = (group_chat_id or "").strip()
    mid = (group_message_id or "").strip()
    if not cid.startswith("oc_") or not mid:
        return
    row = {
        "group_chat_id": cid,
        "group_message_id": mid,
        "broadcast_chat_id": (broadcast_chat_id or "").strip(),
        "broadcast_message_id": (broadcast_message_id or "").strip(),
        "md": (md or "").strip(),
        "issue": (issue or "").strip(),
        "impact": (impact or "").strip(),
        "support": (support or "").strip(),
        "combined_text": (combined_text or "").strip(),
        "mention_names": list(mention_names or []),
        "start_epoch": int(start_epoch or 0),
        "priority": (priority or "P0").strip().upper(),
        "target_chat": (target_chat or "").strip(),
        "source_incident_chat_id": (source_incident_chat_id or "").strip(),
        "source_chat_label": (source_chat_label or "").strip(),
        "sent_by_open_id": (sent_by_open_id or "").strip(),
        "updated_at": int(time.time()),
    }
    k = _key(cid, mid)
    with _LOCK:
        _BY_KEY[k] = row
        if len(_BY_KEY) > _MAX_ROWS:
            oldest = sorted(_BY_KEY.items(), key=lambda x: int(x[1].get("updated_at") or 0))[:50]
            for ok, _ in oldest:
                _BY_KEY.pop(ok, None)
    log.info(
        "group_overview_store: saved chat_id=%s message_id=%s",
        cid,
        mid[:20] + "…" if len(mid) > 20 else mid,
    )


def pop_group_overview(group_chat_id: str, group_message_id: str) -> Optional[Dict[str, Any]]:
    """
    Atomically fetch **and remove** the stored overview. Used by the recall→restore flow so a
    duplicate ``im.message.recalled_v1`` event (Lark may redeliver) finds nothing and is a no-op.
    Returns None when absent or past TTL.
    """
    cid = (group_chat_id or "").strip()
    mid = (group_message_id or "").strip()
    if not cid or not mid:
        return None
    k = _key(cid, mid)
    with _LOCK:
        row = _BY_KEY.pop(k, None)
    if not row:
        return None
    if int(time.time()) - int(row.get("updated_at") or 0) > _TTL_SEC:
        return None
    return dict(row)


def get_group_overview(group_chat_id: str, group_message_id: str) -> Optional[Dict[str, Any]]:
    cid = (group_chat_id or "").strip()
    mid = (group_message_id or "").strip()
    if not cid or not mid:
        return None
    k = _key(cid, mid)
    with _LOCK:
        row = _BY_KEY.get(k)
        if not row:
            return None
        if int(time.time()) - int(row.get("updated_at") or 0) > _TTL_SEC:
            _BY_KEY.pop(k, None)
            return None
        return dict(row)


def attach_broadcast_message(
    group_chat_id: str,
    group_message_id: str,
    *,
    broadcast_chat_id: str,
    broadcast_message_id: str,
) -> None:
    """Link the overview-bot copy (``lark-forwarder``) to the primary-bot overview row."""
    cid = (group_chat_id or "").strip()
    mid = (group_message_id or "").strip()
    bcid = (broadcast_chat_id or "").strip()
    bmid = (broadcast_message_id or "").strip()
    if not cid or not mid or not bmid:
        return
    k = _key(cid, mid)
    with _LOCK:
        row = _BY_KEY.get(k)
        if not row:
            return
        row["broadcast_chat_id"] = bcid
        row["broadcast_message_id"] = bmid
        row["updated_at"] = int(time.time())


def attach_fanout_message(
    group_chat_id: str,
    group_message_id: str,
    *,
    fanout_chat_id: str,
    fanout_message_id: str,
) -> None:
    """Link a detection fan-out copy (``OVERVIEW_DETECTION_FANOUT_CHAT_IDS``) to the primary row.

    The fan-out loop posts a SEPARATE card per extra group, each stored under its own
    ``chat_id:message_id`` key. Without this back-link the DM edit session — which only carries the
    primary ``group_chat_id``/``group_message_id`` — has no way to reach those copies, so Save left
    them showing the pre-edit text. Appends to a ``fanout_messages`` list, deduped by message_id.
    """
    cid = (group_chat_id or "").strip()
    mid = (group_message_id or "").strip()
    fcid = (fanout_chat_id or "").strip()
    fmid = (fanout_message_id or "").strip()
    if not cid or not mid or not fcid or not fmid:
        return
    k = _key(cid, mid)
    with _LOCK:
        row = _BY_KEY.get(k)
        if not row:
            return
        siblings = list(row.get("fanout_messages") or [])
        if any(str(s.get("message_id") or "") == fmid for s in siblings):
            return
        siblings.append({"chat_id": fcid, "message_id": fmid})
        row["fanout_messages"] = siblings
        row["updated_at"] = int(time.time())


def get_fanout_messages(group_chat_id: str, group_message_id: str) -> List[Dict[str, str]]:
    """Fan-out copies linked to this primary overview row. Empty when there are none."""
    row = get_group_overview(group_chat_id, group_message_id) or {}
    out: List[Dict[str, str]] = []
    for s in row.get("fanout_messages") or []:
        cid = str(s.get("chat_id") or "").strip()
        mid = str(s.get("message_id") or "").strip()
        if cid and mid:
            out.append({"chat_id": cid, "message_id": mid})
    return out


def update_group_overview_md(
    group_chat_id: str, group_message_id: str, *, md: str, issue: str, impact: str, support: str, start_epoch: int
) -> None:
    cid = (group_chat_id or "").strip()
    mid = (group_message_id or "").strip()
    k = _key(cid, mid)
    with _LOCK:
        row = _BY_KEY.get(k)
        if not row:
            return
        row["md"] = (md or "").strip()
        row["issue"] = (issue or "").strip()
        row["impact"] = (impact or "").strip()
        row["support"] = (support or "").strip()
        row["start_epoch"] = int(start_epoch or 0)
        row["updated_at"] = int(time.time())


def group_overview_edit_enabled() -> bool:
    from p0_logic import config as _config

    return _config.get_p0_group_overview_edit_enabled()
