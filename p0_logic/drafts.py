"""
P0 drafts and previews: collect text/images, build overview preview, send to group.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from . import cards as _cards
from . import config as _config
from . import draft_store as _store
from . import groq_client as _groq
from . import issues as _issues
from . import lark_client as _lark
from . import session as _session
from . import support as _support
from . import text_processing as _text
from .perf_log import perf_log

log = logging.getLogger("lark-ops-ai")

_PREVIEW_TIMERS: Dict[str, threading.Timer] = {}
_PREVIEW_TIMERS_LOCK = threading.Lock()

AUTO_PREVIEW_DELAY_SEC = _config.AUTO_PREVIEW_DELAY_SEC


def _ensure_draft(sender_open_id: str, target_chat: str) -> Dict[str, Any]:
    now = int(time.time())
    with _store.draft_transaction(sender_open_id) as tx:
        draft = tx.get()
        if not draft or draft.get("target_chat") != target_chat:
            draft = {
                "target_chat": target_chat,
                "source_incident_chat_id": "",
                "draft_priority": "",
                "texts": [],
                "images": [],
                "mention_names": [],
                "updated_at": now,
            }
        else:
            draft["updated_at"] = now
        tx.set(draft)
        return dict(draft)


def merge_dm_scope_from_card(
    sender_open_id: str,
    target_chat: str,
    source_incident_chat_id: str = "",
    draft_priority: str = "",
) -> None:
    """
    Restore ``target_chat`` on this worker when card buttons carry ``oc_...`` in ``value``
    (multi-replica: P0/draft lived on another instance).
    """
    sender_open_id = (sender_open_id or "").strip()
    target_chat = (target_chat or "").strip()
    if not sender_open_id or not target_chat.startswith("oc_"):
        return
    prio = (draft_priority or "").strip().upper()
    if prio not in ("P0", "P1"):
        prio = ""
    src = (source_incident_chat_id or "").strip()
    now = int(time.time())
    with _store.draft_transaction(sender_open_id) as tx:
        d = tx.get()
        if not d:
            tx.set(
                {
                    "target_chat": target_chat,
                    "source_incident_chat_id": src,
                    "draft_priority": prio,
                    "texts": [],
                    "images": [],
                    "mention_names": [],
                    "updated_at": now,
                }
            )
            return
        d["target_chat"] = target_chat
        if src:
            d["source_incident_chat_id"] = src
        if prio:
            d["draft_priority"] = prio
        d["updated_at"] = now
        tx.set(d)


def seed_draft_for_incident(
    sender_open_id: str,
    target_chat: str,
    source_incident_chat_id: str,
    draft_priority: str = "",
) -> None:
    """Reset DM draft to an empty shell for one incident / queue slot (see session.enqueue_dm_instruction_if_needed)."""
    sender_open_id = (sender_open_id or "").strip()
    target_chat = (target_chat or "").strip()
    src = (source_incident_chat_id or "").strip()
    prio = (draft_priority or "").strip().upper()
    if prio not in ("P0", "P1"):
        prio = ""
    now = int(time.time())
    with _store.draft_transaction(sender_open_id) as tx:
        tx.set(
            {
                "target_chat": target_chat,
                "source_incident_chat_id": src,
                "draft_priority": prio,
                "texts": [],
                "images": [],
                "mention_names": [],
                "updated_at": now,
            }
        )


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
    _ensure_draft(sender_open_id, target_chat)
    with _store.draft_transaction(sender_open_id) as tx:
        draft = tx.get() or {}
        if cleaned:
            draft.setdefault("texts", []).append(cleaned)
        draft["mention_names"] = _append_unique_strs(draft.get("mention_names", []), mention_names or [])
        draft["updated_at"] = int(time.time())
        tx.set(draft)
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
    with _store.draft_transaction(sender_open_id) as tx:
        draft = tx.get() or {}
        draft.setdefault("images", []).append({"image_key": image_key, "message_id": message_id, "ocr_text": ocr_text.strip()})
        draft["mention_names"] = _append_unique_strs(draft.get("mention_names", []), mention_names or [])
        draft["updated_at"] = int(time.time())
        tx.set(draft)
        return dict(draft), ocr_text.strip()


def clear_draft(sender_open_id: str) -> None:
    with _store.draft_transaction(sender_open_id) as tx:
        tx.delete()


def get_draft(sender_open_id: str) -> Optional[Dict[str, Any]]:
    sender_open_id = (sender_open_id or "").strip()
    if not sender_open_id:
        return None
    with _store.draft_transaction(sender_open_id) as tx:
        d = tx.get()
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
    source_incident_chat_id: str = "",
    zh_issue_precomputed: Optional[str] = None,
    zh_impact_precomputed: Optional[str] = None,
) -> str:
    prio = (priority or "P0").strip().upper()
    if prio not in ("P0", "P1"):
        prio = "P0"
    md = _cards.build_bilingual_overview_md(
        start_epoch,
        issue,
        impact,
        support,
        priority=prio,
        zh_issue_precomputed=zh_issue_precomputed,
        zh_impact_precomputed=zh_impact_precomputed,
    )
    with _store.preview_transaction(sender_open_id) as tx:
        old_mid = ""
        old_edit_mid = ""
        old = tx.get()
        if old:
            old_mid = str(old.get("preview_message_id") or "").strip()
            old_edit_mid = str(old.get("edit_message_id") or "").strip()
        row: Dict[str, Any] = {
            "target_chat": target_chat,
            "start_epoch": start_epoch,
            "combined_text": combined_text,
            "mention_names": mention_names,
            "issue": issue,
            "impact": impact,
            "support": support,
            "priority": prio,
            "md": md,
            "awaiting_edit_input": awaiting_edit_input,
            "updated_at": int(time.time()),
            "source_incident_chat_id": (source_incident_chat_id or "").strip(),
        }
        if old_mid:
            row["preview_message_id"] = old_mid
        if old_edit_mid:
            row["edit_message_id"] = old_edit_mid
        tx.set(row)
    return str(md or "").strip()


def get_preview(sender_open_id: str) -> Optional[Dict[str, Any]]:
    sender_open_id = (sender_open_id or "").strip()
    if not sender_open_id:
        return None
    with _store.preview_transaction(sender_open_id) as tx:
        p = tx.get()
        return dict(p) if p else None


def clear_preview(sender_open_id: str) -> None:
    with _store.preview_transaction(sender_open_id) as tx:
        tx.delete()


def set_preview_edit_waiting(sender_open_id: str, waiting: bool) -> None:
    with _store.preview_transaction(sender_open_id) as tx:
        p = tx.get()
        if not p:
            return
        p["awaiting_edit_input"] = bool(waiting)
        p["updated_at"] = int(time.time())
        tx.set(p)


def clear_preview_edit_flags(sender_open_id: str) -> None:
    with _store.preview_transaction(sender_open_id) as tx:
        p = tx.get()
        if not p:
            return
        p["awaiting_edit_input"] = False
        p["updated_at"] = int(time.time())
        tx.set(p)


def take_edit_message_id(sender_open_id: str) -> str:
    """Remove and return the DM edit-card ``message_id`` (before recalling that message)."""
    sender_open_id = (sender_open_id or "").strip()
    with _store.preview_transaction(sender_open_id) as tx:
        p = tx.get()
        if not p:
            return ""
        mid = str(p.pop("edit_message_id", None) or "").strip()
        if mid:
            p["updated_at"] = int(time.time())
        tx.set(p)
        return mid


def _draft_priority_for_preview(draft: Dict[str, Any], target_chat: str) -> str:
    dp = str((draft or {}).get("draft_priority") or "").strip().upper()
    if dp in ("P0", "P1"):
        return dp
    src_inc = str((draft or {}).get("source_incident_chat_id") or "").strip()
    if src_inc and src_inc == _session.STANDALONE_DM_SOURCE_CHAT_ID:
        return "P0"
    if src_inc:
        sess = _session.P0_SESSIONS.get(src_inc)
        if sess:
            pr = str(sess.get("priority") or "P0").strip().upper()
            if pr in ("P0", "P1"):
                return pr
    _chat_id, sess = _session.find_session_by_target_chat(target_chat)
    pr = str((sess or {}).get("priority") or "P0").strip().upper()
    return pr if pr in ("P0", "P1") else "P0"


def _build_preview_from_draft(
    sender_open_id: str, tenant_token: str, target_chat: str, start_epoch: int, draft: Dict[str, Any]
) -> str:
    prio = _draft_priority_for_preview(draft, target_chat)
    src_inc = str(draft.get("source_incident_chat_id") or "").strip()
    combined_text, combined_mentions = _compose_combined_source_text(draft)
    if not combined_text.strip():
        return ""
    text_entries = [str(t or "").strip() for t in (draft.get("texts") or []) if str(t or "").strip()]
    image_entries = [str((img or {}).get("ocr_text") or "").strip() for img in (draft.get("images") or []) if str((img or {}).get("ocr_text") or "").strip()]
    text_only = "\n\n".join(text_entries).strip()
    ocr_only = "\n\n".join(image_entries).strip()
    issue_source = "\n\n".join([x for x in [text_only, ocr_only] if x]).strip() or combined_text
    support_source = "\n\n".join([x for x in [text_only, ocr_only] if x]).strip() or combined_text
    impact = _text.build_impact_scope(issue_source)
    zh_issue_pc: Optional[str] = None
    zh_impact_pc: Optional[str] = None
    triplet = None
    # Support map (Lark Sheets) and Groq one-shot are independent — run in parallel to cut wall time.
    def _support_only() -> str:
        return _support.build_support_request(support_source, tenant_token, mention_names=combined_mentions)

    def _groq_triplet_only() -> Optional[Tuple[str, str, str]]:
        if _groq.GROQ_API_KEY and _config.GROQ_OVERVIEW_ONE_SHOT:
            return _groq.groq_overview_issue_and_zh_bilingual(issue_source, impact)
        return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_sup = pool.submit(_support_only)
        f_groq = pool.submit(_groq_triplet_only)
        support = f_sup.result()
        triplet = f_groq.result()
    if triplet:
        issue_en_raw, zh_issue_pc, zh_impact_pc = triplet
        issue = (issue_en_raw or "").strip()
        issue = re.sub(r"\b\d{6,}\b", "", issue)
        issue = re.sub(r"\b\d{4}[/-]\d{1,2}[/-]\d{1,2}\b", "", issue)
        issue = re.sub(r"\s+", " ", issue).strip(" ,.")
        issue = _issues._truncate_issue_output(issue, _issues.ISSUE_SUMMARY_MAX_CHARS) if issue else "Not specified"
    else:
        issue = _issues.summarize_issue(issue_source)
    return _save_preview(
        sender_open_id=sender_open_id,
        target_chat=target_chat,
        start_epoch=start_epoch,
        combined_text=combined_text,
        mention_names=combined_mentions,
        issue=issue,
        impact=impact,
        support=support,
        priority=prio,
        source_incident_chat_id=src_inc,
        zh_issue_precomputed=zh_issue_pc if triplet else None,
        zh_impact_precomputed=zh_impact_pc if triplet else None,
    )


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
            src_inc = str(draft.get("source_incident_chat_id") or "").strip()
            if not _session.dm_preview_allowed_for_incident(src_inc, target_chat):
                return
            _chat_id, sess = _session.find_session_by_target_chat(target_chat)
            if sess:
                start_epoch = int(sess.get("start_epoch") or time.time())
            else:
                start_epoch = int(time.time())
            md = _build_preview_from_draft(sender_open_id=sender_open_id, tenant_token=tenant_token, target_chat=target_chat, start_epoch=start_epoch, draft=draft)
            if not md:
                return
            pr = _draft_priority_for_preview(draft, target_chat)
            lab = _session.get_source_chat_label_for_target_chat(target_chat)
            card = _cards.build_preview_card(
                md,
                priority=pr,
                source_chat_label=lab,
                update_multi=True,
                target_chat=target_chat,
                source_incident_chat_id=str(draft.get("source_incident_chat_id") or "").strip(),
            )
            post_or_patch_preview_card(sender_open_id, tenant_token, card)
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
    source_incident_chat_id: str = "",
    zh_issue_precomputed: Optional[str] = None,
    zh_impact_precomputed: Optional[str] = None,
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
        source_incident_chat_id=source_incident_chat_id,
        zh_issue_precomputed=zh_issue_precomputed,
        zh_impact_precomputed=zh_impact_precomputed,
    )


def post_or_patch_preview_card(sender_open_id: str, tenant_token: str, card: Dict[str, Any]) -> bool:
    """
    Post Overview Preview to the user's DM, or PATCH the previous preview message (same as meeting card).
    Falls back to a new POST if PATCH fails (e.g. old card without update_multi).
    """
    t0 = time.perf_counter()
    try:
        sender_open_id = (sender_open_id or "").strip()
        if not sender_open_id or not tenant_token:
            return False
        mid = ""
        with _store.preview_transaction(sender_open_id) as tx:
            p = tx.get()
            if p:
                mid = str(p.get("preview_message_id") or "").strip()
        if mid:
            st, body = _lark.patch_interactive_card(tenant_token, mid, card)
            if st == 200:
                return True
            log.warning(
                "preview PATCH failed HTTP=%s open_id=%s — sending new card body=%s",
                st,
                sender_open_id,
                (body or "")[:400],
            )
        st, body, new_mid = _lark.post_card_to_open_id(sender_open_id, tenant_token, card)
        if st != 200:
            log.error("preview POST failed HTTP=%s body=%s", st, (body or "")[:500])
            return False
        if new_mid:
            with _store.preview_transaction(sender_open_id) as tx:
                pp = tx.get()
                if pp:
                    pp["preview_message_id"] = new_mid
                    tx.set(pp)
        return True
    finally:
        perf_log("drafts post_or_patch_preview_card total", t0)


def post_or_patch_edit_card(sender_open_id: str, tenant_token: str, card: Dict[str, Any]) -> bool:
    """
    Post the edit-overview form once, then PATCH the same message so repeated Edit / Save
    does not stack multiple edit cards in the DM.
    """
    t0 = time.perf_counter()
    try:
        sender_open_id = (sender_open_id or "").strip()
        if not sender_open_id or not tenant_token:
            return False
        mid = ""
        with _store.preview_transaction(sender_open_id) as tx:
            p = tx.get()
            if p:
                mid = str(p.get("edit_message_id") or "").strip()
        if mid:
            st, body = _lark.patch_interactive_card(tenant_token, mid, card)
            if st == 200:
                return True
            log.warning(
                "edit card PATCH failed HTTP=%s open_id=%s — posting new card body=%s",
                st,
                sender_open_id,
                (body or "")[:400],
            )
        st, body, new_mid = _lark.post_card_to_open_id(sender_open_id, tenant_token, card)
        if st != 200:
            log.error("edit card POST failed HTTP=%s body=%s", st, (body or "")[:500])
            return False
        if new_mid:
            with _store.preview_transaction(sender_open_id) as tx:
                pp = tx.get()
                if pp:
                    pp["edit_message_id"] = new_mid
                    tx.set(pp)
        return True
    finally:
        perf_log("drafts post_or_patch_edit_card total", t0)
