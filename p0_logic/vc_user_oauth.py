"""
Lark user OAuth for VC in-meeting actions (e.g. ``PATCH .../meetings/{id}/invite``).

Tokens are stored per ``open_id`` under ``P0_SHARED_STATE_DIR/vc-user-oauth/``.
Dev-only feature — enable with ``P0_VC_RING_ENABLED=1``.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import config as _config
from .lark_client import LARK_BASE, _lark_http, _timeout_kw

log = logging.getLogger("lark-ops-ai")

_TOKEN_DIR_NAME = "vc-user-oauth"


def _token_dir() -> Path:
    from .draft_store import shared_state_dir

    base = (shared_state_dir() or "").strip()
    if not base:
        base = "/tmp/lark-ops-ai-vc-oauth"
    p = Path(base) / _TOKEN_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def _token_path(open_id: str) -> Path:
    oid = (open_id or "").strip()
    safe = oid.replace("/", "_") if oid else "unknown"
    return _token_dir() / f"{safe}.json"


def load_user_token_row(open_id: str) -> Optional[Dict[str, Any]]:
    oid = (open_id or "").strip()
    if not oid:
        return None
    path = _token_path(oid)
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            row = json.load(f)
        return row if isinstance(row, dict) else None
    except Exception as e:
        log.warning("vc_user_oauth: read failed open_id_tail=%s err=%s", oid[-8:], e)
        return None


def _save_user_token_row(open_id: str, row: Dict[str, Any]) -> None:
    oid = (open_id or "").strip()
    if not oid:
        return
    path = _token_path(oid)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(row, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def has_user_token(open_id: str) -> bool:
    row = load_user_token_row(open_id)
    return bool((row or {}).get("refresh_token") or (row or {}).get("access_token"))


def build_authorize_url(open_id: str) -> str:
    """Browser URL for duty to grant ``vc:meeting`` (one-time per user)."""
    app_id, _ = _config.get_lark_primary_app_credentials()
    app_id = (app_id or "").strip()
    redirect = _config.get_p0_vc_oauth_redirect_uri()
    base_public = (_config.get_p0_vc_oauth_public_base_url() or "").strip().rstrip("/")
    oid = (open_id or "").strip()
    if not app_id or not redirect:
        return ""
    state = urllib.parse.quote(oid, safe="")
    scope = urllib.parse.quote(_config.get_p0_vc_oauth_scope(), safe="")
    # Optional: link via our start route so state is bound to open_id in logs.
    if base_public and oid:
        return f"{base_public}/lark/oauth/start?open_id={urllib.parse.quote(oid, safe='')}"
    return (
        f"https://open.larksuite.com/open-apis/authen/v1/authorize"
        f"?app_id={urllib.parse.quote(app_id, safe='')}"
        f"&redirect_uri={urllib.parse.quote(redirect, safe='')}"
        f"&scope={scope}"
        f"&state={state}"
    )


def exchange_code_for_tokens(code: str, *, open_id_hint: str = "") -> Tuple[bool, str, str]:
    """
    OAuth callback: exchange ``code`` for access + refresh token.
    Returns ``(ok, open_id, message)``.
    """
    code = (code or "").strip()
    if not code:
        return False, "", "missing code"
    app_id, app_secret = _config.get_lark_primary_app_credentials()
    app_id = (app_id or "").strip()
    app_secret = (app_secret or "").strip()
    redirect = _config.get_p0_vc_oauth_redirect_uri()
    if not app_id or not app_secret or not redirect:
        return False, "", "OAuth not configured (app id/secret/redirect)"

    url = f"{LARK_BASE}/authen/v1/oidc/access_token"
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": app_id,
        "client_secret": app_secret,
        "redirect_uri": redirect,
    }
    try:
        r = _lark_http().post(url, json=payload, **_timeout_kw())
        j = r.json() if r.text else {}
        if not isinstance(j, dict) or j.get("code") != 0:
            msg = str(j.get("msg") or r.text or "")[:300]
            log.warning("vc_user_oauth: code exchange failed code=%s msg=%s", j.get("code"), msg)
            return False, "", msg or "token exchange failed"
        data = j.get("data") or {}
        access = str(data.get("access_token") or "").strip()
        refresh = str(data.get("refresh_token") or "").strip()
        expires_in = int(data.get("expires_in") or 7200)
        token_open_id = str(data.get("open_id") or open_id_hint or "").strip()
        if not access or not token_open_id:
            return False, "", "empty access_token or open_id in response"
        row = {
            "open_id": token_open_id,
            "access_token": access,
            "refresh_token": refresh,
            "expires_at": int(time.time()) + max(60, expires_in - 120),
            "updated_at": int(time.time()),
        }
        _save_user_token_row(token_open_id, row)
        log.info("vc_user_oauth: stored token open_id_tail=%s expires_in=%s", token_open_id[-8:], expires_in)
        return True, token_open_id, "authorized"
    except Exception as e:
        log.warning("vc_user_oauth: exchange exception: %s", e)
        return False, "", str(e)


def get_user_access_token(open_id: str) -> str:
    """Return a valid user access token, refreshing when needed."""
    oid = (open_id or "").strip()
    if not oid:
        return ""
    row = load_user_token_row(oid)
    if not row:
        return ""
    now = int(time.time())
    access = str(row.get("access_token") or "").strip()
    expires_at = int(row.get("expires_at") or 0)
    if access and expires_at > now:
        return access
    refresh = str(row.get("refresh_token") or "").strip()
    if not refresh:
        return access
    app_id, app_secret = _config.get_lark_primary_app_credentials()
    app_id = (app_id or "").strip()
    app_secret = (app_secret or "").strip()
    if not app_id or not app_secret:
        return access
    url = f"{LARK_BASE}/authen/v1/oidc/refresh_access_token"
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": app_id,
        "client_secret": app_secret,
    }
    try:
        r = _lark_http().post(url, json=payload, **_timeout_kw())
        j = r.json() if r.text else {}
        if not isinstance(j, dict) or j.get("code") != 0:
            log.warning(
                "vc_user_oauth: refresh failed open_id_tail=%s code=%s msg=%s",
                oid[-8:],
                j.get("code"),
                (j.get("msg") or "")[:200],
            )
            return access
        data = j.get("data") or {}
        access = str(data.get("access_token") or "").strip()
        new_refresh = str(data.get("refresh_token") or refresh).strip()
        expires_in = int(data.get("expires_in") or 7200)
        if access:
            row["access_token"] = access
            row["refresh_token"] = new_refresh
            row["expires_at"] = now + max(60, expires_in - 120)
            row["updated_at"] = now
            _save_user_token_row(oid, row)
            log.info("vc_user_oauth: refreshed open_id_tail=%s", oid[-8:])
        return access
    except Exception as e:
        log.warning("vc_user_oauth: refresh exception open_id_tail=%s err=%s", oid[-8:], e)
        return access
