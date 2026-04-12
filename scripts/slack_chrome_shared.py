"""Shared Chrome CLI flags for Slack Playwright scripts (login + huddle).

Keep launch args identical between slack_open_login_browser.py and
slack_huddle_invite_all.py so the persistent profile behaves the same.
"""
from __future__ import annotations

import os
import sys


def default_slack_chrome_user_agent_string() -> str:
    """Retail-like UA for Slack Playwright; OS-specific default avoids Windows UA on macOS/Linux.

    Mismatched UA vs. real Chrome/OS is a common trigger for Slack/Cloudflare challenges.
    Override with SLACK_CHROME_USER_AGENT or disable with SLACK_CHROME_USER_AGENT_DISABLE=1.
    """
    if sys.platform == "darwin":
        return (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
    if sys.platform.startswith("linux"):
        return (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )


def env_truthy(name: str) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def slack_chrome_launch_args(*, headless: bool | None = None) -> list[str]:
    """Chrome args for Slack automation. If ``headless`` is None, read SLACK_HEADLESS / HEADLESS."""
    if headless is None:
        headless = env_truthy("SLACK_HEADLESS") or env_truthy("HEADLESS")
    disable_gpu = headless or env_truthy("SLACK_CHROME_DISABLE_GPU")

    args: list[str] = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--window-size=1366,768",
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
        "--autoplay-policy=no-user-gesture-required",
        "--disable-notifications",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-popup-blocking",
        "--disable-extensions",
        "--disable-sync",
        "--disable-background-networking",
        "--password-store=basic",
        "--use-mock-keychain",
        "--disable-blink-features=AutomationControlled",
    ]
    if not env_truthy("SLACK_CHROME_OMIT_DISABLE_SETUID_SANDBOX"):
        args.insert(1, "--disable-setuid-sandbox")
    if disable_gpu:
        args[3:3] = [
            "--disable-gpu",
            "--disable-software-rasterizer",
        ]
    return args
