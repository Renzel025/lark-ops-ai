#!/usr/bin/env python3
"""Post a test **Meeting recording ready** card to Lark."""
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
from p0_logic.cards import build_recording_available_card


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat-id", default="", help="oc_... (default: first VC_RECORDING_FANOUT_CHAT_IDS)")
    ap.add_argument("--topic", default="Video meeting—p0 detection dev")
    ap.add_argument("--meeting-no", default="885910443")
    ap.add_argument("--meeting-id", default="7654187624064077532")
    ap.add_argument("--url", default="https://example.com/minutes")
    ap.add_argument("--duration", default="0s")
    args = ap.parse_args()

    oc = (args.chat_id or "").strip()
    if not oc:
        chats = config.get_vc_recording_fanout_chat_ids()
        if not chats:
            print("Set --chat-id=oc_... or VC_RECORDING_FANOUT_CHAT_IDS in .env", file=sys.stderr)
            return 1
        oc = chats[0]

    token = lark.get_tenant_token_primary()
    if not token:
        print("No tenant token — check LARK_APP_ID / LARK_APP_SECRET", file=sys.stderr)
        return 2

    card = build_recording_available_card(
        topic=args.topic,
        meeting_no=args.meeting_no,
        meeting_id=args.meeting_id,
        recording_url=args.url,
        duration_text=args.duration,
    )
    st, body, mid = lark.post_card_to_chat(oc, token, card)
    ok, code, msg = lark.lark_im_message_create_ok(body)
    print(f"posted to {oc} | HTTP {st} | ok {ok} | mid {mid} | {msg}")
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
