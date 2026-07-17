"""
Anthropic Claude API — chat for natural-language ops triage (screenshot requests, etc.).

Auth (first match wins for Bearer paths; API key is separate fallback):
  1. ``ANTHROPIC_AUTH_TOKEN`` / ``CLAUDE_CODE_OAUTH_TOKEN`` (``claude setup-token``)
  2. Claude Code OAuth file ``~/.claude/.credentials.json`` (subscription Pro/Max)
  3. ``ANTHROPIC_API_KEY`` (pay-as-you-go console key)
"""
from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

import requests

from . import claude_oauth as _oauth
from . import config as _config

log = logging.getLogger("lark-ops-ai")

ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
# Subscription OAuth (setup-token / credentials file) requires Claude Code headers + identity.
_CLAUDE_CODE_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."
_OAUTH_DEFAULT_MODEL = "claude-haiku-4-5-20251001"
_LEGACY_API_KEY_HAIKU = "claude-3-5-haiku-20241022"
_OAUTH_AUTH_MODES = frozenset({"auth_token", "oauth_file", "bearer"})


def _api_key() -> str:
    _config.reload_env_runtime()
    return (os.getenv("ANTHROPIC_API_KEY") or "").strip()


def _model() -> str:
    _config.reload_env_runtime()
    return (os.getenv("ANTHROPIC_MODEL") or _LEGACY_API_KEY_HAIKU).strip()


def _oauth_model() -> str:
    """OAuth subscription model — never reuse legacy API-key Haiku ids (they 404)."""
    _config.reload_env_runtime()
    explicit = (os.getenv("ANTHROPIC_OAUTH_MODEL") or "").strip()
    if explicit:
        return explicit
    shared = (os.getenv("ANTHROPIC_MODEL") or "").strip()
    if shared and shared != _LEGACY_API_KEY_HAIKU:
        return shared
    return _OAUTH_DEFAULT_MODEL


def _effective_model(auth_mode: str, model: Optional[str]) -> str:
    """API-key Haiku ids often 404 on subscription OAuth — use OAuth-era model ids."""
    raw = (model or _model()).strip()
    if auth_mode not in _OAUTH_AUTH_MODES:
        return raw or _LEGACY_API_KEY_HAIKU
    oauth_model = _oauth_model()
    if not raw or raw == _LEGACY_API_KEY_HAIKU:
        return oauth_model
    return raw


def _apply_oauth_headers(headers: dict) -> dict:
    out = dict(headers)
    out["anthropic-beta"] = "oauth-2025-04-20,claude-code-20250219"
    out["user-agent"] = "claude-cli/2.1.85 (external, lark-ops-ai)"
    out["x-app"] = "cli"
    return out


def _build_system(auth_mode: str, system_prompt: str):
    """OAuth Sonnet/Opus require Claude Code identity as the first system block."""
    text = (system_prompt or "").strip()
    if auth_mode not in _OAUTH_AUTH_MODES:
        return text
    if text.startswith(_CLAUDE_CODE_IDENTITY):
        return text
    blocks = [{"type": "text", "text": _CLAUDE_CODE_IDENTITY}]
    if text:
        blocks.append({"type": "text", "text": text})
    return blocks


def anthropic_auth_mode() -> str:
    """``api_key`` | ``auth_token`` | ``oauth_file`` | empty."""
    if _api_key():
        # API key always available as fallback; report subscription auth when present.
        bearer, mode = _oauth.resolve_anthropic_bearer_token()
        if bearer and mode:
            return mode
        return "api_key"
    bearer, mode = _oauth.resolve_anthropic_bearer_token()
    return mode


def has_anthropic_auth() -> bool:
    """True when any Claude auth path is configured (OAuth, auth token, or API key)."""
    if _api_key():
        return True
    bearer, _ = _oauth.resolve_anthropic_bearer_token()
    return bool(bearer)


def _request_headers() -> Tuple[dict, str]:
    """
    Build Messages API headers. Subscription OAuth / auth token preferred over API key
    when ``P0_ANTHROPIC_PREFER_OAUTH=1`` (default) or when no API key is set.
    """
    _config.reload_env_runtime()
    prefer_oauth = (os.getenv("P0_ANTHROPIC_PREFER_OAUTH") or "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    bearer, mode = _oauth.resolve_anthropic_bearer_token()
    key = _api_key()
    headers = {
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    if bearer and (prefer_oauth or not key):
        headers["authorization"] = f"Bearer {bearer}"
        return headers, mode or "bearer"
    if key:
        headers["x-api-key"] = key
        return headers, "api_key"
    if bearer:
        headers["authorization"] = f"Bearer {bearer}"
        return headers, mode or "bearer"
    return headers, ""


def anthropic_chat_once(
    system_prompt: str,
    user_content: str,
    max_tokens: int = 256,
    model: Optional[str] = None,
) -> str:
    """Single Claude Messages API round trip. Returns assistant text or empty string on failure."""
    headers, auth_mode = _request_headers()
    if auth_mode not in ("api_key", "auth_token", "oauth_file", "bearer"):
        return ""
    system = _build_system(auth_mode, system_prompt)
    user_text = (user_content or "").strip()
    if not system or not user_text:
        return ""
    if auth_mode in _OAUTH_AUTH_MODES:
        headers = _apply_oauth_headers(headers)
    payload = {
        "model": _effective_model(auth_mode, model),
        "max_tokens": max(64, min(int(max_tokens), 4096)),
        "system": system,
        "messages": [{"role": "user", "content": user_text}],
    }
    log.info("anthropic_chat_once: model=%s auth=%s", payload["model"], auth_mode)
    try:
        resp = requests.post(
            ANTHROPIC_MESSAGES_URL,
            headers=headers,
            json=payload,
            timeout=_config.REQ_TIMEOUT,
        )
    except requests.RequestException as e:
        log.warning("anthropic_chat_once: request failed auth=%s: %s", auth_mode, e)
        return ""
    if resp.status_code == 401 and auth_mode == "oauth_file":
        # Retry once after forced refresh.
        fresh = _oauth.get_oauth_access_token(allow_refresh=True)
        if fresh:
            headers["authorization"] = f"Bearer {fresh}"
            try:
                resp = requests.post(
                    ANTHROPIC_MESSAGES_URL,
                    headers=headers,
                    json=payload,
                    timeout=_config.REQ_TIMEOUT,
                )
            except requests.RequestException as e:
                log.warning("anthropic_chat_once: retry failed: %s", e)
                return ""
    if resp.status_code != 200:
        log.warning(
            "anthropic_chat_once: HTTP=%s auth=%s body=%s",
            resp.status_code,
            auth_mode,
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
