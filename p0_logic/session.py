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
from . import support as _support

log = logging.getLogger("lark-ops-ai")

P0_SESSIONS: Dict[str, Dict[str, Any]] = {}
_LAST_P0_BY_CHAT: Dict[str, int] = {}
_LAST_P0_LOCK = threading.Lock()
_ONGOING_TIMERS: Dict[str, threading.Timer] = {}
_ONGOING_TIMERS_LOCK = threading.Lock()
_ESCALATION_TIMERS: Dict[str, threading.Timer] = {}
_ESCALATION_TIMERS_LOCK = threading.Lock()

# P1 keyword: waiting for Yes/No on "create meeting?" card (keyed by incident group chat_id)
P1_PROMPT_PENDING: Dict[str, Dict[str, Any]] = {}
_P1_PROMPT_LOCK = threading.Lock()

# Last successful ``end_p0_session`` fields per incident chat (in-memory) — replay ended card if user types "end" again.
_LAST_ENDED_SNAPSHOT_BY_CHAT: Dict[str, Dict[str, str]] = {}
_LAST_ENDED_SNAPSHOT_LOCK = threading.Lock()

ONGOING_CARD_DELAY_SEC = _config.ONGOING_CARD_DELAY_SEC
P1_TO_P0_ESCALATION_SEC = _config.P1_TO_P0_ESCALATION_SEC
P0_COOLDOWN_SEC = _config.P0_COOLDOWN_SEC


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


def _cancel_ongoing_timer(chat_id: str) -> None:
    chat_id = (chat_id or "").strip()
    with _ONGOING_TIMERS_LOCK:
        t = _ONGOING_TIMERS.pop(chat_id, None)
    if t:
        try:
            t.cancel()
        except Exception:
            pass


def _cancel_escalation_timer(chat_id: str) -> None:
    chat_id = (chat_id or "").strip()
    with _ESCALATION_TIMERS_LOCK:
        t = _ESCALATION_TIMERS.pop(chat_id, None)
    if t:
        try:
            t.cancel()
        except Exception:
            pass


def _participant_teams_text(sess: Dict[str, Any], tenant_token: str) -> str:
    """Ongoing-meeting card: unique departments from SUPPORT sheet (A=name, B=dept), e.g. ``FPMS, FE``."""
    from . import participants as _participants

    participants = list(sess.get("participants") or [])
    return _participants.departments_line_from_names(participants, tenant_token)


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
    log.info("Bound live meeting_id=%s to chat_id=%s", meeting_ref, last_key)


def end_p0_session(chat_id: str, token: Optional[str] = None) -> None:
    chat_id = (chat_id or "").strip()
    sess = P0_SESSIONS.get(chat_id) or {}
    _cancel_ongoing_timer(chat_id)
    _cancel_escalation_timer(chat_id)
    if sess:
        meeting_no_snap = str(sess.get("meeting_no") or "").strip()
        start_epoch_snap = int(sess.get("start_epoch") or 0)
        duration_snap = _cards.format_duration(start_epoch_snap)
        priority_snap = str(sess.get("priority") or "P0").strip().upper()
        em_snap = str(sess.get("emergency_topic") or "").strip()
        _store_last_ended_snapshot(chat_id, meeting_no_snap, duration_snap, priority_snap, em_snap)
    if token and sess:
        # End the live VC on Lark (same as user clicking End in the client) so recording / Meeting Assistant flows run.
        meeting_id = str(sess.get("meeting_id") or "").strip()
        reserve_id = str(sess.get("reserve_id") or "").strip()
        if meeting_id:
            if not _lark.end_vc_meeting(token, meeting_id):
                log.warning("end_p0_session: end_vc_meeting did not succeed meeting_id=%s", meeting_id)
        elif reserve_id:
            _lark.delete_vc_reserve(token, reserve_id)
        meeting_no = str(sess.get("meeting_no") or "").strip()
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
    P0_SESSIONS.pop(chat_id, None)


def cancel_p0_session(
    chat_id: str,
    token: Optional[str] = None,
    reason: str = "Unspecified",
) -> None:
    chat_id = (chat_id or "").strip()
    sess = P0_SESSIONS.get(chat_id) or {}
    _cancel_ongoing_timer(chat_id)
    _cancel_escalation_timer(chat_id)
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
    P0_SESSIONS.pop(chat_id, None)


def end_p0_session_by_meeting_no(meeting_no: str, token: Optional[str] = None) -> None:
    chat_id, _ = find_session_by_meeting_no(meeting_no)
    if not chat_id:
        log.warning("No active p0 session found for meeting_no=%s", meeting_no)
        return
    end_p0_session(chat_id, token)


def end_p0_session_by_meeting_ref(meeting_ref: str, token: Optional[str] = None) -> None:
    chat_id, _ = find_session_by_meeting_ref(meeting_ref)
    if not chat_id:
        log.warning("No active p0 session found for meeting_ref=%s", meeting_ref)
        return
    end_p0_session(chat_id, token)


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
) -> None:
    """
    Send the overview / DM instruction card to one user (``receive_id_type=open_id``).

    Logs HTTP status and Lark ``code``/``msg`` so silent failures (e.g. 200 + code 9499) are visible.
    """
    oid = (open_id or "").strip()
    if not oid:
        return
    label = (context or "DM instruction").strip()
    card = _cards.build_dm_instruction_card(priority, source_chat_label=source_chat_label)
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
    if chat_id in P0_SESSIONS:
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
    if chat_id in P0_SESSIONS:
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


def _schedule_ongoing_meeting_card(chat_id: str, token: str) -> None:
    chat_id = (chat_id or "").strip()
    if not chat_id:
        return
    _cancel_ongoing_timer(chat_id)

    def run() -> None:
        try:
            sess = P0_SESSIONS.get(chat_id) or {}
            if not sess:
                return
            if str(sess.get("priority") or "").strip().upper() != "P0":
                return
            meeting_no = str(sess.get("meeting_no") or "").strip()
            participant_depts_line = _participant_teams_text(sess, token)
            em_topic = str(sess.get("emergency_topic") or "").strip()
            card = _cards.build_ongoing_meeting_card(
                meeting_no, participant_depts_line, "P0", emergency_topic=em_topic
            )
            st, body, _ = _lark.post_card_to_chat(chat_id, token, card)
            if st != 200:
                log.error("ongoing meeting card failed HTTP=%s body=%s", st, (body or "")[:500])
        finally:
            with _ONGOING_TIMERS_LOCK:
                _ONGOING_TIMERS.pop(chat_id, None)

    timer = threading.Timer(ONGOING_CARD_DELAY_SEC, run)
    timer.daemon = True
    with _ONGOING_TIMERS_LOCK:
        _ONGOING_TIMERS[chat_id] = timer
    timer.start()


def _schedule_p1_escalation_card(chat_id: str, token: str) -> None:
    chat_id = (chat_id or "").strip()
    if not chat_id:
        return
    _cancel_escalation_timer(chat_id)

    def run() -> None:
        try:
            sess = P0_SESSIONS.get(chat_id) or {}
            if not sess:
                return
            if str(sess.get("priority") or "").strip().upper() != "P1":
                return
            meeting_no = str(sess.get("meeting_no") or "").strip()
            sess["awaiting_p1_p0_confirm"] = True
            st, body, _ = _lark.post_card_to_chat(chat_id, token, _cards.build_p1_fifteen_min_confirm_card(meeting_no))
            if st != 200:
                log.error("p1 15min confirm card failed HTTP=%s body=%s", st, (body or "")[:500])
                sess.pop("awaiting_p1_p0_confirm", None)
                return
            log.info("Posted P1 15min P0 confirmation card chat_id=%s meeting_no=%s", chat_id, meeting_no)
        finally:
            with _ESCALATION_TIMERS_LOCK:
                _ESCALATION_TIMERS.pop(chat_id, None)

    timer = threading.Timer(P1_TO_P0_ESCALATION_SEC, run)
    timer.daemon = True
    with _ESCALATION_TIMERS_LOCK:
        _ESCALATION_TIMERS[chat_id] = timer
    timer.start()


def apply_p1_escalation_after_confirm(chat_id: str, token: str) -> bool:
    """User clicked Yes on the 15-min P1 card: post P1→P0 notice, set session to P0, DM + ongoing timer."""
    chat_id = (chat_id or "").strip()
    token = (token or "").strip()
    if not chat_id or not token:
        return False
    sess = P0_SESSIONS.get(chat_id) or {}
    if not sess:
        return False
    if str(sess.get("priority") or "").strip().upper() != "P1":
        return False
    if not sess.get("awaiting_p1_p0_confirm"):
        return False
    meeting_no = str(sess.get("meeting_no") or "").strip()
    trigger_open_id = str(sess.get("trigger_open_id") or "").strip()
    st, body, _ = _lark.post_card_to_chat(chat_id, token, _cards.build_p1_escalated_card(meeting_no))
    if st != 200:
        log.error("apply_p1_escalation_after_confirm card failed HTTP=%s body=%s", st, (body or "")[:500])
        return False
    sess["awaiting_p1_p0_confirm"] = False
    sess["priority"] = "P0"
    log.info("P1 escalated to P0 (user confirmed) chat_id=%s meeting_no=%s", chat_id, meeting_no)
    lab = str(sess.get("source_chat_name") or "").strip()
    p1_p0_targets = _dm_instruction_targets(trigger_open_id)
    log.info(
        "P1->P0 DM targets count=%s open_ids=%s (API expects open_id ou_..., not user_id)",
        len([x for x in p1_p0_targets if (x or "").strip()]),
        [x for x in p1_p0_targets if (x or "").strip()],
    )
    for dm_to in p1_p0_targets:
        if not dm_to:
            continue
        _send_dm_instruction_card_logged(dm_to, token, "P0", lab, context="P1->P0 DM instruction")
    _schedule_ongoing_meeting_card(chat_id, token)
    return True


def decline_p1_escalation_end_as_p1(chat_id: str, token: str) -> bool:
    """
    User tapped **Still P1** on the 15-min card: keep the session as **P1**, do not escalate to P0,
    and do **not** end the meeting. Posts a short notice in the incident group.
    """
    chat_id = (chat_id or "").strip()
    token = (token or "").strip()
    if not chat_id or not token:
        return False
    sess = P0_SESSIONS.get(chat_id) or {}
    if not sess:
        return False
    if str(sess.get("priority") or "").strip().upper() != "P1":
        return False
    if not sess.get("awaiting_p1_p0_confirm"):
        return False
    sess["awaiting_p1_p0_confirm"] = False
    msg = "The meeting is continuing as a P1 meeting."
    st, body = _lark.post_text_to_chat(chat_id, token, msg)
    if st != 200:
        log.warning(
            "Still P1 notice failed HTTP=%s chat_id=%s body=%s",
            st,
            chat_id,
            (body or "")[:300],
        )
        return False
    log.info("P1 15min: Still P1 — session continues (no P0 escalation) chat_id=%s", chat_id)
    return True


def start_p0(
    chat_id: str,
    token: str,
    trigger_open_id: str,
    priority: str = "P0",
    source_chat_name: str = "",
) -> None:
    from . import drafts as _drafts
    from . import participants as _participants

    _config.reload_env_runtime()
    chat_id = (chat_id or "").strip()
    trigger_open_id = (trigger_open_id or "").strip()
    priority = (priority or "P0").strip().upper()
    if not chat_id:
        return
    pop_p1_prompt_pending(chat_id)
    _clear_last_ended_snapshot(chat_id)
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
    dm_targets = _dm_instruction_targets(trigger_open_id)
    log.info(
        "start_p0 DM targets count=%s open_ids=%s (API expects open_id ou_..., not user_id gceda344-style)",
        len([x for x in dm_targets if (x or "").strip()]),
        [x for x in dm_targets if (x or "").strip()],
    )
    for oid in dm_targets:
        if not oid:
            continue
        _drafts.clear_draft(oid)
        _drafts.clear_preview(oid)
        _drafts.cancel_preview_timer(oid)
    if priority == "P0":
        for oid in dm_targets:
            if not oid:
                continue
            _send_dm_instruction_card_logged(oid, token, "P0", chat_label, context="start_p0 DM instruction")
        _schedule_ongoing_meeting_card(chat_id, token)
    elif priority == "P1":
        for oid in dm_targets:
            if not oid:
                continue
            _send_dm_instruction_card_logged(oid, token, "P1", chat_label, context="start_p0 DM instruction")
        _schedule_p1_escalation_card(chat_id, token)
