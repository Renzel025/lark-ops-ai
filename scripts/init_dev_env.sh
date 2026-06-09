#!/usr/bin/env bash
# One-time: create .env.dev overlay from template (secrets stay in .env).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  echo "Missing .env — copy secrets first:  cp env.example .env  (fill LARK_APP_ID, etc.)"
  exit 1
fi

if [[ -f .env.dev ]]; then
  echo ".env.dev already exists — edit it or remove and re-run."
  exit 0
fi

cp env.dev.example .env.dev
echo "Created .env.dev from env.dev.example"
echo "Edit .env.dev: set INCIDENT_GROUP_IDS, INCIDENT_OVERVIEW_TARGET_MAP, P0_DM_INSTRUCTION_OPEN_IDS"
echo "Then run:  bash scripts/run_dev.sh"
