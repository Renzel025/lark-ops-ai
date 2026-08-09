"""
Provider-aware overview generation (issue one-liner + zh_issue + zh_impact).

Mirrors the P0 Issue Watch AI pattern (``features/issue_watch/issue_watch_ai.py``): a single
prompt shape, run through an ordered provider chain with failover. Provider selection and Claude
auth are config-driven — see ``config.overview_ai_provider_chain`` and ``anthropic_client``
(OAuth preferred, API key fallback). Both DM overview (``drafts.py``) and Issue-Watch overview
(``issue_watch_overview.py``) call ``overview_issue_and_zh_bilingual`` so they stay in lock-step.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional, Tuple

from p0_logic import config as _config
from p0_logic import groq_client as _groq
from p0_logic import text_processing as _text

log = logging.getLogger("lark-ops-ai")

# Bilingual one-shot (issue_en + zh_issue + zh_impact). Chinese is token-heavy, so 520 truncated the
# JSON mid-field (parse failed → no overview). 1200 leaves ample headroom for all three fields.
_OVERVIEW_MAX_TOKENS = 1200

# Cache English→Chinese translations: same text → same result, so repeated edits/saves/regenerates
# of the same issue/impact skip the (slower, all-Claude) LLM call. Bounded in-memory.
_ZH_CACHE: Dict[str, str] = {}
_ZH_CACHE_LOCK = threading.Lock()
_ZH_CACHE_MAX = 500

# Same prompt as groq_client.translate_to_zh so provider output stays consistent.
_ZH_TRANSLATE_SYSTEM = (
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


def _run_provider(
    provider: str,
    system_prompt: str,
    user_prompt: str,
    *,
    max_tokens: int = _OVERVIEW_MAX_TOKENS,
    claude_model: str = "",
) -> str:
    """One model round trip for ``provider``. Returns raw text ('' on failure). ``claude_model`` overrides
    the Claude model (e.g. a fast Haiku for translation); empty = the shared overview model."""
    if provider == "claude":
        from p0_logic.anthropic_client import anthropic_chat_once

        model = (claude_model or "").strip() or _config.get_overview_anthropic_model() or None
        return anthropic_chat_once(system_prompt, user_prompt, max_tokens=max_tokens, model=model)
    if provider == "groq":
        return _groq.groq_chat_once(system_prompt, user_prompt, max_tokens=max_tokens)
    return ""


def _translate_one_zh(text: str) -> str:
    """English → one-line Simplified Chinese via the overview provider chain (claude → groq)."""
    src = (text or "").strip()
    if not src:
        return ""
    if _text.is_not_specified(src):
        return "未指定"
    if _text.looks_like_chinese(src):
        return _text.normalize_gaming_zh(_text.clean_single_line_translation(src))
    with _ZH_CACHE_LOCK:
        hit = _ZH_CACHE.get(src)
    if hit is not None:
        return hit
    out = ""
    # Translation stays on Claude but uses a FAST model (Haiku) so "Save" on an edited overview is
    # quick; the provider chain keeps Claude first (Groq only as an emergency failover if Claude errors).
    _fast_zh_model = _config.get_overview_zh_translate_anthropic_model()
    for provider in _config.overview_ai_provider_chain():
        try:
            out = _run_provider(provider, _ZH_TRANSLATE_SYSTEM, src, max_tokens=160, claude_model=_fast_zh_model)
        except Exception as e:  # noqa: BLE001 — try the next provider
            log.warning("overview zh-translate: provider=%s raised %s", provider, e)
            out = ""
        if (out or "").strip():
            break
    cleaned = _text.clean_single_line_translation(out)
    if not cleaned:
        return src  # don't cache failures — retry next time
    result = _text.normalize_gaming_zh(cleaned)
    with _ZH_CACHE_LOCK:
        if len(_ZH_CACHE) >= _ZH_CACHE_MAX:
            for k in list(_ZH_CACHE)[: _ZH_CACHE_MAX // 5]:  # drop oldest ~20%
                _ZH_CACHE.pop(k, None)
        _ZH_CACHE[src] = result
    return result


def translate_issue_impact_pair(en_issue: str, en_impact: str) -> Tuple[str, str]:
    """(zh_issue, zh_impact) via the provider chain (claude → groq), the two calls in parallel."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        fi = pool.submit(_translate_one_zh, en_issue)
        fj = pool.submit(_translate_one_zh, en_impact)
        return fi.result(), fj.result()


def overview_issue_and_zh_bilingual(issue_source: str, impact_en: str) -> Optional[Tuple[str, str, str]]:
    """
    ``(issue_en, zh_issue, zh_impact)`` via the first provider in ``overview_ai_provider_chain()``
    that returns usable JSON, else ``None`` (caller falls back to ``summarize_issue``).
    Provider order defaults to claude → groq; force with ``P0_OVERVIEW_AI_PROVIDER``.
    """
    prompts = _groq.build_overview_oneshot_prompts(issue_source, impact_en)
    if not prompts:
        return None
    system_prompt, user_prompt = prompts
    chain = _config.overview_ai_provider_chain()
    if not chain:
        return None
    for provider in chain:
        try:
            raw = _run_provider(provider, system_prompt, user_prompt)
        except Exception as e:  # noqa: BLE001 — any provider error falls through to the next
            log.warning("overview one-shot: provider=%s raised %s", provider, e)
            continue
        triplet = _groq.parse_overview_oneshot(raw)
        if triplet:
            log.info("overview one-shot: provider=%s ok", provider)
            return triplet
        log.warning("overview one-shot: provider=%s returned no usable JSON", provider)
    return None
