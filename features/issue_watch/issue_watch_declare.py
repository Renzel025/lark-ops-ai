"""
Issue Watch — duty declares P0 from the Major detection alert DM (reply + react + start_p0).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from p0_logic import config as _config
from p0_logic import groq_client as _groq
from . import issue_watch_overview as _iwo
from p0_logic import lark_client as _lark
from features.session import session as _session
from features.session import session_disk as _session_disk

log = logging.getLogger("lark-ops-ai")


def _major_check_person_id_sets(
    recipients: List[Tuple[str, str]],
) -> Tuple[FrozenSet[str], FrozenSet[str]]:
    oids: set = set()
    uids: set = set()
    for oid, uid in recipients:
        if oid:
            oids.add(oid)
        if uid:
            uids.add(uid)
    return frozenset(oids), frozenset(uids)


def _stash_major_check_person_session(
    sess: Dict[str, Any],
    concern_message_id: str,
    recipients: List[Tuple[str, str]],
) -> None:
    oids, uids = _major_check_person_id_sets(recipients)
    # Merge — don't clobber any join-watch ids start_p0 already seeded (e.g. P0_VC_AUTO_INVITE_OPEN_IDS).
    sess["major_check_person_open_ids"] = sorted(set(sess.get("major_check_person_open_ids") or []) | oids)
    sess["major_check_person_user_ids"] = sorted(set(sess.get("major_check_person_user_ids") or []) | uids)
    sess.setdefault("major_check_person_join_prompted", [])
    mid = (concern_message_id or "").strip()
    if mid:
        sess["issue_watch_concern_message_id"] = mid


def _dm_one_check_person(
    token: str,
    open_id: str,
    user_id: str,
    text: str,
) -> Tuple[int, str]:
    oid = (open_id or "").strip()
    uid = (user_id or "").strip()
    if oid:
        return _lark.post_text_to_open_id(oid, token, text)
    if uid:
        return _lark.post_text_to_user_cross_app("", uid, token, text, use_user_id=True)
    return 0, ""


def invite_major_check_persons_after_declare(
    source_chat_id: str,
    tenant_token: str,
    concern_message_id: str,
) -> int:
    """
    After Issue Watch declare + ``start_p0``, register check persons on the session.

    VC ring (when duty joins) is the default invite path. Optional link DMs only when
    ``P0_MAJOR_CHECK_PERSON_DM_ENABLED=1``. Returns count of successful DMs (0 when DM off).
    """
    cid = (source_chat_id or "").strip()
    tok = (tenant_token or "").strip()
    recipients = _config.get_p0_major_check_person_recipients()
    if not cid or not tok or not recipients:
        return 0
    sess = _session.P0_SESSIONS.get(cid) or {}
    _stash_major_check_person_session(sess, concern_message_id, recipients)
    if _session_disk.enabled():
        _session_disk.save_session(cid, sess)
    if not _config.get_p0_major_check_person_dm_enabled():
        log.info(
            "issue_watch_declare: check-person invite = VC ring only (DM disabled) chat_tail=%s count=%s",
            cid[-12:] if len(cid) > 12 else cid,
            len(recipients),
        )
        return 0
    link = (sess.get("link") or "").strip()
    if not link:
        log.warning("issue_watch_declare: check-person DM skipped — no meeting link chat=%s", cid[:24])
        return 0
    body_tmpl = _config.get_p0_major_check_person_dm_text()
    body = body_tmpl.replace("{link}", link)
    sent = 0
    for oid, uid in recipients:
        st, resp = _dm_one_check_person(tok, oid, uid, body)
        ok, code, msg = _lark.lark_im_message_create_ok(resp)
        if st == 200 and ok:
            sent += 1
            log.info(
                "issue_watch_declare: check-person DM sent open_id_tail=%s user_id=%s",
                oid[-8:] if oid else "",
                uid[:12] if uid else "",
            )
        else:
            log.warning(
                "issue_watch_declare: check-person DM failed HTTP=%s code=%s open_id_tail=%s user_id=%s msg=%s",
                st,
                code,
                oid[-8:] if oid else "",
                uid[:12] if uid else "",
                (msg or resp or "")[:160],
            )
    return sent


def maybe_prompt_major_check_person_joined(
    *,
    meeting_ref: str,
    tenant_token: str,
    joiner_open_id: str = "",
    joiner_user_id: str = "",
    participant_name: str = "",
) -> None:
    """
    When a configured check person joins VC, reply once on the original concern thread.
    Non-check-persons and no-shows get no prompt.
    """
    ref = (meeting_ref or "").strip()
    tok = (tenant_token or "").strip()
    if not ref or not tok:
        return
    cid, sess = _session.find_session_by_meeting_ref(ref)
    if not cid or not sess:
        return
    check_oids = set(sess.get("major_check_person_open_ids") or [])
    check_uids = set(sess.get("major_check_person_user_ids") or [])
    if not check_oids and not check_uids:
        return
    jo = (joiner_open_id or "").strip()
    ju = (joiner_user_id or "").strip()
    if not ((jo and jo in check_oids) or (ju and ju in check_uids)):
        return
    dedupe_key = jo or ju
    prompted = list(sess.get("major_check_person_join_prompted") or [])
    if dedupe_key in prompted:
        return
    src_mid = str(
        sess.get("join_prompt_reply_mid")
        or sess.get("meeting_invite_message_id")
        or sess.get("issue_watch_concern_message_id")
        or ""
    ).strip()
    if not src_mid:
        return
    name = (participant_name or "").strip() or "Check person"
    # Tag the joiner: <at user_id> renders as an @mention (falls back to the plain name if no open_id).
    who = f'<at user_id="{jo}"></at>' if jo else name
    text = _config.get_p0_major_check_person_join_thread_text().replace("{name}", who)
    st, body = _lark.post_text_reply_to_message(src_mid, tok, text, reply_in_thread=True)
    ok, code, msg = _lark.lark_im_message_create_ok(body)
    if st != 200 or not ok:
        log.warning(
            "issue_watch_declare: check-person join prompt failed HTTP=%s code=%s concern_tail=%s msg=%s",
            st,
            code,
            src_mid[-12:] if len(src_mid) > 12 else src_mid,
            (msg or body or "")[:160],
        )
        return
    prompted.append(dedupe_key)
    sess["major_check_person_join_prompted"] = prompted
    if _session_disk.enabled():
        _session_disk.save_session(cid, sess)
    log.info(
        "issue_watch_declare: check-person join prompt sent name=%r concern_tail=%s joiner_tail=%s",
        name[:40],
        src_mid[-12:] if len(src_mid) > 12 else src_mid,
        dedupe_key[-8:] if len(dedupe_key) > 8 else dedupe_key,
    )


def _declare_reply_from_alert(snap: Optional[Dict[str, Any]]) -> str:
    """Contextual reply on the concern thread, via the ``P0_ISSUE_WATCH_AI_PROVIDER`` chain
    (Claude first, Groq failover — same as the classifier); env fallback text if all fail."""
    fallback = _config.get_p0_issue_watch_declare_reply_text()
    if not snap or not _config.get_p0_issue_watch_declare_reply_ai_enabled():
        return fallback
    categories = list(snap.get("categories") or [])
    widespread = "widespread_impact" in categories
    try:
        players = max(
            len([x for x in (snap.get("player_ids") or []) if str(x).strip()]),
            int(snap.get("players_count") or 0),
        )
    except (TypeError, ValueError):
        players = 0
    from features.issue_watch.issue_watch_ai import issue_watch_ai_providers_to_try
    from features.overview.overview_ai import run_provider

    system_prompt, user_prompt = _groq.build_issue_watch_declare_reply_prompts(
        categories_md=str(snap.get("categories_md") or "").strip(),
        summary=str(snap.get("summary") or "").strip(),
        concern_excerpt=str(snap.get("concern_raw") or snap.get("concern") or "").strip(),
        players_count=players,
        min_reports_threshold=_config.get_p0_issue_watch_min_reports(),
        widespread_impact=widespread,
    )
    for provider in issue_watch_ai_providers_to_try():
        try:
            raw = run_provider(provider, system_prompt, user_prompt, max_tokens=200)
        except Exception as e:  # noqa: BLE001 — any provider error falls through to the next
            log.warning("issue_watch_declare: declare reply provider=%s raised %s", provider, e)
            continue
        ai = _groq.parse_issue_watch_declare_reply(raw)
        if ai:
            log.info(
                "issue_watch_declare: declare reply provider=%s len=%s players=%s widespread=%s",
                provider,
                len(ai),
                players,
                widespread,
            )
            return ai
        log.warning("issue_watch_declare: declare reply provider=%s returned no usable JSON", provider)
    log.warning("issue_watch_declare: declare reply failed on all providers — using fallback text")
    return fallback


def _post_declare_text_on_concern(
    *,
    src_mid: str,
    detection_chat: str,
    tenant_token: str,
    operator_open_id: str,
    text: str,
    log_label: str,
) -> bool:
    """Reply on the concern thread (+ optional main-group fan-out). Returns True on success."""
    body = (text or "").strip()
    mid = (src_mid or "").strip()
    cid = (detection_chat or "").strip()
    tok = (tenant_token or "").strip()
    oid = (operator_open_id or "").strip()
    if not mid or not body or not tok:
        return False
    st_r, body_r = _lark.post_text_reply_to_message(
        mid,
        tok,
        body,
        reply_in_thread=_config.get_p0_issue_watch_declare_reply_in_thread(),
    )
    ok, api_code, api_msg = _lark.lark_im_message_create_ok(body_r)
    if st_r != 200 or not ok:
        log.warning(
            "issue_watch_declare: %s failed HTTP=%s code=%s chat=%s parent_tail=%s body=%s",
            log_label,
            st_r,
            api_code,
            cid[:24],
            mid[-12:] if len(mid) > 12 else mid,
            (api_msg or body_r or "")[:200],
        )
        if oid:
            _lark.post_text_to_open_id(
                oid,
                tok,
                f"Could not post {log_label} on the concern message in the detection group. "
                "Check bot is in the group and has im:message permission.",
            )
        return False
    parent_id = ""
    reply_mid = ""
    try:
        data = (json.loads(body_r or "{}").get("data") or {})
        parent_id = str(data.get("parent_id") or "").strip()
        reply_mid = str(data.get("message_id") or "").strip()
    except Exception:
        pass
    log.info(
        "issue_watch_declare: %s sent chat_tail=%s parent_tail=%s "
        "api_parent_tail=%s reply_tail=%s thread=%s",
        log_label,
        cid[-12:] if len(cid) > 12 else cid,
        mid[-12:] if len(mid) > 12 else mid,
        parent_id[-12:] if len(parent_id) > 12 else parent_id,
        reply_mid[-12:] if len(reply_mid) > 12 else reply_mid,
        _config.get_p0_issue_watch_declare_reply_in_thread(),
    )
    if _config.get_p0_issue_watch_declare_also_send_to_group():
        st_g, body_g = _lark.post_text_to_chat(cid, tok, body)
        ok_g, code_g, msg_g = _lark.lark_im_message_create_ok(body_g)
        if st_g != 200 or not ok_g:
            log.warning(
                "issue_watch_declare: %s also-send-to-group failed HTTP=%s code=%s chat=%s msg=%s",
                log_label,
                st_g,
                code_g,
                cid[:24],
                (msg_g or body_g or "")[:200],
            )
        else:
            log.info(
                "issue_watch_declare: %s also sent to main group chat_tail=%s",
                log_label,
                cid[-12:] if len(cid) > 12 else cid,
            )
    return True


def _declared_note(_operator_open_id: str = "") -> str:
    """Terminal line that replaces the Declare / Not now buttons once the alert is actioned."""
    return "**Declared as P0** — meeting created. This alert cannot be declared again."


def _duty_operator(operator_open_id: str) -> bool:
    oid = (operator_open_id or "").strip()
    if not oid:
        return False
    allowed = set(_config.get_dm_instruction_open_ids())
    return oid in allowed if allowed else True


def handle_declare_p0(
    operator_open_id: str,
    tenant_token: str,
    *,
    alert_key: str = "",
    source_incident_chat_id: str = "",
    source_message_id: str = "",
    operator_lark_user_id: str = "",
) -> None:
    """
    Duty confirms P0 from the Issue Watch alert DM:
    reply on the concern thread, react on the source message, then ``start_p0``.
    Overview preview is queued by existing ``start_p0`` + Issue Watch alert cache.
    """
    oid = (operator_open_id or "").strip()
    tok = (tenant_token or "").strip()
    if not oid or not tok:
        return
    if not _config.get_p0_issue_watch_declare_p0_enabled():
        _lark.post_text_to_open_id(oid, tok, "Issue Watch declare-from-DM is disabled on this bot.")
        return
    if not _duty_operator(oid):
        _lark.post_text_to_open_id(oid, tok, "Only overview duty recipients can declare P0 from this alert.")
        return

    key = (alert_key or "").strip()
    snap = _iwo.get_alert_snapshot(key) if key else None
    detection_chat = (source_incident_chat_id or "").strip()
    src_mid = (source_message_id or "").strip()
    group_label = ""
    if snap:
        detection_chat = detection_chat or str(snap.get("chat_id") or snap.get("source_incident_chat_id") or "").strip()
        src_mid = src_mid or str(snap.get("message_id") or "").strip()
        group_label = str(snap.get("group_label") or "").strip()

    if not detection_chat:
        _lark.post_text_to_open_id(oid, tok, "Could not resolve the detection group for this alert.")
        return

    # One declare per alert. Without this, clicking the button again — by the other duty recipient,
    # or by the same person after the meeting ended — starts a second VC for the same concern.
    if key:
        claimed, prev_by = _iwo.claim_alert_declare(key, oid)
        if not claimed:
            who = "you" if prev_by == oid else "another duty member"
            _lark.post_text_to_open_id(
                oid,
                tok,
                f"This alert was already declared as P0 by {who}. "
                "Type p0 in the detection group if a new meeting is really needed.",
            )
            _iwo.patch_alert_cards(tok, key, _declared_note(prev_by or oid))
            log.info(
                "issue_watch_declare: duplicate declare blocked alert_key=%s operator_tail=%s prev_tail=%s",
                key[:12],
                oid[-8:] if len(oid) > 8 else oid,
                (prev_by or "")[-8:],
            )
            return
    else:
        log.warning(
            "issue_watch_declare: no alert_key on the button — cannot dedupe this declare "
            "(P0_ISSUE_WATCH_AUTO_OVERVIEW off?) chat_tail=%s",
            detection_chat[-12:] if len(detection_chat) > 12 else detection_chat,
        )

    # Auto-calling major check persons on detection is gated: when off, only the on-call auto-invite
    # ("Calling <names> into the meeting", seeded in start_p0) pages people — the check persons are NOT
    # rung, NOT DM'd, and get no "calling check persons" reply; @bot m still reaches them on demand.
    auto_call_check = _config.get_p0_major_check_person_auto_invite_on_declare_enabled()

    ring_targets: List[str] = []
    if _config.get_p0_vc_ring_enabled():
        from features.recording import vc_ring as _vc_ring

        ring_targets = _vc_ring.resolve_declare_ring_targets(
            snap,
            detection_chat_id=detection_chat,
            operator_open_id=oid,
            tenant_token=tok,
            include_major_check_persons=auto_call_check,
        )
        if ring_targets:
            log.info(
                "issue_watch_declare: ring_targets count=%s chat_tail=%s",
                len(ring_targets),
                detection_chat[-12:] if len(detection_chat) > 12 else detection_chat,
            )
        _vc_ring.maybe_prompt_oauth_dm(oid, tok)

    declare_reply = _declare_reply_from_alert(snap)
    check_recipients = _config.get_p0_major_check_person_recipients() if auto_call_check else []
    if src_mid:
        _post_declare_text_on_concern(
            src_mid=src_mid,
            detection_chat=detection_chat,
            tenant_token=tok,
            operator_open_id=oid,
            text=declare_reply,
            log_label="declare-as-P0 reply",
        )
        if check_recipients:
            invite_reply = _config.get_p0_issue_watch_declare_check_person_reply_text()
            _post_declare_text_on_concern(
                src_mid=src_mid,
                detection_chat=detection_chat,
                tenant_token=tok,
                operator_open_id=oid,
                text=invite_reply,
                log_label="check-person invite reply",
            )
    else:
        log.warning("issue_watch_declare: no source message_id — skipping group reply chat=%s", detection_chat[:24])
        _lark.post_text_to_open_id(
            oid,
            tok,
            "Could not find the source concern message to reply on. P0 declare will still proceed.",
        )

    reaction = _config.get_p0_issue_watch_declare_reaction()
    if src_mid and reaction:
        st_e, _ = _lark.add_message_reaction(src_mid, tok, reaction)
        if st_e != 200:
            log.warning(
                "issue_watch_declare: reaction failed HTTP=%s emoji=%s mid_tail=%s",
                st_e,
                reaction,
                src_mid[-12:] if len(src_mid) > 12 else src_mid,
            )

    log.info(
        "issue_watch_declare: start_p0 chat_tail=%s operator_tail=%s alert_key=%s",
        detection_chat[-12:] if len(detection_chat) > 12 else detection_chat,
        oid[-8:] if len(oid) > 8 else oid,
        key[:12] if key else "",
    )
    _session.start_p0(
        detection_chat,
        tok,
        oid,
        priority="P0",
        source_chat_name=group_label,
        trigger_lark_user_id=(operator_lark_user_id or "").strip(),
        silent_when_blocked=False,
        vc_ring_target_open_ids=ring_targets or None,
        issue_watch_alert_key=key,
    )
    # Close the alert for EVERY recipient — the other duty member's copy still has live buttons.
    if key:
        _iwo.patch_alert_cards(tok, key, _declared_note(oid))
    if auto_call_check and _config.get_p0_major_check_person_recipients():
        n_inv = invite_major_check_persons_after_declare(detection_chat, tok, src_mid)
        log.info(
            "issue_watch_declare: check-person invites sent=%s chat_tail=%s",
            n_inv,
            detection_chat[-12:] if len(detection_chat) > 12 else detection_chat,
        )


def handle_declare_dismiss(
    operator_open_id: str,
    tenant_token: str,
    *,
    alert_key: str = "",
    clicked_card_message_id: str = "",
) -> None:
    """"Not now" — clear the buttons on the clicker's copy only.

    Dismiss is a personal acknowledgement, so the other duty member's card keeps its buttons and
    can still declare.
    """
    oid = (operator_open_id or "").strip()
    tok = (tenant_token or "").strip()
    if not oid or not tok:
        return
    key = (alert_key or "").strip()
    mid = (clicked_card_message_id or "").strip()
    if key and mid:
        _iwo.patch_alert_cards(
            tok,
            key,
            "**Not now** — no P0 declared from this alert. Type p0 in the detection group if that changes.",
            only_message_id=mid,
        )
    _lark.post_text_to_open_id(oid, tok, "Acknowledged. No P0 declared for this alert.")
