# Lark app setup for IT — P1/P0 automation bot (prod + dev copy)

Use this when creating a second dev app or reviewing the production app.  
Copy permissions + events from prod → dev; only App ID / Secret / Encrypt Key / Webhook URL differ.

---

## 1. Webhook (Event & Callback)

| Item | Value |
|------|--------|
| Request URL | `https://<your-domain>/lark/webhook` |
| Prod | e.g. `https://lark-ops.company.com/lark/webhook` |
| Dev | e.g. `https://lark-ops-dev.company.com/lark/webhook` |
| Encrypt Key | Set in app → same value as `LARK_ENCRYPT_KEY` in server `.env` |
| Verification | Lark sends `url_verification` challenge — app must return `challenge` (already implemented) |

Important: One Lark app = one Request URL. Dev and prod need two apps if both receive live events.

---

## 2. Event subscriptions (required)

Subscribe in Developer Console → Events:

| Event | Why |
|-------|-----|
| `im.message.receive_v1` | Group messages (p0/p1, Issue Watch) + DM (overview build) |
| `card.action.trigger` | Buttons: Build overview, Send to group, Issue Watch, P1 confirm, etc. |
| `im.message.recalled_v1` | Recall a sent group overview → re-DM its preview to the operator (`P0_OVERVIEW_RECALL_RESTORE_ENABLED`) |

### VC / recording (if P0 meetings + recording fanout enabled)

| Event | Why |
|-------|-----|
| `vc.meeting.join_meeting_v1` | Track VC participants |
| `vc.meeting.leave_meeting_v1` | Remove participants |
| `vc.meeting.meeting_ended_v1` | End P0 session, schedule recording poll |
| `vc.meeting.recording_ready_v1` | Post recording link to hub group |

---

## 3. API permissions / scopes

Names may appear slightly differently in Lark vs Feishu console — enable the equivalent of:

### Required (core bot)

| Scope (typical name) | Used for |
|----------------------|----------|
| `im:message` | Send/receive messages |
| `im:message:send_as_bot` | Post cards & text as bot |
| `im:message:update` | PATCH meeting card / preview cards in place |
| `im:message:recall` | Cancel preview, recall edit cards |
| `im:resource` | Upload Grafana screenshot images |
| `im:chat` or `im:chat:readonly` | Resolve group names (if used) |
| `contact:user.base:readonly` | Lookup user display names (`contact/v3/users`) |
| `vc:reserve` / VC reserve apply | Create P0/P1 Lark meetings |
| `vc:meeting` / `vc:meeting:readonly` | End meeting, read meeting info |

### Recommended (features you likely use)

| Scope | Used for |
|-------|----------|
| `im:message.urgent` or `im:message:urgent` |
| `im:message.reactions:write_only` | Grafana on-demand ✅/❌ reactions on request message |
| `vc:record:readonly` | Fetch recording URL after meeting |
| `vc:record` | Grant recording view to hub groups (`set_permission`) — confirm with IT (some tenants need user token) |

### Optional (only if enabled in `.env`)

| Scope | Feature |
|-------|---------|
| `sheets:spreadsheet:readonly` | Support sheet → department mapping (`SUPPORT_SHEET_*`) |
| `wiki:wiki:readonly` | Wiki AI (if used) |

---

## 4. Bot membership

IT / ops must add the bot to:

- Detection incident group(s) (`INCIDENT_GROUP_IDS`)
- Prompt / mirror group(s) (`INCIDENT_OVERVIEW_TARGET_MAP` values)
- Overview / fanout groups if configured (`INCIDENT_OVERVIEW_SEND_MAP`, `P0_NOTIFICATION_HUB_CHAT_IDS`, etc.)
- DM: bot can message users listed in `P0_DM_INSTRUCTION_OPEN_IDS` (no group add needed for DM)

---

## 5. Optional extra Lark apps (prod may already have these)

| App | Purpose | Dev copy? |
|-----|---------|-----------|
| Primary (`LARK_APP_ID`) | Main P0 bot + webhook | Yes — create dev app |
| Overview forwarder (`lark-forwarder`) | Second bot posts overview to broadcast room | Optional on dev; set `LARK_OVERVIEW_FORWARDER_ENABLED=0` |

Each extra app needs its own App ID / Secret and (if it sends messages) similar im:message scopes. Only the primary app needs the main webhook for group messages.

---

## 6. Server `.env` (not in Lark console)

After IT creates the dev app, ops fills on dev server only:

```bash
LARK_APP_ID=cli_dev_...
LARK_APP_SECRET=...
LARK_ENCRYPT_KEY=...
INCIDENT_GROUP_IDS=oc_dev_...
# see env.staging.example
```

Prod server keeps prod credentials — never copy dev `.env` to prod.

---

## 7. Minimal dev app (if IT wants smallest scope)

For testing Issue Watch + P0 declare + overview DM only:

- Events: `im.message.receive_v1`, `card.action.trigger`
- Scopes: `im:message`, `im:message:send_as_bot`, `im:message:update`, `im:message:recall`, `contact:user.base:readonly`, VC reserve/meeting scopes
- Skip: `im:message.urgent`, `vc:record`, sheets, forwarder — until needed

---

Generated from `lark-ops-ai` codebase — `main.py` webhook + `env.example`.
