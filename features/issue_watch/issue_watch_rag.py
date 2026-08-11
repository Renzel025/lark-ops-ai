"""
RAG for Major-P0 detection — grounds the classifier in YOUR P0 SOP instead of a fixed prompt.

Pipeline (all of it lives in this file):

  1. FETCH    the P0/P1 Emergency Flow doc from Lark Docx ``raw_content`` (same trick as
              ``wiki_ai_logic``: hit the Docx API directly to sidestep Wiki-space permissions).
  2. CHUNK    it into overlapping ~700-char passages on paragraph boundaries, so one retrieved
              passage is big enough to carry a rule but small enough to stay on topic.
  3. EMBED    each chunk once with Gemini ``gemini-embedding-001`` (Anthropic has no embeddings API)
              and cache the vectors in ``P0_SHARED_STATE_DIR``, keyed by a hash of the doc text —
              so a restart costs nothing and editing the doc rebuilds automatically.
  4. RETRIEVE the top-K chunks for an incoming chat message by cosine similarity.
  5. INJECT   those chunks into the classifier's system prompt as "OUR P0 SOP SAYS".

Everything degrades to "" on any failure, so detection keeps working exactly as before when the
doc, the API key, or the network is unavailable. Gated by ``P0_ISSUE_WATCH_RAG_ENABLED``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from p0_logic import config as _config
from p0_logic import gemini_client as _gemini
from p0_logic import lark_client as _lark
from features.overview import draft_store as _ds

log = logging.getLogger("lark-ops-ai")

_LOCK = threading.RLock()
_INDEX: Dict[str, Any] = {}          # {"doc_hash", "model", "chunks": [...], "vectors": [[...]]}
_LAST_BUILD_ATTEMPT = 0.0
_BUILD_RETRY_SEC = 300.0             # do not hammer the API when the doc or key is broken


# ---------------------------------------------------------------- 1. fetch


_RESOLVED_DOC_TOKEN: Dict[str, str] = {}   # configured token -> docx obj_token


def _resolve_wiki_node(token: str, tenant_token: str) -> str:
    """A ``/wiki/XXXX`` URL token is a wiki NODE, not a docx token — ``raw_content`` rejects it.

    Resolve it to the underlying ``obj_token`` so operators can paste either form.
    """
    try:
        r = requests.get(
            f"{_config.LARK_BASE}/wiki/v2/spaces/get_node",
            params={"token": token, "obj_type": "wiki"},
            headers={"Authorization": f"Bearer {tenant_token}"},
            timeout=_config.REQ_TIMEOUT,
        )
    except requests.RequestException as e:
        log.warning("issue_watch_rag: wiki get_node failed: %s", e)
        return ""
    if r.status_code != 200:
        log.warning(
            "issue_watch_rag: wiki get_node HTTP=%s body=%s", r.status_code, (r.text or "")[:200]
        )
        return ""
    node = ((r.json() or {}).get("data") or {}).get("node") or {}
    obj_type = str(node.get("obj_type") or "").strip()
    obj_token = str(node.get("obj_token") or "").strip()
    if obj_type != "docx" or not obj_token:
        log.warning(
            "issue_watch_rag: wiki node is obj_type=%r (need docx) title=%r",
            obj_type,
            node.get("title"),
        )
        return ""
    log.info(
        "issue_watch_rag: wiki node resolved title=%r obj_token_tail=%s",
        node.get("title"),
        obj_token[-8:],
    )
    return obj_token


def _fetch_docx_raw(doc_token: str, tenant_token: str) -> Tuple[int, str]:
    try:
        r = requests.get(
            f"{_config.LARK_BASE}/docx/v1/documents/{doc_token}/raw_content",
            headers={"Authorization": f"Bearer {tenant_token}"},
            timeout=_config.REQ_TIMEOUT,
        )
    except requests.RequestException as e:
        log.warning("issue_watch_rag: SOP fetch failed: %s", e)
        return 0, ""
    if r.status_code != 200:
        return r.status_code, (r.text or "")[:200]
    try:
        return 200, str(((r.json() or {}).get("data") or {}).get("content") or "").strip()
    except Exception as e:  # noqa: BLE001
        log.warning("issue_watch_rag: SOP parse failed: %s", e)
        return 0, ""


def fetch_sop_text() -> str:
    """Raw text of the P0 SOP doc. Accepts a docx token OR a ``/wiki/`` node token."""
    token = _config.get_p0_rag_doc_token()
    if not token:
        return ""
    tok = _lark.get_tenant_token_primary()
    if not tok:
        return ""

    cached = _RESOLVED_DOC_TOKEN.get(token)
    if cached:
        st, body = _fetch_docx_raw(cached, tok)
        if st == 200:
            return body

    st, body = _fetch_docx_raw(token, tok)
    if st == 200:
        _RESOLVED_DOC_TOKEN[token] = token
        return body

    # Not readable as a docx — try it as a wiki node before giving up.
    obj_token = _resolve_wiki_node(token, tok)
    if obj_token:
        st2, body2 = _fetch_docx_raw(obj_token, tok)
        if st2 == 200:
            _RESOLVED_DOC_TOKEN[token] = obj_token
            return body2
        log.warning("issue_watch_rag: resolved docx unreadable HTTP=%s body=%s", st2, body2)
        return ""
    log.warning(
        "issue_watch_rag: SOP unreadable as docx or wiki node HTTP=%s token_tail=%s body=%s",
        st,
        token[-8:],
        body,
    )
    return ""


# ---------------------------------------------------------------- 2. chunk


def chunk_text(text: str, *, max_chars: int = 0, overlap: int = 120) -> List[str]:
    """Split on blank lines, then pack paragraphs up to ``max_chars`` with a small overlap.

    Overlap keeps a rule that straddles a boundary retrievable from either side.
    """
    body = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    if not body:
        return []
    limit = max_chars or _config.get_p0_rag_chunk_chars()
    paras = [p.strip() for p in body.split("\n\n") if p.strip()]
    chunks: List[str] = []
    cur = ""
    for p in paras:
        if len(cur) + len(p) + 2 <= limit:
            cur = f"{cur}\n\n{p}" if cur else p
            continue
        if cur:
            chunks.append(cur)
            tail = cur[-overlap:] if overlap > 0 else ""
            cur = f"{tail}\n\n{p}" if tail else p
        else:
            # One oversized paragraph — hard-split it.
            for i in range(0, len(p), limit):
                chunks.append(p[i : i + limit])
            cur = ""
    if cur:
        chunks.append(cur)
    return [c.strip() for c in chunks if c.strip()]


# ---------------------------------------------------------------- 3. embed + cache


def _index_path() -> str:
    base = _ds.shared_state_dir()
    return os.path.join(base, "issue_watch_rag_index.json") if base else ""


def _doc_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _load_index_from_disk() -> Dict[str, Any]:
    path = _index_path()
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            row = json.load(f)
        return row if isinstance(row, dict) else {}
    except Exception as e:  # noqa: BLE001
        log.warning("issue_watch_rag: index load failed: %s", e)
        return {}


def _save_index_to_disk(index: Dict[str, Any]) -> None:
    path = _index_path()
    if not path:
        return
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(index, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001
        log.warning("issue_watch_rag: index save failed: %s", e)


def build_index(*, force: bool = False) -> Dict[str, Any]:
    """Return a usable index, embedding the SOP only when the doc changed (or ``force``)."""
    global _INDEX, _LAST_BUILD_ATTEMPT
    with _LOCK:
        model = _config.get_p0_rag_embed_model()
        if not force and _INDEX.get("vectors"):
            return _INDEX
        if not force and (time.time() - _LAST_BUILD_ATTEMPT) < _BUILD_RETRY_SEC and not _INDEX:
            return {}
        _LAST_BUILD_ATTEMPT = time.time()

        sop = fetch_sop_text()
        if not sop:
            return _INDEX or {}
        h = _doc_hash(sop)

        if not force:
            disk = _INDEX or _load_index_from_disk()
            reusable = (
                disk.get("doc_hash") == h
                and disk.get("model") == model
                and int(disk.get("dims") or 0) == _config.get_p0_rag_embed_dims()
                and (
                    disk.get("mode") == "full_doc"
                    or (disk.get("vectors") and len(disk["vectors"]) == len(disk.get("chunks") or []))
                )
            )
            if reusable:
                _INDEX = disk
                log.info(
                    "issue_watch_rag: index loaded chunks=%s doc_hash=%s (no re-embed)",
                    len(disk.get("chunks") or []),
                    h,
                )
                return _INDEX

        dims = _config.get_p0_rag_embed_dims()
        # Small SOP: it fits in the prompt, so retrieval would only risk dropping the rule that
        # mattered. Store the text and skip embeddings entirely.
        if len(sop) <= _config.get_p0_rag_full_doc_max_chars():
            _INDEX = {"doc_hash": h, "model": model, "dims": dims, "mode": "full_doc",
                      "text": sop, "chunks": [], "vectors": [], "built_at": int(time.time())}
            _save_index_to_disk(_INDEX)
            log.info(
                "issue_watch_rag: SOP is %s chars — injecting it WHOLE, no embeddings "
                "(raise P0_RAG_FULL_DOC_MAX_CHARS to force retrieval)",
                len(sop),
            )
            return _INDEX

        chunks = chunk_text(sop)
        if not chunks:
            return _INDEX or {}
        vectors = _gemini.gemini_embed_texts(
            chunks, model=model, task_type="RETRIEVAL_DOCUMENT", dims=dims
        )
        if not vectors:
            log.warning("issue_watch_rag: embedding failed — RAG stays off for this round")
            return _INDEX or {}
        _INDEX = {"doc_hash": h, "model": model, "dims": dims, "mode": "retrieval",
                  "text": "", "chunks": chunks, "vectors": vectors, "built_at": int(time.time())}
        _save_index_to_disk(_INDEX)
        log.info(
            "issue_watch_rag: index BUILT chunks=%s dims=%s doc_hash=%s model=%s",
            len(chunks),
            len(vectors[0]) if vectors else 0,
            h,
            model,
        )
        return _INDEX


# ---------------------------------------------------------------- 4. retrieve


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def retrieve(query: str, top_k: int = 0) -> List[Tuple[float, str]]:
    """Top-K SOP passages for ``query`` as ``(score, chunk)``, best first."""
    q = (query or "").strip()
    if not q:
        return []
    index = build_index()
    chunks = index.get("chunks") or []
    vectors = index.get("vectors") or []
    if not chunks or not vectors:
        return []
    qv = _gemini.gemini_embed_texts(
        [q],
        model=index.get("model") or "",
        task_type="RETRIEVAL_QUERY",
        dims=int(index.get("dims") or 0),
    )
    if not qv:
        return []
    k = top_k or _config.get_p0_rag_top_k()
    scored = [(_cosine(qv[0], v), chunks[i]) for i, v in enumerate(vectors) if i < len(chunks)]
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[: max(1, k)]


# ---------------------------------------------------------------- 5. inject


def sop_context_for_message(message_text: str) -> str:
    """Prompt block of retrieved SOP passages, or ``""`` when RAG is off/unavailable."""
    if not _config.get_p0_issue_watch_rag_enabled():
        return ""
    try:
        index = build_index()
        if index.get("mode") == "full_doc":
            body = str(index.get("text") or "").strip()
            if not body:
                return ""
            log.info("issue_watch_rag: injected the WHOLE SOP (%s chars, no retrieval)", len(body))
        else:
            hits = retrieve(message_text)
            if not hits:
                return ""
            floor = _config.get_p0_rag_min_score()
            kept = [(sc, c) for sc, c in hits if sc >= floor]
            if not kept:
                log.info(
                    "issue_watch_rag: no SOP passage above min score %.2f (best %.2f)",
                    floor,
                    hits[0][0],
                )
                return ""
            body = "\n\n---\n".join(c for _sc, c in kept)
            log.info(
                "issue_watch_rag: injected %s SOP passage(s) top_score=%.3f chars=%s",
                len(kept),
                kept[0][0],
                len(body),
            )
    except Exception as e:  # noqa: BLE001 — detection must never fail because RAG failed
        log.warning("issue_watch_rag: context build failed: %s", e)
        return ""
    return (
        "\n\nOUR P0 SOP (authoritative — prefer it over your own judgement when they disagree):\n"
        f"{body}\n\n"
        "Use the SOP to decide whether this message is a MAJOR P0 for THIS company. "
        "If the SOP does not cover the message, fall back to the rules above.\n"
    )
