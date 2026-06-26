# p0_logic

Shared **core** for the bot (config, Lark client, cards, handlers, AI clients).

Feature-specific logic lives under **`features/`** — see [`features/README.md`](../features/README.md).

| Area | Location |
|------|----------|
| Grafana screenshot | `features/screenshot/` |
| Overview draft/send | `features/overview/` |
| VC recording fan-out | `features/recording/` |
| Issue Watch | `features/issue_watch/` |
| P0/P1 session | `features/session/` |

Thin **shim files** in this folder (e.g. `graph_screenshot.py`) re-export from `features.*` so existing `from p0_logic import …` keeps working.

## Core modules here

| Module | Role |
|--------|------|
| `config` | Env reload, routing, feature flags |
| `lark_client` | Tenant token, post message/card, VC API |
| `cards` | Lark card builders |
| `handlers` | DM + card action orchestration |
| `groq_client` / `anthropic_client` / `gemini_client` | LLM providers |

## Usage from your app

```python
# Install deps (in your app env)
# pip install -r p0_logic/requirements.txt

from p0_logic import (
    get_tenant_token,
    start_p0,
    end_p0_session,
    end_p0_session_by_meeting_no,
    cancel_p0_session_by_meeting_no,
    bind_live_meeting_id,
    handle_dm_generate_overview,
    handle_lark_card_action,
    handle_p0_submit,
)

# Example: get token and start P0
token = get_tenant_token(app_id, app_secret)
start_p0(chat_id=chat_id, token=token, trigger_open_id=open_id, priority="P0")

# Example: handle DM message (e.g. from event callback)
handle_dm_generate_overview(
    sender_open_id=open_id,
    tenant_token=token,
    text=message_text,
    image_key=image_key,  # optional
    mention_names=mentions,
    message_id=message_id,
)

# Example: handle card button click
handle_lark_card_action(payload=event_payload, tenant_token=token)
```

## Environment

Same as before: `ENV_PATH`, `INCIDENT_GROUP_ID`, `REQ_TIMEOUT`, `MEETING_TOPIC`, `P0_OWNER_OPEN_IDS`, `GROQ_API_KEY`, `GROQ_MODEL`, `GROQ_VISION_MODEL`, `SUPPORT_SHEET_*`, `AUTO_PREVIEW_DELAY_SEC`, `ONGOING_CARD_DELAY_SEC`, `P1_TO_P0_ESCALATION_SEC`, `P0_COOLDOWN_SEC`, `SUPPORT_MAP_TTL_SEC`.

## Requirements

- Python 3.9+
- `requests`
- `python-dotenv` (optional, for env reload)
