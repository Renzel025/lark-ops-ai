"""
Event handlers: DM message handling and Lark card actions.
"""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from . import cards as _cards
from . import config as _config
from . import drafts as _drafts
from . import lark_client as _lark
from . import participants as _participants
from . import session as _session
from . import support as _support
from . import text_processing as _text
from .perf_log import perf_log

log = logging.getLogger("lark-ops-ai")

# Avoid spamming the same DM when user taps Build overview repeatedly with no overview target (multi-group, etc.).
_DM_NO_OVERVIEW_TARGET_DEBOUNCE: Dict[str, float] = {}
_DM_NO_OVERVIEW_TARGET_DEBOUNCE_SEC = 120.0

# DM text after Clear draft (chat command or button) — always sent; instruction-card repost stays env-gated.
DM_DRAFT_CLEARED_PROMPT = (
    "🗑️ Draft cleared. Kindly paste screenshots or text again when you're ready."
)

_DM_OVERVIEW_MEETING_ENDED_MSG = (
    "No active meeting session for this overview — use manual create: "
    "type **create overview emergency** or **create overview game**."
)


def _ensure_dm_preview_incident_session(
    sender_open_id: str, tenant_token: str, source_incident_chat_id: str, target_chat: str
) -> bool:
    if _session.dm_preview_allowed_for_incident(source_incident_chat_id, target_chat):
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


def _extract_dm_scope_from_card_payload(payload: Dict[str, Any]) -> Tuple[str, str, str]:
    val = _deep_get(payload, "event", "action", "value")
    if val is None:
        val = _deep_get(payload, "action", "value")
    if not isinstance(val, dict):
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
    candidates = [
        _deep_get(payload, "event", "action", "value", "action"),
        _deep_get(payload, "event", "action", "value", "button_action"),
        _deep_get(payload, "action", "value", "action"),
        _deep_get(payload, "action", "value", "button_action"),
    ]
    for x in candidates:
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


def _extract_form_field(payload: Dict[str, Any], field: str) -> str:
    """Read a form field value, including empty string when the user cleared the field."""
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


def _preview_priority(preview: Dict[str, Any]) -> str:
    pr = str((preview or {}).get("priority") or "P0").strip().upper()
    return pr if pr in ("P0", "P1") else "P0"


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
        m_co = _config.STANDALONE_OVERVIEW_DM_RE.match(cmd)
        if m_co:
            blocked = _session.note_if_standalone_create_overview_blocked(sender_open_id)
            if blocked:
                _lark.post_text_to_open_id(sender_open_id, tenant_token, blocked)
                return
            tag = (m_co.group(1) or "").strip().lower()
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
    from . import issues as _issues

    action_name = (_extract_card_action_name(payload) or "").strip() or "unknown"
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
        preview = _drafts.get_preview(sender_open_id)
        if not preview:
            _lark.post_text_to_open_id(sender_open_id, tenant_token, "⚠️ No preview found.")
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
            edit_mid = str(preview.get("edit_message_id") or "").strip()
            preview_mid = str(preview.get("preview_message_id") or "").strip()
            lab = _session.get_source_chat_label_for_target_chat(target_chat)
            card = _cards.build_overview_result_card(md, priority=pri, source_chat_label=lab)
            # Post to group first: if this fails, operator still has preview + edit cards in the DM.
            st, body, _ = _lark.post_card_to_chat(target_chat, tenant_token, card)
            if st != 200:
                log.error("send_preview failed HTTP=%s body=%s", st, (body or "")[:300])
                _lark.post_text_to_open_id(sender_open_id, tenant_token, "❌ Failed to send overview to group.")
                return
            prev_src = str(preview.get("source_incident_chat_id") or "").strip()
            # Overlap DM ack + recall edit + recall preview (three independent Lark calls).
            tasks: List[Tuple[str, Any]] = []
            max_w = 1 + (1 if edit_mid else 0) + (1 if preview_mid else 0)
            with ThreadPoolExecutor(max_workers=max(1, max_w)) as ex:
                if edit_mid:
                    tasks.append(("edit", ex.submit(_lark.recall_im_message, tenant_token, edit_mid)))
                tasks.append(
                    (
                        "ok",
                        ex.submit(
                            _lark.post_text_to_open_id,
                            sender_open_id,
                            tenant_token,
                            "✅ Overview sent to the target group chat.",
                        ),
                    )
                )
                if preview_mid:
                    tasks.append(("pv", ex.submit(_lark.recall_im_message, tenant_token, preview_mid)))
                for kind, fut in tasks:
                    r = fut.result()
                    if kind == "edit":
                        st_e, body_e = r
                        if st_e != 200:
                            log.warning(
                                "send_preview: edit card recall failed HTTP=%s open_id=%s body=%s",
                                st_e,
                                sender_open_id,
                                (body_e or "")[:400],
                            )
                    elif kind == "pv":
                        st_pv, body_pv = r
                        if st_pv != 200:
                            log.warning(
                                "send_preview: preview card recall failed HTTP=%s open_id=%s body=%s",
                                st_pv,
                                sender_open_id,
                                (body_pv or "")[:400],
                            )
            _drafts.clear_preview(sender_open_id)
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
            )
            new_preview = _drafts.get_preview(sender_open_id) or {}
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
