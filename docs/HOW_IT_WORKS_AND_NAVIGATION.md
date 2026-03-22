# How it works & how to navigate  
# 运作方式与操作路径

This guide explains **what the bot does** and **where you click / what you type** in Lark/Feishu.  
本说明介绍机器人在飞书/Lark 中的**行为**与**操作路径**（点哪里、输入什么）。

---

## 1. Big picture | 总览

| Where | What happens |
|--------|----------------|
| **Incident group** (P0/P1 chat) | Someone types **`p0`**, **`p1`**, **`priority 0`**, or **`priority 1`** → bot starts a session and posts a **meeting card** (when applicable). |
| **Your DM with the bot** | You get an **instruction card**: paste details, screenshots, use buttons to build an **overview preview**, then **send to group**. |

**Flow:** Incident group → session starts → you work in **DM** → optional **preview + edit** → **Send to group** posts the final overview to the target chat.  
**路径：** 应急群触发 → 在 **私聊机器人** 里整理内容 → 可选 **预览/编辑** → **发送到群** 把概览发到目标群。

---

## 2. Starting: incident group | 在应急群开始

1. In the **configured incident group**, type **`p0`** or **`p1`** (or `priority 0` / `priority 1`).  
   在已配置的**应急群**输入 **`p0`** / **`p1`**（或 `priority 0` / `priority 1`）。
2. Bot creates/opens the **P0/P1 session** for that chat and may show a **meeting / VC card** in the group.  
   机器人为该群开启会话，并可能在群内发**会议卡片**。
3. Open your **DM with the app** — you should see the **instruction card** (who gets it depends on config: usually the person who typed `p0`/`p1`, or fixed users in `P0_DM_INSTRUCTION_OPEN_IDS`).  
   打开与应用的**私聊**，会看到**说明卡片**（收件人由配置决定：通常是触发人，或 `P0_DM_INSTRUCTION_OPEN_IDS` 中的用户）。

---

## 3. DM: instruction card → building a draft | 私聊：说明卡片 → 草稿

**Goal:** Collect text and screenshots before generating the overview.  
**目标：** 在生成概览前收集文字与截图。

| Action | How |
|--------|-----|
| Add text | **Paste or type** messages in the DM (they append to the **draft**). |
| Add screenshot | Send an **image**; bot OCRs it into the draft. |
| Check draft | Type **`status`**, **`draft`**, or **`check`** (exact line, case-insensitive). |
| Clear everything | Type **`clear`**, **`reset`**, **`discard`**, or **`cancel`** (exact line) **or** use **Clear** on the card — clears draft + preview (repost instruction depends on `P0_DM_REPOST_INSTRUCTION_AFTER_RESET`). |
| Generate preview | Type **`generate`**, **`preview`**, or **`create overview`** **or** tap **Generate overview** on the instruction card. |

After **Generate**, the bot builds an **Overview preview** card in the same DM (English + 中文).  
点击/输入 **生成** 后，私聊里会出现 **概览预览** 卡片（中英内容）。

---

## 4. Overview preview card — navigation | 概览预览卡片 — 操作

This card shows the draft as a **bilingual overview** and four main actions:

| Button | What it does | 按钮 |
|--------|----------------|------|
| **Send to group** | Posts the overview **card** to the **target incident group** (or configured overview channel). Clears your draft/preview state in DM. | **发送到群** |
| **Generate** | Re-runs AI on the **same source text** (mainly refreshes **Issue**). Updates the **same preview message** (no duplicate preview card). | **重新生成** |
| **Edit** | Opens **one edit card** (or **updates** the same edit card if it already exists). You change Issue / Impact / Support there. | **编辑** |
| **Cancel** | **Recalls** (removes) the preview message, clears draft/preview, and sends a **fresh instruction card** so you can start again. | **取消** |

**Tip:** Preview and edit cards are **updated in place** when possible (`update_multi`), so you don’t get many stacked preview cards.  
**提示：** 预览与编辑卡片会尽量**原地更新**，避免刷屏。

---

## 5. Edit card — navigation | 编辑卡片 — 操作

| Control | What it does | 说明 |
|---------|----------------|------|
| **Save** | Saves your fields → **updates the overview preview** (same preview message) **and** refreshes this **same edit card** with the new values. You can **Save** many times on **one** edit card. | **保存**：更新预览 + 刷新本编辑卡，可多次保存 |
| **Back** | **Removes** the edit card from the chat (recall) and **restores** the overview preview card. Next time you tap **Edit**, a **new** edit card appears. | **返回**：撤回编辑卡，恢复预览；下次编辑会新发一张编辑卡 |

While the bot expects you to use the **Edit card**, it may **block** normal pasted text in DM with a short hint (when “awaiting edit” mode is on). Use **Save** or **Back** to leave that mode.  
在“等待编辑”状态下，私聊粘贴可能被提示去用编辑卡片；用 **保存** 或 **返回** 退出该状态。

---

## 6. After you tap “Send to group” | 点击「发送到群」之后

1. Overview is posted to the **group** as a card.  
   概览以卡片形式发到**群**。
2. Your DM **draft + preview** state is cleared.  
   私聊里的草稿与预览状态会清空。
3. Any open **edit card** message is **recalled** so the DM thread stays clean.  
   若仍有编辑卡消息，会被**撤回**，私聊更干净。

---

## 7. Quick path summary | 路径速查

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

## 8. Optional: other DM commands | 其他私聊指令（若已开启）

Other DM phrases (see `p0_logic/config.py`): **who in meeting** and **is … in meeting** style queries for participants.  
其他私聊短语：会议参与者相关问法见配置中的正则。

---

## 9. Related config (for admins) | 管理员相关配置

| Env | Effect |
|-----|--------|
| `P0_DM_INSTRUCTION_OPEN_IDS` | Multiple users each get their **own** DM thread — not one shared chat. |
| `P0_DM_REPOST_INSTRUCTION_AFTER_RESET` | After **Clear draft** (not Cancel preview): whether to repost the instruction card + status line. **Cancel preview** always recalls preview and reposts instruction. |
| Lark scopes | `im:message:send_as_bot`, `im:message:update` (PATCH cards), `im:message:recall` (DELETE) for cancel / back / send cleanup. |

More variables: **`env.example`** in the repo root.

---

*Last updated for: single edit card (PATCH), preview PATCH, cancel recall + instruction repost, clear-draft repost flag.*
