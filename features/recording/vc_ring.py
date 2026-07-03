"""
VC auto-ring: invite @mentioned users when duty joins an ongoing P0 meeting.

Requires ``P0_VC_RING_ENABLED=1``, duty ``user_access_token`` (OAuth), and
``PATCH /vc/v1/meetings/{meeting_id}/invite``.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Set

from p0_logic import config as _config
from p0_logic import lark_client as _lark
from features.session import session as _session
from . import vc_user_oauth as _oauth

log = logging.getLogger("lark-ops-ai")

_DUTY_MENTION_LOCK = threading.Lock()
# detection chat_id -> {open_id, ids[], ts}
_DUTY_MENTIONS_BY_CHAT: Dict[str, Dict[str, Any]] = {}


def _is_duty_open_id(open_id: str) -> bool:
    oid = (open_id or "").strip()
    if not oid:
        return False
    allowed = set(_config.get_dm_instruction_open_ids())
    return oid in allowed if allowed else True


def note_duty_mentions_in_chat(
    chat_id: str,
    duty_open_id: str,
    mention_open_ids: List[str],
    *,
    tenant_token: str = "",
) -> None:
    """Duty @mentioned users in detection group — merged on declare and during active P0."""
    if not _config.get_p0_vc_ring_enabled():
        return
    cid = (chat_id or "").strip()
    oid = (duty_open_id or "").strip()
    if not cid or not oid or not _is_duty_open_id(oid):
        return
    ids = _filter_ring_targets(mention_open_ids, operator_open_id=oid)
    if not ids:
        return
    with _DUTY_MENTION_LOCK:
        _DUTY_MENTIONS_BY_CHAT[cid] = {"open_id": oid, "ids": ids, "ts": time.time()}
    log.info(
        "vc_ring: duty mentions stored chat_tail=%s duty_tail=%s targets=%s",
        cid[-12:] if len(cid) > 12 else cid,
        oid[-8:],
        len(ids),
    )
    _merge_duty_mentions_into_active_session(
        cid, ids, operator_open_id=oid, tenant_token=tenant_token
    )


def _pending_ring_targets(sess: Dict[str, Any]) -> List[str]:
    """Targets not yet successfully invited this P0 session."""
    all_targets = list(sess.get("vc_ring_target_open_ids") or [])
    invited = {str(x).strip() for x in (sess.get("vc_ring_invited_open_ids") or []) if str(x).strip()}
    return [x for x in all_targets if x not in invited]


def _merge_duty_mentions_into_active_session(
    chat_id: str,
    new_ids: List[str],
    *,
    operator_open_id: str = "",
    tenant_token: str = "",
) -> None:
    """If P0 is already live, append duty @mentions and ring new users when VC is active."""
    cid = (chat_id or "").strip()
    if not cid or not new_ids:
        return
    sess = _session.P0_SESSIONS.get(cid)
    if not isinstance(sess, dict):
        return
    existing = list(sess.get("vc_ring_target_open_ids") or [])
    trigger = str(sess.get("trigger_open_id") or operator_open_id or "").strip()
    merged = _filter_ring_targets(existing + list(new_ids), operator_open_id=trigger)
    if merged == existing:
        return
    newly_added = [x for x in merged if x not in existing]
    sess["vc_ring_target_open_ids"] = merged
    _session.P0_SESSIONS[cid] = sess
    if _session._session_disk.enabled():
        _session._session_disk.save_session(cid, sess)
    log.info(
        "vc_ring: updated active session ring targets count=%s (was %s) new=%s chat_tail=%s",
        len(merged),
        len(existing),
        len(newly_added),
        cid[-12:] if len(cid) > 12 else cid,
    )
    meeting_id = str(sess.get("meeting_id") or "").strip()
    if newly_added and meeting_id and trigger:
        _try_ring_session(
            cid,
            sess,
            declarer_open_id=trigger,
            meeting_ref=meeting_id,
            tenant_token=tenant_token,
        )
    elif newly_added:
        log.info(
            "vc_ring: new targets queued — will ring when declarer joins VC chat_tail=%s",
            cid[-12:] if len(cid) > 12 else cid,
        )


def pop_duty_mentions_for_chat(chat_id: str) -> List[str]:
    cid = (chat_id or "").strip()
    if not cid:
        return []
    with _DUTY_MENTION_LOCK:
        row = _DUTY_MENTIONS_BY_CHAT.pop(cid, None)
    if not row:
        return []
    # Expire after 2h
    if time.time() - float(row.get("ts") or 0) > 7200:
        return []
    return list(row.get("ids") or [])


def invite_open_ids_into_active_meeting(
    chat_id: str,
    target_open_ids: List[str],
    *,
    tenant_token: str = "",
    operator_open_id: str = "",
) -> str:
    """Ring/invite ``target_open_ids`` into the chat's ALREADY-active P0 meeting.

    Used by the ``@bot`` ring commands (m / e / scpms / sfpms / sfe). Reuses the VC-ring
    merge path so each invitee's Lark app rings via ``invite_users_to_vc_meeting`` (which
    needs the declarer's OAuth token). Does NOT create a meeting.

    Returns a status string for the caller to render:
      ``disabled``     — P0_VC_RING_ENABLED is off
      ``no_session``   — no active P0 session / meeting for this chat
      ``no_targets``   — nothing valid to ring (empty or all filtered out)
      ``ringing``      — targets merged and the declarer is authorized → ring attempted now
      ``queued_oauth`` — targets merged but the declarer must finish OAuth first (DM sent)
    """
    if not _config.get_p0_vc_ring_enabled():
        return "disabled"
    cid = (chat_id or "").strip()
    if not cid:
        return "no_session"
    sess = _session.P0_SESSIONS.get(cid)
    if not isinstance(sess, dict):
        return "no_session"
    if not str(sess.get("meeting_id") or "").strip():
        return "no_session"
    raw = [str(x).strip() for x in (target_open_ids or []) if str(x).strip()]
    if not raw:
        return "no_targets"
    trigger = str(sess.get("trigger_open_id") or operator_open_id or "").strip()
    if not _filter_ring_targets(raw, operator_open_id=trigger):
        return "no_targets"
    _merge_duty_mentions_into_active_session(
        cid, raw, operator_open_id=operator_open_id, tenant_token=tenant_token
    )
    # The actual ring uses the declarer's OAuth; report whether it can fire now or is queued.
    if trigger and _oauth.get_user_access_token(trigger):
        return "ringing"
    return "queued_oauth"


def handle_ring_command(
    cmd: str,
    session_source: str,
    notify_chat: str,
    token: str,
    *,
    operator_open_id: str = "",
    tenant_token: str = "",
) -> None:
    """Handle an ``@bot`` ring command (m / e / scpms / sfpms / sfe).

    Pages the resolved people into the already-active meeting for ``session_source`` and
    posts a status reply to ``notify_chat``. Anyone in the group may run these.
    """
    from features.recording import duty_roster as _duty

    c = (cmd or "").strip().lower()
    tok = (tenant_token or token or "").strip()

    if c == "m":
        # Reuse the existing major-P0 check-person list (P0_MAJOR_CHECK_PERSON_IDS),
        # resolved to open_ids (also handles user_id entries).
        targets = _major_check_person_ring_open_ids(tok)
        label = "major-P0 check persons"
        unset_hint = "P0_MAJOR_CHECK_PERSON_IDS"
    elif c == "e":
        targets = _config.get_p0_vc_ring_escalation_open_ids()
        label = "escalation contacts"
        unset_hint = "P0_VC_RING_ESCALATION_OPEN_IDS"
    elif c in _duty.COMMAND_TEAM:
        team = _duty.COMMAND_TEAM[c]
        oid = _duty.get_duty_open_id(team)
        targets = [oid] if oid else []
        label = f"duty SRE {team}"
        unset_hint = f"the duty roster for {team}"
    else:
        return

    if not targets:
        if token:
            _lark.post_text_to_chat(
                notify_chat,
                token,
                f"⚠️ No {label} configured yet ({unset_hint}). Ask an admin to set it.",
            )
        return

    status = invite_open_ids_into_active_meeting(
        session_source,
        targets,
        tenant_token=tok,
        operator_open_id=operator_open_id,
    )
    log.info(
        "ring cmd handled cmd=%s targets=%s status=%s session_tail=%s",
        c,
        len(targets),
        status,
        session_source[-8:] if session_source else "",
    )
    if not token:
        return
    if status == "disabled":
        msg = "⚠️ VC ring is disabled (set P0_VC_RING_ENABLED=1)."
    elif status == "no_session":
        msg = "⚠️ No active meeting here yet — start a meeting first, then run this command."
    elif status == "no_targets":
        msg = f"⚠️ No valid {label} to call."
    elif status == "queued_oauth":
        msg = (
            f"📞 Queued a call to {label} — it will ring once the meeting host finishes the "
            "one-time Lark authorization (the host just got a DM)."
        )
    else:  # ringing
        msg = f"📞 Calling {label} into the meeting now…"
    _lark.post_text_to_chat(notify_chat, token, msg)


def _filter_ring_targets(
    raw_ids: List[str],
    *,
    operator_open_id: str = "",
    exclude: Optional[Set[str]] = None,
) -> List[str]:
    op = (operator_open_id or "").strip()
    skip = set(exclude or [])
    if op:
        skip.add(op)
    bot_oid = (_config.get_lark_bot_open_id() or "").strip()
    if bot_oid:
        skip.add(bot_oid)
    out: List[str] = []
    seen: set[str] = set()
    for raw in raw_ids or []:
        oid = (raw or "").strip()
        if not oid.startswith("ou_") or len(oid) < 12:
            continue
        if oid in skip or oid in seen:
            continue
        seen.add(oid)
        out.append(oid)
        if len(out) >= 10:
            break
    for fb in _config.get_p0_vc_ring_fallback_open_ids():
        if fb not in skip and fb not in seen:
            seen.add(fb)
            out.append(fb)
        if len(out) >= 10:
            break
    return out


def resolve_ring_targets_from_snapshot(
    snap: Optional[Dict[str, Any]],
    *,
    detection_chat_id: str,
    operator_open_id: str,
) -> List[str]:
    """Merge concern @mentions (on alert) + duty @mentions stored before declare."""
    concern: List[str] = []
    if snap:
        raw = snap.get("concern_mention_open_ids") or snap.get("mention_open_ids") or []
        if isinstance(raw, list):
            concern = [str(x).strip() for x in raw]
    duty_ids = pop_duty_mentions_for_chat(detection_chat_id)
    merged = duty_ids + [x for x in concern if x not in duty_ids]
    return _filter_ring_targets(merged, operator_open_id=operator_open_id)


def _major_check_person_ring_open_ids(
    tenant_token: str,
) -> List[str]:
    """``P0_MAJOR_CHECK_PERSON_IDS`` as ``ou_...`` for VC invite (resolves tenant user_id when needed)."""
    tok = (tenant_token or "").strip()
    out: List[str] = []
    for oid, uid in _config.get_p0_major_check_person_recipients():
        if oid and oid.startswith("ou_"):
            out.append(oid)
            continue
        if uid and tok:
            resolved = _lark.lookup_open_id_by_user_id(tok, uid)
            if resolved:
                out.append(resolved)
    return out


def resolve_declare_ring_targets(
    snap: Optional[Dict[str, Any]],
    *,
    detection_chat_id: str,
    operator_open_id: str,
    tenant_token: str = "",
) -> List[str]:
    """
    Ring targets at Issue Watch declare: concern/duty @mentions + ``P0_MAJOR_CHECK_PERSON_IDS``.
    """
    base = resolve_ring_targets_from_snapshot(
        snap,
        detection_chat_id=detection_chat_id,
        operator_open_id=operator_open_id,
    )
    major = _major_check_person_ring_open_ids(tenant_token)
    if not major:
        return base
    return _filter_ring_targets(major + base, operator_open_id=operator_open_id)


def format_declare_reply_with_mentions(reply_text: str, ring_targets: List[str]) -> str:
    """Prepend Lark ``<at>`` tags so tagged users are notified in the thread."""
    body = (reply_text or "").strip()
    if not ring_targets:
        return body
    at_parts = [f'<at user_id="{oid}"></at>' for oid in ring_targets]
    prefix = " ".join(at_parts)
    return f"{prefix} {body}".strip() if body else prefix


def maybe_prompt_oauth_dm(operator_open_id: str, tenant_token: str) -> None:
    if not _config.get_p0_vc_ring_enabled():
        return
    oid = (operator_open_id or "").strip()
    tok = (tenant_token or "").strip()
    if not oid or not tok:
        return
    if _oauth.has_user_token(oid):
        return
    url = _oauth.build_authorize_url(oid)
    if not url:
        log.warning("vc_ring: OAuth URL not configured — set P0_VC_OAUTH_REDIRECT_URI + public base")
        return
    _lark.post_text_to_open_id(
        oid,
        tok,
        "To auto-ring tagged users into the P0 VC, authorize this bot once (VC invite permission):\n"
        f"{url}\n\n"
        "Authorize first, then join the P0 VC — tagged users will be rung when you enter.",
    )


def _resolve_declarer_open_id(
    joiner_open_id: str,
    joiner_user_id: str,
    tenant_token: str,
) -> str:
    oid = (joiner_open_id or "").strip()
    if oid.startswith("ou_"):
        return oid
    uid = (joiner_user_id or "").strip()
    if uid and tenant_token:
        resolved = _lark.lookup_open_id_by_user_id(tenant_token, uid)
        if resolved:
            log.info("vc_ring: resolved joiner open_id from user_id uid_tail=%s", uid[-6:])
            return resolved
    return ""


def _is_session_declarer(
    sess: Dict[str, Any],
    *,
    joiner_open_id: str,
    joiner_user_id: str,
    tenant_token: str,
) -> bool:
    trigger_oid = str(sess.get("trigger_open_id") or "").strip()
    if trigger_oid and joiner_open_id and joiner_open_id == trigger_oid:
        return True
    trigger_uid = str(sess.get("trigger_lark_user_id") or "").strip()
    if trigger_uid and joiner_user_id and joiner_user_id == trigger_uid:
        return True
    if trigger_uid and joiner_open_id and tenant_token:
        joiner_uid = _lark.get_tenant_user_id_by_open_id(tenant_token, joiner_open_id)
        if joiner_uid and joiner_uid == trigger_uid:
            return True
    return False


def _try_ring_session(
    chat_id: str,
    sess: Dict[str, Any],
    *,
    declarer_open_id: str,
    meeting_ref: str,
    tenant_token: str,
) -> bool:
    """Attempt VC invite for pending (not yet invited) targets. Returns True when ring succeeded."""
    declarer = (declarer_open_id or "").strip()
    if not declarer:
        return False
    targets = _pending_ring_targets(sess)
    if not targets:
        if list(sess.get("vc_ring_target_open_ids") or []):
            log.info("vc_ring: all targets already invited chat_id=%s", chat_id[:24])
        else:
            log.warning("vc_ring: no ring targets on session chat_id=%s", chat_id[:24])
        return False

    meeting_id = str(sess.get("meeting_id") or meeting_ref or "").strip()
    if not meeting_id:
        log.warning("vc_ring: no meeting_id on session chat_id=%s", chat_id[:24])
        return False

    user_tok = _oauth.get_user_access_token(declarer)
    if not user_tok:
        maybe_prompt_oauth_dm(declarer, tenant_token)
        log.warning(
            "vc_ring: no user_access_token for declarer_tail=%s — complete OAuth first",
            declarer[-8:],
        )
        return False

    ok, detail = _lark.invite_users_to_vc_meeting(user_tok, meeting_id, targets)
    if ok:
        invited = list(sess.get("vc_ring_invited_open_ids") or [])
        seen = set(invited)
        for oid in targets:
            if oid not in seen:
                invited.append(oid)
                seen.add(oid)
        sess["vc_ring_invited_open_ids"] = invited
        sess["vc_ring_done"] = not _pending_ring_targets(
            {**sess, "vc_ring_invited_open_ids": invited}
        )
        _session.P0_SESSIONS[chat_id] = sess
        if _session._session_disk.enabled():
            _session._session_disk.save_session(chat_id, sess)
        log.info(
            "vc_ring: invited count=%s meeting_id_tail=%s declarer_tail=%s detail=%s",
            len(targets),
            meeting_id[-12:] if len(meeting_id) > 12 else meeting_id,
            declarer[-8:],
            (detail or "")[:200],
        )
        if tenant_token:
            _lark.post_text_to_open_id(
                declarer,
                tenant_token,
                "Calling the check persons for major P0 issues.",
            )
        return True

    log.warning(
        "vc_ring: invite failed meeting_id_tail=%s targets=%s detail=%s",
        meeting_id[-12:] if len(meeting_id) > 12 else meeting_id,
        len(targets),
        (detail or "")[:300],
    )
    return False


def maybe_ring_on_vc_join(
    meeting_ref: str,
    joiner_open_id: str,
    tenant_token: str,
    *,
    joiner_user_id: str = "",
) -> None:
    """
    When the P0 declarer joins the VC, invite ``vc_ring_target_open_ids`` from the session.
    """
    if not _config.get_p0_vc_ring_enabled():
        return
    ref = (meeting_ref or "").strip()
    if not ref:
        return

    joiner = _resolve_declarer_open_id(joiner_open_id, joiner_user_id, tenant_token)
    if not joiner:
        log.warning(
            "vc_ring: join event missing open_id and could not resolve from user_id ref_tail=%s",
            ref[-12:] if len(ref) > 12 else ref,
        )
        return

    chat_id, sess = _session.find_session_by_meeting_ref(ref)
    if not chat_id or not sess:
        log.warning(
            "vc_ring: no session for meeting_ref_tail=%s joiner_tail=%s",
            ref[-12:] if len(ref) > 12 else ref,
            joiner[-8:],
        )
        return

    if not _is_session_declarer(
        sess,
        joiner_open_id=joiner,
        joiner_user_id=joiner_user_id,
        tenant_token=tenant_token,
    ):
        log.warning(
            "vc_ring: joiner not declarer — skip ring joiner_tail=%s trigger_tail=%s trigger_uid=%s",
            joiner[-8:],
            str(sess.get("trigger_open_id") or "")[-8:],
            str(sess.get("trigger_lark_user_id") or "")[-6:],
        )
        return

    _try_ring_session(
        chat_id,
        sess,
        declarer_open_id=joiner,
        meeting_ref=ref,
        tenant_token=tenant_token,
    )


def maybe_retry_pending_vc_ring_for_declarer(declarer_open_id: str, tenant_token: str) -> int:
    """
    After OAuth, retry ring on active sessions where this user declared P0 but ring did not run
    (e.g. they joined VC before authorizing).
    """
    if not _config.get_p0_vc_ring_enabled():
        return 0
    declarer = (declarer_open_id or "").strip()
    if not declarer:
        return 0
    if not _oauth.has_user_token(declarer):
        return 0

    n_ok = 0
    for chat_id, sess in list(_session.P0_SESSIONS.items()):
        if not isinstance(sess, dict):
            continue
        if str(sess.get("trigger_open_id") or "").strip() != declarer:
            continue
        if sess.get("vc_ring_done"):
            continue
        if not list(sess.get("vc_ring_target_open_ids") or []):
            continue
        meeting_ref = str(sess.get("meeting_id") or sess.get("meeting_no") or "").strip()
        if not meeting_ref:
            continue
        if _try_ring_session(
            chat_id,
            sess,
            declarer_open_id=declarer,
            meeting_ref=meeting_ref,
            tenant_token=tenant_token,
        ):
            n_ok += 1
    if n_ok:
        log.info("vc_ring: OAuth retry rang %s session(s) for declarer_tail=%s", n_ok, declarer[-8:])
    return n_ok
