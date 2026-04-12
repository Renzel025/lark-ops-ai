"""Shared .env loading for Slack Playwright scripts (ENV_PATH or repo .env)."""
from __future__ import annotations

import os
from pathlib import Path


def load_slack_dotenv(repo_root: Path) -> Path | None:
    """
    Load dotenv into os.environ. Prefer ENV_PATH if set and file exists;
    else repo_root/.env. Returns path loaded or None if no file found.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        import sys

        print(
            "WARN: python-dotenv not installed; repo .env not loaded. "
            "Install: pip install python-dotenv  OR  use .venv: .venv/bin/python ...",
            file=sys.stderr,
        )
        return None

    env_path = (os.environ.get("ENV_PATH") or "").strip()
    candidates: list[Path] = []
    if env_path:
        candidates.append(Path(env_path).expanduser())
    candidates.append(repo_root / ".env")

    for p in candidates:
        if p.is_file():
            load_dotenv(p, override=False)
            return p.resolve()
    return None
