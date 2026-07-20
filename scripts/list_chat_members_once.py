#!/usr/bin/env python3
"""One-shot: list a Lark group's members as name -> open_id (to bulk-build the duty directory).

Add the bot to the group, then run this with the group's chat_id (oc_...). Paste the output's
Name / open_id columns straight into the directory sheet.

  python3 scripts/list_chat_members_once.py oc_YOUR_GROUP_CHAT_ID
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from p0_logic import config as _config  # noqa: E402
from p0_logic import lark_client as _lark  # noqa: E402


def main() -> None:
    chat_id = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    if not chat_id:
        print("usage: list_chat_members_once.py oc_YOUR_GROUP_CHAT_ID")
        return
    _config.reload_env_runtime()
    token = _lark.get_tenant_token_primary()
    if not token:
        print("ERROR: no tenant token — check the Lark app_id/secret in this box's env.")
        return
    members = _lark.list_chat_members(token, chat_id)
    print(f"{len(members)} members (Name<TAB>open_id — paste into the directory sheet):\n")
    for name, oid in members:
        print(f"{name}\t{oid}")
    if not members:
        print("(none) — is the bot IN this group, and does it have an im chat-read scope?")


if __name__ == "__main__":
    main()
