"""Duty directory: a person's NAME (as written in the roster sheets) -> Lark ``open_id``.

The roster sheets (Frontend / PMS / FPMS / OSE) carry names + phones but no ``open_id``, and the bot
needs an ``open_id`` to invite + ring a person in VC. This reads ONE small directory sheet that maps
names to ids so the flow is: **duty sheet -> name -> directory -> open_id -> ring**.

Expected directory sheet (a header row + one row per person). Fill EITHER ``open_id`` OR ``email``
per person; ``open_id`` wins when both are present:

    Name        | open_id | email
    Bryan       | ou_xxx  |
    Ramel       |         | ramel@company.com
    Guan Zhong  | ou_yyy  |

For rows with only an email, the email is resolved to ``open_id`` via ``batch_get_id`` (needs the
``contact:user.id:readonly`` scope). Result is TTL-cached so a P0 ring does not re-read every time.

Env (mirrors ``p0_logic/support.py``; the sheet_id is the ``?sheet=`` value in the sheet URL):
  DUTY_DIRECTORY_SHEET_TOKEN   spreadsheet_token
  DUTY_DIRECTORY_SHEET_ID      sheet_id (the ?sheet= in the URL)
  DUTY_DIRECTORY_RANGE         A1 range, default "A:C"
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Set, Tuple

from p0_logic import config as _config
from p0_logic import lark_client as _lark

log = logging.getLogger("lark-ops-ai")

_DIR_CACHE: Dict[str, str] = {}
_DIR_CACHE_TS = 0.0
_DIR_LOCK = threading.RLock()
_DIR_TTL_SEC = 300.0

# SRE handler tab (Name | Handler): {normalized_name: {team_token, ...}}, TTL-cached like the directory.
_SRE_CACHE: Dict[str, Set[str]] = {}
_SRE_CACHE_TS = 0.0
_SRE_LOCK = threading.RLock()

# Name-alias tab (SHEET NAME -> REAL NAME): {normalized_sheet_name: real_name}, TTL-cached. Maps roster
# shortcut names to the real name used in the OpenID directory. Empty when unconfigured.
_ALIAS_CACHE: Dict[str, str] = {}
_ALIAS_CACHE_TS = 0.0
_ALIAS_LOADED = False
_ALIAS_LOCK = threading.RLock()


def normalize_name(name: str) -> str:
    """Case/space-insensitive key so 'Guan  Zhong' and 'guan zhong' match the same row."""
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _cfg() -> Tuple[str, str, str, str]:
    _config.reload_env_runtime()
    token = (os.getenv("DUTY_DIRECTORY_SHEET_TOKEN") or "").strip()
    sheet_id = (os.getenv("DUTY_DIRECTORY_SHEET_ID") or "").strip()
    sheet_name = (os.getenv("DUTY_DIRECTORY_SHEET_NAME") or "").strip()
    rng = (os.getenv("DUTY_DIRECTORY_RANGE") or "A:C").strip()
    return token, sheet_id, sheet_name, rng


def _col_index(header: list, *names: str) -> int:
    want = {n.lower() for n in names}
    for i, h in enumerate(header):
        if str(h or "").strip().lower() in want:
            return i
    return -1


def _fetch_directory(tenant_token: str) -> Dict[str, str]:
    token, sheet_id, sheet_name, rng = _cfg()
    if not token:
        log.warning("duty_directory: DUTY_DIRECTORY_SHEET_TOKEN not set")
        return {}
    if not sheet_id:
        # Single-tab sheet: URL has no ?sheet= — resolve the sheet_id (by name, else first sheet).
        sheet_id = _lark.resolve_sheet_id(tenant_token, token, sheet_name)
        if not sheet_id:
            log.warning("duty_directory: could not resolve sheet_id (token/share/permission?)")
            return {}
    rows, err = _lark.read_sheets_values_batch(tenant_token, token, f"{sheet_id}!{rng}")
    if err or not rows:
        log.warning("duty_directory: read failed err=%s rows=%s", err, len(rows or []))
        return {}
    header = list(rows[0] or [])
    ci_name = _col_index(header, "name")
    ci_oid = _col_index(header, "open_id", "openid", "ou_id", "ou id")
    ci_email = _col_index(header, "email", "e-mail")
    if ci_name < 0:
        log.warning("duty_directory: no 'Name' column in header=%r", [str(h) for h in header])
        return {}

    name_open_id: Dict[str, str] = {}
    pending_email: Dict[str, str] = {}  # name -> email (resolve in one batch)
    for r in rows[1:]:
        cells = [str(c or "").strip() for c in (r or [])]

        def g(i: int) -> str:
            return cells[i] if 0 <= i < len(cells) else ""

        nm = normalize_name(g(ci_name))
        if not nm:
            continue
        oid = g(ci_oid) if ci_oid >= 0 else ""
        if oid.startswith("ou_"):
            name_open_id[nm] = oid
            continue
        em = g(ci_email) if ci_email >= 0 else ""
        if em:
            pending_email[nm] = em

    if pending_email:
        resolved = _lark.batch_get_id_by_email(tenant_token, list(set(pending_email.values())))
        for nm, em in pending_email.items():
            oid = resolved.get(em)
            if oid:
                name_open_id[nm] = oid
            else:
                log.warning("duty_directory: could not resolve email for name=%r", nm)

    log.info("duty_directory: loaded %s name->open_id entries", len(name_open_id))
    return name_open_id


def get_directory(tenant_token: str) -> Dict[str, str]:
    """``{normalized_name: open_id}`` for the directory sheet, TTL-cached."""
    global _DIR_CACHE, _DIR_CACHE_TS
    now = time.time()
    with _DIR_LOCK:
        if _DIR_CACHE and (now - _DIR_CACHE_TS) < _DIR_TTL_SEC:
            return dict(_DIR_CACHE)
    mp = _fetch_directory(tenant_token)
    with _DIR_LOCK:
        if mp:
            _DIR_CACHE = mp
            _DIR_CACHE_TS = now
        return dict(_DIR_CACHE)


def _alias_cfg() -> Tuple[str, str, str, str]:
    _config.reload_env_runtime()
    # Same spreadsheet as the OpenID directory; a different ?sheet= (the "Real names" tab).
    token = (os.getenv("DUTY_DIRECTORY_SHEET_TOKEN") or "").strip()
    sheet_id = (os.getenv("DUTY_DIRECTORY_ALIAS_SHEET_ID") or "").strip()
    sheet_name = (os.getenv("DUTY_DIRECTORY_ALIAS_SHEET_NAME") or "Real names").strip()
    rng = (os.getenv("DUTY_DIRECTORY_ALIAS_RANGE") or "A:B").strip()
    return token, sheet_id, sheet_name, rng


def _fetch_alias_map(tenant_token: str) -> Dict[str, str]:
    token, sheet_id, sheet_name, rng = _alias_cfg()
    if not token:
        return {}
    if not sheet_id:
        sheet_id = _lark.resolve_sheet_id(tenant_token, token, sheet_name)
        if not sheet_id:
            return {}
    rows, err = _lark.read_sheets_values_batch(tenant_token, token, f"{sheet_id}!{rng}")
    if err or not rows:
        return {}
    header = list(rows[0] or [])
    ci_short = _col_index(header, "sheet name", "shortcut", "alias")
    ci_real = _col_index(header, "real name", "real", "full name")
    if ci_short < 0:
        ci_short = 0  # default: col A = sheet name, col B = real name
    if ci_real < 0:
        ci_real = 1
    out: Dict[str, str] = {}
    for r in rows[1:]:
        cells = [str(c or "").strip() for c in (r or [])]
        short = normalize_name(cells[ci_short] if ci_short < len(cells) else "")
        real = (cells[ci_real] if ci_real < len(cells) else "").strip()
        if short and real:
            out[short] = real
    log.info("duty_directory: loaded %s name aliases (sheet-name -> real-name)", len(out))
    return out


def get_alias_map(tenant_token: str) -> Dict[str, str]:
    """``{normalized_sheet_name: real_name}`` from the optional Real-names tab, TTL-cached (empty
    result is cached too, so an unconfigured alias tab doesn't re-hit the API every lookup)."""
    global _ALIAS_CACHE, _ALIAS_CACHE_TS, _ALIAS_LOADED
    now = time.time()
    with _ALIAS_LOCK:
        if _ALIAS_LOADED and (now - _ALIAS_CACHE_TS) < _DIR_TTL_SEC:
            return dict(_ALIAS_CACHE)
    mp = _fetch_alias_map(tenant_token)
    with _ALIAS_LOCK:
        _ALIAS_CACHE = mp
        _ALIAS_CACHE_TS = now
        _ALIAS_LOADED = True
        return dict(_ALIAS_CACHE)


def apply_alias(tenant_token: str, name: str) -> str:
    """Map a roster SHEET NAME (shortcut) to its REAL NAME via the alias tab; ``name`` unchanged when
    there is no alias (or the tab is unconfigured)."""
    return get_alias_map(tenant_token).get(normalize_name(name)) or name


def resolve_open_id_for_name(tenant_token: str, name: str) -> str:
    """Directory ``open_id`` for a roster name (SHEET NAME -> REAL NAME via the alias tab first), or ''."""
    real = apply_alias(tenant_token, name)
    return get_directory(tenant_token).get(normalize_name(real), "")


def resolve_open_ids_for_names(tenant_token: str, names) -> Dict[str, str]:
    """``{original_name: open_id}`` for the names in the directory (alias-mapped first; others omitted)."""
    mp = get_directory(tenant_token)
    out: Dict[str, str] = {}
    for nm in names or []:
        oid = mp.get(normalize_name(apply_alias(tenant_token, nm)))
        if oid:
            out[nm] = oid
    return out


# --------------------------------------------------------------------------------------------------
# SRE handler tab (Name | Handler) — which SRE person covers which team(s)
# --------------------------------------------------------------------------------------------------
def _norm_team_token(tok: str) -> str:
    """Space-collapsed, uppercased team token so 'Front End' -> 'FRONTEND', 'cpms' -> 'CPMS'."""
    return re.sub(r"\s+", "", (tok or "")).upper()


def _sre_cfg() -> Tuple[str, str, str, str]:
    _config.reload_env_runtime()
    # Reuses the directory spreadsheet token; the SRE tab is a different ?sheet= (e.g. KMPx2p).
    token = (os.getenv("DUTY_DIRECTORY_SHEET_TOKEN") or "").strip()
    sheet_id = (os.getenv("DUTY_DIRECTORY_SRE_SHEET_ID") or "").strip()
    sheet_name = (os.getenv("DUTY_DIRECTORY_SRE_SHEET_NAME") or "").strip()
    rng = (os.getenv("DUTY_DIRECTORY_SRE_RANGE") or "A:B").strip()
    return token, sheet_id, sheet_name, rng


def _fetch_sre_handler_map(tenant_token: str) -> Dict[str, Set[str]]:
    token, sheet_id, sheet_name, rng = _sre_cfg()
    if not token:
        log.warning("duty_directory: DUTY_DIRECTORY_SHEET_TOKEN not set (SRE handler tab)")
        return {}
    if not sheet_id:
        sheet_id = _lark.resolve_sheet_id(tenant_token, token, sheet_name)
        if not sheet_id:
            log.warning("duty_directory: could not resolve SRE handler sheet_id (share/permission?)")
            return {}
    rows, err = _lark.read_sheets_values_batch(tenant_token, token, f"{sheet_id}!{rng}")
    if err or not rows:
        log.warning("duty_directory: SRE handler read failed err=%s rows=%s", err, len(rows or []))
        return {}
    header = list(rows[0] or [])
    ci_name = _col_index(header, "name")
    ci_handler = _col_index(header, "handler", "team", "teams")
    if ci_name < 0 or ci_handler < 0:
        log.warning(
            "duty_directory: SRE handler tab needs 'Name' + 'Handler' columns; header=%r",
            [str(h) for h in header],
        )
        return {}
    out: Dict[str, Set[str]] = {}
    for r in rows[1:]:
        cells = [str(c or "").strip() for c in (r or [])]

        def g(i: int) -> str:
            return cells[i] if 0 <= i < len(cells) else ""

        nm = normalize_name(g(ci_name))
        if not nm:
            continue
        tokens = {_norm_team_token(t) for t in g(ci_handler).split("/") if t.strip()}
        if not tokens:
            continue
        out.setdefault(nm, set()).update(tokens)  # merge if a name appears more than once
    log.info("duty_directory: loaded %s SRE handler name->team entries", len(out))
    return out


def get_sre_handler_map(tenant_token: str) -> Dict[str, Set[str]]:
    """``{normalized_name: {team_token, ...}}`` for the SRE handler tab, TTL-cached."""
    global _SRE_CACHE, _SRE_CACHE_TS
    now = time.time()
    with _SRE_LOCK:
        if _SRE_CACHE and (now - _SRE_CACHE_TS) < _DIR_TTL_SEC:
            return {k: set(v) for k, v in _SRE_CACHE.items()}
    mp = _fetch_sre_handler_map(tenant_token)
    with _SRE_LOCK:
        if mp:
            _SRE_CACHE = mp
            _SRE_CACHE_TS = now
        return {k: set(v) for k, v in _SRE_CACHE.items()}


def get_sre_team_person_names(tenant_token: str, team_tokens: Set[str]) -> List[str]:
    """ORIGINAL-cased names (sheet order) whose SRE Handler covers any of ``team_tokens``.

    For the ring "who else covers this team" list — no on-shift filter, deduped by normalized name,
    original casing preserved (so 'OSE' / 'Jewell Peñamante' render as written, not title-cased).
    Returns ``[]`` on any read/permission failure."""
    want = {_norm_team_token(t) for t in (team_tokens or set()) if str(t).strip()}
    if not want:
        return []
    token, sheet_id, sheet_name, rng = _sre_cfg()
    if not token:
        return []
    if not sheet_id:
        sheet_id = _lark.resolve_sheet_id(tenant_token, token, sheet_name)
        if not sheet_id:
            return []
    rows, err = _lark.read_sheets_values_batch(tenant_token, token, f"{sheet_id}!{rng}")
    if err or not rows:
        return []
    header = list(rows[0] or [])
    ci_name = _col_index(header, "name")
    ci_handler = _col_index(header, "handler", "team", "teams")
    if ci_name < 0 or ci_handler < 0:
        log.warning(
            "duty_directory: SRE team-list — no Name/Handler header ci_name=%s ci_handler=%s header=%r",
            ci_name, ci_handler, [str(h) for h in header],
        )
        return []
    out: List[str] = []
    seen: Set[str] = set()
    for r in rows[1:]:
        cells = [str(c or "").strip() for c in (r or [])]

        def g(i: int) -> str:
            return cells[i] if 0 <= i < len(cells) else ""

        raw = g(ci_name)
        if not raw:
            continue
        tokens = {_norm_team_token(t) for t in g(ci_handler).split("/") if t.strip()}
        if not (tokens & want):
            continue
        key = normalize_name(raw)
        if key in seen:
            continue
        seen.add(key)
        out.append(raw)
    # Diagnostic: show exactly what the bot read so a "why only one name?" case can be pinned to the
    # sheet/range (small tab, so logging the raw name|handler pairs is cheap).
    def _cell(r: Any, i: int) -> str:
        return str(r[i]).strip() if isinstance(r, list) and 0 <= i < len(r) else ""

    pairs = [(_cell(r, ci_name), _cell(r, ci_handler)) for r in rows[1:21]]
    log.info(
        "duty_directory: SRE team-list want=%s rows=%s ci_name=%s ci_handler=%s pairs=%r matched=%s",
        sorted(want), len(rows), ci_name, ci_handler, pairs, out,
    )
    return out
