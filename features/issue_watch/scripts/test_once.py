#!/usr/bin/env python3
"""Test issue_watch classification + optional DM (dry-run by default)."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[3])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from p0_logic import config
from p0_logic.anthropic_client import anthropic_auth_mode, has_anthropic_auth
from features.issue_watch.issue_watch_ai import classify_issue_watch_message


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("message", nargs="?", default="")
    parser.add_argument(
        "--text",
        default="Hi team kindly help check the CP website on PC its continuously loading.",
    )
    args = parser.parse_args()
    msg = (args.message or args.text).strip()
    print("ENV_PATH:", config.ENV_PATH)
    print("P0_ISSUE_WATCH_ENABLED:", config.get_p0_issue_watch_enabled())
    print("DM recipients:", config.get_dm_instruction_open_ids())
    print("claude auth:", anthropic_auth_mode() or "(none)")
    print("has_anthropic_auth:", has_anthropic_auth())
    print("ANTHROPIC_MODEL:", (os.getenv("ANTHROPIC_MODEL") or "").strip() or "(default)")
    print("ANTHROPIC_OAUTH_MODEL:", (os.getenv("ANTHROPIC_OAUTH_MODEL") or "").strip() or "(auto)")
    print("ANTHROPIC_API_KEY set:", bool((os.getenv("ANTHROPIC_API_KEY") or "").strip()))
    print("---")
    print("MESSAGE:", msg)
    out = classify_issue_watch_message(msg)
    print("RESULT:", out)
    return 0 if out and out.get("is_incident_signal") else 1


if __name__ == "__main__":
    raise SystemExit(main())
