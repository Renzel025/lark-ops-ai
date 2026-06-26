#!/usr/bin/env bash
# Run on ose-bot after a P0 declare + Issue Watch / screenshot miss.
# Usage: sudo bash features/session/scripts/diagnose_p0_incident_logs.sh [since] [until]
# Example: sudo bash features/session/scripts/diagnose_p0_incident_logs.sh '2026-06-08 17:35' '2026-06-08 17:50'

set -euo pipefail

SINCE="${1:-1 hour ago}"
UNTIL="${2:-}"

UNIT="${LARK_OPS_AI_UNIT:-lark-ops-ai}"
ENV_FILE="${LARK_OPS_AI_ENV:-/root/lark-ops-ai/.env}"

echo "=== journalctl $UNIT since=$SINCE until=${UNTIL:-now} ==="
JARGS=(--unit="$UNIT" --since="$SINCE" --no-pager)
if [[ -n "$UNTIL" ]]; then
  JARGS+=(--until="$UNTIL")
fi

journalctl "${JARGS[@]}" | grep -iE \
  'start_p0|adjustment_bitable|bitable|P0 declare trigger|graph screenshot|Capturing Grafana|playwright|Issue Watch|issue_watch|issue_watch_declare|scheduling auto capture|skipped —|overview will DM|no Issue Watch alert|vc_ring|vc\.meeting\.join|VC invite|vc oauth|vc_user_oauth|Bound live meeting_id' \
  || echo "(no matching log lines — widen time window or check service name)"

echo
echo "=== env flags (no secrets) ==="
if [[ -f "$ENV_FILE" ]]; then
  grep -E '^(P0_ADJUSTMENT_BITABLE_|P0_GRAPH_SCREENSHOT_|P0_ISSUE_WATCH_|P0_VC_RING_|P0_VC_OAUTH_|P0_SHARED_STATE_DIR|P0_DM_INSTRUCTION)' "$ENV_FILE" \
    | grep -viE 'PASSWORD|SECRET|APP_TOKEN' \
    || echo "(no matching env keys)"
else
  echo "missing $ENV_FILE"
fi

echo
echo "=== playwright profile dir ==="
if [[ -f "$ENV_FILE" ]]; then
  PROFILE=$(grep -E '^P0_GRAPH_SCREENSHOT_PLAYWRIGHT_USER_DATA_DIR=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' || true)
  if [[ -n "${PROFILE:-}" ]]; then
    ls -ld "$PROFILE" 2>/dev/null || echo "profile dir missing: $PROFILE"
  else
    echo "P0_GRAPH_SCREENSHOT_PLAYWRIGHT_USER_DATA_DIR not set"
  fi
fi
