#!/usr/bin/env python3
"""One-shot: resolve mobile number(s) to Lark open_id via contact/v3/users/batch_get_id.

Proves the duty-roster bridge: the sheet has a person's NAME + PHONE (no open_id), and the bot
needs the open_id to invite + ring them in VC. This turns a phone into an open_id. Once it works,
the flow is: parse sheet -> name -> phone -> open_id -> invite to meeting + ring on Lark.

Requires the bot's ``contact:user.id:readonly`` scope (+ published contact data range) on this box.
Use E.164 format (+countrycode, no spaces/dashes): +60162000168 (MY), +639296694545 (PH).

  python3 scripts/resolve_mobile_once.py +60162000168 +639296694545
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from p0_logic import config as _config  # noqa: E402
from p0_logic import lark_client as _lark  # noqa: E402


def main() -> None:
    mobiles = [a.strip() for a in sys.argv[1:] if a.strip()]
    if not mobiles:
        print("usage: resolve_mobile_once.py +60162000168 [+639296694545 ...]")
        return
    _config.reload_env_runtime()
    token = _lark.get_tenant_token_primary()
    if not token:
        print("ERROR: no tenant token — check the Lark app_id/secret in this box's env.")
        return
    res = _lark.batch_get_id_by_mobile(token, mobiles)
    print(f"resolved {len(res)}/{len(mobiles)}:")
    for m in mobiles:
        print(f"  {m} -> {res.get(m) or '(no match)'}")
    if not res:
        print(
            "\n(nothing resolved) check: contact:user.id:readonly scope published, contact data\n"
            "range set (else 41050), and the number is the account's registered mobile in +CC format."
        )


if __name__ == "__main__":
    main()
