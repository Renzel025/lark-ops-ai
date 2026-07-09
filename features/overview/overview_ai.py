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
from typing import Optional, Tuple

from p0_logic import config as _config
from p0_logic import groq_client as _groq

log = logging.getLogger("lark-ops-ai")

_OVERVIEW_MAX_TOKENS = 520


def _run_provider(provider: str, system_prompt: str, user_prompt: str) -> str:
    """One model round trip for ``provider``. Returns raw text ('' on failure)."""
    if provider == "claude":
        from p0_logic.anthropic_client import anthropic_chat_once

        model = _config.get_overview_anthropic_model() or None
        return anthropic_chat_once(system_prompt, user_prompt, max_tokens=_OVERVIEW_MAX_TOKENS, model=model)
    if provider == "groq":
        return _groq.groq_chat_once(system_prompt, user_prompt, max_tokens=_OVERVIEW_MAX_TOKENS)
    return ""


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
