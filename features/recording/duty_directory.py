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
from typing import Dict, Tuple

from p0_logic import config as _config
from p0_logic import lark_client as _lark

log = logging.getLogger("lark-ops-ai")

_DIR_CACHE: Dict[str, str] = {}
_DIR_CACHE_TS = 0.0
_DIR_LOCK = threading.RLock()
_DIR_TTL_SEC = 300.0


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


def resolve_open_id_for_name(tenant_token: str, name: str) -> str:
    """Directory ``open_id`` for a roster name, or '' when unknown."""
    return get_directory(tenant_token).get(normalize_name(name), "")


def resolve_open_ids_for_names(tenant_token: str, names) -> Dict[str, str]:
    """``{original_name: open_id}`` for the names that are in the directory (others omitted)."""
    mp = get_directory(tenant_token)
    out: Dict[str, str] = {}
    for nm in names or []:
        oid = mp.get(normalize_name(nm))
        if oid:
            out[nm] = oid
    return out
