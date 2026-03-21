"""
Issue summarization and regeneration using Groq.
"""
from __future__ import annotations

import re

from . import groq_client as _groq
from . import text_processing as _text


def summarize_issue(text: str) -> str:
    src = (text or "").strip()
    if not src:
        return "Not specified"
    best_issue_text = _text._pick_best_issue_text(src) or src
    scrubbed = re.sub(r"\[Screenshot\s+\d+\s+OCR\]", " ", best_issue_text, flags=re.IGNORECASE)
    scrubbed = re.sub(r"\b\d{6,}\b", "<ID>", scrubbed)
    scrubbed = re.sub(r"\b\d{4}[/-]\d{1,2}[/-]\d{1,2}\b", "<DATE>", scrubbed)
    scrubbed = re.sub(r"\s+", " ", scrubbed).strip()
    if not scrubbed:
        return "Not specified"
    if not _groq.GROQ_API_KEY:
        cut = re.split(r"[.\n]", scrubbed, maxsplit=1)[0].strip()
        return cut[:240] if cut else "Not specified"
    system_prompt = (
        "You are an on-call incident assistant.\n"
        "Write ONE concise factual issue sentence for a P0/P1 overview.\n"
        "STRICT RULES:\n"
        "- Use only the provided text.\n"
        "- Focus only on the actual incident symptom or user-facing problem.\n"
        "- Ignore names, @mentions, chat headers, screenshot labels, ticket references, and unrelated thread noise.\n"
        "- Do NOT include counts unless essential to the symptom.\n"
        "- Do NOT include player IDs, account IDs, or ticket IDs.\n"
        "- Do NOT include dates or previous incident references.\n"
        "- Do NOT invent anything.\n"
        "- Output exactly one concise sentence only."
    )
    out = _groq.groq_chat_once(system_prompt, scrubbed, max_tokens=90)
    out = (out or "").strip()
    if not out:
        cut = re.split(r"[.\n]", scrubbed, maxsplit=1)[0].strip()
        return cut[:240] if cut else "Not specified"
    out = re.sub(r"\b\d{6,}\b", "", out)
    out = re.sub(r"\b\d{4}[/-]\d{1,2}[/-]\d{1,2}\b", "", out)
    out = re.sub(r"\s+", " ", out).strip(" ,.")
    return out[:240].rstrip() if out else "Not specified"


def regenerate_issue_only(old_issue: str, context_text: str = "") -> str:
    src = (old_issue or "").strip()
    ctx = (context_text or "").strip()
    if not src:
        return "Not specified"
    if not _groq.GROQ_API_KEY:
        return src
    clean_ctx = re.sub(r"\b\d{6,}\b", "<ID>", ctx)
    clean_ctx = re.sub(r"\b\d{4}[/-]\d{1,2}[/-]\d{1,2}\b", "<DATE>", clean_ctx)
    clean_ctx = re.sub(r"\s+", " ", clean_ctx).strip()[:1800]
    system_prompt = (
        "You are an on-call incident assistant.\n"
        "Rewrite the issue sentence into a different but equivalent wording.\n"
        "STRICT RULES:\n"
        "- Return exactly ONE sentence.\n"
        "- Keep the same meaning.\n"
        "- Make it noticeably different from the current issue wording.\n"
        "- Focus only on the actual incident symptom.\n"
        "- Do NOT include player counts.\n"
        "- Do NOT include player IDs, account IDs, or ticket IDs.\n"
        "- Do NOT include dates or references to previous incidents.\n"
        "- Do NOT mention support teams.\n"
        "- Do NOT add assumptions.\n"
        "- Output only the rewritten issue sentence."
    )
    user_prompt = (
        f"Current issue sentence:\n{src}\n\n"
        f"Incident context:\n{clean_ctx}\n\n"
        "Rewrite it with different wording while preserving meaning."
    )
    out = _groq.groq_chat_once(system_prompt, user_prompt, max_tokens=80)
    out = (out or "").strip()
    if not out:
        return src
    out = re.sub(r"\b\d{6,}\b", "", out)
    out = re.sub(r"\b\d{4}[/-]\d{1,2}[/-]\d{1,2}\b", "", out)
    out = re.sub(r"\s+", " ", out).strip(" .,")
    if not out:
        return src
    if out.lower() == src.lower():
        second_prompt = (
            f"Current issue sentence:\n{src}\n\n"
            f"Incident context:\n{clean_ctx}\n\n"
            "Rewrite it again using a clearly different sentence structure. "
            "Do not include counts, IDs, or dates."
        )
        out2 = _groq.groq_chat_once(system_prompt, second_prompt, max_tokens=80).strip()
        if out2:
            out2 = re.sub(r"\b\d{6,}\b", "", out2)
            out2 = re.sub(r"\b\d{4}[/-]\d{1,2}[/-]\d{1,2}\b", "", out2)
            out2 = re.sub(r"\s+", " ", out2).strip(" .,")
            if out2 and out2.lower() != src.lower():
                return out2[:240].rstrip()
    return out[:240].rstrip()
