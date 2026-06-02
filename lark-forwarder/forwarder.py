"""
Minimal Lark overview forwarder — overview-only bot posts to broadcast group via incoming webhook.

Primary ``lark-ops-ai`` calls ``POST /post-overview`` with markdown; this service relays to the
broadcast group's custom bot webhook (no primary bot membership required in that group).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import requests
from fastapi import FastAPI, Header, HTTPException, Request

log = logging.getLogger("lark-forwarder")

app = FastAPI()

DEFAULT_WEBHOOK_URL = (os.getenv("LARK_FORWARDER_WEBHOOK_URL") or os.getenv("WEBHOOK_URL") or "").strip()
FORWARDER_SECRET = (os.getenv("LARK_FORWARDER_SECRET") or os.getenv("LARK_OVERVIEW_FORWARDER_SECRET") or "").strip()


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


def _webhook_for_chat(chat_id: str) -> str:
    cid = (chat_id or "").strip()
    if cid.startswith("oc_") and cid in WEBHOOK_MAP:
        return WEBHOOK_MAP[cid]
    return DEFAULT_WEBHOOK_URL


def _check_auth(authorization: Optional[str]) -> None:
    if not FORWARDER_SECRET:
        return
    auth = (authorization or "").strip()
    if auth == f"Bearer {FORWARDER_SECRET}" or auth == FORWARDER_SECRET:
        return
    raise HTTPException(status_code=401, detail="unauthorized")


def _post_text_to_webhook(webhook_url: str, text: str) -> tuple[bool, int, str]:
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
        "webhook_configured": bool(DEFAULT_WEBHOOK_URL or WEBHOOK_MAP),
        "webhook_map_entries": len(WEBHOOK_MAP),
    }


@app.post("/post-overview")
async def post_overview(request: Request, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    _check_auth(authorization)
    body = await request.json()
    text = (body.get("text") or body.get("markdown") or "").strip()
    chat_id = (body.get("chat_id") or "").strip()
    webhook = _webhook_for_chat(chat_id)
    if not text:
        return {"ok": False, "error": "missing text"}
    if not webhook:
        return {"ok": False, "error": "no webhook for chat_id", "chat_id": chat_id}
    ok, status, detail = _post_text_to_webhook(webhook, text)
    return {
        "ok": ok,
        "chat_id": chat_id or None,
        "lark_status": status,
        "detail": detail if not ok else "",
    }


@app.post("/webhook")
async def webhook(request: Request):
    """Legacy Lark event relay (unchanged behavior)."""
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
