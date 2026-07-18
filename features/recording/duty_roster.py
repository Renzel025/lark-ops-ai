"""Duty / on-call roster lookup: resolve today's duty person(s) from the team roster sheets.

Two families of ring command use this:

* ``scpms / sfpms / sfe`` (SRE duty) -> ``COMMAND_TEAM`` + ``get_duty_open_id`` (env stub for now).
* ``fe / fpms`` (team duty) -> read the team's Lark roster sheet live, parse **today's** duty
  name(s), then map name -> ``open_id`` via ``duty_directory``. Flow:

      roster sheet -> today's duty name -> directory -> open_id -> ring

Each ``fe/fpms`` roster is env-configured (sheet_id is the ``?sheet=`` in the URL):
  DUTY_ROSTER_FE_SHEET_TOKEN / _SHEET_ID / _RANGE      (Frontend "Latest Duty List")
  DUTY_ROSTER_FPMS_SHEET_TOKEN / _SHEET_ID / _RANGE    (FPMS 排班表 "2026")

The two roster layouts differ, so each has its own pure parser (``parse_frontend_duty`` /
``parse_fpms_duty``) that takes the raw ``values`` rows + the day-of-month and returns the name(s).
"""
from __future__ import annotations

import datetime
import logging
import os
from typing import Any, Callable, Dict, List, Tuple

from p0_logic import config as _config
from p0_logic import lark_client as _lark

log = logging.getLogger("lark-ops-ai")

# Ring-command keyword -> team code (SRE duty family).
COMMAND_TEAM = {
    "scpms": "CPMS",
    "sfpms": "FPMS",
    "sfe": "FE",
}


def get_duty_open_id(team: str) -> str:
    """Current duty SRE ``open_id`` for ``team`` (CPMS/FPMS/FE) via env stub, or "".

    Reads ``P0_VC_RING_DUTY_<TEAM>_OPEN_ID``. (SRE-duty family; the fe/fpms family below reads
    the live roster sheets instead.)
    """
    t = (team or "").strip().upper()
    if not t:
        return ""
    _config.reload_env_runtime()
    env_name = f"P0_VC_RING_DUTY_{t}_OPEN_ID"
    oid = (os.getenv(env_name) or "").strip()
    if oid.startswith("ou_"):
        return oid
    if oid:
        log.warning("duty_roster: %s is set but is not an ou_ open_id: %r", env_name, oid[:16])
    return ""


# --------------------------------------------------------------------------------------------------
# Pure parsers: raw sheet ``values`` rows (list of lists) + day-of-month -> duty name(s)
# --------------------------------------------------------------------------------------------------
def _as_day(cell: Any) -> int:
    """Return 1..31 if ``cell`` is a bare day number, else 0."""
    s = str(cell if cell is not None else "").strip()
    if not s:
        return 0
    try:
        n = int(float(s))
    except (TypeError, ValueError):
        return 0
    return n if 1 <= n <= 31 else 0


def _is_date_row(row: List[Any], *, min_days: int = 3) -> bool:
    """A row is a 'date row' when it holds several bare day numbers (1..31)."""
    return sum(1 for c in (row or []) if _as_day(c)) >= min_days


_MONTH_NUM = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _header_month(text: str) -> int:
    """Month number from an FPMS date-header like '日期 - July' / '日期 - Jan', else 0."""
    t = (text or "").strip().lower()
    for key, num in _MONTH_NUM.items():
        if key in t:
            return num
    return 0


def parse_frontend_duty(rows: List[List[Any]], today: datetime.date) -> List[str]:
    """Frontend 'Latest Duty List' (single current month): stacked 10-day blocks — a date row,
    then the duty name row directly below it (an optional partner row may follow). [primary, (partner)]."""
    day = today.day
    for i, row in enumerate(rows):
        if not _is_date_row(row):
            continue
        for c, cell in enumerate(row):
            if _as_day(cell) != day:
                continue
            names: List[str] = []
            if i + 1 < len(rows) and c < len(rows[i + 1]):
                primary = str(rows[i + 1][c] or "").strip()
                if primary:
                    names.append(primary)
            # optional partner on the next row (skip if that row is itself the next date block)
            if i + 2 < len(rows) and not _is_date_row(rows[i + 2]) and c < len(rows[i + 2]):
                partner = str(rows[i + 2][c] or "").strip()
                if partner and _as_day(partner) == 0:
                    names.append(partner)
            return names
    return []


def parse_fpms_duty(rows: List[List[Any]], today: datetime.date) -> List[str]:
    """FPMS 排班表 (12 month blocks stacked): find the date-header row for today's MONTH
    (``日期 - <month>`` in col A, day numbers across), then the day column, then every person
    (name in col A) whose day cell is non-empty.

    NOTE: FPMS marks duty with cell COLOUR; only cells that also carry a value (e.g. '2') come
    back from the values API. Colour-only cells read as blank and are invisible here — confirm
    with scripts/read_duty_sheet_once.py before relying on this.
    """
    day, month = today.day, today.month
    date_row_idx = -1
    fallback_idx = -1
    for i, row in enumerate(rows):
        head = str((row[0] if row else "") or "")
        if "日期" in head:
            if _header_month(head) == month:
                date_row_idx = i
                break
            if fallback_idx < 0 and _is_date_row(row):
                fallback_idx = i
        elif fallback_idx < 0 and _is_date_row(row):
            fallback_idx = i
    if date_row_idx < 0:
        date_row_idx = fallback_idx
    if date_row_idx < 0:
        return []
    day_col = -1
    for c, cell in enumerate(rows[date_row_idx]):
        if _as_day(cell) == day:
            day_col = c
            break
    if day_col < 0:
        return []
    names: List[str] = []
    for row in rows[date_row_idx + 1:]:
        head = str((row[0] if row else "") or "")
        if "日期" in head:  # reached the next month block — stop
            break
        name = head.strip()
        if not name:
            continue
        mark = str((row[day_col] if day_col < len(row) else "") or "").strip()
        if mark:
            names.append(name)
    return names


# --------------------------------------------------------------------------------------------------
# Live-read resolver for the fe/fpms family
# --------------------------------------------------------------------------------------------------
_ROSTER: Dict[str, Tuple[str, Callable[[List[List[Any]], datetime.date], List[str]]]] = {
    "fe": ("DUTY_ROSTER_FE", parse_frontend_duty),
    "fpms": ("DUTY_ROSTER_FPMS", parse_fpms_duty),
}


def is_roster_command(cmd: str) -> bool:
    return (cmd or "").strip().lower() in _ROSTER


def _roster_env(prefix: str) -> Tuple[str, str, str]:
    _config.reload_env_runtime()
    token = (os.getenv(f"{prefix}_SHEET_TOKEN") or "").strip()
    sheet_id = (os.getenv(f"{prefix}_SHEET_ID") or "").strip()
    rng = (os.getenv(f"{prefix}_RANGE") or "A:AF").strip()
    return token, sheet_id, rng


def resolve_duty_names(cmd: str, tenant_token: str) -> List[str]:
    """Today's duty name(s) for a ``fe``/``fpms`` command, read live from its roster sheet.

    Uses the VPS local date; the box runs on UTC+8 (CST/MYT), the rosters' timezone.
    """
    c = (cmd or "").strip().lower()
    reg = _ROSTER.get(c)
    if not reg:
        return []
    prefix, parser = reg
    token, sheet_id, rng = _roster_env(prefix)
    if not token or not sheet_id:
        log.warning("duty_roster: %s_SHEET_TOKEN/_SHEET_ID not set", prefix)
        return []
    rows, err = _lark.read_sheets_values_batch(tenant_token, token, f"{sheet_id}!{rng}")
    if err or not rows:
        log.warning("duty_roster: %s read failed err=%s rows=%s", c, err, len(rows or []))
        return []
    today = datetime.date.today()
    names = parser(rows, today)
    log.info("duty_roster: %s date=%s duty_names=%s", c, today.isoformat(), names)
    return names


def resolve_duty_open_ids(cmd: str, tenant_token: str) -> Tuple[List[str], List[str]]:
    """``(open_ids, unresolved_names)`` for a ``fe``/``fpms`` command: parse today's duty from the
    roster sheet, then map each name -> open_id via the duty directory."""
    from features.recording import duty_directory as _dir

    names = resolve_duty_names(cmd, tenant_token)
    open_ids: List[str] = []
    unresolved: List[str] = []
    seen = set()
    for nm in names:
        oid = _dir.resolve_open_id_for_name(tenant_token, nm)
        if oid:
            if oid not in seen:
                seen.add(oid)
                open_ids.append(oid)
        else:
            unresolved.append(nm)
    if unresolved:
        log.warning("duty_roster: %s names not in directory: %s", cmd, unresolved)
    return open_ids, unresolved
