#!/usr/bin/env python3
"""Test issue_watch classification + optional DM (dry-run by default)."""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from p0_logic import config

config.apply_env_layers()

from p0_logic.issue_watch_ai import classify_issue_watch_message


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
    print("ANTHROPIC_API_KEY set:", bool((os.getenv("ANTHROPIC_API_KEY") or "").strip()))
    print("GROQ_API_KEY set:", bool((os.getenv("GROQ_API_KEY") or "").strip()))
    print("P0_ISSUE_WATCH_AI_PROVIDER:", os.getenv("P0_ISSUE_WATCH_AI_PROVIDER") or "auto")
    print("---")
    print("MESSAGE:", msg)
    out = classify_issue_watch_message(msg)
    print("RESULT:", out)
    return 0 if out and out.get("is_incident_signal") else 1


if __name__ == "__main__":
    raise SystemExit(main())
