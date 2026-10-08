"""
Read-only MCP server for inspecting the lark-ops-ai bot on its dev and prod boxes.

Every tool here SSHes into the box (using the "bot-dev" / "bot-prod" host aliases already
configured in ~/.ssh/config) and runs a single read-only command: journalctl, cat, grep, ls,
systemctl is-active, git log. None of them can modify anything on the box, restart the service,
or send anything to Lark — that is a deliberate, hard limit, not an oversight.

Setup (once, from the repo root — needs Python 3.10+ and the bot-dev / bot-prod SSH aliases):
    python3.12 -m venv tools/lark-bot-inspector/.venv
    tools/lark-bot-inspector/.venv/bin/pip install -r tools/lark-bot-inspector/requirements.txt
Registered with Claude Code by the repo-root ``.mcp.json`` (project scope) — approve it on the
next Claude Code start. Smoke test:
    tools/lark-bot-inspector/.venv/bin/python tools/lark-bot-inspector/server.py
"""
from __future__ import annotations

import re
import shlex
import subprocess
from typing import Literal

from mcp.server.mcpserver import MCPServer

mcp = MCPServer(
    "lark-bot-inspector",
    instructions=(
        "Read-only inspection of the lark-ops-ai bot's dev and prod boxes: service logs, "
        "active P0/P1 session state, DM draft/preview state, Issue Watch mute state, feature "
        "flag values (secrets are never shown), and which git commit is deployed. "
        "No tool here can change anything on the box."
    ),
)

_HOSTS = {"dev": "bot-dev", "prod": "bot-prod"}
_REPO_DIRS = {"dev": "/root/lark-ops-ai-dev", "prod": "/root/lark-ops-ai"}
_ENV_FILES = {"dev": "/root/lark-ops-ai-dev/.env", "prod": "/root/lark-ops-ai/.env"}
_UNIT = "lark-ops-ai"  # same unit name on both boxes — dev's unit is NOT lark-ops-ai-dev

# Anything whose name contains one of these is refused, whole value, no partial reveal either.
_SECRET_NAME_MARKERS = ("SECRET", "TOKEN", "KEY", "PASSWORD", "PASS", "AUTH", "CREDENTIAL")

_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
_SAFE_FLAG_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,100}$")
_MAX_OUTPUT_CHARS = 20_000


def _host(box: str) -> str:
    h = _HOSTS.get((box or "").strip().lower())
    if not h:
        raise ValueError(f"box must be 'dev' or 'prod', got {box!r}")
    return h


def _ssh(box: str, remote_command: str, timeout: int = 30) -> str:
    """Run one read-only command on `box` over SSH, as root via passwordless sudo (the state dir
    and .env are root-owned; the SSH login user is not). Never raises on a failing remote command —
    returns its stderr/stdout so the caller (an LLM) can see and reason about the failure."""
    host = _host(box)
    # `sudo -n` (never prompt) + `bash -c <the whole script as one quoted arg>` — every tool above
    # builds remote_command as a small shell script (pipes, $(...), ||), so it must reach bash
    # intact as a single argument, not be re-split by this wrapper.
    sudo_command = "sudo -n bash -c " + shlex.quote(remote_command)
    try:
        proc = subprocess.run(
            [
                "ssh",
                "-o", "RemoteCommand=none",
                "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=15",
                "-T", host,
                sudo_command,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"[ssh timed out after {timeout}s connecting to {box}]"
    out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    out = out.strip() or "(no output)"
    if len(out) > _MAX_OUTPUT_CHARS:
        out = out[-_MAX_OUTPUT_CHARS:]
        out = f"[...truncated to the last {_MAX_OUTPUT_CHARS} chars...]\n" + out
    return out


def _require_safe_token(value: str, what: str) -> str:
    v = (value or "").strip()
    if not _SAFE_TOKEN_RE.match(v):
        raise ValueError(
            f"{what}={value!r} is not a safe token (letters/digits/._- only, max 200 chars)"
        )
    return v


@mcp.tool()
def list_boxes() -> str:
    """List the boxes this server can inspect, and what 'box' value to pass to the other tools."""
    return (
        "dev  -> repo /root/lark-ops-ai-dev on host 'bot-dev' (systemd unit: lark-ops-ai)\n"
        "prod -> repo /root/lark-ops-ai     on host 'bot-prod' (systemd unit: lark-ops-ai)\n"
        "Pass box='dev' or box='prod' to every other tool here."
    )


@mcp.tool()
def service_status(box: Literal["dev", "prod"]) -> str:
    """systemctl status + which git commit is currently deployed + the box's current time."""
    repo = _REPO_DIRS[box]
    cmd = (
        f"echo '--- systemctl ---'; systemctl is-active {shlex.quote(_UNIT)}; "
        f"echo '--- deployed commit ---'; cd {shlex.quote(repo)} && git log --oneline -1; "
        f"echo '--- box time ---'; date"
    )
    return _ssh(box, cmd)


@mcp.tool()
def check_logs(
    box: Literal["dev", "prod"],
    since: str,
    until: str = "",
    grep_pattern: str = "",
    max_lines: int = 200,
) -> str:
    """
    Read the bot's journalctl logs on `box`.

    since / until: journalctl time strings, e.g. "2026-08-20 22:45:00", "1 hour ago", "today".
    grep_pattern: optional extended-regex (grep -E) to filter lines, e.g. "overview built:|send_preview".
    max_lines: cap on how many matching lines to return (most recent ones kept), default 200, max 2000.
    """
    max_lines = max(1, min(int(max_lines), 2000))
    since_q = shlex.quote(since)
    parts = ["journalctl", "-u", shlex.quote(_UNIT), "--since", since_q, "--no-pager"]
    if until.strip():
        parts += ["--until", shlex.quote(until)]
    cmd = " ".join(parts)
    if grep_pattern.strip():
        cmd += f" | grep -E {shlex.quote(grep_pattern)}"
    cmd += f" | tail -n {max_lines}"
    return _ssh(box, cmd, timeout=45)


@mcp.tool()
def check_env_flag(box: Literal["dev", "prod"], flag_name: str) -> str:
    """
    Read one feature-flag/config line from the box's .env — e.g. "P0_ISSUE_WATCH_ENABLED",
    "P0_COMMAND_ONLY_DECLARE". Refuses anything whose name looks like a secret (contains
    SECRET/TOKEN/KEY/PASSWORD/AUTH/CREDENTIAL) — those are never returned, not even redacted.
    """
    name = (flag_name or "").strip().upper()
    if not _SAFE_FLAG_NAME_RE.match(name):
        return f"refused: {flag_name!r} is not a plausible ENV_VAR_NAME"
    if any(marker in name for marker in _SECRET_NAME_MARKERS):
        return f"refused: {flag_name!r} looks like a secret — this tool never reads those"
    env_file = _ENV_FILES[box]
    cmd = f"grep -nE '^{name}=' {shlex.quote(env_file)} || echo '(unset — not in .env)'"
    return _ssh(box, cmd)


@mcp.tool()
def list_active_sessions(box: Literal["dev", "prod"]) -> str:
    """List active P0/P1 session state files on `box` (one per incident chat currently in-session)."""
    cmd = (
        "D=$(grep -oE '^P0_SHARED_STATE_DIR=.*' "
        f"{shlex.quote(_ENV_FILES[box])} | cut -d= -f2-); "
        'if [ -z "$D" ]; then echo "(P0_SHARED_STATE_DIR not set)"; '
        'else ls -la "$D/sessions/" 2>&1 || echo "(no sessions dir)"; fi'
    )
    return _ssh(box, cmd)


@mcp.tool()
def get_session_detail(box: Literal["dev", "prod"], chat_id_or_tail: str) -> str:
    """
    Show the full session JSON for one incident chat on `box` — priority, target_chat,
    vc_ring_target_open_ids, start time, etc. Pass the full oc_... chat_id, or just enough of the
    tail to uniquely match a filename under sessions/ (matched with `find -name '*<tail>*'`).
    """
    tail = _require_safe_token(chat_id_or_tail, "chat_id_or_tail")
    cmd = (
        "D=$(grep -oE '^P0_SHARED_STATE_DIR=.*' "
        f"{shlex.quote(_ENV_FILES[box])} | cut -d= -f2-); "
        f'F=$(find "$D/sessions/" -maxdepth 1 -iname "*{tail}*.json" 2>/dev/null | head -1); '
        'if [ -z "$F" ]; then echo "(no session file matching that chat_id/tail)"; '
        'else cat "$F"; fi'
    )
    return _ssh(box, cmd)


@mcp.tool()
def get_draft_or_preview(
    box: Literal["dev", "prod"],
    open_id_or_tail: str,
    kind: Literal["draft", "preview"] = "preview",
) -> str:
    """
    Show the DM draft or preview JSON for one duty open_id on `box` — issue/impact/support text,
    priority, target_chat, message ids for the posted card. Pass the full ou_... open_id, or just
    enough of the tail to uniquely match a filename.
    """
    tail = _require_safe_token(open_id_or_tail, "open_id_or_tail")
    subdir = "drafts" if kind == "draft" else "previews"
    cmd = (
        "D=$(grep -oE '^P0_SHARED_STATE_DIR=.*' "
        f"{shlex.quote(_ENV_FILES[box])} | cut -d= -f2-); "
        f'F=$(find "$D/{subdir}/" -maxdepth 1 -iname "*{tail}*.json" 2>/dev/null | head -1); '
        f'if [ -z "$F" ]; then echo "(no {kind} file matching that open_id/tail)"; '
        'else cat "$F"; fi'
    )
    return _ssh(box, cmd)


@mcp.tool()
def check_issue_watch_mute(box: Literal["dev", "prod"]) -> str:
    """Show the Issue Watch /off /on mute-state file on `box` (global switch, all detection groups)."""
    cmd = (
        "D=$(grep -oE '^P0_SHARED_STATE_DIR=.*' "
        f"{shlex.quote(_ENV_FILES[box])} | cut -d= -f2-); "
        'F="$D/issue_watch_mute.json"; '
        'if [ -f "$F" ]; then cat "$F"; else echo "(no mute-state file — never muted, or feature off)"; fi'
    )
    return _ssh(box, cmd)


if __name__ == "__main__":
    mcp.run()
