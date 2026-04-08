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

from playwright.sync_api import BrowserContext, Page, sync_playwright


def _pick_page_for_slack(ctx: BrowserContext) -> Page:
    """Prefer Slack tab; else first tab (goto loads URL). Do not close all blanks — new_page() may fail on VNC."""
    pages = list(ctx.pages)
    for pg in pages:
        u = (pg.url or "").lower()
        if "slack.com" in u and "about:blank" not in u:
            try:
                pg.bring_to_front()
            except Exception:
                pass
            return pg
    if pages:
        page = pages[0]
        try:
            page.bring_to_front()
        except Exception:
            pass
        return page
    try:
        return ctx.new_page()
    except Exception as e:
        raise RuntimeError(
            "No tab and new_page() failed — check DISPLAY in VNC. Underlying: " + str(e)
        ) from e


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
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
        "--autoplay-policy=no-user-gesture-required",
    ]

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            session,
            headless=False,
            viewport={"width": 1366, "height": 768},
            args=args,
        )
        for origin in ("https://app.slack.com", "https://slack.com"):
            try:
                ctx.grant_permissions(
                    ["camera", "microphone", "notifications"],
                    origin=origin,
                )
            except Exception as e:
                print(f"WARN: grant_permissions ({origin}): {e}", file=sys.stderr)
        page = _pick_page_for_slack(ctx)
        page.goto(url, wait_until="domcontentloaded", timeout=120000)
        print(f"Loaded URL: {page.url}", flush=True)
        print("Log in to Slack in the window, then press Enter here to save and exit.")
        try:
            input()
        except EOFError:
            pass
        ctx.close()


if __name__ == "__main__":
    main()
