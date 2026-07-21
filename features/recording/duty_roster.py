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
import re
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from p0_logic import config as _config
from p0_logic import lark_client as _lark

log = logging.getLogger("lark-ops-ai")

# Ring-command keyword -> team code (SRE duty family). Used for the env-stub fallback
# (P0_VC_RING_DUTY_<TEAM>_OPEN_ID) when the live SRE resolver yields nothing.
COMMAND_TEAM = {
    "scpms": "CPMS",
    "sfpms": "FPMS",
    "sfe": "FE",
    "spms": "PMS",
}

# SRE ring-command -> the Handler token(s) that mean "this person covers this team", matched as an
# EXACT token against the SRE-tab Handler cell split on "/". Tokens are space-collapsed + uppercased
# so "Front End" -> "FRONTEND". PMS must NOT substring-match CPMS/FPMS, hence exact-token matching.
SRE_COMMAND_TEAM_TOKENS: Dict[str, Set[str]] = {
    "scpms": {"CPMS"},
    "sfpms": {"FPMS"},
    "sfe": {"FRONTEND", "FE"},
    "spms": {"PMS"},
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
    """Frontend 'Latest Duty List' (single current month): stacked 10-day blocks — a date row, then
    the duty name row directly below it. Returns the PRIMARY duty person for today (the row right
    under the date). The partner/backup row below it is intentionally NOT rung — use /e to escalate."""
    day = today.day
    for i, row in enumerate(rows):
        if not _is_date_row(row):
            continue
        for c, cell in enumerate(row):
            if _as_day(cell) != day:
                continue
            if i + 1 < len(rows) and c < len(rows[i + 1]):
                primary = str(rows[i + 1][c] or "").strip()
                if primary:
                    return [primary]
            return []
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
# SRE duty-shift parser (OSE & SRE Duty Shift, "SRE PLATFORM" section)
# --------------------------------------------------------------------------------------------------
def _norm_name(name: str) -> str:
    """Case/space-insensitive key (mirrors duty_directory.normalize_name) so names join cleanly."""
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _checkbox_on(cell: Any) -> bool:
    """True when a checkbox / duty cell means 'on shift'. The values API returns 1/0 (int) for
    Lark checkboxes; also tolerate '1'/'true'/'yes'/'✓'."""
    if cell is None:
        return False
    if isinstance(cell, bool):
        return cell
    s = str(cell).strip().lower()
    if not s:
        return False
    if s in ("1", "true", "yes", "checked", "✓", "y"):
        return True
    try:
        return int(float(s)) == 1
    except (TypeError, ValueError):
        return False


def _clean_person_name(cell: Any) -> str:
    """Clean roster name = the text before the phone/note. Cuts at the FIRST of ``(`` / ``[`` / ``+``
    so both ``Kelvin (+60125989338)`` (SRE, parens) and ``Kah Zheng +60169294328`` (DBA, bare ``+``)
    reduce to just the name. Names never contain those chars."""
    s = str(cell if cell is not None else "").strip()
    if not s:
        return ""
    cut = len(s)
    for delim in ("(", "[", "+"):
        k = s.find(delim)
        if 0 < k < cut:
            cut = k
    return re.sub(r"\s+", " ", s[:cut]).strip()


def _is_sre_section_end(col_a: str) -> bool:
    """True when col A is the legend/section header that ends the SRE PLATFORM person list.

    Below the SRE PLATFORM people the sheet has 'BACKEND TEAM ...' / 'FRONTEND TEAM ...' legend rows,
    then a separate DBA section (with its OWN checkboxes) and 'SRE Game' blocks. We must stop before
    those or /scpms would wrongly page DBA duty.
    """
    t = re.sub(r"\s+", " ", (col_a or "").strip()).upper()
    if not t:
        return False
    if "BACKEND TEAM" in t or "FRONTEND TEAM" in t or "SRE GAME" in t or "SRE PLATFORM" in t:
        return True
    if t == "DBA" or t.startswith("DBA ") or t.startswith("DBA\n") or t.startswith("DBA("):
        return True
    return False


def parse_sre_shift_on_duty(rows: List[List[Any]], today: datetime.date) -> List[str]:
    """Clean names of everyone ON SHIFT TODAY in the OSE & SRE Duty Shift 'SRE PLATFORM' section.

    Layout: a continuous daily timeline where col B (index 1) = Jan 1 and each next column is +1 day,
    so today's 0-based column index = ``today.timetuple().tm_yday`` (col A = index 0). Do NOT read the
    day-number row (it is a live formula). Under the 'SRE PLATFORM' header each person is
    ``Name (+phone)`` in col A with a checkbox (1/0) per day; return the clean names whose today cell
    reads 1. The section ends at the BACKEND/FRONTEND TEAM legend (see ``_is_sre_section_end``).

    ASSUMES the range starts at column A (env default does).
    """
    header_idx = -1
    for i, row in enumerate(rows):
        col_a = re.sub(r"\s+", " ", str((row[0] if row else "") or "")).upper()
        if "SRE PLATFORM" in col_a:
            header_idx = i
            break
    if header_idx < 0:
        return []
    day_idx = today.timetuple().tm_yday  # 0-based col index (col B == Jan 1 == index 1)
    names: List[str] = []
    seen: Set[str] = set()
    blanks = 0
    for row in rows[header_idx + 1:]:
        col_a = str((row[0] if row else "") or "").strip()
        if not col_a:
            blanks += 1
            if blanks >= 5:  # a wide run of blank rows -> section clearly ended
                break
            continue
        blanks = 0
        if _is_sre_section_end(col_a):
            break
        cell = row[day_idx] if day_idx < len(row) else None
        if _checkbox_on(cell):
            nm = _clean_person_name(col_a)
            key = _norm_name(nm)
            if nm and key not in seen:
                seen.add(key)
                names.append(nm)
    return names


def parse_dba_shift_on_duty(rows: List[List[Any]], today: datetime.date) -> List[str]:
    """Clean names ON SHIFT TODAY in the OSE & SRE Duty Shift 'DBA' section (below SRE PLATFORM).

    Same continuous-daily-timeline column logic as ``parse_sre_shift_on_duty`` (today's 0-based col =
    ``today.tm_yday``). Finds the exact ``DBA`` header in col A, reads person rows (``Name (+phone)``)
    with a per-day checkbox (1/0), and stops at the next section header (``SRE Game`` etc.). The DBA
    people ARE the duty — there is no handler tab.
    """
    header_idx = -1
    for i, row in enumerate(rows):
        col_a = re.sub(r"\s+", " ", str((row[0] if row else "") or "")).strip().upper()
        if col_a == "DBA" or col_a.startswith("DBA "):
            header_idx = i
            break
    if header_idx < 0:
        return []
    day_idx = today.timetuple().tm_yday
    names: List[str] = []
    seen: Set[str] = set()
    blanks = 0
    for row in rows[header_idx + 1:]:
        col_a = str((row[0] if row else "") or "").strip()
        if not col_a:
            blanks += 1
            if blanks >= 5:
                break
            continue
        blanks = 0
        up = re.sub(r"\s+", " ", col_a).upper()
        if "SRE GAME" in up or "SRE PLATFORM" in up or "BACKEND TEAM" in up or "FRONTEND TEAM" in up:
            break  # next section -> DBA block ended
        cell = row[day_idx] if day_idx < len(row) else None
        if _checkbox_on(cell):
            nm = _clean_person_name(col_a)
            key = _norm_name(nm)
            if nm and key not in seen:
                seen.add(key)
                names.append(nm)
    return names


# --------------------------------------------------------------------------------------------------
# PMS Support weekly roster parser
# --------------------------------------------------------------------------------------------------
def _parse_month_day(cell: Any) -> Tuple[int, int]:
    """Return ``(month, day)`` from a date-ish cell, else ``(0, 0)``.

    Handles: datetime/date objects (openpyxl), the live values API's ``'20-Jul'`` day-month strings,
    ISO / slash dates, and Excel serial numbers. The YEAR is ignored (the PMS sheet is day-month).
    """
    if cell is None:
        return (0, 0)
    if isinstance(cell, datetime.datetime):
        return (cell.month, cell.day)
    if isinstance(cell, datetime.date):
        return (cell.month, cell.day)
    if isinstance(cell, (int, float)) and not isinstance(cell, bool):
        try:
            d = datetime.date(1899, 12, 30) + datetime.timedelta(days=int(cell))
            return (d.month, d.day)
        except (ValueError, OverflowError):
            return (0, 0)
    s = str(cell).strip()
    if not s:
        return (0, 0)
    if re.fullmatch(r"\d+(\.\d+)?", s):
        try:
            d = datetime.date(1899, 12, 30) + datetime.timedelta(days=int(float(s)))
            return (d.month, d.day)
        except (ValueError, OverflowError):
            return (0, 0)
    for fmt in (
        "%d-%b", "%d-%B", "%b-%d", "%B-%d", "%d %b", "%b %d",
        "%Y-%m-%d", "%d-%b-%Y", "%d/%m/%Y", "%Y/%m/%d",
    ):
        try:
            dt = datetime.datetime.strptime(s, fmt)
            return (dt.month, dt.day)
        except ValueError:
            continue
    m = re.search(r"(\d{1,2})\D+([A-Za-z]{3,})", s)
    if m:
        mon = _MONTH_NUM.get(m.group(2)[:3].lower(), 0)
        if mon:
            return (mon, int(m.group(1)))
    m = re.search(r"([A-Za-z]{3,})\D+(\d{1,2})", s)
    if m:
        mon = _MONTH_NUM.get(m.group(1)[:3].lower(), 0)
        if mon:
            return (mon, int(m.group(2)))
    return (0, 0)


def _pms_week_contains(today: datetime.date, start_cell: Any, end_cell: Any) -> bool:
    """True when ``today`` falls in [start, end], reading both as day-month in the current year and
    handling the Dec->Jan year wrap (end < start => end rolls to next year; also try start in the
    previous year so an early-January date matches a late-December week)."""
    sm, sd = _parse_month_day(start_cell)
    em, ed = _parse_month_day(end_cell)
    if not sm or not em:
        return False
    for sy in (today.year, today.year - 1):
        try:
            start = datetime.date(sy, sm, sd)
            end = datetime.date(sy, em, ed)
        except ValueError:
            continue
        if end < start:
            try:
                end = datetime.date(sy + 1, em, ed)
            except ValueError:
                continue
        if start <= today <= end:
            return True
    return False


def parse_pms_duty(rows: List[List[Any]], today: datetime.date) -> List[str]:
    """PMS Support weekly roster: find the header row (Start / End / First Level columns), then the
    week whose [Start, End] contains today, and return the First-Level name."""
    hdr_idx = -1
    ci_start = ci_end = ci_first = -1
    for i, row in enumerate(rows):
        low = [re.sub(r"\s+", " ", str(c or "").strip().lower()) for c in (row or [])]
        cs = ce = cf = -1
        for j, v in enumerate(low):
            if v == "start" and cs < 0:
                cs = j
            elif v == "end" and ce < 0:
                ce = j
            elif v.startswith("first level") and cf < 0:
                cf = j
        if cs >= 0 and ce >= 0 and cf >= 0:
            hdr_idx, ci_start, ci_end, ci_first = i, cs, ce, cf
            break
    if hdr_idx < 0:
        return []
    for row in rows[hdr_idx + 1:]:
        def g(idx: int) -> Any:
            return row[idx] if 0 <= idx < len(row) else None

        first = str(g(ci_first) or "").strip()
        if not first:
            continue
        if _pms_week_contains(today, g(ci_start), g(ci_end)):
            return [first]
    return []


# --------------------------------------------------------------------------------------------------
# Live-read resolver for the fe/fpms/pms family
# --------------------------------------------------------------------------------------------------
_ROSTER: Dict[str, Tuple[str, Callable[[List[List[Any]], datetime.date], List[str]]]] = {
    "fe": ("DUTY_ROSTER_FE", parse_frontend_duty),
    "fpms": ("DUTY_ROSTER_FPMS", parse_fpms_duty),
    "pms": ("DUTY_ROSTER_PMS", parse_pms_duty),
}


def is_roster_command(cmd: str) -> bool:
    return (cmd or "").strip().lower() in _ROSTER


def _roster_env(prefix: str) -> Tuple[str, str, str, str]:
    _config.reload_env_runtime()
    token = (os.getenv(f"{prefix}_SHEET_TOKEN") or "").strip()
    sheet_id = (os.getenv(f"{prefix}_SHEET_ID") or "").strip()
    sheet_name = (os.getenv(f"{prefix}_SHEET_NAME") or "").strip()
    rng = (os.getenv(f"{prefix}_RANGE") or "A:AF").strip()
    return token, sheet_id, sheet_name, rng


def resolve_duty_names(cmd: str, tenant_token: str) -> List[str]:
    """Today's duty name(s) for a ``fe``/``fpms`` command, read live from its roster sheet.

    Uses the VPS local date; the box runs on UTC+8 (CST/MYT), the rosters' timezone.
    """
    c = (cmd or "").strip().lower()
    reg = _ROSTER.get(c)
    if not reg:
        return []
    prefix, parser = reg
    token, sheet_id, sheet_name, rng = _roster_env(prefix)
    if not token:
        log.warning("duty_roster: %s_SHEET_TOKEN not set", prefix)
        return []
    if not sheet_id:
        # Single-tab sheet: URL has no ?sheet= — resolve the sheet_id (by name, else first sheet).
        sheet_id = _lark.resolve_sheet_id(tenant_token, token, sheet_name)
        if not sheet_id:
            log.warning("duty_roster: %s could not resolve sheet_id (token/share/permission?)", prefix)
            return []
    rows, err = _lark.read_sheets_values_batch(tenant_token, token, f"{sheet_id}!{rng}")
    if err or not rows:
        log.warning("duty_roster: %s read failed err=%s rows=%s", c, err, len(rows or []))
        return []
    today = datetime.date.today()
    # Debug: show what the values API actually returned so a []-result parse can be diagnosed.
    head = [[str(x)[:10] for x in (r or [])[:14]] for r in rows[:6]]
    log.info("duty_roster: %s read rows=%s head=%r", c, len(rows), head)
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


# --------------------------------------------------------------------------------------------------
# SRE duty resolver (scpms / sfpms / sfe / spms)
# --------------------------------------------------------------------------------------------------
# PRIMARY source = the SRE handler tab (Name | Handler): pick names whose Handler covers the team.
# The OSE & SRE duty-shift "on-shift today" filter is applied ONLY when DUTY_SRE_SHIFT_SHEET_TOKEN is
# configured; otherwise every team handler is rung (the all-"OSE" test placeholder case).
def is_sre_command(cmd: str) -> bool:
    return (cmd or "").strip().lower() in SRE_COMMAND_TEAM_TOKENS


def _sre_shift_env() -> Tuple[str, str, str, str]:
    _config.reload_env_runtime()
    token = (os.getenv("DUTY_SRE_SHIFT_SHEET_TOKEN") or "").strip()
    sheet_id = (os.getenv("DUTY_SRE_SHIFT_SHEET_ID") or "").strip()
    sheet_name = (os.getenv("DUTY_SRE_SHIFT_SHEET_NAME") or "").strip()
    # Default reaches ~col 370 (NF) so any day of the year resolves (HZ would stop mid-August); rows
    # to 130 give headroom below the SRE PLATFORM section (now ~r82-110 after OTE/TEAM blocks moved it down).
    rng = (os.getenv("DUTY_SRE_SHIFT_RANGE") or "A1:NF130").strip()
    return token, sheet_id, sheet_name, rng


def resolve_sre_shift_names(tenant_token: str) -> Optional[List[str]]:
    """Clean names on shift TODAY in the OSE & SRE duty-shift 'SRE PLATFORM' section, read live.

    Returns ``None`` when the on-shift filter should be SKIPPED — either the shift sheet is not
    configured, or its read failed (403/not shared/etc.). The caller then FAILS OPEN (rings all team
    handlers) instead of ringing nobody. Returns a list (possibly empty) only on a successful read.
    """
    token, sheet_id, sheet_name, rng = _sre_shift_env()
    if not token:
        return None
    if not sheet_id:
        sheet_id = _lark.resolve_sheet_id(tenant_token, token, sheet_name)
        if not sheet_id:
            log.warning("duty_roster: SRE shift could not resolve sheet_id — skipping on-shift filter")
            return None
    rows, err = _lark.read_sheets_values_batch(tenant_token, token, f"{sheet_id}!{rng}")
    if err or not rows:
        log.warning(
            "duty_roster: SRE shift read failed (share the bot on the sheet?) err=%s rows=%s "
            "— skipping on-shift filter (fail-open)",
            err,
            len(rows or []),
        )
        return None
    today = datetime.date.today()
    names = parse_sre_shift_on_duty(rows, today)
    log.info("duty_roster: SRE shift date=%s on_shift=%s", today.isoformat(), names)
    return names


def sre_team_names(
    cmd: str,
    handler_map: Dict[str, Set[str]],
    on_shift_norm: Optional[Set[str]] = None,
) -> List[str]:
    """Normalized SRE-tab names whose Handler covers ``cmd``'s team (EXACT token match). When
    ``on_shift_norm`` is not None, keep only names also on shift today. Pure — unit-testable."""
    want = SRE_COMMAND_TEAM_TOKENS.get((cmd or "").strip().lower())
    if not want:
        return []
    out: List[str] = []
    for nm, toks in handler_map.items():
        if not (toks & want):
            continue
        if on_shift_norm is not None and nm not in on_shift_norm:
            continue
        out.append(nm)
    return out


def resolve_sre_duty_open_ids(cmd: str, tenant_token: str) -> Tuple[List[str], List[str]]:
    """``(open_ids, unresolved_names)`` for an SRE command: SRE-tab handlers for the team, optionally
    intersected with today's duty shift, then name -> open_id via the OpenID directory tab."""
    from features.recording import duty_directory as _dir

    c = (cmd or "").strip().lower()
    if not is_sre_command(c):
        return [], []
    handler_map = _dir.get_sre_handler_map(tenant_token)
    # None => skip the filter (not configured OR read failed => fail-open, ring all team handlers).
    shift_names = resolve_sre_shift_names(tenant_token)
    on_shift_norm = None if shift_names is None else {_norm_name(n) for n in shift_names}
    names = sre_team_names(c, handler_map, on_shift_norm)
    open_ids: List[str] = []
    unresolved: List[str] = []
    seen: Set[str] = set()
    for nm in names:
        oid = _dir.resolve_open_id_for_name(tenant_token, nm)
        if oid:
            if oid not in seen:
                seen.add(oid)
                open_ids.append(oid)
        else:
            unresolved.append(nm)
    if unresolved:
        log.warning("duty_roster: SRE %s names not in directory: %s", c, unresolved)
    log.info(
        "duty_roster: SRE %s handlers=%s shift_filter=%s team_names=%s open_ids=%s",
        c, len(handler_map), on_shift_norm is not None, names, len(open_ids),
    )
    return open_ids, unresolved


# --------------------------------------------------------------------------------------------------
# DBA duty resolver (/dba) — the 'DBA' section of the OSE & SRE Duty Shift sheet
# --------------------------------------------------------------------------------------------------
def _dba_env() -> Tuple[str, str, str, str]:
    """DBA reads the SAME OSE & SRE Duty Shift sheet as the SRE shift; DUTY_DBA_* overrides it so
    /dba can be enabled WITHOUT turning on the SRE on-shift filter (which keys off DUTY_SRE_SHIFT_*)."""
    _config.reload_env_runtime()
    token = (os.getenv("DUTY_DBA_SHEET_TOKEN") or os.getenv("DUTY_SRE_SHIFT_SHEET_TOKEN") or "").strip()
    sheet_id = (os.getenv("DUTY_DBA_SHEET_ID") or os.getenv("DUTY_SRE_SHIFT_SHEET_ID") or "").strip()
    sheet_name = (os.getenv("DUTY_DBA_SHEET_NAME") or os.getenv("DUTY_SRE_SHIFT_SHEET_NAME") or "").strip()
    rng = (os.getenv("DUTY_DBA_RANGE") or os.getenv("DUTY_SRE_SHIFT_RANGE") or "A1:NF130").strip()
    return token, sheet_id, sheet_name, rng


def resolve_dba_duty_open_ids(tenant_token: str) -> Tuple[List[str], List[str]]:
    """``(open_ids, unresolved_names)`` for ``/dba``: today's on-shift DBA people from the duty-shift
    sheet's 'DBA' section, mapped name -> open_id via the OpenID directory tab."""
    from features.recording import duty_directory as _dir

    token, sheet_id, sheet_name, rng = _dba_env()
    if not token:
        log.warning("duty_roster: DBA sheet token not set (DUTY_DBA_SHEET_TOKEN / DUTY_SRE_SHIFT_SHEET_TOKEN)")
        return [], []
    if not sheet_id:
        sheet_id = _lark.resolve_sheet_id(tenant_token, token, sheet_name)
        if not sheet_id:
            log.warning("duty_roster: DBA could not resolve sheet_id (token/share/permission?)")
            return [], []
    rows, err = _lark.read_sheets_values_batch(tenant_token, token, f"{sheet_id}!{rng}")
    if err or not rows:
        log.warning("duty_roster: DBA read failed (share the bot?) err=%s rows=%s", err, len(rows or []))
        return [], []
    names = parse_dba_shift_on_duty(rows, datetime.date.today())
    open_ids: List[str] = []
    unresolved: List[str] = []
    seen: Set[str] = set()
    for nm in names:
        oid = _dir.resolve_open_id_for_name(tenant_token, nm)
        if oid:
            if oid not in seen:
                seen.add(oid)
                open_ids.append(oid)
        else:
            unresolved.append(nm)
    if unresolved:
        log.warning("duty_roster: DBA names not in directory: %s", unresolved)
    log.info("duty_roster: DBA on_shift=%s open_ids=%s", names, len(open_ids))
    return open_ids, unresolved
