#!/usr/bin/env python3
"""
One-time Chromium + Playwright to seed a Grafana session into the persistent profile the bot
uses for P0 screenshots (``P0_GRAPH_SCREENSHOT_PLAYWRIGHT_USER_DATA_DIR``).

It reuses the SAME auto-login flow as the capture engine
(``features/screenshot/graph_screenshot._grafana_auto_login_if_needed``): it navigates to the
dashboard, and if Grafana is showing the login page it fills
``P0_GRAPH_SCREENSHOT_USERNAME`` / ``P0_GRAPH_SCREENSHOT_PASSWORD`` and submits — then prints an
explicit success/failure line. On SSO/OAuth or when run headed you can also finish login by hand.

IMPORTANT — stop the service first (profile lock):
  The bot's persistent profile can only be opened by one Chromium at a time. Stop the service
  before seeding, or Chromium will fail to launch on the locked profile:
      sudo systemctl stop lark-ops-ai      # (dev VPS unit is 'lark-ops-ai')
      python3 features/screenshot/scripts/grafana_playwright_login_once.py
      sudo systemctl start lark-ops-ai

Usage (from repo root, same venv as the bot):
  export P0_GRAPH_SCREENSHOT_PLAYWRIGHT_USER_DATA_DIR=/var/lib/lark-ops-ai/grafana-playwright-profile
  export P0_GRAPH_SCREENSHOT_URL='https://grafana.example.com/d/uid/slug?orgId=1'
  export P0_GRAPH_SCREENSHOT_USERNAME='...'   # for automatic login
  export P0_GRAPH_SCREENSHOT_PASSWORD='...'
  mkdir -p "$P0_GRAPH_SCREENSHOT_PLAYWRIGHT_USER_DATA_DIR"
  python3 features/screenshot/scripts/grafana_playwright_login_once.py

Flags:
  --headless   run without a display (works when creds are set; no manual step)
  positional:  [profile_dir] [url]

On a headless server, either set creds and pass ``--headless``, or use SSH -X / VNC for the
headed manual flow, then keep the SAME profile path on the server.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parents[3])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _bootstrap_env() -> None:
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    from p0_logic import config as cfg

    path = cfg.resolve_env_file_path()
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    if os.path.isfile(path):
        try:
            load_dotenv(path, encoding="utf-8", override=True)
        except TypeError:
            load_dotenv(path, override=True)
    cfg.reload_env_runtime()


def main() -> None:
    _bootstrap_env()

    argv = [a for a in sys.argv[1:] if a != "--headless"]
    headless = "--headless" in sys.argv

    if len(argv) >= 2:
        profile = argv[0].strip()
        url = argv[1].strip()
    else:
        profile = (os.getenv("P0_GRAPH_SCREENSHOT_PLAYWRIGHT_USER_DATA_DIR") or "").strip()
        url = (os.getenv("P0_GRAPH_SCREENSHOT_URL") or "").strip()

    if not profile or not url:
        print(
            "Set P0_GRAPH_SCREENSHOT_PLAYWRIGHT_USER_DATA_DIR and P0_GRAPH_SCREENSHOT_URL in .env,\n"
            "or: python3 features/screenshot/scripts/grafana_playwright_login_once.py /path/to/profile 'https://...'",
            file=sys.stderr,
        )
        sys.exit(1)
    if not os.path.isdir(profile):
        print(f"Directory does not exist (create it first): {profile}", file=sys.stderr)
        sys.exit(1)

    from p0_logic import config as cfg

    user = cfg.get_p0_graph_screenshot_username()
    pwd = cfg.get_p0_graph_screenshot_password()
    nav_ms = cfg.get_p0_graph_screenshot_nav_timeout_ms()
    goto_wait = cfg.get_p0_graph_screenshot_goto_wait_until()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Install: pip install playwright && playwright install chromium", file=sys.stderr)
        sys.exit(1)

    # Reuse the SAME login/detect helpers as the capture engine so seeding and runtime behave
    # identically.
    from features.screenshot.graph_screenshot import (
        _grafana_auto_login_if_needed,
        _grafana_login_page_detected,
        _grafana_on_dashboard_page,
    )

    print(
        "Seeding Grafana session into persistent profile:\n"
        f"  profile : {profile}\n"
        f"  url     : {url}\n"
        f"  creds   : {'set (auto-login)' if (user and pwd) else 'NOT set (manual login only)'}\n"
        f"  headless: {headless}\n"
        "NOTE: the bot service must be STOPPED first (profile is single-locked).\n"
    )

    ok = False
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            profile,
            headless=headless,
            viewport={"width": 1280, "height": 720},
            args=["--disable-dev-shm-usage"],
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, wait_until=goto_wait, timeout=nav_ms)

            if user and pwd:
                ok = _grafana_auto_login_if_needed(
                    page, url, nav_ms=nav_ms, goto_wait=goto_wait
                )
            else:
                ok = _grafana_on_dashboard_page(page) and not _grafana_login_page_detected(page)

            if not ok and not headless:
                print(
                    "\nAutomatic login did not reach the dashboard "
                    "(bad creds / SSO / manual step needed).\n"
                    "Finish logging in IN THE BROWSER WINDOW, then press Enter here to save.\n"
                )
                input("Press Enter after login is complete… ")
                ok = _grafana_on_dashboard_page(page) and not _grafana_login_page_detected(page)
        finally:
            ctx.close()

    if ok:
        print("\nSEED OK: Grafana session reached the dashboard and was saved to the profile.")
        print("Restart the service:  sudo systemctl start lark-ops-ai")
        sys.exit(0)
    print(
        "\nSEED FAILED: still on the login page (bad credentials, or SSO/OAuth that this "
        "username/password flow cannot complete).\n"
        "  - Verify P0_GRAPH_SCREENSHOT_USERNAME / P0_GRAPH_SCREENSHOT_PASSWORD.\n"
        "  - If the host uses SSO, re-run WITHOUT --headless (headed) and log in by hand.\n"
        "  - Then restart:  sudo systemctl start lark-ops-ai",
        file=sys.stderr,
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
