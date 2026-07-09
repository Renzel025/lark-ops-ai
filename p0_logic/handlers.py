"""
Event handlers: DM message handling and Lark card actions.
"""
from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from features.overview import bitable_adjustments as _bitable_adj
from . import cards as _cards
from . import config as _config
from features.overview import drafts as _drafts
from . import lark_client as _lark
from features.overview import group_overview_store as _group_overview_store
from features.overview import overview_forwarder as _overview_forwarder
from features.session import participants as _participants
from features.session import session as _session
from . import support as _support
from . import text_processing as _text
from .perf_log import perf_log

log = logging.getLogger("lark-ops-ai")

# Avoid spamming the same DM when user taps Build overview repeatedly with no overview target (multi-group, etc.).
_DM_NO_OVERVIEW_TARGET_DEBOUNCE: Dict[str, float] = {}
_DM_NO_OVERVIEW_TARGET_DEBOUNCE_SEC = 120.0

# Card actions that require a stored overview preview — not severity / minor follow-up buttons.
_PREVIEW_WORKFLOW_ACTIONS = frozenset(
    {
        "send_preview",
        "generate_again",
        "edit_preview",
        "save_edit",
        "back_to_preview",
        "back_group_edit",
        "dismiss_sent_overview",
        "cancel_preview",
    }
)

_GROUP_OVERVIEW_EDIT_ACTIONS = frozenset({"edit_group_overview", "back_group_edit"})

# DM text after Clear draft (chat command or button) — always sent; instruction-card repost stays env-gated.
DM_DRAFT_CLEARED_PROMPT = (
    "🗑️ Draft cleared. Kindly paste screenshots or text again when you're ready."
)

_DM_OVERVIEW_MEETING_ENDED_MSG = (
    "No active meeting session for this overview\n"
    "use manual create\n"
    "Type - coe - create overview for emergency group\n"
    "Type - cog - create overview for game urgent group"
)


def _ensure_dm_preview_incident_session(
    sender_open_id: str, tenant_token: str, source_incident_chat_id: str, target_chat: str
) -> bool:
    oid = (sender_open_id or "").strip()
    if oid:
        _drafts.orphan_incident_draft_if_session_ended(oid)
        _drafts.orphan_preview_incident_if_session_ended(oid)
    src = (source_incident_chat_id or "").strip()
    tc = (target_chat or "").strip()
    if oid:
        d = _drafts.get_draft(oid) or {}
        pv = _drafts.get_preview(oid) or {}
        if not src:
            src = str(d.get("source_incident_chat_id") or pv.get("source_incident_chat_id") or "").strip()
        if not tc:
            tc = str(d.get("target_chat") or pv.get("target_chat") or "").strip()
    if _session.dm_preview_allowed_for_incident(src, tc):
        return True
    _lark.post_text_to_open_id(sender_open_id, tenant_token, _DM_OVERVIEW_MEETING_ENDED_MSG)
    return False


def _parallel_post_preview_and_edit(
    sender_open_id: str,
    tenant_token: str,
    preview_card: Dict[str, Any],
    edit_card: Optional[Dict[str, Any]],
) -> Tuple[bool, bool]:
    """
    Post/patch the preview DM card and optionally the edit DM card in parallel.
    Second bool is True when there was no edit card (nothing to patch).
    """
    if not edit_card:
        return _drafts.post_or_patch_preview_card(sender_open_id, tenant_token, preview_card), True
    with ThreadPoolExecutor(max_workers=2) as ex:
        fp = ex.submit(_drafts.post_or_patch_preview_card, sender_open_id, tenant_token, preview_card)
        fe = ex.submit(_drafts.post_or_patch_edit_card, sender_open_id, tenant_token, edit_card)
        return fp.result(), fe.result()


def _deep_get(d: Any, *path: str) -> Any:
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _card_action_value_dict(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Lark ``card.action.trigger`` may send ``event.action.value`` as a **JSON string** or a dict.
    Without parsing the string, ``value.action`` is invisible and routes fall through to the preview handler.
    """
    val = _deep_get(payload, "event", "action", "value")
    if val is None:
        val = _deep_get(payload, "action", "value")
    if isinstance(val, dict):
        return val
    if isinstance(val, str) and val.strip():
        try:
            j = json.loads(val)
            if isinstance(j, dict):
                return j
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return {}


def _extract_card_action_sender_open_id(payload: Dict[str, Any]) -> str:
    candidates = [
        _deep_get(payload, "event", "operator", "operator_id", "open_id"),
        _deep_get(payload, "event", "operator", "open_id"),
        _deep_get(payload, "event", "user", "open_id"),
        _deep_get(payload, "event", "sender", "sender_id", "open_id"),
        _deep_get(payload, "event", "open_id"),
        _deep_get(payload, "open_id"),
    ]
    for x in candidates:
        if isinstance(x, str) and x.strip():
            return x.strip()
    return ""


def _extract_card_action_operator_lark_user_id(payload: Dict[str, Any]) -> str:
    """Tenant ``user_id`` (e.g. ``SNT0006``) on ``event.operator`` — needed when DM uses a second app token."""
    candidates = [
        _deep_get(payload, "event", "operator", "user_id"),
        _deep_get(payload, "event", "operator", "operator_id", "user_id"),
    ]
    for x in candidates:
        if isinstance(x, str) and x.strip():
            return x.strip()
    return ""


def _post_dm_text_card_action(sender_open_id: str, tenant_token: str, text: str, operator_lark_user_id: str) -> None:
    """Reply in DM; use ``user_id`` receive when severity is a separate Lark app (open_id is app-scoped)."""
    oid = (sender_open_id or "").strip()
    uid = (operator_lark_user_id or "").strip()
    sid, _ssec = _config.get_lark_severity_app_credentials()
    pid, _ = _config.get_lark_primary_app_credentials()
    use_uid = bool(sid and pid and sid != pid and uid)
    if use_uid:
        _lark.post_text_to_user_cross_app(oid, uid, tenant_token, text, use_user_id=True)
    else:
        _lark.post_text_to_open_id(oid, tenant_token, text)


def _post_dm_text_primary_bot(sender_open_id: str, text: str) -> None:
    """Fallback DM using the primary app token (correct ``open_id`` scope when severity bot cannot address the user)."""
    oid = (sender_open_id or "").strip()
    if not oid:
        return
    tok = _lark.get_tenant_token_primary()
    if tok:
        _lark.post_text_to_open_id(oid, tok, text)


def _extract_dm_scope_from_card_payload(payload: Dict[str, Any]) -> Tuple[str, str, str]:
    val = _card_action_value_dict(payload)
    if not val:
        return "", "", ""
    tc = str(val.get("target_chat") or "").strip()
    src = str(val.get("source_incident_chat_id") or "").strip()
    pr = str(val.get("draft_priority") or "").strip()
    return tc, src, pr


def _maybe_merge_dm_scope_from_card(sender_open_id: str, payload: Dict[str, Any]) -> None:
    oid = (sender_open_id or "").strip()
    if not oid:
        return
    tc, src_inc, pr_scope = _extract_dm_scope_from_card_payload(payload)
    if tc.startswith("oc_"):
        _drafts.merge_dm_scope_from_card(oid, tc, src_inc, pr_scope)


def _extract_card_action_name(payload: Dict[str, Any]) -> str:
    val = _card_action_value_dict(payload)
    for key in ("action", "button_action"):
        x = val.get(key)
        if isinstance(x, str) and x.strip():
            return x.strip()
    return ""


def card_action_name_from_payload(payload: Dict[str, Any]) -> str:
    """Public helper for webhook routing (e.g. fast path before BackgroundTasks)."""
    return (_extract_card_action_name(payload) or "").strip() or "unknown"


def _show_participants_body_text() -> str:
    participants = _participants.list_meeting_participants()
    empty_msg = "ℹ️ No participants have been tracked in the meeting yet."
    if not participants:
        return empty_msg
    line = _participants.format_participants_names_display(participants)
    return empty_msg if not line.strip() else f"Participants\n{line}"


def _try_dm_cancel_standalone_overview(sender_open_id: str, tenant_token: str) -> bool:
    """Release the ``coe`` / ``cog`` DM slot (typed ``c``). Always consumes the DM line."""
    sender_open_id = (sender_open_id or "").strip()
    if not sender_open_id:
        return True
    if not _session.is_standalone_overview_active(sender_open_id):
        _lark.post_text_to_open_id(
            sender_open_id,
            tenant_token,
            "ℹ️ No standalone overview in progress.",
        )
        return True
    if _dm_has_open_preview_workflow(sender_open_id):
        _lark.post_text_to_open_id(sender_open_id, tenant_token, _CLEAR_DRAFT_USE_CANCEL_ON_PREVIEW_MSG)
        return True
    _session.release_standalone_overview_cancel(sender_open_id, tenant_token)
    _drafts.clear_draft(sender_open_id)
    _drafts.clear_preview(sender_open_id)
    _drafts.cancel_preview_timer(sender_open_id)
    _lark.post_text_to_open_id(
        sender_open_id,
        tenant_token,
        "Standalone overview cancelled. Type coe or cog to start again.",
    )
    return True


def _send_help_commands_card(sender_open_id: str, tenant_token: str) -> None:
    """Post the bilingual command reference card to the operator DM."""
    sender_open_id = (sender_open_id or "").strip()
    if not sender_open_id or not tenant_token:
        return
    card = _cards.build_help_commands_card()
    st, body, _ = _lark.post_card_to_open_id(sender_open_id, tenant_token, card)
    ok, code, msg = _lark.lark_im_message_create_ok(body)
    if st != 200 or not ok:
        log.warning(
            "show_help failed HTTP=%s lark_code=%s lark_msg=%r open_id=%s body=%s",
            st,
            code,
            msg,
            sender_open_id,
            (body or "")[:400],
        )
        _lark.post_text_to_open_id(sender_open_id, tenant_token, "⚠️ Failed to send the help card.")


def handle_lark_card_action_show_participants_sync(
    payload: Dict[str, Any], _tenant_token: str
) -> Dict[str, Any]:
    """
    Lark card.action.trigger should return toast in the HTTP response for instant UX.
    Do not call post_text_to_open_id here — that adds an extra server→Lark round trip (~300ms+).
    """
    sender_open_id = _extract_card_action_sender_open_id(payload)
    _maybe_merge_dm_scope_from_card(sender_open_id, payload)
    if not sender_open_id:
        return {
            "toast": {
                "type": "warning",
                "content": "Could not identify sender.",
            }
        }
    body = _show_participants_body_text()
    max_len = 1200
    if len(body) > max_len:
        body = body[: max_len - 1] + "…"
    return {
        "toast": {
            "type": "info",
            "content": body,
            "i18n": {
                "zh_cn": body,
                "en_us": body,
            },
        }
    }


def _extract_p1_confirm_nonce(payload: Dict[str, Any]) -> str:
    d = _card_action_value_dict(payload)
    v = d.get("p1_nonce")
    if v is None:
        v = _deep_get(payload, "event", "action", "value", "p1_nonce") or _deep_get(
            payload, "action", "value", "p1_nonce"
        )
    if v is None:
        return ""
    return str(v).strip()


def _scan_open_chat_id_nested(obj: Any) -> str:
    """Last resort: any `open_chat_id` string in nested dict/list (Lark layouts vary)."""
    if isinstance(obj, dict):
        oc = obj.get("open_chat_id")
        if isinstance(oc, str) and oc.strip().startswith("oc_"):
            return oc.strip()
        for v in obj.values():
            found = _scan_open_chat_id_nested(v)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _scan_open_chat_id_nested(item)
            if found:
                return found
    return ""


def _extract_card_action_open_chat_id(payload: Dict[str, Any]) -> str:
    """Group chat id where the interactive card was posted (incident group, etc.)."""
    # Lark card.action.trigger schema 2.0 often puts ids under event.context (host=im_message).
    candidates = [
        _deep_get(payload, "event", "context", "open_chat_id"),
        _deep_get(payload, "event", "context", "chat_id"),
        _deep_get(payload, "event", "action", "open_chat_id"),
        _deep_get(payload, "event", "action", "chat_id"),
        _deep_get(payload, "event", "open_chat_id"),
        _deep_get(payload, "event", "message", "chat_id"),
        _deep_get(payload, "action", "open_chat_id"),
    ]
    for x in candidates:
        if isinstance(x, str) and x.strip():
            return x.strip()
    return _scan_open_chat_id_nested(payload)


def _extract_card_action_open_message_id(payload: Dict[str, Any]) -> str:
    """Message id (om_...) of the card the user clicked — for recalling stale DM cards."""
    candidates = [
        _deep_get(payload, "event", "context", "open_message_id"),
        _deep_get(payload, "event", "message", "message_id"),
        _deep_get(payload, "event", "open_message_id"),
        _deep_get(payload, "open_message_id"),
    ]
    for x in candidates:
        if isinstance(x, str) and x.strip():
            return x.strip()
    return ""


def _extract_form_field(payload: Dict[str, Any], field: str) -> str:
    """Read a form field value, including empty string when the user cleared the field."""
    val_d = _card_action_value_dict(payload)
    if field in val_d and isinstance(val_d[field], str):
        return val_d[field].strip()
    candidates = [
        _deep_get(payload, "event", "action", "form_value", field),
        _deep_get(payload, "action", "form_value", field),
        _deep_get(payload, "event", "form_value", field),
        _deep_get(payload, "form_value", field),
        _deep_get(payload, "event", "action", "value", field),
        _deep_get(payload, "action", "value", field),
    ]
    for x in candidates:
        if isinstance(x, str):
            return x.strip()
    return ""


def _extract_support_input_value(payload: Dict[str, Any]) -> str:
    return _extract_form_field(payload, "support_input")


def _extract_impact_input_value(payload: Dict[str, Any]) -> str:
    return _extract_form_field(payload, "impact_input")


def _extract_issue_input_value(payload: Dict[str, Any]) -> str:
    return _extract_form_field(payload, "issue_input")


def _extract_incident_start_datetime_value(payload: Dict[str, Any]) -> str:
    """Lark ``picker_datetime`` in forms: string or ``{ option: ... }`` in form_value."""
    s = _extract_form_field(payload, "incident_start_datetime")
    if s:
        return s
    fv = _deep_get(payload, "event", "action", "form_value", "incident_start_datetime")
    if isinstance(fv, dict):
        return str(fv.get("option") or fv.get("value") or "").strip()
    if isinstance(fv, str) and fv.strip():
        return fv.strip()
    return ""


def _form_field_left_blank(manual: str) -> bool:
    """True if user did not enter a new value — keep preview field as-is."""
    s = (manual or "").strip()
    return (not s) or _text.is_not_specified(s)


def _overview_field_complete(value: str) -> bool:
    s = (value or "").strip()
    if not s:
        return False
    if _text.is_not_specified(s):
        return False
    if re.fullmatch(r"[-–—]+", s):
        return False
    return True


def _missing_overview_fields_for_send(
    issue: str, impact: str, support: str, combined_text: str = ""
) -> List[str]:
    """Human labels for fields that must be filled before **Send to group**."""
    missing: List[str] = []
    if not (combined_text or "").strip():
        missing.append("Incident details (paste text or screenshots in DM, then Build overview)")
    if not _overview_field_complete(issue):
        missing.append("Issue")
    if not _overview_field_complete(impact):
        missing.append("Impact scope")
    if not _overview_field_complete(support):
        missing.append("Support request")
    return missing


def _preview_priority(preview: Dict[str, Any]) -> str:
    pr = str((preview or {}).get("priority") or "P0").strip().upper()
    return pr if pr in ("P0", "P1") else "P0"


def _finalize_group_overview_for_edit(
    tenant_token: str,
    *,
    group_chat_id: str,
    group_message_id: str,
    md: str,
    priority: str,
    source_chat_label: str,
    target_chat: str,
    source_incident_chat_id: str,
    combined_text: str,
    mention_names: List[str],
    issue: str,
    impact: str,
    support: str,
    start_epoch: int,
    sent_by_open_id: str,
) -> None:
    """Store overview snapshot and PATCH group message to add **Edit overview** (update_multi)."""
    if not _group_overview_store.group_overview_edit_enabled():
        return
    cid = (group_chat_id or "").strip()
    mid = (group_message_id or "").strip()
    if not cid.startswith("oc_") or not mid:
        return
    pri = (priority or "P0").strip().upper()
    if pri not in ("P0", "P1"):
        pri = "P0"
    lab = (source_chat_label or "").strip()
    src = (source_incident_chat_id or "").strip()
    tc = (target_chat or "").strip()
    md_s = (md or "").strip()
    _group_overview_store.save_group_overview(
        group_chat_id=cid,
        group_message_id=mid,
        md=md_s,
        issue=issue,
        impact=impact,
        support=support,
        combined_text=combined_text,
        mention_names=mention_names,
        start_epoch=start_epoch,
        priority=pri,
        target_chat=tc,
        source_incident_chat_id=src,
        source_chat_label=lab,
        sent_by_open_id=sent_by_open_id,
    )
    card = _cards.build_overview_result_card(
        md_s,
        priority=pri,
        source_chat_label=lab,
        target_chat=tc,
        source_incident_chat_id=src,
        allow_group_edit=False,
    )
    primary_ok, _ = _patch_group_overview_cards(tenant_token, cid, mid, card)
    if not primary_ok:
        log.warning(
            "group_overview_edit: PATCH failed HTTP chat_id=%s message_id=%s",
            cid,
            mid[:20] + "…" if len(mid) > 20 else mid,
        )
    else:
        log.info("group_overview_edit: group overview stored for DM Edit chat_id=%s message_id=%s", cid, mid)


def handle_group_overview_recalled(
    tenant_token: str, group_chat_id: str, group_message_id: str
) -> None:
    """
    A **sent** group overview was recalled (``im.message.recalled_v1``). If we still have its
    snapshot, re-DM the preview (Send/Edit) to the operator who sent it so they can resend.
    Idempotent: the snapshot is popped, so a redelivered recall event is a no-op.
    """
    if not _config.get_p0_overview_recall_restore_enabled():
        return
    cid = (group_chat_id or "").strip()
    mid = (group_message_id or "").strip()
    if not cid or not mid:
        return
    row = _group_overview_store.pop_group_overview(cid, mid)
    if not row:
        return  # not a tracked overview, or already restored
    oid = str(row.get("sent_by_open_id") or "").strip()
    if not oid:
        log.warning(
            "overview recall restore: no sent_by_open_id — cannot DM chat_id=%s message_id=%s",
            cid,
            mid[:20] + "…" if len(mid) > 20 else mid,
        )
        return
    tok = (tenant_token or "").strip()
    if not tok:
        return
    # Cascade: also recall the Overview-bot broadcast copy so a re-Send does not duplicate it.
    bc_mid = str(row.get("broadcast_message_id") or "").strip()
    if bc_mid and _config.lark_overview_forwarder_enabled() and _config.get_lark_overview_forwarder_url():
        recalled = _overview_forwarder.recall_overview_via_forwarder(bc_mid)
        log.info(
            "overview recall restore: cascade broadcast recall message_id=%s ok=%s",
            bc_mid[:20] + "…" if len(bc_mid) > 20 else bc_mid,
            recalled,
        )
    target_chat = str(row.get("target_chat") or "").strip()
    src = str(row.get("source_incident_chat_id") or "").strip()
    pri = str(row.get("priority") or "P0").strip().upper()
    if pri not in ("P0", "P1"):
        pri = "P0"
    md = _drafts.restore_preview_after_recall(
        oid,
        target_chat=target_chat,
        start_epoch=int(row.get("start_epoch") or 0),
        combined_text=str(row.get("combined_text") or ""),
        mention_names=list(row.get("mention_names") or []),
        issue=str(row.get("issue") or ""),
        impact=str(row.get("impact") or ""),
        support=str(row.get("support") or ""),
        md=str(row.get("md") or ""),
        priority=pri,
        source_incident_chat_id=src,
    )
    _lark.post_text_to_open_id(
        oid,
        tok,
        "↩️ The overview you sent to the group was recalled — here's the preview again. "
        "Edit if needed, then tap **Send to group**.",
    )
    lab = _session.get_source_chat_label_for_target_chat(target_chat)
    card = _cards.build_preview_card(
        md,
        priority=pri,
        source_chat_label=lab,
        target_chat=target_chat,
        source_incident_chat_id=src,
    )
    ok = _drafts.post_or_patch_preview_card(oid, tok, card)
    log.info(
        "overview recall restore: re-DM preview open_id_tail=%s chat_id=%s posted=%s",
        oid[-8:] if len(oid) > 8 else oid,
        cid,
        ok,
    )


def _patch_group_overview_cards(
    tenant_token: str, group_chat_id: str, group_message_id: str, card: Dict[str, Any]
) -> Tuple[bool, bool]:
    """
    PATCH the primary-bot overview in the group, then the overview-bot copy (lark-forwarder) if linked.
    Returns ``(primary_ok, broadcast_ok)``; ``broadcast_ok`` is True when there is no broadcast row to patch.
    """
    cid = (group_chat_id or "").strip()
    mid = (group_message_id or "").strip()
    primary_ok = False
    if mid:
        st, body = _lark.patch_interactive_card(tenant_token, mid, card)
        primary_ok = st == 200
        if not primary_ok:
            log.warning(
                "patch group overview primary failed HTTP=%s chat_id=%s body=%s",
                st,
                cid,
                (body or "")[:350],
            )
    broadcast_ok = True
    row = _group_overview_store.get_group_overview(cid, mid) or {}
    bmid = str(row.get("broadcast_message_id") or "").strip()
    if bmid and _config.lark_overview_forwarder_enabled() and _config.get_lark_overview_forwarder_url():
        broadcast_ok = _overview_forwarder.patch_overview_via_forwarder(bmid, card)
    return primary_ok, broadcast_ok


def _recall_send_block_warning_dm(sender_open_id: str, tenant_token: str) -> None:
    """Delete the stale 'Cannot send to group' text after fields are fixed or send succeeds."""
    warn_mid = _drafts.take_send_block_warning_message_id(sender_open_id)
    if not warn_mid:
        return
    st, body = _lark.recall_im_message(tenant_token, warn_mid)
    if st != 200:
        log.warning(
            "recall send-block warning failed HTTP=%s open_id=%s body=%s",
            st,
            sender_open_id,
            (body or "")[:300],
        )


def _post_send_block_warning_dm(sender_open_id: str, tenant_token: str, text: str) -> None:
    _recall_send_block_warning_dm(sender_open_id, tenant_token)
    from p0_logic import monitoring_notify as _monitor

    st, body = _monitor.post_duty_dm(
        sender_open_id,
        tenant_token,
        text,
        monitor_label="overview send blocked",
    )
    if st != 200:
        log.warning(
            "send-block warning POST failed HTTP=%s open_id=%s body=%s",
            st,
            sender_open_id,
            (body or "")[:300],
        )
        return
    mid = _lark.parse_im_message_id_from_response(body)
    if mid:
        _drafts.set_send_block_warning_message_id(sender_open_id, mid)


def _overview_send_pending_note(*, use_forwarder: bool, broadcast_dest: str) -> str:
    tail: List[str] = []
    if use_forwarder and (broadcast_dest or "").strip().startswith("oc_"):
        tail.append("broadcast room")
    if _config.p0_adjustment_bitable_enabled() and _config.get_p0_adjustment_bitable_sources():
        tail.append("deployment notices")
    if not tail:
        return ""
    return "⏳ Finishing " + " & ".join(tail) + "…"


def _post_dm_overview_sent_card(
    sender_open_id: str,
    tenant_token: str,
    *,
    pri: str,
    lab: str,
    lark_overview_dest: str,
    post_mid: str,
    target_chat: str,
    src_inc: str,
    forwarder_warning: str = "",
) -> None:
    """Replace blue preview DM with green sent card (removes Send to group immediately)."""
    extra = (forwarder_warning or "").strip()
    if not (post_mid or "").strip():
        return
    if not _group_overview_store.group_overview_edit_enabled():
        _lark.post_text_to_open_id(
            sender_open_id,
            tenant_token,
            "✅ Overview sent to the target group chat." + (f"\n{extra}" if extra else ""),
        )
        return
    sent_card = _cards.build_dm_overview_sent_card(
        pri,
        source_chat_label=lab,
        group_chat_id=lark_overview_dest,
        group_message_id=post_mid,
        target_chat=target_chat,
        source_incident_chat_id=src_inc,
        forwarder_warning=extra,
    )
    if not _drafts.post_or_patch_preview_card(sender_open_id, tenant_token, sent_card):
        _lark.post_text_to_open_id(
            sender_open_id,
            tenant_token,
            "✅ Overview sent to the target group chat." + (f"\n{extra}" if extra else ""),
        )


def _recall_dm_overview_edit_card(sender_open_id: str, tenant_token: str) -> None:
    """Remove the DM edit form so only the sent-overview card stays visible."""
    edit_mid = _drafts.take_edit_message_id(sender_open_id)
    if edit_mid:
        st, body = _lark.recall_im_message(tenant_token, edit_mid)
        if st != 200:
            log.warning(
                "recall edit card failed HTTP=%s open_id=%s body=%s",
                st,
                sender_open_id,
                (body or "")[:300],
            )
    _drafts.clear_preview_edit_flags(sender_open_id)


def _handle_edit_group_overview(
    payload: Dict[str, Any], tenant_token: str, sender_open_id: str
) -> None:
    if not _group_overview_store.group_overview_edit_enabled():
        _lark.post_text_to_open_id(
            sender_open_id,
            tenant_token,
            "ℹ️ Group overview edit is disabled (P0_GROUP_OVERVIEW_EDIT_ENABLED=0).",
        )
        return
    val = _card_action_value_dict(payload)
    gcid = str(val.get("group_chat_id") or "").strip()
    gmid = str(val.get("group_message_id") or "").strip()
    state = _group_overview_store.get_group_overview(gcid, gmid)
    if not state:
        _lark.post_text_to_open_id(
            sender_open_id,
            tenant_token,
            "⚠️ Cannot edit this overview (too old, or it was posted before group edit was enabled).",
        )
        return
    _drafts.load_group_overview_edit_session(sender_open_id, state)
    _drafts.set_preview_edit_waiting(sender_open_id, True)
    lab = str(state.get("source_chat_label") or "").strip()
    if not lab:
        lab = _session.get_source_chat_label_for_target_chat(str(state.get("target_chat") or ""))
    pri = str(state.get("priority") or "P0").strip().upper()
    edit_card = _cards.build_edit_overview_card(
        str(state.get("issue") or ""),
        str(state.get("impact") or ""),
        str(state.get("support") or ""),
        priority=pri,
        source_chat_label=lab,
        start_epoch=int(state.get("start_epoch") or 0),
        editing_group_overview=True,
    )
    if not _drafts.post_or_patch_edit_card(sender_open_id, tenant_token, edit_card):
        _lark.post_text_to_open_id(sender_open_id, tenant_token, "⚠️ Failed to open edit form in DM.")
        return


def _dm_has_open_preview_workflow(sender_open_id: str) -> bool:
    """True when a preview or edit flow is active — operators should use Cancel on the preview card, not Clear draft."""
    pv = _drafts.get_preview(sender_open_id) or {}
    if str(pv.get("md") or "").strip():
        return True
    if pv.get("awaiting_edit_input"):
        return True
    return False


_CLEAR_DRAFT_USE_CANCEL_ON_PREVIEW_MSG = (
    "ℹ️ You have an overview preview open. Use **Cancel** on the preview card to discard it (or **Send to group**). "
    "**Clear draft** only applies before you tap **Build overview**."
)


def _dm_card_meta(sender_open_id: str) -> Tuple[str, str]:
    """(source_chat_label, priority) for DM instruction card titles."""
    draft = _drafts.get_draft(sender_open_id) or {}
    tc = str(draft.get("target_chat") or "").strip() or (_session.get_active_target_chat() or "")
    lab = _session.get_source_chat_label_for_target_chat(tc)
    _cid, sess = _session.find_session_by_target_chat(tc) if tc else ("", {})
    pr = str((sess or {}).get("priority") or "P0").strip().upper()
    if pr not in ("P0", "P1"):
        pr = "P0"
    return lab, pr


def _send_instruction_card(
    sender_open_id: str,
    tenant_token: str,
    note: Optional[str] = None,
    *,
    repost_instruction_card: bool = True,
    priority: str = "P0",
    source_chat_label: str = "",
) -> None:
    if note:
        _lark.post_text_to_open_id(sender_open_id, tenant_token, note)
    if repost_instruction_card:
        d = _drafts.get_draft(sender_open_id) or {}
        tc = str(d.get("target_chat") or "").strip()
        src = str(d.get("source_incident_chat_id") or "").strip()
        _, _, _ = _lark.post_card_to_open_id(
            sender_open_id,
            tenant_token,
            _cards.build_dm_instruction_card(
                priority,
                source_chat_label=source_chat_label,
                target_chat=tc,
                source_incident_chat_id=src,
            ),
        )


def _generate_preview_now(sender_open_id: str, tenant_token: str) -> bool:
    draft = _drafts.get_draft(sender_open_id)
    if not draft:
        _lark.post_text_to_open_id(sender_open_id, tenant_token, "⚠️ No draft yet. Please paste screenshots or text first.")
        return False
    target_chat = str(draft.get("target_chat") or "").strip()
    if not target_chat:
        _lark.post_text_to_open_id(sender_open_id, tenant_token, "⚠️ No active target chat found.")
        return False
    src_inc = str(draft.get("source_incident_chat_id") or "").strip()
    if not _ensure_dm_preview_incident_session(sender_open_id, tenant_token, src_inc, target_chat):
        return False
    _chat_id, sess = _session.find_session_by_target_chat(target_chat)
    if sess:
        start_epoch = int(sess.get("start_epoch") or time.time())
    else:
        # Draft tied to ``OVERVIEW_TARGET_GROUP_CHAT_ID`` only — no live P0 row; use wall clock for overview header.
        start_epoch = int(time.time())
    md = _drafts.build_preview_from_draft(sender_open_id=sender_open_id, tenant_token=tenant_token, target_chat=target_chat, start_epoch=start_epoch, draft=draft)
    if not md:
        _lark.post_text_to_open_id(sender_open_id, tenant_token, "⚠️ Draft is empty.")
        return False
    prev = _drafts.get_preview(sender_open_id) or {}
    pr = _preview_priority(prev)
    lab = _session.get_source_chat_label_for_target_chat(target_chat)
    card = _cards.build_preview_card(
        md,
        priority=pr,
        source_chat_label=lab,
        update_multi=True,
        target_chat=target_chat,
        source_incident_chat_id=str(draft.get("source_incident_chat_id") or "").strip(),
    )
    if not _drafts.post_or_patch_preview_card(sender_open_id, tenant_token, card):
        _lark.post_text_to_open_id(sender_open_id, tenant_token, "❌ Failed to send or update preview card.")
        return False
    return True


def _recover_preview_from_draft_if_needed(
    sender_open_id: str, tenant_token: str, action_name: str
) -> bool:
    """
    Rebuild persisted preview from draft when the preview JSON is missing (restart, worker race,
    rare store glitch) but draft content and ``oc_`` scope are still present.
    """
    if action_name == "cancel_preview":
        return False
    draft = _drafts.get_draft(sender_open_id)
    if not draft:
        return False
    target_chat = str(draft.get("target_chat") or "").strip()
    if not target_chat.startswith("oc_"):
        return False
    src_inc = str(draft.get("source_incident_chat_id") or "").strip()
    if not _ensure_dm_preview_incident_session(sender_open_id, tenant_token, src_inc, target_chat):
        return False
    _chat_id, sess = _session.find_session_by_target_chat(target_chat)
    start_epoch = int(sess.get("start_epoch") or time.time()) if sess else int(time.time())
    md = _drafts.build_preview_from_draft(
        sender_open_id=sender_open_id,
        tenant_token=tenant_token,
        target_chat=target_chat,
        start_epoch=start_epoch,
        draft=draft,
    )
    if not md or not _drafts.get_preview(sender_open_id):
        return False
    log.info(
        "preview recovered from draft action=%s open_id_tail=%s",
        action_name,
        sender_open_id[-8:] if len(sender_open_id) > 8 else sender_open_id,
    )
    return True


def handle_dm_generate_overview(
    sender_open_id: str,
    tenant_token: str,
    text: Optional[str] = None,
    image_key: Optional[str] = None,
    mention_names: Optional[List[str]] = None,
    message_id: Optional[str] = None,
) -> None:
    sender_open_id = (sender_open_id or "").strip()
    mention_names = mention_names or []
    message_id = (message_id or "").strip()
    if text:
        cmd = _text.clean_pasted_text(text).strip()
        if _config.HELP_RE.match(cmd):
            _send_help_commands_card(sender_open_id, tenant_token)
            return
        if _config.STANDALONE_OVERVIEW_ABORT_RE.match(cmd):
            _try_dm_cancel_standalone_overview(sender_open_id, tenant_token)
            return
        tag = _config.parse_standalone_overview_dm_command(cmd)
        if tag:
            tag = (tag or "").strip().lower()
            blocked = _session.note_if_standalone_create_overview_blocked(sender_open_id, tenant_token)
            if blocked:
                _lark.post_text_to_open_id(sender_open_id, tenant_token, blocked)
                return
            tc = _config.get_standalone_overview_target_chat_id_for_tag(tag)
            if not tc:
                _lark.post_text_to_open_id(
                    sender_open_id,
                    tenant_token,
                    f'⚠️ Could not resolve the overview group for "{tag}". '
                    "Set P0_STANDALONE_OVERVIEW_TAGS=emergency=oc_...,game=oc_... "
                    'or INCIDENT_GROUP_EMERGENCY_TOPICS so one label contains "emergency" and one contains "game" (or 游戏).',
                )
                return
            lab = _config.get_emergency_topic_for_source_chat(tc).strip() or f"{tag} overview"
            _session.enqueue_dm_instruction_if_needed(
                sender_open_id,
                tenant_token,
                {
                    "chat_id": _session.STANDALONE_DM_SOURCE_CHAT_ID,
                    "target_chat": tc,
                    "priority": "P0",
                    "label": lab,
                },
            )
            return

    draft_existing = _drafts.get_draft(sender_open_id)
    if draft_existing and str(draft_existing.get("target_chat") or "").strip():
        target_chat = str(draft_existing.get("target_chat") or "").strip()
    else:
        target_chat = _session.get_dm_target_chat_for_operator(sender_open_id)
    if not target_chat:
        pv = _drafts.get_preview(sender_open_id) or {}
        target_chat = str(pv.get("target_chat") or "").strip()
    if not target_chat:
        target_chat = _config.get_dm_overview_target_chat_id()
    if not target_chat:
        now = time.time()
        last = _DM_NO_OVERVIEW_TARGET_DEBOUNCE.get(sender_open_id, 0.0)
        if now - last >= _DM_NO_OVERVIEW_TARGET_DEBOUNCE_SEC:
            _DM_NO_OVERVIEW_TARGET_DEBOUNCE[sender_open_id] = now
            _lark.post_text_to_open_id(
                sender_open_id,
                tenant_token,
                "⚠️ No incident target for this DM. Type **create overview emergency** or **create overview game**, "
                "or **p0** in the group → green card → **Build overview**.",
            )
        else:
            log.info(
                "DM overview: no target chat (debounced, no repeat DM) open_id=%s",
                sender_open_id[-8:] if sender_open_id else "",
            )
        return
    src_text = _text.clean_pasted_text(text)
    preview = _drafts.get_preview(sender_open_id) or {}
    if image_key:
        try:
            _drafts.add_image_to_draft(sender_open_id=sender_open_id, target_chat=target_chat, tenant_token=tenant_token, image_key=image_key, message_id=message_id, mention_names=mention_names)
            return
        except Exception as e:
            log.error("Failed to add image to draft: %s", e)
            _lark.post_text_to_open_id(sender_open_id, tenant_token, f"❌ Failed to process screenshot.\n{e}")
            return
    if src_text:
        if preview.get("awaiting_edit_input"):
            _lark.post_text_to_open_id(
                sender_open_id,
                tenant_token,
                "ℹ️ Use the Edit card to update Issue, Impact, and Support (or tap Back on that card).",
            )
            return
        if _config.WHO_IN_MEETING_RE.match(src_text):
            participants = _participants.list_meeting_participants()
            if not participants:
                _lark.post_text_to_open_id(sender_open_id, tenant_token, "ℹ️ No participants have been tracked in the meeting yet.")
                return
            line = _participants.format_participants_names_display(participants)
            if not line.strip():
                _lark.post_text_to_open_id(sender_open_id, tenant_token, "ℹ️ No participants have been tracked in the meeting yet.")
                return
            _lark.post_text_to_open_id(sender_open_id, tenant_token, f"Participants\n{line}")
            return
        m = _config.IS_IN_MEETING_RE.match(src_text)
        if m:
            person = (m.group(1) or "").strip()
            if not person:
                _lark.post_text_to_open_id(sender_open_id, tenant_token, "ℹ️ Please provide a name.")
                return
            if _participants.is_person_in_meeting(person):
                _lark.post_text_to_open_id(sender_open_id, tenant_token, f"✅ Yes, {person} is currently in the meeting.")
            else:
                _lark.post_text_to_open_id(sender_open_id, tenant_token, f"❌ No, {person} is not currently in the meeting.")
            return
        if _config.CLEAR_RE.match(src_text):
            if _dm_has_open_preview_workflow(sender_open_id):
                _lark.post_text_to_open_id(sender_open_id, tenant_token, _CLEAR_DRAFT_USE_CANCEL_ON_PREVIEW_MSG)
                return
            _drafts.clear_draft(sender_open_id)
            _drafts.clear_preview(sender_open_id)
            _drafts.cancel_preview_timer(sender_open_id)
            _lark.post_text_to_open_id(sender_open_id, tenant_token, DM_DRAFT_CLEARED_PROMPT)
            if _config.get_dm_repost_instruction_after_reset():
                lab, pr = _dm_card_meta(sender_open_id)
                _send_instruction_card(
                    sender_open_id, tenant_token, None, priority=pr, source_chat_label=lab
                )
            return
        if _config.STATUS_RE.match(src_text):
            draft = _drafts.get_draft(sender_open_id)
            if not draft:
                _lark.post_text_to_open_id(sender_open_id, tenant_token, "ℹ️ No active draft yet.")
                return
            _lark.post_text_to_open_id(sender_open_id, tenant_token, _drafts.draft_summary_text(draft))
            return
        _drafts.add_text_to_draft(sender_open_id=sender_open_id, target_chat=target_chat, text=src_text, mention_names=mention_names)
        return


def handle_lark_card_action(payload: Dict[str, Any], tenant_token: str) -> None:
    from features.overview import issues as _issues

    action_name = card_action_name_from_payload(payload)
    if action_name == "unknown":
        action_name = ""
    t0 = time.perf_counter()
    try:
        sender_open_id = _extract_card_action_sender_open_id(payload)
        _maybe_merge_dm_scope_from_card(sender_open_id, payload)

        if action_name == "p1_confirm_meeting_yes":
            chat_id = _extract_card_action_open_chat_id(payload)
            if not chat_id:
                log.warning("p1_confirm_meeting_yes missing open_chat_id payload=%s", json.dumps(payload, ensure_ascii=False)[:2000])
                return
            nonce = _extract_p1_confirm_nonce(payload)
            err = _session.handle_p1_meeting_confirm_yes(chat_id, tenant_token, sender_open_id, nonce)
            if err == "session_active":
                if sender_open_id:
                    _lark.post_text_to_open_id(sender_open_id, tenant_token, "ℹ️ A meeting session is already active.")
                return
            if err == "stale":
                if sender_open_id:
                    _lark.post_text_to_open_id(
                        sender_open_id,
                        tenant_token,
                        "ℹ️ This P1 confirmation is out of date or was already answered.",
                    )
                return
            return

        if action_name == "p1_confirm_meeting_no":
            chat_id = _extract_card_action_open_chat_id(payload)
            if not chat_id:
                return
            nonce = _extract_p1_confirm_nonce(payload)
            err = _session.handle_p1_meeting_confirm_no(chat_id, tenant_token, nonce)
            if err == "session_active":
                _lark.post_text_to_chat(
                    chat_id,
                    tenant_token,
                    "ℹ️ A meeting is already active in this chat. Just type **cancel meeting** if you want to end it.",
                )
                return
            if err == "stale":
                if sender_open_id:
                    _lark.post_text_to_open_id(
                        sender_open_id,
                        tenant_token,
                        "ℹ️ This P1 confirmation is out of date or was already answered.",
                    )
                return
            return

        if not sender_open_id or not action_name:
            log.warning("card.action.trigger missing sender/action payload=%s", json.dumps(payload, ensure_ascii=False)[:4000])
            return
        if action_name in ("issue_watch_use_overview", "issue_watch_manual_overview"):
            from features.issue_watch import issue_watch_overview as _iwo

            val = _card_action_value_dict(payload)
            alert_key = str(val.get("issue_watch_alert_key") or "").strip()
            tc, src_inc, _pr = _extract_dm_scope_from_card_payload(payload)
            if action_name == "issue_watch_use_overview":
                _iwo.handle_use_suggested_overview(
                    sender_open_id,
                    tenant_token,
                    alert_key=alert_key,
                    source_incident_chat_id=src_inc,
                    target_chat=tc,
                )
            else:
                _iwo.handle_manual_overview(
                    sender_open_id,
                    tenant_token,
                    alert_key=alert_key,
                    source_incident_chat_id=src_inc,
                    target_chat=tc,
                    clicked_card_message_id=_extract_card_action_open_message_id(payload),
                )
            return
        if action_name in ("issue_watch_declare_p0", "issue_watch_declare_p0_dismiss"):
            from features.issue_watch import issue_watch_declare as _iwd

            val = _card_action_value_dict(payload)
            if action_name == "issue_watch_declare_p0_dismiss":
                _iwd.handle_declare_dismiss(sender_open_id, tenant_token)
            else:
                tc, src_inc, _pr = _extract_dm_scope_from_card_payload(payload)
                _iwd.handle_declare_p0(
                    sender_open_id,
                    tenant_token,
                    alert_key=str(val.get("issue_watch_alert_key") or "").strip(),
                    source_incident_chat_id=src_inc,
                    source_message_id=str(val.get("issue_watch_source_message_id") or "").strip(),
                    operator_lark_user_id=_extract_card_action_operator_lark_user_id(payload),
                )
            return
        if action_name == "generate_preview":
            _generate_preview_now(sender_open_id, tenant_token)
            return
        if action_name == "clear_draft":
            if _dm_has_open_preview_workflow(sender_open_id):
                _lark.post_text_to_open_id(sender_open_id, tenant_token, _CLEAR_DRAFT_USE_CANCEL_ON_PREVIEW_MSG)
                return
            _drafts.clear_draft(sender_open_id)
            _drafts.clear_preview(sender_open_id)
            _drafts.cancel_preview_timer(sender_open_id)
            _lark.post_text_to_open_id(sender_open_id, tenant_token, DM_DRAFT_CLEARED_PROMPT)
            if _config.get_dm_repost_instruction_after_reset():
                lab, pr = _dm_card_meta(sender_open_id)
                _send_instruction_card(
                    sender_open_id, tenant_token, None, priority=pr, source_chat_label=lab
                )
            return
        if action_name == "show_participants":
            # Primary path: main.lark_webhook returns toast synchronously (handle_lark_card_action_show_participants_sync).
            # This branch only runs if handle_lark_card_action is invoked without that routing (e.g. tests or legacy callers).
            body = _show_participants_body_text()
            _lark.post_text_to_open_id(sender_open_id, tenant_token, body)
            return
        if action_name == "show_help":
            _send_help_commands_card(sender_open_id, tenant_token)
            return
        if action_name == "edit_group_overview":
            _handle_edit_group_overview(payload, tenant_token, sender_open_id)
            return
        if action_name not in _PREVIEW_WORKFLOW_ACTIONS:
            log.warning("Unknown card action: %s", action_name)
            return
        preview = _drafts.get_preview(sender_open_id)
        if not preview:
            _recover_preview_from_draft_if_needed(sender_open_id, tenant_token, action_name)
            preview = _drafts.get_preview(sender_open_id)
        if not preview:
            _lark.post_text_to_open_id(
                sender_open_id,
                tenant_token,
                "⚠️ No overview preview in this bot session. Open the **green** card from the primary bot, "
                "paste details, then tap **Build overview**. (Declaring P0 alone does not create a preview.)",
            )
            return
        target_chat = str(preview.get("target_chat") or "").strip()
        start_epoch = int(preview.get("start_epoch") or 0)
        combined_text = str(preview.get("combined_text") or "").strip()
        mention_names = list(preview.get("mention_names") or [])
        issue = str(preview.get("issue") or "Not specified").strip()
        impact = str(preview.get("impact") or "Not specified").strip()
        support = str(preview.get("support") or "Not specified").strip()
        md = str(preview.get("md") or "").strip()
        pri = _preview_priority(preview)
        src_inc = str(preview.get("source_incident_chat_id") or "").strip()
        if action_name in ("send_preview", "generate_again", "edit_preview", "save_edit", "back_to_preview"):
            if not _ensure_dm_preview_incident_session(sender_open_id, tenant_token, src_inc, target_chat):
                return
        if action_name == "send_preview":
            if not target_chat or not md:
                _lark.post_text_to_open_id(sender_open_id, tenant_token, "⚠️ Preview is incomplete.")
                return
            missing_fields = _missing_overview_fields_for_send(
                issue, impact, support, combined_text
            )
            if missing_fields:
                lines = "\n".join(f"Missing fields - {name}" for name in missing_fields)
                _post_send_block_warning_dm(
                    sender_open_id,
                    tenant_token,
                    "⚠️ Cannot send to group — complete all overview details first:\n"
                    f"{lines}\n\n"
                    "Tap Edit on the preview card → fill every field → Save, "
                    "then tap Send to group again.",
                )
                log.info(
                    "send_preview blocked incomplete fields open_id_tail=%s missing=%s",
                    sender_open_id[-12:] if len(sender_open_id) > 12 else sender_open_id,
                    missing_fields,
                )
                return
            _recall_send_block_warning_dm(sender_open_id, tenant_token)
            claimed, claim_reason = _drafts.try_claim_overview_send(sender_open_id)
            if not claimed:
                if claim_reason == "already_sent":
                    _lark.post_text_to_open_id(
                        sender_open_id,
                        tenant_token,
                        "ℹ️ Overview was already sent to the group. "
                        "Use **Edit overview** on the green card if you need changes.",
                    )
                elif claim_reason == "in_progress":
                    _lark.post_text_to_open_id(
                        sender_open_id,
                        tenant_token,
                        "⏳ Overview is still posting — please wait (do not tap Send again).",
                    )
                else:
                    _lark.post_text_to_open_id(
                        sender_open_id,
                        tenant_token,
                        "⚠️ Could not send — no active preview. Tap **Build overview** again.",
                    )
                log.info(
                    "send_preview: blocked duplicate open_id_tail=%s reason=%s",
                    sender_open_id[-12:] if len(sender_open_id) > 12 else sender_open_id,
                    claim_reason or "unknown",
                )
                return
            edit_mid = str(preview.get("edit_message_id") or "").strip()
            preview_mid = str(preview.get("preview_message_id") or "").strip()
            lab = _session.get_source_chat_label_for_target_chat(target_chat)
            card = _cards.build_overview_result_card(md, priority=pri, source_chat_label=lab)
            lark_overview_dest, broadcast_dest, use_forwarder = _config.resolve_overview_send_routing(
                src_inc, target_chat
            )
            if not lark_overview_dest.startswith("oc_"):
                _lark.post_text_to_open_id(sender_open_id, tenant_token, "⚠️ No valid overview destination chat.")
                _drafts.release_overview_send_claim(sender_open_id)
                return
            log.info(
                "send_preview: Lark overview primary chat_id=%s broadcast=%s forwarder=%s "
                "(source_incident=%s session_target_chat=%s)",
                lark_overview_dest,
                broadcast_dest or "(none)",
                use_forwarder,
                src_inc or "(empty)",
                target_chat or "(empty)",
            )
            # Post to overview group (may differ from session target_chat when cards stay in incident chat).
            st, body, post_mid = _lark.post_card_to_chat(lark_overview_dest, tenant_token, card)
            api_ok, lark_code, lark_msg = _lark.lark_im_message_create_ok(body)
            if st != 200 or not api_ok:
                log.error(
                    "send_preview failed HTTP=%s lark_code=%s lark_msg=%r dest=%s body_head=%s",
                    st,
                    lark_code,
                    lark_msg,
                    lark_overview_dest,
                    (body or "")[:400],
                )
                _lark.post_text_to_open_id(
                    sender_open_id,
                    tenant_token,
                    "❌ Lark refused the overview post (see server log: lark_code / bot in group?).",
                )
                _drafts.release_overview_send_claim(sender_open_id)
                return
            post_mid = (post_mid or "").strip()
            if not post_mid:
                log.warning(
                    "send_preview: Lark code=0 but empty message_id — verify group feed dest=%s body_head=%s",
                    lark_overview_dest,
                    (body or "")[:350],
                )
                _drafts.release_overview_send_claim(sender_open_id)
            else:
                log.info(
                    "send_preview: posted overview message_id=%s dest_chat_id=%s (session_target_chat=%s)",
                    post_mid,
                    lark_overview_dest,
                    target_chat,
                )
                _drafts.record_overview_group_post(
                    sender_open_id,
                    group_chat_id=lark_overview_dest,
                    group_message_id=post_mid,
                )
                _finalize_group_overview_for_edit(
                    tenant_token,
                    group_chat_id=lark_overview_dest,
                    group_message_id=post_mid,
                    md=md,
                    priority=pri,
                    source_chat_label=lab,
                    target_chat=target_chat,
                    source_incident_chat_id=src_inc,
                    combined_text=combined_text,
                    mention_names=mention_names,
                    issue=issue,
                    impact=impact,
                    support=support,
                    start_epoch=start_epoch,
                    sent_by_open_id=sender_open_id,
                )
                pending_note = _overview_send_pending_note(
                    use_forwarder=use_forwarder,
                    broadcast_dest=broadcast_dest,
                )
                _post_dm_overview_sent_card(
                    sender_open_id,
                    tenant_token,
                    pri=pri,
                    lab=lab,
                    lark_overview_dest=lark_overview_dest,
                    post_mid=post_mid,
                    target_chat=target_chat,
                    src_inc=src_inc,
                    forwarder_warning=pending_note,
                )
            forwarder_ok = True
            forwarder_mid = ""
            if use_forwarder and broadcast_dest and md:
                forwarder_ok, forwarder_mid = _overview_forwarder.post_overview_via_forwarder(
                    md,
                    chat_id=broadcast_dest,
                    priority=pri,
                    source_label=lab,
                    card=card,
                )
                if forwarder_ok and forwarder_mid and post_mid:
                    _group_overview_store.attach_broadcast_message(
                        lark_overview_dest,
                        post_mid,
                        broadcast_chat_id=broadcast_dest,
                        broadcast_message_id=forwarder_mid,
                    )
                elif forwarder_ok and not forwarder_mid:
                    log.warning(
                        "send_preview: forwarder ok but no message_id — upgrade lark-forwarder for editable broadcast overviews"
                    )
                if not forwarder_ok:
                    log.warning(
                        "send_preview: overview forwarder failed broadcast_dest=%s (primary post ok)",
                        broadcast_dest,
                    )
            fanout_ids = _config.get_overview_detection_fanout_chat_ids()
            if (
                fanout_ids
                and _config.is_overview_post_destination_detection(lark_overview_dest, src_inc, target_chat)
            ):
                for oc_extra in fanout_ids:
                    if oc_extra == lark_overview_dest:
                        continue
                    st_f, body_f, mid_f = _lark.post_card_to_chat(oc_extra, tenant_token, card)
                    ok_f, code_f, msg_f = _lark.lark_im_message_create_ok(body_f)
                    if st_f == 200 and ok_f:
                        mid_f_s = (mid_f or "").strip()
                        log.info(
                            "send_preview: detection fan-out overview message_id=%s dest=%s",
                            mid_f_s or "(none)",
                            oc_extra,
                        )
                        if mid_f_s:
                            _finalize_group_overview_for_edit(
                                tenant_token,
                                group_chat_id=oc_extra,
                                group_message_id=mid_f_s,
                                md=md,
                                priority=pri,
                                source_chat_label=lab,
                                target_chat=target_chat,
                                source_incident_chat_id=src_inc,
                                combined_text=combined_text,
                                mention_names=mention_names,
                                issue=issue,
                                impact=impact,
                                support=support,
                                start_epoch=start_epoch,
                                sent_by_open_id=sender_open_id,
                            )
                    else:
                        log.warning(
                            "send_preview: detection fan-out failed HTTP=%s lark_code=%s lark_msg=%r dest=%s",
                            st_f,
                            code_f,
                            msg_f,
                            oc_extra,
                        )
            adj_dm_note = ""
            if not post_mid:
                log.info("send_preview: adjustment bitable skipped (overview post_mid empty)")
            if post_mid:
                try:
                    _, adj_dm_note = _bitable_adj.maybe_post_adjustment_notice_after_overview(
                        tenant_token,
                        group_chat_id=lark_overview_dest,
                        overview_message_id=post_mid,
                        sender_open_id=sender_open_id,
                        source_chat_id=src_inc,
                    )
                except Exception as e:
                    log.warning("send_preview: adjustment bitable notice failed: %s", e)
            prev_src = str(preview.get("source_incident_chat_id") or "").strip()
            fwd_warn = ""
            if use_forwarder and broadcast_dest and not forwarder_ok:
                fwd_warn = "⚠️ Broadcast room post via overview bot failed (see server log)."
            if edit_mid:
                st_e, body_e = _lark.recall_im_message(tenant_token, edit_mid)
                if st_e != 200:
                    log.warning(
                        "send_preview: edit card recall failed HTTP=%s open_id=%s body=%s",
                        st_e,
                        sender_open_id,
                        (body_e or "")[:400],
                    )
            _drafts.save_preview(
                sender_open_id=sender_open_id,
                target_chat=target_chat,
                start_epoch=start_epoch,
                combined_text=combined_text,
                mention_names=mention_names,
                issue=issue,
                impact=impact,
                support=support,
                priority=pri,
                source_incident_chat_id=src_inc,
                group_chat_id=lark_overview_dest if post_mid else "",
                group_message_id=post_mid or "",
                group_edit_only=bool(post_mid),
            )
            dm_extra = (f"\n{fwd_warn}" if fwd_warn else "") + (f"\n{adj_dm_note}" if adj_dm_note else "")
            if post_mid:
                final_note = fwd_warn or ""
                _post_dm_overview_sent_card(
                    sender_open_id,
                    tenant_token,
                    pri=pri,
                    lab=lab,
                    lark_overview_dest=lark_overview_dest,
                    post_mid=post_mid,
                    target_chat=target_chat,
                    src_inc=src_inc,
                    forwarder_warning=final_note,
                )
                if adj_dm_note:
                    _lark.post_text_to_open_id(sender_open_id, tenant_token, adj_dm_note)
            elif dm_extra.strip():
                _lark.post_text_to_open_id(
                    sender_open_id,
                    tenant_token,
                    "⚠️ Overview post did not return a message id — check the group feed." + dm_extra,
                )
            _drafts.clear_draft(sender_open_id)
            _drafts.cancel_preview_timer(sender_open_id)
            _session.release_dm_after_overview_sent(sender_open_id, tenant_token, prev_src)
            return
        if action_name == "generate_again":
            if not combined_text:
                _lark.post_text_to_open_id(sender_open_id, tenant_token, "⚠️ Cannot generate. Preview source is empty.")
                return
            new_issue = _issues.regenerate_issue_only(issue, combined_text)
            _drafts.save_preview(
                sender_open_id=sender_open_id,
                target_chat=target_chat,
                start_epoch=start_epoch,
                combined_text=combined_text,
                mention_names=mention_names,
                issue=new_issue,
                impact=impact,
                support=support,
                awaiting_edit_input=False,
                priority=pri,
                source_incident_chat_id=str(preview.get("source_incident_chat_id") or "").strip(),
            )
            new_preview = _drafts.get_preview(sender_open_id) or {}
            lab = _session.get_source_chat_label_for_target_chat(target_chat)
            se = int(new_preview.get("start_epoch") or 0)
            card = _cards.build_preview_card(
                str(new_preview.get("md") or ""),
                priority=_preview_priority(new_preview),
                source_chat_label=lab,
                update_multi=True,
                target_chat=target_chat,
                source_incident_chat_id=str(preview.get("source_incident_chat_id") or "").strip(),
            )
            if str(new_preview.get("edit_message_id") or "").strip():
                lab2 = _session.get_source_chat_label_for_target_chat(target_chat)
                edit_c = _cards.build_edit_overview_card(
                    new_issue, impact, support, priority=pri, source_chat_label=lab2, start_epoch=se
                )
                ok_p, ok_e = _parallel_post_preview_and_edit(sender_open_id, tenant_token, card, edit_c)
                if not ok_p:
                    _lark.post_text_to_open_id(sender_open_id, tenant_token, "❌ Failed to update preview card.")
                    return
                if not ok_e:
                    log.warning("generate_again: failed to refresh edit card open_id=%s", sender_open_id)
            else:
                if not _drafts.post_or_patch_preview_card(sender_open_id, tenant_token, card):
                    _lark.post_text_to_open_id(sender_open_id, tenant_token, "❌ Failed to update preview card.")
                    return
            return
        if action_name == "edit_preview":
            _drafts.set_preview_edit_waiting(sender_open_id, True)
            lab = _session.get_source_chat_label_for_target_chat(target_chat)
            edit_card = _cards.build_edit_overview_card(
                issue, impact, support, priority=pri, source_chat_label=lab, start_epoch=start_epoch
            )
            if not _drafts.post_or_patch_edit_card(sender_open_id, tenant_token, edit_card):
                log.error("edit_preview failed open_id=%s", sender_open_id)
                _lark.post_text_to_open_id(sender_open_id, tenant_token, "⚠️ Failed to open the edit card.")
            return
        if action_name == "dismiss_sent_overview":
            prev = _drafts.get_preview(sender_open_id) or {}
            _recall_send_block_warning_dm(sender_open_id, tenant_token)
            _recall_dm_overview_edit_card(sender_open_id, tenant_token)
            sent_mid = str(prev.get("preview_message_id") or "").strip()
            if sent_mid:
                _lark.recall_im_message(tenant_token, sent_mid)
            _drafts.clear_preview(sender_open_id)
            _drafts.clear_preview_edit_flags(sender_open_id)
            if _config.get_dm_repost_instruction_after_reset():
                lab_d, pr_d = _dm_card_meta(sender_open_id)
                _send_instruction_card(
                    sender_open_id, tenant_token, None, priority=pr_d, source_chat_label=lab_d
                )
            return
        if action_name == "back_group_edit":
            prev = _drafts.get_preview(sender_open_id) or {}
            if not prev.get("group_edit_only"):
                _lark.post_text_to_open_id(sender_open_id, tenant_token, "ℹ️ No group overview edit in progress.")
                return
            _recall_dm_overview_edit_card(sender_open_id, tenant_token)
            return
        if action_name == "save_edit":
            manual_issue = _extract_issue_input_value(payload)
            manual_impact = _extract_impact_input_value(payload)
            manual_support = _extract_support_input_value(payload)
            raw_dt = _extract_incident_start_datetime_value(payload)
            parsed_epoch = _cards.parse_lark_datetime_picker_value(raw_dt) if raw_dt else 0
            new_start_epoch = parsed_epoch if parsed_epoch > 0 else start_epoch
            # Blank / placeholder = do not overwrite generated preview values
            new_issue = (
                issue
                if _form_field_left_blank(manual_issue)
                else _text.normalize_issue_manual(manual_issue)
            )
            new_impact = (
                impact
                if _form_field_left_blank(manual_impact)
                else _text.normalize_impact_scope_manual(manual_impact)
            )
            new_support = (
                support
                if _form_field_left_blank(manual_support)
                else _support.normalize_support_request_text(manual_support)
            )
            group_cid = str(preview.get("group_chat_id") or "").strip()
            group_mid = str(preview.get("group_message_id") or "").strip()
            group_edit_only = bool(preview.get("group_edit_only"))
            _drafts.save_preview(
                sender_open_id=sender_open_id,
                target_chat=target_chat,
                start_epoch=new_start_epoch,
                combined_text=combined_text,
                mention_names=mention_names,
                issue=new_issue,
                impact=new_impact,
                support=new_support,
                awaiting_edit_input=False,
                priority=pri,
                source_incident_chat_id=str(preview.get("source_incident_chat_id") or "").strip(),
                group_chat_id=group_cid,
                group_message_id=group_mid,
                group_edit_only=group_edit_only,
            )
            new_preview = _drafts.get_preview(sender_open_id) or {}
            new_md = str(new_preview.get("md") or "").strip()
            if group_edit_only and group_cid.startswith("oc_") and group_mid and new_md:
                lab_g = str(new_preview.get("source_chat_label") or "").strip()
                if not lab_g:
                    lab_g = _session.get_source_chat_label_for_target_chat(target_chat)
                src_g = str(new_preview.get("source_incident_chat_id") or "").strip()
                gcard = _cards.build_overview_result_card(
                    new_md,
                    priority=pri,
                    source_chat_label=lab_g,
                    target_chat=target_chat,
                    source_incident_chat_id=src_g,
                    allow_group_edit=False,
                )
                primary_ok, broadcast_ok = _patch_group_overview_cards(
                    tenant_token, group_cid, group_mid, gcard
                )
                if not primary_ok:
                    _lark.post_text_to_open_id(
                        sender_open_id,
                        tenant_token,
                        "❌ Failed to update the group overview (see server log).",
                    )
                    return
                if not broadcast_ok:
                    log.warning(
                        "save_edit group: overview-bot PATCH failed chat_id=%s primary_mid=%s",
                        group_cid,
                        group_mid[:20],
                    )
                _group_overview_store.update_group_overview_md(
                    group_cid,
                    group_mid,
                    md=new_md,
                    issue=new_issue,
                    impact=new_impact,
                    support=new_support,
                    start_epoch=new_start_epoch,
                )
                _recall_dm_overview_edit_card(sender_open_id, tenant_token)
                bc_warn = ""
                row_bc = _group_overview_store.get_group_overview(group_cid, group_mid) or {}
                if str(row_bc.get("broadcast_message_id") or "").strip() and not broadcast_ok:
                    bc_warn = "Overview bot copy could not be updated (restart lark-forwarder?)."
                sent_card = _cards.build_dm_overview_sent_card(
                    pri,
                    source_chat_label=lab_g,
                    group_chat_id=group_cid,
                    group_message_id=group_mid,
                    target_chat=target_chat,
                    source_incident_chat_id=src_g,
                    group_updated=True,
                    forwarder_warning=bc_warn,
                )
                _drafts.post_or_patch_preview_card(sender_open_id, tenant_token, sent_card)
                return
            lab = _session.get_source_chat_label_for_target_chat(target_chat)
            se2 = int(new_preview.get("start_epoch") or 0)
            card = _cards.build_preview_card(
                str(new_preview.get("md") or ""),
                priority=_preview_priority(new_preview),
                source_chat_label=lab,
                update_multi=True,
                target_chat=target_chat,
                source_incident_chat_id=str(preview.get("source_incident_chat_id") or "").strip(),
            )
            edit_refresh = _cards.build_edit_overview_card(
                new_issue,
                new_impact,
                new_support,
                priority=pri,
                source_chat_label=lab,
                start_epoch=se2,
            )
            if str(new_preview.get("edit_message_id") or "").strip():
                ok_p, ok_e = _parallel_post_preview_and_edit(sender_open_id, tenant_token, card, edit_refresh)
                if not ok_p:
                    _lark.post_text_to_open_id(sender_open_id, tenant_token, "❌ Failed to refresh preview card.")
                    return
                if not ok_e:
                    log.warning("save_edit: failed to refresh edit card open_id=%s", sender_open_id)
            else:
                if not _drafts.post_or_patch_preview_card(sender_open_id, tenant_token, card):
                    _lark.post_text_to_open_id(sender_open_id, tenant_token, "❌ Failed to refresh preview card.")
                    return
            if not _missing_overview_fields_for_send(
                new_issue, new_impact, new_support, combined_text
            ):
                _recall_send_block_warning_dm(sender_open_id, tenant_token)
            return
        if action_name == "back_to_preview":
            edit_mid = _drafts.take_edit_message_id(sender_open_id)
            if edit_mid:
                st_b, body_b = _lark.recall_im_message(tenant_token, edit_mid)
                if st_b != 200:
                    log.warning(
                        "edit card recall on Back failed HTTP=%s open_id=%s body=%s",
                        st_b,
                        sender_open_id,
                        (body_b or "")[:300],
                    )
            _drafts.clear_preview_edit_flags(sender_open_id)
            lab = _session.get_source_chat_label_for_target_chat(target_chat)
            card = _cards.build_preview_card(
                md,
                priority=pri,
                source_chat_label=lab,
                update_multi=True,
                target_chat=target_chat,
                source_incident_chat_id=str(preview.get("source_incident_chat_id") or "").strip(),
            )
            if not _drafts.post_or_patch_preview_card(sender_open_id, tenant_token, card):
                _lark.post_text_to_open_id(sender_open_id, tenant_token, "❌ Failed to restore preview card.")
            return
        if action_name == "cancel_preview":
            prev = _drafts.get_preview(sender_open_id) or {}
            preview_mid = str(prev.get("preview_message_id") or "").strip()
            edit_mid = str(prev.get("edit_message_id") or "").strip()
            hint_mid = str(prev.get("issue_watch_declare_hint_message_id") or "").strip()
            manual_mid = str(prev.get("issue_watch_declare_manual_message_id") or "").strip()
            lab, pr = _dm_card_meta(sender_open_id)
            src_inc = str(prev.get("source_incident_chat_id") or "").strip()
            standalone_cancel = src_inc == _session.STANDALONE_DM_SOURCE_CHAT_ID
            if edit_mid:
                st_e, body_e = _lark.recall_im_message(tenant_token, edit_mid)
                if st_e != 200:
                    log.warning(
                        "edit card recall on cancel failed HTTP=%s open_id=%s body=%s",
                        st_e,
                        sender_open_id,
                        (body_e or "")[:300],
                    )
            if preview_mid:
                st, body = _lark.recall_im_message(tenant_token, preview_mid)
                if st != 200:
                    try:
                        j = json.loads(body or "{}")
                        log.warning(
                            "preview recall failed HTTP=%s open_id=%s code=%s msg=%s",
                            st,
                            sender_open_id,
                            j.get("code"),
                            j.get("msg"),
                        )
                    except Exception:
                        log.warning(
                            "preview recall failed HTTP=%s open_id=%s body=%s",
                            st,
                            sender_open_id,
                            (body or "")[:400],
                        )
            for extra_mid in (hint_mid, manual_mid):
                if not extra_mid:
                    continue
                st_x, body_x = _lark.recall_im_message(tenant_token, extra_mid)
                if st_x != 200:
                    log.warning(
                        "issue watch declare DM recall on cancel failed HTTP=%s open_id=%s body=%s",
                        st_x,
                        sender_open_id,
                        (body_x or "")[:300],
                    )
            _drafts.clear_preview(sender_open_id)
            _drafts.clear_draft(sender_open_id)
            _drafts.cancel_preview_timer(sender_open_id)
            if standalone_cancel:
                _session.release_standalone_overview_cancel(sender_open_id, tenant_token)
                _lark.post_text_to_open_id(
                    sender_open_id,
                    tenant_token,
                    "🗑️ Preview cancelled trigger again create overview.",
                )
            else:
                # Recall removes the old preview from the thread; repost instruction for live P0/P1 flows.
                _send_instruction_card(
                    sender_open_id, tenant_token, "🗑️ Preview cancelled.", priority=pr, source_chat_label=lab
                )
            return
        log.warning("Unknown card action: %s", action_name)
    except Exception as e:
        log.error("handle_lark_card_action error: %s", e, exc_info=True)
    finally:
        perf_log(f"card_action action={action_name}", t0)


def handle_p0_submit(*args: Any, **kwargs: Any) -> None:
    return
