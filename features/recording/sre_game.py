"""SRE Game escalation ring — /srebac /srer /sredt /sresic /srebl /srepai /srecg /srepp /sredb /sreib.

The "SRE Game" section of the OSE & SRE Duty Shift sheet lists, per game, an ORDERED contact list
(1st, 2nd, 3rd… contact). No per-day checkboxes — the row ORDER is the escalation priority. A command
rings the 1st contact into the active P0 meeting, then WATCHES for them to join:

    @bot /srebac  -> ring Wylie (1st), wait 90s
      • Wylie JOINS the VC within 90s -> ✅ reached, stop (auto).
      • Wylie does NOT join in 90s     -> ⏱️ prompt: reply /n to invite Chi Sheun (2nd), /r to retry Wylie.

Lark has no "invite declined/expired" event, so "did not accept" = "did not JOIN within the timeout".
Replies in the command's thread: /n = escalate to next, /r = retry current, /y = mark reached (stop).
``sredt``/``sresic`` share "Dragon Tiger & Sicbo"; ``srecg``/``srepp`` share "Colorgame & Pulaputi".
Contacts resolve name->open_id via the OpenID directory (subject to the same primary-app requirement).
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Tuple

from p0_logic import config as _config
from p0_logic import lark_client as _lark
from features.recording import duty_roster as _duty
from features.recording import duty_directory as _dir
from features.recording import vc_ring as _vc_ring

log = logging.getLogger("lark-ops-ai")

SRE_GAME_HEADERS: Dict[str, str] = {
    "srebac": "BACCARAT", "srer": "ROULETTE", "sredt": "DRAGON TIGER", "sresic": "SICBO",
    "srebl": "BLACK JACK", "srepai": "PAIGOW", "srecg": "COLORGAME", "srepp": "PULAPUTI",
    "sredb": "DROPBALL", "sreib": "IN BETWEEN",
}
SRE_GAME_LABEL: Dict[str, str] = {
    "srebac": "Baccarat", "srer": "Roulette", "sredt": "Dragon Tiger", "sresic": "Sicbo",
    "srebl": "Blackjack", "srepai": "Paigow", "srecg": "Colorgame", "srepp": "Pulaputi",
    "sredb": "Dropball", "sreib": "In Between",
}
SRE_GAME_CMD_RE = re.compile(
    r"^(srebac|srer|sredt|sresic|srebl|srepai|srecg|srepp|sredb|sreib)$", re.IGNORECASE
)

_GAME_HEADER_KWS = (
    "BACCARAT", "ROULETTE", "DRAGON TIGER", "SICBO", "BLACK JACK", "PAIGOW",
    "COLORGAME", "PULAPUTI", "DROPBALL", "IN BETWEEN",
)
# The next top-level sections below "SRE Game" — LIVESLOT sits right after "In Between".
_SECTION_ENDS = ("LIVESLOT", "EGAME", "IT TEAM", "SRE PLATFORM", "DBA", "BACKEND TEAM", "FRONTEND TEAM")

_ESC_LOCK = threading.RLock()
# thread_root_message_id -> {cmd,label,pairs:[(name,open_id)],idx,session_source,notify_chat,ts,
#                            awaiting_oid, reached:bool, timer:threading.Timer|None}
_ESC_BY_THREAD: Dict[str, Dict[str, Any]] = {}
_ESC_TTL_SEC = 7200.0


def _timeout_sec() -> float:
    _config.reload_env_runtime()
    raw = (os.getenv("P0_SRE_GAME_INVITE_TIMEOUT_SEC") or "").strip()
    try:
        v = float(raw)
        return v if v > 0 else 90.0
    except (TypeError, ValueError):
        return 90.0


def is_sre_game_command(cmd: str) -> bool:
    return bool(SRE_GAME_CMD_RE.match((cmd or "").strip().lower()))


def _up(cell: Any) -> str:
    return re.sub(r"\s+", " ", str(cell if cell is not None else "").strip()).upper()


def parse_sre_game_contacts(rows: List[List[Any]], header_kw: str) -> List[str]:
    """Ordered contact NAMES for a game, scoped to the 'SRE Game' section (so 'COLORGAME' inside
    EGAME's 'ColorGameSlot' is never matched). Skips 'If can't contact…' notes; stops at the next
    game sub-header, a following section, or a run of blanks."""
    header_kw = header_kw.upper()
    start = -1
    for i, row in enumerate(rows):
        if "SRE GAME" in _up(row[0] if row else ""):
            start = i
            break
    if start < 0:
        return []
    game_idx = -1
    for i in range(start + 1, len(rows)):
        up = _up(rows[i][0] if rows[i] else "")
        if any(e in up for e in _SECTION_ENDS):
            return []
        if header_kw in up:
            game_idx = i
            break
    if game_idx < 0:
        return []
    names: List[str] = []
    seen: set = set()
    blanks = 0
    for row in rows[game_idx + 1:]:
        col_a = str((row[0] if row else "") or "").strip()
        if not col_a:
            blanks += 1
            if blanks >= 3:
                break
            continue
        blanks = 0
        up = _up(col_a)
        if any(e in up for e in _SECTION_ENDS) or any(e in up for e in _GAME_HEADER_KWS):
            break
        if "IF CAN'T CONTACT" in up or "IF CANT CONTACT" in up:
            continue
        nm = _duty._clean_person_name(col_a)
        key = _duty._norm_name(nm)
        if nm and key not in seen:
            seen.add(key)
            names.append(nm)
    return names


def resolve_sre_game_contacts(cmd: str, tenant_token: str) -> List[Tuple[str, str]]:
    """Ordered ``[(name, open_id)]`` for a game command (open_id '' when not in the directory)."""
    c = (cmd or "").strip().lower()
    kw = SRE_GAME_HEADERS.get(c)
    if not kw:
        return []
    rows = _duty._read_shift_rows(tenant_token)
    if not rows:
        return []
    return [(nm, _dir.resolve_open_id_for_name(tenant_token, nm)) for nm in parse_sre_game_contacts(rows, kw)]


def _ordinal(n: int) -> str:
    if 10 <= (n % 100) <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


_ESCALATE_WORDS = {"n", "no"}
_REACHED_WORDS = {"y", "yes"}
_RETRY_WORDS = {"r", "retry"}


def _norm_reply(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").strip().lower().lstrip("/"))


def _is_escalate(text: str) -> bool:
    return _norm_reply(text) in _ESCALATE_WORDS


def _is_reached(text: str) -> bool:
    return _norm_reply(text) in _REACHED_WORDS


def _is_retry(text: str) -> bool:
    return _norm_reply(text) in _RETRY_WORDS


def _reply(mid: str, token: str, text: str) -> None:
    if mid and token:
        _lark.post_text_reply_to_message(mid, token, text, reply_in_thread=True)


def _ring_contact(session_source: str, pair: Tuple[str, str], tenant_token: str, operator_open_id: str) -> str:
    _name, oid = pair
    if not oid:
        return "unresolved"
    return _vc_ring.invite_open_ids_into_active_meeting(
        session_source, [oid], tenant_token=tenant_token, operator_open_id=operator_open_id
    )


def _calling_prompt(mid: str, token: str, label: str, pairs: List[Tuple[str, str]], idx: int,
                    status: str, *, retry: bool = False) -> None:
    name, oid = pairs[idx]
    total = len(pairs)
    who = f'<at user_id="{oid}"></at>' if oid else name
    lead = "🔁 Retrying — calling" if retry else "📞 Calling"
    if not oid:
        head = (
            f"⚠️ {label} {_ordinal(idx + 1)}/{total} contact **{name}** is NOT in the OpenID directory "
            f"— can't ring. Add them (Name → open_id), then retry."
        )
    elif status == "no_session":
        head = "⚠️ No active meeting — start a P0 meeting first, then run this command."
    else:
        head = f"{lead} {who} ({_ordinal(idx + 1)}/{total} — {label}) into the meeting…"
    to = int(_timeout_sec())
    if idx + 1 < total:
        nxt = pairs[idx + 1][0]
        tail = (
            f"\nWaiting {to}s to join. If not: reply **/n** → {nxt} ({_ordinal(idx + 2)}), "
            f"**/r** to retry {name}, or **/y** if reached."
        )
    else:
        tail = f"\nWaiting {to}s (last contact). Reply **/r** to retry, or **/y** if reached."
    _reply(mid, token, head + tail)


def _cancel_timer(st: Dict[str, Any]) -> None:
    t = st.get("timer")
    if t is not None:
        try:
            t.cancel()
        except Exception:  # noqa: BLE001
            pass
        st["timer"] = None


def _arm_timeout(thread_key: str, idx: int) -> None:
    timer = threading.Timer(_timeout_sec(), _on_timeout, args=(thread_key, idx))
    timer.daemon = True
    with _ESC_LOCK:
        st = _ESC_BY_THREAD.get(thread_key)
        if not st:
            return
        _cancel_timer(st)
        st["timer"] = timer
    timer.start()


def _on_timeout(thread_key: str, idx: int) -> None:
    with _ESC_LOCK:
        st = _ESC_BY_THREAD.get(thread_key)
        if not st or st.get("reached") or st.get("idx") != idx:
            return  # already joined / advanced / stopped
        pairs = list(st["pairs"])
    token = _lark.get_tenant_token_primary()
    if not token:
        return
    name = pairs[idx][0]
    total = len(pairs)
    to = int(_timeout_sec())
    if idx + 1 < total:
        nxt = pairs[idx + 1][0]
        msg = (
            f"⏱️ {name} hasn't joined ({to}s). Reply **/n** to invite {nxt} ({_ordinal(idx + 2)}), "
            f"or **/r** to retry {name}."
        )
    else:
        msg = f"⏱️ {name} hasn't joined ({to}s) — last contact. Reply **/r** to retry, or **/y** to close."
    _reply(thread_key, token, msg)
    log.info("sre_game: timeout cmd=%s idx=%s name=%s (no join in %ss)", st.get("cmd"), idx, name, to)


def start_sre_game_escalation(
    cmd: str,
    session_source: str,
    notify_chat: str,
    token: str,
    *,
    command_message_id: str,
    thread_root: str = "",
    operator_open_id: str = "",
    tenant_token: str = "",
) -> None:
    """Ring the 1st contact for ``cmd``, open a thread escalation, and watch 90s for them to join."""
    if not _config.get_p0_vc_ring_enabled():
        log.info("sre_game: ignored (P0_VC_RING_ENABLED off) cmd=%s", (cmd or "").strip().lower())
        return
    c = (cmd or "").strip().lower()
    tok = (tenant_token or token or "").strip()
    label = SRE_GAME_LABEL.get(c, c.upper())
    thread_key = (thread_root or command_message_id or "").strip()
    pairs = resolve_sre_game_contacts(c, tok)
    if not pairs:
        _reply(command_message_id, token, f"⚠️ No {label} SRE contacts found in the 'SRE Game' section.")
        return
    status = _ring_contact(session_source, pairs[0], tok, operator_open_id)
    with _ESC_LOCK:
        _ESC_BY_THREAD[thread_key] = {
            "cmd": c, "label": label, "pairs": pairs, "idx": 0,
            "session_source": session_source, "notify_chat": notify_chat, "ts": time.time(),
            "awaiting_oid": pairs[0][1], "reached": False, "timer": None,
        }
    log.info(
        "sre_game: started cmd=%s label=%s contacts=%s thread_tail=%s status=%s",
        c, label, [p[0] for p in pairs], thread_key[-8:] if thread_key else "", status,
    )
    _calling_prompt(command_message_id, token, label, pairs, 0, status)
    if pairs[0][1] and status not in ("no_session", "disabled"):
        _arm_timeout(thread_key, 0)


def maybe_handle_sre_game_reply(
    thread_key: str,
    text: str,
    token: str,
    *,
    tenant_token: str = "",
    operator_open_id: str = "",
) -> bool:
    """Interpret a /y (reached), /n (escalate), or /r (retry) reply in an active escalation thread.
    Returns True only when it handled the message; anything else returns False so normal routing
    proceeds (never swallows unrelated chatter)."""
    key = (thread_key or "").strip()
    if not key:
        return False
    with _ESC_LOCK:
        st = _ESC_BY_THREAD.get(key)
        if st and time.time() - float(st.get("ts") or 0) > _ESC_TTL_SEC:
            _cancel_timer(st)
            _ESC_BY_THREAD.pop(key, None)
            st = None
    if not st:
        return False
    tok = (tenant_token or token or "").strip()

    if _is_reached(text):
        cur = st["pairs"][st["idx"]][0]
        with _ESC_LOCK:
            _cancel_timer(st)
            _ESC_BY_THREAD.pop(key, None)
        _reply(key, token, f"✅ Reached {cur} — {st['label']} escalation stopped.")
        return True

    if _is_retry(text):
        with _ESC_LOCK:
            _cancel_timer(st)
            idx = st["idx"]
            st["ts"] = time.time()
        status = _ring_contact(st["session_source"], st["pairs"][idx], tok, operator_open_id)
        log.info("sre_game: retry cmd=%s %s (%s) status=%s", st["cmd"], st["pairs"][idx][0], _ordinal(idx + 1), status)
        _calling_prompt(key, token, st["label"], st["pairs"], idx, status, retry=True)
        if st["pairs"][idx][1] and status not in ("no_session", "disabled"):
            _arm_timeout(key, idx)
        return True

    if _is_escalate(text):
        nxt = st["idx"] + 1
        if nxt >= len(st["pairs"]):
            with _ESC_LOCK:
                _cancel_timer(st)
                _ESC_BY_THREAD.pop(key, None)
            _reply(key, token, f"⚠️ No more {st['label']} contacts — end of the escalation list.")
            return True
        with _ESC_LOCK:
            _cancel_timer(st)
            st["idx"] = nxt
            st["awaiting_oid"] = st["pairs"][nxt][1]
            st["ts"] = time.time()
        status = _ring_contact(st["session_source"], st["pairs"][nxt], tok, operator_open_id)
        log.info("sre_game: escalated cmd=%s to %s (%s) status=%s", st["cmd"], st["pairs"][nxt][0], _ordinal(nxt + 1), status)
        _calling_prompt(key, token, st["label"], st["pairs"], nxt, status)
        if st["pairs"][nxt][1] and status not in ("no_session", "disabled"):
            _arm_timeout(key, nxt)
        return True

    return False  # not /y /n /r -> let normal routing handle this message


def maybe_mark_sre_game_contact_joined(joiner_open_id: str, tenant_token: str = "") -> None:
    """On a VC join, if the joiner is the contact an active escalation is waiting for, mark it reached
    (auto-stop) and cancel the timeout. Called from the VC join hook for every joiner."""
    oid = (joiner_open_id or "").strip()
    if not oid:
        return
    hit_key = ""
    hit_st: Dict[str, Any] = {}
    with _ESC_LOCK:
        for k, st in _ESC_BY_THREAD.items():
            if not st.get("reached") and str(st.get("awaiting_oid") or "") == oid:
                hit_key, hit_st = k, st
                break
        if hit_key:
            _cancel_timer(hit_st)
            _ESC_BY_THREAD.pop(hit_key, None)
    if not hit_key:
        return
    token = (tenant_token or "").strip() or _lark.get_tenant_token_primary()
    name = hit_st["pairs"][hit_st["idx"]][0]
    log.info("sre_game: contact joined cmd=%s name=%s — escalation done", hit_st.get("cmd"), name)
    _reply(hit_key, token, f"✅ {name} joined the meeting — {hit_st['label']} escalation done.")
