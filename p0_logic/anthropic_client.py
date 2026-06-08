"""
Anthropic Claude API — chat for natural-language ops triage (screenshot requests, etc.).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import requests

from . import config as _config

log = logging.getLogger("lark-ops-ai")

ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


def _api_key() -> str:
    _config.reload_env_runtime()
    return (os.getenv("ANTHROPIC_API_KEY") or "").strip()


def _model() -> str:
    _config.reload_env_runtime()
    return (os.getenv("ANTHROPIC_MODEL") or "claude-3-5-haiku-20241022").strip()


def anthropic_chat_once(
    system_prompt: str,
    user_content: str,
    max_tokens: int = 256,
    model: Optional[str] = None,
) -> str:
    """Single Claude Messages API round trip. Returns assistant text or empty string on failure."""
    key = _api_key()
    if not key:
        return ""
    payload = {
        "model": (model or _model()).strip(),
        "max_tokens": max(64, min(int(max_tokens), 1024)),
        "system": (system_prompt or "").strip(),
        "messages": [{"role": "user", "content": (user_content or "").strip()}],
    }
    if not payload["system"] or not payload["messages"][0]["content"]:
        return ""
    try:
        resp = requests.post(
            ANTHROPIC_MESSAGES_URL,
            headers={
                "x-api-key": key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json=payload,
            timeout=_config.REQ_TIMEOUT,
        )
    except requests.RequestException as e:
        log.warning("anthropic_chat_once: request failed: %s", e)
        return ""
    if resp.status_code != 200:
        log.warning(
            "anthropic_chat_once: HTTP=%s body=%s",
            resp.status_code,
            (resp.text or "")[:400],
        )
        return ""
    try:
        data = resp.json()
        blocks = data.get("content") or []
        parts = []
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "text":
                t = str(b.get("text") or "")
                if t:
                    parts.append(t)
        return "\n".join(parts).strip()
    except Exception as e:
        log.warning("anthropic_chat_once: parse failed: %s", e)
        return ""
