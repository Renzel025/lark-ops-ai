# Env variables — feature ON/OFF guide

See also: **[MANUAL_TEST_COMMANDS.md](./MANUAL_TEST_COMMANDS.md)** — Python/shell commands to test each feature.

Reference for **lark-ops-ai-dev** (`features/` layout). Values use **`on` / `off`** (also accepted: `yes` / `no`, `true` / `false`, `1` / `0`).

**Rule:** If there is an `ENABLED` toggle → set `on` or `off`. If it is only chat IDs / tokens / open IDs → **blank = off**, value set = **on**.

**Servers:**

| | Host | Repo path |
|--|------|-----------|
| **Prod** | OSE-bot | `/root/lark-ops-ai` |
| **Dev** | OSE-bot-dev | `/root/lark-ops-ai-dev` |

---

## 1. `session/` — P0/P1 + VC meeting

| Variable | ON | OFF | Default |
|----------|----|-----|---------|
| *(core)* | Set `INCIDENT_GROUP_IDS=oc_...` | No group = no P0 | — |
| `P0_SINGLE_INCIDENT_GROUP` | on | off | off |
| `P0_MEETING_CARDS_IN_SOURCE_INCIDENT_CHAT` | on | off | off |
| `P0_MEETING_CANCELLED_FANOUT_ENABLED` | on | off | **on** |
| `P0_ONGOING_DM_BUZZ_ENABLED` | on | off | **on** |
| `P0_VC_AUTO_CANCEL_IF_NO_JOINS_SEC` | number (e.g. `1800`) | blank = off | off |
| `P0_REDECLARE_SUPERSEDES_ACTIVE` | on = 2nd P0 cancels current + starts new | off = ignored | **off** ⚠️ on kills a live meeting on re-declare |
| `P0_MULTI_MEETING_PER_GROUP` | on = each p0 = its own coexisting meeting | off = one per group | **off** (wins over supersede; end each via native VC end) |
| `P0_SESSION_DISK_MAX_AGE_HOURS` | hours before a persisted session file is treated as ended | `12` | **12** — stops a stale `sessions/*.json` (native VC-end missed cleanup) from blocking every new p0; `0` = never expire |

**Not toggles — must be set:**

| Variable | Purpose |
|----------|---------|
| `INCIDENT_GROUP_IDS` | Which groups accept P0/P1 |
| `P0_DM_INSTRUCTION_OPEN_ID` / `P0_DM_INSTRUCTION_OPEN_IDS` | Who gets DM overview builder |
| `P0_OWNER_OPEN_IDS` | VC meeting invitees |

### Prod vs dev (session)

| Setting | Prod | Dev |
|---------|------|-----|
| `P0_SINGLE_INCIDENT_GROUP` | **on** | **on** |
| `INCIDENT_GROUP_IDS` | **on** — 2 groups (CP-Emergency + Game urgent) | **on** — 1 dev group |
| `P0_GROUP_OVERVIEW_EDIT_ENABLED` | **on** | **on** |
| `P0_ONGOING_DM_BUZZ_ENABLED` | **on** | **on** |
| `P0_VC_AUTO_CANCEL_IF_NO_JOINS_CHAT_IDS` | **on** — both prod incident groups | **on** — dev group |
| `P0_MEETING_CARDS_IN_SOURCE_INCIDENT_CHAT` | not set (default **off**) | not set (default **off**) |
| `P0_MEETING_CANCELLED_FANOUT_ENABLED` | not set (default **on**) | not set (default **on**) |

---

## 2. `overview/` — DM overview + Bitable + forwarder

| Variable | ON | OFF | Default | Notes |
|----------|----|-----|---------|-------|
| `P0_ADJUSTMENT_BITABLE_ENABLED` | on | off | on if `APP_TOKEN` set | Master switch for Bitable cards |
| `P0_ADJUSTMENT_BITABLE_ON_P0_DECLARE` | on | off | **on** (dev code) | Post 📦/🔴 on P0 declare — **dev only until merge** |
| `P0_ADJUSTMENT_BITABLE_THREAD_FOLLOWUPS` | on | off | **on** (dev code) | Page 1 in group; page 2+ in thread — **dev only until merge** |
| `P0_ADJUSTMENT_BITABLE_POST_CHAT_ID` | `oc_...` set | blank = off | off | Fixed group for 📦/🔴; blank = follow meeting/overview routing |
| `P0_ADJUSTMENT_BITABLE_ALSO_SEND_TO_GROUP` | on | off | off (dev) / on (prod legacy) | **Legacy orange cards only** — does **not** affect new 📦/🔴 cards |
| `P0_ADJUSTMENT_BITABLE_REPLY_IN_THREAD` | on | off | **on** | Legacy path: reply under overview |
| `LARK_OVERVIEW_FORWARDER_ENABLED` | on | off | off | Broadcast overview via forwarder |
| `P0_GROUP_OVERVIEW_EDIT_ENABLED` | on | off | **on** | Edit overview in group after Send |
| `P0_OVERVIEW_POST_TO_INCIDENT_SOURCE_CHAT` | on | off | off | Send overview to detection group |
| `P0_OVERVIEW_AI_PROVIDER` | `claude` / `groq` | `auto` | **auto** | LLM for overview issue+bilingual. `auto` = Claude if configured → Groq → `summarize_issue`. Mirrors `P0_ISSUE_WATCH_AI_PROVIDER` |
| `P0_OVERVIEW_ANTHROPIC_MODEL` | model id set | blank | blank | Claude model just for overviews; blank = shared `ANTHROPIC_MODEL`. Set a Sonnet id for higher accuracy |
| `P0_OVERVIEW_RECALL_RESTORE_ENABLED` | on | off | **on** | Recall a sent group overview → re-DM its preview to the sender. Needs `im.message.recalled_v1` event subscribed; in-memory 24h TTL |

**Must set for Bitable:**

| Variable | Purpose |
|----------|---------|
| `P0_ADJUSTMENT_BITABLE_APP_TOKEN` | Lark Base app token |
| `P0_ADJUSTMENT_BITABLE_TABLE_ID` | Deploy table |
| `P0_ADJUSTMENT_BITABLE_OPS_TABLE_ID` | Ops table (线上操作) |

**Optional Bitable tuning (numbers, not on/off):**

- `P0_ADJUSTMENT_BITABLE_OPS_MAX_ROWS` (default 8)
- `P0_ADJUSTMENT_BITABLE_DEPLOY_MAX_ROWS` (default 16)
- `P0_ADJUSTMENT_BITABLE_OPS_PAGE_SIZE` / `DEPLOY_PAGE_SIZE` (default 8)

### Prod vs dev (overview / Bitable)

| Setting | Prod | Dev |
|---------|------|-----|
| `P0_ADJUSTMENT_BITABLE_ENABLED` | **on** | **on** |
| Bitable tokens + table IDs | **set** (same Base) | **set** (same Base) |
| Card design | **old** orange markdown (after Send overview) | **new** 📦 部署流水 + 🔴 线上操作汇总 |
| `P0_ADJUSTMENT_BITABLE_ON_P0_DECLARE` | N/A (not in prod code) | **on** (default in code) |
| `P0_ADJUSTMENT_BITABLE_THREAD_FOLLOWUPS` | N/A (not in prod code) | **on** |
| `P0_ADJUSTMENT_BITABLE_ALSO_SEND_TO_GROUP` | **on** (legacy only) | **on** (no effect on new cards) |
| `LARK_OVERVIEW_FORWARDER_ENABLED` | **on** | **on** |
| `INCIDENT_OVERVIEW_SEND_MAP` | **on** → prod broadcast group | **on** → dev broadcast group |
| `OVERVIEW_DETECTION_FANOUT_CHAT_IDS` | **on** | **on** |

---

## 3. `issue_watch/`

| Variable | ON | OFF | Default |
|----------|----|-----|---------|
| `P0_ISSUE_WATCH_ENABLED` | on | off | **off** |
| `P0_ISSUE_WATCH_AUTO_OVERVIEW` | on | off | **on** |
| `P0_ISSUE_WATCH_BUZZ_ENABLED` | on | off | **on** |
| `P0_ISSUE_WATCH_DECLARE_P0_ENABLED` | on | off | **on** |
| `P0_ISSUE_WATCH_DECLARE_REPLY_AI` | on | off | **on** |
| `P0_ISSUE_WATCH_DECLARE_REPLY_IN_THREAD` | on | off | **on** |
| `P0_ISSUE_WATCH_DECLARE_ALSO_SEND_TO_GROUP` | on | off | **on** |

### Prod vs dev (issue watch)

| Setting | Prod | Dev |
|---------|------|-----|
| `P0_ISSUE_WATCH_ENABLED` | **off** | **on** |
| Sub-options (buzz, declare, thread, etc.) | configured (ready if enabled) | **on** |
| `P0_ISSUE_WATCH_AI_PROVIDER` | claude | groq |
| `P0_ISSUE_WATCH_MIN_CONFIDENCE` | 0.75 | 0.88 |
| `P0_ISSUE_WATCH_MIN_REPORTS` | 4 | 2 |
| `P0_ISSUE_WATCH_MIN_AFFECTED_PLAYERS` | 3 | **3** |

---

## 4. `screenshot/` — Grafana

| Variable | ON | OFF | Default |
|----------|----|-----|---------|
| `P0_GRAPH_SCREENSHOT_ENABLED` | on | off | **off** |
| `P0_GRAPH_SCREENSHOT_ON_DEMAND` | on | off | **on** |
| `P0_GRAPH_SCREENSHOT_AI` | on | off | **on** |

**Must set:**

| Variable | Purpose |
|----------|---------|
| `P0_GRAPH_SCREENSHOT_URL` | Grafana dashboard URL |
| `P0_GRAPH_SCREENSHOT_TARGET_CHAT_ID` | Where to post (optional) |

### Prod vs dev (screenshot)

| Setting | Prod | Dev |
|---------|------|-----|
| `P0_GRAPH_SCREENSHOT_ENABLED` | **on** | **on** |
| Grafana URL / credentials | **set** | **set** (same dashboard) |
| `P0_GRAPH_SCREENSHOT_TARGET_CHAT_ID` | prod screenshot group | dev screenshot group |
| `P0_GRAPH_SCREENSHOT_BROWSER_POOL` | **on** (`=1`) | **off** (`=0`) |

---

## 5. `recording/` — VC recording + ring

| Variable | ON | OFF | Default |
|----------|----|-----|---------|
| `P0_VC_RING_ENABLED` | on | off | **off** |
| `VC_RECORDING_FANOUT_SET_PERMISSION` | on | off | **on** |
| `VC_RECORDING_FANOUT_PLAIN_META` | on | off | off (dev) / **on** (prod) |
| `VC_RECORDING_FANOUT_TENANT_WIDE_VIEW` | on | off | off |

**Must set for recording fan-out:**

| Variable | Purpose |
|----------|---------|
| `VC_RECORDING_FANOUT_CHAT_IDS=oc_...` | blank = recording fan-out **off** |

**VC ring also needs:** `P0_VC_OAUTH_PUBLIC_BASE_URL`, `P0_VC_OAUTH_REDIRECT_URI`, OAuth scopes, and optionally `P0_MAJOR_CHECK_PERSON_IDS`.

### Prod vs dev (recording)

| Setting | Prod | Dev |
|---------|------|-----|
| `VC_RECORDING_FANOUT_CHAT_IDS` | **on** → boss hub group | **on** → same boss hub |
| `VC_RECORDING_FANOUT_USER_OPEN_IDS` | **set** | **set** |
| `VC_RECORDING_FANOUT_SET_PERMISSION` | **on** | **on** |
| `VC_RECORDING_FANOUT_TENANT_WIDE_VIEW` | **on** | **on** |
| `VC_RECORDING_FANOUT_DRIVE_PERM` | edit | edit |
| `VC_RECORDING_FANOUT_PLAIN_META` | not set (prod default **on** — card + text) | not set (dev default **off** — card only) |
| `P0_VC_RING_ENABLED` | **off** | **on** |
| `P0_VC_OAUTH_PUBLIC_BASE_URL` | `https://mybot.ink` | `https://testdev.mybot.ink` |
| `P0_MAJOR_CHECK_PERSON_IDS` | blank | **set** (2 users) |

---

## Extra (not under `features/` folder)

| Feature | ON | OFF | Variable |
|---------|----|-----|----------|
| **Monitoring GC** | `P0_MONITORING_CHAT_IDS=oc_...` set | blank = off | `P0_MONITORING_DUTY_WARNINGS`, `P0_MONITORING_LOG_ALERTS` |
| **Wiki AI** | `WIKI_GROUP_CHAT_ID` + `WIKI_DOC_TOKEN` set | blank = off | no `ENABLED` toggle |
| **Thread confirm** | `P0_THREAD_CONFIRM_ASKER_OPEN_IDS` and/or `TARGET_OPEN_IDS` set | both blank = off | see thread confirm vars below |
| **Keyword AI triage** | `P0_KEYWORD_AI_TRIAGE=on` | off | default **on** |
| **Groq keyword gate** | `P0_KEYWORD_GROQ_GATE=on` | off | default off |
| **Gemini** | `GEMINI_API_KEY` set + `P0_KEYWORD_AI_PROVIDER=auto` or `gemini` | no key = skipped | dev code only until merge |
| **Severity bot DM** | `P0_SEVERITY_PROMPT_ENABLED=on` | off | default off |

**Thread confirm extras:**

| Variable | ON | OFF | Default |
|----------|----|-----|---------|
| `P0_THREAD_CONFIRM_ALLOW_TOPLEVEL_YES` | on | off | off |
| `P0_THREAD_CONFIRM_USE_GROQ` | on | off | off |
| `P0_THREAD_CONFIRM_ALLOW_ASKER_SELF_YES` | on | off | off |
| `P0_THREAD_CONFIRM_ALLOW_TARGET_MENTIONS` | on | off | varies |

### Prod vs dev (extra)

| Setting | Prod | Dev |
|---------|------|-----|
| Wiki | **on** (same wiki group) | **on** |
| Thread confirm | **on** (asker + target IDs) | **on** |
| `P0_THREAD_CONFIRM_ALLOW_TOPLEVEL_YES` | **on** | **on** |
| `P0_THREAD_CONFIRM_USE_GROQ` | **on** | **on** |
| `P0_KEYWORD_GROQ_GATE` | **on** | **on** |
| `P0_KEYWORD_AI_TRIAGE` | **on** | **on** (default) |
| `GEMINI_API_KEY` + `P0_KEYWORD_AI_PROVIDER=auto` | **set** | **set** |
| `P0_SEVERITY_PROMPT_ENABLED` | **on** | **on** |
| `P0_MEETING_CREATED_TEXT_CHAT_IDS` | **on** → boss hub | **on** → dev hub |
| `P0_NOTIFICATION_HUB_CHAT_IDS` | **on** → boss hub | **off** (blank) |

---

## Quick reference — one glance

| Want this | Env |
|-----------|-----|
| P0 / VC | `INCIDENT_GROUP_IDS` ✅ |
| Bitable cards | `P0_ADJUSTMENT_BITABLE_ENABLED=on` + token + table IDs |
| Bitable on P0 declare (dev / post-merge) | `P0_ADJUSTMENT_BITABLE_ON_P0_DECLARE=on` |
| Bitable thread pages (dev / post-merge) | `P0_ADJUSTMENT_BITABLE_THREAD_FOLLOWUPS=on` |
| Bitable fixed post group | `P0_ADJUSTMENT_BITABLE_POST_CHAT_ID=oc_...` |
| Issue Watch | `P0_ISSUE_WATCH_ENABLED=on` |
| Grafana screenshot | `P0_GRAPH_SCREENSHOT_ENABLED=on` |
| Recording card | `VC_RECORDING_FANOUT_CHAT_IDS=oc_...` |
| VC ring | `P0_VC_RING_ENABLED=on` + OAuth |
| Wiki | `WIKI_GROUP_CHAT_ID` + `WIKI_DOC_TOKEN` |
| Thread confirm | `P0_THREAD_CONFIRM_ASKER_OPEN_IDS=ou_...` |
| Overview forwarder | `LARK_OVERVIEW_FORWARDER_ENABLED=on` |

---

## Prod vs dev — summary table

| Feature | Prod | Dev |
|---------|------|-----|
| P0/P1 session + VC | **on** | **on** |
| DM overview + edit | **on** | **on** |
| Wiki AI | **on** | **on** |
| Thread confirm | **on** | **on** |
| Keyword AI + Groq gate | **on** | **on** |
| Overview forwarder | **on** | **on** |
| VC recording fan-out | **on** | **on** |
| VC ring | **off** | **on** |
| Ongoing DM buzz | **on** | **on** |
| Issue Watch | **off** | **on** |
| Grafana screenshot | **on** | **on** |
| Bitable (any) | **on** — old cards | **on** — new 📦/🔴 cards |
| Bitable on P0 declare | **off** (no code) | **on** |
| Boss card thread pages | **off** (no code) | **on** |
| Gemini in keyword chain | key set; code on prod may not use yet | **on** |

---

## After merging dev → prod

Add to prod `.env` when ready:

```bash
P0_ADJUSTMENT_BITABLE_THREAD_FOLLOWUPS=on
P0_ADJUSTMENT_BITABLE_ON_P0_DECLARE=on   # or off until tested
P0_ADJUSTMENT_BITABLE_POST_CHAT_ID=oc_...   # optional fixed hub for 📦/🔴
```

`P0_ADJUSTMENT_BITABLE_ALSO_SEND_TO_GROUP` — **optional**; does not control new 📦/🔴 cards.

Test before full rollout:

```bash
python3 features/overview/scripts/test_bitable_once.py --post --chat-id=oc_YOUR_REAL_GROUP_ID
```

---

*Last updated from prod (OSE-bot) and dev (OSE-bot-dev) `.env` review. Do not commit secrets into this file.*
