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
    except ImportError as e:
        raise RuntimeError(
            "python-dotenv is required: pip install python-dotenv"
        ) from e

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
