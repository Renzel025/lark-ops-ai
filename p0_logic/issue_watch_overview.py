"""
Issue Watch → optional auto-overview: duty picks suggested preview or manual Build overview.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from . import cards as _cards
from . import config as _config
from . import drafts as _drafts
from . import lark_client as _lark
from . import session as _session
from . import support as _support
from . import text_processing as _text

log = logging.getLogger("lark-ops-ai")

_ALERT_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_TTL_SEC = 7200.0


def make_alert_key(chat_id: str, message_id: str, fingerprint: str) -> str:
    raw = f"{(chat_id or '').strip()}:{(message_id or '').strip()}:{(fingerprint or '').strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _prune_cache() -> None:
    cutoff = time.time() - _CACHE_TTL_SEC
    global _ALERT_CACHE
    _ALERT_CACHE = {k: v for k, v in _ALERT_CACHE.items() if float(v.get("ts") or 0) >= cutoff}


def store_alert_snapshot(alert_key: str, payload: Dict[str, Any]) -> None:
    key = (alert_key or "").strip()
    if not key:
        return
    _prune_cache()
    row = dict(payload)
    row["ts"] = time.time()
    _ALERT_CACHE[key] = row


def get_alert_snapshot(alert_key: str) -> Optional[Dict[str, Any]]:
    key = (alert_key or "").strip()
    if not key:
        return None
    _prune_cache()
    row = _ALERT_CACHE.get(key)
    return dict(row) if row else None


def resolve_overview_routing(detection_chat_id: str) -> Tuple[str, str, str]:
    """
    Returns ``(source_incident_chat_id, target_chat, group_label)`` for DM overview scope.
    """
    cid = (detection_chat_id or "").strip()
    label = _config.get_emergency_topic_for_source_chat(cid).strip()
    if not label or label.startswith("oc_"):
        label = _session.get_source_chat_label_for_target_chat(cid) or cid

    target = ""
    sess = _session.P0_SESSIONS.get(cid)
    if sess:
        target = str(sess.get("target_chat") or "").strip()
    if not target:
        m = _config.get_incident_overview_target_map()
        if cid in m:
            target = str(m[cid] or "").strip()
    if not target:
        fallback = _config.get_overview_target_group_chat_id()
        target = fallback if fallback.startswith("oc_") else cid
    if not cid.startswith("oc_"):
        cid = target or cid
    return cid, target, label


def prepare_alert_for_overview(payload: Dict[str, Any]) -> str:
    """Store snapshot and return ``alert_key`` for card buttons (empty when disabled)."""
    if not _config.get_p0_issue_watch_auto_overview_enabled():
        return ""
    if payload.get("supplemental_player_ids"):
        return ""
    cid = str(payload.get("chat_id") or "").strip()
    mid = str(payload.get("message_id") or "").strip()
    fp = str(payload.get("fingerprint") or "generic").strip()
    if not cid or not mid:
        return ""
    key = make_alert_key(cid, mid, fp)
    src, tgt, _label = resolve_overview_routing(cid)
    snap = dict(payload)
    snap["source_incident_chat_id"] = src
    snap["target_chat"] = tgt
    store_alert_snapshot(key, snap)
    return key


def _primary_category_label(categories_md: str) -> str:
    for line in (categories_md or "").splitlines():
        t = line.strip().lstrip("•").strip()
        if not t:
            continue
        if re.search(r"(?i)\d+\s+players?\s+are\s+affected", t):
            continue
        return t
    return ""


def build_overview_fields_from_alert(
    snapshot: Dict[str, Any],
    tenant_token: str,
) -> Tuple[str, str, str, str]:
    """``issue``, ``impact``, ``support``, ``combined_text`` for preview / draft."""
    summary = str(snapshot.get("summary") or "").strip()
    concern = str(snapshot.get("concern_raw") or snapshot.get("concern") or "").strip()
    concern = re.sub(r"^[「『]|[」』]$", "", concern).strip()
    cat = _primary_category_label(str(snapshot.get("categories_md") or ""))
    player_ids = [str(x).strip() for x in (snapshot.get("player_ids") or []) if str(x).strip()]
    try:
        n = max(len(player_ids), int(snapshot.get("players_count") or 0))
    except (TypeError, ValueError):
        n = len(player_ids)

    issue = summary or concern
    if cat and cat.lower() not in (issue or "").lower():
        issue = f"{cat}: {summary}" if summary else cat
    issue = (issue or "").strip() or "Not specified"

    id_blob = ", ".join(player_ids[:24])
    combined_parts = [p for p in [concern, f"Account IDs: {id_blob}" if id_blob else ""] if p]
    combined_text = "\n\n".join(combined_parts).strip() or concern or summary

    bits: List[str] = []
    if n >= 1:
        bits.append(f"{n} players are affected" if n != 1 else "1 player is affected")
    if player_ids:
        bits.append(f"Affected player IDs: {id_blob}")
    if cat:
        bits.append(cat)
    impact = "; ".join(bits) if bits else _text.build_impact_scope(combined_text)
    if _text.is_not_specified(impact):
        impact = "Not specified"

    support = _support.build_support_request(combined_text, tenant_token)
    return issue, impact, support, combined_text


def _session_start_epoch(source_incident_chat_id: str, target_chat: str) -> int:
    src = (source_incident_chat_id or "").strip()
    if src and src in _session.P0_SESSIONS:
        return int(_session.P0_SESSIONS[src].get("start_epoch") or time.time())
    _cid, sess = _session.find_session_by_target_chat(target_chat)
    if sess:
        return int(sess.get("start_epoch") or time.time())
    return int(time.time())


def handle_use_suggested_overview(
    operator_open_id: str,
    tenant_token: str,
    *,
    alert_key: str = "",
    source_incident_chat_id: str = "",
    target_chat: str = "",
) -> None:
    oid = (operator_open_id or "").strip()
    tok = (tenant_token or "").strip()
    if not oid or not tok:
        return

    snap = get_alert_snapshot(alert_key)
    src = (source_incident_chat_id or "").strip()
    tgt = (target_chat or "").strip()
    if snap:
        src = str(snap.get("source_incident_chat_id") or src).strip()
        tgt = str(snap.get("target_chat") or tgt).strip()
    if not src and snap:
        src, tgt, _ = resolve_overview_routing(str(snap.get("chat_id") or ""))
    if not tgt:
        _lark.post_text_to_open_id(oid, tok, "⚠️ No overview target chat configured for this detection group.")
        return

    if not snap:
        _lark.post_text_to_open_id(oid, tok, "⚠️ This detection alert expired — paste concern in DM and use **Build overview**.")
        return

    issue, impact, support, combined = build_overview_fields_from_alert(snap, tok)
    _drafts.seed_draft_for_incident(oid, tgt, src, "P0")
    if combined:
        _drafts.add_text_to_draft(oid, tgt, combined)

    start_epoch = _session_start_epoch(src, tgt)
    _drafts.save_preview(
        sender_open_id=oid,
        target_chat=tgt,
        start_epoch=start_epoch,
        combined_text=combined,
        mention_names=[],
        issue=issue,
        impact=impact,
        support=support,
        priority="P0",
        source_incident_chat_id=src,
    )
    pv = _drafts.get_preview(oid) or {}
    md = str(pv.get("md") or "").strip()
    if not md:
        _lark.post_text_to_open_id(oid, tok, "⚠️ Could not build overview from this alert.")
        return

    lab = _session.get_source_chat_label_for_target_chat(tgt) or str(snap.get("group_label") or "")
    card = _cards.build_preview_card(
        md,
        priority="P0",
        source_chat_label=lab,
        update_multi=True,
        target_chat=tgt,
        source_incident_chat_id=src,
    )
    if not _drafts.post_or_patch_preview_card(oid, tok, card):
        _lark.post_text_to_open_id(oid, tok, "❌ Failed to send overview preview card.")
        return

    if not _session.dm_preview_allowed_for_incident(src, tgt):
        _lark.post_text_to_open_id(
            oid,
            tok,
            "📝 **Suggested overview** is ready — tap **Edit** if needed. "
            "Declare **P0** in the detection group first, then **Send to group**.",
        )
    else:
        _lark.post_text_to_open_id(
            oid,
            tok,
            "📝 **Suggested overview** from detection alert — review, **Edit** if needed, then **Send to group**.",
        )
    log.info(
        "issue_watch_overview: suggested preview open_id_tail=%s src=%s tgt_tail=%s alert_key=%s",
        oid[-8:] if len(oid) > 8 else oid,
        src[:24],
        tgt[-12:] if len(tgt) > 12 else tgt,
        (alert_key or "")[:12],
    )


def handle_manual_overview(
    operator_open_id: str,
    tenant_token: str,
    *,
    alert_key: str = "",
    source_incident_chat_id: str = "",
    target_chat: str = "",
) -> None:
    oid = (operator_open_id or "").strip()
    tok = (tenant_token or "").strip()
    if not oid or not tok:
        return

    snap = get_alert_snapshot(alert_key)
    src = (source_incident_chat_id or "").strip()
    tgt = (target_chat or "").strip()
    label = ""
    if snap:
        src = str(snap.get("source_incident_chat_id") or src).strip()
        tgt = str(snap.get("target_chat") or tgt).strip()
        label = str(snap.get("group_label") or "").strip()
    if not src and snap:
        src, tgt, label = resolve_overview_routing(str(snap.get("chat_id") or ""))
    if not tgt:
        _lark.post_text_to_open_id(oid, tok, "⚠️ No overview target chat configured for this detection group.")
        return

    _drafts.clear_preview(oid)
    _drafts.cancel_preview_timer(oid)
    _drafts.seed_draft_for_incident(oid, tgt, src, "P0")

    _session._send_dm_instruction_card_logged(
        oid,
        tok,
        "P0",
        label or _session.get_source_chat_label_for_target_chat(tgt),
        "issue watch manual overview",
        target_chat=tgt,
        source_incident_chat_id=src,
    )
    _lark.post_text_to_open_id(
        oid,
        tok,
        "📝 **Manual overview** — paste screenshots or text in any order, then tap **Build overview**.",
    )
    log.info(
        "issue_watch_overview: manual flow open_id_tail=%s src=%s tgt_tail=%s",
        oid[-8:] if len(oid) > 8 else oid,
        src[:24],
        tgt[-12:] if len(tgt) > 12 else tgt,
    )
