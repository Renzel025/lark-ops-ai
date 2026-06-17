"""
Google Gemini API — chat for P0/P1 keyword triage (Flash models).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import requests

from . import config as _config

log = logging.getLogger("lark-ops-ai")

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"


def _api_key() -> str:
    _config.reload_env_runtime()
    return (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()


def _model() -> str:
    _config.reload_env_runtime()
    # Override in .env: gemini-3.5-flash, gemini-3.1-flash-lite, gemini-3-flash-preview, …
    return (os.getenv("GEMINI_MODEL") or "gemini-2.0-flash").strip()


def gemini_chat_once(
    system_prompt: str,
    user_content: str,
    max_tokens: int = 256,
    model: Optional[str] = None,
) -> str:
    """Single Gemini generateContent call. Returns assistant text or empty string on failure."""
    key = _api_key()
    mdl = (model or _model()).strip()
    if not key or not mdl:
        return ""
    sys_p = (system_prompt or "").strip()
    usr_p = (user_content or "").strip()
    if not sys_p or not usr_p:
        return ""
    url = f"{GEMINI_API_BASE}/models/{mdl}:generateContent"
    payload = {
        "systemInstruction": {"parts": [{"text": sys_p}]},
        "contents": [{"role": "user", "parts": [{"text": usr_p}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": max(64, min(int(max_tokens), 1024)),
        },
    }
    try:
        resp = requests.post(
            url,
            params={"key": key},
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=_config.REQ_TIMEOUT,
        )
    except requests.RequestException as e:
        log.warning("gemini_chat_once: request failed: %s", e)
        return ""
    if resp.status_code != 200:
        log.warning(
            "gemini_chat_once: HTTP=%s model=%s body=%s",
            resp.status_code,
            mdl,
            (resp.text or "")[:400],
        )
        return ""
    try:
        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            return ""
        parts = (candidates[0].get("content") or {}).get("parts") or []
        texts = [str(p.get("text") or "") for p in parts if isinstance(p, dict) and p.get("text")]
        return "\n".join(texts).strip()
    except Exception as e:
        log.warning("gemini_chat_once: parse failed: %s", e)
        return ""
