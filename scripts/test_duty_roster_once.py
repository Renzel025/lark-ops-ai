#!/usr/bin/env python3
"""One-shot: read a duty roster (fe/fpms) LIVE and print the raw rows + today's parsed duty names.

No P0 meeting or @bot mention needed — direct output to stdout. Run on the box with the bot's
app_id/secret + the DUTY_ROSTER_<X>_SHEET_* env set, and the bot shared on the sheet.

  python3 scripts/test_duty_roster_once.py fe
  python3 scripts/test_duty_roster_once.py fpms
"""
from __future__ import annotations

import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from p0_logic import config as _config  # noqa: E402
from p0_logic import lark_client as _lark  # noqa: E402
from features.recording import duty_roster as _dr  # noqa: E402


def main() -> None:
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "fe").strip().lower()
    _config.reload_env_runtime()
    token = _lark.get_tenant_token_primary()
    if not token:
        print("ERROR: no tenant token (check app_id/secret in env)")
        return
    reg = _dr._ROSTER.get(cmd)
    if not reg:
        print(f"unknown roster '{cmd}' — use one of: {list(_dr._ROSTER)}")
        return
    prefix, parser = reg
    sheet_token, sheet_id, sheet_name, rng = _dr._roster_env(prefix)
    print(f"cmd={cmd}  token_tail={sheet_token[-8:] if sheet_token else '(none)'}  "
          f"sheet_id={sheet_id or '(auto)'}  sheet_name={sheet_name or '-'}  range={rng}")
    if not sheet_token:
        print(f"ERROR: {prefix}_SHEET_TOKEN not set in .env")
        return
    if not sheet_id:
        sheet_id = _lark.resolve_sheet_id(token, sheet_token, sheet_name)
        print(f"resolved sheet_id: {sheet_id or '(FAILED — bot not shared / no sheets scope?)'}")
        if not sheet_id:
            return
    rows, err = _lark.read_sheets_values_batch(token, sheet_token, f"{sheet_id}!{rng}")
    print(f"read err={err!r}  rows={len(rows)}")
    for i, r in enumerate(rows[:14], 1):
        cells = [("·" if x in (None, "") else str(x))[:12] for x in (r or [])[:16]]
        print(f"  r{i:>2}:", " | ".join(cells))
    print("\nPARSED today =", parser(rows, datetime.date.today()))


if __name__ == "__main__":
    main()
