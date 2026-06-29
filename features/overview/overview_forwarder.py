"""
HTTP client for lark-forwarder — posts overview card via the overview-only Lark app.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

import requests

from p0_logic import config as _config

log = logging.getLogger("lark-ops-ai")


def _forwarder_endpoint(suffix: str) -> str:
    url_base = _config.get_lark_overview_forwarder_url()
    if not url_base:
        return ""
    path = suffix if suffix.startswith("/") else f"/{suffix}"
    if url_base.endswith(path):
        return url_base
    return f"{url_base.rstrip('/')}{path}"


def _forwarder_headers() -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    secret = _config.get_lark_overview_forwarder_secret()
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    return headers


def post_overview_via_forwarder(
    markdown_text: str,
    *,
    chat_id: str,
    priority: str = "",
    source_label: str = "",
    card: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """
    POST overview to ``lark-forwarder`` ``/post-overview``.
    Returns ``(ok, message_id)`` — ``message_id`` is set when the forwarder used the Overview Lark app.
    """
    endpoint = _forwarder_endpoint("/post-overview")
    if not endpoint:
        return False, ""
    text = (markdown_text or "").strip()
    dest = (chat_id or "").strip()
    if not text or not dest.startswith("oc_"):
        log.warning("overview_forwarder: skipped — empty text or invalid chat_id=%r", dest[:28] if dest else dest)
        return False, ""
    payload = {
        "text": text,
        "markdown": text,
        "chat_id": dest,
        "priority": (priority or "").strip(),
        "source_label": (source_label or "").strip(),
    }
    if isinstance(card, dict) and card:
        payload["card"] = card
    try:
        r = requests.post(endpoint, json=payload, headers=_forwarder_headers(), **_config.timeout_kw())
        body = r.json() if r.content else {}
        ok = r.status_code == 200 and isinstance(body, dict) and body.get("ok") is True
        mid = str(body.get("message_id") or "").strip() if ok else ""
        if ok:
            log.info(
                "overview_forwarder: posted broadcast overview chat_id=%s message_id=%s status=%s",
                dest,
                mid[:24] + "…" if len(mid) > 24 else mid or "(none)",
                body.get("lark_status") or r.status_code,
            )
            return True, mid
        log.warning(
            "overview_forwarder: failed HTTP=%s chat_id=%s body=%s",
            r.status_code,
            dest,
            (r.text or "")[:400],
        )
        return False, ""
    except Exception as e:
        log.warning("overview_forwarder: request failed chat_id=%s err=%s", dest, e)
        return False, ""


def patch_overview_via_forwarder(message_id: str, card: Dict[str, Any]) -> bool:
    """PATCH an overview card posted by the Overview bot (``lark-forwarder`` ``/patch-overview``)."""
    endpoint = _forwarder_endpoint("/patch-overview")
    mid = (message_id or "").strip()
    if not endpoint or not mid or not isinstance(card, dict):
        return False
    try:
        r = requests.post(
            endpoint,
            json={"message_id": mid, "card": card},
            headers=_forwarder_headers(),
            **_config.timeout_kw(),
        )
        body = r.json() if r.content else {}
        ok = r.status_code == 200 and isinstance(body, dict) and body.get("ok") is True
        if ok:
            log.info("overview_forwarder: patched broadcast message_id=%s", mid[:24] + "…" if len(mid) > 24 else mid)
            return True
        log.warning(
            "overview_forwarder: patch failed HTTP=%s message_id=%s body=%s",
            r.status_code,
            mid[:24],
            (r.text or "")[:400],
        )
        return False
    except Exception as e:
        log.warning("overview_forwarder: patch failed message_id=%s err=%s", mid[:24], e)
        return False
