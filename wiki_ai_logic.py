import json
import logging
import os
import requests

from p0_logic.anthropic_client import anthropic_chat_once
from p0_logic.groq_client import groq_chat_once

log = logging.getLogger("lark-ops-ai")

# Direct Document Token from the URL (override with env WIKI_DOC_TOKEN if needed)
OBJ_TOKEN = os.getenv("WIKI_DOC_TOKEN", "O94kwR7YWiRyFkkTVf2lHHzpgbc").strip()


def get_wiki_content(token):
    """
    Fetches text directly from the Docx API.
    Bypasses the Wiki Node API to avoid permission errors from the Wiki Space.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    try:
        doc_url = f"https://open-sg.larksuite.com/open-apis/docx/v1/documents/{OBJ_TOKEN}/raw_content"
        doc_res = requests.get(doc_url, headers=headers).json()
        content = doc_res.get("data", {}).get("content", "")
        if content:
            return content
        print(f"⚠️ Docx API Access Issue: {doc_res.get('msg')}")
    except Exception as e:
        print(f"🚨 Docx Read Error: {str(e)}")
    return ""


def handle_wiki_ai(incoming_text, chat_id, token, groq_key=None):
    """
    Answers from the fetched Docx content: Claude first, Groq (``GROQ_MODEL``) as fallback — the same
    provider order as the rest of the bot. ``groq_key`` is unused (Groq reads ``GROQ_API_KEY``) and
    kept only so existing callers don't break.
    """
    wiki_context = get_wiki_content(token)

    if not wiki_context:
        reply = "I cannot read the document, please check if the bot has 'Viewer' access to the Doc."
    else:
        system = f"You are OSE-AI. Strictly use this document context to answer: {wiki_context}. Be concise."
        reply = ""
        try:
            reply = anthropic_chat_once(system, incoming_text, max_tokens=800)
        except Exception as e:  # noqa: BLE001
            log.warning("wiki_ai: claude failed — trying groq: %s", e)
        if not reply:
            try:
                reply = groq_chat_once(system, incoming_text, max_tokens=800)
            except Exception as e:  # noqa: BLE001
                log.warning("wiki_ai: groq failed: %s", e)
        if not reply:
            reply = "AI Processing Error."

    requests.post(
        "https://open-sg.larksuite.com/open-apis/im/v1/messages?receive_id_type=chat_id",
        headers={"Authorization": f"Bearer {token}"},
        json={"receive_id": chat_id, "msg_type": "text", "content": json.dumps({"text": reply})}
    )
