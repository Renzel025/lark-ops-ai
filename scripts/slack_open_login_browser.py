#!/usr/bin/env python3
"""
Open Slack for one-time login into SESSION_DIR (run before slack_huddle_invite_all.py).

SPEC: Use the SAME SESSION_DIR and CHROME_PATH (if any) as the huddle script. Same venv:
  python -m pip install -r scripts/requirements-huddle.txt

Uses the same launch flags as slack_huddle_invite_all.py (ignore --enable-automation,
stealth, UA) so the saved profile matches automation. CHROME_PATH = system Chrome is
recommended if huddle automation uses it (see env.example).

Usage (e.g. inside VNC):
  export DISPLAY=:1
  export SESSION_DIR=/path/to/slack_profile
  export SLACK_CHANNEL_URL='https://app.slack.com/client/T.../C...'
  # optional: export CHROME_PATH=/usr/bin/google-chrome   # or: $(command -v google-chrome-stable)
  python3 scripts/slack_open_login_browser.py

Press Enter in the terminal when done logging in; browser closes.
"""
from __future__ import annotations

import os
import sys

from playwright.sync_api import BrowserContext, Page, sync_playwright

from slack_chrome_shared import slack_chrome_launch_args

_DEFAULT_SLACK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def _slack_user_agent_for_launch() -> str | None:
    if os.getenv("SLACK_CHROME_USER_AGENT_DISABLE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return None
    ua = (os.getenv("SLACK_CHROME_USER_AGENT") or "").strip()
    return ua if ua else _DEFAULT_SLACK_USER_AGENT


def _maybe_apply_stealth_sync(ctx: BrowserContext) -> None:
    if os.getenv("SLACK_PLAYWRIGHT_STEALTH_DISABLE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return
    try:
        from playwright_stealth import Stealth
    except ImportError:
        print(
            "WARN: pip install playwright-stealth for anti-detection hooks.",
            file=sys.stderr,
        )
        return
    try:
        Stealth().apply_stealth_sync(ctx)
        print("playwright-stealth: applied to persistent context.", flush=True)
    except Exception as e:
        print(f"WARN: playwright-stealth apply failed: {e}", file=sys.stderr)


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
    chrome_path = (os.getenv("CHROME_PATH") or "").strip() or None

    # Same CLI flags as slack_huddle_invite_all.py (see scripts/slack_chrome_shared.py).
    # headless=False; SLACK_CHROME_DISABLE_GPU still honored if you need to test software raster.
    args = slack_chrome_launch_args(headless=False)

    with sync_playwright() as p:
        ctx_kw: dict = {
            "user_data_dir": session,
            "headless": False,
            "viewport": {"width": 1366, "height": 768},
            "args": args,
            # Selenium: excludeSwitches=["enable-automation"] → ignore Playwright's default --enable-automation
            "ignore_default_args": ["--enable-automation"],
            "chromium_sandbox": False,
        }
        if chrome_path:
            ctx_kw["executable_path"] = chrome_path
        _ua = _slack_user_agent_for_launch()
        if _ua:
            ctx_kw["user_agent"] = _ua
        ctx = p.chromium.launch_persistent_context(**ctx_kw)
        _maybe_apply_stealth_sync(ctx)
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
