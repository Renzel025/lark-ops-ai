"""
Groq API: chat completion, vision OCR, translation to Chinese.
"""
from __future__ import annotations

import base64
import logging
import time
from typing import Optional

import requests

from . import config as _config
from .perf_log import perf_log
from . import text_processing as _text

log = logging.getLogger("lark-ops-ai")

GROQ_API_KEY = _config.GROQ_API_KEY
GROQ_BASE = _config.GROQ_BASE
GROQ_MODEL = _config.GROQ_MODEL
GROQ_VISION_MODEL = _config.GROQ_VISION_MODEL


def _timeout_kw():
    return _config.timeout_kw()


def groq_chat_once(system_prompt: str, user_content: str, max_tokens: int, model: Optional[str] = None) -> str:
    if not GROQ_API_KEY:
        return ""
    url = f"{GROQ_BASE}/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": (model or GROQ_MODEL),
        "temperature": 0.2,
        "top_p": 0.9,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    }
    t0 = time.perf_counter()
    try:
        try:
            r = requests.post(url, headers=headers, json=payload, **_timeout_kw())
        except Exception as e:
            log.error("Groq request error: %s", e)
            return ""
        if r.status_code != 200:
            log.error("Groq API error: %s - %s", r.status_code, (r.text or "")[:200])
            return ""
        try:
            j = r.json() if r.text else {}
            return (j.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        except Exception:
            return ""
    finally:
        perf_log(f"groq_chat_once model={model or GROQ_MODEL}", t0)


def groq_vision_ocr(image_bytes: bytes) -> str:
    if not GROQ_API_KEY or not image_bytes:
        return ""
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:image/jpeg;base64,{b64}"
    url = f"{GROQ_BASE}/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    system_prompt = (
        "You extract ONLY visible text from screenshots.\n"
        "Rules:\n"
        "- Output only text visible in the image.\n"
        "- Preserve names, @mentions, IDs, timestamps, numbers, and line breaks.\n"
        "- Do not summarize.\n"
        "- Do not infer.\n"
        "- Do not create templates.\n"
        "- Do not add labels like Player ID, Game Name, Error Code unless they are literally visible.\n"
        "- If a part is unreadable, skip that part instead of inventing."
    )
    payload = {
        "model": GROQ_VISION_MODEL,
        "temperature": 0.0,
        "max_tokens": 2200,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Extract the screenshot text exactly as visible."},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
    }
    t0 = time.perf_counter()
    try:
        try:
            r = requests.post(url, headers=headers, json=payload, **_timeout_kw())
        except Exception as e:
            log.error("Groq vision request error: %s", e)
            return ""
        if r.status_code != 200:
            log.error("Groq vision error HTTP=%s head=%s", r.status_code, (r.text or "")[:300])
            return ""
        try:
            j = r.json() if r.text else {}
            return (j.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        except Exception as e:
            log.error("Groq vision parse error: %s", e)
            return ""
    finally:
        perf_log(f"groq_vision_ocr model={GROQ_VISION_MODEL}", t0)


def translate_to_zh(text: str) -> str:
    src = (text or "").strip()
    if not src:
        return ""
    if _text.is_not_specified(src):
        return "未指定"
    if _text.looks_like_chinese(src):
        return _text.normalize_gaming_zh(_text.clean_single_line_translation(src))
    system_prompt = (
        "You are a professional translator for gaming incident reports.\n"
        "Translate the user's text into Simplified Chinese.\n"
        "STRICT RULES:\n"
        "- Translate ONLY the provided text.\n"
        "- Do NOT add explanations.\n"
        "- Do NOT add examples.\n"
        "- Do NOT summarize.\n"
        "- Do NOT expand the content.\n"
        "- Do NOT create incident IDs, templates, bullets, or extra paragraphs.\n"
        "- Keep numbers, IDs, acronyms, product names, and timestamps exactly as they are.\n"
        "- Use 玩家 for game players, not 球员.\n"
        "- Output exactly ONE concise line of Simplified Chinese only.\n"
    )
    out = groq_chat_once(system_prompt, src, max_tokens=120)
    cleaned = _text.clean_single_line_translation(out)
    if not cleaned:
        return src
    return _text.normalize_gaming_zh(cleaned)
