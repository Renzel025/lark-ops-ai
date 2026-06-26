#!/usr/bin/env python3
"""Diagnose Bitable ops/deploy fetch + optional post boss-style cards to a group."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[3])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from p0_logic import config

config.apply_env_layers()

from p0_logic import lark_client as lark
from features.overview import bitable_adjustments as adj


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat-id", default="", help="oc_... group to post cards (requires --post)")
    ap.add_argument("--post", action="store_true", help="Post ops + deploy cards to --chat-id")
    args = ap.parse_args()

    print("ENV_PATH:", config.ENV_PATH)
    print("enabled:", config.p0_adjustment_bitable_enabled())
    print("on_p0_declare:", config.p0_adjustment_bitable_on_p0_declare())
    app = config.get_p0_adjustment_bitable_app_token()
    dep_tbl = config.get_p0_adjustment_bitable_table_id()
    ops_tbl = config.get_p0_adjustment_bitable_ops_table_id()
    print("app_token_tail:", app[-8:] if len(app) > 8 else (app or "(empty)"))
    print("deploy_table:", dep_tbl or "(empty)")
    print("ops_table:", ops_tbl or "(empty)")

    token = lark.get_tenant_token_primary()
    if not token:
        print("ERROR: no tenant token — check LARK_APP_ID / LARK_APP_SECRET", file=sys.stderr)
        return 2

    cutoff, end, window = adj._window_bounds_ms()  # noqa: SLF001 — ops diagnostic
    print("window:", window)
    print("cutoff_ms:", cutoff, "end_ms:", end)
    print("---")

    if dep_tbl:
        raw, err = lark.list_bitable_records(token, app, dep_tbl)
        print(f"deploy RAW records: {len(raw)}" + (f" | ERR: {err}" if err else ""))
        dep_rows, err2, win2, dep_total = adj.fetch_deploy_card_rows(token)
        print(f"deploy IN-WINDOW rows: {len(dep_rows)}/{dep_total}" + (f" | ERR: {err2}" if err2 else ""))
        if raw and not dep_rows and not err2:
            sample = raw[0].get("fields") if isinstance(raw[0], dict) else {}
            if isinstance(sample, dict):
                print("deploy sample field names:", sorted(str(k) for k in sample.keys())[:15])
    else:
        print("deploy: no P0_ADJUSTMENT_BITABLE_TABLE_ID")

    print("---")

    if ops_tbl:
        raw, err = lark.list_bitable_records(token, app, ops_tbl)
        print(f"ops RAW records: {len(raw)}" + (f" | ERR: {err}" if err else ""))
        ops_rows, err2, win2, ops_total = adj.fetch_ops_card_rows(token)
        print(f"ops IN-WINDOW rows: {len(ops_rows)}/{ops_total}" + (f" | ERR: {err2}" if err2 else ""))
        if raw and not ops_rows and not err2:
            sample = raw[0].get("fields") if isinstance(raw[0], dict) else {}
            if isinstance(sample, dict):
                print("ops sample field names:", sorted(str(k) for k in sample.keys())[:15])
    else:
        print("ops: no P0_ADJUSTMENT_BITABLE_OPS_TABLE_ID")

    if not args.post:
        print("---")
        print("Dry-run only. Add --post --chat-id=oc_... to send cards.")
        return 0

    oc = (args.chat_id or "").strip()
    if not oc.startswith("oc_"):
        print("ERROR: --post requires --chat-id=oc_...", file=sys.stderr)
        return 3

    posted, lines, diag = adj._post_boss_style_notices(  # noqa: SLF001
        token, group_chat_id=oc, trigger="manual_test"
    )
    print("---")
    print("posted:", posted)
    print("diag:", diag)
    for line in lines:
        print(line)
    return 0 if posted else 4


if __name__ == "__main__":
    raise SystemExit(main())
