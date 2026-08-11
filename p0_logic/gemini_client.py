"""
Google Gemini API — chat for P0/P1 keyword triage (Flash models).
"""
from __future__ import annotations

import logging
import math
import os
from typing import List, Optional

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


def _embed_request(model: str, text: str, task_type: str, dims: int) -> dict:
    req: dict = {
        "model": f"models/{model}",
        "content": {"parts": [{"text": (text or "")[:8000]}]},
        "taskType": task_type,
    }
    if dims > 0:
        req["outputDimensionality"] = dims
    return req


def _l2_normalize(vec: List[float]) -> List[float]:
    n = math.sqrt(sum(x * x for x in vec))
    return [x / n for x in vec] if n else vec


def gemini_embed_texts(
    texts: List[str],
    *,
    model: str = "",
    task_type: str = "RETRIEVAL_DOCUMENT",
    dims: int = 0,
) -> List[List[float]]:
    """
    Embed texts with Gemini (default ``text-embedding-004``). Returns one vector per input,
    or ``[]`` on failure — callers fall back rather than raise.

    ``task_type`` matters for retrieval quality: embed the corpus as ``RETRIEVAL_DOCUMENT`` and the
    incoming question as ``RETRIEVAL_QUERY`` so the two land in the same space asymmetrically.
    Anthropic has no embeddings API, which is why this lives on Gemini.

    ``dims`` truncates the vector (Matryoshka) — 768 instead of 3072 keeps ranking quality while
    making the cached index 4x smaller and cosine 4x cheaper. Truncated vectors are re-normalised,
    which Google requires for the similarity to stay meaningful.
    """
    key = _api_key()
    items = [str(t or "").strip() for t in (texts or []) if str(t or "").strip()]
    if not key or not items:
        return []
    mdl = (model or "").strip() or _config.get_p0_rag_embed_model()
    d = int(dims or _config.get_p0_rag_embed_dims())
    url = f"{GEMINI_API_BASE}/models/{mdl}:batchEmbedContents"
    out: List[List[float]] = []
    # The API caps a batch at 100 inputs.
    for i in range(0, len(items), 100):
        batch = items[i : i + 100]
        payload = {
            "requests": [
                _embed_request(mdl, t, task_type, d)
                for t in batch
            ]
        }
        try:
            resp = requests.post(
                f"{url}?key={key}", json=payload, timeout=max(30, _config.REQ_TIMEOUT)
            )
        except requests.RequestException as e:
            log.warning("gemini_embed_texts: request failed: %s", e)
            return []
        if resp.status_code != 200:
            log.warning(
                "gemini_embed_texts: HTTP=%s model=%s body=%s",
                resp.status_code,
                mdl,
                (resp.text or "")[:300],
            )
            return []
        try:
            rows = (resp.json() or {}).get("embeddings") or []
            for r in rows:
                vals = [float(x) for x in (r.get("values") or [])]
                if vals:
                    out.append(_l2_normalize(vals))
        except Exception as e:  # noqa: BLE001
            log.warning("gemini_embed_texts: parse failed: %s", e)
            return []
    if len(out) != len(items):
        log.warning("gemini_embed_texts: got %s vectors for %s inputs", len(out), len(items))
        return []
    return out
