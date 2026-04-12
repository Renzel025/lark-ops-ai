#!/usr/bin/env python3
"""
Quick check: same Slack Web API path as p0_logic/slack_bridge.py (auth.test + chat.postMessage).

Usage (server, same .env as the bot):
  cd /root/lark-ops-ai
  set -a && source .env && set +a
  ./.venv/bin/python scripts/test_slack_notify.py

Or pass channel explicitly:
  SLACK_TEST_CHANNEL=C0AR3JYATQF ./.venv/bin/python scripts/test_slack_notify.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import requests


def main() -> None:
    os.chdir(_REPO)
    try:
        from dotenv import load_dotenv

        p = (os.getenv("ENV_PATH") or "").strip() or str(_REPO / ".env")
        load_dotenv(Path(p), encoding="utf-8", override=True)
    except Exception:
        pass

    token = (os.getenv("SLACK_BOT_TOKEN") or os.getenv("SLACK_BOT_USER_OAUTH_TOKEN") or "").strip()
    raw_map = (os.getenv("SLACK_API_CHANNEL_MAP") or "").strip()
    print("SLACK_API_CHANNEL_MAP len:", len(raw_map), "(0 means missing — bot will skip chat.postMessage)")
    if raw_map:
        from p0_logic.config import _parse_incident_keyed_url_map

        keys = list(_parse_incident_keyed_url_map(raw_map).keys())
        print("parsed oc_ keys in SLACK_API_CHANNEL_MAP:", keys)

    ch = (os.getenv("SLACK_TEST_CHANNEL") or "").strip()
    if not ch:
        for seg in raw_map.split(","):
            seg = seg.strip()
            if "=" in seg:
                _, _, v = seg.partition("=")
                ch = v.strip()
                break

    print("SLACK_BOT_TOKEN len:", len(token), "(0 means missing)")
    print("channel for test:", ch or "(set SLACK_TEST_CHANNEL=C... or fix SLACK_API_CHANNEL_MAP)")

    if not token:
        print("FATAL: no SLACK_BOT_TOKEN")
        sys.exit(2)
    if not ch:
        print("FATAL: no channel id")
        sys.exit(2)

    h = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    r = requests.post("https://slack.com/api/auth.test", headers=h, json={}, timeout=15)
    print("auth.test:", json.dumps(r.json(), indent=2)[:800])

    r2 = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers=h,
        json={"channel": ch, "text": "test_slack_notify.py — delete this message"},
        timeout=30,
    )
    d = r2.json()
    print("chat.postMessage:", json.dumps(d, indent=2)[:800])
    if not d.get("ok"):
        err = d.get("error")
        print("\nIf error=not_in_channel → /invite @YourBot in that channel (or channels:join + join API).")
        if err == "channel_not_found":
            print("If channel_not_found → wrong workspace or wrong C id.")
        sys.exit(1)
    print("\nOK — message should appear in the Slack channel.")


if __name__ == "__main__":
    main()
