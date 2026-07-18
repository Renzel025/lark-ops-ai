#!/usr/bin/env python3
"""One-shot: read a Lark Sheet range and dump the RAW cell values the values API returns.

Purpose: settle exactly what the Sheets values API gives back for the duty rosters —
- FPMS color-coded cells: does a GREEN (colour-only) cell return a value, or blank?
  Does the YELLOW "2" cell return "2"?
- OSE SRE checkbox cells: 1/0 vs TRUE/FALSE vs blank?
- PMS: plain text (First Level name) — should read cleanly.

The values API returns cell TEXT/NUMBERS only — NOT fill colour. This script shows the
truth so we design the parser against reality, not a guess.

Run on a box where the bot's Lark app_id/secret are in the env (dev box):

  # FPMS 2026 July block (sheet_id = 1VXmDV from ?sheet= in the URL)
  python3 scripts/read_duty_sheet_once.py \
    --spreadsheet F1rRskiOChUiWvts5nTlhVFngSf \
    --range '1VXmDV!A153:AF177'

  # PMS 2026 (sheet_id 1cPvzX)
  python3 scripts/read_duty_sheet_once.py \
    --spreadsheet LRBmswY7whi9LttJXMVlVozigkh --range '1cPvzX!A1:H55'

  # OSE FINAL OSE & QA MERGE, SRE PLATFORM block (sheet_id 0phcuL)
  python3 scripts/read_duty_sheet_once.py \
    --spreadsheet BJWCsAB0zhYm8OtxTL5l1EkOgbb --range '0phcuL!A78:GR106'
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from p0_logic import config as _config  # noqa: E402
from p0_logic import lark_client as _lark  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spreadsheet", required=True, help="spreadsheet_token (from the sheet URL)")
    ap.add_argument("--range", required=True, help="'<sheet_id>!A1:Z100' — sheet_id = the ?sheet= value in the URL")
    ap.add_argument("--maxrows", type=int, default=30, help="how many rows to print")
    ap.add_argument("--maxcols", type=int, default=34, help="how many cols per row to print")
    args = ap.parse_args()

    _config.reload_env_runtime()
    token = _lark.get_tenant_token_primary()
    if not token:
        print("ERROR: no tenant token — check the Lark app_id/secret in this box's env.")
        return

    rows, err = _lark.read_sheets_values_batch(token, args.spreadsheet, args.range)
    if err:
        print("API error:", err)
    print(f"got {len(rows)} rows from {args.spreadsheet} range={args.range}\n")
    # '·' marks a blank/None cell so colour-only cells are obvious; real values print as repr().
    for i, r in enumerate(rows[: args.maxrows], 1):
        cells = [("·" if c in (None, "") else repr(c)) for c in (r or [])]
        print(f"r{i:>3}:", " ".join(cells[: args.maxcols]))


if __name__ == "__main__":
    main()
