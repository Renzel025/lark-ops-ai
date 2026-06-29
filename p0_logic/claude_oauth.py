"""Claude Code OAuth credentials (~/.claude/.credentials.json) for subscription auth."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests

from . import config as _config

log = logging.getLogger("lark-ops-ai")

# Public client id used by Claude Code CLI (same on all installs).
_CLAUDE_CODE_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_OAUTH_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
_REFRESH_LOCK = False


def _credentials_path() -> Path:
    _config.reload_env_runtime()
    raw = (os.getenv("CLAUDE_CREDENTIALS_PATH") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".claude" / ".credentials.json"


def _load_credentials_file() -> Dict[str, Any]:
    path = _credentials_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as e:
        log.warning("claude_oauth: read %s failed: %s", path, e)
        return {}


def _save_credentials_file(data: Dict[str, Any]) -> None:
    path = _credentials_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except OSError as e:
        log.warning("claude_oauth: write %s failed: %s", path, e)


def _expires_at_sec(oauth: Dict[str, Any]) -> float:
    raw = oauth.get("expiresAt") or oauth.get("expires_at") or 0
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        return 0.0
    # Claude Code stores ms since epoch on some platforms.
    if ts > 1e12:
        ts = ts / 1000.0
    return ts


def _refresh_oauth_tokens(oauth: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    refresh = (oauth.get("refreshToken") or oauth.get("refresh_token") or "").strip()
    if not refresh:
        return "", oauth
    try:
        resp = requests.post(
            _OAUTH_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": _CLAUDE_CODE_OAUTH_CLIENT_ID,
            },
            headers={"content-type": "application/x-www-form-urlencoded"},
            timeout=_config.REQ_TIMEOUT,
        )
    except requests.RequestException as e:
        log.warning("claude_oauth: refresh request failed: %s", e)
        return "", oauth
    if resp.status_code != 200:
        log.warning(
            "claude_oauth: refresh HTTP=%s body=%s",
            resp.status_code,
            (resp.text or "")[:300],
        )
        return "", oauth
    try:
        body = resp.json()
    except ValueError:
        return "", oauth
    access = (body.get("access_token") or "").strip()
    if not access:
        return "", oauth
    new_oauth = dict(oauth)
    new_oauth["accessToken"] = access
    if body.get("refresh_token"):
        new_oauth["refreshToken"] = str(body["refresh_token"]).strip()
    expires_in = body.get("expires_in")
    try:
        if expires_in is not None:
            new_oauth["expiresAt"] = int(time.time()) + int(expires_in)
    except (TypeError, ValueError):
        pass
    return access, new_oauth


def get_oauth_access_token(*, allow_refresh: bool = True) -> str:
    """
    Return a valid Claude subscription OAuth access token from credentials file.
    Refreshes and rewrites the file when expired (same flow as Claude Code).
    """
    creds = _load_credentials_file()
    oauth = creds.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return ""
    access = (oauth.get("accessToken") or oauth.get("access_token") or "").strip()
    exp = _expires_at_sec(oauth)
    stale = exp > 0 and time.time() >= (exp - 120)
    if access and not stale:
        return access
    if not allow_refresh:
        return access if access and not stale else ""
    new_access, new_oauth = _refresh_oauth_tokens(oauth)
    if not new_access:
        return access if access else ""
    creds["claudeAiOauth"] = new_oauth
    _save_credentials_file(creds)
    log.info("claude_oauth: refreshed access token (subscription auth)")
    return new_access


def get_auth_token_env() -> str:
    """``ANTHROPIC_AUTH_TOKEN`` or ``CLAUDE_CODE_OAUTH_TOKEN`` (Bearer, from ``claude setup-token``)."""
    _config.reload_env_runtime()
    for name in ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        tok = (os.getenv(name) or "").strip()
        if tok:
            return tok
    return ""


def resolve_anthropic_bearer_token() -> Tuple[str, str]:
    """
    Bearer token for Messages API. Priority (Claude Code–like):
    1. ``ANTHROPIC_AUTH_TOKEN`` / ``CLAUDE_CODE_OAUTH_TOKEN``
    2. OAuth file ``~/.claude/.credentials.json`` (auto refresh)
    Returns ``(token, mode)`` where mode is ``auth_token`` | ``oauth_file`` | empty.
    """
    env_tok = get_auth_token_env()
    if env_tok:
        return env_tok, "auth_token"
    oauth_tok = get_oauth_access_token()
    if oauth_tok:
        return oauth_tok, "oauth_file"
    return "", ""


def oauth_credentials_present() -> bool:
    creds = _load_credentials_file()
    oauth = creds.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return False
    return bool(
        (oauth.get("accessToken") or oauth.get("access_token") or "").strip()
        or (oauth.get("refreshToken") or oauth.get("refresh_token") or "").strip()
    )
