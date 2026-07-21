---
name: duty-roster
description: Build, debug, and extend the @bot duty-ring commands that read team on-call rosters from Lark Sheets, resolve today's duty person(s) to open_id, and ring them into the active P0 VC.
disable-model-invocation: false
user-invocable: true
---

# Duty Roster ring commands  (Lark Sheet → today's duty → open_id → ring)

`@bot <cmd>` inside an incident group pages people into the **already-active** P0 VC meeting.

**Current focus = the 10 EMERGENCY commands.** (Game OM / game PO / EGAME SRE families are deferred —
the user is deciding on a data-driven "Duty Command Registry" sheet rather than ~130 hardcoded commands.)

| Command | Who | Source / status |
|---|---|---|
| `m` | major-P0 check persons | `P0_MAJOR_CHECK_PERSON_IDS` (config) |
| `e` | escalation contacts | `P0_VC_RING_ESCALATION_OPEN_IDS` (config) |
| `fe` / `fpms` / `pms` | **team** duty | live roster sheet → parser → directory → open_id ✅ **built** (pms = PMS Support, weekly First Level by [Start,End]) |
| `scpms` / `sfpms` / `sfe` / `spms` | **SRE** duty (CPMS/FPMS/FE/PMS) | ✅ **built** — SRE handler tab (`Name\|Handler`) → team match → directory → open_id; OPTIONAL on-shift filter gated on `DUTY_SRE_ONSHIFT_FILTER=1` (env stub `P0_VC_RING_DUTY_<TEAM>_OPEN_ID` = last-resort fallback) |
| `cpms` | CPMS team duty | TODO (CPMS sheet) |
| `dba` | DBA duty | ✅ **built** — today's on-shift people from the 'DBA' section of the ONE OSE & SRE Duty Shift sheet (`DUTY_SHIFT_SHEET_TOKEN`) → OpenID directory |
| `sosm` | liveslot SRE duty | ✅ **built** — today's on-shift people from the 'Liveslot' section of the same sheet → OpenID directory |

**SRE resolution** (`duty_roster.resolve_sre_duty_open_ids`): the SRE handler tab is a 2nd tab on the
directory sheet (`Name | Handler`, `?sheet=KMPx2p`); Handler is split on `/` and matched EXACTLY
(`PMS` must not substring-match `CPMS`/`FPMS`). The on-shift filter is DECOUPLED from the shift-sheet
token (which `/dba` and `/sosm` also need) and gated on its own flag **`DUTY_SRE_ONSHIFT_FILTER`**
(default OFF → every team handler rings; the all-"OSE" test tab rings the OSE test account). Turn it
on to also intersect with today's on-shift set from the OSE & SRE Duty Shift "SRE PLATFORM" section
(checkbox 1/0, column = `day_of_year`, continuous daily timeline from A1=Jan 1).

All 9 are recognized by `RING_CMD_RE`; the TODO ones reply "not wired up yet" until a parser + sheet
env are added. There is a dedicated **`duty-roster-expert` agent** (`.claude/agents/`) for this work.

Code: `features/recording/duty_roster.py` (parsers + resolver), `features/recording/duty_directory.py`
(name→open_id), wired in `features/recording/vc_ring.py::handle_ring_command`, gated by `RING_CMD_RE`
in `p0_logic/config.py`. Ring cmds require the bot to be **@mentioned** and `P0_VC_RING_ENABLED=1`.

## The fe/fpms flow
```
roster sheet (live)  →  parse today's duty NAME(s)  →  directory (name→open_id)  →  invite + ring in VC
```

## Roster sheets — EACH HAS A DIFFERENT LAYOUT
All under *Casino Plus x IGO* on `casinoplus.sg.larksuite.com`. `sheet_id` = the `?sheet=` in the URL.
Local `*.xlsx` in the repo root are STALE samples (gitignored) — always read the **LIVE** sheet.

1. **Frontend Duty Timetable** — token `B3bYsGn6UhTQixtR1oVlppiIgFg`, tab **"Latest Duty List"**
   (single *current* month). Stacked 10-day blocks: a date row (`1..10`), then the duty-name row
   directly below, optional partner row. `parse_frontend_duty`. Text values → values API reads it. ✅

2. **PMS Support** — token `LRBmswY7whi9LttJXMVlVozigkh`, tab **"2026"** (sheet_id `1cPvzX`).
   Weekly rows: `Month | Week | Start | End | First Level | Second | Third | Final`. Find the week
   where today ∈ [Start, End]; **First Level** = duty. Text values. (Parser TODO for `pms`.)

3. **FPMS 排班表** — token `F1rRskiOChUiWvts5nTlhVFngSf`, tab **"2026"** (sheet_id `1VXmDV`).
   12 month blocks stacked; each: `日期 - <month>` header + day numbers across, then person rows
   (name in col A) with a MARK in the day column. `parse_fpms_duty` (finds today's MONTH block, then
   the day column, then names with a non-empty cell). **GOTCHA:** cells are colour-coded, but the
   yellow ones carry the VALUE `2` → the values API returns `2`, so those parse. **Colour-ONLY** (green,
   no value) cells read as blank → invisible to the values API (see "cell colours" below).

4. **OSE & SRE Duty Shift** — token `Pwy8szuqohsPZetrvnflvQcBg9c` (was `BJWCsAB0…`, re-created 2026-07),
   tab **"FINAL OSE & QA MERGE"** (URL has no `?sheet=` → auto-resolve by NAME, leave sheet_id empty).
   ONE sheet, several checkbox sections. A1=`DATE(2026,1,1)`; columns are a CONTINUOUS daily timeline
   (col B = Jan 1, +1 day each), so today's 0-based col = `today.tm_yday`. Every section marks on-shift
   with a per-day **checkbox** (`1`/`0`). Env: `DUTY_SHIFT_SHEET_TOKEN` / `_SHEET_NAME` / `_RANGE`
   (=`A1:NF210`, must reach Liveslot ~r184); legacy `DUTY_SRE_SHIFT_*` / `DUTY_DBA_*` still fall back.
   Sections (all parsed by the generic `_parse_shift_section(rows, today, header_matches, own_kw)`,
   which finds the header row, reads person rows `Name (+phone)` below, and stops at the next section
   header in `_SHIFT_SECTION_ENDS` or after ≥5 blank rows):
   - **SRE PLATFORM** (~r82) → `parse_sre_shift_on_duty`, ends at BACKEND/FRONTEND TEAM legend (~r110).
     The team (CPMS/FPMS/FE/PMS) is NOT in this sheet — it comes from the SRE handler tab.
   - **DBA** (~r115, col A EXACTLY `DBA`) → `parse_dba_shift_on_duty`, ends at 'SRE Game'. Drives `/dba`.
   - **Liveslot** (~r179) → `parse_liveslot_shift_on_duty`, ends at 'EGAME'. Drives `/sosm`. ✅ built.
   - **DEFERRED** (do not touch): SRE Game (r130-177, priority 1st/2nd/3rd contacts), EGAME (r187,
     game-name keyword map), IT team (r200).

## Reading a sheet
`lark_client.read_sheets_values_batch(tenant_token, spreadsheet_token, "<sheet_id>!A1:range")` → rows
(list of lists), **values only** (v2 `values_batch_get`), mirrors `p0_logic/support.py`.
Checkbox → `1`/`0`; FPMS mark → `2`; **fill colour is NOT returned**.

**Cell colour / checkbox precisely:** if a roster relies on colour-ONLY marks, the values API can't see
them. The Lark CLI skill `lark-sheets` (`+cells-get --include value,style,data_validation`) maps to a
cell-style HTTP read — add a helper only if a roster needs colour, else ask the sheet owner to add a
text mark.

## name → open_id  (the directory)
Rosters carry names + phones but no open_id. `duty_directory.py` reads ONE directory sheet
`Name | open_id | email` (open_id wins; email is resolved via `batch_get_id`). TTL-cached 5 min.
Env `DUTY_DIRECTORY_SHEET_TOKEN / _SHEET_ID / _RANGE`; call `resolve_open_id_for_name(token, name)`.
All resolution needs a REAL person identifier in the sheet (name/email/phone), **not** a team label.

- **batch_get_id** by mobile/email — `lark_client.batch_get_id_by_mobile` / `_by_email` — BOT identity
  (tenant token), exact match. Needs `contact:user.id:readonly` + published contact data range (else
  **41050**). Accounts are often **email**-registered, so email resolves where phone doesn't.
- **search-user** by NAME — Lark search API, USER identity (user_access_token; OAuth lives in
  `vc_user_oauth.py`); name is AMBIGUOUS → risky for auto-ring. CLI skill `lark-contact` `+search-user`.
- **directory table** — most reliable for a P0 (no ambiguity). Bootstrap once via batch_get_id/search.

## Env (fe / fpms)
```
DUTY_ROSTER_FE_SHEET_TOKEN=B3bYsGn6UhTQixtR1oVlppiIgFg
DUTY_ROSTER_FE_SHEET_ID=<Latest Duty List ?sheet=>
DUTY_ROSTER_FE_RANGE=A:N
DUTY_ROSTER_FPMS_SHEET_TOKEN=F1rRskiOChUiWvts5nTlhVFngSf
DUTY_ROSTER_FPMS_SHEET_ID=1VXmDV
DUTY_ROSTER_FPMS_RANGE=A:AG
DUTY_DIRECTORY_SHEET_TOKEN=...  DUTY_DIRECTORY_SHEET_ID=...  DUTY_DIRECTORY_RANGE=A:C
P0_VC_RING_ENABLED=1
```
**Share the bot** into every sheet, including the directory.

## Add a new roster command (e.g. cpms / pms)
1. Write a pure parser `parse_<x>_duty(rows, today: datetime.date) -> List[str]` in `duty_roster.py`.
2. Register in `_ROSTER`: `"cpms": ("DUTY_ROSTER_CPMS", parse_cpms_duty)`.
3. Add the command to `RING_CMD_RE` in `config.py`.
4. Set `DUTY_ROSTER_CPMS_SHEET_TOKEN/_SHEET_ID/_RANGE`.
`handle_ring_command` already routes any `is_roster_command(c)` through `resolve_duty_open_ids`.

## Testing (one-shot; run on the box that has the bot's app_id/secret)
- `scripts/read_duty_sheet_once.py --spreadsheet <tok> --range '<sid>!A1:..'` — dump raw values to see
  what the API returns for checkboxes / colours / the `2` mark.
- `scripts/resolve_mobile_once.py +60... name@co.com` — phone/email → open_id via batch_get_id.
- `scripts/test_duty_directory_once.py OSE Bryan` — directory name→open_id.

## Gotchas
- `resolve_duty_open_ids` **dedupes by open_id** — a roster full of "OSE" rings once.
- `"OSE"` as a duty NAME = a test placeholder; map `OSE | ou_<test account>` in the directory.
- Uses the **VPS local date** (box is UTC+8 = the rosters' timezone). Keep "Latest Duty List" on the
  current month; `parse_fpms_duty` picks today's month block by the `日期 - <month>` header.
- Never commit the roster `*.xlsx` (real names/phones) — gitignored; read live via the Sheets API.
