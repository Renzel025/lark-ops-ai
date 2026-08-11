#!/usr/bin/env python3
"""
Inspect the Major-P0 RAG pipeline one stage at a time — run this on the box, not locally.

    python3 features/issue_watch/scripts/rag_once.py --doc          # what the SOP fetch returns
    python3 features/issue_watch/scripts/rag_once.py --build        # chunk + embed + cache
    python3 features/issue_watch/scripts/rag_once.py --ask "cannot receive OTP upon testing"
    python3 features/issue_watch/scripts/rag_once.py --classify "5 players cannot deposit"

``--ask`` shows the retrieved passages and their cosine scores (retrieval only, no LLM).
``--classify`` runs the full two-stage path so you can see whether the SOP vetoed the signal.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from p0_logic import config as _config  # noqa: E402

_config.apply_env_layers()

from features.issue_watch import issue_watch_rag as rag  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", action="store_true", help="fetch the SOP doc and show a preview")
    ap.add_argument("--build", action="store_true", help="build/refresh the embedding index")
    ap.add_argument("--force", action="store_true", help="re-embed even if the doc is unchanged")
    ap.add_argument("--ask", default="", help="retrieve SOP passages for this message")
    ap.add_argument("--classify", default="", help="full classify incl. the SOP second stage")
    args = ap.parse_args()

    print(f"RAG enabled  : {_config.get_p0_issue_watch_rag_enabled()}")
    print(f"doc token    : {(_config.get_p0_rag_doc_token() or '(unset)')[-10:]}")
    print(f"embed model  : {_config.get_p0_rag_embed_model()}")
    print(f"top_k / floor: {_config.get_p0_rag_top_k()} / {_config.get_p0_rag_min_score()}\n")

    if args.doc:
        text = rag.fetch_sop_text()
        print(f"SOP chars={len(text)}")
        print("-" * 60)
        print(text[:1500] or "(empty — check P0_RAG_DOC_TOKEN and bot doc permission)")
        print("-" * 60)
        print(f"would split into {len(rag.chunk_text(text))} chunk(s)")

    if args.build or args.force:
        idx = rag.build_index(force=args.force)
        print(f"index chunks={len(idx.get('chunks') or [])} model={idx.get('model')} hash={idx.get('doc_hash')}")

    if args.ask:
        for score, chunk in rag.retrieve(args.ask):
            head = " ".join(chunk.split())[:180]
            print(f"  {score:.3f}  {head}…")

    if args.classify:
        from features.issue_watch.issue_watch_ai import classify_issue_watch_message

        out = classify_issue_watch_message(args.classify)
        print(f"\nsignal     : {out.get('is_incident_signal') if out else None}")
        print(f"confidence : {out.get('confidence') if out else None}")
        print(f"provider   : {out.get('provider') if out else None}")
        print(f"reason     : {out.get('reason') if out else None}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
