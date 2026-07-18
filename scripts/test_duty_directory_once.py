#!/usr/bin/env python3
"""One-shot: load the duty directory sheet and print the name -> open_id map.

Proves the bridge: duty-sheet name -> directory -> open_id. First set in env:
  DUTY_DIRECTORY_SHEET_TOKEN, DUTY_DIRECTORY_SHEET_ID (the ?sheet= in the URL), DUTY_DIRECTORY_RANGE
and share the directory sheet with the bot. Then:

  python3 scripts/test_duty_directory_once.py               # dump the whole directory
  python3 scripts/test_duty_directory_once.py Bryan Ramel   # also resolve specific roster names
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from p0_logic import config as _config  # noqa: E402
from p0_logic import lark_client as _lark  # noqa: E402
from features.recording import duty_directory as _dir  # noqa: E402


def main() -> None:
    _config.reload_env_runtime()
    token = _lark.get_tenant_token_primary()
    if not token:
        print("ERROR: no tenant token — check the Lark app_id/secret in this box's env.")
        return

    names = [a.strip() for a in sys.argv[1:] if a.strip()]
    mp = _dir.get_directory(token)
    print(f"directory has {len(mp)} name->open_id entries:")
    for k, v in sorted(mp.items()):
        print(f"  {k} -> {v}")
    if not mp:
        print("  (empty) check DUTY_DIRECTORY_SHEET_TOKEN/_SHEET_ID/_RANGE, bot share, and headers")

    if names:
        print("\nlookups (as a roster parser would call):")
        for n in names:
            print(f"  {n!r} -> {_dir.resolve_open_id_for_name(token, n) or '(not in directory)'}")


if __name__ == "__main__":
    main()
