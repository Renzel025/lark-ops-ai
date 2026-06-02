"""
HTTP client for lark-forwarder — posts overview text via the overview-only bot webhook.
"""
from __future__ import annotations

import logging

import requests

from . import config as _config

log = logging.getLogger("lark-ops-ai")


def post_overview_via_forwarder(
    markdown_text: str,
    *,
    chat_id: str,
    priority: str = "",
    source_label: str = "",
) -> bool:
    """
    POST overview markdown to ``lark-forwarder`` ``/post-overview``.
    Returns True when the forwarder responds with ``ok: true``.
    """
    url_base = _config.get_lark_overview_forwarder_url()
    if not url_base:
        return False
    text = (markdown_text or "").strip()
    dest = (chat_id or "").strip()
    if not text or not dest.startswith("oc_"):
        log.warning("overview_forwarder: skipped — empty text or invalid chat_id=%r", dest[:28] if dest else dest)
        return False
    endpoint = url_base if url_base.endswith("/post-overview") else f"{url_base.rstrip('/')}/post-overview"
    headers = {"Content-Type": "application/json"}
    secret = _config.get_lark_overview_forwarder_secret()
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    payload = {
        "text": text,
        "markdown": text,
        "chat_id": dest,
        "priority": (priority or "").strip(),
        "source_label": (source_label or "").strip(),
    }
    try:
        r = requests.post(endpoint, json=payload, headers=headers, **_config.timeout_kw())
        body = r.json() if r.content else {}
        ok = r.status_code == 200 and isinstance(body, dict) and body.get("ok") is True
        if ok:
            log.info(
                "overview_forwarder: posted broadcast overview chat_id=%s status=%s",
                dest,
                body.get("lark_status") or r.status_code,
            )
            return True
        log.warning(
            "overview_forwarder: failed HTTP=%s chat_id=%s body=%s",
            r.status_code,
            dest,
            (r.text or "")[:400],
        )
        return False
    except Exception as e:
        log.warning("overview_forwarder: request failed chat_id=%s err=%s", dest, e)
        return False
