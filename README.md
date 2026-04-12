# lark-ops-ai

Single runnable project: **main.py** + **lark_logic.py** + **p0_logic** (refactored package). No extra wiring — imports are already correct.

## Layout

```
lark-ops-ai/
├── main.py           # FastAPI webhook, VC events, DM, card actions
├── lark_logic.py      # P0/P1 triggers, wiki routing, process_message
├── wiki_ai_logic.py   # Wiki/doc + Groq answers
├── p0_logic/          # P0 session, drafts, cards (package)
└── README.md
```

## Run

```bash
cd /Users/slphc/lark-ops-ai
pip install -r p0_logic/requirements.txt
pip install fastapi uvicorn lark-oapi pycryptodome
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Env

- `LARK_APP_ID`, `LARK_APP_SECRET`, `LARK_ENCRYPT_KEY`
- `GROQ_API_KEY`
- `INCIDENT_GROUP_ID`, `WIKI_GROUP_CHAT_ID` (optional)
- `WIKI_DOC_TOKEN` (optional, for wiki_ai_logic)
- Other p0_logic vars: see `p0_logic/README.md`

## Do you need to “connect” anything?

No. **main.py** and **lark_logic.py** already import from **p0_logic**; the package is in this folder, so nothing else to connect.

## Slack Huddle (`scripts/`)

Headed Chrome + Playwright automation (same layout as **p1-p0-automation**): VNC on EC2, `.env`, `run_slack_huddle.py`, optional systemd `scripts/aws/slack-huddle.service.example`.

- Copy **`scripts/env.ubuntu.paste.example`** → repo root **`.env`** and edit paths / `SLACK_CHANNEL_URL`.
- Install: **`sudo python3 scripts/install_slack_huddle_deps.py`** (or `pip install -r scripts/requirements-huddle.txt` in a venv).
- Login once: **`scripts/slack_open_login_browser.py`** with `DISPLAY` set.
- Run: **`.venv/bin/python scripts/run_slack_huddle.py`**
