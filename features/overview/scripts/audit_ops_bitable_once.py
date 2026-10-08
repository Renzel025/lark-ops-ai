#!/usr/bin/env python3
"""Audit the 🔴 线上操作汇总 card: list every 线上操作 row near the window and say why it is SHOWN or
DROPPED (rejected / empty 执行操作 / time out of window / time missing or unparseable). Read-only —
posts nothing. Run with the SERVICE's python from the repo root:

    $(systemctl show lark-ops-ai -p ExecStart --value | grep -oE '/[^ ;]*python[0-9.]*' | head -1) \
        features/overview/scripts/audit_ops_bitable_once.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Repo root when run from features/overview/scripts/; else the CWD (e.g. saved to /tmp, run from the repo).
_HERE = Path(__file__).resolve()
ROOT = str(_HERE.parents[3]) if len(_HERE.parents) > 3 and (_HERE.parents[3] / "p0_logic").is_dir() else os.getcwd()
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from p0_logic import config

config.apply_env_layers()

from p0_logic import lark_client as lark
from features.overview import bitable_adjustments as adj

_NEAR_MS = 3 * 24 * 3600 * 1000  # also list rows up to 3 days either side, to spot near-misses


def _raw(fields, names):
    for n in names:
        if n in fields:
            return fields[n]
    return None


def main() -> int:
    tok = lark.get_tenant_token_primary()
    app = config.get_p0_adjustment_bitable_app_token()
    tbl = config.get_p0_adjustment_bitable_ops_table_id()
    cfg = config.get_p0_adjustment_bitable_ops_field_names()
    cut, end, win = adj._window_bounds_ms()  # noqa: SLF001
    recs, err = lark.list_bitable_records(
        tok, app, tbl, page_size=500, max_pages=config.get_p0_adjustment_bitable_max_pages()
    )
    print("window:", win)
    print("records fetched:", len(recs), ("| ERR: " + err) if err else "")
    print("time columns:", cfg["op_start_time"], "/", cfg["op_done_time"])
    print("-" * 100)

    shown, dropped, no_time = [], [], []
    for r in recs:
        f = r.get("fields") if isinstance(r, dict) else None
        if not isinstance(f, dict):
            continue
        rid = str(r.get("record_id") or "")[-6:]
        st = adj._pick_time_ms(f, cfg["op_start_time"]) or 0  # noqa: SLF001
        dn = adj._pick_time_ms(f, cfg["op_done_time"]) or 0  # noqa: SLF001
        action = adj._pick_field(f, cfg["operation"])  # noqa: SLF001
        status = adj._pick_field(f, cfg.get("status", ("执行状况阶段",)))  # noqa: SLF001
        st_raw = _raw(f, cfg["op_start_time"])
        dn_raw = _raw(f, cfg["op_done_time"])
        label = "%s | %s | start=%s done=%s | status=%s" % (
            rid,
            (action or "(empty)")[:40],
            adj._fmt_ts_full(st) if st else repr(st_raw)[:40],  # noqa: SLF001
            adj._fmt_ts_full(dn) if dn else repr(dn_raw)[:40],  # noqa: SLF001
            status or "-",
        )
        if not st and not dn:
            # No usable time at all: either blank, or a value the parser can't read (silently dropped).
            if st_raw not in (None, "", []) or dn_raw not in (None, "", []):
                no_time.append("UNPARSEABLE TIME  " + label)
            continue
        in_win = [t for t in (st, dn) if t and cut <= t <= end]
        near = any(t and cut - _NEAR_MS <= t <= end + _NEAR_MS for t in (st, dn))
        if not in_win:
            if near:
                dropped.append("OUT OF WINDOW     " + label)
            continue
        if adj._is_rejected_status(status):  # noqa: SLF001
            dropped.append("REJECTED          " + label)
        elif not adj._field_meaningful(action):  # noqa: SLF001
            dropped.append("EMPTY 执行操作     " + label)
        else:
            shown.append((max(in_win), "SHOWN             " + label))

    shown.sort(reverse=True)
    print("SHOWN on card (%d):" % len(shown))
    for _, line in shown:
        print("  " + line)
    print("\nDROPPED by a filter (%d):" % len(dropped))
    for line in dropped:
        print("  " + line)
    print("\nTIME MISSING/UNPARSEABLE — never considered (%d):" % len(no_time))
    for line in no_time:
        print("  " + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
