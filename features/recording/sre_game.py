"""SRE Game escalation ring — /srebac /srer /sredt /sresic /srebl /srepai /srecg /srepp /sredb /sreib.

The "SRE Game" section of the OSE & SRE Duty Shift sheet lists, per game, an ORDERED contact list
(1st, 2nd, 3rd… contact). There are NO per-day checkboxes here — the row ORDER is the escalation
priority. A command rings the 1st contact into the active P0 meeting and posts a thread reply asking
whether they were reached; a reply of "no" in that thread escalates to the next contact, and so on.

Flow (all inside the thread of the typed command):
    @bot /srebac  -> ring Wylie (1st) + "Na-reach? reply 'no' -> Chi Sheun (2nd)"
    reply "no"    -> ring Chi Sheun (2nd) + prompt for Wilfred (3rd)
    reply "yes"   -> stop (reached)

``sredt``/``sresic`` share one section ("Dragon Tiger & Sicbo"); ``srecg``/``srepp`` share
"Colorgame & Pulaputi". Contacts resolve name->open_id via the OpenID directory (so they are subject
to the same primary-app requirement as the other duty commands).
"""
from __future__ import annotations

import logging
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

# Ring command -> the game-header keyword to find WITHIN the "SRE Game" section (uppercased substring).
# dt/sic both land on "Dragon Tiger & Sicbo"; cg/pp both land on "Colorgame & Pulaputi".
SRE_GAME_HEADERS: Dict[str, str] = {
    "srebac": "BACCARAT",
    "srer": "ROULETTE",
    "sredt": "DRAGON TIGER",
    "sresic": "SICBO",
    "srebl": "BLACK JACK",
    "srepai": "PAIGOW",
    "srecg": "COLORGAME",
    "srepp": "PULAPUTI",
    "sredb": "DROPBALL",
    "sreib": "IN BETWEEN",
}
SRE_GAME_LABEL: Dict[str, str] = {
    "srebac": "Baccarat", "srer": "Roulette", "sredt": "Dragon Tiger", "sresic": "Sicbo",
    "srebl": "Blackjack", "srepai": "Paigow", "srecg": "Colorgame", "srepp": "Pulaputi",
    "sredb": "Dropball", "sreib": "In Between",
}
SRE_GAME_CMD_RE = re.compile(
    r"^(srebac|srer|sredt|sresic|srebl|srepai|srecg|srepp|sredb|sreib)$", re.IGNORECASE
)

# Any of these in col A marks a NEW game sub-header (so the previous game's contact list ends).
_GAME_HEADER_KWS = (
    "BACCARAT", "ROULETTE", "DRAGON TIGER", "SICBO", "BLACK JACK", "PAIGOW",
    "COLORGAME", "PULAPUTI", "DROPBALL", "IN BETWEEN",
)
# End of the whole "SRE Game" section — the next top-level section headers below it. LIVESLOT sits
# right after "In Between" (the last game), so it MUST be here or /sreib bleeds into the Liveslot rows.
_SECTION_ENDS = ("LIVESLOT", "EGAME", "IT TEAM", "SRE PLATFORM", "DBA", "BACKEND TEAM", "FRONTEND TEAM")

_ESC_LOCK = threading.Lock()
# thread_root_message_id -> {cmd, label, pairs:[(name, open_id)], idx, session_source, notify_chat, ts}
_ESC_BY_THREAD: Dict[str, Dict[str, Any]] = {}
_ESC_TTL_SEC = 7200.0


def is_sre_game_command(cmd: str) -> bool:
    return bool(SRE_GAME_CMD_RE.match((cmd or "").strip().lower()))


def _up(cell: Any) -> str:
    return re.sub(r"\s+", " ", str(cell if cell is not None else "").strip()).upper()


def parse_sre_game_contacts(rows: List[List[Any]], header_kw: str) -> List[str]:
    """Ordered contact NAMES for a game, read from the 'SRE Game' section of the duty-shift sheet.

    Scoped to the 'SRE Game' section so a game keyword that also appears in EGAME (e.g. 'COLORGAME'
    inside 'ColorGameSlot') is never matched. Skips the 'If can't contact…' note rows; stops at the
    next game sub-header, the section end, or a run of blank rows.
    """
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
            return []  # ran past the SRE Game section without finding the game
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
        if any(e in up for e in _SECTION_ENDS):
            break
        if any(e in up for e in _GAME_HEADER_KWS):
            break  # next game sub-header
        if "IF CAN'T CONTACT" in up or "IF CANT CONTACT" in up:
            continue  # instruction note
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
    names = parse_sre_game_contacts(rows, kw)
    pairs: List[Tuple[str, str]] = []
    for nm in names:
        pairs.append((nm, _dir.resolve_open_id_for_name(tenant_token, nm)))
    return pairs


def _ordinal(n: int) -> str:
    if 10 <= (n % 100) <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


_ESCALATE_WORDS = {"no", "n", "hindi", "di", "wala", "next", "escalate", "cant", "cannot", "x", "0"}
_REACHED_WORDS = {"yes", "y", "oo", "reached", "ok", "okay", "done", "nakontak", "nareach", "1", "stop"}


def _norm_reply(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (text or "").strip().lower()).strip()


def _is_escalate(text: str) -> bool:
    t = _norm_reply(text)
    return (
        t in _ESCALATE_WORDS
        or t.startswith("no ")
        or t.startswith("hindi")
        or t.startswith("wala")
        or t.startswith("cant")
        or t.startswith("cannot")
    )


def _is_reached(text: str) -> bool:
    t = _norm_reply(text)
    return t in _REACHED_WORDS or t.startswith("yes") or t.startswith("oo ")


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


def _prompt(mid: str, token: str, label: str, pairs: List[Tuple[str, str]], idx: int, ring_status: str) -> None:
    name, oid = pairs[idx]
    total = len(pairs)
    who = f'<at user_id="{oid}"></at>' if oid else name
    if not oid:
        head = (
            f"⚠️ {label} {_ordinal(idx + 1)}/{total} contact **{name}** is NOT in the OpenID directory "
            f"— can't ring. Add them (Name → open_id), then retry."
        )
    elif ring_status == "no_session":
        head = "⚠️ No active meeting — start a P0 meeting first, then run this command."
    else:
        head = f"📞 Calling {who} ({_ordinal(idx + 1)}/{total} — {label}) into the meeting now…"
    if idx + 1 < total:
        nxt = pairs[idx + 1][0]
        tail = f"\nNa-reach? Reply **no** to escalate → {nxt} ({_ordinal(idx + 2)}), or **yes** if reached."
    else:
        tail = "\n(Last contact — no further escalation.)"
    _reply(mid, token, head + tail)


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
    """Ring the 1st contact for ``cmd`` and open a thread-reply escalation under the command message."""
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
        }
    log.info(
        "sre_game: started cmd=%s label=%s contacts=%s thread_tail=%s status=%s",
        c, label, [p[0] for p in pairs], thread_key[-8:] if thread_key else "", status,
    )
    _prompt(command_message_id, token, label, pairs, 0, status)


def maybe_handle_sre_game_reply(
    thread_key: str,
    text: str,
    token: str,
    *,
    tenant_token: str = "",
    operator_open_id: str = "",
) -> bool:
    """If ``thread_key`` has an active SRE-game escalation, interpret a yes/no reply. Returns True only
    when it actually handled the message; a non-yes/no reply returns False so normal routing proceeds
    (never swallows unrelated chatter)."""
    key = (thread_key or "").strip()
    if not key:
        return False
    with _ESC_LOCK:
        st = _ESC_BY_THREAD.get(key)
    if not st:
        return False
    if time.time() - float(st.get("ts") or 0) > _ESC_TTL_SEC:
        with _ESC_LOCK:
            _ESC_BY_THREAD.pop(key, None)
        return False

    if _is_reached(text):
        cur = st["pairs"][st["idx"]][0]
        with _ESC_LOCK:
            _ESC_BY_THREAD.pop(key, None)
        _reply(key, token, f"✅ Reached {cur} — {st['label']} escalation stopped.")
        return True
    if not _is_escalate(text):
        return False  # not a yes/no -> let normal routing handle this message

    nxt = st["idx"] + 1
    if nxt >= len(st["pairs"]):
        with _ESC_LOCK:
            _ESC_BY_THREAD.pop(key, None)
        _reply(key, token, f"⚠️ No more {st['label']} contacts — end of the escalation list.")
        return True
    tok = (tenant_token or token or "").strip()
    status = _ring_contact(st["session_source"], st["pairs"][nxt], tok, operator_open_id)
    with _ESC_LOCK:
        st["idx"] = nxt
        st["ts"] = time.time()
    log.info(
        "sre_game: escalated cmd=%s to %s (%s) status=%s",
        st["cmd"], st["pairs"][nxt][0], _ordinal(nxt + 1), status,
    )
    _prompt(key, token, st["label"], st["pairs"], nxt, status)
    return True
