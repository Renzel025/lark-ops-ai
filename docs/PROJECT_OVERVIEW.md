# Lark Ops AI — Project Overview (Master Guide)

Complete guide for lark-ops-ai: how it was built, how the architecture is set up, how the code runs, and how to use each feature.

For deep dives, see also:

| Doc | Focus |
|-----|--------|
| [ARCHITECTURE_AND_FLOW.md](ARCHITECTURE_AND_FLOW.md) | Webhook routing, session lifecycle |
| [DEPLOYMENT_ARCHITECTURE.md](DEPLOYMENT_ARCHITECTURE.md) | VPS, nginx, TLS, systemd, Groq outbound |
| [HOW_IT_WORKS_AND_NAVIGATION.md](HOW_IT_WORKS_AND_NAVIGATION.md) | DM buttons, Edit/Save/Back paths |
| [P0_P1_OPERATOR_GUIDE.md](P0_P1_OPERATOR_GUIDE.md) | Operator SOP (P0/P1/end/cancel) |
| [IT_LARK_DEV_APP_CHECKLIST.md](IT_LARK_DEV_APP_CHECKLIST.md) | Lark app scopes & permissions |
| [CI_CD.md](CI_CD.md) | CI/CD notes |
| [../env.example](../env.example) | Full env template |
| [../p0_logic/README.md](../p0_logic/README.md) | `p0_logic` package API |

---

## 1. What is this project?

lark-ops-ai is a Lark/Feishu bot that automates P0/P1 incident response:

- Creates Lark VC meetings when someone declares an incident
- Guides duty/on-call in DM to build a bilingual incident overview (English + Chinese)
- Posts the overview to the configured incident group
- Optional: Issue Watch (auto-detect player issues in chat), Grafana screenshots, Bitable deployment cards, VC ring, ongoing DM buzz, and more

Runtime stack:

```
Lark cloud  →  HTTPS webhook  →  nginx  →  uvicorn (FastAPI main.py)
                                              ↓
                                    lark_logic + p0_logic
                                              ↓
                              Outbound: Lark Open API + Groq + Claude (Anthropic)
```

---

## 2. Setup steps (end-to-end)

Typical path from zero to a running bot:

### Phase A — Lark Developer Console

1. Create a custom app (bot) on the [Lark Open Platform](https://open.larksuite.com/).
2. Enable bot capability; add the app to incident groups.
3. Event subscription → Request URL: `https://<YOUR_DOMAIN>/lark/webhook`
4. Subscribe to events (minimum):
   - `im.message.receive_v1`
   - `card.action.trigger`
   - VC events: `vc.meeting.join_meeting_v1`, `vc.meeting.leave_meeting_v1`, `vc.meeting.meeting_ended_v1`, …
5. Copy `LARK_APP_ID`, `LARK_APP_SECRET`, `LARK_ENCRYPT_KEY` (if encryption is enabled).
6. Grant scopes (see `IT_LARK_DEV_APP_CHECKLIST.md`): IM send/update/recall, VC reserve, urgent, etc.

### Phase B — Server & code

```bash
git clone <repo> lark-ops-ai
cd lark-ops-ai
python3 -m venv .venv
source .venv/bin/activate
pip install -r p0_logic/requirements.txt
pip install fastapi uvicorn lark-oapi pycryptodome requests python-dotenv
cp env.example .env
# Edit .env: LARK_*, INCIDENT_GROUP_IDS, P0_DM_INSTRUCTION_OPEN_IDS, GROQ_API_KEY, …
```

Dev overlay (`.env.dev` — routing overrides for your dev VPS, same nginx flow as prod):

```bash
cp env.dev.example .env.dev
# Edit group IDs, duty open_ids, P0_VC_OAUTH_PUBLIC_BASE_URL=https://your-dev-domain, …
ENV_PROFILE=dev bash scripts/run_dev.sh   # optional: smoke-test on your Mac only
```

Webhook path (dev and prod — same pattern, no tunnel tools):

```
Lark  →  https://<your-domain>/lark/webhook  →  nginx :443  →  uvicorn :8000  →  main.py
```

- Dev: Lark Developer Console webhook URL points at your dev domain (e.g. dev VPS).
- Prod: Same, with your prod domain.
- `run_dev.sh` on a laptop only starts uvicorn locally — Lark will not reach it unless the webhook URL is changed to that machine (we do not use that in this project).

### Phase C — Server deploy (dev & prod)

1. DNS A record → VPS public IP
2. nginx reverse proxy + Let's Encrypt TLS on `:443`
3. systemd unit runs `uvicorn main:app --host 0.0.0.0 --port 8000`
4. Lark webhook URL = `https://<domain>/lark/webhook`
5. Verify: `journalctl -u lark-ops-ai -f`

Full detail: [DEPLOYMENT_ARCHITECTURE.md](DEPLOYMENT_ARCHITECTURE.md)

### Phase D — Configure features in `.env`

| Area | Key vars |
|------|----------|
| Incident groups | `INCIDENT_GROUP_IDS`, `P0_SINGLE_INCIDENT_GROUP`, `INCIDENT_GROUP_EMERGENCY_TOPICS` |
| Duty DM | `P0_DM_INSTRUCTION_OPEN_IDS` |
| AI | `GROQ_API_KEY` (overview + OCR), optional `ANTHROPIC_API_KEY` / Claude (Issue Watch) |
| Issue Watch | `P0_ISSUE_WATCH_ENABLED=1`, `P0_ISSUE_WATCH_DECLARE_P0_ENABLED=1` |
| Bitable after overview | `P0_ADJUSTMENT_BITABLE_ENABLED=1`, table tokens |
| VC ring | `P0_VC_RING_ENABLED=1`, `P0_MAJOR_CHECK_PERSON_IDS`, OAuth URLs |
| Ongoing buzz | `P0_ONGOING_DM_BUZZ_*` (5 min / 10 min DM reminders) |
| Broadcast overview | `INCIDENT_OVERVIEW_SEND_MAP`, `LARK_OVERVIEW_FORWARDER_*`, `lark-forwarder/` service |

---

## 3. Architecture — how everything connects (plain English)

### The big picture in one sentence

Lark sends events to your server → your Python code decides what to do → your server calls Lark (and other APIs) back to post messages, create meetings, and generate overview text.

---

### Step by step — what happens when someone uses the bot

1. Something happens in Lark

- Someone types `p0` in an incident group
- Duty taps a button in DM
- Someone joins a video meeting
- Lark’s cloud records that event

2. Lark calls your server (inbound)

```
Lark cloud
    │
    │  HTTPS POST  (webhook)
    ▼
Your domain :443  (nginx — handles SSL / HTTPS)
    │
    ▼
Port 8000  (uvicorn — runs main.py / FastAPI)
    │
    ▼
Python code  (lark_logic.py + p0_logic/)
```

Lark does not run your bot. It only notifies your server: “here’s a new message / button click / VC event.”

3. Your code runs

| Piece | What it does |
|-------|----------------|
| `main.py` | Receives the webhook, decrypts if needed, starts background work |
| `lark_logic.py` | Routes the message: incident group vs DM vs wiki |
| `p0_logic/` | P0 sessions, meetings, overview drafts, Issue Watch, cards, etc. |
| In-memory state | Active meetings (`P0_SESSIONS`), DM drafts, previews, timers — lives in RAM until restart |

4. Your server calls out (outbound)

Your code then calls other services to actually do things:

| Service | Why |
|---------|-----|
| Lark Open API | Post messages, create/end VC, send DM cards, ring users |
| Groq | Generate overview text, OCR screenshots |
| Anthropic (Claude) | Issue Watch classification (optional) |
| lark-forwarder | Post overview to broadcast room with second bot (optional) |

Those calls go from your VPS to the internet — they do not go through the same URL Lark uses to reach you.

5. Users see the result back in Lark

- Meeting link in the group
- Green / blue cards in DM
- Overview posted to the incident or broadcast group

---

### Simple diagram (no special tools needed)

```
┌─────────────────────────────────────────────────────────────┐
│  LARK (groups, DM, video meetings)                          │
└───────────────────────────┬─────────────────────────────────┘
                            │  events come IN (webhook)
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  YOUR SERVER                                                │
│  nginx → uvicorn → main.py → lark_logic → p0_logic          │
│  (stores active sessions + drafts in memory)                │
└───────────────────────────┬─────────────────────────────────┘
                            │  your code calls OUT
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  Lark API · Groq · Claude · lark-forwarder          │
└───────────────────────────┬─────────────────────────────────┘
                            │  replies / posts go back to Lark
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  LARK (users see messages, cards, meetings)                 │
└─────────────────────────────────────────────────────────────┘
```

---

### Inbound vs outbound (important)

| Direction | Meaning | Example |
|-----------|---------|---------|
| Inbound | Lark → your server | Webhook POST to `https://your-domain/lark/webhook` |
| Outbound | Your server → external APIs | Create VC, post card, call Groq for overview text |

Both must work: port 443 open for inbound, and HTTPS allowed outbound to Lark and Groq.

---

## 4. Webhook entry — `main.py`

```
Lark POST /lark/webhook
        │
        ├─ Encrypted? ──yes──► Decrypt with LARK_ENCRYPT_KEY
        │
        ├─ url_verification? ──yes──► Return challenge JSON → done
        │
        └─ Normal event
                │
                ├─ Reply {"code": 0} immediately (Lark must not wait)
                │
                └─ _process_lark_payload runs in background
                        │
                        ├─ im.message.receive_v1  → process_message (lark_logic.py)
                        │                              ├─ DM → handle_dm_generate_overview
                        │                              └─ detection group → try_handle_issue_watch
                        ├─ card.action.trigger      → handle_lark_card_action (handlers.py)
                        ├─ vc.meeting.*             → participants.py + session.py
                        └─ OAuth callback           → vc_user_oauth (VC ring permission)
```

Key idea: HTTP responds fast (`code: 0`); heavy work runs in a background task so Lark does not timeout.

---

## 5. Message routing — `lark_logic.py`

### Incident group order

Check messages in this order:

1. P1 confirmation pending? → handle create meeting / decline / Not needed
2. cancel / end / cooldown reset? → end session, cancel meeting, or reset cooldown
3. Contains `p0`? → `start_p0` (reserve VC, post link, DM duty)
4. Contains `p1`? → post P1 confirmation card
5. Otherwise → Issue Watch (Claude → Groq → keywords), or no reply

### Other chats

| Chat type | Handler |
|-----------|---------|
| Wiki group | `wiki_ai_logic.py` |
| DM with bot | `handle_dm_generate_overview` (draft/preview/overview) |
| Graph screenshot hub | `graph_screenshot_request.py` |

---

## 6. P0 / P1 session lifecycle

One active session per incident group (`P0_SESSIONS[chat_id]`) — a second declare in the same group is blocked until `end meeting` / `cancel meeting`.

Session states:

| From | Trigger | To |
|------|---------|-----|
| No session | User types `p0` or Issue Watch Declare P0 | P0 active |
| No session | User types `p1` | P1 awaiting confirmation |
| P1 awaiting | User confirms create meeting | P1 active |
| P1 awaiting | User taps Not needed / declines | No session |
| P1 active | 15-minute card → Declare as P0 | P0 active |
| P0 or P1 active | `end meeting` / `cancel` / VC ended | No session |

### What `start_p0` does (`session.py`)

1. `create_vc_reserve` → Lark VC link
2. Post plain-text meeting notice + URL (Lark unfurls native VC preview)
3. Optional fan-out to boss/hub groups
4. DM green instruction card to duty (`P0_DM_INSTRUCTION_OPEN_IDS`)
5. Optional VC ring when duty joins meeting
6. Schedule ongoing DM buzz (5 min / 10 min)
7. Schedule graph screenshot (if enabled)
8. Issue Watch: queue suggested overview if declared from alert

### End / cancel

- `p0 end` / `p1 end` → end VC, patch or skip meeting notice, recording fan-out, clear session
- `cancel meeting` → cancel VC, grey cancelled notice, release DM slots

---

## 7. DM overview pipeline

1. Someone declares p0 in the incident group
2. Bot posts VC link in the group (plain text + Lark unfurl)
3. Bot sends green instruction card to duty in DM
4. Duty pastes text + screenshots in DM
5. Duty taps Build overview → bot calls Groq → shows blue preview card
6. Duty taps Send to group → bot posts bilingual overview in the incident group
7. Optional Bitable deployment cards after send
8. Bot clears draft; if another incident was queued, prompts duty for that one next

### Per-operator FIFO queue

If two incidents are declared around the same time, each operator gets one active DM overview slot:

- First incident → green card immediately
- Second incident → queued + notice: "Finish the first overview first…"
- After Send overview → auto-prompt the next item (`release_dm_after_overview_sent`)

---

## 8. Module map — `p0_logic/`

| Module | Role | Key functions / entry points |
|--------|------|---------------------------|
| `config.py` | Env reload, getters, routing maps | `get_incident_group_chat_ids`, `p0_single_incident_group_mode`, `get_p0_issue_watch_enabled` |
| `session.py` | Session state, timers, start/end | `start_p0`, `end_p0_session`, `cancel_p0_session`, `schedule_p0_ongoing_dm_buzz` |
| `handlers.py` | Card button actions | `handle_lark_card_action`, `handle_dm_generate_overview`, send preview |
| `drafts.py` | Draft + preview per user | `seed_draft_for_incident`, `build_preview_from_draft`, `send_preview` |
| `cards.py` | All Lark card JSON | `build_dm_instruction_card`, `build_p0_meeting_created_text`, `build_p0_ongoing_dm_buzz_card` |
| `lark_client.py` | HTTP to Lark API | `post_text_to_chat`, `create_vc_reserve`, `post_card_to_open_id`, `urgent_message_for_users` |
| `groq_client.py` | Groq chat + vision OCR | `groq_overview_issue_and_zh_bilingual`, `groq_p0_keyword_declares_new_bridge` |
| `issue_watch.py` | Detection group AI watch | `try_handle_issue_watch` |
| `issue_watch_ai.py` | Claude/keyword classify | `classify_issue_watch_message` |
| `issue_watch_declare.py` | Declare P0 from alert DM | `handle_declare_p0` |
| `issue_watch_overview.py` | Suggested overview from alert | `push_suggested_overview_on_p0_declare`, `resolve_overview_routing` |
| `overview_forwarder.py` | HTTP client to `lark-forwarder` | `post_overview_via_forwarder`, `patch_overview_via_forwarder` |
| `group_overview_store.py` | Links primary + broadcast overview message IDs | `attach_broadcast_message`, used on Edit → Save |
| `vc_ring.py` | Ring @mentions into VC | `maybe_ring_on_vc_join`, `resolve_declare_ring_targets` |
| `vc_recording_fanout.py` | Minutes link after meeting | schedule on end |
| `graph_screenshot.py` | Auto Grafana PNG on P0 | `schedule_p0_graph_screenshot` |
| `participants.py` | VC join/leave tracking | `add_meeting_participant`, dept line for cards |
| `session_disk.py` | Optional session persistence | save/load across restart |

Top-level files:

| File | Role |
|------|------|
| `main.py` | FastAPI app, webhook decrypt, VC event wiring |
| `lark_logic.py` | Route group vs DM vs wiki messages |
| `wiki_ai_logic.py` | Wiki/doc Q&A (separate from P0) |

---

## 9. Feature guide — how to use

### 9.1 P0 declare (manual — type in group)

1. In the incident group, type: `p0` or `priority 0`
2. Bot posts meeting link (plain text; Lark shows VC preview)
3. Duty receives DM green card → Build overview
4. Paste details + screenshots → Send to group
5. Type `p0 end` or `end meeting` when done

P1: same flow with `p1`; includes a 15-minute escalation card to declare P0.

### 9.2 Issue Watch (auto-detect in group)

When enabled (`P0_ISSUE_WATCH_ENABLED=1`):

1. Staff/OM posts in the detection group (e.g. "players can't deposit")
2. Bot classifies (Claude → Groq → keyword rules)
3. If threshold met → DM alert card to duty with Declare as P0 / Not now
4. Declare as P0 → thread reply on concern, reaction, `start_p0`, optional suggested overview
5. A meeting is not created from chat text alone — duty must Declare or someone must type `p0`

AI failover chain:

```
Issue Watch message
        ↓
   Claude (if ANTHROPIC_API_KEY set)
        ↓
   success? → use result
        ↓ fail (no credits / timeout / bad JSON)
   Groq (if GROQ_API_KEY set)
        ↓
   success? → use result
        ↓ fail
   keyword rules (local regex — deposit, login, withdraw, etc.)
        ↓
   still no match? → ignore message
```

Set `P0_ISSUE_WATCH_AI_PROVIDER=auto` (default), `claude`, or `groq` to pick which LLM tries first. Both keys can be set — the other LLM is tried before keywords.

Important env:

```bash
P0_ISSUE_WATCH_ENABLED=1
P0_ISSUE_WATCH_DECLARE_P0_ENABLED=1
P0_DM_INSTRUCTION_OPEN_IDS=ou_duty1,ou_duty2
P0_ISSUE_WATCH_AI_PROVIDER=auto
ANTHROPIC_API_KEY=...   # Issue Watch — tried first when set
GROQ_API_KEY=...        # Issue Watch failover + overview generation
```

### 9.3 Overview DM — buttons

| Button | Action |
|--------|--------|
| Build overview | Generate preview from draft |
| Send to group | Post overview; trigger Bitable if enabled |
| Generate | Refresh Issue (same preview message) |
| Edit | Form: Incident start, Issue, Impact, Support |
| Cancel | Discard preview; new green card |

Typed shortcuts: `status`, `clear`, `generate`, `create overview emergency|game` (standalone, no meeting).

See [HOW_IT_WORKS_AND_NAVIGATION.md](HOW_IT_WORKS_AND_NAVIGATION.md)

### 9.4 Meeting notice format (Option A)

Plain text in group — no red custom card:

```
p0 detection dev

🚨 P0 meeting created.

join in meeting link
https://vc-sg.larksuite.com/j/...
```

Lark auto-unfurls VC preview (Meeting ID, timer, Joined/Ended).

### 9.5 Ongoing DM buzz (P0 still active)

### 9.6 VC ring & check persons

When duty joins VC, the bot rings configured users (`P0_MAJOR_CHECK_PERSON_IDS`, @mentions on concern).

On Issue Watch declare: thread reply "Calling and inviting the check persons for major P0 issues" (no @tags in thread).

### 9.7 Bitable deployment cards (after Send overview)

Queries Deployments and ops tables; posts cards for rows in the 48h MYT window (yesterday 00:00 → today 23:59).

```bash
P0_ADJUSTMENT_BITABLE_ENABLED=1
P0_ADJUSTMENT_BITABLE_APP_TOKEN=...
P0_ADJUSTMENT_BITABLE_TABLE_ID=...
P0_ADJUSTMENT_BITABLE_OPS_TABLE_ID=...
```

### 9.9 Grafana graph screenshot

On P0 start or on-demand in allowed chats — Playwright captures dashboard PNG.

### 9.10 Single incident group mode

```bash
P0_SINGLE_INCIDENT_GROUP=1
INCIDENT_GROUP_IDS=oc_one_group
INCIDENT_GROUP_EMERGENCY_TOPICS="oc_one_group=p0 detection dev"
```

Meeting + overview + commands all stay in one group (no detection/prompt split).

### 9.11 Broadcast overview (two-bot / `lark-forwarder`)

Use this when Send overview must appear in two places:

1. Primary bot → detection / incident group (where the P0 happened)
2. Overview bot → a separate broadcast room (e.g. PO notification, emergency-wide channel)

The primary automation bot does not need to be a member of the broadcast room. A second service — `lark-forwarder` — runs under a separate Lark app (Overview bot) and posts there.

When duty taps Send to group:

1. Primary bot posts overview card in the detection / incident group
2. Primary bot calls lark-forwarder → `POST /post-overview` (markdown + broadcast chat_id)
3. lark-forwarder uses the Overview bot Lark app to post in the broadcast group
4. Primary bot stores both message IDs (primary + broadcast)
5. Edit → Save in DM PATCHes both cards (when forwarder returned the broadcast message_id)

Routing resolution (`config.resolve_overview_send_routing`):

| Priority | Env | Effect |
|----------|-----|--------|
| 1 | `INCIDENT_OVERVIEW_SEND_MAP=oc_detection=oc_broadcast` | Broadcast destination for that detection group |
| 2 | `P0_OVERVIEW_POST_TO_INCIDENT_SOURCE_CHAT=1` | Primary post stays in detection (not prompt mirror) |
| 3 | Forwarder off | Single post only (legacy `OVERVIEW_TARGET_GROUP_CHAT_ID` / map) |

Primary bot `.env`:

```bash
INCIDENT_OVERVIEW_SEND_MAP=oc_detection_a=oc_broadcast_room
P0_OVERVIEW_POST_TO_INCIDENT_SOURCE_CHAT=1
LARK_OVERVIEW_FORWARDER_ENABLED=1
LARK_OVERVIEW_FORWARDER_URL=http://127.0.0.1:8010
LARK_OVERVIEW_FORWARDER_SECRET=change-me
```

`lark-forwarder` service (separate process, see `lark-forwarder/`):

```bash
# lark-forwarder/.env — Overview Lark app credentials
LARK_APP_ID=cli_overview_bot
LARK_APP_SECRET=...
LARK_FORWARDER_BROADCAST_CHAT_ID=oc_broadcast_room
LARK_FORWARDER_SECRET=change-me   # must match primary
```

Run forwarder on port 8010 (or behind nginx on a subdomain). Restart both services after deploy.

Edit overview in group: When `P0_GROUP_OVERVIEW_EDIT_ENABLED=1`, duty edits in DM → Save PATCHes:

- Primary overview card in detection group
- Broadcast copy via `PATCH /patch-overview` on `lark-forwarder` (requires forwarder to return `message_id` on post)

Without forwarder: Set only `INCIDENT_OVERVIEW_SEND_MAP` — primary bot posts directly to the mapped broadcast group (primary bot must be in that chat).

Optional fan-out: `OVERVIEW_DETECTION_FANOUT_CHAT_IDS` — duplicate overview to extra groups from the primary bot (separate from forwarder pattern).

See also `env.example` § Send overview / forwarder comments and `docs/DEPLOYMENT_ARCHITECTURE.md`.

---

## 10. Dual meeting / two VC links (current limit)

| | Today | If you need 2 active VC in same group |
|--|-------|----------------------------------------|
| Lark platform | Allowed | Allowed |
| Bot | 1 session per `chat_id` | Requires refactor: `session_id`, end/cancel picker |
| Overview queue | FIFO per operator | Bind queue item to session |

Practical interim: one bridge + queued overviews (second declare enqueues DM instead of blocking forever).

---

## 11. Troubleshooting quick reference

| Symptom | Check |
|---------|--------|
| Bot ignores group messages | `INCIDENT_GROUP_IDS` matches webhook `chat_id` in logs |
| Issue Watch no DM | `P0_ISSUE_WATCH_ENABLED`, `P0_DM_INSTRUCTION_OPEN_IDS`, logs `issue_watch:` |
| Issue Watch crash on alert | `get_overview_target_chat_id_for_source_incident` (routing fix) |
| Claude fails | Groq tried next (if `GROQ_API_KEY` set); then keyword rules for deposit/login patterns |
| No overview AI | `GROQ_API_KEY` |
| DM 230013 | Bot availability for user in Lark admin |
| Second p0 blocked | Active session — `end meeting` first |

Logs: `journalctl -u lark-ops-ai -f`

---

## 12. How the code was organized (design history)

1. Monolith scripts → split into `p0_logic` package (session, cards, drafts, handlers)
2. `main.py` = thin FastAPI webhook layer
3. `lark_logic.py` = chat-type routing (incident vs wiki vs DM)
4. Env-driven routing — multi-group, detection/prompt split, later `P0_SINGLE_INCIDENT_GROUP`
5. Issue Watch — Claude + keyword pipeline, declare-from-DM
6. Meeting UX — plain text + Lark unfurl (Option A)
7. Integrations — Bitable, VC ring, recording fan-out, graph screenshot, broadcast overview forwarder
8. Operator UX — FIFO overview queue, Major/Minor buzz timers, formal escalation text

---

## 13. Document index (all `.md` files)

```
lark-ops-ai/
├── README.md                          ← Quick start
├── p0_logic/README.md                 ← Package modules + import examples
└── docs/
    ├── PROJECT_OVERVIEW.md            ← This file (master guide)
    ├── ARCHITECTURE_AND_FLOW.md       ← Webhook + session flow
    ├── DEPLOYMENT_ARCHITECTURE.md     ← nginx, systemd, Groq
    ├── HOW_IT_WORKS_AND_NAVIGATION.md ← UI navigation
    ├── P0_P1_OPERATOR_GUIDE.md        ← Operator SOP
    ├── IT_LARK_DEV_APP_CHECKLIST.md   ← Lark scopes
    ├── CI_CD.md                       ← CI/CD
    └── ../lark-forwarder/             ← Overview broadcast service (separate bot app)
```

---

Last updated: 2026-06 — reflects Issue Watch, Option A meeting text, Major/Minor ongoing buzz, Bitable, single-group mode, and declare UX changes.
