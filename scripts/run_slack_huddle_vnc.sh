#!/usr/bin/env bash
# Run slack_huddle_invite_all.py the way that usually fixes white huddle windows:
# headed (not SLACK_HEADLESS), DISPLAY set, .env loaded.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export DISPLAY="${DISPLAY:-:1}"
unset SLACK_HEADLESS HEADLESS 2>/dev/null || true

# Load .env via python-dotenv (same as p0_logic) — NOT `source`, because bash splits on
# spaces (e.g. MEETING_TOPIC=CP-Emergency feedback... breaks without quotes in .env).
if [[ -f "$ROOT/.env" ]]; then
  _load_py="${ROOT}/.venv/bin/python"
  [[ -x "${_load_py}" ]] || _load_py="$(command -v python3)"
  eval "$(ROOT="${ROOT}" "${_load_py}" -c "
import os, shlex, sys
from pathlib import Path
try:
    from dotenv import dotenv_values
except ImportError:
    print('ERROR: pip install python-dotenv (needed to load .env safely)', file=sys.stderr)
    sys.exit(1)
p = Path(os.environ['ROOT']) / '.env'
for k, v in dotenv_values(p).items():
    if v is None or k is None:
        continue
    k = str(k).strip()
    if not k:
        continue
    print(f'export {k}={shlex.quote(str(v))}')
")"
fi

PY="${SLACK_SUBPROCESS_PYTHON:-$ROOT/.venv/bin/python}"
if [[ ! -x "$PY" ]] && command -v python3 >/dev/null; then
  PY="$(command -v python3)"
fi

exec "$PY" "$ROOT/scripts/slack_huddle_invite_all.py"
