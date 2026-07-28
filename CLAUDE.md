# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Lark/Feishu bot for P0/P1 incident operations. A single FastAPI process receives Lark webhook events and drives: VC meeting creation, a DM-based incident-overview builder, Grafana screenshot capture, VC cloud-recording fan-out, optional in-group "Issue Watch" P0 detection, and Bitable deploy/ops summary cards. It is a bot backend, not a web UI — almost all interaction happens inside Lark chats and interactive cards.

## Commands

```bash
# Install (app env)
pip install -r p0_logic/requirements.txt
pip install fastapi uvicorn lark-oapi pycryptodome requests python-dotenv

# Run locally (smoke test only — real webhooks need a deployed VPS behind nginx)
cp env.dev.example .env.dev            # dev routing overlay
cp env.example .env                    # secrets + base config
bash scripts/init_dev_env.sh           # first-time dev env scaffolding
ENV_PROFILE=dev bash scripts/run_dev.sh   # uvicorn main:app --reload on :8000

# Lint / typecheck (CI tooling — see requirements-dev.txt)
ruff check .
pyright                                # config in pyrightconfig.json (checks p0_logic/, py3.8 target)
```

There is **no unit test suite**. "Tests" here are manual one-shot scripts under `features/*/scripts/` run against real Lark/Grafana. See `docs/MANUAL_TEST_COMMANDS.md`. Examples:

```bash
python3 features/overview/scripts/test_bitable_once.py --post --chat-id=oc_YOUR_GROUP
python3 features/recording/scripts/post_card_once.py --chat-id=oc_...
python3 features/screenshot/scripts/grafana_screenshot_run_once.py --post-lark
python3 features/screenshot/scripts/grafana_playwright_login_once.py   # seed Grafana session once
python3 features/issue_watch/scripts/test_once.py "website loading"
bash features/session/scripts/diagnose_p0_incident_logs.sh
```

Deploy is `git pull` + `systemctl restart lark-ops-ai` on the VPS (see `docs/DEPLOY.md`). CI/CD Jenkinsfiles live in a **separate** `lark-ops-ai-jenkins` repo — not here.

## Architecture

Request path:

```
Lark → HTTPS /lark/webhook → nginx → uvicorn (main.py) → lark_logic.process_message / p0_logic.handlers → features/*
```

**`main.py`** — the FastAPI app and the only HTTP surface. It decrypts AES event bodies (`LARK_ENCRYPT_KEY`, plus `LARK_ENCRYPT_KEY_2` when two Lark apps share one webhook URL), answers the `url_verification` challenge, classifies the callback type (`im.message.receive_v1`, `card.action.trigger`, `vc.meeting.*_v1`), and extracts message parts (text, image keys, @mentions, VC participant refs). It returns `200` fast and does heavy work in a `BackgroundTasks` job (`_process_lark_payload`) — **except** `card.action.trigger` for `show_participants`, which is handled synchronously so the toast/card can be returned in the same HTTP response. VC join/leave events are translated into participant add/remove + ring + issue-watch hooks here.

**`lark_logic.py`** — `process_message()` is the message-routing brain for chat text. It decides whether a message is a P0/P1 declaration, a typed session command (end/cancel meeting), an `@bot` ring command, a Grafana screenshot request, a threaded "Is this P0?" yes/no confirmation, a wiki-doc Q&A ask (delegated to `wiki_ai_logic.handle_wiki_ai`), or nothing. Much of this file is heuristics + optional Groq AI triage to avoid false P0 triggers from casual chat — lots of small `_is_*` / `_matches_*` predicates guard each route. Routing depends on whether `chat_id` is a configured incident group, a mirror/session-command chat, or has a pending thread confirmation.

**`wiki_ai_logic.py`** — root helper that answers questions against a Lark Docx document's raw content (`docx/v1/documents/{token}/raw_content`, doc token via `WIKI_DOC_TOKEN`) using Groq. Deliberately hits the Docx API directly to sidestep Wiki-space permission errors.

**`p0_logic/`** — shared core, imported as a flat public API via `p0_logic/__init__.py` (which re-exports from both `p0_logic.*` and `features.*`). Callers do `from p0_logic import start_p0, handle_lark_card_action, get_tenant_token, ...`. Key modules:
- `config.py` — **all** env reading, feature flags, and routing decisions live here (`apply_env_layers()` merges `.env` + `.env.dev` under `ENV_PROFILE=dev`; `get_incident_group_chat_ids()`, single-group mode, meeting-card destination routing). When adding a feature toggle, add its getter here.
- `lark_client.py` — tenant-token cache (per app_id, ~2h expiry), message/card post + patch, VC API calls. Supports a second "severity" app (`LARK_SEVERITY_APP_ID` / `_2` aliases) for DMs.
- `cards.py` — all interactive-card JSON builders.
- `handlers.py` — orchestrates DM overview generation (`handle_dm_generate_overview`) and card-button actions (`handle_lark_card_action`, `handle_p0_submit`). This is where a card click turns into session/state changes and follow-up posts.
- `groq_client.py` / `anthropic_client.py` / `gemini_client.py` — pluggable LLM providers (triage, overview drafting). Provider is env-selected per feature.

**`features/`** — one folder per product area, each holding its logic + `scripts/` manual runners. `p0_logic/__init__.py` imports from these, so feature code is the source of truth (no duplicated logic in `p0_logic/`). Areas: `session/` (P0/P1 lifecycle, VC meeting, participants), `overview/` (draft→preview→send overview, forwarder, Bitable deploy/ops cards), `recording/` (VC recording-ready fan-out + duty-ring paging, see below), `issue_watch/` (in-group major-P0 detection), `screenshot/` (Playwright Grafana capture), `monitoring/` (GC alerts to a monitoring group).

**`features/recording/` duty-ring subsystem** (gated by `P0_VC_RING_ENABLED`) — pages on-call people into the active P0 VC when `@bot` is given a ring command in an incident group. `duty_roster.py` reads today's on-call person(s) per team/shift from Lark Sheets; `duty_directory.py` resolves a name to a directory `open_id` (must be minted by the **primary** app — see cross-app note below). `vc_ring.py` parses direct (`/c /m /e`), SRE-duty (`/scpms /sfpms /sfe /spms`), team-roster (`/fe /fpms /pms`), and shift-section (`/dba /sosm`) commands — including multiple bare commands in one message — resolves targets, and invites them (`invite_open_ids_into_active_meeting`); it also threads ring status as a reply under the triggering command message and handles the "already in the P0 meeting" join prompt. `sre_game.py` runs the `/srebac` escalation flow: posts an ordered on-call chain, advances on `/n` (next) or `/r @person` (retry a specific check person), and times out to the next contact if nobody replies. The `duty-roster-expert` agent and `duty-roster` skill own this subsystem end-to-end.

**`lark-forwarder/`** — a **separate** tiny FastAPI service (`forwarder.py`) that broadcasts overview text to other groups via a dedicated "Overview" Lark app or a custom-bot webhook. The main app calls it over HTTP when `LARK_OVERVIEW_FORWARDER_ENABLED=on`.

## Conventions that matter

- **Everything is env-driven and toggleable.** Behavior differs between prod (`/root/lark-ops-ai`), dev (`/root/lark-ops-ai-dev`), and a staging tier purely via env — same code, different `.env`. Before changing behavior, check `docs/ENV_FEATURES_TOGGLES.md` for the flag and its prod/dev defaults; read the flag through a `config.py` getter, never `os.getenv` directly in feature code.
- **Three deploy tiers.** Prod and staging each run their own systemd unit + `.env` on a VPS (`scripts/setup_staging_server.sh` bootstraps staging → `lark-ops-ai-staging.service`, which can track `main` or a branch ahead of prod); dev is the local `ENV_PROFILE=dev` overlay. `env.{dev,staging,prod}.example` are the per-tier templates.
- **Idempotency.** Lark delivers events more than once — routes dedupe by `message_id`/`event_id` (see the `_dedupe` helpers). Preserve this when adding handlers.
- **Return 200 fast.** Do slow work (LLM calls, multiple Lark round-trips) in the background task, not inline in the webhook, unless the flow genuinely needs a synchronous card/toast response.
- **`p0_logic` targets Python 3.8** (pyright config) with a `backports.zoneinfo` fallback — avoid 3.9+-only syntax in that package.
- The `lark-expert` agent carries deep Lark API knowledge and this repo's conventions — use it for anything touching Lark APIs, cards, tokens, or webhook callbacks. `playwright-expert` covers the Grafana screenshot pipeline; `log-monitoring-expert` covers logging/alerting; `duty-roster-expert` covers the `features/recording/` duty-ring subsystem (rosters, directory resolution, ring commands, SRE Game escalation).
- Secrets live only in `.env*` on the server (git-ignored). `env.example` is the full annotated reference.
