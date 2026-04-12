#!/usr/bin/env python3
"""
Run slack_huddle_invite_all.py with the same env prep as the old shell wrappers:
  - chdir repo root
  - load .env (ENV_PATH or repo/.env)
  - DISPLAY default :1 if unset (Linux/VNC)
  - unset SLACK_HEADLESS / HEADLESS (headed huddle)
  - optional: tee stdout/stderr to SLACK_PLAYWRIGHT_LOG

Usage (repo root or anywhere):
  .venv/bin/python scripts/run_slack_huddle.py
  .venv/bin/python scripts/run_slack_huddle.py --no-tee

Env: SESSION_DIR, SLACK_CHANNEL_URL (required). See env.example / scripts/env.huddle.aws.example.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_HUDDLE = REPO_ROOT / "scripts" / "slack_huddle_invite_all.py"


def _default_display() -> None:
    if sys.platform == "linux" and not (os.environ.get("DISPLAY") or "").strip():
        os.environ["DISPLAY"] = ":1"


def _clear_headless_flags() -> None:
    for k in ("SLACK_HEADLESS", "HEADLESS"):
        os.environ.pop(k, None)


def _pick_python() -> str:
    cand = (os.environ.get("SLACK_SUBPROCESS_PYTHON") or "").strip()
    if cand and Path(cand).is_file():
        return cand
    venv_py = REPO_ROOT / ".venv" / "bin" / "python"
    if venv_py.is_file():
        return str(venv_py)
    return sys.executable


def _preflight() -> None:
    errs: list[str] = []
    if not (os.environ.get("SESSION_DIR") or "").strip():
        errs.append("SESSION_DIR is not set (add to .env or export).")
    if not (os.environ.get("SLACK_CHANNEL_URL") or "").strip():
        errs.append(
            "SLACK_CHANNEL_URL is not set (full https://app.slack.com/client/T.../C... URL)."
        )
    if not SCRIPT_HUDDLE.is_file():
        errs.append(f"Missing {SCRIPT_HUDDLE} — wrong repo root?")
    if errs:
        for e in errs:
            print(f"ERROR: {e}", file=sys.stderr)
        env_hint = (os.environ.get("ENV_PATH") or "").strip() or str(REPO_ROOT / ".env")
        print(f"Fix .env ({env_hint}) or environment, then re-run.", file=sys.stderr)
        raise SystemExit(2)


def _log_path(args: argparse.Namespace) -> Path | None:
    if args.no_tee:
        return None
    raw = (os.environ.get("SLACK_PLAYWRIGHT_LOG") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return REPO_ROOT / "logs" / "slack_huddle_run.log"


def main() -> None:
    os.chdir(REPO_ROOT)
    parser = argparse.ArgumentParser(description="Run Slack Huddle Playwright flow.")
    parser.add_argument(
        "--no-tee",
        action="store_true",
        help="Do not append stdout/stderr to a log file (only terminal).",
    )
    args = parser.parse_args()

    from slack_env_utils import load_slack_dotenv

    load_slack_dotenv(REPO_ROOT)
    _default_display()
    _clear_headless_flags()
    _preflight()

    py = _pick_python()
    cmd = [py, str(SCRIPT_HUDDLE)]
    env = os.environ.copy()
    log_file = _log_path(args)

    if log_file is None:
        raise SystemExit(subprocess.call(cmd, cwd=str(REPO_ROOT), env=env))

    log_file.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    header = (
        f"===== {ts} run_slack_huddle DISPLAY={env.get('DISPLAY')} "
        f"SESSION_DIR={env.get('SESSION_DIR')} =====\n"
    )
    with log_file.open("a", encoding="utf-8") as lf:
        lf.write(header)
        lf.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            lf.write(line)
            lf.flush()
            sys.stdout.write(line)
            sys.stdout.flush()
        rc = proc.wait()
        raise SystemExit(rc)


if __name__ == "__main__":
    main()
