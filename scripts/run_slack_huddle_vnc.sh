#!/usr/bin/env bash
# Run slack_huddle_invite_all.py the way that usually fixes white huddle windows:
# headed (not SLACK_HEADLESS), DISPLAY set, .env loaded.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export DISPLAY="${DISPLAY:-:1}"
unset SLACK_HEADLESS HEADLESS 2>/dev/null || true

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ROOT/.env"
  set +a
fi

PY="${SLACK_SUBPROCESS_PYTHON:-$ROOT/.venv/bin/python}"
if [[ ! -x "$PY" ]] && command -v python3 >/dev/null; then
  PY="$(command -v python3)"
fi

exec "$PY" "$ROOT/scripts/slack_huddle_invite_all.py"
