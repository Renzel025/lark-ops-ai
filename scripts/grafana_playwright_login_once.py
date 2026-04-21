#!/usr/bin/env python3
"""
One-time **headed** Chromium + Playwright so you can log in to Grafana (or any dashboard).
Saves cookies/session under ``P0_GRAPH_SCREENSHOT_PLAYWRIGHT_USER_DATA_DIR`` — the same path
the bot uses for P0 screenshots.

Usage (from repo root, same venv as the bot):
  export P0_GRAPH_SCREENSHOT_PLAYWRIGHT_USER_DATA_DIR=/var/lib/lark-ops-ai/grafana-playwright-profile
  export P0_GRAPH_SCREENSHOT_URL='https://grafana.example.com/d/uid/slug?orgId=1'
  mkdir -p "$P0_GRAPH_SCREENSHOT_PLAYWRIGHT_USER_DATA_DIR"
  python3 scripts/grafana_playwright_login_once.py

Or:
  python3 scripts/grafana_playwright_login_once.py /path/to/profile 'https://grafana...'

On a **headless server**, use SSH -X, VNC, or run this once on a machine with a display, then copy
the profile directory to the server (same path recommended).
"""
from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        env_path = os.path.join(_REPO_ROOT, ".env")
        if os.path.isfile(env_path):
            load_dotenv(env_path)
    except Exception:
        pass


def main() -> None:
    _load_dotenv()
    if len(sys.argv) >= 3:
        profile = sys.argv[1].strip()
        url = sys.argv[2].strip()
    else:
        profile = (os.getenv("P0_GRAPH_SCREENSHOT_PLAYWRIGHT_USER_DATA_DIR") or "").strip()
        url = (os.getenv("P0_GRAPH_SCREENSHOT_URL") or "").strip()

    if not profile or not url:
        print(
            "Set P0_GRAPH_SCREENSHOT_PLAYWRIGHT_USER_DATA_DIR and P0_GRAPH_SCREENSHOT_URL in .env,\n"
            "or: python3 scripts/grafana_playwright_login_once.py /path/to/profile 'https://...'",
            file=sys.stderr,
        )
        sys.exit(1)
    if not os.path.isdir(profile):
        print(f"Directory does not exist (create it first): {profile}", file=sys.stderr)
        sys.exit(1)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Install: pip install playwright && playwright install chromium", file=sys.stderr)
        sys.exit(1)

    print("Opening browser — log in to Grafana, then press Enter here to save and exit.\n")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            profile,
            headless=False,
            viewport={"width": 1280, "height": 720},
            args=["--disable-dev-shm-usage"],
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, wait_until="load", timeout=120_000)
            input("Press Enter after login is complete… ")
        finally:
            ctx.close()
    print("Done. The bot can now use this profile for headless screenshots.")


if __name__ == "__main__":
    main()
