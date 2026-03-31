# P0 / P1 meeting & overview automation — operator guide

English operator doc (refined from internal SOP). Button labels match the current bot UI.

---

## 1. What this automation does

- **Incident group:** When someone declares **P0** or **P1** (e.g. types `p0`, `p1`, `priority 0`, `priority 1`), the bot can **create a Lark video meeting** and post a **Join** card.
- **DM (duty / on-call):** The bot messages configured user(s) with an **instruction card** to build a **bilingual incident overview** from pasted **text** and **screenshots** (OCR), then **send it to the target group**. The DM draft is tied to a **target** group: **active P0/P1 session** if any; otherwise **one** configured incident group is used automatically; optional env **`OVERVIEW_TARGET_GROUP_CHAT_ID`** overrides; **multiple** incident groups without env need a live **p0** session or an admin-configured default.

Which groups count as “incident” and which DM recipients get the card are set in **`.env`** (see §7).

---

## 2. End-to-end flow (short)

1. **Group:** Declare **P0/P1** → meeting card (and DM instruction, if configured).
2. **DM:** Paste details / screenshots → tap **Build overview** (typed build commands removed — use the button).
3. **Preview card:** Review → **Send to group**, **Generate** (refresh issue), **Edit**, or **Cancel**.
4. **Group:** When the incident is done → **`p0 end` / `p1 end`** (or VC ends) → **meeting ended** card + summary line with **duration**.

---

## 3. DM — instruction card

| Action | What it does |
|--------|----------------|
| **Build overview** | Builds the preview from current draft (text + screenshot OCR). |
| **Clear draft** | Clears draft + preview. You always get a DM line: *Draft cleared. Kindly paste screenshots or text again…* Optional: whole instruction card repost (`P0_DM_REPOST_INSTRUCTION_AFTER_RESET`). |
| **Participants** | Lists **names** currently tracked for the meeting (from VC join events). |

You can also type in DM (whole line, case-insensitive):

- `status` / `draft` / `check` — draft summary  
- `clear` / `reset` / `discard` / `cancel` — same idea as **Clear draft**  
- `create overview emergency` or `create overview game` — start a **standalone** overview (no live meeting) for that incident group. Routing uses `P0_STANDALONE_OVERVIEW_TAGS` or label matching on `INCIDENT_GROUP_EMERGENCY_TOPICS`. For normal P0/P1 sessions, use **Build overview** on the card.

---

## 4. DM — overview **preview** card

| Button | What it does |
|--------|----------------|
| **Send to group** | Posts the final overview card to the **target** incident/overview group. Clears your draft/preview state. |
| **Generate** | Re-runs AI mainly on **Issue**; updates the **same** preview message when possible. |
| **Edit** | Opens the **edit** form (one card, updated in place on repeat saves). |
| **Cancel** | **Recalls** the preview message and sends a **fresh** instruction card so you can start over. |

**Edit card:** **Save** applies changes and refreshes the preview; **Back** dismisses the edit card and returns to the preview.

---

## 5. Group chat — during the meeting

### P0 ongoing

- After a delay, the bot may post an **ongoing P0** card (participants / departments line).
- **Departments** come from the **support sheet** (name → dept). Names **not** on the sheet roll up as **`Other`** on that line. **Participants** in DM shows **raw display names**.

### Ending / cancelling

- **End:** phrases like **`p0 end`**, **`end p0`**, **`p1 end`**, **`end meeting`** (whole line), etc. → invite card becomes **✅ meeting ended** (when possible) + chat line: *Meeting ended. Duration: … Meeting ID: …*
- **Cancel:** **`cancel meeting`** — optional reason after the phrase; if omitted, reason is *Unspecified*.
- Typing **end** again with no active session may **replay** the last ended card (if the bot still has it in memory) or show a **no active meeting** card.

**Note:** Anyone in the group who can message the bot can trigger these commands unless you add future restrictions.

---

## 6. P1-specific flows

### A) First time: “P1 mentioned”

- Card asks whether to **Create meeting** or **Don't need**.
- If **Don't need**, no VC is created for that prompt.

### B) After ~15 minutes (P1 session)

- Card **⏱ P1 15 mins meeting** asks whether to declare **P0**.
- **Declare as P0** → escalation notice, session becomes **P0**, P0 DM + ongoing flow.
- **Still P1** → session **stays P1**; bot posts: *The meeting is continuing as a P1 meeting.* (no automatic “meeting ended” from this button.)

---

## 7. Configuration (for admins / `.env`)

| Goal | Variable(s) |
|------|-------------|
| Which **group chats** run P0/P1 | `INCIDENT_GROUP_ID` or `INCIDENT_GROUP_IDS` (`oc_...`) |
| **DM recipients** for instruction/overview | `P0_DM_INSTRUCTION_OPEN_ID` or `P0_DM_INSTRUCTION_OPEN_IDS` (comma-separated `ou_...`). If empty → whoever typed `p0`/`p1`. |
| VC **organizer** (`owner_id`) | `P0_OWNER_OPEN_IDS` — **first** `ou_` is used. Alias: `P0_INVITEE_OPEN_IDS`. |
| **Reserve window** (not a 2h cap) | `P0_VC_RESERVE_END_OFFSET_SEC` — seconds until reserve `end_time`; default **30 days** (Lark allows up to ~30 days). |
| Per-group **titles / VC topic** | `INCIDENT_GROUP_EMERGENCY_TOPICS` (`oc_xxx=Label,…`) |
| Ignore users for **starting** P0/P1 only | `P0_TRIGGER_IGNORE_OPEN_IDS` |

See **`env.example`** in the repo for the full template.

---

## 8. Limitations (honest)

- **Lark** still requires a reserve **end time**; we use a **long** default (30 days), not “infinite.”
- **Actual max call length** may still depend on **Lark product** limits; the bot does not hang up the call.
- **Replay / snapshots** for “end again” are **in-memory** — lost if the bot process restarts.

---

## 9. Complete command reference (typed text)

Unless noted, matching is **case-insensitive**. **Incident group** = chats listed in `INCIDENT_GROUP_ID` / `INCIDENT_GROUP_IDS`.

### Incident group — declare / trigger

| What | How |
|------|-----|
| **P0 meeting** | **`p0`** or **`priority 0`** anywhere in the message (word boundaries). Ignored if the line looks like a pasted meeting-card footer (`P0 declared - created a meeting…`). |
| **P1 prompt (“create meeting?”)** | **`p1`** or **`priority 1`** anywhere (same footer rule for `P1 declared - …`). |

### Incident group — whole line only

| Command | Aliases / notes |
|---------|------------------|
| **Cooldown reset** (no new VC) | `cooldown reset`, `reset cooldown`, `clear cooldown`, `p0 cooldown reset` |
| **Demo — ongoing P0 card** | `p0 demo ongoing`, `demo p0 ongoing`, optional `card` |
| **Demo — P1 15‑min card** | `p1 demo 15`, `demo p1 15`, optional `mins` / `card` |

### Incident group — cancel / end

| Command | Pattern |
|---------|---------|
| **Cancel** | Line starts with `cancel meeting`, `cancel p0`, `cancel p1`, or `cancel` — optional free text after as **reason**. |
| **End P0** | `p0 end`, `end p0`, `close p0`, `p0 resolved` (anywhere in message). |
| **End P1** | `p1 end`, `end p1`, `close p1`, `p1 resolved` (anywhere in message). |
| **End (any active)** | `end meeting` — whole line only; ends whichever P0/P1 session is active in this chat. |

### Incident group — while P1 “create meeting?” is open

| Intent | Typed line |
|--------|------------|
| Create meeting | `create meeting`, `p1 create`, or `yes` |
| Decline | `not needed`, `don't need`, `dont need`, or `no` |

### DM (duty / overview) — whole line

| Command | Aliases |
|---------|---------|
| Draft status | `status`, `draft`, `check` |
| Clear draft | `clear`, `reset`, `discard`, `cancel` |
| Build overview | _(button only — no typed aliases)_ |
| Standalone overview (no meeting) | `create overview emergency`, `create overview game` |
| Who is in the meeting | `who is in the meeting`, `who are in the meeting`, `participants`, `list participants`, `sino nasa meeting` |
| Is someone in the meeting? | `is <name> in the meeting?` (e.g. `is Alice in the meeting?`) |

### Wiki group (`WIKI_GROUP_CHAT_ID`)

- **Any message text** in that chat is sent to **wiki AI** (Groq + linked Doc); no fixed slash-commands.

### Card buttons (not typed)

- **DM:** Build overview, Clear draft, Participants, preview (**Send to group**, **Generate**, **Edit**, **Cancel**), edit form (**Save**, **Back**).
- **Group:** P1 **Create meeting** / **Not needed**; P1 15‑min **Declare as P0** / **Still P1**; overview/edit flows as implemented in `p0_logic/handlers.py`.

### Operator restriction (optional)

If **`P0_INCIDENT_GROUP_COMMAND_OPEN_IDS`** is set, **cancel / end / cooldown reset** and some **P1 typed** controls may be limited to those `ou_` users. Declaring **`p0` / `p1`** in the group is **not** gated by that list (see also **`P0_TRIGGER_IGNORE_OPEN_IDS`** for separate ignore behavior).

---

*Generated/refined from: `P0_P1 meeting and overview automation.docx` + current `lark-ops-ai` / `p0_logic` behavior.*
