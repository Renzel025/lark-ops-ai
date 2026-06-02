"""
Lark overview forwarder — posts overview text to broadcast group(s).

Primary ``lark-ops-ai`` calls ``POST /post-overview`` with markdown + ``chat_id``.

Preferred: **Overview Lark app** already added to the broadcast group —
``LARK_APP_ID`` + ``LARK_APP_SECRET`` + ``oc_`` chat id (env and/or request body).

Optional fallback: incoming **custom bot webhook** (``LARK_FORWARDER_WEBHOOK_URL``).
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple

import requests
from fastapi import FastAPI, Header, HTTPException, Request

log = logging.getLogger("lark-forwarder")

app = FastAPI()

LARK_BASE = (os.getenv("LARK_OPEN_API_BASE") or "https://open-sg.larksuite.com/open-apis").strip().rstrip("/")
LARK_APP_ID = (os.getenv("LARK_OVERVIEW_APP_ID") or os.getenv("LARK_APP_ID") or "").strip()
LARK_APP_SECRET = (os.getenv("LARK_OVERVIEW_APP_SECRET") or os.getenv("LARK_APP_SECRET") or "").strip()
DEFAULT_BROADCAST_CHAT_ID = (
    os.getenv("LARK_FORWARDER_BROADCAST_CHAT_ID")
    or os.getenv("LARK_OVERVIEW_BROADCAST_CHAT_ID")
    or ""
).strip()
DEFAULT_WEBHOOK_URL = (os.getenv("LARK_FORWARDER_WEBHOOK_URL") or os.getenv("WEBHOOK_URL") or "").strip()
FORWARDER_SECRET = (os.getenv("LARK_FORWARDER_SECRET") or os.getenv("LARK_OVERVIEW_FORWARDER_SECRET") or "").strip()

_TOKEN_CACHE: Dict[str, Any] = {}


def _parse_webhook_map(raw: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in (raw or "").split(","):
        p = part.strip()
        if "=" not in p:
            continue
        k, v = p.split("=", 1)
        k, v = k.strip(), v.strip()
        if k.startswith("oc_") and v.startswith("http"):
            out[k] = v
    return out


WEBHOOK_MAP: Dict[str, str] = _parse_webhook_map(os.getenv("LARK_FORWARDER_WEBHOOK_MAP") or "")


def _lark_app_configured() -> bool:
    return bool(LARK_APP_ID and LARK_APP_SECRET)


def _webhook_for_chat(chat_id: str) -> str:
    cid = (chat_id or "").strip()
    if cid.startswith("oc_") and cid in WEBHOOK_MAP:
        return WEBHOOK_MAP[cid]
    return DEFAULT_WEBHOOK_URL


def _resolve_chat_id(request_chat_id: str) -> str:
    cid = (request_chat_id or "").strip()
    if cid.startswith("oc_"):
        return cid
    if DEFAULT_BROADCAST_CHAT_ID.startswith("oc_"):
        return DEFAULT_BROADCAST_CHAT_ID
    return ""


def _check_auth(authorization: Optional[str]) -> None:
    if not FORWARDER_SECRET:
        return
    auth = (authorization or "").strip()
    if auth == f"Bearer {FORWARDER_SECRET}" or auth == FORWARDER_SECRET:
        return
    raise HTTPException(status_code=401, detail="unauthorized")


def _get_tenant_token() -> str:
    if not _lark_app_configured():
        return ""
    now = int(time.time())
    if _TOKEN_CACHE.get("token") and now < int(_TOKEN_CACHE.get("exp") or 0):
        return str(_TOKEN_CACHE["token"])
    url = f"{LARK_BASE}/auth/v3/tenant_access_token/internal"
    try:
        r = requests.post(
            url,
            json={"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET},
            timeout=15,
        )
        data = r.json() if r.text else {}
        if data.get("code") != 0:
            log.warning("tenant token failed code=%s msg=%s", data.get("code"), data.get("msg"))
            return ""
        tok = (data.get("tenant_access_token") or "").strip()
        if not tok:
            return ""
        exp = int(data.get("expire") or 3600)
        _TOKEN_CACHE["token"] = tok
        _TOKEN_CACHE["exp"] = now + exp - 120
        return tok
    except Exception as e:
        log.warning("tenant token error: %s", e)
        return ""


def _post_text_via_lark_app(chat_id: str, text: str) -> Tuple[bool, int, str]:
    cid = (chat_id or "").strip()
    if not cid.startswith("oc_") or not text.strip():
        return False, 0, "missing chat_id or text"
    token = _get_tenant_token()
    if not token:
        return False, 0, "tenant token failed — check LARK_APP_ID / LARK_APP_SECRET"
    url = f"{LARK_BASE}/im/v1/messages?receive_id_type=chat_id"
    payload = {
        "receive_id": cid,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }
    try:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=15,
        )
        body = r.json() if r.text else {}
        ok = r.status_code == 200 and body.get("code") == 0
        if not ok:
            log.warning(
                "lark im post failed HTTP=%s code=%s msg=%s chat_id=%s",
                r.status_code,
                body.get("code"),
                body.get("msg"),
                cid[:28],
            )
            return False, r.status_code, json.dumps(body, ensure_ascii=False)[:500]
        return True, r.status_code, ""
    except Exception as e:
        log.warning("lark im post error chat_id=%s err=%s", cid[:28], e)
        return False, 0, str(e)


def _post_text_to_webhook(webhook_url: str, text: str) -> Tuple[bool, int, str]:
    url = (webhook_url or "").strip()
    if not url or not text.strip():
        return False, 0, "missing webhook or text"
    try:
        r = requests.post(
            url,
            json={"msg_type": "text", "content": {"text": text}},
            timeout=15,
        )
        ok = r.status_code == 200
        if not ok:
            log.warning("webhook post failed HTTP=%s body=%s", r.status_code, (r.text or "")[:300])
        return ok, r.status_code, (r.text or "")[:500]
    except Exception as e:
        log.warning("webhook post error: %s", e)
        return False, 0, str(e)


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "mode": "lark_app" if _lark_app_configured() else ("webhook" if DEFAULT_WEBHOOK_URL or WEBHOOK_MAP else "unconfigured"),
        "lark_app_configured": _lark_app_configured(),
        "default_broadcast_chat_id": DEFAULT_BROADCAST_CHAT_ID[:28] + "..." if len(DEFAULT_BROADCAST_CHAT_ID) > 28 else DEFAULT_BROADCAST_CHAT_ID or None,
        "webhook_configured": bool(DEFAULT_WEBHOOK_URL or WEBHOOK_MAP),
        "webhook_map_entries": len(WEBHOOK_MAP),
    }


@app.post("/post-overview")
async def post_overview(request: Request, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    _check_auth(authorization)
    body = await request.json()
    text = (body.get("text") or body.get("markdown") or "").strip()
    chat_id = _resolve_chat_id((body.get("chat_id") or "").strip())
    if not text:
        return {"ok": False, "error": "missing text"}
    if not chat_id.startswith("oc_"):
        return {"ok": False, "error": "missing chat_id — set LARK_FORWARDER_BROADCAST_CHAT_ID or pass chat_id in body"}

    if _lark_app_configured():
        ok, status, detail = _post_text_via_lark_app(chat_id, text)
        return {
            "ok": ok,
            "mode": "lark_app",
            "chat_id": chat_id,
            "lark_status": status,
            "detail": detail if not ok else "",
        }

    webhook = _webhook_for_chat(chat_id)
    if not webhook:
        return {
            "ok": False,
            "error": "no lark app credentials and no webhook — configure LARK_APP_ID/SECRET or LARK_FORWARDER_WEBHOOK_URL",
            "chat_id": chat_id,
        }
    ok, status, detail = _post_text_to_webhook(webhook, text)
    return {
        "ok": ok,
        "mode": "webhook",
        "chat_id": chat_id,
        "lark_status": status,
        "detail": detail if not ok else "",
    }


@app.post("/webhook")
async def webhook(request: Request):
    """Legacy Lark event relay."""
    data = await request.json()
    if "challenge" in data:
        return {"challenge": data["challenge"]}
    try:
        msg = data["event"]["message"]["content"]
    except Exception:
        return {"code": 0}
    if DEFAULT_WEBHOOK_URL:
        requests.post(
            DEFAULT_WEBHOOK_URL,
            json={"msg_type": "text", "content": {"text": msg}},
            timeout=15,
        )
    return {"code": 0}
