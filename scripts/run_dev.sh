#!/usr/bin/env bash
# Local dev server: .env (secrets) + .env.dev (routing overrides). No prod .env edits.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  echo "Missing .env — run:  cp env.example .env  and fill Lark credentials."
  exit 1
fi
if [[ ! -f .env.dev ]]; then
  echo "Missing .env.dev — run:  bash scripts/init_dev_env.sh"
  exit 1
fi

export ENV_PROFILE=dev
export PYTHONUNBUFFERED=1

HOST="${DEV_HOST:-127.0.0.1}"
PORT="${DEV_PORT:-8000}"

echo "Starting dev (ENV_PROFILE=dev): .env + .env.dev on http://${HOST}:${PORT}"
echo "For real Lark webhooks, deploy on dev/prod VPS (nginx → this port). Local run is smoke-test only."

exec python3 -m uvicorn main:app --host "$HOST" --port "$PORT" --reload --log-level info
