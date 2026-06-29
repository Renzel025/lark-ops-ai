# Deploy

## Flow

```
Lark → HTTPS /lark/webhook → nginx → uvicorn (main.py) → lark_logic + features/*
                              ↑
                         systemd keeps process alive
```

Outbound: Lark Open API (messages, VC) + Groq/Claude when AI is enabled. `.env` on the server is not in git.

## Server setup

```bash
cd /root/lark-ops-ai-dev   # or prod: /root/lark-ops-ai
git pull origin main
python3 -m venv .venv
source .venv/bin/activate
pip install -r p0_logic/requirements.txt
pip install fastapi uvicorn lark-oapi pycryptodome requests python-dotenv

cp env.example .env   # first time only — fill LARK_*, GROQ_*, group IDs
systemctl restart lark-ops-ai
journalctl -u lark-ops-ai -n 50 --no-pager
```

Webhook URL in Lark console: `https://<your-domain>/lark/webhook`

## systemd (example)

```ini
[Service]
WorkingDirectory=/root/lark-ops-ai-dev
EnvironmentFile=/root/lark-ops-ai-dev/.env
ExecStart=/root/lark-ops-ai-dev/.venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8000
Restart=always
```

Staging examples: `scripts/nginx/`, `scripts/systemd/`

## CI/CD

Jenkins pipelines live in the separate **`lark-ops-ai-jenkins`** repo (`Jenkinsfile`, `scripts/deploy-remote.sh`). Manual deploy = `git pull` + restart above.

## Lark app setup

See [IT_LARK_DEV_APP_CHECKLIST.md](IT_LARK_DEV_APP_CHECKLIST.md) for scopes and event subscriptions.
