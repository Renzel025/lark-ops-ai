# Features (architecture)

Each folder = **one product area**. Logic + manual scripts live together.

| Folder | What it does | Run scripts from repo root |
|--------|----------------|---------------------------|
| **`features/screenshot/`** | P0 Grafana capture + post to Lark | `python3 features/screenshot/scripts/grafana_screenshot_run_once.py --post-lark` |
| **`features/overview/`** | Draft → preview → send overview, forwarder, Bitable | (via DM / card actions in Lark) |
| **`features/recording/`** | VC cloud recording ready fan-out | `python3 features/recording/scripts/post_card_once.py` |
| **`features/issue_watch/`** | Major P0 detection in groups | `python3 features/issue_watch/scripts/test_once.py "message"` |
| **`features/session/`** | P0/P1 session, meeting, participants | `bash features/session/scripts/diagnose_p0_incident_logs.sh` |

Full command list: **[docs/MANUAL_TEST_COMMANDS.md](../docs/MANUAL_TEST_COMMANDS.md)**  
Env ON/OFF guide: **[docs/ENV_FEATURES_TOGGLES.md](../docs/ENV_FEATURES_TOGGLES.md)**

## Layout per feature

```
features/screenshot/
  graph_screenshot.py          # logic
  graph_screenshot_ai.py
  graph_screenshot_request.py
  scripts/
    grafana_screenshot_run_once.py
    grafana_playwright_login_once.py
    grafana_screenshot_open_browser.py
```

## `p0_logic/` (shared core)

Still holds **shared** pieces used by every feature:

- `config`, `lark_client`, `cards`, `handlers`, `groq_client`, …

`p0_logic/` holds shared core only. Feature code lives under `features/` (imported directly — no duplicate files in `p0_logic/`).

## Infra (not a product feature)

`scripts/run_dev.sh`, `scripts/nginx/`, `scripts/systemd/` — deploy/dev only.
