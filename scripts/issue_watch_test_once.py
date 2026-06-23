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

from p0_logic.issue_watch_ai import classify_issue_watch_message, is_maintenance_or_test_message


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
    print("P0_ISSUE_WATCH_MIN_CONFIDENCE:", config.get_p0_issue_watch_min_confidence())
    print("P0_ISSUE_WATCH_MIN_SOLO_REPORTERS:", config.get_p0_issue_watch_min_solo_reporters())
    print("---")
    print("MESSAGE:", msg)
    if is_maintenance_or_test_message(msg):
        print("MAINTENANCE_GUARD: would skip (no alert)")
    out = classify_issue_watch_message(msg)
    print("RESULT:", out)
    if out and out.get("is_incident_signal"):
        conf = float(out.get("confidence") or 0)
        min_conf = config.get_p0_issue_watch_min_confidence()
        min_solo = config.get_p0_issue_watch_min_solo_reporters()
        would_alert = conf >= min_conf and min_solo <= 1
        print(
            f"ALERT_GATE: conf={conf:.2f} min_conf={min_conf} "
            f"→ solo alert only if reporters>={min_solo} (this test assumes 1 reporter)"
        )
        print("WOULD_MAJOR_ALERT:", would_alert)
    return 0 if out and out.get("is_incident_signal") else 1


if __name__ == "__main__":
    raise SystemExit(main())
