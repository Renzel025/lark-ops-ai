"""
Issue Watch → optional auto-overview: duty picks suggested preview or manual Build overview.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from p0_logic import cards as _cards
from p0_logic import config as _config
from features.overview import drafts as _drafts
from features.overview import overview_ai as _overview_ai
from . import issue_watch_alert_disk as _iw_disk
from features.overview import issues as _issues
from p0_logic import lark_client as _lark
from features.session import session as _session
from p0_logic import support as _support
from p0_logic import text_processing as _text

log = logging.getLogger("lark-ops-ai")

_ALERT_CACHE: Dict[str, Dict[str, Any]] = {}
_ALERT_INDEX_BY_CHAT: Dict[str, Tuple[str, float]] = {}
_CACHE_TTL_SEC = 7200.0


def make_alert_key(chat_id: str, message_id: str, fingerprint: str) -> str:
    raw = f"{(chat_id or '').strip()}:{(message_id or '').strip()}:{(fingerprint or '').strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _prune_cache() -> None:
    cutoff = time.time() - _CACHE_TTL_SEC
    global _ALERT_CACHE, _ALERT_INDEX_BY_CHAT
    _ALERT_CACHE = {k: v for k, v in _ALERT_CACHE.items() if float(v.get("ts") or 0) >= cutoff}
    _ALERT_INDEX_BY_CHAT = {
        cid: (key, ts)
        for cid, (key, ts) in _ALERT_INDEX_BY_CHAT.items()
        if float((_ALERT_CACHE.get(key) or {}).get("ts") or ts) >= cutoff
    }


def store_alert_snapshot(alert_key: str, payload: Dict[str, Any]) -> None:
    key = (alert_key or "").strip()
    if not key:
        return
    _prune_cache()
    row = dict(payload)
    row["ts"] = time.time()
    _ALERT_CACHE[key] = row
    cid = str(row.get("chat_id") or "").strip()
    if cid:
        _ALERT_INDEX_BY_CHAT[cid] = (key, float(row["ts"]))
        _iw_disk.save_alert_snapshot(cid, key, row)


def find_latest_alert_key_for_chat(chat_id: str, max_age_sec: float = _CACHE_TTL_SEC) -> str:
    """Most recent Issue Watch alert for a detection group (used after P0 declare)."""
    cid = (chat_id or "").strip()
    if not cid:
        return ""
    _prune_cache()
    indexed = _ALERT_INDEX_BY_CHAT.get(cid)
    if indexed:
        key, ts = indexed
        if time.time() - float(ts) <= max_age_sec and key in _ALERT_CACHE:
            return key
    best_key = ""
    best_ts = 0.0
    for key, row in _ALERT_CACHE.items():
        if str(row.get("chat_id") or "").strip() != cid:
            continue
        ts = float(row.get("ts") or 0)
        if ts > best_ts:
            best_ts = ts
            best_key = key
    if best_key and time.time() - best_ts <= max_age_sec:
        _ALERT_INDEX_BY_CHAT[cid] = (best_key, best_ts)
        return best_key
    disk_row = _iw_disk.load_latest_alert(cid, max_age_sec=max_age_sec)
    if disk_row:
        dkey = str(disk_row.get("alert_key") or "").strip()
        if not dkey:
            dkey = make_alert_key(
                cid,
                str(disk_row.get("message_id") or ""),
                str(disk_row.get("fingerprint") or "generic"),
            )
        _ALERT_CACHE[dkey] = disk_row
        _ALERT_INDEX_BY_CHAT[cid] = (dkey, float(disk_row.get("ts") or time.time()))
        log.info(
            "issue_watch_overview: restored alert from disk chat_id_tail=%s alert_key=%s",
            cid[-12:] if len(cid) > 12 else cid,
            dkey[:12],
        )
        return dkey
    return ""


_DECLARE_CLAIM_LOCK = threading.Lock()


def attach_alert_card_message(alert_key: str, open_id: str, message_id: str) -> None:
    """Remember which DM card carries this alert for each recipient, so declare/dismiss can PATCH
    every copy — otherwise the other duty person's card keeps live buttons after a declare."""
    key = (alert_key or "").strip()
    oid = (open_id or "").strip()
    mid = (message_id or "").strip()
    if not key or not oid or not mid:
        return
    with _DECLARE_CLAIM_LOCK:
        row = _ALERT_CACHE.get(key)
        if not row:
            return
        cards = [c for c in (row.get("card_messages") or []) if isinstance(c, dict)]
        if any(str(c.get("message_id") or "") == mid for c in cards):
            return
        cards.append({"open_id": oid, "message_id": mid})
        row["card_messages"] = cards
        cid = str(row.get("chat_id") or "").strip()
        if cid:
            _iw_disk.save_alert_snapshot(cid, key, row)


def alert_card_messages(alert_key: str) -> List[Dict[str, str]]:
    snap = get_alert_snapshot(alert_key) or {}
    return [c for c in (snap.get("card_messages") or []) if isinstance(c, dict)]


def claim_alert_declare(alert_key: str, operator_open_id: str) -> Tuple[bool, str]:
    """Atomically mark this alert declared. Returns ``(claimed, already_declared_by_open_id)``.

    Stops a second click — from the same person after the meeting ended, or from the other duty
    recipient — creating another meeting for the same detection alert.
    """
    key = (alert_key or "").strip()
    oid = (operator_open_id or "").strip()
    if not key:
        return True, ""
    with _DECLARE_CLAIM_LOCK:
        row = _ALERT_CACHE.get(key)
        if not row:
            return True, ""
        prev = str(row.get("declared_by") or "").strip()
        if prev:
            return False, prev
        row["declared_by"] = oid
        row["declared_at"] = int(time.time())
        cid = str(row.get("chat_id") or "").strip()
        if cid:
            _iw_disk.save_alert_snapshot(cid, key, row)
        return True, ""


def build_declared_alert_card(alert_key: str, note: str) -> Optional[Dict[str, Any]]:
    """Rebuild the alert card in its terminal state (same body, buttons replaced by ``note``)."""
    snap = get_alert_snapshot(alert_key)
    if not snap:
        return None
    return _cards.build_issue_watch_alert_card(
        group_label=str(snap.get("group_label") or ""),
        categories_md=str(snap.get("categories_md") or ""),
        summary=str(snap.get("summary") or ""),
        concern=str(snap.get("concern") or snap.get("concern_raw") or ""),
        alert_time=str(snap.get("alert_time") or ""),
        player_ids_md=str(snap.get("player_ids_md") or ""),
        source_message_link=str(snap.get("source_message_link") or ""),
        source_message_time=str(snap.get("source_message_time") or ""),
        declared_note=note,
    )


def patch_alert_cards(tenant_token: str, alert_key: str, note: str, *, only_message_id: str = "") -> int:
    """PATCH the alert card(s) into a terminal state. Returns how many were updated."""
    tok = (tenant_token or "").strip()
    card = build_declared_alert_card(alert_key, note)
    if not tok or not card:
        return 0
    targets = alert_card_messages(alert_key)
    only = (only_message_id or "").strip()
    if only:
        targets = [c for c in targets if str(c.get("message_id") or "") == only] or [
            {"open_id": "", "message_id": only}
        ]
    done = 0
    for c in targets:
        mid = str(c.get("message_id") or "").strip()
        if not mid:
            continue
        st, body = _lark.patch_interactive_card(tok, mid, card)
        if st == 200:
            done += 1
        else:
            log.warning(
                "issue_watch_overview: alert card patch failed HTTP=%s mid_tail=%s body=%s",
                st,
                mid[-12:] if len(mid) > 12 else mid,
                (body or "")[:200],
            )
    log.info(
        "issue_watch_overview: alert cards patched=%s/%s alert_key=%s",
        done,
        len(targets),
        (alert_key or "")[:12],
    )
    return done


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
        target = _config.get_overview_target_chat_id_for_source_incident(cid) or cid
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


def _issue_from_groq_triplet(triplet: Tuple[str, str, str]) -> str:
    issue_en_raw, _zh_issue, _zh_impact = triplet
    issue = (issue_en_raw or "").strip()
    issue = re.sub(r"\b\d{6,}\b", "", issue)
    issue = re.sub(r"\b\d{4}[/-]\d{1,2}[/-]\d{1,2}\b", "", issue)
    issue = re.sub(r"\s+", " ", issue).strip(" ,.")
    return _issues._truncate_issue_output(issue, _issues.ISSUE_SUMMARY_MAX_CHARS) if issue else "Not specified"


def build_overview_fields_from_alert(
    snapshot: Dict[str, Any],
    tenant_token: str,
) -> Tuple[str, str, str, str, Optional[str], Optional[str]]:
    """
    Build overview fields from an Issue Watch alert snapshot.

    Issue + bilingual zh lines use the same Groq one-shot path as manual **Build overview**.
    Impact scope stays rule-based (player IDs / counts from the alert).
    """
    summary = str(snapshot.get("summary") or "").strip()
    concern = str(snapshot.get("concern_raw") or snapshot.get("concern") or "").strip()
    concern = re.sub(r"^[「『]|[」』]$", "", concern).strip()
    cat = _primary_category_label(str(snapshot.get("categories_md") or ""))
    player_ids = [str(x).strip() for x in (snapshot.get("player_ids") or []) if str(x).strip()]
    try:
        n = max(len(player_ids), int(snapshot.get("players_count") or 0))
    except (TypeError, ValueError):
        n = len(player_ids)

    id_blob = ", ".join(player_ids[:24])
    combined_parts = [p for p in [concern, f"Account IDs: {id_blob}" if id_blob else ""] if p]
    combined_text = "\n\n".join(combined_parts).strip() or concern or summary

    bits: List[str] = []
    if n >= 1:
        count_line = f"{n} players are affected" if n != 1 else "1 player is affected"
        if cat and cat.lower() not in count_line.lower():
            bits.append(f"{count_line} ({cat})")
        else:
            bits.append(count_line)
    elif cat:
        bits.append(cat)
    impact = "; ".join(bits) if bits else _text.build_impact_scope(combined_text)
    if _text.is_not_specified(impact):
        impact = "Not specified"

    zh_issue_pc: Optional[str] = None
    zh_impact_pc: Optional[str] = None
    triplet: Optional[Tuple[str, str, str]] = None

    def _support_only() -> str:
        return _support.build_support_request(combined_text, tenant_token)

    def _triplet_only() -> Optional[Tuple[str, str, str]]:
        return _overview_ai.overview_issue_and_zh_bilingual(combined_text, impact)

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_sup = pool.submit(_support_only)
        f_ai = pool.submit(_triplet_only)
        support = f_sup.result()
        triplet = f_ai.result()

    if triplet:
        issue = _issue_from_groq_triplet(triplet)
        zh_issue_pc, zh_impact_pc = triplet[1], triplet[2]
    else:
        issue = _issues.summarize_issue(combined_text)

    return issue, impact, support, combined_text, zh_issue_pc, zh_impact_pc


def _recall_dm_messages(tenant_token: str, *message_ids: str) -> None:
    tok = (tenant_token or "").strip()
    if not tok:
        return
    seen: set[str] = set()
    for raw in message_ids:
        mid = (raw or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        try:
            st, body = _lark.recall_im_message(tok, mid)
            if st != 200:
                log.warning(
                    "issue_watch_overview: recall failed message_id=%s HTTP=%s body=%s",
                    mid[:24],
                    st,
                    (body or "")[:200],
                )
        except Exception as e:
            log.warning("issue_watch_overview: recall failed message_id=%s err=%s", mid[:24], e)


def _recall_issue_watch_declare_dms(
    operator_open_id: str,
    tenant_token: str,
    *,
    clicked_card_message_id: str = "",
) -> None:
    """Remove suggested-preview DM clutter when duty chooses manual overview."""
    oid = (operator_open_id or "").strip()
    tok = (tenant_token or "").strip()
    if not oid:
        return
    pv = _drafts.get_preview(oid) or {}
    _recall_dm_messages(
        tok,
        str(pv.get("preview_message_id") or ""),
        str(pv.get("issue_watch_declare_hint_message_id") or ""),
        str(pv.get("issue_watch_declare_manual_message_id") or ""),
        clicked_card_message_id,
    )


def _session_start_epoch(source_incident_chat_id: str, target_chat: str) -> int:
    src = (source_incident_chat_id or "").strip()
    if src and src in _session.P0_SESSIONS:
        return int(_session.P0_SESSIONS[src].get("start_epoch") or time.time())
    _cid, sess = _session.find_session_by_target_chat(target_chat)
    if sess:
        return int(sess.get("start_epoch") or time.time())
    return int(time.time())


def _post_suggested_overview_preview(
    operator_open_id: str,
    tenant_token: str,
    snap: Dict[str, Any],
    *,
    alert_key: str = "",
    source_incident_chat_id: str = "",
    target_chat: str = "",
    on_p0_declare: bool = False,
) -> bool:
    """Build draft + blue preview card from an Issue Watch snapshot. Returns True on success."""
    oid = (operator_open_id or "").strip()
    tok = (tenant_token or "").strip()
    if not oid or not tok or not snap:
        return False

    src = (source_incident_chat_id or "").strip()
    tgt = (target_chat or "").strip()
    src = str(snap.get("source_incident_chat_id") or src).strip()
    tgt = str(snap.get("target_chat") or tgt).strip()
    if not src:
        src, tgt, _ = resolve_overview_routing(str(snap.get("chat_id") or ""))
    if not tgt:
        _lark.post_text_to_open_id(oid, tok, "⚠️ No overview target chat configured for this detection group.")
        return False

    issue, impact, support, combined, zh_issue, zh_impact = build_overview_fields_from_alert(snap, tok)
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
        zh_issue_precomputed=zh_issue,
        zh_impact_precomputed=zh_impact,
    )
    pv = _drafts.get_preview(oid) or {}
    md = str(pv.get("md") or "").strip()
    if not md:
        _lark.post_text_to_open_id(oid, tok, "⚠️ Could not build overview from this alert.")
        return False

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
        return False

    if on_p0_declare:
        st_h, body_h = _lark.post_text_to_open_id(
            oid,
            tok,
            _cards.build_issue_watch_declare_overview_hint_text(),
        )
        hint_mid = _lark.parse_im_message_id_from_response(body_h) if st_h == 200 else ""
        _drafts.patch_preview_fields(
            oid,
            issue_watch_declare_hint_message_id=hint_mid,
        )
    elif not _session.dm_preview_allowed_for_incident(src, tgt):
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
        "issue_watch_overview: suggested preview open_id_tail=%s src=%s tgt_tail=%s alert_key=%s declare=%s",
        oid[-8:] if len(oid) > 8 else oid,
        src[:24],
        tgt[-12:] if len(tgt) > 12 else tgt,
        (alert_key or "")[:12],
        on_p0_declare,
    )
    return True


def push_suggested_overview_on_p0_declare(
    operator_open_id: str,
    tenant_token: str,
    alert_key: str,
    *,
    source_incident_chat_id: str = "",
    target_chat: str = "",
    source_chat_label: str = "",
) -> bool:
    """After P0 declare: auto-DM suggested overview preview (duty keeps or builds manually)."""
    key = (alert_key or "").strip()
    snap = get_alert_snapshot(key)
    if not snap:
        return False
    if source_chat_label:
        snap = dict(snap)
        snap.setdefault("group_label", source_chat_label)
    return _post_suggested_overview_preview(
        operator_open_id,
        tenant_token,
        snap,
        alert_key=key,
        source_incident_chat_id=source_incident_chat_id,
        target_chat=target_chat,
        on_p0_declare=True,
    )


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
    if not snap:
        _lark.post_text_to_open_id(oid, tok, "⚠️ This detection alert expired — paste concern in DM and use **Build overview**.")
        return
    _post_suggested_overview_preview(
        oid,
        tok,
        snap,
        alert_key=alert_key,
        source_incident_chat_id=source_incident_chat_id,
        target_chat=target_chat,
        on_p0_declare=False,
    )


def handle_manual_overview(
    operator_open_id: str,
    tenant_token: str,
    *,
    alert_key: str = "",
    source_incident_chat_id: str = "",
    target_chat: str = "",
    clicked_card_message_id: str = "",
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

    _recall_issue_watch_declare_dms(oid, tok, clicked_card_message_id=clicked_card_message_id)
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
    log.info(
        "issue_watch_overview: manual flow (declare DMs recalled) open_id_tail=%s src=%s tgt_tail=%s",
        oid[-8:] if len(oid) > 8 else oid,
        src[:24],
        tgt[-12:] if len(tgt) > 12 else tgt,
    )
