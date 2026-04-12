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
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

import requests

from . import config as _config

log = logging.getLogger("lark-ops-ai")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SLACK_SCRIPT = _REPO_ROOT / "scripts" / "slack_huddle_invite_all.py"


def _python_for_slack_subprocess() -> str:
    """
    Interpreter that has ``playwright`` installed. Defaults to ``sys.executable`` (same as the bot).

    If the bot runs with a venv but subprocess would pick system Python without playwright, set:
    ``SLACK_SUBPROCESS_PYTHON=/path/to/venv/bin/python``
    """
    explicit = (
        os.getenv("SLACK_SUBPROCESS_PYTHON") or os.getenv("SLACK_PYTHON_EXECUTABLE") or ""
    ).strip()
    if explicit:
        return explicit
    return sys.executable
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
    cid = (incident_chat_id or "").strip()
    if not token:
        log.warning(
            "Slack chat.postMessage skipped: SLACK_BOT_TOKEN empty — set in process env / %s",
            _config.ENV_PATH,
        )
        return False
    if not channel:
        raw_map = (os.getenv("SLACK_API_CHANNEL_MAP") or "").strip()
        if not raw_map:
            log.warning(
                "Slack chat.postMessage skipped: SLACK_API_CHANNEL_MAP is empty in the process — "
                "add one line to %s (no line breaks): SLACK_API_CHANNEL_MAP=oc_xxx=C0...,oc_yyy=C0... "
                "If token works but this is empty, a broken quoted line above in .env can hide later vars; fix quotes.",
                _config.ENV_PATH,
            )
        else:
            from p0_logic.config import _parse_incident_keyed_url_map

            keys = list(_parse_incident_keyed_url_map(raw_map).keys())
            log.warning(
                "Slack chat.postMessage skipped: incident_chat_id=%s not in map — map has these oc_ keys "
                "(must match Lark exactly, character for character): %s",
                cid or "(empty)",
                keys,
            )
        return False
    if not body:
        return False
    if len(body) > 38000:
        body = body[:37900] + "\n\n…(truncated)"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }

    def _post() -> Dict[str, Any]:
        r = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers=headers,
            json={"channel": channel, "text": body},
            timeout=30,
        )
        return r.json()

    try:
        data = _post()
        if data.get("ok"):
            return True
        err = data.get("error")
        # Bot not in channel is the #1 reason messages never appear in the group.
        if err == "not_in_channel" and _truthy_env("SLACK_BOT_TRY_JOIN_CHANNEL"):
            try:
                jr = requests.post(
                    "https://slack.com/api/conversations.join",
                    headers=headers,
                    json={"channel": channel},
                    timeout=30,
                )
                join_data = jr.json()
                if join_data.get("ok"):
                    log.info("Slack conversations.join ok channel=%s — retrying chat.postMessage", channel)
                    data = _post()
                    if data.get("ok"):
                        return True
                    err = data.get("error")
                else:
                    log.warning(
                        "Slack conversations.join failed: error=%s (need channels:join scope?) response=%s",
                        join_data.get("error"),
                        str(join_data)[:400],
                    )
            except Exception as je:
                log.warning("Slack conversations.join request failed: %s", je)
        log.warning(
            "Slack chat.postMessage failed: error=%s response=%s",
            err,
            str(data)[:500],
        )
        if err == "not_in_channel":
            log.warning(
                "Slack fix: open that channel in Slack → /invite @YourBot (same app as SLACK_BOT_TOKEN). "
                "Or set SLACK_BOT_TRY_JOIN_CHANNEL=1 and add OAuth scope channels:join to the app, then reinstall."
            )
        elif err == "channel_not_found":
            log.warning(
                "Slack fix: SLACK_API_CHANNEL_MAP uses C… from the *same workspace* as the bot token; "
                "wrong workspace → channel_not_found."
            )
        elif err in ("invalid_auth", "token_revoked", "account_inactive"):
            log.warning("Slack fix: regenerate Bot User OAuth Token (xoxb-) in api.slack.com and update SLACK_BOT_TOKEN.")
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


def run_slack_p0_notify_and_huddle(incident_chat_id: str, priority: str, source_chat_label: str) -> None:
    """
    Slack notify + optional huddle subprocess — used on ``start_p0`` (legacy) or after **Major** is chosen.
    """
    notify_slack_p0_started(incident_chat_id, priority, source_chat_label)
    if _config.slack_huddle_on_p0_start():
        enqueue_slack_huddle_automation(incident_chat_id, priority)
    else:
        log.warning(
            "run_slack_p0_notify_and_huddle: huddle NOT started (SLACK_HUDDLE_ON_P0_START=0) chat_id=%s",
            (incident_chat_id or "").strip(),
        )


def notify_slack_p0_started(
    incident_chat_id: str,
    priority: str,
    source_chat_label: str,
) -> None:
    """
    Short Slack alert when P0/P1 is declared in Lark (Web API or webhook fallback).

    Huddle Playwright runs separately; operators use server logs if automation fails.
    """
    cid = (incident_chat_id or "").strip()
    pr = (priority or "P0").strip().upper()
    label = (source_chat_label or "").strip() or "incident group"
    text = (
        f"Lark: {pr} declared in incident group\n"
        f"Group: {label}\n"
        "Calling all members on this channel"
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


def _enqueue_remote_slack_huddle(
    incident_chat_id: str, priority: str, channel_url: str
) -> None:
    """
    When Playwright cannot run reliably on the cloud host (WebRTC / blank popups), run the
    script on another machine: set ``SLACK_HUDDLE_REMOTE_URL`` to that worker's HTTPS URL.
    """
    url = (os.getenv("SLACK_HUDDLE_REMOTE_URL") or "").strip()
    if not url:
        return
    secret = (os.getenv("SLACK_HUDDLE_REMOTE_SECRET") or "").strip()
    payload = {
        "incident_chat_id": incident_chat_id,
        "priority": priority,
        "channel_url": channel_url,
    }
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"

    def _run() -> None:
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=30)
            if r.status_code >= 400:
                log.error(
                    "slack_huddle REMOTE http_error status=%s body=%s",
                    r.status_code,
                    (r.text or "")[:800],
                )
            else:
                log.info(
                    "slack_huddle REMOTE ok status=%s chat_id=%s",
                    r.status_code,
                    incident_chat_id,
                )
        except Exception as e:
            log.error(
                "slack_huddle REMOTE failed chat_id=%s error=%s",
                incident_chat_id,
                e,
                exc_info=True,
            )

    threading.Thread(
        target=_run, daemon=True, name="slack-huddle-remote"
    ).start()
    log.info(
        "slack_huddle REMOTE queued chat_id=%s priority=%s url=%s",
        incident_chat_id,
        priority,
        url[:96] + ("..." if len(url) > 96 else ""),
    )


def enqueue_slack_huddle_automation(incident_chat_id: str, priority: str) -> None:
    """
    Fire-and-forget subprocess: ``scripts/slack_huddle_invite_all.py`` with env from config.

    If ``SLACK_HUDDLE_REMOTE_URL`` is set, only an HTTP POST is sent to that worker (no local
    Playwright on this host). Otherwise the script runs locally.

    No-op if automation disabled, unmapped incident chat, or missing channel URL / session dir.
    **Tail** ``logs/slack_huddle_playwright.log`` (or ``SLACK_PLAYWRIGHT_LOG``) for script stdout/stderr.
    """
    cid = (incident_chat_id or "").strip()
    log_path = _playwright_subprocess_log_path()
    pr = (priority or "P0").strip().upper()
    remote_url = (os.getenv("SLACK_HUDDLE_REMOTE_URL") or "").strip()
    if not _config.slack_automation_enabled():
        log.warning(
            "slack_huddle SKIP chat_id=%s reason=SLACK_AUTOMATION_ENABLED=0 | fix: set SLACK_AUTOMATION_ENABLED=1",
            cid,
        )
        return
    channel_url = _config.get_slack_channel_url_for_incident_chat(cid)
    if not channel_url:
        log.warning(
            "slack_huddle SKIP chat_id=%s reason=no Slack deep link | fix: LARK_SLACK_CHANNEL_URL_MAP=oc_...=https://app.slack.com/client/T/C/ "
            "or SLACK_CHANNEL_URL if single group; oc_ must match this incident",
            cid,
        )
        return
    if remote_url:
        _enqueue_remote_slack_huddle(cid, pr, channel_url)
        return
    session_dir = _config.get_slack_session_dir_for_incident_chat(cid)
    if not session_dir:
        log.warning(
            "slack_huddle SKIP chat_id=%s reason=no SESSION_DIR | fix: SESSION_DIR=/path/to/chromium/profile",
            cid,
        )
        return
    if not _SLACK_SCRIPT.is_file():
        log.warning(
            "slack_huddle SKIP chat_id=%s reason=script missing path=%s | fix: deploy repo with scripts/slack_huddle_invite_all.py",
            cid,
            _SLACK_SCRIPT,
        )
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
    py_exe = _python_for_slack_subprocess()
    log.info(
        "slack_huddle STARTING chat_id=%s priority=%s python=%s session_dir=%s channel_url=%s log_file=%s",
        cid,
        pr,
        py_exe,
        session_dir,
        channel_url[:80] + ("..." if len(channel_url) > 80 else ""),
        log_path,
    )
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lf = open(log_path, "a", encoding="utf-8", buffering=1)
        _SLACK_PLAYWRIGHT_LOG_HANDLES.append(lf)
        lf.write(f"\n===== {ts} chat_id={cid} priority={pr} python={py_exe} =====\n")
        lf.flush()
        # close_fds must be False so the child's inherited stdout fd is not closed on exec.
        proc = subprocess.Popen(
            [py_exe, str(_SLACK_SCRIPT)],
            env=env,
            cwd=str(_REPO_ROOT),
            stdout=lf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=False,
        )
        log.info(
            "slack_huddle subprocess LAUNCHED pid=%s chat_id=%s — tail stderr/stdout: %s",
            proc.pid,
            cid,
            log_path,
        )
    except Exception as e:
        log.error(
            "slack_huddle LAUNCH FAILED chat_id=%s error=%s | check permissions on %s",
            cid,
            e,
            log_path,
            exc_info=True,
        )
