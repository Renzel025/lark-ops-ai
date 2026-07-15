#!/usr/bin/env python3
"""One-shot: DM the duty the **P0 ongoing DM buzz** card (Major 5-min / Minor 10-min).

This bypasses the live-session timer so you can test the DM without declaring a P0
and waiting 5/10 minutes. It sends the same card the real buzz sends, to the same
recipients (``P0_DM_INSTRUCTION_OPEN_IDS``), optionally with Lark urgent (buzz).

Examples:
  python3 features/session/scripts/post_ongoing_buzz_once.py --tier major --post
  python3 features/session/scripts/post_ongoing_buzz_once.py --tier minor --post --open-id ou_xxx
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

from p0_logic import lark_client as lark
from p0_logic.cards import build_p0_ongoing_dm_buzz_card


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", choices=["major", "minor"], default="minor",
                    help="major = 5-min buzz, minor = 10-min buzz (default minor)")
    ap.add_argument("--chat-name", default="P0 detection dev", help="source group label shown on the card")
    ap.add_argument("--meeting-no", default="885910443")
    ap.add_argument("--open-id", action="append", default=[],
                    help="ou_... recipient (repeatable). Default: P0_DM_INSTRUCTION_OPEN_IDS")
    ap.add_argument("--post", action="store_true", help="actually send (otherwise dry-run prints the card)")
    ap.add_argument("--no-urgent", action="store_true", help="skip Lark urgent (buzz), just send the card")
    args = ap.parse_args()

    tier = args.tier
    delay_sec = (
        config.get_p0_ongoing_dm_buzz_major_delay_sec()
        if tier == "major"
        else config.get_p0_ongoing_dm_buzz_minor_delay_sec()
    )
    buzz_min = max(1, int(delay_sec) // 60)
    duration_text = f"{buzz_min} minute" if buzz_min == 1 else f"{buzz_min} minutes"

    card = build_p0_ongoing_dm_buzz_card(
        source_chat_label=args.chat_name,
        meeting_no=args.meeting_no,
        duration_text=duration_text,
        contact_names=config.get_p0_ongoing_contact_names(),
        severity_tier=tier,
    )

    targets = [x.strip() for x in args.open_id if x.strip()] or config.get_dm_instruction_open_ids()
    if not targets:
        print("No recipients — pass --open-id ou_... or set P0_DM_INSTRUCTION_OPEN_IDS", file=sys.stderr)
        return 1

    print(f"tier={tier} delay_sec={delay_sec} duration={duration_text!r} recipients={len(targets)}")
    if not args.post:
        print("DRY RUN (no --post). Recipients:", ", ".join(t[-12:] for t in targets))
        return 0

    token = lark.get_tenant_token_primary()
    if not token:
        print("No primary tenant token — check LARK_APP_ID/SECRET in .env", file=sys.stderr)
        return 1

    urgent_mode = "off" if args.no_urgent else config.get_p0_ongoing_lark_urgent_mode()
    sent = 0
    for oid in targets:
        st, body, mid = lark.post_card_to_open_id(oid, token, card)
        if st != 200:
            print(f"  FAILED open_id_tail={oid[-12:]} HTTP={st} body={(body or '')[:200]}", file=sys.stderr)
            continue
        sent += 1
        print(f"  sent open_id_tail={oid[-12:]} message_id={mid}")
        if urgent_mode != "off" and mid:
            uok, udetail = lark.urgent_message_for_users(token, mid, [oid], mode=urgent_mode)
            print(f"    urgent_{urgent_mode}={'ok' if uok else 'FAILED ' + (udetail or '')[:150]}")
    print(f"DONE sent={sent}/{len(targets)}")
    return 0 if sent else 1


if __name__ == "__main__":
    raise SystemExit(main())
