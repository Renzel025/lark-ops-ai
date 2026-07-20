#!/usr/bin/env python3
"""One-shot: diagnose the SRE duty ring (/scpms /sfpms /sfe /spms) end-to-end on the box.

Prints every step so a "No duty SRE X configured" reply can be pinpointed:
  1. env values it read
  2. the SRE handler tab (Name -> {team tokens})
  3. per command: the matched team names, then their resolved open_ids

  python3 scripts/test_sre_duty_once.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from p0_logic import config as _config  # noqa: E402
from p0_logic import lark_client as _lark  # noqa: E402
from features.recording import duty_directory as _dir  # noqa: E402
from features.recording import duty_roster as _duty  # noqa: E402


def main() -> None:
    _config.reload_env_runtime()
    print("=== env ===")
    for k in (
        "DUTY_DIRECTORY_SHEET_TOKEN", "DUTY_DIRECTORY_SHEET_ID", "DUTY_DIRECTORY_SHEET_NAME",
        "DUTY_DIRECTORY_RANGE", "DUTY_DIRECTORY_SRE_SHEET_ID", "DUTY_DIRECTORY_SRE_RANGE",
        "DUTY_SRE_SHIFT_SHEET_TOKEN",
    ):
        print(f"  {k} = {os.getenv(k) or '(unset)'}")

    token = _lark.get_tenant_token_primary()
    if not token:
        print("\nERROR: no tenant token — check the Lark app_id/secret in this box's env.")
        return

    print("\n=== SRE handler tab (Name -> teams) ===")
    hmap = _dir.get_sre_handler_map(token)
    if not hmap:
        print("  (EMPTY) — bot not shared on the directory sheet, wrong DUTY_DIRECTORY_SRE_SHEET_ID,")
        print("           or the SRE tab has no 'Name'+'Handler' header row. See the WARNING logs above.")
    for nm, toks in hmap.items():
        print(f"  {nm!r} -> {sorted(toks)}")

    print("\n=== OpenID directory (name -> open_id) size ===")
    dmap = _dir.get_directory(token)
    print(f"  {len(dmap)} entries; e.g. {list(dmap.items())[:3]}")

    print("\n=== per command ===")
    for cmd in ("scpms", "sfpms", "sfe", "spms"):
        names = _duty.sre_team_names(cmd, hmap)  # no shift filter (test posture)
        oids, unresolved = _duty.resolve_sre_duty_open_ids(cmd, token)
        print(f"  /{cmd}: team_names={names}  open_ids={oids}  unresolved={unresolved}")


if __name__ == "__main__":
    main()
