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

  # If the bot uses a non-default env file (same as systemd ``ENV_PATH``):
  export ENV_PATH=/root/lark-ops-ai/.env
  python3 scripts/grafana_screenshot_run_once.py

Requires ``P0_GRAPH_SCREENSHOT_URL`` in the env file (``ENV_PATH`` or repo ``.env``).
``P0_GRAPH_SCREENSHOT_ENABLED`` does **not** need to be on for this script.
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


def _bootstrap_env(env_path_override: str = "") -> str:
    """Load env the same way as ``main._load_dotenv_early`` + ``config.reload_env_runtime``."""
    if env_path_override.strip():
        os.environ["ENV_PATH"] = env_path_override.strip()
    from p0_logic import config as cfg

    path = cfg.resolve_env_file_path()
    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None  # type: ignore[misc, assignment]
    if load_dotenv and os.path.isfile(path):
        try:
            load_dotenv(path, encoding="utf-8", override=True)
        except TypeError:
            load_dotenv(path, override=True)
    cfg.reload_env_runtime()
    return path


def _diagnose_missing_url(env_path: str) -> None:
    exists = os.path.isfile(env_path)
    print(f"ENV file: {env_path} (exists={exists})", file=sys.stderr)
    try:
        from dotenv import dotenv_values  # noqa: F401
    except ImportError:
        print(
            "python-dotenv is not installed — install: pip install python-dotenv",
            file=sys.stderr,
        )
        return
    if not exists:
        print(
            "Create the file or set ENV_PATH=/path/to/.env before running this script.",
            file=sys.stderr,
        )
        return
    try:
        from dotenv import dotenv_values

        vals = dotenv_values(env_path) or {}
        raw = vals.get("P0_GRAPH_SCREENSHOT_URL")
        if raw is None:
            print(
                "P0_GRAPH_SCREENSHOT_URL is missing in that file (add an uncommented line).",
                file=sys.stderr,
            )
        elif not str(raw).strip():
            print("P0_GRAPH_SCREENSHOT_URL is present but empty.", file=sys.stderr)
        else:
            print(
                "P0_GRAPH_SCREENSHOT_URL is set in the file but did not reach the process "
                "(check for typos or run: export ENV_PATH=...)",
                file=sys.stderr,
            )
    except Exception as e:
        print(f"Could not read env file: {e}", file=sys.stderr)


def main() -> int:
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
    ap.add_argument(
        "--env-path",
        default="",
        help="Override ENV_PATH for this run (default: ENV_PATH env var or repo .env).",
    )
    args = ap.parse_args()

    env_path = _bootstrap_env(args.env_path or "")
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
        _diagnose_missing_url(env_path)
        return 1

    print(f"env: {env_path}")
    print(f"url: {url[:72]}{'...' if len(url) > 72 else ''}")
    print(
        f"capture: viewport={cfg.get_p0_graph_screenshot_viewport_width()}x"
        f"{cfg.get_p0_graph_screenshot_viewport_height()} "
        f"zoom={cfg.get_p0_graph_screenshot_zoom_percent()}% "
        f"top+bottom={cfg.get_p0_graph_screenshot_top_and_bottom()} "
        f"login_3rd={cfg.get_p0_graph_screenshot_include_login_panel()}"
    )

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
