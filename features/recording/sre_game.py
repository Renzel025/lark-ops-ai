"""SRE Game escalation ring — /srebac /srer /sredt /sresic /srebl /srepai /srecg /srepp /sredb /sreib.

The "SRE Game" section of the OSE & SRE Duty Shift sheet lists, per game, an ORDERED contact list
(1st, 2nd, 3rd… contact). No per-day checkboxes — the row ORDER is the escalation priority. A command
rings the 1st contact into the active P0 meeting, then WATCHES for them to join:

    @bot /srebac  -> ring Wylie (1st), wait 90s
      • Wylie JOINS the VC within 90s -> reached, stop (auto) + posts "Wylie joined the meeting".
      • Wylie does NOT join in 90s     -> prompt: reply /n to invite Chi Sheun (2nd), /r to retry Wylie.

Lark has no "invite declined/expired" event, so "did not accept" = "did not JOIN within the timeout".
Replies in the command's thread: /n = escalate to next, /r = retry current. When the contact actually
JOINS the VC, the escalation auto-stops and posts "<name> joined the meeting" — there is no manual
"reached" reply. ``sredt``/``sresic`` share "Dragon Tiger & Sicbo"; ``srecg``/``srepp`` share "Colorgame & Pulaputi".
Contacts resolve name->open_id via the OpenID directory (subject to the same primary-app requirement).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Tuple

from p0_logic import config as _config
from p0_logic import lark_client as _lark
from p0_logic import cards as _cards
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
_RETRY_WORDS = {"r", "retry"}


def _norm_reply(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").strip().lower().lstrip("/"))


def _is_escalate(text: str) -> bool:
    return _norm_reply(text) in _ESCALATE_WORDS


def _is_retry(text: str) -> bool:
    return _norm_reply(text) in _RETRY_WORDS


def _reply(mid: str, token: str, text: str) -> Dict[str, str]:
    """Post a threaded reply to ``mid``; return the created message's {message_id, root_id, thread_id}
    so the escalation can be keyed by whatever thread root Lark actually assigns (it is NOT always the
    replied-to message — hence the multi-key registration)."""
    if not (mid and token and (text or "").strip()):
        return {}
    # Post the prompt as a clean interactive card (header + lark_md body) rather than plain text,
    # then parse the created message ids EXACTLY as before (required for multi-key registration).
    card = _cards.build_ring_status_card("SRE duty", text)
    st, body = _lark.post_card_reply_to_message(mid, token, card, reply_in_thread=True)
    ids: Dict[str, str] = {}
    if st == 200 and body:
        try:
            data = (json.loads(body) or {}).get("data") or {}
            for k in ("message_id", "root_id", "thread_id"):
                v = str(data.get(k) or "").strip()
                if v:
                    ids[k] = v
        except (ValueError, TypeError):
            pass
    return ids


def _register(state: Dict[str, Any], keys: List[str]) -> None:
    """Register ``state`` under every non-empty key (deduped) so a reply matching ANY of the thread's
    identifiers (command msg id / bot-reply msg id / root id / thread id) resolves to it."""
    ks = sorted({k.strip() for k in keys if k and k.strip()})
    state["_keys"] = ks
    with _ESC_LOCK:
        for k in ks:
            _ESC_BY_THREAD[k] = state


def _pop_state(state: Dict[str, Any]) -> None:
    """Cancel the timer and remove ``state`` under all of its registered keys."""
    with _ESC_LOCK:
        _cancel_timer(state)
        for k in state.get("_keys", []):
            if _ESC_BY_THREAD.get(k) is state:
                _ESC_BY_THREAD.pop(k, None)


def _ring_contact(session_source: str, pair: Tuple[str, str], tenant_token: str, operator_open_id: str) -> str:
    _name, oid = pair
    if not oid:
        return "unresolved"
    # Force a direct re-invite each step: the normal merge path dedupes and would NOT re-ring a contact
    # already invited (breaks /r retry and re-calling the same person). Escalation must actually ring.
    return _vc_ring.force_reinvite_open_ids(
        session_source, [oid], tenant_token=tenant_token, operator_open_id=operator_open_id
    )


def _calling_prompt(mid: str, token: str, label: str, pairs: List[Tuple[str, str]], idx: int,
                    status: str, *, retry: bool = False) -> Dict[str, str]:
    name, oid = pairs[idx]
    total = len(pairs)
    # Card lark_md mention form is <at id=ou_xxx></at> (NOT the text-message <at user_id="...">).
    who = f'<at id={oid}></at>' if oid else name
    lead = "Retrying — calling" if retry else "Calling"
    if not oid:
        head = (
            f"{label} {_ordinal(idx + 1)}/{total} contact {name} is NOT in the OpenID directory "
            f"— can't ring. Add them (Name → open_id), then retry."
        )
    elif status == "no_session":
        head = "No active meeting — start a P0 meeting first, then run this command."
    else:
        head = f"{lead} {who} ({_ordinal(idx + 1)}/{total} — {label}) into the meeting…"
    to = int(_timeout_sec())
    if oid and status not in ("no_session",):
        opts = []
        if idx + 1 < total:
            opts.append(f"/n to call the next contact ({pairs[idx + 1][0]}) now")
        opts.append("/r to retry")
        # Lark sends no "declined" event, so we auto-ask after the timeout — but the operator can act
        # IMMEDIATELY (e.g. the moment they see the call declined) by replying one of these. When the
        # contact joins the VC, the escalation auto-stops — no manual "reached" reply is needed.
        tail = (
            f"\nWaiting up to {to}s for them to accept — I'll auto-confirm when they join the meeting."
            f"\nDeclined or can't reach them? Reply " + ", ".join(opts) + " — I'll also ask automatically after {}s.".format(to)
        )
    else:
        tail = ""
    return _reply(mid, token, head + tail)


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
        label = str(st.get("label") or "")
    token = _lark.get_tenant_token_primary()
    if not token:
        return
    name = pairs[idx][0]
    total = len(pairs)
    to = int(_timeout_sec())
    lines = [
        f"{label} — {_ordinal(idx + 1)} contact {name} did not accept the invite "
        f"(declined or no answer within {to}s).",
        "",
        "What next?",
    ]
    if idx + 1 < total:
        lines.append(f"• /n → proceed to the next contact: {pairs[idx + 1][0]} ({_ordinal(idx + 2)})")
    else:
        lines.append("• (no more contacts after this one)")
    lines.append(f"• /r → retry {name} ({_ordinal(idx + 1)})")
    lines.append("(If they join the meeting, I'll auto-confirm and stop — no reply needed.)")
    _reply(thread_key, token, "\n".join(lines))
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
    pairs = resolve_sre_game_contacts(c, tok)
    if not pairs:
        _reply(command_message_id, token, f"No {label} SRE contacts found in the 'SRE Game' section.")
        return
    # Retire any prior escalation for the SAME chat + game — a repeated /srebac supersedes the old one,
    # so stale states don't pile up and steal the join-detect / thread replies.
    with _ESC_LOCK:
        _seen: set = set()
        prior = []
        for st in _ESC_BY_THREAD.values():
            if id(st) in _seen:
                continue
            _seen.add(id(st))
            if st.get("session_source") == session_source and st.get("cmd") == c:
                prior.append(st)
    for st in prior:
        _pop_state(st)
    primary = (command_message_id or thread_root or "").strip()
    state: Dict[str, Any] = {
        "cmd": c, "label": label, "pairs": pairs, "idx": 0,
        "session_source": session_source, "notify_chat": notify_chat, "ts": time.time(),
        "awaiting_oid": pairs[0][1], "reached": False, "timer": None, "primary": primary, "_keys": [],
    }
    status = _ring_contact(session_source, pairs[0], tok, operator_open_id)
    ids = _calling_prompt(command_message_id, token, label, pairs, 0, status)
    # Register under the command message, its root, AND the bot-reply's message/root/thread id — Lark's
    # thread root is NOT always the command message, so a /n reply may carry any of these as its root.
    _register(state, [command_message_id, thread_root,
                      ids.get("message_id", ""), ids.get("root_id", ""), ids.get("thread_id", "")])
    log.info(
        "sre_game: started cmd=%s label=%s contacts=%s keys=%s status=%s",
        c, label, [p[0] for p in pairs], [k[-8:] for k in state["_keys"]], status,
    )
    if pairs[0][1] and status not in ("no_session", "disabled") and primary:
        _arm_timeout(primary, 0)


def maybe_handle_sre_game_reply(
    thread_keys: List[str],
    text: str,
    token: str,
    *,
    tenant_token: str = "",
    operator_open_id: str = "",
) -> bool:
    """Interpret a /n (escalate) or /r (retry) reply in an active escalation thread. (There is no manual
    "reached" reply — the escalation auto-stops when the contact joins the VC.) ``thread_keys`` = the
    reply's candidate identifiers (root_id, parent_id, thread_id); the state is matched against ANY of
    them. Returns True only when it handled the message; anything else returns False so normal routing
    proceeds (never swallows unrelated chatter)."""
    keys = [k.strip() for k in (thread_keys or []) if k and k.strip()]
    if not keys:
        return False
    with _ESC_LOCK:
        active = list(_ESC_BY_THREAD.keys())
        st = None
        for k in keys:
            st = _ESC_BY_THREAD.get(k)
            if st:
                break
        if st and time.time() - float(st.get("ts") or 0) > _ESC_TTL_SEC:
            _pop_state(st)
            st = None
    if active:  # only log when an escalation is actually active (silent in prod / ring-off)
        log.info(
            "sre_game: reply keys=%s text=%r matched=%s active_tails=%s",
            [k[-8:] for k in keys], text, bool(st), [k[-8:] for k in active],
        )
    if not st:
        return False
    tok = (tenant_token or token or "").strip()
    primary = str(st.get("primary") or keys[0]).strip()

    if _is_retry(text):
        with _ESC_LOCK:
            _cancel_timer(st)
            idx = st["idx"]
            st["ts"] = time.time()
        status = _ring_contact(st["session_source"], st["pairs"][idx], tok, operator_open_id)
        log.info("sre_game: retry cmd=%s %s (%s) status=%s", st["cmd"], st["pairs"][idx][0], _ordinal(idx + 1), status)
        _calling_prompt(primary, token, st["label"], st["pairs"], idx, status, retry=True)
        if st["pairs"][idx][1] and status not in ("no_session", "disabled"):
            _arm_timeout(primary, idx)
        return True

    if _is_escalate(text):
        nxt = st["idx"] + 1
        if nxt >= len(st["pairs"]):
            _pop_state(st)
            _reply(primary, token, f"No more {st['label']} contacts — end of the escalation list.")
            return True
        with _ESC_LOCK:
            _cancel_timer(st)
            st["idx"] = nxt
            st["awaiting_oid"] = st["pairs"][nxt][1]
            st["ts"] = time.time()
        status = _ring_contact(st["session_source"], st["pairs"][nxt], tok, operator_open_id)
        log.info("sre_game: escalated cmd=%s to %s (%s) status=%s", st["cmd"], st["pairs"][nxt][0], _ordinal(nxt + 1), status)
        _calling_prompt(primary, token, st["label"], st["pairs"], nxt, status)
        if st["pairs"][nxt][1] and status not in ("no_session", "disabled"):
            _arm_timeout(primary, nxt)
        return True

    return False  # not /n /r -> let normal routing handle this message


def maybe_mark_sre_game_contact_joined(joiner_open_id: str, tenant_token: str = "") -> None:
    """On a VC join, if the joiner is the contact an active escalation is waiting for, mark it reached
    (auto-stop) and cancel the timeout. Called from the VC join hook for every joiner."""
    oid = (joiner_open_id or "").strip()
    if not oid:
        return
    hit_st: Dict[str, Any] = {}
    with _ESC_LOCK:
        # Pick the MOST RECENT escalation awaiting this joiner (many may await the same person across
        # repeated commands; the current thread has the newest ts) — not just the first one found.
        best_ts = -1.0
        seen_ids: set = set()
        for st in _ESC_BY_THREAD.values():
            if id(st) in seen_ids:
                continue
            seen_ids.add(id(st))
            if st.get("reached") or str(st.get("awaiting_oid") or "") != oid:
                continue
            ts = float(st.get("ts") or 0)
            if ts > best_ts:
                best_ts = ts
                hit_st = st
        if hit_st:
            hit_st["reached"] = True
            _pop_state(hit_st)
    if not hit_st:
        return
    token = (tenant_token or "").strip() or _lark.get_tenant_token_primary()
    name = hit_st["pairs"][hit_st["idx"]][0]
    primary = str(hit_st.get("primary") or "").strip()
    log.info("sre_game: contact joined cmd=%s name=%s — escalation done", hit_st.get("cmd"), name)
    _reply(primary, token, f"{name} joined the meeting — {hit_st['label']} escalation done.")
