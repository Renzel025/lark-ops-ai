#!/usr/bin/env python3
"""
Debian/Ubuntu: install Chrome + Python venv + pip deps for Slack Huddle scripts.
Run with sudo on a fresh AWS box:

  sudo .venv/bin/python scripts/install_slack_huddle_deps.py

Or (creates venv with system python3):

  sudo python3 scripts/install_slack_huddle_deps.py

Optional: PLAYWRIGHT_INSTALL_CHROMIUM=1 to run ``playwright install chromium``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(cmd: list[str], **kw: object) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def _apt_install(packages: list[str]) -> bool:
    try:
        _run(
            ["apt-get", "install", "-y", "-qq", *packages],
            env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
        )
        return True
    except subprocess.CalledProcessError:
        return False


def main() -> None:
    os.chdir(REPO_ROOT)
    if os.geteuid() != 0:
        print("WARN: not root — apt may fail. Use: sudo python3 scripts/install_slack_huddle_deps.py", file=sys.stderr)

    _run(["apt-get", "update", "-qq"], env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"})

    pkgs_primary = [
        "ca-certificates",
        "curl",
        "gnupg",
        "python3",
        "python3-pip",
        "python3-venv",
        "fonts-liberation",
        "libasound2t64",
        "libatk-bridge2.0-0",
        "libatk1.0-0",
        "libatspi2.0-0",
        "libcups2",
        "libdbus-1-3",
        "libdrm2",
        "libgbm1",
        "libgtk-3-0",
        "libnspr4",
        "libnss3",
        "libpango-1.0-0",
        "libx11-6",
        "libxcb1",
        "libxcomposite1",
        "libxdamage1",
        "libxext6",
        "libxfixes3",
        "libxkbcommon0",
        "libxrandr2",
        "xdg-utils",
    ]
    pkgs_fallback = [
        "ca-certificates",
        "curl",
        "gnupg",
        "python3",
        "python3-pip",
        "python3-venv",
        "fonts-liberation",
        "libasound2",
        "libatk-bridge2.0-0",
        "libatk1.0-0",
        "libatspi2.0-0",
        "libcups2",
        "libdbus-1-3",
        "libdrm2",
        "libgbm1",
        "libgtk-3-0",
        "libnspr4",
        "libnss3",
        "libpango-1.0-0",
        "libx11-6",
        "libxcb1",
        "libxcomposite1",
        "libxdamage1",
        "libxext6",
        "libxfixes3",
        "libxkbcommon0",
        "libxrandr2",
        "xdg-utils",
    ]
    if not _apt_install(pkgs_primary):
        print("NOTE: primary package set failed; trying fallback (libasound2 without t64).", flush=True)
        if not _apt_install(pkgs_fallback):
            sys.exit(1)

    if not shutil.which("google-chrome-stable"):
        keyring = Path("/usr/share/keyrings/google-chrome.gpg")
        keyring.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            ["curl", "-fsSL", "https://dl.google.com/linux/linux_signing_key.pub"],
            check=False,
            capture_output=True,
        )
        if r.returncode == 0:
            subprocess.run(
                ["gpg", "--dearmor", "-o", str(keyring)],
                input=r.stdout,
                check=True,
            )
            Path("/etc/apt/sources.list.d").mkdir(parents=True, exist_ok=True)
            Path("/etc/apt/sources.list.d/google-chrome.list").write_text(
                "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] "
                "http://dl.google.com/linux/chrome/deb/ stable main\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["apt-get", "update", "-qq"],
                check=False,
                env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
            )
            subprocess.run(
                ["apt-get", "install", "-y", "-qq", "google-chrome-stable"],
                check=False,
                env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
            )
        if not shutil.which("google-chrome-stable"):
            print(
                "NOTE: google-chrome-stable not installed (wrong arch or network). "
                "Install Chrome manually and set CHROME_PATH.",
                file=sys.stderr,
            )

    venv = REPO_ROOT / ".venv"
    if not venv.is_dir():
        _run([sys.executable, "-m", "venv", str(venv)])

    pip = venv / "bin" / "pip"
    req = REPO_ROOT / "scripts" / "requirements-huddle.txt"
    _run([str(pip), "install", "-q", "--upgrade", "pip"])
    _run([str(pip), "install", "-q", "-r", str(req)])
    _run([str(pip), "install", "-q", "python-dotenv"])

    if (os.environ.get("PLAYWRIGHT_INSTALL_CHROMIUM") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        pw = venv / "bin" / "playwright"
        if pw.is_file():
            _run([str(pw), "install", "chromium"])

    (REPO_ROOT / "logs").mkdir(parents=True, exist_ok=True)
    print("Done. Example:", flush=True)
    print("  export DISPLAY=:1", flush=True)
    print(f"  {venv / 'bin' / 'python'} scripts/slack_open_login_browser.py", flush=True)
    print(f"  {venv / 'bin' / 'python'} scripts/run_slack_huddle.py", flush=True)


if __name__ == "__main__":
    main()
