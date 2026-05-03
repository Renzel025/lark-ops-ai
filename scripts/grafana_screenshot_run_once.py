#!/usr/bin/env python3
"""
Run the **same** Playwright capture as the P0 bot, but save PNG(s) to disk (no Lark upload).

Use this to verify URL, profile, clip, and timings without triggering a real P0.

From repo root, same venv as the bot:

  # Optional: watch the browser (needs DISPLAY, e.g. VNC)
  export P0_GRAPH_SCREENSHOT_HEADED=1

  python3 scripts/grafana_screenshot_run_once.py
  python3 scripts/grafana_screenshot_run_once.py /tmp/my-shots

Requires ``P0_GRAPH_SCREENSHOT_URL`` in ``.env`` (and profile path if Grafana is not public).
``P0_GRAPH_SCREENSHOT_ENABLED`` does **not** need to be on for this script.
"""
from __future__ import annotations

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
    out_dir = (sys.argv[1] if len(sys.argv) >= 2 else "").strip()
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

    from p0_logic.graph_screenshot import _capture_png_payloads

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
