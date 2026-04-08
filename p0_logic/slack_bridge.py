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

import requests

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


def _outgoing_slack_text(text: str) -> str:
    """
    Optional prefix: ``SLACK_MESSAGE_PREFIX`` (full control, e.g. ``<!channel>``) or
    ``SLACK_BOT_USER_ID`` → prepends ``<@U...>`` so the bot is pinged in-channel.
    """
    _config.reload_env_runtime()
    t = (text or "").strip()
    if not t:
        return t
    custom = (os.getenv("SLACK_MESSAGE_PREFIX") or "").strip()
    if custom:
        return f"{custom}\n{t}"
    uid = _config.get_slack_bot_user_id()
    if uid.startswith("U"):
        return f"<@{uid}>\n{t}"
    return t


def post_slack_chat_api_message(incident_chat_id: str, text: str) -> bool:
    """
    ``chat.postMessage`` via Slack Web API (Bot token + channel ID). Preferred when configured.

    Requires ``SLACK_BOT_TOKEN`` + ``SLACK_API_CHANNEL_MAP`` for this ``oc_``.
    """
    token = _config.get_slack_bot_token()
    channel = _config.get_slack_api_channel_id_for_incident_chat(incident_chat_id)
    body = _outgoing_slack_text(text)
    if not token or not channel or not body:
        return False
    if len(body) > 38000:
        body = body[:37900] + "\n\n…(truncated)"
    try:
        r = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json={"channel": channel, "text": body},
            timeout=30,
        )
        data = r.json()
        if data.get("ok"):
            return True
        log.warning(
            "Slack chat.postMessage failed: error=%s response=%s",
            data.get("error"),
            str(data)[:500],
        )
        return False
    except Exception as e:
        log.warning("Slack chat.postMessage request failed: %s", e)
        return False


def post_text_to_slack_for_incident(incident_chat_id: str, text: str) -> bool:
    """
    Post plain text to Slack: try **Web API** first, then Incoming Webhook (overview map).
    """
    cid = (incident_chat_id or "").strip()
    if post_slack_chat_api_message(cid, text):
        return True
    wh = _config.get_slack_overview_webhook_for_incident_chat(cid)
    if wh:
        return post_overview_to_slack_webhook(wh, text)
    return False


def post_overview_to_slack_webhook(webhook_url: str, markdown: str) -> bool:
    """
    POST overview text to a Slack Incoming Webhook. Returns True on HTTP 200.
    """
    url = (webhook_url or "").strip()
    if not url:
        return False
    body = _outgoing_slack_text(markdown)
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


def notify_slack_p0_started(
    incident_chat_id: str,
    priority: str,
    source_chat_label: str,
) -> None:
    """
    POST to Incoming Webhook: Lark P0/P1 declared + whether huddle Playwright will run.

    Uses ``SLACK_INCIDENT_NOTIFY_WEBHOOK_MAP`` or ``SLACK_OVERVIEW_WEBHOOK_MAP``.
    """
    cid = (incident_chat_id or "").strip()
    pr = (priority or "P0").strip().upper()
    label = (source_chat_label or "").strip() or cid
    channel_url = _config.get_slack_channel_url_for_incident_chat(cid)
    session_dir = _config.get_slack_session_dir_for_incident_chat(cid)
    auto = _config.slack_automation_enabled()
    on_p0 = _config.slack_huddle_on_p0_start()
    will_run = (
        auto
        and on_p0
        and bool(channel_url)
        and bool(session_dir)
        and _SLACK_SCRIPT.is_file()
    )
    if will_run:
        huddle_line = "Slack huddle automation: STARTING NOW (headless Playwright → channel in LARK_SLACK_CHANNEL_URL_MAP)."
    else:
        reasons: list[str] = []
        if not auto:
            reasons.append("SLACK_AUTOMATION_ENABLED=0")
        if not on_p0:
            reasons.append("SLACK_HUDDLE_ON_P0_START=0")
        if not channel_url:
            reasons.append("missing LARK_SLACK_CHANNEL_URL_MAP / SLACK_CHANNEL_URL for this oc_")
        if not session_dir:
            reasons.append("missing SESSION_DIR / SLACK_SESSION_DIR")
        if not _SLACK_SCRIPT.is_file():
            reasons.append("scripts/slack_huddle_invite_all.py missing")
        huddle_line = "Slack huddle automation: NOT started. " + ("; ".join(reasons) if reasons else "unknown")
    text = (
        f"Lark: {pr} declared in incident group\n"
        f"Group: {label}\n"
        f"chat_id: {cid}\n"
        f"{huddle_line}"
    )
    if post_slack_chat_api_message(cid, text):
        log.info("notify_slack_p0_started: Slack Web API ok chat_id=%s", cid)
        return
    wh = _config.get_slack_incident_notify_webhook_for_incident_chat(cid)
    if wh and post_overview_to_slack_webhook(wh, text):
        log.info("notify_slack_p0_started: Incoming Webhook ok chat_id=%s", cid)
        return
    log.warning(
        "notify_slack_p0_started: failed chat_id=%s — set SLACK_BOT_TOKEN + SLACK_API_CHANNEL_MAP "
        "or SLACK_INCIDENT_NOTIFY_WEBHOOK_MAP / SLACK_OVERVIEW_WEBHOOK_MAP",
        cid,
    )


def enqueue_slack_huddle_automation(incident_chat_id: str, priority: str) -> None:
    """
    Fire-and-forget subprocess: ``scripts/slack_huddle_invite_all.py`` with env from config.

    No-op if automation disabled, unmapped incident chat, or missing channel URL / session dir.
    """
    cid = (incident_chat_id or "").strip()
    if not _config.slack_automation_enabled():
        log.warning(
            "enqueue_slack_huddle_automation skipped: SLACK_AUTOMATION_ENABLED=0 chat_id=%s",
            cid,
        )
        return
    channel_url = _config.get_slack_channel_url_for_incident_chat(cid)
    session_dir = _config.get_slack_session_dir_for_incident_chat(cid)
    if not channel_url:
        log.warning(
            "enqueue_slack_huddle_automation skipped: no Slack channel URL for chat_id=%s "
            "(set LARK_SLACK_CHANNEL_URL_MAP or SLACK_CHANNEL_URL + INCIDENT_GROUP_IDS)",
            cid,
        )
        return
    if not session_dir:
        log.warning(
            "enqueue_slack_huddle_automation skipped: SESSION_DIR / SLACK_SESSION_DIR empty chat_id=%s",
            cid,
        )
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
