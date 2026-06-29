#!/usr/bin/env python3
"""
Run the **same** Playwright capture as the P0 bot. By default saves PNG(s) to disk **only**
(no Lark) — use that to verify URL, profile, clip, and timings.

  # Local files only (default dir: logs/grafana-screenshot-manual/)
  python3 features/screenshot/scripts/grafana_screenshot_run_once.py

  # Custom output directory
  python3 features/screenshot/scripts/grafana_screenshot_run_once.py /tmp/my-shots

  # Also post caption + images to the Lark group in ``P0_GRAPH_SCREENSHOT_TARGET_CHAT_ID``
  # (needs ``LARK_APP_ID`` / ``LARK_APP_SECRET`` or whatever ``get_tenant_token_primary`` uses in .env)
  python3 features/screenshot/scripts/grafana_screenshot_run_once.py --post-lark

  # Specific time window (rewrites ``from=`` on P0_GRAPH_SCREENSHOT_URL):
  python3 features/screenshot/scripts/grafana_screenshot_run_once.py --range 1h --post-lark
  python3 features/screenshot/scripts/grafana_screenshot_run_once.py --range 30m

  # All P0 auto ranges (default 6h only) — same as declaring P0:
  python3 features/screenshot/scripts/grafana_screenshot_run_once.py --all-auto-ranges --post-lark

  # Optional: watch Chromium on VNC
  export P0_GRAPH_SCREENSHOT_HEADED=1

  # If the bot uses a non-default env file (same as systemd ``ENV_PATH``):
  export ENV_PATH=/root/lark-ops-ai/.env
  python3 features/screenshot/scripts/grafana_screenshot_run_once.py

Requires ``P0_GRAPH_SCREENSHOT_URL`` in the env file (``ENV_PATH`` or repo ``.env``).
``P0_GRAPH_SCREENSHOT_ENABLED`` does **not** need to be on for this script.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from datetime import datetime

_REPO_ROOT = str(Path(__file__).resolve().parents[3])
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
    ap.add_argument(
        "--range",
        default="",
        help="Time window: 30m, 1h, 2h, 3h, or 6h (sets Grafana from= on P0_GRAPH_SCREENSHOT_URL).",
    )
    ap.add_argument(
        "--all-auto-ranges",
        action="store_true",
        help="Capture each range in P0_GRAPH_SCREENSHOT_AUTO_RANGES (default 6h only).",
    )
    args = ap.parse_args()

    env_path = _bootstrap_env(args.env_path or "")
    out_dir = (args.out_dir or "").strip()
    if not out_dir:
        out_dir = os.path.join(_REPO_ROOT, "logs", "grafana-screenshot-manual")
    os.makedirs(out_dir, exist_ok=True)

    from p0_logic import config as cfg

    base_url = cfg.get_p0_graph_screenshot_url()
    if not base_url:
        print(
            "Set P0_GRAPH_SCREENSHOT_URL in .env (and profile if login is required).",
            file=sys.stderr,
        )
        _diagnose_missing_url(env_path)
        return 1

    range_keys: list[str] = []
    if args.all_auto_ranges:
        range_keys = list(cfg.get_p0_graph_screenshot_auto_range_keys())
    elif (args.range or "").strip():
        range_keys = [(args.range or "").strip().lower()]
    else:
        range_keys = [""]

    print(f"env: {env_path}")
    if range_keys == [""]:
        print(f"url: {base_url[:72]}{'...' if len(base_url) > 72 else ''}")
    else:
        print(f"ranges: {range_keys}")
    print(
        f"capture: viewport={cfg.get_p0_graph_screenshot_viewport_width()}x"
        f"{cfg.get_p0_graph_screenshot_viewport_height()} "
        f"zoom={cfg.get_p0_graph_screenshot_zoom_percent()}% "
        f"top+bottom={cfg.get_p0_graph_screenshot_top_and_bottom()} "
        f"login_3rd={cfg.get_p0_graph_screenshot_include_login_panel()} "
        f"band_max_ms={cfg.get_p0_graph_screenshot_band_max_wait_ms()} "
        f"fast={cfg.get_p0_graph_screenshot_fast_capture()} "
        f"time_bar={cfg.get_p0_graph_screenshot_include_time_bar()}"
    )

    from features.screenshot.graph_screenshot import _capture_png_payloads, post_p0_graph_screenshots_to_chat

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
    else:
        tok = ""
        chat_id = ""

    any_ok = False
    for rk in range_keys:
        if rk:
            url = cfg.build_p0_graph_screenshot_url_for_range(rk)
            if not url:
                print(f"ERROR: unknown range {rk!r}", file=sys.stderr)
                return 1
            range_disp = cfg.get_p0_graph_screenshot_range_display(rk)
            print(f"capture range={rk} url: {url[:72]}{'...' if len(url) > 72 else ''}")
        else:
            url = base_url
            rk = ""
            range_disp = ""

        pngs, captured_at = _capture_png_payloads(url)
        if not pngs:
            print(
                f"Capture returned no PNG range={rk or 'default'} — check logs above.",
                file=sys.stderr,
            )
            continue
        any_ok = True

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = f"_{rk}" if rk else ""
        paths = []
        for i, blob in enumerate(pngs):
            if len(pngs) == 1:
                name = f"grafana_{stamp}{suffix}.png"
            else:
                name = f"grafana_{stamp}{suffix}_part{i + 1}.png"
            path = os.path.join(out_dir, name)
            with open(path, "wb") as f:
                f.write(blob)
            paths.append(path)

        print(f"captured_at: {captured_at}" + (f" range={rk}" if rk else ""))
        for p in paths:
            print(p)

        if args.post_lark and tok and chat_id:
            post_p0_graph_screenshots_to_chat(
                tok,
                chat_id,
                pngs,
                captured_at,
                source_label="manual grafana_screenshot_run_once",
                range_label=rk,
            )
            print(f"post-lark: sent range={rk or 'default'} to chat_id tail={chat_id[-12:]}")

    if not any_ok:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
