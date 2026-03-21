"""
P0 drafts and previews: collect text/images, build overview preview, send to group.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from . import cards as _cards
from . import config as _config
from . import groq_client as _groq
from . import issues as _issues
from . import lark_client as _lark
from . import session as _session
from . import support as _support
from . import text_processing as _text

log = logging.getLogger("lark-ops-ai")

P0_DRAFTS: Dict[str, Dict[str, Any]] = {}
P0_PREVIEWS: Dict[str, Dict[str, Any]] = {}
_P0_DRAFTS_LOCK = threading.Lock()
_P0_PREVIEWS_LOCK = threading.Lock()
_PREVIEW_TIMERS: Dict[str, threading.Timer] = {}
_PREVIEW_TIMERS_LOCK = threading.Lock()

AUTO_PREVIEW_DELAY_SEC = _config.AUTO_PREVIEW_DELAY_SEC


def _ensure_draft(sender_open_id: str, target_chat: str) -> Dict[str, Any]:
    now = int(time.time())
    with _P0_DRAFTS_LOCK:
        draft = P0_DRAFTS.get(sender_open_id)
        if not draft or draft.get("target_chat") != target_chat:
            draft = {"target_chat": target_chat, "texts": [], "images": [], "mention_names": [], "updated_at": now}
            P0_DRAFTS[sender_open_id] = draft
        draft["updated_at"] = now
        return draft


def _append_unique_strs(base: List[str], items: List[str]) -> List[str]:
    seen = set(x.strip() for x in base if x and x.strip())
    for item in items:
        item = (item or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        base.append(item)
    return base


def add_text_to_draft(
    sender_open_id: str, target_chat: str, text: str, mention_names: Optional[List[str]] = None
) -> Dict[str, Any]:
    cleaned = _text.clean_pasted_text(text)
    draft = _ensure_draft(sender_open_id, target_chat)
    with _P0_DRAFTS_LOCK:
        if cleaned:
            draft["texts"].append(cleaned)
        draft["mention_names"] = _append_unique_strs(draft.get("mention_names", []), mention_names or [])
        draft["updated_at"] = int(time.time())
        return dict(draft)


def _add_image_to_draft(
    sender_open_id: str,
    target_chat: str,
    tenant_token: str,
    image_key: str,
    message_id: str,
    mention_names: Optional[List[str]] = None,
) -> Tuple[Dict[str, Any], str]:
    draft = _ensure_draft(sender_open_id, target_chat)
    img = b""
    err1 = None
    err2 = None
    try:
        img = _lark.download_image_bytes(tenant_token, image_key)
    except Exception as e:
        err1 = e
        log.warning("im/v1/images failed (maybe user screenshot). err=%s", e)
    if (not img) and message_id:
        try:
            img = _lark.download_message_resource_bytes(tenant_token, message_id, image_key)
        except Exception as e:
            err2 = e
    if not img:
        raise RuntimeError(str(err2 or err1 or "download failed"))
    ocr_text = _groq.groq_vision_ocr(img)
    if not ocr_text.strip():
        raise RuntimeError("OCR returned empty")
    with _P0_DRAFTS_LOCK:
        draft["images"].append({"image_key": image_key, "message_id": message_id, "ocr_text": ocr_text.strip()})
        draft["mention_names"] = _append_unique_strs(draft.get("mention_names", []), mention_names or [])
        draft["updated_at"] = int(time.time())
        return dict(draft), ocr_text.strip()


def clear_draft(sender_open_id: str) -> None:
    with _P0_DRAFTS_LOCK:
        P0_DRAFTS.pop(sender_open_id, None)


def get_draft(sender_open_id: str) -> Optional[Dict[str, Any]]:
    with _P0_DRAFTS_LOCK:
        d = P0_DRAFTS.get(sender_open_id)
        return dict(d) if d else None


def draft_summary_text(draft: Dict[str, Any]) -> str:
    texts = draft.get("texts") or []
    images = draft.get("images") or []
    mentions = draft.get("mention_names") or []
    return f"📝 Draft status\n- Text entries: {len(texts)}\n- Screenshots: {len(images)}\n- Mention names: {len(mentions)}"


def _compose_combined_source_text(draft: Dict[str, Any]) -> Tuple[str, List[str]]:
    texts = draft.get("texts") or []
    images = draft.get("images") or []
    mentions = draft.get("mention_names") or []
    blocks: List[str] = []
    for t in texts:
        t = (t or "").strip()
        if t:
            blocks.append(t)
    for idx, img in enumerate(images, start=1):
        ocr_text = str((img or {}).get("ocr_text") or "").strip()
        if ocr_text:
            blocks.append(f"[Screenshot {idx} OCR]\n{ocr_text}")
    combined = "\n\n".join(blocks).strip()
    return combined, mentions


def _save_preview(
    sender_open_id: str,
    target_chat: str,
    start_epoch: int,
    combined_text: str,
    mention_names: List[str],
    issue: str,
    impact: str,
    support: str,
    awaiting_edit_input: bool = False,
    priority: str = "P0",
) -> None:
    prio = (priority or "P0").strip().upper()
    if prio not in ("P0", "P1"):
        prio = "P0"
    with _P0_PREVIEWS_LOCK:
        P0_PREVIEWS[sender_open_id] = {
            "target_chat": target_chat,
            "start_epoch": start_epoch,
            "combined_text": combined_text,
            "mention_names": mention_names,
            "issue": issue,
            "impact": impact,
            "support": support,
            "priority": prio,
            "md": _cards.build_bilingual_overview_md(start_epoch, issue, impact, support, priority=prio),
            "awaiting_edit_input": awaiting_edit_input,
            "updated_at": int(time.time()),
        }


def get_preview(sender_open_id: str) -> Optional[Dict[str, Any]]:
    with _P0_PREVIEWS_LOCK:
        p = P0_PREVIEWS.get(sender_open_id)
        return dict(p) if p else None


def clear_preview(sender_open_id: str) -> None:
    with _P0_PREVIEWS_LOCK:
        P0_PREVIEWS.pop(sender_open_id, None)


def set_preview_edit_waiting(sender_open_id: str, waiting: bool) -> None:
    with _P0_PREVIEWS_LOCK:
        p = P0_PREVIEWS.get(sender_open_id)
        if not p:
            return
        p["awaiting_edit_input"] = bool(waiting)
        p["updated_at"] = int(time.time())


def clear_preview_edit_flags(sender_open_id: str) -> None:
    with _P0_PREVIEWS_LOCK:
        p = P0_PREVIEWS.get(sender_open_id)
        if not p:
            return
        p["awaiting_edit_input"] = False
        p["updated_at"] = int(time.time())


def _build_preview_from_draft(
    sender_open_id: str, tenant_token: str, target_chat: str, start_epoch: int, draft: Dict[str, Any]
) -> str:
    _chat_id, sess = _session.find_session_by_target_chat(target_chat)
    prio = str((sess or {}).get("priority") or "P0").strip().upper()
    if prio not in ("P0", "P1"):
        prio = "P0"
    combined_text, combined_mentions = _compose_combined_source_text(draft)
    if not combined_text.strip():
        return ""
    text_entries = [str(t or "").strip() for t in (draft.get("texts") or []) if str(t or "").strip()]
    image_entries = [str((img or {}).get("ocr_text") or "").strip() for img in (draft.get("images") or []) if str((img or {}).get("ocr_text") or "").strip()]
    text_only = "\n\n".join(text_entries).strip()
    ocr_only = "\n\n".join(image_entries).strip()
    issue_source = "\n\n".join([x for x in [text_only, ocr_only] if x]).strip() or combined_text
    support_source = "\n\n".join([x for x in [text_only, ocr_only] if x]).strip() or combined_text
    issue = _issues.summarize_issue(issue_source)
    impact = _text.build_impact_scope(issue_source)
    support = _support.build_support_request(support_source, tenant_token, mention_names=combined_mentions)
    _save_preview(
        sender_open_id=sender_open_id,
        target_chat=target_chat,
        start_epoch=start_epoch,
        combined_text=combined_text,
        mention_names=combined_mentions,
        issue=issue,
        impact=impact,
        support=support,
        priority=prio,
    )
    preview = get_preview(sender_open_id) or {}
    return str(preview.get("md") or "").strip()


def cancel_preview_timer(sender_open_id: str) -> None:
    with _PREVIEW_TIMERS_LOCK:
        t = _PREVIEW_TIMERS.pop(sender_open_id, None)
    if t:
        try:
            t.cancel()
        except Exception:
            pass


def schedule_auto_preview(sender_open_id: str, tenant_token: str) -> None:
    cancel_preview_timer(sender_open_id)

    def run() -> None:
        try:
            draft = get_draft(sender_open_id)
            if not draft:
                return
            target_chat = str(draft.get("target_chat") or "").strip()
            if not target_chat:
                return
            _chat_id, sess = _session.find_session_by_target_chat(target_chat)
            if not sess:
                return
            start_epoch = int(sess.get("start_epoch") or time.time())
            md = _build_preview_from_draft(sender_open_id=sender_open_id, tenant_token=tenant_token, target_chat=target_chat, start_epoch=start_epoch, draft=draft)
            if not md:
                return
            prev = get_preview(sender_open_id) or {}
            pr = str(prev.get("priority") or "P0").strip().upper()
            if pr not in ("P0", "P1"):
                pr = "P0"
            lab = _session.get_source_chat_label_for_target_chat(target_chat)
            _lark.post_card_to_open_id(
                sender_open_id, tenant_token, _cards.build_preview_card(md, priority=pr, source_chat_label=lab)
            )
        finally:
            with _PREVIEW_TIMERS_LOCK:
                _PREVIEW_TIMERS.pop(sender_open_id, None)

    timer = threading.Timer(AUTO_PREVIEW_DELAY_SEC, run)
    timer.daemon = True
    with _PREVIEW_TIMERS_LOCK:
        _PREVIEW_TIMERS[sender_open_id] = timer
    timer.start()


def add_image_to_draft(
    sender_open_id: str,
    target_chat: str,
    tenant_token: str,
    image_key: str,
    message_id: str,
    mention_names: Optional[List[str]] = None,
) -> Tuple[Dict[str, Any], str]:
    return _add_image_to_draft(sender_open_id, target_chat, tenant_token, image_key, message_id, mention_names)


def build_preview_from_draft(
    sender_open_id: str, tenant_token: str, target_chat: str, start_epoch: int, draft: Dict[str, Any]
) -> str:
    """Build preview markdown from draft and save preview state. Returns the overview md."""
    return _build_preview_from_draft(sender_open_id, tenant_token, target_chat, start_epoch, draft)


def save_preview(
    sender_open_id: str,
    target_chat: str,
    start_epoch: int,
    combined_text: str,
    mention_names: List[str],
    issue: str,
    impact: str,
    support: str,
    awaiting_edit_input: bool = False,
    priority: str = "P0",
) -> None:
    """Persist preview state for the sender."""
    _save_preview(
        sender_open_id=sender_open_id,
        target_chat=target_chat,
        start_epoch=start_epoch,
        combined_text=combined_text,
        mention_names=mention_names,
        issue=issue,
        impact=impact,
        support=support,
        awaiting_edit_input=awaiting_edit_input,
        priority=priority,
    )
