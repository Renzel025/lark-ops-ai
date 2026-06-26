#!/usr/bin/env python3
"""
Open Grafana in **headed** Chromium with the **same** viewport / kiosk / zoom as P0 screenshots.
Use on VNC to check sizing — no capture, no Lark post. Press Enter in the terminal to close.

  cd /root/lark-ops-ai
  export ENV_PATH=/root/lark-ops-ai/.env
  /usr/local/bin/python3.8 features/screenshot/scripts/grafana_screenshot_open_browser.py

Requires ``P0_GRAPH_SCREENSHOT_URL`` (and username/password or Playwright profile if login needed).
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parents[3])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _bootstrap_env(env_path_override: str = "") -> None:
    if env_path_override.strip():
        os.environ["ENV_PATH"] = env_path_override.strip()
    from p0_logic import config as cfg

    path = cfg.resolve_env_file_path()
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    if os.path.isfile(path):
        try:
            load_dotenv(path, encoding="utf-8", override=True)
        except TypeError:
            load_dotenv(path, override=True)
    cfg.reload_env_runtime()


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Open Grafana in headed Chromium (P0 sizing).")
    ap.add_argument("--env-path", default="", help="Override ENV_PATH for this run.")
    args = ap.parse_args()

    _bootstrap_env(args.env_path or "")
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    from features.screenshot.graph_screenshot import open_grafana_dashboard_for_inspection

    return open_grafana_dashboard_for_inspection()


if __name__ == "__main__":
    raise SystemExit(main())
