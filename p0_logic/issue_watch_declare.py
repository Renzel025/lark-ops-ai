"""
Issue Watch — duty declares P0 from the Major detection alert DM (reply + react + start_p0).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from . import cards as _cards
from . import config as _config
from . import groq_client as _groq
from . import issue_watch_overview as _iwo
from . import lark_client as _lark
from . import session as _session
from . import vc_ring as _vc_ring

log = logging.getLogger("lark-ops-ai")


def _declare_reply_from_alert(snap: Optional[Dict[str, Any]]) -> str:
    """Groq contextual reply on the concern thread; env fallback if AI off or unavailable."""
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
    ai = _groq.groq_issue_watch_declare_group_reply(
        categories_md=str(snap.get("categories_md") or "").strip(),
        summary=str(snap.get("summary") or "").strip(),
        concern_excerpt=str(snap.get("concern_raw") or snap.get("concern") or "").strip(),
        players_count=players,
        min_reports_threshold=_config.get_p0_issue_watch_min_reports(),
        widespread_impact=widespread,
    )
    if ai:
        log.info("issue_watch_declare: Groq declare reply len=%s players=%s widespread=%s", len(ai), players, widespread)
        return ai
    log.warning("issue_watch_declare: Groq declare reply failed — using fallback text")
    return fallback


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

    ring_targets: list[str] = []
    if _config.get_p0_vc_ring_enabled():
        ring_targets = _vc_ring.resolve_ring_targets_from_snapshot(
            snap,
            detection_chat_id=detection_chat,
            operator_open_id=oid,
        )
    reply_text = _declare_reply_from_alert(snap)
    reply_post = _vc_ring.format_declare_reply_with_mentions(reply_text, ring_targets)
    if src_mid:
        st_r, body_r = _lark.post_text_reply_to_message(
            src_mid,
            tok,
            reply_post,
            reply_in_thread=_config.get_p0_issue_watch_declare_reply_in_thread(),
        )
        ok, api_code, api_msg = _lark.lark_im_message_create_ok(body_r)
        if st_r != 200 or not ok:
            log.warning(
                "issue_watch_declare: reply-on-message failed HTTP=%s code=%s chat=%s parent_tail=%s body=%s",
                st_r,
                api_code,
                detection_chat[:24],
                src_mid[-12:] if len(src_mid) > 12 else src_mid,
                (api_msg or body_r or "")[:200],
            )
            _lark.post_text_to_open_id(
                oid,
                tok,
                "Could not reply on the concern message in the detection group. "
                "Check bot is in the group and has im:message permission.",
            )
        else:
            parent_id = ""
            reply_mid = ""
            try:
                data = (json.loads(body_r or "{}").get("data") or {})
                parent_id = str(data.get("parent_id") or "").strip()
                reply_mid = str(data.get("message_id") or "").strip()
            except Exception:
                pass
            log.info(
                "issue_watch_declare: thread reply sent chat_tail=%s parent_tail=%s "
                "api_parent_tail=%s reply_tail=%s thread=%s",
                detection_chat[-12:] if len(detection_chat) > 12 else detection_chat,
                src_mid[-12:] if len(src_mid) > 12 else src_mid,
                parent_id[-12:] if len(parent_id) > 12 else parent_id,
                reply_mid[-12:] if len(reply_mid) > 12 else reply_mid,
                _config.get_p0_issue_watch_declare_reply_in_thread(),
            )
            if _config.get_p0_issue_watch_declare_also_send_to_group():
                st_g, body_g = _lark.post_text_to_chat(detection_chat, tok, reply_post)
                ok_g, code_g, msg_g = _lark.lark_im_message_create_ok(body_g)
                if st_g != 200 or not ok_g:
                    log.warning(
                        "issue_watch_declare: also-send-to-group failed HTTP=%s code=%s chat=%s msg=%s",
                        st_g,
                        code_g,
                        detection_chat[:24],
                        (msg_g or body_g or "")[:200],
                    )
                else:
                    log.info(
                        "issue_watch_declare: also sent declare reply to main group chat_tail=%s",
                        detection_chat[-12:] if len(detection_chat) > 12 else detection_chat,
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
        "issue_watch_declare: start_p0 chat_tail=%s operator_tail=%s alert_key=%s ring_targets=%s",
        detection_chat[-12:] if len(detection_chat) > 12 else detection_chat,
        oid[-8:] if len(oid) > 8 else oid,
        key[:12] if key else "",
        len(ring_targets),
    )
    _session.start_p0(
        detection_chat,
        tok,
        oid,
        priority="P0",
        source_chat_name=group_label,
        trigger_lark_user_id=(operator_lark_user_id or "").strip(),
        silent_when_blocked=False,
        vc_ring_target_open_ids=ring_targets if _config.get_p0_vc_ring_enabled() else None,
    )
    _lark.post_text_to_open_id(oid, tok, _cards.build_issue_watch_declare_followup_text())
    if _config.get_p0_vc_ring_enabled():
        _vc_ring.maybe_prompt_oauth_dm(oid, tok)


def handle_declare_dismiss(operator_open_id: str, tenant_token: str) -> None:
    oid = (operator_open_id or "").strip()
    tok = (tenant_token or "").strip()
    if not oid or not tok:
        return
    _lark.post_text_to_open_id(oid, tok, "Acknowledged. No P0 declared for this alert.")
