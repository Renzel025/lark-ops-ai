#!/usr/bin/env python3
"""
Open Slack in Playwright's bundled Chromium (same engine as slack_huddle_invite_all.py
when CHROME_PATH is unset) — for one-time login into SESSION_DIR.

Usage (e.g. inside VNC):
  export DISPLAY=:1
  export SESSION_DIR=/path/to/slack_profile
  export SLACK_CHANNEL_URL='https://app.slack.com/client/T.../C...'
  unset CHROME_PATH
  python3 scripts/slack_open_login_browser.py

Press Enter in the terminal when done logging in; browser closes.
"""
from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright


def main() -> None:
    session = (os.getenv("SESSION_DIR") or "").strip()
    if not session:
        print("ERROR: SESSION_DIR is required.", file=sys.stderr)
        sys.exit(2)
    url = (os.getenv("SLACK_CHANNEL_URL") or "https://app.slack.com/").strip()
    # Force Playwright's downloaded Chromium, not system CHROME_PATH
    os.environ.pop("CHROME_PATH", None)

    args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
    ]

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            session,
            headless=False,
            viewport={"width": 1366, "height": 768},
            args=args,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url, wait_until="domcontentloaded")
        print("Log in to Slack in the window, then press Enter here to save and exit.")
        try:
            input()
        except EOFError:
            pass
        ctx.close()


if __name__ == "__main__":
    main()
