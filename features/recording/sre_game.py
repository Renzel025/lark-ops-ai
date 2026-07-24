"""SRE Game escalation ring — /srebac /srer /sredt /sresic /srebl /srepai /srecg /srepp /sredb /sreib.

The "SRE Game" section of the OSE & SRE Duty Shift sheet lists, per game, an ORDERED contact list
(1st, 2nd, 3rd… contact). No per-day checkboxes — the row ORDER is the priority. A command rings the
1st contact into the active P0 meeting, shows the full numbered roster, and WATCHES for joins:

    @bot /srebac  -> ring OSE (1st), show the Baccarat check-person list, wait 90s
      • OSE JOINS the VC     -> posts "OSE joined the meeting".
      • OSE does NOT join     -> after 90s, a "did not proceed to join" heads-up; use /c to call someone else.

Lark has no "invite declined/expired" event, so "did not accept" = "did not JOIN within the timeout".
In the command's thread, reply /c @checkperson to call another person from the list (retry the current
one or tag other specific people) — there is no /n stepping and no manual "reached" reply. When a
called contact JOINS the VC it posts "<name> joined the meeting". ``sredt``/``sresic`` share "Dragon
Tiger & Sicbo"; ``srecg``/``srepp`` share "Colorgame & Pulaputi".
Contacts resolve name->open_id via the OpenID directory (subject to the same primary-app requirement).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

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

# PO (Product-manager) game family — /po<game>, parallels /sre<game> but reads the SEPARATE
# "Game Issue Emergency Contact" sheet and rings that game's PRODUCT MANAGERS (1st/2nd/3rd PM columns).
# Token → the EXACT game name as written in that sheet's "Game" column.
# Fixed /po<game> shortcut -> the exact game name(s) in the Game Issue sheet. A few tokens cover TWO
# games (pogm = both Marble games; pogz = both Gamezone/Tongits lines) — their PMs are merged.
PO_GAME_HEADERS: Dict[str, List[str]] = {
    "pobac": ["Baccarat"], "pobt": ["Baccarat Tournament"], "por": ["Roulette"],
    "podt": ["Dragon Tiger"], "posic": ["Sic Bo"], "pobl": ["Black Jack"], "popai": ["Pai Gow"],
    "pocg": ["Color Game"], "popp": ["Pula Puti"], "podb": ["Drop Ball"], "poib": ["InBetween"],
    "poht": ["Hantak"], "poosm": ["OSM"], "poegs": ["EGS"], "poev": ["Evo Live Games"],
    "poez": ["EEZE Live Game"], "pogm": ["Marble Race: Las Vegas", "Marble 5vs5: Monaco"],
    "popt": ["Playtech Live Game"], "posb": ["SportBet/Ebet"],
    "pogz": ["Tongits Plus/ Texas Poker", "Tongits Joker / Pusoy Plus/ Lucky 9 Plus"],
}
PO_GAME_LABEL: Dict[str, str] = {
    "pobac": "Baccarat", "pobt": "Baccarat Tournament", "por": "Roulette", "podt": "Dragon Tiger",
    "posic": "Sic Bo", "pobl": "Black Jack", "popai": "Pai Gow", "pocg": "Color Game",
    "popp": "Pula Puti", "podb": "Drop Ball", "poib": "InBetween", "poht": "Hantak",
    "poosm": "OSM", "poegs": "EGS", "poev": "Evo Live Games", "poez": "EEZE Live Game",
    "pogm": "Marble (Las Vegas / Monaco)", "popt": "Playtech Live Game", "posb": "SportBet/Ebet",
    "pogz": "Gamezone (Tongits / Pusoy / Lucky 9)",
}
# Built from the keys so the token list stays in sync; ^…$ anchors so 'por' never matches 'posb'.
PO_GAME_CMD_RE = re.compile(r"^(?:" + "|".join(PO_GAME_HEADERS) + r")$", re.IGNORECASE)


def is_po_game_command(cmd: str) -> bool:
    return bool(PO_GAME_CMD_RE.match((cmd or "").strip().lower()))

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
        # Keep EVERY contact row in sheet order, INCLUDING repeats — the same person can legitimately be
        # both the 1st and a later fallback contact, and the escalation list must match the sheet 1:1.
        if nm:
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


# --------------------------------------------------------------------------------------------------
# EGAME escalation (/segame <game>) — a DIFFERENT section of the SAME OSE & SRE Duty Shift sheet.
# Layout per group: a games-header row (slash-separated game names) then the contact rows below it:
#   Maria Makilling/ Malakas/ Bakunawa/ …       <- games this group handles
#   Jin (60125855200)                           <- 1st contact
#   YK (60175245040)                            <- 2nd contact
# "/segame Bakunawa" rings the 1st contact of the group whose header lists that EXACT game token
# ("Bakunawa" != "Bakunawa 2"). Same escalation engine as /srebac (ring 1st, list, /c).
# --------------------------------------------------------------------------------------------------
_EGAME_SECTION_ENDS = ("SRE GAME", "LIVESLOT", "IT TEAM", "SRE PLATFORM", "DBA", "BACKEND TEAM", "FRONTEND TEAM")


def _egame_norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s if s is not None else "").strip().lower())


def _egame_squash(s: Any) -> str:
    """Normalized name with RUNS of the same char collapsed ('makilling' -> 'makiling'), so a
    doubled-letter typo still matches. Used only as a fallback + only when it's UNAMBIGUOUS."""
    return re.sub(r"(.)\1+", r"\1", _egame_norm(s))


def _egame_groups(rows: List[List[Any]]) -> List[Tuple[List[str], List[str]]]:
    """``[(game_display_names, contact_names), …]`` for each group under the 'EGAME' section (a
    slash-separated games-header row followed by its contact rows)."""
    start = -1
    for i, row in enumerate(rows):
        if _up(row[0] if row else "") == "EGAME":
            start = i
            break
    if start < 0:
        return []
    groups: List[Tuple[List[str], List[str]]] = []
    n = len(rows)
    i = start + 1
    while i < n:
        col_a = str((rows[i][0] if rows[i] else "") or "").strip()
        if col_a and any(e in _up(col_a) for e in _EGAME_SECTION_ENDS):
            break  # left the EGAME section
        if col_a and "/" in col_a:  # a games-header row
            display = [g.strip() for g in col_a.split("/") if g.strip()]
            names: List[str] = []
            blanks = 0
            j = i + 1
            while j < n:
                ca = str((rows[j][0] if rows[j] else "") or "").strip()
                if not ca:
                    blanks += 1
                    if blanks >= 2:
                        break
                    j += 1
                    continue
                blanks = 0
                if any(e in _up(ca) for e in _EGAME_SECTION_ENDS) or "/" in ca:
                    break  # next group / next section
                nm = _duty._clean_person_name(ca)
                if nm:
                    names.append(nm)
                j += 1
            groups.append((display, names))
            i = j
            continue
        i += 1
    return groups


def egame_game_names(rows: List[List[Any]]) -> List[str]:
    """All EGAME game names (display, in order) — shown as a hint when a typed name doesn't match."""
    out: List[str] = []
    for display, _names in _egame_groups(rows):
        out.extend(display)
    return out


def parse_egame_contacts(rows: List[List[Any]], game_name: str) -> List[str]:
    """Ordered contact NAMES for the e-game whose EGAME games-header lists ``game_name``. Match is
    case/space-insensitive and EXACT ('Bakunawa' != 'Bakunawa 2'); as a fallback a doubled-letter typo
    ('Maria Makiling' vs the sheet's 'Maria Makilling') is tolerated when it's UNAMBIGUOUS."""
    target = _egame_norm(game_name)
    if not target:
        return []
    groups = _egame_groups(rows)
    for display, names in groups:
        if target in {_egame_norm(g) for g in display}:
            return names
    tsq = _egame_squash(game_name)
    squashed = [names for display, names in groups if tsq in {_egame_squash(g) for g in display}]
    return squashed[0] if len(squashed) == 1 else []


def resolve_egame_contacts(game_name: str, tenant_token: str) -> List[Tuple[str, str]]:
    """Ordered ``[(name, open_id)]`` for an e-game (open_id '' when the name isn't in the directory)."""
    rows = _duty._read_shift_rows(tenant_token)
    if not rows:
        return []
    return [
        (nm, _dir.resolve_open_id_for_name(tenant_token, nm))
        for nm in parse_egame_contacts(rows, game_name)
    ]


# --------------------------------------------------------------------------------------------------
# PO game family (/po<game>) — reads the SEPARATE "Game Issue Emergency Contact" sheet (env
# DUTY_GAME_ISSUE_*) and rings a game's PRODUCT MANAGERS (the 1st/2nd/3rd Product-Manager columns).
# --------------------------------------------------------------------------------------------------
_PO_SECTION_END_KW = ("部门", "department", "person in cha")


def _game_issue_sheet_env() -> Tuple[str, str, str, str]:
    _config.reload_env_runtime()
    token = (os.getenv("DUTY_GAME_ISSUE_SHEET_TOKEN") or "").strip()
    sheet_id = (os.getenv("DUTY_GAME_ISSUE_SHEET_ID") or "").strip()
    sheet_name = (os.getenv("DUTY_GAME_ISSUE_SHEET_NAME") or "").strip()
    rng = (os.getenv("DUTY_GAME_ISSUE_RANGE") or "A1:AP60").strip()
    return token, sheet_id, sheet_name, rng


def _read_game_issue_rows(tenant_token: str) -> List[List[Any]]:
    token, sheet_id, sheet_name, rng = _game_issue_sheet_env()
    if not token:
        log.warning("po_game: DUTY_GAME_ISSUE_SHEET_TOKEN not set")
        return []
    if not sheet_id:
        sheet_id = _lark.resolve_sheet_id(tenant_token, token, sheet_name)
        if not sheet_id:
            log.warning("po_game: could not resolve game-issue sheet_id (share/permission?)")
            return []
    rows, err = _lark.read_sheets_values_batch(tenant_token, token, f"{sheet_id}!{rng}")
    if err or not rows:
        log.warning("po_game: game-issue read failed err=%s rows=%s", err, len(rows or []))
        return []
    return rows


def _strip_at_names(cell: Any) -> List[str]:
    """Names in a Sheets contact cell. Handles three shapes the values API returns:
      * plain text — '@Nelson C' (possibly several, newline-separated);
      * a Lark @-mention OBJECT — ``{'type':'mention','name':'OSE','en_name':'OSE','text':'@OSE',…}``
        (the Game Issue sheet stores contacts as real @-mentions, NOT text) -> take name/en_name;
      * a LIST of rich-text segments (mix of the above) -> flatten.
    Always returns clean display names with the leading '@' and inner whitespace normalised."""
    if isinstance(cell, list):
        out: List[str] = []
        for seg in cell:
            out.extend(_strip_at_names(seg))
        return out
    if isinstance(cell, dict):
        nm = str(cell.get("name") or cell.get("en_name") or str(cell.get("text") or "").lstrip("@")).strip()
        return [re.sub(r"\s+", " ", nm)] if nm else []
    names: List[str] = []
    for line in str(cell if cell is not None else "").replace("\r", "\n").split("\n"):
        nm = re.sub(r"\s+", " ", line.strip().lstrip("@").strip())
        if nm:
            names.append(nm)
    return names


def _po_header(rows: List[List[Any]]) -> Tuple[int, int, List[int]]:
    """``(header_row_idx, game_col, [pm_cols])`` for the Game Issue 'Emergency Contact' PM section — the
    first row with a 'Game' column AND one or more 'Product Manager' columns; ``(-1, -1, [])`` if none."""
    for i, row in enumerate(rows):
        norm = [_egame_norm(c) for c in (row or [])]
        gcol = -1
        pcs: List[int] = []
        for j, v in enumerate(norm):
            if gcol < 0 and "game" in v and "operat" not in v and "product" not in v:
                gcol = j
            if "product ma" in v:
                pcs.append(j)
        if gcol >= 0 and pcs:
            return i, gcol, pcs
    return -1, -1, []


def parse_po_game_managers(rows: List[List[Any]], game_name: str) -> List[str]:
    """Ordered PRODUCT-MANAGER names for ``game_name`` in the Game Issue Emergency Contact sheet: find
    the header row (a 'Game' column + one or more 'Product Manager' columns), then the row whose Game
    cell equals ``game_name`` (exact, case/space-insensitive), and return the PM columns' names (@
    stripped, deduped, in order). Stops at the next section header so only the PM section is used."""
    target = _egame_norm(game_name)
    if not target:
        return []
    hdr, game_col, pm_cols = _po_header(rows)
    if hdr < 0:
        return []
    for row in rows[hdr + 1:]:
        norm = [_egame_norm(c) for c in (row or [])]
        if any(any(kw in v for kw in _PO_SECTION_END_KW) for v in norm):
            break  # next section — stop (only the first, Product-Manager section counts)
        raw_gv = str((row[game_col] if game_col < len(row) else "") or "")
        # A game cell may hold SEVERAL games on separate lines (e.g. Gamezone) — match ANY line.
        game_lines = {_egame_norm(ln) for ln in raw_gv.replace("\r", "\n").split("\n") if ln.strip()}
        if target not in game_lines:
            continue
        names: List[str] = []
        for col in pm_cols:
            names.extend(_strip_at_names(row[col] if col < len(row) else ""))
        seen: set = set()
        out: List[str] = []
        for nm in names:
            k = _duty._norm_name(nm)
            if k and k not in seen:
                seen.add(k)
                out.append(nm)
        return out
    return []


def po_game_names(rows: List[List[Any]]) -> List[str]:
    """All game names in the Game Issue 'Emergency Contact' PM section (the Game column), in order —
    shown as a hint when a typed /po game name doesn't match."""
    hdr, game_col, _pm = _po_header(rows)
    if hdr < 0:
        return []
    out: List[str] = []
    for row in rows[hdr + 1:]:
        norm = [_egame_norm(c) for c in (row or [])]
        if any(any(kw in v for kw in _PO_SECTION_END_KW) for v in norm):
            break
        raw_gv = str((row[game_col] if game_col < len(row) else "") or "")
        for ln in raw_gv.replace("\r", "\n").split("\n"):  # multi-game cells (Gamezone) -> one per line
            ln = ln.strip()
            if ln:
                out.append(ln)
    return out


def resolve_po_game_contacts_by_name(game_name: str, tenant_token: str) -> List[Tuple[str, str]]:
    """Ordered ``[(name, open_id)]`` product managers for a game NAME (free-text ``/po <game>`` — works
    for ANY game in the sheet, incl. OSM / EGS / Marble Race / 'Baccarat Tournament' / etc.)."""
    rows = _read_game_issue_rows(tenant_token)
    if not rows:
        return []
    return [(nm, _dir.resolve_open_id_for_name(tenant_token, nm)) for nm in parse_po_game_managers(rows, game_name)]


def resolve_po_game_contacts(cmd: str, tenant_token: str) -> List[Tuple[str, str]]:
    """Ordered ``[(name, open_id)]`` product managers for a FIXED ``/po<game>`` token — MERGES the PMs
    of every game the token maps to (most map to one; pogm/pogz map to two), deduped, in order."""
    games = PO_GAME_HEADERS.get((cmd or "").strip().lower())
    if not games:
        return []
    rows = _read_game_issue_rows(tenant_token)
    if not rows:
        return []
    names: List[str] = []
    seen: set = set()
    for game in games:
        for nm in parse_po_game_managers(rows, game):
            k = _duty._norm_name(nm)
            if k and k not in seen:
                seen.add(k)
                names.append(nm)
    return [(nm, _dir.resolve_open_id_for_name(tenant_token, nm)) for nm in names]


def _ordinal(n: int) -> str:
    if 10 <= (n % 100) <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def _first_token(text: str) -> str:
    """Lowercased first word of the reply, sans a leading slash (e.g. '/c @A @B' -> 'c')."""
    t = (text or "").strip().lstrip("/").strip()
    return re.split(r"\s+", t, 1)[0].lower() if t else ""


def _is_call(text: str) -> bool:
    """``/c`` — call/invite the tagged check person(s) directly (retry the current one or bring in
    other specific people). Tags carry the open_ids; the word itself is just the trigger."""
    return _first_token(text) == "c"


def _reply(mid: str, token: str, text: str) -> Dict[str, str]:
    """Post a threaded reply to ``mid``; return the created message's {message_id, root_id, thread_id}
    so the escalation can be keyed by whatever thread root Lark actually assigns (it is NOT always the
    replied-to message — hence the multi-key registration)."""
    if not (mid and token and (text or "").strip()):
        return {}
    # Post the prompt as a clean interactive card (header + lark_md body) rather than plain text,
    # then parse the created message ids EXACTLY as before (required for multi-key registration).
    card = _cards.build_ring_status_card("Inviting check person", text)
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


def _reply_text(mid: str, token: str, text: str) -> None:
    """Plain-text threaded reply (no card). Used for the lightweight 'joined the meeting' confirmation."""
    if not (mid and token and (text or "").strip()):
        return
    _lark.post_text_reply_to_message(mid, token, text, reply_in_thread=True)


def _watch(state: Dict[str, Any], open_id: str, name: str) -> None:
    """Track ``open_id`` (→ display name) as a called check person, so their VC join is confirmed."""
    oid = (open_id or "").strip()
    if not oid:
        return
    w = state.setdefault("watched", {})
    w[oid] = (name or "").strip() or w.get(oid) or "The check person"


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
    # already invited (breaks /c re-calling the same person). The ring must actually re-fire.
    return _vc_ring.force_reinvite_open_ids(
        session_source, [oid], tenant_token=tenant_token, operator_open_id=operator_open_id
    )


def _contacts_list_md(header: str, pairs: List[Tuple[str, str]]) -> str:
    """Numbered roster of ALL check persons for the game, under the BOLD ``header`` title line."""
    lines = [f"**{header}**"]
    for i, (nm, _oid) in enumerate(pairs):
        lines.append(f"{i + 1}. {nm}")
    return "\n".join(lines)


def _command_hints_md() -> List[str]:
    """The '/c' command hint under the roster — same wording/style as the ring card's note."""
    return [
        "",
        "**commands**",
        "**/c @name** — use this if you want to contact someone else in the provided list",
    ]


def _calling_prompt(mid: str, token: str, label: str, pairs: List[Tuple[str, str]], idx: int,
                    status: str, *, roster_header: str) -> Dict[str, str]:
    """Post the 'Calling <1st contact>' card WITH the numbered check-person roster (titled
    ``roster_header``) + the /c hint, and return the created message ids for thread registration."""
    name, oid = pairs[idx]
    # Card lark_md mention form is <at id=ou_xxx></at> (NOT the text-message <at user_id="...">).
    who = f'<at id={oid}></at>' if oid else name
    if not oid:
        head = (
            f"{name} ({_ordinal(idx + 1)} check person {label}) is NOT in the OpenID directory "
            f"— can't ring. Add them (Name → open_id), then retry."
        )
    elif status == "no_session":
        head = "No active meeting — start a P0 meeting first, then run this command."
    else:
        head = f"Calling {who} ({_ordinal(idx + 1)} check person **{label}**) into the meeting"
    if oid and status not in ("no_session",):
        body = "\n\n" + _contacts_list_md(roster_header, pairs) + "\n" + "\n".join(_command_hints_md())
    else:
        body = ""
    return _reply(mid, token, head + body)


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
    to = int(_timeout_sec())
    # Short, plain-text one-liner — no card header, no roster/command guide (the operator already saw
    # the guide on the first /srebac call; repeating it every timeout is too noisy).
    head = (
        f"{name} ({_ordinal(idx + 1)} check person {label}) did not proceed to join the meeting "
        f"(no answer within {to}s)."
    )
    _reply_text(thread_key, token, head)
    log.info("sre_game: timeout cmd=%s idx=%s name=%s (no join in %ss)", st.get("cmd"), idx, name, to)


def _begin_escalation(
    esc_key: str,
    label: str,
    roster_header: str,
    pairs: List[Tuple[str, str]],
    session_source: str,
    notify_chat: str,
    token: str,
    *,
    command_message_id: str,
    thread_root: str,
    operator_open_id: str,
    tok: str,
) -> None:
    """Shared escalation start: retire any prior escalation for the SAME chat + ``esc_key``, ring the
    1st contact, post the card (roster titled ``roster_header``), register the thread keys, and arm the
    90s no-join timeout. Used by both /srebac (SRE Game) and /segame (EGAME)."""
    with _ESC_LOCK:
        _seen: set = set()
        prior = []
        for st in _ESC_BY_THREAD.values():
            if id(st) in _seen:
                continue
            _seen.add(id(st))
            if st.get("session_source") == session_source and st.get("cmd") == esc_key:
                prior.append(st)
    for st in prior:
        _pop_state(st)
    primary = (command_message_id or thread_root or "").strip()
    state: Dict[str, Any] = {
        "cmd": esc_key, "label": label, "pairs": pairs, "idx": 0,
        "session_source": session_source, "notify_chat": notify_chat, "ts": time.time(),
        "awaiting_oid": pairs[0][1], "reached": False, "timer": None, "primary": primary, "_keys": [],
        # Every open_id we've called (1st contact + any /c) mapped to a display name, so a VC join by
        # ANY of them — not just the current contact — posts "<name> joined the meeting".
        "watched": {},
    }
    _watch(state, pairs[0][1], pairs[0][0])
    status = _ring_contact(session_source, pairs[0], tok, operator_open_id)
    ids = _calling_prompt(command_message_id, token, label, pairs, 0, status, roster_header=roster_header)
    # Register under the command message, its root, AND the bot-reply's message/root/thread id — Lark's
    # thread root is NOT always the command message, so a /c reply may carry any of these as its root.
    _register(state, [command_message_id, thread_root,
                      ids.get("message_id", ""), ids.get("root_id", ""), ids.get("thread_id", "")])
    log.info(
        "sre_game: started key=%s label=%s contacts=%s keys=%s status=%s",
        esc_key, label, [p[0] for p in pairs], [k[-8:] for k in state["_keys"]], status,
    )
    if pairs[0][1] and status not in ("no_session", "disabled") and primary:
        _arm_timeout(primary, 0)


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
    """Ring the 1st contact for ``cmd`` (SRE Game section), open a thread escalation, watch 90s to join."""
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
    _begin_escalation(
        c, label, f"SRE {label} check persons", pairs, session_source, notify_chat, token,
        command_message_id=command_message_id, thread_root=thread_root,
        operator_open_id=operator_open_id, tok=tok,
    )


def start_egame_escalation(
    game_name: str,
    session_source: str,
    notify_chat: str,
    token: str,
    *,
    command_message_id: str,
    thread_root: str = "",
    operator_open_id: str = "",
    tenant_token: str = "",
) -> None:
    """Ring the 1st contact handling ``game_name`` in the EGAME section (e.g. /segame Bakunawa); same
    escalation engine as /srebac (ring 1st, list, /c). Game match is EXACT ('Bakunawa' != 'Bakunawa 2')."""
    if not _config.get_p0_vc_ring_enabled():
        log.info("sre_game: egame ignored (P0_VC_RING_ENABLED off) game=%r", game_name)
        return
    g = (game_name or "").strip()
    tok = (tenant_token or token or "").strip()
    if not g:
        _reply(command_message_id, token, "Usage: /segame <game>, e.g. /segame Bakunawa")
        return
    pairs = resolve_egame_contacts(g, tok)
    if not pairs:
        rows = _duty._read_shift_rows(tok)
        avail = egame_game_names(rows) if rows else []
        hint = (" Available games: " + ", ".join(avail) + ".") if avail else " Check the 'EGAME' section."
        _reply(command_message_id, token, f"No EGAME contacts found for '{g}'.{hint}")
        return
    _begin_escalation(
        f"egame:{_egame_norm(g)}", g, f"{g} (EGAME) check persons", pairs, session_source, notify_chat,
        token, command_message_id=command_message_id, thread_root=thread_root,
        operator_open_id=operator_open_id, tok=tok,
    )


def start_po_game_escalation(
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
    """Ring the 1st PRODUCT MANAGER for ``cmd`` (/po<game>, e.g. /pobac) from the Game Issue Emergency
    Contact sheet; same escalation engine as /srebac (ring 1st, list, /c)."""
    if not _config.get_p0_vc_ring_enabled():
        log.info("po_game: ignored (P0_VC_RING_ENABLED off) cmd=%s", (cmd or "").strip().lower())
        return
    c = (cmd or "").strip().lower()
    tok = (tenant_token or token or "").strip()
    label = PO_GAME_LABEL.get(c, c.upper())
    pairs = resolve_po_game_contacts(c, tok)
    if not pairs:
        _reply(
            command_message_id, token,
            f"No {label} product managers found in the Game Issue Emergency Contact sheet.",
        )
        return
    _begin_escalation(
        f"po:{c}", label, f"{label} product managers", pairs, session_source, notify_chat, token,
        command_message_id=command_message_id, thread_root=thread_root,
        operator_open_id=operator_open_id, tok=tok,
    )


def start_po_game_escalation_by_name(
    game_name: str,
    session_source: str,
    notify_chat: str,
    token: str,
    *,
    command_message_id: str,
    thread_root: str = "",
    operator_open_id: str = "",
    tenant_token: str = "",
) -> None:
    """Ring the 1st PRODUCT MANAGER for a FREE-TEXT game name (``/po <game>``) — covers ANY game in the
    Game Issue sheet, incl. those without a fixed /po<game> token (Hantak, OSM, EGS, Marble Race,
    'Baccarat Tournament', …). Same escalation engine as /srebac."""
    if not _config.get_p0_vc_ring_enabled():
        log.info("po_game: by-name ignored (P0_VC_RING_ENABLED off) game=%r", game_name)
        return
    g = (game_name or "").strip()
    tok = (tenant_token or token or "").strip()
    if not g:
        _reply(command_message_id, token, "Usage: /po <game>, e.g. /po Baccarat")
        return
    pairs = resolve_po_game_contacts_by_name(g, tok)
    if not pairs:
        rows = _read_game_issue_rows(tok)
        avail = po_game_names(rows) if rows else []
        hint = (" Available games: " + ", ".join(avail) + ".") if avail else " Check the Game Issue Emergency Contact sheet."
        _reply(command_message_id, token, f"No product managers found for '{g}'.{hint}")
        return
    _begin_escalation(
        f"po:{_egame_norm(g)}", g, f"{g} product managers", pairs, session_source, notify_chat, token,
        command_message_id=command_message_id, thread_root=thread_root,
        operator_open_id=operator_open_id, tok=tok,
    )


def maybe_handle_sre_game_reply(
    thread_keys: List[str],
    text: str,
    token: str,
    *,
    tenant_token: str = "",
    operator_open_id: str = "",
    tagged_open_ids: Optional[List[str]] = None,
) -> bool:
    """Interpret a /c (call tagged check persons) reply in an active escalation thread. A VC join
    CONFIRMS "<name> joined the meeting" — the operator uses /c to page anyone else from the list.
    ``thread_keys`` = the reply's candidate identifiers (root_id, parent_id, thread_id); the state is
    matched against ANY of them. ``tagged_open_ids`` = open_ids of the Lark users @mentioned in the
    reply (the /c targets). Returns True only when it handled the message; anything else returns False
    so normal routing proceeds (never swallows unrelated chatter)."""
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

    if _is_call(text):
        # /c @people — force-invite the tagged check person(s) into the meeting (retry the current one
        # or bring in other specific people from the list). force_reinvite bypasses the merge-dedupe so
        # an already-invited person actually rings again.
        tagged = [t for t in (tagged_open_ids or []) if t]
        if not tagged:
            _reply_text(primary, token, "Tag the check person(s) to call, e.g. /c @Name")
            return True
        status = _vc_ring.force_reinvite_open_ids(
            st["session_source"], tagged, tenant_token=tok, operator_open_id=operator_open_id
        )
        log.info("sre_game: /c cmd=%s tagged=%s status=%s", st["cmd"], len(tagged), status)
        if status in ("no_session",):
            _reply_text(primary, token, "No active meeting — start a P0 meeting first.")
        else:
            # Watch each tagged person for a VC join (name from the roster, else a directory lookup),
            # so "<name> joined the meeting" fires even for a /c-invited contact.
            pair_names = {oid: nm for nm, oid in st["pairs"] if oid}
            for t in tagged:
                nm = pair_names.get(t) or _lark.lookup_user_name_by_open_id(tok, t)
                _watch(st, t, nm)
            who = " ".join(f"<at id={t}></at>" for t in tagged)
            _reply_text(primary, token, f"Calling {who} into the meeting")
        return True

    return False  # not /c -> let normal routing handle this message


def maybe_mark_sre_game_contact_joined(joiner_open_id: str, tenant_token: str = "") -> None:
    """On a VC join, if the joiner is ANY check person an active escalation has called (the 1st contact
    or a later /c invite — tracked in ``watched``), post "<name> joined the meeting". It only confirms
    that person joined (and cancels the pending timeout when the current awaited contact is the one who
    joined) — the operator can keep paging more people with /c. Called from the VC join hook per joiner."""
    oid = (joiner_open_id or "").strip()
    if not oid:
        return
    hit_st: Dict[str, Any] = {}
    name = ""
    with _ESC_LOCK:
        # Pick the MOST RECENT escalation that called this joiner (the current thread has the newest ts).
        best_ts = -1.0
        seen_ids: set = set()
        for st in _ESC_BY_THREAD.values():
            if id(st) in seen_ids:
                continue
            seen_ids.add(id(st))
            if oid not in (st.get("watched") or {}):
                continue
            ts = float(st.get("ts") or 0)
            if ts > best_ts:
                best_ts = ts
                hit_st = st
        if hit_st:
            # Confirm this person once (drop from watched so a re-join doesn't double-post), but keep the
            # escalation alive so /c can still reach the remaining contacts.
            name = (hit_st.get("watched") or {}).pop(oid, "") or "The check person"
            if str(hit_st.get("awaiting_oid") or "") == oid:
                _cancel_timer(hit_st)
    if not hit_st:
        return
    token = (tenant_token or "").strip() or _lark.get_tenant_token_primary()
    primary = str(hit_st.get("primary") or "").strip()
    log.info("sre_game: contact joined cmd=%s name=%s (escalation stays active)", hit_st.get("cmd"), name)
    _reply_text(primary, token, f"{name} joined the meeting")
