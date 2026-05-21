"""
P0 session state: create, end, cancel, timers, and session lookup.
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from . import cards as _cards
from . import config as _config
from . import lark_client as _lark
from . import session_disk as _session_disk
from . import support as _support

log = logging.getLogger("lark-ops-ai")

P0_SESSIONS: Dict[str, Dict[str, Any]] = {}
_LAST_P0_BY_CHAT: Dict[str, int] = {}
_LAST_P0_LOCK = threading.Lock()

# P1 keyword: waiting for Yes/No on "create meeting?" card (keyed by incident group chat_id)
P1_PROMPT_PENDING: Dict[str, Dict[str, Any]] = {}
_P1_PROMPT_LOCK = threading.Lock()

# Last successful ``end_p0_session`` fields per incident chat (in-memory) — replay ended card if user types "end" again.
_LAST_ENDED_SNAPSHOT_BY_CHAT: Dict[str, Dict[str, str]] = {}
_LAST_ENDED_SNAPSHOT_LOCK = threading.Lock()

P0_COOLDOWN_SEC = _config.P0_COOLDOWN_SEC

# Sentinel for DM overview queue items that are not tied to a live P0 session row.
STANDALONE_DM_SOURCE_CHAT_ID = "__standalone__"

# Per operator (open_id): one active DM instruction slot; further incidents queue until overview is sent.
_DM_INSTR_QUEUE: Dict[str, List[Dict[str, Any]]] = {}
_DM_ACTIVE_ITEM: Dict[str, Dict[str, Any]] = {}
_DM_INSTR_LOCK = threading.Lock()

# DM text when a second+ incident queues while the operator is still on the first overview.
_DM_CONCURRENT_MEETINGS_NOTICE = (
    "ℹ️ Multiple meetings were declared around the same time.\n"
    "Finish the first overview first then it will proceed to other one"
)


def _post_dm_concurrent_meetings_notice(operator_open_id: str, token: str) -> None:
    oid = (operator_open_id or "").strip()
    token = (token or "").strip()
    if not oid or not token:
        return
    try:
        st, body = _lark.post_text_to_open_id(oid, token, _DM_CONCURRENT_MEETINGS_NOTICE)
        if st != 200:
            log.warning(
                "concurrent meetings notice failed HTTP=%s open_id_tail=%s body=%s",
                st,
                oid[-8:] if len(oid) > 8 else oid,
                (body or "")[:400],
            )
    except Exception as e:
        log.warning("concurrent meetings notice exception open_id_tail=%s err=%s", oid[-8:] if len(oid) > 8 else oid, e)


def note_if_standalone_create_overview_blocked(operator_open_id: str, tenant_token: str = "") -> str:
    """
    If non-empty, DM this text instead of enqueueing another standalone ``create overview``.
    Covers: active incident slot, duplicate standalone, draft tied to a live incident.

    When the incident meeting already ended but DM state was not released (or draft still
    points at the old ``oc_``), we heal that first so the operator is not told both
    \"use Build overview\" and \"no meeting — type create overview emergency\".
    """
    oid = (operator_open_id or "").strip()
    tok = (tenant_token or "").strip()
    if not oid:
        return ""

    stale_cid = ""
    with _DM_INSTR_LOCK:
        active = _DM_ACTIVE_ITEM.get(oid)
        if active:
            cid = str(active.get("chat_id") or "").strip()
            if cid and cid != STANDALONE_DM_SOURCE_CHAT_ID and not chat_has_active_session(cid):
                stale_cid = cid

    if stale_cid:
        if tok:
            release_dm_slots_for_incident_chat(stale_cid, tok)
        else:
            log.warning(
                "note_if_standalone: stale DM slot for ended incident, no token — dropping slot only open_id_tail=%s",
                oid[-8:] if len(oid) > 8 else oid,
            )
            with _DM_INSTR_LOCK:
                cur = _DM_ACTIVE_ITEM.get(oid)
                if cur and str(cur.get("chat_id") or "").strip() == stale_cid:
                    del _DM_ACTIVE_ITEM[oid]

    with _DM_INSTR_LOCK:
        active = _DM_ACTIVE_ITEM.get(oid)
        if active:
            cid = str(active.get("chat_id") or "").strip()
            if cid == STANDALONE_DM_SOURCE_CHAT_ID:
                return (
                    "ℹ️ Standalone overview is already active. Finish the first request then proceed to "
                    "trigger again create overview."
                )
            return "ℹ️ For this incident use the Build overview button on the DM card."

    from . import drafts as _drafts

    _drafts.orphan_incident_draft_if_session_ended(oid)
    d = _drafts.get_draft(oid) or {}
    src = str(d.get("source_incident_chat_id") or "").strip()
    if src and src != STANDALONE_DM_SOURCE_CHAT_ID:
        return "ℹ️ For this incident use the Build overview button on the DM card."
    return ""


def get_dm_target_chat_for_operator(operator_open_id: str) -> str:
    """Target ``oc_`` for DM drafts while a queued slot is active (avoids wrong session when multiple P0 exist)."""
    oid = (operator_open_id or "").strip()
    if not oid:
        return ""
    with _DM_INSTR_LOCK:
        active = _DM_ACTIVE_ITEM.get(oid)
        if active:
            return str(active.get("target_chat") or "").strip()
    return get_active_target_chat() or _config.get_dm_overview_target_chat_id()


def enqueue_dm_instruction_if_needed(operator_open_id: str, token: str, item: Dict[str, Any]) -> None:
    """
    Post at most one DM instruction card per operator at a time. Additional incidents are queued FIFO
    until ``release_dm_after_overview_sent`` runs after a successful Send overview.
    """
    oid = (operator_open_id or "").strip()
    token = (token or "").strip()
    if not oid or not token:
        log.warning("enqueue_dm_instruction_if_needed: missing operator_open_id or token")
        return
    chat_id = (item.get("chat_id") or "").strip()
    target_chat = (item.get("target_chat") or "").strip()
    priority = (item.get("priority") or "P0").strip().upper()
    if priority not in ("P0", "P1"):
        priority = "P0"
    label = str(item.get("label") or "").strip()
    op_uid = str(item.get("operator_lark_user_id") or "").strip()
    norm = {
        "chat_id": chat_id,
        "target_chat": target_chat,
        "priority": priority,
        "label": label,
        "operator_lark_user_id": op_uid,
    }
    send_now = False
    with _DM_INSTR_LOCK:
        if oid not in _DM_ACTIVE_ITEM:
            _DM_ACTIVE_ITEM[oid] = norm
            send_now = True
            log.info(
                "DM instruction active (immediate) open_id_tail=%s incident=%s target=%s",
                oid[-8:] if len(oid) > 8 else oid,
                chat_id,
                target_chat,
            )
        else:
            _DM_INSTR_QUEUE.setdefault(oid, []).append(norm)
            log.info(
                "DM instruction queued open_id_tail=%s queue_len=%s incident=%s",
                oid[-8:] if len(oid) > 8 else oid,
                len(_DM_INSTR_QUEUE.get(oid) or []),
                chat_id,
            )
    if not send_now:
        _post_dm_concurrent_meetings_notice(oid, token)
        return
    from . import drafts as _drafts

    _drafts.clear_draft(oid)
    _drafts.clear_preview(oid)
    _drafts.cancel_preview_timer(oid)
    _drafts.seed_draft_for_incident(oid, target_chat, chat_id, draft_priority=priority)
    _send_dm_instruction_card_logged(
        oid,
        token,
        priority,
        label,
        context="DM instruction",
        target_chat=target_chat,
        source_incident_chat_id=chat_id,
        operator_lark_user_id=op_uid,
    )


def release_dm_slots_for_incident_chat(source_incident_chat_id: str, token: str) -> None:
    """
    When a P0/P1 session ends (end / cancel) without sending an overview, the operator's DM instruction
    slot must still advance — otherwise the next ``p1`` looks like a second concurrent incident and
    triggers the \"Multiple meetings\" notice.
    """
    src = (source_incident_chat_id or "").strip()
    tok = (token or "").strip()
    if not src or not tok:
        return
    with _DM_INSTR_LOCK:
        oids = [oid for oid, a in _DM_ACTIVE_ITEM.items() if str(a.get("chat_id") or "").strip() == src]
    for oid in oids:
        release_dm_after_overview_sent(oid, tok, src)


def release_dm_after_overview_sent(operator_open_id: str, token: str, sent_source_incident_chat_id: str) -> None:
    """After overview is posted to the group: advance the FIFO queue and post the next instruction card if any."""
    oid = (operator_open_id or "").strip()
    token = (token or "").strip()
    sent = (sent_source_incident_chat_id or "").strip()
    next_item: Optional[Dict[str, Any]] = None
    with _DM_INSTR_LOCK:
        active = _DM_ACTIVE_ITEM.get(oid)
        if not active:
            return
        exp = str(active.get("chat_id") or "").strip()
        if sent and exp and sent != exp:
            log.warning(
                "release_dm_after_overview_sent: source mismatch expected=%s got=%s open_id_tail=%s",
                exp,
                sent,
                oid[-8:] if len(oid) > 8 else oid,
            )
            return
        del _DM_ACTIVE_ITEM[oid]
        q = list(_DM_INSTR_QUEUE.get(oid) or [])
        if q:
            next_item = q.pop(0)
            _DM_INSTR_QUEUE[oid] = q
            _DM_ACTIVE_ITEM[oid] = next_item
    from . import drafts as _drafts

    _drafts.clear_draft(oid)
    _drafts.clear_preview(oid)
    _drafts.cancel_preview_timer(oid)
    if next_item:
        tc = str(next_item.get("target_chat") or "").strip()
        cid = str(next_item.get("chat_id") or "").strip()
        pr = str(next_item.get("priority") or "P0").strip().upper()
        if pr not in ("P0", "P1"):
            pr = "P0"
        lab = str(next_item.get("label") or "").strip()
        q_op = str(next_item.get("operator_lark_user_id") or "").strip()
        _drafts.seed_draft_for_incident(oid, tc, cid, draft_priority=pr)
        _send_dm_instruction_card_logged(
            oid,
            token,
            pr,
            lab,
            context="queued DM instruction",
            target_chat=tc,
            source_incident_chat_id=cid,
            operator_lark_user_id=q_op,
        )


def release_standalone_overview_cancel(operator_open_id: str, token: str) -> None:
    """
    Standalone ``create overview`` preview was cancelled without sending: remove the active
    slot so the operator can trigger ``create overview emergency|game`` again.
    Does **not** repost the green instruction card for the cancelled flow (caller sends text only).
    If another incident was queued, advance FIFO and post that instruction card.
    """
    oid = (operator_open_id or "").strip()
    token = (token or "").strip()
    if not oid:
        return
    next_item: Optional[Dict[str, Any]] = None
    with _DM_INSTR_LOCK:
        active = _DM_ACTIVE_ITEM.get(oid)
        if not active:
            return
        if str(active.get("chat_id") or "").strip() != STANDALONE_DM_SOURCE_CHAT_ID:
            return
        del _DM_ACTIVE_ITEM[oid]
        q = list(_DM_INSTR_QUEUE.get(oid) or [])
        if q:
            next_item = q.pop(0)
            _DM_INSTR_QUEUE[oid] = q
            _DM_ACTIVE_ITEM[oid] = next_item
    from . import drafts as _drafts

    if next_item and token:
        tc = str(next_item.get("target_chat") or "").strip()
        cid = str(next_item.get("chat_id") or "").strip()
        pr = str(next_item.get("priority") or "P0").strip().upper()
        if pr not in ("P0", "P1"):
            pr = "P0"
        lab = str(next_item.get("label") or "").strip()
        q_op = str(next_item.get("operator_lark_user_id") or "").strip()
        _drafts.seed_draft_for_incident(oid, tc, cid, draft_priority=pr)
        _send_dm_instruction_card_logged(
            oid,
            token,
            pr,
            lab,
            context="queued DM after standalone cancel",
            target_chat=tc,
            source_incident_chat_id=cid,
            operator_lark_user_id=q_op,
        )


def _safe_match_ref(val: Any, meeting_ref: str) -> bool:
    s = str(val or "").strip()
    return bool(s and meeting_ref and s == meeting_ref)


def find_session_by_meeting_ref(meeting_ref: str) -> Tuple[str, Dict[str, Any]]:
    meeting_ref = (meeting_ref or "").strip()
    if not meeting_ref:
        return "", {}
    for chat_id, sess in P0_SESSIONS.items():
        if _safe_match_ref((sess or {}).get("meeting_id"), meeting_ref) or _safe_match_ref((sess or {}).get("meeting_no"), meeting_ref):
            return chat_id, (sess or {})
    return "", {}


def find_session_by_meeting_no(meeting_no: str) -> Tuple[str, Dict[str, Any]]:
    meeting_no = (meeting_no or "").strip()
    if not meeting_no:
        return "", {}
    for chat_id, sess in P0_SESSIONS.items():
        cur = str((sess or {}).get("meeting_no") or "").strip()
        if cur and cur == meeting_no:
            return chat_id, (sess or {})
    if _session_disk.enabled():
        cid, sd = _session_disk.find_session_by_meeting_no_disk(meeting_no)
        if cid and sd:
            P0_SESSIONS[cid] = sd
            return cid, sd
    return "", {}


def get_source_chat_label_for_target_chat(target_chat: str) -> str:
    """Human-readable source incident group name stored on the active session (if any)."""
    target_chat = (target_chat or "").strip()
    if not target_chat:
        return ""
    _cid, sess = find_session_by_target_chat(target_chat)
    return str((sess or {}).get("source_chat_name") or "").strip()


def find_session_by_target_chat(target_chat: str) -> Tuple[str, Dict[str, Any]]:
    target_chat = (target_chat or "").strip()
    if not target_chat:
        return "", {}
    for chat_id, sess in P0_SESSIONS.items():
        cur_target = str((sess or {}).get("target_chat") or "").strip()
        if cur_target == target_chat:
            return chat_id, (sess or {})
    return "", {}


def resolve_source_incident_chat_for_session_command(message_chat_id: str) -> str:
    """
    Map a **message** ``oc_`` (detection or prompt / mirror) to the session **source** incident ``oc_``.

    Used so **cancel** / **end** typed in the prompt group still find the row in ``P0_SESSIONS`` and
    release DM slots (``release_dm_slots_for_incident_chat``).
    """
    cid = (message_chat_id or "").strip()
    if not cid.startswith("oc_"):
        return ""
    if chat_has_active_session(cid):
        return cid
    det = _config.get_source_incident_chat_id_for_mirror_target(cid)
    if det and chat_has_active_session(det):
        return det
    src, _sess = find_session_by_target_chat(cid)
    if src:
        return src
    if _session_disk.enabled():
        src2, data = _session_disk.find_session_source_by_target_chat_disk(cid)
        if src2 and isinstance(data, dict):
            if src2 not in P0_SESSIONS:
                P0_SESSIONS[src2] = data
            return src2
    return ""


def get_active_session_key() -> str:
    if not P0_SESSIONS:
        return ""
    return list(P0_SESSIONS.keys())[-1]


def get_active_session() -> Optional[Dict[str, Any]]:
    key = get_active_session_key()
    return P0_SESSIONS.get(key) if key else None


def get_active_target_chat() -> str:
    if not P0_SESSIONS:
        return ""
    last_key = list(P0_SESSIONS.keys())[-1]
    sess = P0_SESSIONS.get(last_key) or {}
    target_chat = str(sess.get("target_chat") or "").strip()
    return target_chat or last_key


def _session_prompt_chat_id(sess: Dict[str, Any], source_incident_chat_id: str) -> str:
    """Group where meeting / P1 cards were posted: ``target_chat`` when split from detection group."""
    t = str((sess or {}).get("target_chat") or "").strip()
    return t or (source_incident_chat_id or "").strip()


def _patch_meeting_invite_to_terminal(
    sess: Dict[str, Any],
    token: str,
    *,
    kind: str,
    duration_text: str,
    cancel_reason: str = "",
) -> bool:
    """
    Replace the original red invite card in-place (same message_id) so chat is not spammed.
    ``kind`` = ``ended`` | ``cancelled``. Returns True if PATCH returned HTTP 200.
    """
    mid = str((sess or {}).get("meeting_invite_message_id") or "").strip()
    if not mid or not token:
        return False
    try:
        meeting_no = str(sess.get("meeting_no") or "").strip()
        priority = str(sess.get("priority") or "P0").strip().upper()
        em_topic = str(sess.get("emergency_topic") or "").strip()
        if kind == "ended":
            card = _cards.build_meeting_ended_card(
                meeting_no,
                duration_text,
                priority,
                emergency_topic=em_topic,
                update_multi=True,
            )
        elif kind == "cancelled":
            card = _cards.build_meeting_cancelled_card(
                meeting_no=meeting_no,
                duration_text=duration_text,
                priority=priority,
                reason=cancel_reason or "Unspecified",
                emergency_topic=em_topic,
                update_multi=True,
            )
        else:
            log.warning("patch meeting invite: unknown kind=%r", kind)
            return False
        st, body = _lark.patch_interactive_card(token, mid, card)
        if st != 200:
            log.warning(
                "patch meeting invite terminal failed HTTP=%s kind=%s message_id=%s body=%s",
                st,
                kind,
                mid,
                (body or "")[:500],
            )
            return False
        log.info("Patched meeting invite → %s message_id=%s", kind, mid)
        return True
    except Exception as e:
        log.warning("patch meeting invite terminal exception: %s", e)
        return False


def _store_last_ended_snapshot(
    chat_id: str,
    meeting_no: str,
    duration_text: str,
    priority: str,
    emergency_topic: str,
) -> None:
    chat_id = (chat_id or "").strip()
    if not chat_id:
        return
    prio = (priority or "P0").strip().upper()
    if prio not in ("P0", "P1"):
        prio = "P0"
    with _LAST_ENDED_SNAPSHOT_LOCK:
        _LAST_ENDED_SNAPSHOT_BY_CHAT[chat_id] = {
            "meeting_no": meeting_no or "",
            "duration_text": (duration_text or "Not available").strip() or "Not available",
            "priority": prio,
            "emergency_topic": (emergency_topic or "").strip(),
        }


def _clear_last_ended_snapshot(chat_id: str) -> None:
    chat_id = (chat_id or "").strip()
    with _LAST_ENDED_SNAPSHOT_LOCK:
        _LAST_ENDED_SNAPSHOT_BY_CHAT.pop(chat_id, None)


def get_last_ended_snapshot(chat_id: str) -> Optional[Dict[str, str]]:
    """Copy of last ``end_p0_session`` card fields for this chat, or None."""
    chat_id = (chat_id or "").strip()
    with _LAST_ENDED_SNAPSHOT_LOCK:
        d = _LAST_ENDED_SNAPSHOT_BY_CHAT.get(chat_id)
        return dict(d) if d else None


def bind_live_meeting_id(meeting_ref: str) -> None:
    """
    Store the live VC ``meeting.id`` from webhook ``vc.meeting.join_meeting_v1`` on the correct session.

    Prefer resolution by ``meeting_no`` / existing ``meeting_id``; if still unknown, bind to the only
    session that has no ``meeting_id`` yet, else the newest such session, else legacy fallback to the
    last in-memory key.
    """
    meeting_ref = (meeting_ref or "").strip()
    if not meeting_ref or not P0_SESSIONS:
        return
    cid, _ = find_session_by_meeting_ref(meeting_ref)
    if cid:
        sess = P0_SESSIONS.get(cid) or {}
        cur = str(sess.get("meeting_id") or "").strip()
        if cur != meeting_ref:
            sess["meeting_id"] = meeting_ref
            P0_SESSIONS[cid] = sess
            if _session_disk.enabled():
                _session_disk.save_session(cid, sess)
            log.info("Bound live meeting_id=%s to chat_id=%s (matched ref)", meeting_ref, cid)
        return
    candidates: List[Tuple[str, Dict[str, Any]]] = []
    for k, s in P0_SESSIONS.items():
        mid = str((s or {}).get("meeting_id") or "").strip()
        if not mid:
            candidates.append((k, s or {}))
    bind_key = ""
    if len(candidates) == 1:
        bind_key = candidates[0][0]
    elif len(candidates) > 1:
        candidates.sort(key=lambda t: int((t[1] or {}).get("start_epoch") or 0), reverse=True)
        bind_key = candidates[0][0]
        log.warning(
            "bind_live_meeting_id: multiple sessions missing meeting_id; bound meeting_ref=%s to newest chat_id=%s",
            meeting_ref,
            bind_key,
        )
    if bind_key:
        sess = P0_SESSIONS.get(bind_key) or {}
        sess["meeting_id"] = meeting_ref
        P0_SESSIONS[bind_key] = sess
        if _session_disk.enabled():
            _session_disk.save_session(bind_key, sess)
        log.info("Bound live meeting_id=%s to chat_id=%s (unbound session)", meeting_ref, bind_key)
        return
    last_key = list(P0_SESSIONS.keys())[-1]
    sess = P0_SESSIONS.get(last_key) or {}
    sess["meeting_id"] = meeting_ref
    P0_SESSIONS[last_key] = sess
    if _session_disk.enabled():
        _session_disk.save_session(last_key, sess)
    log.warning(
        "bind_live_meeting_id: fallback last chat_id=%s for meeting_ref=%s (all sessions had meeting_id)",
        last_key,
        meeting_ref,
    )


def record_vc_external_join_for_meeting_ref(meeting_ref: str, joiner_open_id: str) -> None:
    """
    Count a VC join as "external" (not the incident trigger) for auto-cancel-if-empty semantics.
    Persisted on the session row when disk is enabled.

    Only increments when ``joiner_open_id`` is **non-empty** and differs from the session
    ``trigger_open_id``. Join events without ``open_id`` are ignored so Lark noise does not
    block auto-cancel (empty room).
    """
    meeting_ref = (meeting_ref or "").strip()
    if not meeting_ref:
        return
    joiner_open_id = (joiner_open_id or "").strip()
    if not joiner_open_id:
        log.debug(
            "record_vc_external_join skipped (no joiner open_id) meeting_ref=%s",
            meeting_ref,
        )
        return
    cid, sess = find_session_by_meeting_ref(meeting_ref)
    if not cid or not sess:
        return
    trigger = str((sess or {}).get("trigger_open_id") or "").strip()
    if trigger and joiner_open_id == trigger:
        return
    sess2 = P0_SESSIONS.get(cid) or {}
    n = int(sess2.get("vc_external_join_count") or 0)
    sess2["vc_external_join_count"] = n + 1
    P0_SESSIONS[cid] = sess2
    if _session_disk.enabled():
        _session_disk.save_session(cid, sess2)
    log.info(
        "record_vc_external_join meeting_ref=%s chat_id=%s count=%s joiner_open_id=%s",
        meeting_ref,
        cid,
        sess2["vc_external_join_count"],
        joiner_open_id,
    )


def schedule_vc_auto_cancel_if_no_external_joins(chat_id: str) -> None:
    """Schedule auto-cancel when no external join was recorded (see config per-source chat)."""
    chat_id = (chat_id or "").strip()
    if not chat_id:
        return
    _config.reload_env_runtime()
    delay = float(_config.get_p0_vc_auto_cancel_sec_for_source_chat(chat_id))
    if delay <= 0:
        scoped = _config.get_p0_vc_auto_cancel_if_no_joins_chat_ids()
        if scoped and chat_id not in scoped:
            log.info(
                "vc auto-cancel: not scheduled chat_id=%s (not in P0_VC_AUTO_CANCEL_IF_NO_JOINS_CHAT_IDS)",
                chat_id,
            )
        elif scoped:
            log.info(
                "vc auto-cancel: not scheduled chat_id=%s (P0_VC_AUTO_CANCEL_IF_NO_JOINS_CHAT_SEC is 0)",
                chat_id,
            )
        else:
            log.info(
                "vc auto-cancel: not scheduled chat_id=%s (P0_VC_AUTO_CANCEL_IF_NO_JOINS_SEC unset or 0)",
                chat_id,
            )
        return
    sess0 = P0_SESSIONS.get(chat_id)
    if not sess0:
        log.warning("vc auto-cancel: not scheduled chat_id=%s (no session in memory)", chat_id)
        return
    run_id = secrets.token_hex(8)
    sess0["vc_auto_cancel_run_id"] = run_id
    P0_SESSIONS[chat_id] = sess0
    if _session_disk.enabled():
        _session_disk.save_session(chat_id, sess0)
    log.info(
        "vc auto-cancel: scheduled chat_id=%s delay_sec=%s run_id=%s",
        chat_id,
        int(delay),
        run_id,
    )

    def worker() -> None:
        time.sleep(delay)
        try:
            _config.reload_env_runtime()
            if _config.get_p0_vc_auto_cancel_sec_for_source_chat(chat_id) <= 0:
                log.info(
                    "vc auto-cancel: worker exit (config now disabled or chat not eligible) chat_id=%s",
                    chat_id,
                )
                return
            sess = P0_SESSIONS.get(chat_id)
            if not sess:
                log.info("vc auto-cancel: worker exit (session gone) chat_id=%s", chat_id)
                return
            if str(sess.get("vc_auto_cancel_run_id") or "") != run_id:
                log.info(
                    "vc auto-cancel: worker exit (stale run_id, new session?) chat_id=%s",
                    chat_id,
                )
                return
            ext = int(sess.get("vc_external_join_count") or 0)
            if ext > 0:
                log.info(
                    "vc auto-cancel: skipped chat_id=%s external_join_count=%s (need ou_ on join events to count)",
                    chat_id,
                    ext,
                )
                return
            tok = _lark.get_tenant_token_primary()
            if not tok:
                log.warning("vc auto-cancel: no primary tenant token chat_id=%s", chat_id)
                return
            log.info(
                "vc auto-cancel: no external joins after %s s — cancelling chat_id=%s",
                int(delay),
                chat_id,
            )
            cancel_p0_session(
                chat_id,
                tok,
                reason="No participants joined (auto-cancel).",
            )
        except Exception as e:
            log.warning("vc auto-cancel worker failed chat_id=%s err=%s", chat_id, e)

    threading.Thread(
        target=worker,
        name=f"vc-auto-cancel-{chat_id[-12:]}",
        daemon=True,
    ).start()


def end_p0_session(
    chat_id: str,
    token: Optional[str] = None,
    *,
    vc_end_meeting_id: str = "",
    skip_vc_end: bool = False,
) -> None:
    chat_id = (chat_id or "").strip()
    sess = P0_SESSIONS.get(chat_id) or {}
    if not sess and _session_disk.enabled():
        sess = _session_disk.load_session(chat_id) or {}
    if sess and chat_id and chat_id not in P0_SESSIONS:
        P0_SESSIONS[chat_id] = sess
    if sess:
        meeting_no_snap = str(sess.get("meeting_no") or "").strip()
        start_epoch_snap = int(sess.get("start_epoch") or 0)
        duration_snap = _cards.format_duration(start_epoch_snap)
        priority_snap = str(sess.get("priority") or "P0").strip().upper()
        em_snap = str(sess.get("emergency_topic") or "").strip()
        _store_last_ended_snapshot(chat_id, meeting_no_snap, duration_snap, priority_snap, em_snap)
    if token and sess:
        # End the live VC on Lark so recording / Video Meeting Assistant can finalize (same as End in the client).
        # ``vc.meeting.meeting_ended_v1`` fires *after* the meeting is already over — POST .../meetings/{id}/end then
        # returns 404; skip those calls and only clean up reserve + cards.
        preferred = (vc_end_meeting_id or "").strip()
        meeting_id = preferred or str(sess.get("meeting_id") or "").strip()
        meeting_no = str(sess.get("meeting_no") or "").strip()
        reserve_id = str(sess.get("reserve_id") or "").strip()
        vc_ended = False
        if not skip_vc_end:
            if meeting_id:
                vc_ended = _lark.end_vc_meeting(token, meeting_id)
                if not vc_ended:
                    log.warning("end_p0_session: end_vc_meeting failed meeting_id=%s", meeting_id)
            if not vc_ended and meeting_no and meeting_no != meeting_id:
                vc_ended = _lark.end_vc_meeting(token, meeting_no)
                if vc_ended:
                    log.info("end_p0_session: ended VC via meeting_no=%s", meeting_no)
                else:
                    log.warning("end_p0_session: end_vc_meeting failed meeting_no=%s", meeting_no)
        else:
            log.info("end_p0_session: skip_vc_end=1 (meeting already ended on Lark)")
        if not vc_ended and reserve_id:
            _lark.delete_vc_reserve(token, reserve_id)
        start_epoch = int(sess.get("start_epoch") or 0)
        priority = str(sess.get("priority") or "P0").strip().upper()
        duration_text = _cards.format_duration(start_epoch)
        em_topic = str(sess.get("emergency_topic") or "").strip()
        patched = _patch_meeting_invite_to_terminal(sess, token, kind="ended", duration_text=duration_text)
        prompt_cid = _session_prompt_chat_id(sess, chat_id)
        if not patched:
            try:
                _lark.post_card_to_chat(
                    prompt_cid,
                    token,
                    _cards.build_meeting_ended_card(
                        meeting_no, duration_text, priority, emergency_topic=em_topic, update_multi=False
                    ),
                )
            except Exception as e:
                log.error("Failed to post meeting ended card (fallback): %s", e)
        summary = f"✅ Meeting ended. Duration: {duration_text}"
        if meeting_no:
            summary += f". Meeting ID: {meeting_no}"
        try:
            _lark.post_text_to_chat(prompt_cid, token, summary)
        except Exception as e:
            log.warning("post_text meeting ended summary failed chat_id=%s err=%s", chat_id, e)
    if token and chat_id:
        s_end = P0_SESSIONS.get(chat_id)
        if s_end and s_end.get("dm_instruction_deferred"):
            _flush_deferred_dm_instruction_for_incident(chat_id)
    P0_SESSIONS.pop(chat_id, None)
    _session_disk.delete_session(chat_id)
    if token:
        release_dm_slots_for_incident_chat(chat_id, token)


def cancel_p0_session(
    chat_id: str,
    token: Optional[str] = None,
    reason: str = "Unspecified",
) -> None:
    chat_id = (chat_id or "").strip()
    sess = P0_SESSIONS.get(chat_id) or {}
    if not sess and _session_disk.enabled():
        sess = _session_disk.load_session(chat_id) or {}
    if sess and chat_id and chat_id not in P0_SESSIONS:
        P0_SESSIONS[chat_id] = sess
    if sess:
        _clear_last_ended_snapshot(chat_id)
    reserve_id = str(sess.get("reserve_id") or "").strip()
    meeting_id = str(sess.get("meeting_id") or "").strip()
    meeting_no = str(sess.get("meeting_no") or "").strip()
    if token and sess:
        meeting_ended = False
        if meeting_id:
            meeting_ended = _lark.end_vc_meeting(token, meeting_id)
        if (not meeting_ended) and reserve_id:
            _lark.delete_vc_reserve(token, reserve_id)
        log.info("cancel_p0_session VC action chat_id=%s reserve_id=%s meeting_id=%s meeting_no=%s", chat_id, reserve_id, meeting_id, meeting_no)
        start_epoch = int(sess.get("start_epoch") or 0)
        priority = str(sess.get("priority") or "P0").strip().upper()
        duration_text = _cards.format_duration(start_epoch)
        em_topic = str(sess.get("emergency_topic") or "").strip()
        patched = _patch_meeting_invite_to_terminal(
            sess, token, kind="cancelled", duration_text=duration_text, cancel_reason=reason
        )
        prompt_cid = _session_prompt_chat_id(sess, chat_id)
        if not patched:
            try:
                _lark.post_card_to_chat(
                    prompt_cid,
                    token,
                    _cards.build_meeting_cancelled_card(
                        meeting_no=meeting_no,
                        duration_text=duration_text,
                        priority=priority,
                        reason=reason,
                        emergency_topic=em_topic,
                        update_multi=False,
                    ),
                )
            except Exception as e:
                log.error("Failed to post meeting cancelled card (fallback): %s", e)
    if token and chat_id:
        s_can = P0_SESSIONS.get(chat_id)
        if s_can and s_can.get("dm_instruction_deferred"):
            _flush_deferred_dm_instruction_for_incident(chat_id)
    P0_SESSIONS.pop(chat_id, None)
    _session_disk.delete_session(chat_id)
    if token:
        release_dm_slots_for_incident_chat(chat_id, token)


def end_p0_session_by_meeting_no(meeting_no: str, token: Optional[str] = None) -> None:
    chat_id, _ = find_session_by_meeting_no(meeting_no)
    if not chat_id:
        log.warning("No active p0 session found for meeting_no=%s", meeting_no)
        return
    end_p0_session(chat_id, token)


def end_p0_session_by_meeting_ref(
    meeting_ref: str,
    token: Optional[str] = None,
    *,
    meeting_no_fallback: str = "",
) -> None:
    """Resolve session by long ``meeting.id`` or stored ref; optional ``meeting_no`` if join never bound."""
    meeting_ref = (meeting_ref or "").strip()
    chat_id, _ = find_session_by_meeting_ref(meeting_ref)
    if not chat_id and meeting_no_fallback:
        chat_id, _ = find_session_by_meeting_no(meeting_no_fallback.strip())
    if not chat_id:
        log.warning(
            "No active p0 session found for meeting_ref=%s meeting_no_fallback=%s",
            meeting_ref,
            meeting_no_fallback,
        )
        return
    end_p0_session(chat_id, token, vc_end_meeting_id=meeting_ref, skip_vc_end=True)


def cancel_p0_session_by_meeting_no(
    meeting_no: str,
    token: Optional[str] = None,
    reason: str = "Unspecified",
) -> None:
    chat_id, _ = find_session_by_meeting_no(meeting_no)
    if not chat_id:
        log.warning("No active p0 session found for meeting_no=%s", meeting_no)
        return
    cancel_p0_session(chat_id, token, reason=reason)


def _dm_instruction_targets(trigger_open_id: str) -> List[str]:
    """open_ids that receive the DM instruction card: env list if set, else [trigger] if any."""
    fixed = _config.get_dm_instruction_open_ids()
    if fixed:
        return fixed
    t = (trigger_open_id or "").strip()
    return [t] if t else []


def _parse_lark_api_code(raw: Any) -> int:
    """Lark responses use numeric ``code`` (often int, sometimes str). Never raises."""
    if raw is None:
        return -1
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return -1
        try:
            return int(s)
        except ValueError:
            return -1
    try:
        return int(raw)
    except (TypeError, ValueError, OverflowError):
        return -1


def _dm_instruction_lark_response_ok(st: int, resp_body: str) -> bool:
    """True when HTTP 200 and Lark ``code`` is 0 (or missing)."""
    if st != 200:
        return False
    try:
        j = json.loads(resp_body) if resp_body else {}
    except Exception:
        return False
    if not isinstance(j, dict):
        return False
    if "code" not in j:
        return True
    return _parse_lark_api_code(j.get("code")) == 0


def _send_dm_instruction_card_logged(
    open_id: str,
    tenant_token: str,
    priority: str,
    source_chat_label: str,
    context: str = "",
    *,
    target_chat: str = "",
    source_incident_chat_id: str = "",
    operator_lark_user_id: str = "",
) -> None:
    """
    Send the green **Build overview** DM instruction card.

    Prefers ``receive_id_type=user_id`` when ``operator_lark_user_id`` is set (same tenant id as group
    messages) — some tenants return HTTP 400 / 230099 for interactive cards via ``open_id`` only.
    Falls back to ``open_id`` if the user_id path fails.
    """
    oid = (open_id or "").strip()
    if not oid:
        return
    label = (context or "DM instruction").strip()
    card = _cards.build_dm_instruction_card(
        priority,
        source_chat_label=source_chat_label,
        target_chat=target_chat,
        source_incident_chat_id=source_incident_chat_id,
    )
    uid = (operator_lark_user_id or "").strip()
    attempts: List[Tuple[str, Any]] = []
    if uid:
        attempts.append(
            (
                "user_id",
                lambda: _lark.post_card_to_user_cross_app(oid, uid, tenant_token, card, use_user_id=True),
            )
        )
    attempts.append(("open_id", lambda: _lark.post_card_to_open_id(oid, tenant_token, card)))
    try:
        st, resp_body, mid = 0, "", ""
        mode_used = "open_id"
        for i, (name, fn) in enumerate(attempts):
            st, resp_body, mid = fn()
            if _dm_instruction_lark_response_ok(st, resp_body):
                mode_used = name
                break
            if i + 1 < len(attempts):
                log.warning(
                    "%s: %s path failed HTTP=%s open_id_tail=%s body=%s — retrying",
                    label,
                    name,
                    st,
                    oid[-8:] if len(oid) > 8 else oid,
                    (resp_body or "")[:400],
                )
        else:
            body_head = (resp_body or "")[:800]
            log.error(
                "%s failed after %s priority=%s open_id=%s body=%s",
                label,
                [a[0] for a in attempts],
                priority,
                oid,
                body_head,
            )
            return
        log.info(
            "%s sent OK via %s priority=%s open_id=%s message_id=%s",
            label,
            mode_used,
            priority,
            oid,
            (mid or "").strip() or "(none)",
        )
    except Exception as e:
        log.error("%s exception priority=%s open_id=%s err=%s", label, priority, oid, e)


def get_p1_prompt_pending(chat_id: str) -> Optional[Dict[str, Any]]:
    chat_id = (chat_id or "").strip()
    if not chat_id:
        return None
    with _P1_PROMPT_LOCK:
        p = P1_PROMPT_PENDING.get(chat_id)
        return dict(p) if p else None


def set_p1_prompt_pending(chat_id: str, trigger_open_id: str) -> str:
    """Store pending P1 meeting confirmation; returns nonce embedded in the Yes/No card buttons."""
    chat_id = (chat_id or "").strip()
    trigger_open_id = (trigger_open_id or "").strip()
    if not chat_id:
        return ""
    nonce = secrets.token_hex(8)
    with _P1_PROMPT_LOCK:
        P1_PROMPT_PENDING[chat_id] = {
            "trigger_open_id": trigger_open_id,
            "ts": int(time.time()),
            "nonce": nonce,
        }
    return nonce


def pop_p1_prompt_pending(chat_id: str) -> Optional[Dict[str, Any]]:
    chat_id = (chat_id or "").strip()
    if not chat_id:
        return None
    with _P1_PROMPT_LOCK:
        p = P1_PROMPT_PENDING.pop(chat_id, None)
        return dict(p) if p else None


def consume_p1_prompt_for_confirm(chat_id: str, nonce_from_button: str = "") -> Optional[Dict[str, Any]]:
    """
    Remove P1 meeting-confirm pending only if button nonce matches (stops stale card clicks).
    If payload has no nonce (legacy card), only consume when stored pending has no nonce.
    """
    chat_id = (chat_id or "").strip()
    want = (nonce_from_button or "").strip()
    if not chat_id:
        return None
    with _P1_PROMPT_LOCK:
        p = P1_PROMPT_PENDING.get(chat_id)
        if not p:
            return None
        stored = str(p.get("nonce") or "").strip()
        if want:
            if stored != want:
                log.warning("P1 confirm ignored: nonce mismatch chat_id=%s", chat_id)
                return None
        else:
            if stored:
                log.warning("P1 confirm ignored: card missing nonce but server expects one chat_id=%s", chat_id)
                return None
        P1_PROMPT_PENDING.pop(chat_id, None)
        return dict(p)


def request_p1_meeting_confirmation(chat_id: str, token: str, trigger_open_id: str) -> bool:
    """Post Yes/No card in the same chat as meeting cards (``get_session_meeting_card_post_chat_id``)."""
    chat_id = (chat_id or "").strip()
    token = (token or "").strip()
    if not chat_id or not token:
        return False
    pend = get_p1_prompt_pending(chat_id)
    nonce = str((pend or {}).get("nonce") or "").strip()
    if not nonce:
        log.error("request_p1_meeting_confirmation: no pending nonce for chat_id=%s", chat_id)
        return False
    prompt_chat = _config.get_session_meeting_card_post_chat_id(chat_id)
    st, body, _ = _lark.post_card_to_chat(prompt_chat, token, _cards.build_p1_meeting_confirm_card(nonce))
    if st != 200:
        log.error("request_p1_meeting_confirmation failed HTTP=%s body=%s", st, (body or "")[:500])
        return False
    return True


def chat_has_active_session(chat_id: str) -> bool:
    cid = (chat_id or "").strip()
    if not cid:
        return False
    if cid in P0_SESSIONS:
        return True
    if _session_disk.enabled():
        d = _session_disk.load_session(cid)
        if d:
            P0_SESSIONS[cid] = d
            return True
    return False


def dm_preview_allowed_for_incident(source_incident_chat_id: str, target_chat: str) -> bool:
    """
    DM overview (Build overview / Send to group) is tied to a live P0/P1 session for the incident group.
    Standalone ``create overview`` flows (no VC) stay allowed without a session.
    """
    src = (source_incident_chat_id or "").strip()
    tc = (target_chat or "").strip()
    if src == STANDALONE_DM_SOURCE_CHAT_ID:
        return True
    if src and src != STANDALONE_DM_SOURCE_CHAT_ID:
        return chat_has_active_session(src)
    if tc:
        _cid, sess = find_session_by_target_chat(tc)
        return bool(sess)
    return False


def handle_p1_meeting_confirm_yes(
    chat_id: str, token: str, fallback_trigger_open_id: str, nonce: str
) -> str:
    """
    Consume P1 "create meeting?" pending and start a P1 VC. Used by card **create** action and typed **create meeting**.

    Returns ``""`` on success, or ``"session_active"`` / ``"stale"``.
    """
    chat_id = (chat_id or "").strip()
    token = (token or "").strip()
    if not chat_id or not token:
        return "stale"
    if chat_has_active_session(chat_id):
        return "session_active"
    pending = consume_p1_prompt_for_confirm(chat_id, nonce)
    if not pending:
        return "stale"
    trigger = str(pending.get("trigger_open_id") or "").strip() or (fallback_trigger_open_id or "").strip()
    start_p0(chat_id, token, trigger, priority="P1")
    return ""


def handle_p1_meeting_confirm_no(chat_id: str, token: str, nonce: str) -> str:
    """
    Consume P1 prompt and skip VC. Returns ``""``, ``"session_active"``, or ``"stale"``.
    """
    chat_id = (chat_id or "").strip()
    token = (token or "").strip()
    if not chat_id or not token:
        return "stale"
    if chat_has_active_session(chat_id):
        return "session_active"
    pending = consume_p1_prompt_for_confirm(chat_id, nonce)
    if not pending:
        return "stale"
    _lark.post_text_to_chat(
        chat_id,
        token,
        "ℹ️ No P1 meeting will be created. Type **p1** in this group again when you need a new meeting.",
    )
    return ""


def p0_cooldown(chat_id: str) -> bool:
    chat_id = (chat_id or "").strip()
    if not chat_id:
        return False
    now = int(time.time())
    with _LAST_P0_LOCK:
        last = _LAST_P0_BY_CHAT.get(chat_id, 0)
        if now - last < P0_COOLDOWN_SEC:
            return True
        _LAST_P0_BY_CHAT[chat_id] = now
        return False


def p0_cooldown_remaining_sec(chat_id: str) -> int:
    """Seconds left before this chat can trigger p0/p1 again (0 if cooldown clear). Read-only."""
    chat_id = (chat_id or "").strip()
    if not chat_id:
        return 0
    now = int(time.time())
    with _LAST_P0_LOCK:
        last = _LAST_P0_BY_CHAT.get(chat_id, 0)
        elapsed = now - last
        if elapsed >= P0_COOLDOWN_SEC:
            return 0
        return int(P0_COOLDOWN_SEC - elapsed)


def _severity_second_app_active() -> bool:
    sid, sec = _config.get_lark_severity_app_credentials()
    pid, _ = _config.get_lark_primary_app_credentials()
    return bool(sid and sec and pid and sid != pid)


def _resolve_lark_user_id_for_dm(
    operator_open_id: str,
    sess: Dict[str, Any],
    explicit_lark_user_id: str,
) -> str:
    """Tenant user_id for cross-app DM; prefer event payload, then session trigger, then contact lookup."""
    ex = (explicit_lark_user_id or "").strip()
    if ex:
        return ex
    oid = (operator_open_id or "").strip()
    tr_open = str(sess.get("trigger_open_id") or "").strip()
    if oid and tr_open and oid == tr_open:
        u = str(sess.get("trigger_lark_user_id") or "").strip()
        if u:
            return u
    tok_p = _lark.get_tenant_token_primary()
    return _lark.get_tenant_user_id_by_open_id(tok_p, oid)


def clear_p0_cooldown(chat_id: str) -> None:
    """
    Drop the per-chat cooldown timestamp so **p0** / **p1** keywords can fire again
    without waiting — does **not** start a meeting.
    """
    chat_id = (chat_id or "").strip()
    if not chat_id:
        return
    with _LAST_P0_LOCK:
        _LAST_P0_BY_CHAT.pop(chat_id, None)
    log.info("clear_p0_cooldown chat_id=%s", chat_id)


def start_p0(
    chat_id: str,
    token: str,
    trigger_open_id: str,
    priority: str = "P0",
    source_chat_name: str = "",
    trigger_lark_user_id: str = "",
    silent_when_blocked: bool = False,
) -> None:
    """
    Create a new P0/P1 VC meeting session.

    ``silent_when_blocked`` — when True, do NOT post visible warnings to the source
    chat if blocked by an active session or cooldown. Just log and return. Use this
    from heuristic / keyword auto-trigger paths to avoid noisy false-positives in
    production incident groups (e.g. when someone re-pastes an overview template).
    Explicit user actions (P0 thread confirm, P1 confirm Yes) should leave this
    False so users get a clear reason why nothing happened.
    """
    from . import participants as _participants

    _config.reload_env_runtime()
    chat_id = (chat_id or "").strip()
    trigger_open_id = (trigger_open_id or "").strip()
    trigger_lark_user_id = (trigger_lark_user_id or "").strip()
    priority = (priority or "P0").strip().upper()
    if not chat_id:
        return
    pop_p1_prompt_pending(chat_id)
    _clear_last_ended_snapshot(chat_id)
    # Bot warnings during start: same chat as meeting cards (incident vs mirror — see config).
    notify_chat = _config.get_session_meeting_card_post_chat_id(chat_id)
    with _session_disk.exclusive_lock(chat_id):
        if P0_SESSIONS.get(chat_id):
            if silent_when_blocked:
                log.info("start_p0: blocked (session already active) silent chat_id=%s", chat_id)
                return
            _lark.post_text_to_chat(
                notify_chat,
                token,
                "ℹ️ A P0/P1 meeting session is already active in this group. Use **end meeting** or **cancel meeting** before declaring again.",
            )
            return
        sd = _session_disk.load_session(chat_id)
        if sd:
            P0_SESSIONS[chat_id] = sd
            if silent_when_blocked:
                log.info("start_p0: blocked (session loaded from disk) silent chat_id=%s", chat_id)
                return
            _lark.post_text_to_chat(
                notify_chat,
                token,
                "ℹ️ A P0/P1 meeting session is already active in this group. Use **end meeting** or **cancel meeting** before declaring again.",
            )
            return
        if p0_cooldown(chat_id):
            if silent_when_blocked:
                log.info("start_p0: blocked (cooldown active) silent chat_id=%s", chat_id)
                return
            total_min = max(1, (P0_COOLDOWN_SEC + 59) // 60)
            mins_label = "minute" if total_min == 1 else "minutes"
            msg = f"⚠️ Meeting was just created earlier — try again after {total_min} {mins_label}."
            _lark.post_text_to_chat(notify_chat, token, msg)
            return
        now = int(time.time())
        emergency_topic = _config.get_emergency_topic_for_source_chat(chat_id)
        vc_meeting_topic = _config.get_vc_meeting_topic_for_source_chat(chat_id)
        vc = _lark.create_vc_reserve(token, meeting_topic=vc_meeting_topic)
        link = (vc.get("link") or "").strip()
        if not link:
            _lark.post_text_to_chat(notify_chat, token, "❌ Failed to create Lark VC meeting (reserve/apply).")
            return
        target_chat = _config.get_session_meeting_card_post_chat_id(chat_id)
        chat_label = (source_chat_name or "").strip()
        if not chat_label:
            chat_label = _lark.get_group_chat_name(chat_id, token)
        affected_players = ""
        P0_SESSIONS[chat_id] = {
            "priority": priority,
            "start_epoch": now,
            "link": link,
            "reserve_id": vc.get("reserve_id", ""),
            "meeting_no": vc.get("meeting_no", ""),
            "meeting_id": vc.get("meeting_id", ""),
            "trigger_open_id": trigger_open_id,
            "trigger_lark_user_id": trigger_lark_user_id,
            "source_chat": chat_id,
            "target_chat": target_chat,
            "source_chat_name": chat_label,
            "emergency_topic": emergency_topic,
            "participants": [],
            "affected_players": affected_players,
            "vc_external_join_count": 0,
        }
        if trigger_open_id:
            try:
                host_label = _lark.lookup_user_name_by_open_id(token, trigger_open_id)
                if not host_label:
                    host_label = f"Host ({trigger_open_id[-6:]})"
                _participants.add_meeting_participant(host_label)
                log.info("Seeded host participant=%s for chat_id=%s", host_label, chat_id)
            except Exception as e:
                log.warning("Failed seeding fallback host participant open_id=%s err=%s", trigger_open_id, e)
        log.info("start session created priority=%s source_chat=%s target_chat=%s trigger_open_id=%s", priority, chat_id, target_chat, trigger_open_id)
        meeting_no = str(vc.get("meeting_no", "")).strip()
        st, body, invite_mid = _lark.post_card_to_chat(
            target_chat,
            token,
            _cards.build_meeting_card(
                link=link,
                meeting_no=meeting_no,
                priority=priority,
                affected_players=affected_players,
                emergency_topic=emergency_topic,
            ),
        )
        if st != 200:
            log.error("start_p0: meeting card failed HTTP=%s body=%s", st, (body or "")[:300])
            _lark.post_text_to_chat(notify_chat, token, "❌ Failed to post meeting card.")
            P0_SESSIONS.pop(chat_id, None)
            return
        if invite_mid:
            P0_SESSIONS[chat_id]["meeting_invite_message_id"] = invite_mid
        if _session_disk.enabled():
            _session_disk.save_session(chat_id, P0_SESSIONS[chat_id])
    dm_targets = _dm_instruction_targets(trigger_open_id)
    log.info(
        "start_p0 DM targets count=%s open_ids=%s (API expects open_id ou_..., not user_id gceda344-style)",
        len([x for x in dm_targets if (x or "").strip()]),
        [x for x in dm_targets if (x or "").strip()],
    )
    dm_targets_list = [x for x in dm_targets if (x or "").strip()]
    # Severity (2nd bot) only for **P0**. P1 sessions get the usual primary-bot overview DM immediately.
    use_severity_for_session = (
        _config.slack_severity_prompt_enabled()
        and dm_targets_list
        and priority == "P0"
    )
    # Severity bot (P0 only) can DM in parallel with the primary bot's green overview card.
    try:
        from .slack_bridge import run_slack_p0_notify_and_huddle

        if use_severity_for_session:
            P0_SESSIONS[chat_id]["slack_severity"] = "pending"
            if _session_disk.enabled():
                _session_disk.save_session(chat_id, P0_SESSIONS[chat_id])
            card = _cards.build_slack_severity_prompt_card(
                source_incident_chat_id=chat_id,
                target_chat=target_chat,
                group_label=chat_label,
                priority=priority,
            )
            s_id, s_sec = _config.get_lark_severity_app_credentials()
            p_id, _p = _config.get_lark_primary_app_credentials()
            log.info(
                "start_p0 severity DM: secondary_env_ok=%s severity_app_tail=%s primary_app_tail=%s "
                "severity_same_app_as_primary=%s (if True, use separate LARK_SEVERITY_APP_ID for dedicated severity bot)",
                bool(s_id and s_sec),
                (s_id[-12:] if s_id else "MISSING"),
                (p_id[-12:] if p_id else "none"),
                bool(s_id and p_id and s_id == p_id),
            )
            tok_sev = _lark.get_tenant_token_for_severity_dm()
            tok_pri = _lark.get_tenant_token_primary()
            sess_snap = P0_SESSIONS.get(chat_id) or {}
            for oid in dm_targets_list:
                uid = _resolve_lark_user_id_for_dm(oid, sess_snap, trigger_lark_user_id if oid == trigger_open_id else "")
                use_uid = _severity_second_app_active() and bool(uid)
                if _severity_second_app_active() and not uid:
                    # Second app cannot address user by open_id alone — without user_id we used to skip (looked like "overview only").
                    # Same severity card from the primary bot: open_id works; Major/Minor still route to this service.
                    log.warning(
                        "start_p0: no tenant user_id for cross-app severity DM open_id_tail=%s — "
                        "posting severity card from primary bot instead",
                        oid[-12:] if oid else "",
                    )
                    st, body, _mid = _lark.post_card_to_open_id(oid, tok_pri, card)
                else:
                    st, body, _mid = _lark.post_card_to_user_cross_app(
                        oid, uid, tok_sev, card, use_user_id=use_uid
                    )
                if st != 200:
                    log.warning(
                        "start_p0: slack severity card HTTP=%s open_id=%s body=%s",
                        st,
                        oid,
                        (body or "")[:300],
                    )
                else:
                    try:
                        jb = json.loads(body or "{}")
                        if isinstance(jb, dict) and jb.get("code") not in (0, None):
                            log.warning(
                                "start_p0: severity card Lark API code=%s msg=%s (still using this token; check bot / permission)",
                                jb.get("code"),
                                jb.get("msg"),
                            )
                    except Exception:
                        pass
            if _config.slack_notify_channel_on_p0_declare_when_severity_prompt():
                try:
                    from .slack_bridge import notify_slack_p0_started

                    notify_slack_p0_started(chat_id, priority, chat_label)
                    P0_SESSIONS[chat_id]["slack_p0_channel_ping_sent"] = True
                    if _session_disk.enabled():
                        _session_disk.save_session(chat_id, P0_SESSIONS[chat_id])
                except Exception as e_decl:
                    log.warning("start_p0: notify_slack_p0_started (declare-time) failed: %s", e_decl)
        else:
            run_slack_p0_notify_and_huddle(chat_id, priority, chat_label)
    except Exception as e:
        log.warning("start_p0: slack bridge hook failed: %s", e)
    try:
        from .graph_screenshot import schedule_p0_graph_screenshot

        schedule_p0_graph_screenshot(token, priority, chat_label)
    except Exception as e:
        log.warning("start_p0: graph screenshot hook failed: %s", e)
    # Primary bot always DM's the green overview card (same declaration as severity bot for P0 when enabled).
    for oid in dm_targets:
        if not oid:
            continue
        dm_item: Dict[str, Any] = {
            "chat_id": chat_id,
            "target_chat": target_chat,
            "priority": priority,
            "label": chat_label,
        }
        if oid == trigger_open_id and trigger_lark_user_id:
            dm_item["operator_lark_user_id"] = trigger_lark_user_id
        enqueue_dm_instruction_if_needed(oid, token, dm_item)
    try:
        schedule_vc_auto_cancel_if_no_external_joins(chat_id)
    except Exception as e:
        log.warning("start_p0: schedule vc auto-cancel failed: %s", e)


def slack_cross_post_slack_enabled_for_incident_chat(chat_id: str) -> bool:
    """
    When ``SLACK_SEVERITY_PROMPT_BEFORE_AUTOMATION`` is on, **huddle** triggered from ``send_preview``
    (see ``SLACK_HUDDLE_ON_OVERVIEW_SEND``) runs unless the operator chose **Minor**.

    **Pending** (Major/Minor not answered yet) or **Major** → allowed. **Minor** → skipped.

    **Declare-time** Slack channel ping (when enabled) happens in ``start_p0`` and is separate from this.

    **Overview text mirror** to Slack on ``send_preview`` is **not** gated by this (see ``handlers.send_preview``).

    **P1** sessions never use the severity DM (see ``start_p0``); treat them like severity is off.
    """
    if not _config.slack_severity_prompt_enabled():
        return True
    sess = P0_SESSIONS.get((chat_id or "").strip())
    if not sess:
        return False
    pr = str(sess.get("priority") or "P0").strip().upper()
    if pr == "P1":
        return True
    sev = (sess.get("slack_severity") or "pending").strip().lower()
    return sev != "minor"


def _dm_instruction_item_from_session(chat_id: str, sess: Dict[str, Any]) -> Dict[str, Any]:
    pr = str(sess.get("priority") or "P0").strip().upper()
    if pr not in ("P0", "P1"):
        pr = "P0"
    return {
        "chat_id": chat_id,
        "target_chat": str(sess.get("target_chat") or "").strip(),
        "priority": pr,
        "label": str(sess.get("source_chat_name") or "").strip(),
    }


def _flush_deferred_dm_instruction_for_incident(chat_id: str) -> None:
    """Legacy: post the green DM if ``dm_instruction_deferred`` is still set (older sessions / cancel+end flush)."""
    cid = (chat_id or "").strip()
    if not cid.startswith("oc_"):
        return
    tok = _lark.get_tenant_token_primary()
    if not tok:
        log.warning("_flush_deferred_dm_instruction_for_incident: no primary tenant token chat_id=%s", cid)
        return
    sess = P0_SESSIONS.get(cid)
    if not sess or not sess.get("dm_instruction_deferred"):
        return
    sess["dm_instruction_deferred"] = False
    if _session_disk.enabled():
        _session_disk.save_session(cid, sess)
    item = _dm_instruction_item_from_session(cid, sess)
    trigger = str(sess.get("trigger_open_id") or "").strip()
    for oid in _dm_instruction_targets(trigger):
        if oid:
            enqueue_dm_instruction_if_needed(oid, tok, item)


def apply_slack_severity_choice(
    chat_id: str,
    token: str,
    sender_open_id: str,
    is_major: bool,
    operator_lark_user_id: str = "",
) -> Optional[str]:
    """
    Handle Major/Minor DM button. Returns ``None`` on success, or
    ``no_session`` / ``already_answered`` / ``disabled`` / ``missing_user_id``.

    ``operator_lark_user_id`` (tenant ``user_id``, e.g. ``SNT0006``) is required for a **second** Lark app
    sending DMs — ``open_id`` is app-scoped (99992361 ``open_id cross app``).
    """
    if not _config.slack_severity_prompt_enabled():
        return "disabled"
    cid = (chat_id or "").strip()
    if not cid.startswith("oc_"):
        return "no_session"
    sess = P0_SESSIONS.get(cid)
    if not sess:
        return "no_session"
    sev = (sess.get("slack_severity") or "pending").strip().lower()
    if sev not in ("pending", ""):
        return "already_answered"
    luid = _resolve_lark_user_id_for_dm(sender_open_id, sess, operator_lark_user_id or "")
    if _severity_second_app_active() and not luid:
        return "missing_user_id"
    severity = "major" if is_major else "minor"
    sess["slack_severity"] = severity
    if not is_major:
        sess["slack_minor_phase"] = "role_active"
    if _session_disk.enabled():
        _session_disk.save_session(cid, sess)
    if is_major:
        label = str(sess.get("source_chat_name") or "").strip()
        pr = str(sess.get("priority") or "P0").strip().upper()
        if pr not in ("P0", "P1"):
            pr = "P0"
        try:
            from .slack_bridge import enqueue_slack_huddle_automation, run_slack_p0_notify_and_huddle

            if sess.get("slack_p0_channel_ping_sent"):
                if _config.slack_huddle_on_p0_start():
                    enqueue_slack_huddle_automation(cid, pr)
                else:
                    log.warning(
                        "apply_slack_severity_choice: huddle NOT started (SLACK_HUDDLE_ON_P0_START=0) chat_id=%s",
                        cid,
                    )
            else:
                run_slack_p0_notify_and_huddle(cid, pr, label)
        except Exception as e:
            log.warning("apply_slack_severity_choice: Slack hook failed: %s", e)
    elif (sender_open_id or "").strip():
        pr = str(sess.get("priority") or "P0").strip().upper()
        if pr not in ("P0", "P1"):
            pr = "P0"
        lab = str(sess.get("source_chat_name") or "").strip()
        tgt = str(sess.get("target_chat") or "").strip()
        card = _cards.build_slack_minor_role_prompt_card(
            source_incident_chat_id=cid,
            target_chat=tgt,
            group_label=lab,
            priority=pr,
        )
        tok_sev = _lark.get_tenant_token_for_severity_dm()
        use_uid = _severity_second_app_active() and bool(luid)
        st, body, _mid = _lark.post_card_to_user_cross_app(
            sender_open_id, luid, tok_sev, card, use_user_id=use_uid
        )
        if st != 200:
            log.warning(
                "apply_slack_severity_choice: minor role card HTTP=%s open_id=%s body=%s",
                st,
                sender_open_id,
                (body or "")[:300],
            )
    if (sender_open_id or "").strip():
        tok_sev = _lark.get_tenant_token_for_severity_dm()
        use_uid = _severity_second_app_active() and bool(luid)
        if is_major:
            if sess.get("slack_p0_channel_ping_sent"):
                msg = (
                    "✅ Recorded as **Major** — huddle automation started if enabled "
                    "(Slack channel was already notified when P0 was declared)."
                )
            else:
                msg = (
                    "✅ Recorded as **Major** — Slack notified and huddle automation started (if enabled)."
                )
        else:
            if sess.get("slack_p0_channel_ping_sent"):
                msg = (
                    "✅ Recorded as **Minor** — optional huddle/OM automation skipped "
                    "(Slack channel may have been pinged when P0 was declared).\n"
                    "Use the card above to choose SRE BACKEND / SRE FE / No need (duty → Slack is stubbed)."
                )
            else:
                msg = (
                    "✅ Recorded as **Minor** — Slack automation skipped.\n"
                    "Use the card above to choose SRE BACKEND / SRE FE / No need (duty → Slack is stubbed)."
                )
        _lark.post_text_to_user_cross_app(sender_open_id, luid, tok_sev, msg, use_user_id=use_uid)
    return None


def apply_slack_minor_card_action(
    chat_id: str,
    token: str,
    sender_open_id: str,
    action_name: str,
    operator_lark_user_id: str = "",
) -> Optional[str]:
    """
    Minor follow-up cards after severity. Returns ``None`` on success, or
    ``disabled`` / ``no_session`` / ``not_minor`` / ``already_done`` / ``bad_phase`` /
    ``unknown_action`` / ``missing_user_id``.
    """
    if not _config.slack_severity_prompt_enabled():
        return "disabled"
    cid = (chat_id or "").strip()
    soid = (sender_open_id or "").strip()
    tok = _lark.get_tenant_token_for_severity_dm()
    if not cid.startswith("oc_") or not soid or not tok:
        return "no_session"
    sess = P0_SESSIONS.get(cid)
    if not sess:
        return "no_session"
    if (sess.get("slack_severity") or "").strip().lower() != "minor":
        return "not_minor"
    phase = str(sess.get("slack_minor_phase") or "").strip().lower()
    if phase == "done":
        return "already_done"
    act = (action_name or "").strip().lower()
    lab = str(sess.get("source_chat_name") or "").strip()
    pr = str(sess.get("priority") or "P0").strip().upper()
    if pr not in ("P0", "P1"):
        pr = "P0"
    tgt = str(sess.get("target_chat") or "").strip()
    luid = _resolve_lark_user_id_for_dm(soid, sess, operator_lark_user_id or "")
    use_uid = _severity_second_app_active() and bool(luid)
    if _severity_second_app_active() and not luid:
        return "missing_user_id"

    def _stub_backend(team: str) -> str:
        return (
            f"🔔 Stub: **{team}** backend duty → Slack ping is **not wired** yet "
            f"(Lark bot would resolve on-call for the date, then notify Slack)."
        )

    def _stub_fe() -> str:
        return "🔔 Stub: **FE** duty → Slack ping is **not wired** yet."

    if act == "slack_minor_sre_backend":
        if phase != "role_active":
            return "bad_phase"
        sess["slack_minor_phase"] = "backend_team_active"
        if _session_disk.enabled():
            _session_disk.save_session(cid, sess)
        card = _cards.build_slack_minor_backend_team_card(
            source_incident_chat_id=cid,
            target_chat=tgt,
            group_label=lab,
            priority=pr,
        )
        st, body, _ = _lark.post_card_to_user_cross_app(soid, luid, tok, card, use_user_id=use_uid)
        if st != 200:
            log.warning("apply_slack_minor: backend team card HTTP=%s body=%s", st, (body or "")[:300])
        return None

    if act == "slack_minor_sre_fe":
        if phase != "role_active":
            return "bad_phase"
        sess["slack_minor_phase"] = "fe_active"
        if _session_disk.enabled():
            _session_disk.save_session(cid, sess)
        card = _cards.build_slack_minor_fe_reach_card(
            source_incident_chat_id=cid,
            target_chat=tgt,
            group_label=lab,
            priority=pr,
        )
        st, body, _ = _lark.post_card_to_user_cross_app(soid, luid, tok, card, use_user_id=use_uid)
        if st != 200:
            log.warning("apply_slack_minor: fe card HTTP=%s body=%s", st, (body or "")[:300])
        return None

    if act == "slack_minor_no_need":
        if phase != "role_active":
            return "bad_phase"
        sess["slack_minor_phase"] = "done"
        if _session_disk.enabled():
            _session_disk.save_session(cid, sess)
        _lark.post_text_to_user_cross_app(
            soid,
            luid,
            tok,
            "✅ **No SRE reach-out** recorded. Use the green **Send overview** card from the primary bot DM if you still need to post an overview.",
            use_user_id=use_uid,
        )
        return None

    if act in ("slack_minor_team_fpms", "slack_minor_team_cpms", "slack_minor_team_pms"):
        if phase != "backend_team_active":
            return "bad_phase"
        team = {"slack_minor_team_fpms": "FPMS", "slack_minor_team_cpms": "CPMS", "slack_minor_team_pms": "PMS"}[act]
        sess["slack_minor_phase"] = "done"
        sess["slack_minor_backend_team"] = team
        if _session_disk.enabled():
            _session_disk.save_session(cid, sess)
        _lark.post_text_to_user_cross_app(soid, luid, tok, _stub_backend(team), use_user_id=use_uid)
        return None

    if act == "slack_minor_fe_reach":
        if phase != "fe_active":
            return "bad_phase"
        sess["slack_minor_phase"] = "done"
        if _session_disk.enabled():
            _session_disk.save_session(cid, sess)
        _lark.post_text_to_user_cross_app(soid, luid, tok, _stub_fe(), use_user_id=use_uid)
        return None

    return "unknown_action"
