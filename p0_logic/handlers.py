"""
Event handlers: DM message handling and Lark card actions.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from . import cards as _cards
from . import config as _config
from . import drafts as _drafts
from . import lark_client as _lark
from . import participants as _participants
from . import session as _session
from . import support as _support
from . import text_processing as _text

log = logging.getLogger("lark-ops-ai")


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


def _form_field_left_blank(manual: str) -> bool:
    """True if user did not enter a new value — keep preview field as-is."""
    s = (manual or "").strip()
    return (not s) or _text.is_not_specified(s)


def _preview_priority(preview: Dict[str, Any]) -> str:
    pr = str((preview or {}).get("priority") or "P0").strip().upper()
    return pr if pr in ("P0", "P1") else "P0"


def _send_instruction_card(sender_open_id: str, tenant_token: str, note: Optional[str] = None) -> None:
    if note:
        _lark.post_text_to_open_id(sender_open_id, tenant_token, note)
    _lark.post_card_to_open_id(sender_open_id, tenant_token, _cards.build_dm_instruction_card())


def _generate_preview_now(sender_open_id: str, tenant_token: str) -> bool:
    draft = _drafts.get_draft(sender_open_id)
    if not draft:
        _lark.post_text_to_open_id(sender_open_id, tenant_token, "⚠️ No draft yet. Please paste screenshots or text first.")
        return False
    target_chat = str(draft.get("target_chat") or "").strip()
    if not target_chat:
        _lark.post_text_to_open_id(sender_open_id, tenant_token, "⚠️ No active target chat found.")
        return False
    _chat_id, sess = _session.find_session_by_target_chat(target_chat)
    if not sess:
        _lark.post_text_to_open_id(sender_open_id, tenant_token, "⚠️ No active P0 session found.")
        return False
    import time
    start_epoch = int(sess.get("start_epoch") or time.time())
    md = _drafts.build_preview_from_draft(sender_open_id=sender_open_id, tenant_token=tenant_token, target_chat=target_chat, start_epoch=start_epoch, draft=draft)
    if not md:
        _lark.post_text_to_open_id(sender_open_id, tenant_token, "⚠️ Draft is empty.")
        return False
    prev = _drafts.get_preview(sender_open_id) or {}
    pr = _preview_priority(prev)
    st, body = _lark.post_card_to_open_id(sender_open_id, tenant_token, _cards.build_preview_card(md, priority=pr))
    if st != 200:
        log.error("generate_preview_now failed HTTP=%s body=%s", st, (body or "")[:500])
        _lark.post_text_to_open_id(sender_open_id, tenant_token, "❌ Failed to send preview card.")
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
    import time

    sender_open_id = (sender_open_id or "").strip()
    mention_names = mention_names or []
    message_id = (message_id or "").strip()
    target_chat = _session.get_active_target_chat()
    if not target_chat:
        _lark.post_text_to_open_id(sender_open_id, tenant_token, "⚠️ No active P0 session. Trigger p0 in the incident group first.")
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
            line = _participants.departments_line_from_names(participants, tenant_token)
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
            _drafts.clear_draft(sender_open_id)
            _drafts.clear_preview(sender_open_id)
            _drafts.cancel_preview_timer(sender_open_id)
            _send_instruction_card(sender_open_id, tenant_token, "🗑️ Draft cleared.")
            return
        if _config.STATUS_RE.match(src_text):
            draft = _drafts.get_draft(sender_open_id)
            if not draft:
                _lark.post_text_to_open_id(sender_open_id, tenant_token, "ℹ️ No active draft yet.")
                return
            _lark.post_text_to_open_id(sender_open_id, tenant_token, _drafts.draft_summary_text(draft))
            return
        if _config.GENERATE_RE.match(src_text):
            _generate_preview_now(sender_open_id, tenant_token)
            return
        _drafts.add_text_to_draft(sender_open_id=sender_open_id, target_chat=target_chat, text=src_text, mention_names=mention_names)
        return


def handle_lark_card_action(payload: Dict[str, Any], tenant_token: str) -> None:
    from . import issues as _issues

    try:
        sender_open_id = _extract_card_action_sender_open_id(payload)
        action_name = _extract_card_action_name(payload)

        if action_name == "p1_confirm_meeting_yes":
            chat_id = _extract_card_action_open_chat_id(payload)
            if not chat_id:
                log.warning("p1_confirm_meeting_yes missing open_chat_id payload=%s", json.dumps(payload, ensure_ascii=False)[:2000])
                return
            if chat_id in _session.P0_SESSIONS:
                if sender_open_id:
                    _lark.post_text_to_open_id(sender_open_id, tenant_token, "ℹ️ A meeting session is already active.")
                return
            nonce = _extract_p1_confirm_nonce(payload)
            pending = _session.consume_p1_prompt_for_confirm(chat_id, nonce)
            if not pending:
                if sender_open_id:
                    _lark.post_text_to_open_id(
                        sender_open_id,
                        tenant_token,
                        "ℹ️ This P1 confirmation is out of date or was already answered.",
                    )
                return
            trigger = str(pending.get("trigger_open_id") or "").strip() or sender_open_id
            _session.start_p0(chat_id, tenant_token, trigger, priority="P1")
            return

        if action_name == "p1_confirm_meeting_no":
            chat_id = _extract_card_action_open_chat_id(payload)
            if not chat_id:
                return
            if chat_id in _session.P0_SESSIONS:
                _lark.post_text_to_chat(
                    chat_id,
                    tenant_token,
                    "ℹ️ A meeting is already active in this chat. Just type **cancel meeting** if you want to end it.",
                )
                return
            nonce = _extract_p1_confirm_nonce(payload)
            pending = _session.consume_p1_prompt_for_confirm(chat_id, nonce)
            if pending:
                _lark.post_text_to_chat(
                    chat_id,
                    tenant_token,
                    "ℹ️ No P1 meeting will be created. Type **p1** in this group again when you need a new meeting.",
                )
            elif sender_open_id:
                _lark.post_text_to_open_id(
                    sender_open_id,
                    tenant_token,
                    "ℹ️ This P1 confirmation is out of date or was already answered.",
                )
            return

        if action_name == "p1_declare_p0_yes":
            chat_id = _extract_card_action_open_chat_id(payload)
            if not chat_id:
                log.warning("p1_declare_p0_yes missing open_chat_id")
                return
            if not _session.apply_p1_escalation_after_confirm(chat_id, tenant_token):
                if sender_open_id:
                    _lark.post_text_to_open_id(
                        sender_open_id,
                        tenant_token,
                        "ℹ️ Could not declare P0 (session ended, not in P1, or already answered).",
                    )
            return

        if action_name == "p1_declare_p0_no":
            chat_id = _extract_card_action_open_chat_id(payload)
            if not chat_id:
                log.warning("p1_declare_p0_no missing open_chat_id")
                return
            if not _session.decline_p1_escalation_end_as_p1(chat_id, tenant_token):
                if sender_open_id:
                    _lark.post_text_to_open_id(
                        sender_open_id,
                        tenant_token,
                        "ℹ️ Could not complete action (session ended, not in P1, or already answered).",
                    )
            return

        if not sender_open_id or not action_name:
            log.warning("card.action.trigger missing sender/action payload=%s", json.dumps(payload, ensure_ascii=False)[:4000])
            return
        if action_name == "generate_preview":
            _generate_preview_now(sender_open_id, tenant_token)
            return
        if action_name == "clear_draft":
            _drafts.clear_draft(sender_open_id)
            _drafts.clear_preview(sender_open_id)
            _drafts.cancel_preview_timer(sender_open_id)
            _send_instruction_card(sender_open_id, tenant_token, "🗑️ Draft cleared.")
            return
        if action_name == "show_participants":
            participants = _participants.list_meeting_participants()
            if not participants:
                _lark.post_text_to_open_id(sender_open_id, tenant_token, "ℹ️ No participants have been tracked in the meeting yet.")
                return
            line = _participants.departments_line_from_names(participants, tenant_token)
            _lark.post_text_to_open_id(sender_open_id, tenant_token, f"Participants\n{line}")
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
        if action_name == "send_preview":
            if not target_chat or not md:
                _lark.post_text_to_open_id(sender_open_id, tenant_token, "⚠️ Preview is incomplete.")
                return
            card = _cards.build_overview_result_card(md, priority=pri)
            st, body = _lark.post_card_to_chat(target_chat, tenant_token, card)
            if st != 200:
                log.error("send_preview failed HTTP=%s body=%s", st, (body or "")[:300])
                _lark.post_text_to_open_id(sender_open_id, tenant_token, "❌ Failed to send overview to group.")
                return
            _drafts.clear_preview(sender_open_id)
            _drafts.clear_draft(sender_open_id)
            _drafts.cancel_preview_timer(sender_open_id)
            _send_instruction_card(sender_open_id, tenant_token, "✅ Overview sent to the target group chat.")
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
            )
            new_preview = _drafts.get_preview(sender_open_id) or {}
            card = _cards.build_preview_card(str(new_preview.get("md") or ""), priority=_preview_priority(new_preview))
            st, body = _lark.post_card_to_open_id(sender_open_id, tenant_token, card)
            if st != 200:
                log.error("generate_again failed HTTP=%s body=%s", st, (body or "")[:500])
                _lark.post_text_to_open_id(sender_open_id, tenant_token, "❌ Failed to update preview card.")
            return
        if action_name == "edit_preview":
            _drafts.set_preview_edit_waiting(sender_open_id, True)
            st, body = _lark.post_card_to_open_id(
                sender_open_id, tenant_token, _cards.build_edit_overview_card(issue, impact, support, priority=pri)
            )
            if st != 200:
                log.error("edit_preview failed HTTP=%s body=%s", st, (body or "")[:500])
                _lark.post_text_to_open_id(sender_open_id, tenant_token, "⚠️ Failed to open the edit card.")
            return
        if action_name == "save_edit":
            manual_issue = _extract_issue_input_value(payload)
            manual_impact = _extract_impact_input_value(payload)
            manual_support = _extract_support_input_value(payload)
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
                start_epoch=start_epoch,
                combined_text=combined_text,
                mention_names=mention_names,
                issue=new_issue,
                impact=new_impact,
                support=new_support,
                awaiting_edit_input=False,
                priority=pri,
            )
            new_preview = _drafts.get_preview(sender_open_id) or {}
            _lark.post_card_to_open_id(
                sender_open_id,
                tenant_token,
                _cards.build_preview_card(str(new_preview.get("md") or ""), priority=_preview_priority(new_preview)),
            )
            return
        if action_name == "back_to_preview":
            _drafts.clear_preview_edit_flags(sender_open_id)
            _lark.post_card_to_open_id(sender_open_id, tenant_token, _cards.build_preview_card(md, priority=pri))
            return
        if action_name == "cancel_preview":
            _drafts.clear_preview(sender_open_id)
            _drafts.clear_draft(sender_open_id)
            _drafts.cancel_preview_timer(sender_open_id)
            _send_instruction_card(sender_open_id, tenant_token, "🗑️ Preview cancelled.")
            return
        log.warning("Unknown card action: %s", action_name)
    except Exception as e:
        log.error("handle_lark_card_action error: %s", e, exc_info=True)


def handle_p0_submit(*args: Any, **kwargs: Any) -> None:
    return
