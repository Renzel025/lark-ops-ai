"""Rolling per-chat message buffer + concern resolution for the typed-"p0" auto-overview (Path B).

When a P0 is declared by TYPING "p0" (not an Issue Watch alert, which is tied to a specific detected
concern), the auto-overview needs to know WHICH concern it is about — especially in a busy group where
many issues were raised before the "p0". This resolves the concern text the overview is built from:

  1. reply / thread  -> the parent (replied-to) message's text  — EXACT
  2. else, AI-pick    -> Claude chooses the concern among recent messages (P0_TYPED_DECLARE_CONCERN_AI_PICK)
  3. else, recent     -> the most recent substantive message before the "p0"
  4. else             -> the declaration text itself (unchanged)

The buffer records every group message the bot already receives (no new Lark scope), keeping each
message's ``parent_id``/``root_id`` so the reply anchor works without threading it through callers.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from p0_logic import config as _config

log = logging.getLogger("lark-ops-ai")

_BUF_LOCK = threading.Lock()
# chat_id -> deque[{message_id, parent_id, root_id, sender, text, ts}]
_BUF: Dict[str, "Deque[Dict[str, Any]]"] = {}
_BUF_MAX = 40          # messages kept per chat
_BUF_TTL = 7200.0      # 2h — older rows are ignored when resolving

_BARE_P0_RE = re.compile(r"(?is)^\W*(?:p0|priority\s*0|p1|priority\s*1)\W*$")
_ID_ONLY_RE = re.compile(r"(?is)^[\d\s,;:.\-]+$")


def record_group_message(
    chat_id: str,
    *,
    message_id: str = "",
    parent_id: str = "",
    root_id: str = "",
    sender_open_id: str = "",
    text: str = "",
    ts: Optional[float] = None,
) -> None:
    """Append a received group message to the rolling buffer. Cheap; called on every group message."""
    cid = (chat_id or "").strip()
    body = (text or "").strip()
    if not cid or not body:
        return
    row = {
        "message_id": (message_id or "").strip(),
        "parent_id": (parent_id or "").strip(),
        "root_id": (root_id or "").strip(),
        "sender": (sender_open_id or "").strip(),
        "text": body,
        "ts": float(ts) if ts else time.time(),
    }
    with _BUF_LOCK:
        dq = _BUF.get(cid)
        if dq is None:
            dq = deque(maxlen=_BUF_MAX)
            _BUF[cid] = dq
        dq.append(row)


def _rows(cid: str) -> List[Dict[str, Any]]:
    with _BUF_LOCK:
        return list(_BUF.get(cid) or [])


def _text_by_id(cid: str, message_id: str) -> str:
    mid = (message_id or "").strip()
    if not mid:
        return ""
    for r in reversed(_rows(cid)):
        if r["message_id"] == mid:
            return str(r.get("text") or "").strip()
    return ""


def _row_by_id(cid: str, message_id: str) -> Optional[Dict[str, Any]]:
    mid = (message_id or "").strip()
    if not mid:
        return None
    for r in reversed(_rows(cid)):
        if r["message_id"] == mid:
            return r
    return None


def _is_bare_priority(text: str) -> bool:
    return bool(_BARE_P0_RE.match((text or "").strip()))


def _looks_substantive(text: str) -> bool:
    """A candidate concern: long enough, not a bare 'p0', not just a list of IDs/numbers."""
    t = (text or "").strip()
    if len(t) < _config.get_p0_issue_watch_min_text_len():
        return False
    if _is_bare_priority(t) or _ID_ONLY_RE.match(t):
        return False
    return True


def _recent_concerns(cid: str, *, exclude_message_id: str = "") -> List[Dict[str, Any]]:
    now = time.time()
    within = _config.get_p0_typed_declare_concern_window_min() * 60.0
    limit = _config.get_p0_typed_declare_concern_max_msgs()
    ex = (exclude_message_id or "").strip()
    out: List[Dict[str, Any]] = []
    for r in _rows(cid):
        if ex and r["message_id"] == ex:
            continue
        if (now - float(r.get("ts") or 0)) > within:
            continue
        if not _looks_substantive(str(r.get("text") or "")):
            continue
        out.append(r)
    return out[-limit:] if limit > 0 else out


def _ai_pick_concern(decl_text: str, concerns: List[Dict[str, Any]]) -> str:
    """Ask Claude which recent message is the concern being declared P0. Returns its text or ''."""
    if not concerns:
        return ""
    from p0_logic import anthropic_client as _anthropic

    numbered = "\n".join(f"[{i}] {str(c.get('text') or '')[:400]}" for i, c in enumerate(concerns))
    system = (
        "You help an incident bot pick which recent chat message is the concern being declared as a "
        "P0 incident. You are given the declaration message and a numbered list of recent messages. "
        "Reply with ONLY the bracket number of the single message that best describes the concern "
        "being declared (e.g. 2). If none of them is a real concern, reply -1. No other text."
    )
    user = (
        f"Declaration message: {(decl_text or '(bare p0)').strip()[:300]!r}\n\n"
        f"Recent messages:\n{numbered}\n\n"
        "Which bracket number is the concern being declared as P0?"
    )
    try:
        ans = (_anthropic.anthropic_chat_once(system, user, max_tokens=8) or "").strip()
    except Exception as e:  # noqa: BLE001
        log.warning("concern_context: AI-pick failed err=%s", e)
        return ""
    m = re.search(r"-?\d+", ans)
    if not m:
        return ""
    try:
        idx = int(m.group(0))
    except ValueError:
        return ""
    if 0 <= idx < len(concerns):
        log.info("concern_context: AI-pick chose [%s] of %s recent concern(s)", idx, len(concerns))
        return str(concerns[idx].get("text") or "").strip()
    return ""


def _combine(concern: str, decl_text: str) -> str:
    """Concern is the source; append the declaration text only if it adds detail beyond a bare 'p0'."""
    concern = (concern or "").strip()
    decl = (decl_text or "").strip()
    if not concern:
        return decl
    if decl and not _is_bare_priority(decl) and decl not in concern:
        return f"{concern}\n\n{decl}"
    return concern


def resolve_declaration_concern(
    chat_id: str,
    *,
    decl_message_id: str = "",
    decl_text: str = "",
) -> str:
    """Resolve the concern text a typed-"p0" auto-overview should be built from (see module docstring).

    Falls back to ``decl_text`` unchanged when nothing better is found or the feature is off.
    """
    cid = (chat_id or "").strip()
    decl = (decl_text or "").strip()
    if not cid or not _config.get_p0_typed_declare_auto_overview_enabled():
        return decl

    # 1. Reply / thread anchor — the parent (or thread root) message's text. Parent id comes from the
    #    buffered declaration row, so no caller has to thread it through.
    row = _row_by_id(cid, decl_message_id)
    if row:
        for pid in (str(row.get("parent_id") or ""), str(row.get("root_id") or "")):
            ptext = _text_by_id(cid, pid)
            if ptext and not _is_bare_priority(ptext) and ptext != decl:
                log.info("concern_context: resolved concern via reply-parent cid_tail=%s", cid[-8:])
                return _combine(ptext, decl)

    concerns = _recent_concerns(cid, exclude_message_id=decl_message_id)
    if not concerns:
        return decl

    # 2. AI-pick among the recent concerns.
    if _config.get_p0_typed_declare_concern_ai_pick_enabled():
        picked = _ai_pick_concern(decl, concerns)
        if picked:
            return _combine(picked, decl)

    # 3. Most recent substantive message.
    log.info("concern_context: resolved concern via most-recent (of %s) cid_tail=%s", len(concerns), cid[-8:])
    return _combine(str(concerns[-1].get("text") or ""), decl)
