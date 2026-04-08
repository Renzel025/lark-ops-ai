"""
Optional Lark incident group → Slack: headless Playwright huddle automation + overview webhook mirror.

Triggered from ``session.start_p0`` (optional; see ``SLACK_HUDDLE_ON_P0_START``) and from
``handlers.send_preview`` after a successful \"Send overview\" to Lark (optional;
``SLACK_HUDDLE_ON_OVERVIEW_SEND``). Overview text to Slack uses Incoming Webhooks
(``SLACK_OVERVIEW_WEBHOOK_MAP``) — no Slack bot token; server POSTs from lark-ops-ai.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from . import config as _config

log = logging.getLogger("lark-ops-ai")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SLACK_SCRIPT = _REPO_ROOT / "scripts" / "slack_huddle_invite_all.py"
# Keep subprocess log file objects alive so the child can keep writing after Popen returns.
_SLACK_PLAYWRIGHT_LOG_HANDLES: list[object] = []


def _playwright_subprocess_log_path() -> Path:
    """Where stdout/stderr of slack_huddle_invite_all.py are appended (tail this to debug failures)."""
    raw = (os.getenv("SLACK_PLAYWRIGHT_LOG") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (_REPO_ROOT / "logs" / "slack_huddle_playwright.log").resolve()


def _truthy_env(name: str) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def post_overview_to_slack_webhook(webhook_url: str, markdown: str) -> bool:
    """
    POST overview text to a Slack Incoming Webhook. Returns True on HTTP 200.
    """
    url = (webhook_url or "").strip()
    if not url:
        return False
    body = (markdown or "").strip()
    if not body:
        return False
    # Incoming webhooks: keep under Slack's practical limits
    if len(body) > 38000:
        body = body[:37900] + "\n\n…(truncated for Slack)"
    payload: Dict[str, Any] = {"text": body}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return int(getattr(resp, "status", 200) or 200) == 200
    except urllib.error.HTTPError as e:
        log.warning("Slack overview webhook HTTP error %s: %s", e.code, (e.read() or b"")[:400])
        return False
    except Exception as e:
        log.warning("Slack overview webhook failed: %s", e)
        return False


def enqueue_slack_huddle_automation(incident_chat_id: str, priority: str) -> None:
    """
    Fire-and-forget subprocess: ``scripts/slack_huddle_invite_all.py`` with env from config.

    No-op if automation disabled, unmapped incident chat, or missing channel URL / session dir.
    """
    if not _config.slack_automation_enabled():
        return
    cid = (incident_chat_id or "").strip()
    channel_url = _config.get_slack_channel_url_for_incident_chat(cid)
    session_dir = _config.get_slack_session_dir_for_incident_chat(cid)
    if not channel_url or not session_dir:
        return
    if not _SLACK_SCRIPT.is_file():
        log.warning("Slack huddle script missing: %s", _SLACK_SCRIPT)
        return
    env = os.environ.copy()
    env["SESSION_DIR"] = session_dir
    env["SLACK_CHANNEL_URL"] = channel_url
    env["SLACK_HEADLESS"] = "1"
    # Optional passthroughs (operators set in .env)
    for key in (
        "CHROME_PATH",
        "SCREENSHOT_DIR",
        "HARD_TIMEOUT_MS",
        "HEADLESS",
    ):
        v = (os.getenv(key) or "").strip()
        if v:
            env[key] = v
    pr = (priority or "P0").strip().upper()
    log_path = _playwright_subprocess_log_path()
    log.info(
        "enqueue_slack_huddle_automation chat_id=%s priority=%s session_dir=%s log=%s",
        cid,
        pr,
        session_dir,
        log_path,
    )
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lf = open(log_path, "a", encoding="utf-8", buffering=1)
        _SLACK_PLAYWRIGHT_LOG_HANDLES.append(lf)
        lf.write(f"\n===== {ts} chat_id={cid} priority={pr} =====\n")
        lf.flush()
        # close_fds must be False so the child's inherited stdout fd is not closed on exec.
        subprocess.Popen(
            [sys.executable, str(_SLACK_SCRIPT)],
            env=env,
            cwd=str(_REPO_ROOT),
            stdout=lf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=False,
        )
    except Exception as e:
        log.warning("enqueue_slack_huddle_automation failed: %s", e)
