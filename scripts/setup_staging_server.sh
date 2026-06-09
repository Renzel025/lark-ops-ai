#!/usr/bin/env bash
# Bootstrap lark-ops-ai on a dedicated STAGING/DEV server (not production).
# Run on the new server as root (or adjust paths).
#
# Prereqs: git, python3.8+, nginx optional for TLS webhook.
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/root/lark-ops-ai}"
REPO_URL="${REPO_URL:-}"  # optional: git clone URL if dir missing
BRANCH="${BRANCH:-main}"  # or develop — staging can track a branch ahead of prod

echo "=== lark-ops-ai staging bootstrap ==="
echo "Install dir: $INSTALL_DIR"

if [[ ! -d "$INSTALL_DIR/.git" ]]; then
  if [[ -z "$REPO_URL" ]]; then
    echo "Clone the repo first, e.g.:"
    echo "  git clone <your-repo-url> $INSTALL_DIR"
    exit 1
  fi
  git clone -b "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

if [[ ! -f .env ]]; then
  if [[ -f env.staging.example ]]; then
    cp env.staging.example .env
    echo "Created .env from env.staging.example — EDIT before starting:"
    echo "  nano $INSTALL_DIR/.env"
  else
    cp env.example .env
    echo "Created .env from env.example — use DEV group IDs only."
  fi
else
  echo ".env already exists — skipped"
fi

mkdir -p /var/lib/lark-ops-ai-staging
chmod 700 /var/lib/lark-ops-ai-staging 2>/dev/null || true

if [[ ! -d .venv ]]; then
  python3.8 -m venv .venv || python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -U pip
pip install -q -r requirements.txt 2>/dev/null || pip install -q fastapi uvicorn requests python-dotenv lark-oapi pycryptodome

SERVICE_SRC="scripts/systemd/lark-ops-ai-staging.service.example"
SERVICE_DST="/etc/systemd/system/lark-ops-ai-staging.service"
if [[ -f "$SERVICE_SRC" ]]; then
  sed "s|/root/lark-ops-ai|$INSTALL_DIR|g" "$SERVICE_SRC" | sudo tee "$SERVICE_DST" >/dev/null
  echo "Installed systemd unit: $SERVICE_DST"
  echo "  sudo systemctl daemon-reload"
  echo "  sudo systemctl enable lark-ops-ai-staging"
  echo "  sudo systemctl start lark-ops-ai-staging"
  echo "  journalctl -u lark-ops-ai-staging -f"
else
  echo "Manual start: cd $INSTALL_DIR && set -a && source .env && set +a && uvicorn main:app --host 127.0.0.1 --port 8000"
fi

cat <<'EOF'

=== Next steps (staging server) ===
1. Edit .env — dev INCIDENT_GROUP_IDS only, never prod oc_ groups.
2. Lark Developer Console → your DEV app (recommended):
   - Event subscription webhook URL → https://<staging-host>/webhook (via nginx)
   - Subscribe to im.message.receive_v1 in DEV groups only.
3. Production server keeps its own .env and webhook URL — do not share.
4. Deploy code to staging:  git pull && sudo systemctl restart lark-ops-ai-staging
5. Promote to prod when ready: merge branch → git pull on prod → restart lark-ops-ai only.

Branch workflow (suggested):
  staging server:  git checkout develop  (or feature branches)
  production:      git checkout main

EOF
