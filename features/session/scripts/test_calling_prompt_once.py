#!/usr/bin/env python3
"""One-shot tester for the duty-DM **"Calling <auto-invite names>"** prompt.

This is the plain-text line the bot DMs the duty on a P0 declare, just ABOVE the
ring-guide card — naming the ``P0_VC_AUTO_INVITE_OPEN_IDS`` people being paged into
the VC. It is NOT a ``cards.py`` builder, so it does not appear in
``scripts/post_card_once.py --list`` — hence this dedicated runner.

It (1) prints exactly why the prompt would fire or no-op in production
(gate + resolved names), then (2) optionally posts the real message to a DM so you
can see it in Lark.

Usage:
  # DRY-RUN: show the gate + resolved names + the exact text (sends nothing)
  python3 features/session/scripts/test_calling_prompt_once.py

  # POST the real DM to a user (user_id path = matches prod; reach across apps)
  python3 features/session/scripts/test_calling_prompt_once.py --post --user-id SNT0006

  # POST by open_id instead
  python3 features/session/scripts/test_calling_prompt_once.py --post --open-id ou_XXXX

  # ignore the enabled-gate (post even if ring/prompt is off in this .env)
  python3 features/session/scripts/test_calling_prompt_once.py --post --user-id SNT0006 --force
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[3])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from p0_logic import config
config.apply_env_layers()

from p0_logic import lark_client as lark  # noqa: E402
from features.session.session import _humanize_name_list  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--open-id", default="", help="DM recipient ou_... (the duty)")
    ap.add_argument("--user-id", default="", help="DM recipient tenant user_id (prod path; avoids 99992361)")
    ap.add_argument("--post", action="store_true", help="actually send the DM (default = dry-run)")
    ap.add_argument("--force", action="store_true", help="post even if the enabled-gate is off in this .env")
    args = ap.parse_args()

    print("=" * 72)
    print("Calling <names> DM prompt — one-shot tester")
    print("=" * 72)

    ring_on = config.get_p0_vc_ring_enabled()
    gate_on = config.get_p0_dm_auto_invite_prompt_enabled()
    auto_ids = [o for o in config.get_p0_vc_auto_invite_open_ids() if o]
    text_tpl = config.get_p0_dm_auto_invite_prompt_text()

    print(f"P0_VC_RING_ENABLED               : {ring_on}")
    print(f"P0_DM_AUTO_INVITE_PROMPT_ENABLED : {gate_on}  (needs ring ON + flag != 0)")
    print(f"P0_VC_AUTO_INVITE_OPEN_IDS       : count={len(auto_ids)} -> {[o[-8:] for o in auto_ids]}")
    print(f"P0_DM_AUTO_INVITE_PROMPT_TEXT    : {text_tpl!r}")
    print("-" * 72)

    if not auto_ids:
        print("NO-OP REASON: P0_VC_AUTO_INVITE_OPEN_IDS is EMPTY — there is nobody to 'call', so the")
        print("              prompt never posts. Set that env var (comma/space list of ou_...) first.")
        return 2

    tok = lark.get_tenant_token_primary()
    if not tok:
        print("ERROR: no primary tenant token (check LARK_APP_ID / LARK_APP_SECRET in .env).")
        return 1

    recipient_oid = (args.open_id or "").strip()
    recipient_uid = (args.user_id or "").strip()

    # Resolve display names (exclude the DM recipient themselves, mirroring the real code).
    names = []
    for o in auto_ids:
        if recipient_oid and o == recipient_oid:
            continue
        nm = ""
        try:
            nm = (lark.lookup_user_name_by_open_id(tok, o) or "").strip()
        except Exception as e:  # noqa: BLE001
            print(f"  ! name lookup failed for {o[-8:]}: {e}")
        names.append(nm or f"({o[-6:]})")
        print(f"  resolved {o[-8:]} -> {names[-1]!r}")

    who = _humanize_name_list(names)
    text = text_tpl.replace("{names}", who)
    print("-" * 72)
    print(f"EXACT DM TEXT:\n  {text}")
    print("-" * 72)

    if not gate_on and not args.force:
        print("GATE OFF: in this .env the prompt would NOT fire automatically (ring off or flag=0).")
        print("          Use --force to post it anyway for testing, or fix the env on the box.")
        if not args.post:
            return 0

    if not args.post:
        print("DRY-RUN: nothing sent. Add --post (and --user-id / --open-id) to actually DM it.")
        return 0

    if not recipient_oid and not recipient_uid:
        print("ERROR: --post needs a recipient. Pass --user-id <id> (prod path) or --open-id ou_...")
        return 1

    st, body = lark.post_text_to_user_cross_app(
        recipient_oid, recipient_uid, tok, text, use_user_id=bool(recipient_uid)
    )
    mid = lark.parse_im_message_id_from_response(body or "")
    print(f"POST -> HTTP {st}  message_id={mid or '(none)'}")
    if st == 200 and mid:
        print("OK — check that DM in Lark. This is the exact prompt the bot posts on P0 declare.")
        return 0
    print(f"FAILED body: {(body or '')[:400]}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
