#!/usr/bin/env python3
"""One-shot: resolve a mobile OR email to Lark open_id via contact/v3/users/batch_get_id.

Proves the duty-roster bridge: the sheet has a person's NAME + PHONE (accounts may instead be
registered by EMAIL) but no open_id, and the bot needs the open_id to invite + ring them in VC.
The phone/email is only a LOOKUP KEY; the call/invite is 100% via the Lark open_id.

Args are routed automatically: anything with '@' is treated as an email, otherwise a mobile.
Mobiles use E.164 (+countrycode, no spaces/dashes). Requires contact:user.id:readonly on this box.

  python3 scripts/resolve_mobile_once.py +60162000168 bryan@casinoplus.com you@company.com
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from p0_logic import config as _config  # noqa: E402
from p0_logic import lark_client as _lark  # noqa: E402


def main() -> None:
    args = [a.strip() for a in sys.argv[1:] if a.strip()]
    if not args:
        print("usage: resolve_mobile_once.py +60162000168 name@company.com ...")
        return
    emails = [a for a in args if "@" in a]
    mobiles = [a for a in args if "@" not in a]

    _config.reload_env_runtime()
    token = _lark.get_tenant_token_primary()
    if not token:
        print("ERROR: no tenant token — check the Lark app_id/secret in this box's env.")
        return

    res = {}
    if mobiles:
        res.update(_lark.batch_get_id_by_mobile(token, mobiles))
    if emails:
        res.update(_lark.batch_get_id_by_email(token, emails))

    print(f"resolved {len(res)}/{len(args)}:")
    for a in args:
        print(f"  {a} -> {res.get(a) or '(no match)'}")
    if not res:
        print(
            "\n(nothing resolved) check: contact:user.id:readonly published, contact data range set\n"
            "(else 41050), and the value matches the account exactly (mobile in +CC format, or the\n"
            "registered email). If phone fails but email works, the accounts are email-registered."
        )


if __name__ == "__main__":
    main()
