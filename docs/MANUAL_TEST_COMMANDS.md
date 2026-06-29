# Manual test commands

Run from repo root:

```bash
cd /root/lark-ops-ai-dev    # dev
# cd /root/lark-ops-ai      # prod
export ENV_PATH=/root/lark-ops-ai-dev/.env   # optional, if not default
```

Related: [ENV_FEATURES_TOGGLES.md](./ENV_FEATURES_TOGGLES.md) — which features are ON/OFF on prod vs dev.

---

## Has a test script

| Feature | Command |
|---------|---------|
| **Bitable** 📦/🔴 | `python3 features/overview/scripts/test_bitable_once.py` |
| | `python3 features/overview/scripts/test_bitable_once.py --post --chat-id=oc_REAL_ID` |
| | Or `--post` only if `P0_ADJUSTMENT_BITABLE_POST_CHAT_ID=oc_...` is set |
| **Issue Watch** (AI only) | `python3 features/issue_watch/scripts/test_once.py "CP site loading"` |
| **Grafana screenshot** | `python3 features/screenshot/scripts/grafana_screenshot_run_once.py` |
| | `python3 features/screenshot/scripts/grafana_screenshot_run_once.py --post-lark` |
| | `python3 features/screenshot/scripts/grafana_screenshot_run_once.py --range 1h --post-lark` |
| **Grafana login** (once) | `python3 features/screenshot/scripts/grafana_playwright_login_once.py` |
| **Grafana open browser** | `python3 features/screenshot/scripts/grafana_screenshot_open_browser.py` |
| **Recording card** | `python3 features/recording/scripts/post_card_once.py --chat-id=oc_...` |
| **P0 logs diagnose** | `bash features/session/scripts/diagnose_p0_incident_logs.sh` |
| **Monitoring GC** | `python3 features/monitoring/scripts/test_monitoring_once.py` |
| | `python3 features/monitoring/scripts/test_monitoring_once.py --kind log` |

Requires `P0_MONITORING_CHAT_IDS=oc_...` in `.env`.

---

## No Python script — test in Lark only

| Feature | How |
|---------|-----|
| **P0/P1 + VC** | Type `P0` / `P1` in incident group (bot must be running) |
| **DM overview** | DM bot → Send overview |
| **Thread confirm** | “Is this P0?” → reply **yes** |
| **Wiki AI** | Message in wiki group |
| **VC ring** | Real P0 VC + `P0_VC_RING_ENABLED=on` |
| **Overview forwarder** | Send overview with `LARK_OVERVIEW_FORWARDER_ENABLED=on` |

---

## Monitoring GC — automatic (no manual test needed)

| Trigger | What monitoring GC receives |
|---------|------------------------------|
| **Duty warning mirror** | Same text as duty DM (e.g. overview missing fields on Send) |
| **Log ERROR alerts** | ERROR+ log lines (bitable fail, post fail, etc.) |

Env: `P0_MONITORING_CHAT_IDS`, `P0_MONITORING_DUTY_WARNINGS=on`, `P0_MONITORING_LOG_ALERTS=on`

---

## Run the bot

```bash
bash scripts/run_dev.sh                 # local dev
sudo systemctl restart lark-ops-ai      # server
sudo journalctl -u lark-ops-ai -f       # watch logs
```

---

## Prod (old repo) — same scripts, different path

| Feature | Prod path |
|---------|-----------|
| Issue Watch | `python3 scripts/issue_watch/test_once.py "message"` |
| Grafana screenshot | `python3 scripts/grafana/screenshot_run_once.py --post-lark` |
| Grafana login | `python3 scripts/grafana/playwright_login_once.py` |
| Recording card | `python3 scripts/recording/post_card_once.py --chat-id=oc_...` |
| P0 logs | `bash scripts/diagnose_p0_incident_logs.sh` |
| Bitable 📦/🔴 | not available until dev is merged to prod |
| Monitoring GC | `features/monitoring/scripts/test_monitoring_once.py` (dev only until merge) |

---

## Notes

- **`oc_REAL_ID`** — use a real group ID from `INCIDENT_GROUP_IDS`, not a placeholder.
- **Bitable `--post`** — sends 📦 deploy (page 1 + thread) then 🔴 ops (page 1 + thread).
- **Issue Watch script** — AI classify only; does not send DM. Full flow needs `P0_ISSUE_WATCH_ENABLED=on` + message in incident group.
- **Grafana `--post-lark`** — posts to `P0_GRAPH_SCREENSHOT_TARGET_CHAT_ID`.
