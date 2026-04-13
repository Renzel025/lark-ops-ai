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


def note_if_standalone_create_overview_blocked(operator_open_id: str) -> str:
    """
    If non-empty, DM this text instead of enqueueing another standalone ``create overview``.
    Covers: active incident slot, duplicate standalone, draft tied to a live incident.
    """
    oid = (operator_open_id or "").strip()
    if not oid:
        return ""
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
    norm = {"chat_id": chat_id, "target_chat": target_chat, "priority": priority, "label": label}
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
        _drafts.seed_draft_for_incident(oid, tc, cid, draft_priority=pr)
        _send_dm_instruction_card_logged(
            oid,
            token,
            pr,
            lab,
            context="queued DM instruction",
            target_chat=tc,
            source_incident_chat_id=cid,
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
        _drafts.seed_draft_for_incident(oid, tc, cid, draft_priority=pr)
        _send_dm_instruction_card_logged(
            oid,
            token,
            pr,
            lab,
            context="queued DM after standalone cancel",
            target_chat=tc,
            source_incident_chat_id=cid,
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
    meeting_ref = (meeting_ref or "").strip()
    if not meeting_ref or not P0_SESSIONS:
        return
    last_key = list(P0_SESSIONS.keys())[-1]
    sess = P0_SESSIONS.get(last_key) or {}
    sess["meeting_id"] = meeting_ref
    P0_SESSIONS[last_key] = sess
    if _session_disk.enabled():
        _session_disk.save_session(last_key, sess)
    log.info("Bound live meeting_id=%s to chat_id=%s", meeting_ref, last_key)


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
        if not patched:
            try:
                _lark.post_card_to_chat(
                    chat_id,
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
            _lark.post_text_to_chat(chat_id, token, summary)
        except Exception as e:
            log.warning("post_text meeting ended summary failed chat_id=%s err=%s", chat_id, e)
    if token and chat_id:
        s_end = P0_SESSIONS.get(chat_id)
        if s_end and s_end.get("dm_instruction_deferred"):
            _flush_deferred_dm_instruction_for_incident(chat_id, token)
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
        if not patched:
            try:
                _lark.post_card_to_chat(
                    chat_id,
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
            _flush_deferred_dm_instruction_for_incident(chat_id, token)
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


def _send_dm_instruction_card_logged(
    open_id: str,
    tenant_token: str,
    priority: str,
    source_chat_label: str,
    context: str = "",
    *,
    target_chat: str = "",
    source_incident_chat_id: str = "",
) -> None:
    """
    Send the overview / DM instruction card to one user (``receive_id_type=open_id``).

    Logs HTTP status and Lark ``code``/``msg`` so silent failures (e.g. 200 + code 9499) are visible.
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
    try:
        st, resp_body, mid = _lark.post_card_to_open_id(oid, tenant_token, card)
        body_head = (resp_body or "")[:800]
        if st != 200:
            log.error(
                "%s failed HTTP=%s priority=%s open_id=%s body=%s",
                label,
                st,
                priority,
                oid,
                body_head,
            )
            return
        try:
            j = json.loads(resp_body) if resp_body else {}
        except Exception:
            j = {}
        if isinstance(j, dict) and "code" in j:
            api_code = _parse_lark_api_code(j.get("code"))
            if api_code != 0:
                log.error(
                    "%s Lark API code=%s msg=%r priority=%s open_id=%s",
                    label,
                    j.get("code"),
                    j.get("msg"),
                    priority,
                    oid,
                )
                return
        log.info(
            "%s sent OK priority=%s open_id=%s message_id=%s",
            label,
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
    """Post Yes/No card in the incident group; caller must set pending first."""
    chat_id = (chat_id or "").strip()
    token = (token or "").strip()
    if not chat_id or not token:
        return False
    pend = get_p1_prompt_pending(chat_id)
    nonce = str((pend or {}).get("nonce") or "").strip()
    if not nonce:
        log.error("request_p1_meeting_confirmation: no pending nonce for chat_id=%s", chat_id)
        return False
    st, body, _ = _lark.post_card_to_chat(chat_id, token, _cards.build_p1_meeting_confirm_card(nonce))
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
) -> None:
    from . import participants as _participants

    _config.reload_env_runtime()
    chat_id = (chat_id or "").strip()
    trigger_open_id = (trigger_open_id or "").strip()
    priority = (priority or "P0").strip().upper()
    if not chat_id:
        return
    pop_p1_prompt_pending(chat_id)
    _clear_last_ended_snapshot(chat_id)
    with _session_disk.exclusive_lock(chat_id):
        if P0_SESSIONS.get(chat_id):
            _lark.post_text_to_chat(
                chat_id,
                token,
                "ℹ️ A P0/P1 meeting session is already active in this group. Use **end meeting** or **cancel meeting** before declaring again.",
            )
            return
        sd = _session_disk.load_session(chat_id)
        if sd:
            P0_SESSIONS[chat_id] = sd
            _lark.post_text_to_chat(
                chat_id,
                token,
                "ℹ️ A P0/P1 meeting session is already active in this group. Use **end meeting** or **cancel meeting** before declaring again.",
            )
            return
        if p0_cooldown(chat_id):
            total_min = max(1, (P0_COOLDOWN_SEC + 59) // 60)
            mins_label = "minute" if total_min == 1 else "minutes"
            msg = f"⚠️ Meeting was just created earlier — try again after {total_min} {mins_label}."
            _lark.post_text_to_chat(chat_id, token, msg)
            return
        now = int(time.time())
        emergency_topic = _config.get_emergency_topic_for_source_chat(chat_id)
        vc_meeting_topic = _config.get_vc_meeting_topic_for_source_chat(chat_id)
        vc = _lark.create_vc_reserve(token, meeting_topic=vc_meeting_topic)
        link = (vc.get("link") or "").strip()
        if not link:
            _lark.post_text_to_chat(chat_id, token, "❌ Failed to create Lark VC meeting (reserve/apply).")
            return
        target_chat = _config.get_overview_post_chat_id() or chat_id
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
            "source_chat": chat_id,
            "target_chat": target_chat,
            "source_chat_name": chat_label,
            "emergency_topic": emergency_topic,
            "participants": [],
            "affected_players": affected_players,
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
            chat_id,
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
            _lark.post_text_to_chat(chat_id, token, "❌ Failed to post meeting card.")
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
    item = {
        "chat_id": chat_id,
        "target_chat": target_chat,
        "priority": priority,
        "label": chat_label,
    }
    dm_targets_list = [x for x in dm_targets if (x or "").strip()]
    # Severity prompt before the usual DM instruction card so operators see Major/Minor first.
    try:
        from .slack_bridge import run_slack_p0_notify_and_huddle

        if _config.slack_severity_prompt_enabled() and dm_targets_list:
            P0_SESSIONS[chat_id]["slack_severity"] = "pending"
            P0_SESSIONS[chat_id]["dm_instruction_deferred"] = True
            if _session_disk.enabled():
                _session_disk.save_session(chat_id, P0_SESSIONS[chat_id])
            card = _cards.build_slack_severity_prompt_card(
                source_incident_chat_id=chat_id,
                target_chat=target_chat,
                group_label=chat_label,
                priority=priority,
            )
            for oid in dm_targets_list:
                st, body, _mid = _lark.post_card_to_open_id(oid, token, card)
                if st != 200:
                    log.warning(
                        "start_p0: slack severity card HTTP=%s open_id=%s body=%s",
                        st,
                        oid,
                        (body or "")[:300],
                    )
        else:
            run_slack_p0_notify_and_huddle(chat_id, priority, chat_label)
    except Exception as e:
        log.warning("start_p0: slack bridge hook failed: %s", e)
    # When severity DM is shown first, defer the green "Send overview" card until Major/Minor (and minor sub-flow) complete.
    if not (_config.slack_severity_prompt_enabled() and dm_targets_list):
        for oid in dm_targets:
            if not oid:
                continue
            enqueue_dm_instruction_if_needed(oid, token, item)


def slack_cross_post_slack_enabled_for_incident_chat(chat_id: str) -> bool:
    """
    When ``SLACK_SEVERITY_PROMPT_BEFORE_AUTOMATION`` is on, Slack notify / overview mirror / huddle
    only run if the operator chose **Major**. **Minor** or **pending** → no Slack automation.
    """
    if not _config.slack_severity_prompt_enabled():
        return True
    sess = P0_SESSIONS.get((chat_id or "").strip())
    if not sess:
        return False
    sev = (sess.get("slack_severity") or "pending").strip().lower()
    return sev == "major"


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


def _flush_deferred_dm_instruction_for_incident(chat_id: str, token: str) -> None:
    """Post the green DM instruction card if it was deferred for the severity-first flow."""
    cid = (chat_id or "").strip()
    tok = (token or "").strip()
    if not cid.startswith("oc_") or not tok:
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
) -> Optional[str]:
    """
    Handle Major/Minor DM button. Returns ``None`` on success, or ``no_session`` / ``already_answered`` / ``disabled``.
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
            from .slack_bridge import run_slack_p0_notify_and_huddle

            run_slack_p0_notify_and_huddle(cid, pr, label)
        except Exception as e:
            log.warning("apply_slack_severity_choice: Slack hook failed: %s", e)
        _flush_deferred_dm_instruction_for_incident(cid, token)
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
        st, body, _mid = _lark.post_card_to_open_id(sender_open_id, token, card)
        if st != 200:
            log.warning(
                "apply_slack_severity_choice: minor role card HTTP=%s open_id=%s body=%s",
                st,
                sender_open_id,
                (body or "")[:300],
            )
    if (sender_open_id or "").strip():
        if is_major:
            msg = (
                "✅ Recorded as **Major** — Slack notified and huddle automation started (if enabled)."
            )
        else:
            msg = (
                "✅ Recorded as **Minor** — Slack automation skipped.\n"
                "Use the card above to choose SRE BACKEND / SRE FE / No need (duty → Slack is stubbed)."
            )
        _lark.post_text_to_open_id(sender_open_id, token, msg)
    return None


def apply_slack_minor_card_action(
    chat_id: str,
    token: str,
    sender_open_id: str,
    action_name: str,
) -> Optional[str]:
    """
    Minor follow-up cards after severity. Returns ``None`` on success, or
    ``disabled`` / ``no_session`` / ``not_minor`` / ``already_done`` / ``bad_phase`` / ``unknown_action``.
    """
    if not _config.slack_severity_prompt_enabled():
        return "disabled"
    cid = (chat_id or "").strip()
    tok = (token or "").strip()
    soid = (sender_open_id or "").strip()
    if not cid.startswith("oc_") or not tok or not soid:
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
        st, body, _ = _lark.post_card_to_open_id(soid, tok, card)
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
        st, body, _ = _lark.post_card_to_open_id(soid, tok, card)
        if st != 200:
            log.warning("apply_slack_minor: fe card HTTP=%s body=%s", st, (body or "")[:300])
        return None

    if act == "slack_minor_no_need":
        if phase != "role_active":
            return "bad_phase"
        sess["slack_minor_phase"] = "done"
        if _session_disk.enabled():
            _session_disk.save_session(cid, sess)
        _flush_deferred_dm_instruction_for_incident(cid, tok)
        _lark.post_text_to_open_id(
            soid,
            tok,
            "✅ **No SRE reach-out** recorded. The DM overview card should appear if it was deferred.",
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
        _flush_deferred_dm_instruction_for_incident(cid, tok)
        _lark.post_text_to_open_id(soid, tok, _stub_backend(team))
        return None

    if act == "slack_minor_fe_reach":
        if phase != "fe_active":
            return "bad_phase"
        sess["slack_minor_phase"] = "done"
        if _session_disk.enabled():
            _session_disk.save_session(cid, sess)
        _flush_deferred_dm_instruction_for_incident(cid, tok)
        _lark.post_text_to_open_id(soid, tok, _stub_fe())
        return None

    return "unknown_action"
