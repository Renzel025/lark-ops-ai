#!/usr/bin/env python3
"""
Run the **same** Playwright capture as the P0 bot. By default saves PNG(s) to disk **only**
(no Lark) — use that to verify URL, profile, clip, and timings.

  # Local files only (default dir: logs/grafana-screenshot-manual/)
  python3 scripts/grafana_screenshot_run_once.py

  # Custom output directory
  python3 scripts/grafana_screenshot_run_once.py /tmp/my-shots

  # Also post caption + images to the Lark group in ``P0_GRAPH_SCREENSHOT_TARGET_CHAT_ID``
  # (needs ``LARK_APP_ID`` / ``LARK_APP_SECRET`` or whatever ``get_tenant_token_primary`` uses in .env)
  python3 scripts/grafana_screenshot_run_once.py --post-lark

  # Optional: watch Chromium on VNC
  export P0_GRAPH_SCREENSHOT_HEADED=1

Requires ``P0_GRAPH_SCREENSHOT_URL`` in ``.env``. ``P0_GRAPH_SCREENSHOT_ENABLED`` does **not**
need to be on for this script.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        env_path = os.path.join(_REPO_ROOT, ".env")
        if os.path.isfile(env_path):
            load_dotenv(env_path)
    except Exception:
        pass


def main() -> int:
    _load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser(description="Grafana screenshot — same Playwright path as the P0 bot.")
    ap.add_argument(
        "out_dir",
        nargs="?",
        default="",
        help="Directory to write PNG(s). Default: logs/grafana-screenshot-manual/",
    )
    ap.add_argument(
        "--post-lark",
        action="store_true",
        help="After capture, post caption + image(s) to P0_GRAPH_SCREENSHOT_TARGET_CHAT_ID (tenant token from .env).",
    )
    args = ap.parse_args()

    out_dir = (args.out_dir or "").strip()
    if not out_dir:
        out_dir = os.path.join(_REPO_ROOT, "logs", "grafana-screenshot-manual")
    os.makedirs(out_dir, exist_ok=True)

    from p0_logic import config as cfg

    url = cfg.get_p0_graph_screenshot_url()
    if not url:
        print(
            "Set P0_GRAPH_SCREENSHOT_URL in .env (and profile if login is required).",
            file=sys.stderr,
        )
        return 1

    from p0_logic.graph_screenshot import _capture_png_payloads, post_p0_graph_screenshots_to_chat

    pngs, captured_at = _capture_png_payloads()
    if not pngs:
        print("Capture returned no PNG — check logs above (Playwright installed? URL loads?).", file=sys.stderr)
        return 2

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    paths = []
    for i, blob in enumerate(pngs):
        name = f"grafana_{stamp}.png" if len(pngs) == 1 else f"grafana_{stamp}_part{i + 1}.png"
        path = os.path.join(out_dir, name)
        with open(path, "wb") as f:
            f.write(blob)
        paths.append(path)

    print(f"captured_at: {captured_at}")
    for p in paths:
        print(p)

    if args.post_lark:
        from p0_logic import lark_client as lark

        chat_id = cfg.get_p0_graph_screenshot_target_chat_id()
        if not chat_id:
            print(
                "ERROR: --post-lark requires P0_GRAPH_SCREENSHOT_TARGET_CHAT_ID=oc_... in .env",
                file=sys.stderr,
            )
            return 3
        tok = lark.get_tenant_token_primary()
        if not tok:
            print(
                "ERROR: could not get tenant token (set LARK_APP_ID + LARK_APP_SECRET or your bot's token vars).",
                file=sys.stderr,
            )
            return 4
        post_p0_graph_screenshots_to_chat(
            tok,
            chat_id,
            pngs,
            captured_at,
            source_label="manual grafana_screenshot_run_once",
        )
        print(f"post-lark: sent to chat_id tail={chat_id[-12:]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
