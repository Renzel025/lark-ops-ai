# p0_logic.py

import os
import json
import time
import threading
import requests
import logging
import subprocess
from pathlib import Path
from typing import Any, Dict
from datetime import datetime
from zoneinfo import ZoneInfo

from notify.dispatcher import notify_all

log = logging.getLogger("lark-ops-ai")

REQ_TIMEOUT = 10

# Owner (OPEN_ID)
SPECIFIC_OWNER_ID = "ou_cb50274ea2ff11149ba48d95c1803f01"
MEETING_TOPIC = "CP-Emergency feedback紧急问题反馈群"

# Force timezone to Philippines
PHT = ZoneInfo("Asia/Manila")

# Runtime sessions by chat_id
P0_SESSIONS: Dict[str, Dict[str, Any]] = {}

_TOKEN_CACHE = {"token": "", "exp": 0}
_TOKEN_LOCK = threading.Lock()

# Translation cache
_TRANSLATE_CACHE: Dict[str, str] = {}
_TRANSLATE_LOCK = threading.Lock()

# IM send base (known working)
LARK_IM_BASE = "https://open-sg.larksuite.com/open-apis"

# VC domains to try (auto-detect)
VC_BASES = [
    "https://open.larksuite.com/open-apis",
    "https://open-sg.larksuite.com/open-apis",
]

# Groq
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_BASE = "https://api.groq.com/openai/v1"


# ============================================================
# Puppeteer trigger for Telegram "video call" click
# ============================================================
_PPTR_LOCK = threading.Lock()
_PPTR_LAST_RUN = 0
_PPTR_COOLDOWN_SEC = 90  # prevent spamming if multiple P0 triggers happen quickly


def trigger_telegram_puppeteer_call_best_effort() -> None:
    """
    Best-effort trigger that runs:
      xvfb-run -a node /home/ubuntu/lark-ops-ai/puppeeter/telegram_main.js

    - Non-blocking (runs in a daemon thread)
    - Cooldown-protected
    - Timeout protected (won't hang forever)
    """
    global _PPTR_LAST_RUN

    now = int(time.time())
    with _PPTR_LOCK:
        if now - _PPTR_LAST_RUN < _PPTR_COOLDOWN_SEC:
            log.info("Puppeteer trigger skipped (cooldown %ss).", _PPTR_COOLDOWN_SEC)
            return
        _PPTR_LAST_RUN = now

    def _runner():
        try:
            default_js = "/home/ubuntu/lark-ops-ai/puppeeter/telegram_main.js"
            js_path = Path(os.getenv("TELEGRAM_PPTR_JS", default_js))

            if not js_path.exists():
                log.error("telegram_main.js not found at: %s", js_path)
                return

            cmd = ["xvfb-run", "-a", "node", str(js_path)]
            log.info("Starting Puppeteer Telegram call: %s", " ".join(cmd))

            env = os.environ.copy()

            res = subprocess.run(
                cmd,
                cwd=str(js_path.parent),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=180,  # 3 minutes max
            )

            out_tail = (res.stdout or "")[-2000:]
            log.info("Puppeteer finished (rc=%s). Output tail:\n%s", res.returncode, out_tail)

        except subprocess.TimeoutExpired:
            log.error("Puppeteer timed out (>180s).")
        except Exception as e:
            log.error("Puppeteer trigger failed: %s", e)

    threading.Thread(target=_runner, daemon=True).start()


def get_tenant_token(app_id: str, app_secret: str) -> str:
    now = int(time.time())
    with _TOKEN_LOCK:
        if _TOKEN_CACHE["token"] and now < _TOKEN_CACHE["exp"]:
            return _TOKEN_CACHE["token"]

    try:
        url = f"{LARK_IM_BASE}/auth/v3/tenant_access_token/internal"
        resp = requests.post(
            url, json={"app_id": app_id, "app_secret": app_secret}, timeout=REQ_TIMEOUT
        )
        data = resp.json()
        if data.get("code") != 0:
            log.error("Lark Token API Error: %s", data.get("msg"))
            return ""

        tok = data.get("tenant_access_token", "")
        if tok:
            with _TOKEN_LOCK:
                _TOKEN_CACHE["token"] = tok
                _TOKEN_CACHE["exp"] = now + (int(data.get("expire", 3600)) - 120)
            return tok
    except Exception as e:
        log.error("Network error fetching token: %s", e)
    return ""


def _post_text(chat_id: str, token: str, text: str) -> None:
    url = f"{LARK_IM_BASE}/im/v1/messages?receive_id_type=chat_id"
    payload = {
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }
    r = requests.post(
        url, headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=REQ_TIMEOUT
    )
    log.info("Post Text Response: %s - %s", r.status_code, r.text)


def _post_card(chat_id: str, token: str, card: Dict[str, Any]) -> None:
    url = f"{LARK_IM_BASE}/im/v1/messages?receive_id_type=chat_id"
    payload = {
        "receive_id": chat_id,
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
    }
    r = requests.post(
        url, headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=REQ_TIMEOUT
    )
    log.info("Post Card Response: %s - %s", r.status_code, r.text)


def end_p0_session(chat_id: str) -> None:
    P0_SESSIONS.pop(chat_id, None)


# -------------------------
# Telegram notify helpers
# -------------------------
def _notify_tg_start(start_epoch: int, meeting_link: str) -> None:
    ts = datetime.fromtimestamp(start_epoch, tz=PHT).strftime("%Y-%m-%d %H:%M")

    msg_main = (
        f"🚨 Declared P0 on emergency group: {MEETING_TOPIC}\n"
        f"🕒 Time: {ts} (PHT)\n"
        f"Please help to join on meeting:\n"
        f"{meeting_link}"
    )

    msg_auto = (
        "📞 Automatic Telegram calling has been initiated to notify OM members.\n"
        "Please standby and join once ringing is received."
    )

    notify_all(msg_main)
    notify_all(msg_auto)


def _notify_tg_overview(md: str) -> None:
    notify_all(md)


# -------------------------
# Translation: Groq LLM
# -------------------------
def translate_to_zh(text: str) -> str:
    src = (text or "").strip()
    if not src:
        return src

    key = f"groq:zh:{src}"
    with _TRANSLATE_LOCK:
        if key in _TRANSLATE_CACHE:
            return _TRANSLATE_CACHE[key]

    if not GROQ_API_KEY:
        with _TRANSLATE_LOCK:
            _TRANSLATE_CACHE[key] = src
        return src

    try:
        url = f"{GROQ_BASE}/chat/completions"
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}

        system_prompt = (
            "You are a translator for incident reports (P0/P1 on-call) in gaming/fintech operations.\n"
            "Translate the user's text into Simplified Chinese.\n"
            "\n"
            "Disambiguation rules (do NOT ask questions):\n"
            "- In ops/incident context, the word 'credit' is most likely about account balance/coins/funds being credited.\n"
            "- Only translate 'credit' as '致谢/鸣谢' if the text is clearly about papers/books/presentations or an acknowledgements section.\n"
            "\n"
            "Strict rules:\n"
            "- Keep acronyms/team names unchanged (e.g., FE, SRE, FPMS, Albularyo).\n"
            "- Keep numbers unchanged.\n"
            "- Do not add meaning, jokes, or rewrite the message.\n"
            "- If the input has slang/filler (e.g., 'lets go'), translate literally without inventing new content.\n"
            "\n"
            "Return ONLY the translated text. No quotes, no explanations."
        )

        payload = {
            "model": "llama-3.1-8b-instant",
            "temperature": 0.0,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": src},
            ],
        }

        r = requests.post(url, headers=headers, json=payload, timeout=REQ_TIMEOUT)
        if r.status_code == 200:
            j = r.json() if r.text else {}
            out = (j.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
            if out:
                with _TRANSLATE_LOCK:
                    _TRANSLATE_CACHE[key] = out
                return out
        else:
            log.error("Groq translate error: %s - %s", r.status_code, r.text[:300])

    except Exception as e:
        log.error("Groq translate failed: %s", e)

    with _TRANSLATE_LOCK:
        _TRANSLATE_CACHE[key] = src
    return src


# -------------------------
# VC create (try many) - kept for JOIN NOW button
# -------------------------
def _create_vc_best_effort(token: str) -> str:
    fallback = "https://vc.larksuite.com/j/589568093"
    headers = {"Authorization": f"Bearer {token}"}

    attempts = []
    for base in VC_BASES:
        attempts.append(
            (
                f"{base}/vc/v1/meetings?user_id_type=open_id",
                {"topic": MEETING_TOPIC, "host_id": SPECIFIC_OWNER_ID},
                ("meeting_url", "url", "join_url"),
            )
        )
        attempts.append(
            (
                f"{base}/videoconference/v1/conferences?user_id_type=open_id",
                {"topic": MEETING_TOPIC, "conference_type": "common", "host_id": SPECIFIC_OWNER_ID},
                ("url", "join_url", "meeting_url"),
            )
        )

    for url, payload, keys in attempts:
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=REQ_TIMEOUT)
            log.info("VC Create try: %s -> %s - %s", url, r.status_code, r.text[:250])

            if r.status_code == 200:
                j = r.json() if r.text else {}
                data = (j.get("data") or {}) if isinstance(j, dict) else {}
                for k in keys:
                    link = data.get(k)
                    if link:
                        return link
        except Exception as e:
            log.error("VC Create failed: %s (%s)", url, e)

    return fallback


# -------------------------
# Cards
# -------------------------
def build_p0_group_card(link: str) -> Dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {"enable_forward": True},
        "header": {
            "template": "red",
            "title": {"tag": "plain_text", "content": f"🚨 P0 EMERGENCY — {MEETING_TOPIC}"},
        },
        "body": {
            "elements": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "JOIN NOW"},
                    "type": "primary",
                    "multi_url": {"url": link, "pc_url": link},
                },
                {"tag": "hr"},
                {"tag": "div", "text": {"tag": "plain_text", "content": "Overview Form"}},
                {
                    "tag": "form",
                    "name": "p0_form",
                    "elements": [
                        {"tag": "div", "text": {"tag": "plain_text", "content": "🔥 Issue Description:"}},
                        {
                            "tag": "input",
                            "name": "issue_val",
                            "placeholder": {"tag": "plain_text", "content": "Describe the issue"},
                        },
                        {"tag": "div", "text": {"tag": "plain_text", "content": "🎯 Impact Scope:"}},
                        {
                            "tag": "input",
                            "name": "impact_val",
                            "placeholder": {"tag": "plain_text", "content": "Who/what is affected"},
                        },
                        {"tag": "div", "text": {"tag": "plain_text", "content": "👥 Support Request:"}},
                        {
                            "tag": "input",
                            "name": "support_val",
                            "placeholder": {"tag": "plain_text", "content": "Which team/support needed"},
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "Submit Overview"},
                            "type": "primary",
                            "action_type": "form_submit",
                            "name": "p0_submit_btn",
                            "value": {"action": "p0_submit"},
                        },
                    ],
                },
            ]
        },
    }


def build_bilingual_overview_md(start_epoch: int, issue: str, impact: str, support: str) -> str:
    start_time = datetime.fromtimestamp(start_epoch, tz=PHT).strftime("%Y-%m-%d %H:%M")

    zh_issue = translate_to_zh(issue)
    zh_impact = translate_to_zh(impact)
    zh_support = support  # keep team/support names as-is

    return (
        f"**P0 Incident Overview**\n"
        f"🕒 **Time**: {start_time} - Incident Start\n"
        f"🔥 **Issue**: {issue}\n"
        f"🎯 **Impact Scope**: {impact}\n"
        f"👥 **Support Request**: {support}\n"
        f"\n"
        f"**P0 事故概览**\n"
        f"🕒 **时间**: {start_time}（事故开始）\n"
        f"🔥 **问题**: {zh_issue}\n"
        f"🎯 **影响范围**: {zh_impact}\n"
        f"👥 **支援请求**: {zh_support}"
    )


def build_overview_result_card(md: str) -> Dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {"enable_forward": True},
        "body": {"elements": [{"tag": "div", "text": {"tag": "lark_md", "content": md}}]},
    }


def start_p0(chat_id: str, token: str) -> None:
    now = int(time.time())
    link = _create_vc_best_effort(token)

    P0_SESSIONS[chat_id] = {"start_epoch": now, "link": link, "owner_open_id": SPECIFIC_OWNER_ID}
    _post_card(chat_id, token, build_p0_group_card(link))

    # Trigger puppeteer call (best effort, non-blocking)
    try:
        trigger_telegram_puppeteer_call_best_effort()
    except Exception as e:
        log.error("Puppeteer trigger exception: %s", e)

    # Telegram messages
    try:
        _notify_tg_start(now, link)
    except Exception as e:
        log.error("Telegram notify(start) failed: %s", e)


def handle_p0_submit(evt: Dict[str, Any], token: str) -> None:
    action = evt.get("action", {}) or {}
    form = action.get("form_value", {}) or {}

    chat_id = (evt.get("context", {}) or {}).get("open_chat_id")
    if not chat_id:
        return

    operator = evt.get("operator", {}) or {}
    operator_open_id = operator.get("open_id") or operator.get("user_id")

    if operator_open_id != SPECIFIC_OWNER_ID:
        _post_text(chat_id, token, "❌ Owner only. You cannot submit this overview.")
        return

    sess = P0_SESSIONS.get(chat_id) or {}
    start = sess.get("start_epoch", int(time.time()))

    md = build_bilingual_overview_md(
        start,
        (form.get("issue_val") or "N/A").strip(),
        (form.get("impact_val") or "N/A").strip(),
        (form.get("support_val") or "N/A").strip(),
    )

    _post_card(chat_id, token, build_overview_result_card(md))

    try:
        _notify_tg_overview(md)
    except Exception as e:
        log.error("Telegram notify(overview) failed: %s", e)

    end_p0_session(chat_id)
