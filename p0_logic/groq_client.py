"""
Groq API: chat completion, vision OCR, translation to Chinese.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Tuple

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


def translate_issue_impact_pair_to_zh(en_issue: str, en_impact: str) -> Tuple[str, str]:
    """
    Fallback when one-shot overview is off or fails: two Groq calls in parallel.
    """
    t0 = time.perf_counter()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            fi = pool.submit(translate_to_zh, en_issue)
            fj = pool.submit(translate_to_zh, en_impact)
            return fi.result(), fj.result()
    finally:
        perf_log("groq translate_issue_impact_pair", t0)


def _parse_json_object(raw: str) -> Optional[dict]:
    if not (raw or "").strip():
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None


def _scrub_issue_source_for_model(issue_source: str) -> str:
    src = (issue_source or "").strip()
    if not src:
        return ""
    best = _text._pick_best_issue_text(src) or src
    scrubbed = re.sub(r"\[Screenshot\s+\d+\s+OCR\]", " ", best, flags=re.IGNORECASE)
    scrubbed = re.sub(r"\b\d{6,}\b", "<ID>", scrubbed)
    scrubbed = re.sub(r"\b\d{4}[/-]\d{1,2}[/-]\d{1,2}\b", "<DATE>", scrubbed)
    scrubbed = re.sub(r"\s+", " ", scrubbed).strip()
    return scrubbed


def groq_overview_issue_and_zh_bilingual(issue_source: str, impact_en: str) -> Optional[Tuple[str, str, str]]:
    """
    Single Groq round trip: English issue one-liner + zh_issue + zh_impact.
    Replaces summarize_issue + translate_issue_impact_pair (2–3 HTTP calls → 1).
    Returns None on failure so callers fall back to the legacy path.
    """
    if not GROQ_API_KEY:
        return None
    scrubbed = _scrub_issue_source_for_model(issue_source)
    if not scrubbed:
        return None
    impact_line = (impact_en or "").strip() or "Not specified"
    system_prompt = (
        "You are an on-call incident assistant.\n"
        "Output ONLY valid JSON (no markdown fences, no commentary). Keys: issue_en, zh_issue, zh_impact\n"
        "- issue_en: ONE concise English sentence for the user-facing incident symptom. "
        "Use only the incident text. No player IDs, ticket IDs, dates, or @names. No bullets.\n"
        "- zh_issue: ONE line Simplified Chinese: translate issue_en faithfully.\n"
        "- zh_impact: ONE line Simplified Chinese: translate the impact scope English line given below. "
        'If that line is "Not specified" or empty, set zh_impact to 未指定.\n'
        "Use 玩家 for game players, not 球员. Keep acronyms (FPMS, CPMS, …) unchanged in Chinese lines.\n"
    )
    user_prompt = (
        f"Incident text:\n{scrubbed}\n\n"
        f"Impact scope (English — translate this exact line to zh_impact):\n{impact_line}\n"
    )
    t0 = time.perf_counter()
    try:
        raw = groq_chat_once(system_prompt, user_prompt, max_tokens=520)
        obj = _parse_json_object(raw or "")
        if not obj:
            log.warning("groq_overview one-shot: JSON parse failed head=%s", (raw or "")[:200])
            return None
        issue_en = str(obj.get("issue_en") or "").strip()
        zh_issue = str(obj.get("zh_issue") or "").strip()
        zh_impact = str(obj.get("zh_impact") or "").strip()
        if not issue_en:
            return None
        return issue_en, zh_issue, zh_impact
    finally:
        perf_log("groq_overview_issue_zh_one_shot", t0)
