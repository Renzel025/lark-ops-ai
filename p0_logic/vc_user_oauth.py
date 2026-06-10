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
from typing import Any, Dict, List, Optional, Tuple

from . import config as _config
from .lark_client import LARK_BASE, _lark_http, _timeout_kw

log = logging.getLogger("lark-ops-ai")

_TOKEN_DIR_NAME = "vc-user-oauth"
# OAuth v2 (RFC 6749): authorize on accounts.*, token + user_info on open.larksuite.com (not open-sg).
_OAUTH_AUTHORIZE_BASE = "https://accounts.larksuite.com/open-apis"
_OAUTH_TOKEN_BASES = (
    "https://open.larksuite.com/open-apis",
    LARK_BASE,
)
_OAUTH_USER_INFO_BASES = _OAUTH_TOKEN_BASES


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


def _oauth_scope_with_offline(scope: str) -> str:
    parts = [p.strip() for p in (scope or "").split() if p.strip()]
    if "offline_access" not in parts:
        parts.append("offline_access")
    return " ".join(parts)


def _oauth_response_ok(j: Dict[str, Any]) -> bool:
    code = j.get("code")
    return code == 0 or str(code) == "0"


def _parse_lark_json(r: Any, *, label: str) -> Tuple[Optional[Dict[str, Any]], str]:
    text = (getattr(r, "text", None) or "").strip()
    status = getattr(r, "status_code", None)
    if not text:
        return None, f"{label}: empty body (HTTP {status})"
    try:
        j = r.json()
    except Exception as e:
        snippet = text[:300].replace("\n", " ")
        log.warning(
            "vc_user_oauth: %s non-JSON HTTP %s body=%s err=%s",
            label,
            status,
            snippet,
            e,
        )
        return None, f"{label}: invalid JSON (HTTP {status})"
    return j if isinstance(j, dict) else None, f"{label}: unexpected response type"


def build_oauth_authorize_redirect_url(*, app_id: str, redirect: str, scope: str, state: str) -> str:
    """Lark OAuth v2 authorize page (``accounts.larksuite.com``)."""
    app_id = (app_id or "").strip()
    redirect = (redirect or "").strip()
    if not app_id or not redirect:
        return ""
    scope_q = urllib.parse.quote(_oauth_scope_with_offline(scope), safe="")
    return (
        f"{_OAUTH_AUTHORIZE_BASE}/authen/v1/authorize"
        f"?client_id={urllib.parse.quote(app_id, safe='')}"
        f"&redirect_uri={urllib.parse.quote(redirect, safe='')}"
        f"&response_type=code"
        f"&scope={scope_q}"
        f"&state={urllib.parse.quote(state or '', safe='')}"
    )


def _lark_api_msg(j: Optional[Dict[str, Any]], *, fallback: str = "") -> str:
    if not j:
        return fallback
    return str(
        j.get("error_description") or j.get("msg") or j.get("error") or fallback or ""
    )[:300]


def _get_app_access_token(app_id: str, app_secret: str) -> str:
    """App access token for legacy ``authen/v1/access_token`` code exchange."""
    app_id = (app_id or "").strip()
    app_secret = (app_secret or "").strip()
    if not app_id or not app_secret:
        return ""
    payload = {"app_id": app_id, "app_secret": app_secret}
    for base in _OAUTH_TOKEN_BASES:
        url = f"{base.rstrip('/')}/auth/v3/app_access_token/internal"
        try:
            r = _lark_http().post(url, json=payload, **_timeout_kw())
            j, err = _parse_lark_json(r, label="app_access_token")
            if not j or not _oauth_response_ok(j):
                log.warning(
                    "vc_user_oauth: app_access_token failed base=%s http=%s code=%s msg=%s",
                    base,
                    r.status_code,
                    (j or {}).get("code"),
                    _lark_api_msg(j, fallback=err),
                )
                continue
            tok = str(j.get("app_access_token") or "").strip()
            if tok:
                return tok
        except Exception as e:
            log.warning("vc_user_oauth: app_access_token exception base=%s err=%s", base, e)
    return ""


def _store_exchanged_tokens(
    *,
    access: str,
    refresh: str,
    expires_in: int,
    open_id: str,
    oauth_version: str,
) -> Tuple[bool, str, str]:
    token_open_id = (open_id or "").strip()
    access = (access or "").strip()
    if not access or not token_open_id:
        return False, "", "empty access_token or open_id after exchange"
    row = {
        "open_id": token_open_id,
        "access_token": access,
        "refresh_token": (refresh or "").strip(),
        "expires_at": int(time.time()) + max(60, int(expires_in or 7200) - 120),
        "updated_at": int(time.time()),
        "oauth_version": oauth_version,
    }
    _save_user_token_row(token_open_id, row)
    log.info(
        "vc_user_oauth: stored token open_id_tail=%s expires_in=%s refresh=%s via=%s",
        token_open_id[-8:],
        expires_in,
        bool(refresh),
        oauth_version,
    )
    return True, token_open_id, "authorized"


def _exchange_v2_authorization_code(
    code: str,
    *,
    app_id: str,
    app_secret: str,
    redirect: str,
) -> Tuple[Optional[Dict[str, Any]], str, str]:
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": app_id,
        "client_secret": app_secret,
        "redirect_uri": redirect,
    }
    headers = {"Content-Type": "application/json; charset=utf-8"}
    last_err = "v2 token exchange failed"
    for base in _OAUTH_TOKEN_BASES:
        url = f"{base.rstrip('/')}/authen/v2/oauth/token"
        try:
            r = _lark_http().post(url, json=payload, headers=headers, **_timeout_kw())
            j, parse_err = _parse_lark_json(r, label="v2/oauth/token")
            if not j:
                last_err = parse_err
                continue
            if not _oauth_response_ok(j):
                last_err = _lark_api_msg(j, fallback=parse_err)
                log.warning(
                    "vc_user_oauth: v2 code exchange failed base=%s http=%s code=%s msg=%s",
                    base,
                    r.status_code,
                    j.get("code"),
                    last_err,
                )
                continue
            access = str(j.get("access_token") or "").strip()
            if not access:
                last_err = "empty access_token in v2 response"
                continue
            return (
                {
                    "access_token": access,
                    "refresh_token": str(j.get("refresh_token") or "").strip(),
                    "expires_in": int(j.get("expires_in") or 7200),
                    "open_id": "",
                },
                "v2",
                "",
            )
        except Exception as e:
            last_err = str(e)
            log.warning("vc_user_oauth: v2 exchange exception base=%s err=%s", base, e)
    return None, "", last_err


def _exchange_v1_authorization_code(code: str, *, app_access_token: str) -> Tuple[Optional[Dict[str, Any]], str, str]:
    tok = (app_access_token or "").strip()
    if not tok:
        return None, "", "no app_access_token"
    payload = {"grant_type": "authorization_code", "code": code}
    headers = {
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/json; charset=utf-8",
    }
    last_err = "v1 token exchange failed"
    for base in _OAUTH_TOKEN_BASES:
        url = f"{base.rstrip('/')}/authen/v1/access_token"
        try:
            r = _lark_http().post(url, json=payload, headers=headers, **_timeout_kw())
            j, parse_err = _parse_lark_json(r, label="v1/access_token")
            if not j:
                last_err = parse_err
                continue
            if not _oauth_response_ok(j):
                last_err = _lark_api_msg(j, fallback=parse_err)
                log.warning(
                    "vc_user_oauth: v1 code exchange failed base=%s http=%s code=%s msg=%s",
                    base,
                    r.status_code,
                    j.get("code"),
                    last_err,
                )
                continue
            data = j.get("data") or {}
            access = str(data.get("access_token") or "").strip()
            if not access:
                last_err = "empty access_token in v1 response"
                continue
            return (
                {
                    "access_token": access,
                    "refresh_token": str(data.get("refresh_token") or "").strip(),
                    "expires_in": int(data.get("expires_in") or 7200),
                    "open_id": str(data.get("open_id") or "").strip(),
                },
                "v1",
                "",
            )
        except Exception as e:
            last_err = str(e)
            log.warning("vc_user_oauth: v1 exchange exception base=%s err=%s", base, e)
    return None, "", last_err


def _exchange_oidc_authorization_code(
    code: str,
    *,
    app_id: str,
    app_secret: str,
    redirect: str,
) -> Tuple[Optional[Dict[str, Any]], str, str]:
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": app_id,
        "client_secret": app_secret,
        "redirect_uri": redirect,
    }
    headers = {"Content-Type": "application/json; charset=utf-8"}
    last_err = "oidc token exchange failed"
    for base in _OAUTH_TOKEN_BASES:
        url = f"{base.rstrip('/')}/authen/v1/oidc/access_token"
        try:
            r = _lark_http().post(url, json=payload, headers=headers, **_timeout_kw())
            j, parse_err = _parse_lark_json(r, label="oidc/access_token")
            if not j:
                last_err = parse_err
                continue
            if not _oauth_response_ok(j):
                last_err = _lark_api_msg(j, fallback=parse_err)
                log.warning(
                    "vc_user_oauth: oidc code exchange failed base=%s http=%s code=%s msg=%s",
                    base,
                    r.status_code,
                    j.get("code"),
                    last_err,
                )
                continue
            data = j.get("data") or {}
            access = str(data.get("access_token") or "").strip()
            if not access:
                last_err = "empty access_token in oidc response"
                continue
            return (
                {
                    "access_token": access,
                    "refresh_token": str(data.get("refresh_token") or "").strip(),
                    "expires_in": int(data.get("expires_in") or 7200),
                    "open_id": str(data.get("open_id") or "").strip(),
                },
                "oidc",
                "",
            )
        except Exception as e:
            last_err = str(e)
            log.warning("vc_user_oauth: oidc exchange exception base=%s err=%s", base, e)
    return None, "", last_err


def _fetch_open_id_from_user_info(access_token: str) -> str:
    tok = (access_token or "").strip()
    if not tok:
        return ""
    headers = {"Authorization": f"Bearer {tok}"}
    for base in _OAUTH_USER_INFO_BASES:
        url = f"{base.rstrip('/')}/authen/v1/user_info"
        try:
            r = _lark_http().get(url, headers=headers, **_timeout_kw())
            j, err = _parse_lark_json(r, label="user_info")
            if not j or not _oauth_response_ok(j):
                log.warning(
                    "vc_user_oauth: user_info failed base=%s code=%s msg=%s err=%s",
                    base,
                    (j or {}).get("code"),
                    ((j or {}).get("msg") or (j or {}).get("error_description") or "")[:200],
                    err,
                )
                continue
            data = j.get("data") or {}
            oid = str(data.get("open_id") or "").strip()
            if oid:
                return oid
        except Exception as e:
            log.warning("vc_user_oauth: user_info exception base=%s err=%s", base, e)
    return ""


def build_authorize_url(open_id: str) -> str:
    """Browser URL for duty to grant ``vc:meeting`` (one-time per user)."""
    app_id, _ = _config.get_lark_primary_app_credentials()
    app_id = (app_id or "").strip()
    redirect = _config.get_p0_vc_oauth_redirect_uri()
    base_public = (_config.get_p0_vc_oauth_public_base_url() or "").strip().rstrip("/")
    oid = (open_id or "").strip()
    if not app_id or not redirect:
        return ""
    # Optional: link via our start route so state is bound to open_id in logs.
    if base_public and oid:
        return f"{base_public}/lark/oauth/start?open_id={urllib.parse.quote(oid, safe='')}"
    return build_oauth_authorize_redirect_url(
        app_id=app_id,
        redirect=redirect,
        scope=_config.get_p0_vc_oauth_scope(),
        state=oid,
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

    hint = (open_id_hint or "").strip()
    errors: List[str] = []
    try:
        app_tok = _get_app_access_token(app_id, app_secret)
        attempts = (
            ("v2", lambda: _exchange_v2_authorization_code(
                code, app_id=app_id, app_secret=app_secret, redirect=redirect
            )),
            ("v1", lambda: _exchange_v1_authorization_code(code, app_access_token=app_tok)),
            ("oidc", lambda: _exchange_oidc_authorization_code(
                code, app_id=app_id, app_secret=app_secret, redirect=redirect
            )),
        )
        for label, fn in attempts:
            row, via, err = fn()
            if err:
                errors.append(f"{label}: {err}")
            if not row:
                continue
            access = str(row.get("access_token") or "").strip()
            refresh = str(row.get("refresh_token") or "").strip()
            expires_in = int(row.get("expires_in") or 7200)
            token_open_id = str(row.get("open_id") or "").strip()
            if not token_open_id and via == "v2":
                token_open_id = _fetch_open_id_from_user_info(access)
            if not token_open_id and hint.startswith("ou_"):
                token_open_id = hint
            if not token_open_id:
                errors.append(f"{via}: could not resolve open_id")
                continue
            return _store_exchanged_tokens(
                access=access,
                refresh=refresh,
                expires_in=expires_in,
                open_id=token_open_id,
                oauth_version=via,
            )
        summary = "; ".join(errors) if errors else "token exchange failed"
        log.warning(
            "vc_user_oauth: all exchange attempts failed redirect_host=%s app_tail=%s errors=%s",
            urllib.parse.urlparse(redirect).netloc or "?",
            app_id[-6:] if app_id else "?",
            summary[:500],
        )
        return False, "", summary
    except Exception as e:
        log.warning("vc_user_oauth: exchange exception: %s", e)
        return False, "", str(e)


def get_user_access_token(open_id: str, *, force_refresh: bool = False) -> str:
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
    if not force_refresh and access and expires_at > now:
        return access
    refresh = str(row.get("refresh_token") or "").strip()
    if not refresh:
        return access
    app_id, app_secret = _config.get_lark_primary_app_credentials()
    app_id = (app_id or "").strip()
    app_secret = (app_secret or "").strip()
    if not app_id or not app_secret:
        return access
    oauth_version = str(row.get("oauth_version") or "v2").strip()
    try:
        if oauth_version == "v1":
            app_tok = _get_app_access_token(app_id, app_secret)
            if app_tok:
                payload = {"grant_type": "refresh_token", "refresh_token": refresh}
                headers = {
                    "Authorization": f"Bearer {app_tok}",
                    "Content-Type": "application/json; charset=utf-8",
                }
                for base in _OAUTH_TOKEN_BASES:
                    url = f"{base.rstrip('/')}/authen/v1/refresh_access_token"
                    r = _lark_http().post(url, json=payload, headers=headers, **_timeout_kw())
                    j, parse_err = _parse_lark_json(r, label="v1/refresh_access_token")
                    if not j or not _oauth_response_ok(j):
                        log.warning(
                            "vc_user_oauth: v1 refresh failed open_id_tail=%s base=%s code=%s msg=%s",
                            oid[-8:],
                            base,
                            (j or {}).get("code"),
                            _lark_api_msg(j, fallback=parse_err),
                        )
                        continue
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
                        log.info("vc_user_oauth: refreshed (v1) open_id_tail=%s", oid[-8:])
                    return access
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": app_id,
            "client_secret": app_secret,
        }
        headers = {"Content-Type": "application/json; charset=utf-8"}
        for base in _OAUTH_TOKEN_BASES:
            url = f"{base.rstrip('/')}/authen/v2/oauth/token"
            r = _lark_http().post(url, json=payload, headers=headers, **_timeout_kw())
            j, parse_err = _parse_lark_json(r, label="v2/oauth/token refresh")
            if not j or not _oauth_response_ok(j):
                log.warning(
                    "vc_user_oauth: refresh failed open_id_tail=%s base=%s http=%s code=%s msg=%s",
                    oid[-8:],
                    base,
                    r.status_code,
                    (j or {}).get("code"),
                    _lark_api_msg(j, fallback=parse_err),
                )
                continue
            access = str(j.get("access_token") or "").strip()
            new_refresh = str(j.get("refresh_token") or refresh).strip()
            expires_in = int(j.get("expires_in") or 7200)
            if access:
                row["access_token"] = access
                row["refresh_token"] = new_refresh
                row["expires_at"] = now + max(60, expires_in - 120)
                row["updated_at"] = now
                row["oauth_version"] = "v2"
                _save_user_token_row(oid, row)
                log.info("vc_user_oauth: refreshed open_id_tail=%s", oid[-8:])
            return access
        return access
    except Exception as e:
        log.warning("vc_user_oauth: refresh exception open_id_tail=%s err=%s", oid[-8:], e)
        return access
