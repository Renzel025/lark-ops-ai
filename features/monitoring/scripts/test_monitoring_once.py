#!/usr/bin/env python3
"""Post a test alert to P0_MONITORING_CHAT_IDS."""
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
from p0_logic import monitoring_notify as mon


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=("duty", "log"), default="duty")
    args = ap.parse_args()

    chats = config.get_p0_monitoring_chat_ids()
    print("P0_MONITORING_CHAT_IDS:", chats or "(empty)")
    if not chats:
        print("Set P0_MONITORING_CHAT_IDS=oc_... in .env", file=sys.stderr)
        return 1

    token = lark.get_tenant_token_primary()
    if not token:
        print("No tenant token", file=sys.stderr)
        return 2

    if args.kind == "log":
        n = mon.post_log_alert(
            token,
            "Manual test — monitoring log alert",
            level="ERROR",
            logger_name="test",
            dedupe_key="manual_test_log",
        )
    else:
        n = mon.mirror_duty_text(
            token,
            "⚠️ Manual test — overview send blocked mirror",
            duty_open_id="ou_manual_test",
            label="overview send blocked",
        )
    print("posted to", n, "group(s)")
    return 0 if n else 3


if __name__ == "__main__":
    raise SystemExit(main())
