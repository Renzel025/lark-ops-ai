# How it works & how to navigate  

This guide explains what the bot does and where you click / what you type in Lark/Feishu.  

---

## 1. Big picture

| Where | What happens |
|--------|----------------|
| Incident group (P0/P1 chat) | Someone types `p0`, `p1`, `priority 0`, or `priority 1` → bot starts a session and posts a meeting card (when applicable). |
| Your DM with the bot | You get an instruction card: paste details, screenshots, use buttons to build an overview preview, then send to group. |

Flow: Incident group → session starts → you work in DM → optional preview + edit → Send to group posts the final overview to the target chat.  

---

## 2. Starting: incident group

1. In the configured incident group, type `p0` or `p1` (or `priority 0` / `priority 1`).  
2. Bot creates/opens the P0/P1 session for that chat and may show a meeting / VC card in the group.  
3. Open your DM with the app — you should see the instruction card (who gets it depends on config: usually the person who typed `p0`/`p1`, or fixed users in `P0_DM_INSTRUCTION_OPEN_IDS`).  

---

## 3. DM: instruction card → building a draft

Goal: Collect text and screenshots before generating the overview.  

| Action | How |
|--------|-----|
| Add text | Paste or type messages in the DM (they append to the draft). |
| Add screenshot | Send an image; bot OCRs it into the draft. |
| Check draft | Type `status`, `draft`, or `check` (exact line, case-insensitive). |
| Clear everything | Type `clear`, `reset`, `discard`, or `cancel` (exact line) or use Clear on the card — clears draft + preview (repost instruction depends on `P0_DM_REPOST_INSTRUCTION_AFTER_RESET`). |
| Generate preview | Type `generate`, `preview`, or `create overview` or tap Generate overview on the instruction card. |

After Generate, the bot builds an Overview preview card in the same DM (English + translated Chinese body).

---

## 4. Overview preview card — navigation

This card shows the draft as a bilingual overview and four main actions:

| Button | What it does |
|--------|----------------|
| Send to group | Posts the overview card to the target incident group (or configured overview channel). Clears your draft/preview state in DM. |
| Generate | Re-runs AI on the same source text (mainly refreshes Issue). Updates the same preview message (no duplicate preview card). |
| Edit | Opens one edit card (or updates the same edit card if it already exists). You change Issue / Impact / Support there. |
| Cancel | Recalls (removes) the preview message, clears draft/preview, and sends a fresh instruction card so you can start again. |

Tip: Preview and edit cards are updated in place when possible (`update_multi`), so you don’t get many stacked preview cards.  

---

## 5. Edit card — navigation

| Control | What it does |
|---------|----------------|
| Save | Saves your fields → updates the overview preview (same preview message) and refreshes this same edit card with the new values. You can Save many times on one edit card. |
| Back | Removes the edit card from the chat (recall) and restores the overview preview card. Next time you tap Edit, a new edit card appears. |

While the bot expects you to use the Edit card, it may block normal pasted text in DM with a short hint (when “awaiting edit” mode is on). Use Save or Back to leave that mode.  

---

## 6. After you tap “Send to group”

1. Overview is posted to the group as a card.  
2. Your DM draft + preview state is cleared.  
3. Any open edit card message is recalled so the DM thread stays clean.  

---

## 7. Quick path summary

```
Incident group: type p0 / p1
        ↓
DM: instruction card
        ↓
Paste text + images → Generate (or button)
        ↓
Preview card: [Send] [Generate] [Edit] [Cancel]
        ↓
Optional: Edit card → Save (repeat) or Back
        ↓
Send to group → done
```

---

## 8. Optional: other DM commands

Other DM phrases (see `p0_logic/config.py`): who in meeting and is … in meeting style queries for participants.  

---

## 9. Related config (for admins)

| Env | Effect |
|-----|--------|
| `P0_DM_INSTRUCTION_OPEN_IDS` | Multiple users each get their own DM thread — not one shared chat. |
| `P0_DM_REPOST_INSTRUCTION_AFTER_RESET` | After Clear draft (not Cancel preview): whether to repost the instruction card + status line. Cancel preview always recalls preview and reposts instruction. |
| Lark scopes | `im:message:send_as_bot`, `im:message:update` (PATCH cards), `im:message:recall` (DELETE) for cancel / back / send cleanup. |

More variables: `env.example` in the repo root.

---

Last updated for: single edit card (PATCH), preview PATCH, cancel recall + instruction repost, clear-draft repost flag.
