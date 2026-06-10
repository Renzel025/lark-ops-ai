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

from . import config as _config
from . import lark_client as _lark
from . import session as _session
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


def note_duty_mentions_in_chat(chat_id: str, duty_open_id: str, mention_open_ids: List[str]) -> None:
    """Duty @mentioned users in detection group — used on next declare ring."""
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
    """Merge concern @mentions + latest duty @mentions in this chat."""
    concern: List[str] = []
    if snap:
        raw = snap.get("concern_mention_open_ids") or snap.get("mention_open_ids") or []
        if isinstance(raw, list):
            concern = [str(x).strip() for x in raw]
    duty_ids = pop_duty_mentions_for_chat(detection_chat_id)
    merged = duty_ids + [x for x in concern if x not in duty_ids]
    return _filter_ring_targets(merged, operator_open_id=operator_open_id)


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
        "After authorizing, declare P0, join the meeting, then tagged users will be rung automatically.",
    )


def maybe_ring_on_vc_join(meeting_ref: str, joiner_open_id: str, tenant_token: str) -> None:
    """
    When the P0 declarer joins the VC, invite ``vc_ring_target_open_ids`` from the session.
    """
    if not _config.get_p0_vc_ring_enabled():
        return
    ref = (meeting_ref or "").strip()
    joiner = (joiner_open_id or "").strip()
    if not ref or not joiner:
        return

    chat_id, sess = _session.find_session_by_meeting_ref(ref)
    if not chat_id or not sess:
        return
    trigger = str(sess.get("trigger_open_id") or "").strip()
    if joiner != trigger:
        log.debug(
            "vc_ring: joiner not declarer — skip ring joiner_tail=%s trigger_tail=%s",
            joiner[-8:],
            trigger[-8:] if trigger else "",
        )
        return
    targets = list(sess.get("vc_ring_target_open_ids") or [])
    if not targets:
        log.info("vc_ring: no ring targets on session chat_id=%s", chat_id[:24])
        return
    if sess.get("vc_ring_done"):
        return

    meeting_id = str(sess.get("meeting_id") or ref).strip()
    user_tok = _oauth.get_user_access_token(joiner)
    if not user_tok:
        maybe_prompt_oauth_dm(joiner, tenant_token)
        log.warning(
            "vc_ring: no user_access_token for declarer_tail=%s — OAuth required before ring",
            joiner[-8:],
        )
        return

    ok, detail = _lark.invite_users_to_vc_meeting(user_tok, meeting_id, targets)
    if ok:
        sess["vc_ring_done"] = True
        _session.P0_SESSIONS[chat_id] = sess
        if _session._session_disk.enabled():
            _session._session_disk.save_session(chat_id, sess)
        log.info(
            "vc_ring: invited count=%s meeting_id_tail=%s declarer_tail=%s detail=%s",
            len(targets),
            meeting_id[-12:] if len(meeting_id) > 12 else meeting_id,
            joiner[-8:],
            (detail or "")[:200],
        )
        if tenant_token:
            _lark.post_text_to_open_id(
                joiner,
                tenant_token,
                f"VC ring: invited {len(targets)} user(s) into the meeting.",
            )
    else:
        log.warning(
            "vc_ring: invite failed meeting_id_tail=%s targets=%s detail=%s",
            meeting_id[-12:] if len(meeting_id) > 12 else meeting_id,
            len(targets),
            (detail or "")[:300],
        )
