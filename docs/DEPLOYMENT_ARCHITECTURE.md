# Lark Ops AI — deployment architecture

EN: End-to-end picture: how the service is exposed on your domain with TLS, how Lark reaches it, how the app runs on the server, and how Groq AI is called outbound for LLM/vision.

TL (Tagalog): Buong setup: Lark → HTTPS domain → nginx → uvicorn → `main.py`; plus Groq (outbound API) para sa overview/OCR/wiki.


---

## Figure 1 — Logical deployment diagram

Caption: End-to-end path from Lark events to your VPS (nginx → app), plus outbound calls your app makes to Groq (AI) and Lark Open Platform (tokens, chat, VC).

### How to read this (for anyone)

| | |
|--|--|
| EN | The diagram has two kinds of arrows: (1) Inbound — Lark’s cloud sends HTTPS webhooks to your domain only. (2) Outbound — After your Python code runs, it calls the internet to get a tenant token, send messages, reserve VC, and (for AI) call Groq. Those calls do not go through your domain name; they go from uvicorn → public APIs. |
| TL | May dalawang direksyon: Papasok = mula Lark cloud tungo sa nginx mo (HTTPS). Palabas = ang app mismo ang tatawag sa Groq at Lark API (hiwalay na HTTPS). Hindi dadaan ang Groq sa nginx mo. |

Open in any browser (zoom-friendly):

- SVG (static): [architecture-diagram.svg](architecture-diagram.svg)
- HTML (interactive — inbound / outbound / full): [diagram.html](diagram.html) (same `docs/` folder; uses Tailwind CDN)

![Figure 1 — Lark Ops AI deployment (logical): Lark cloud → TLS → nginx → uvicorn/FastAPI → outbound Groq + Lark APIs](architecture-diagram.svg)

If the image does not render, open `docs/architecture-diagram.svg` directly. / Kung hindi lumabas, buksan ang SVG file mismo.

Diagram version: Open `docs/diagram.html` in a browser (v3 — Lark Cloud, ECS Aliyun server, Groq + Claude + features).

---

### Step-by-step flow (matches the picture)

1. User / bot activity in Lark — Someone sends a message, taps a card button, or joins a VC. Lark’s servers create an event.
2. Lark → your server (inbound) — Lark sends an HTTP POST to the URL you configured: `https://<YOUR_DOMAIN>/lark/webhook`. Traffic hits nginx on port 443 (TLS). Nothing in this step talks to Groq yet.
3. nginx → uvicorn — nginx forwards to `http://127.0.0.1:8000` (or similar). uvicorn runs your FastAPI app (`main.py`).
4. Your code runs — FastAPI handles `/lark/webhook`, then `lark_logic` / `p0_logic` decide what to do (P0/P1, DM overview, wiki, etc.).
5. Your server → internet (outbound) — The same process may call:
   - Lark Open Platform — tenant token, send/update messages, VC reserve/end, contact APIs.
   - Groq — only when AI is needed (overview text, vision/OCR on screenshots, wiki answers).
6. systemd (yellow box) — Not in the data path; it starts and restarts the uvicorn service so the app stays up after reboots or crashes.

TL (Tagalog, short):  
Una, may nangyari sa Lark. Pangalawa, nag-POST ang Lark sa webhook mo (HTTPS). Tatlo, nginx papunta sa uvicorn. Apat, tumatakbo ang Python. Lima, kung kailangan, tumatawag ang app sa Lark API at sa Groq. Ang systemd, taga-manage lang ng process.


---

### What each box means

| # | Box | What it is (EN) | Ano ito (TL) |
|---|---|-----------------|--------------|
| A | Lark / Feishu cloud | Lark’s servers: messaging, interactive cards, VC events. They push webhooks to you. | Serbisyo ng Lark; sila ang nagpapadala ng event sa URL mo. |
| B | TLS :443 | Encrypted public web traffic. Your certificate terminates at nginx. | Naka-encrypt na HTTPS papasok sa server. |
| C | nginx | Reverse proxy: SSL, optional body size limits, forwards to the app. | Proxy sa harap ng app; dito SSL. |
| D | uvicorn + FastAPI | Python ASGI server running `main:app`. This is where `/lark/webhook` is implemented. | Dito tumatakbo ang `main.py` at webhook route. |
| E | systemd | Linux service unit (`lark-ops-ai.service`): auto-start on boot, Restart=always. | Para auto-restart kung bumagsak ang app. |
| F | Groq Cloud | Separate AI provider. Your app calls `api.groq.com` when generating text/vision. | Hiwalay na AI; hindi bahagi ng Lark. |
| G | Lark Open Platform | Official Lark APIs: token, IM, VC, contacts — used outbound from your VPS. | Opisyal na API ng Lark para mag-reply at mag-VC. |

---

### Inbound vs outbound (important)

```text
INBOUND  (Lark → you)     :  Only path:  Internet → nginx:443 → uvicorn:8000 → your code
OUTBOUND (you → APIs)     :  From your code:  VPS → api.groq.com, open-sg.larksuite.com, etc.
```

Why it matters: Firewall rules must allow inbound 443 for Lark to reach you, and outbound HTTPS for your app to reach Groq and Lark — both are required for full features.

TL: Kailangan bukas ang 443 papasok (para sa webhook), at pahintulutan ang HTTPS palabas papuntang Groq at Lark.


---

## 1. Architecture (high level)

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Lark / Feishu cloud                                                     │
│  • im.message.receive_v1, card.action.trigger, vc.* events               │
│  • Outbound HTTPS POST to your public URL                                 │
└───────────────────────────────────┬─────────────────────────────────────┘
                                    │ TLS (443)
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  Your server (ECS Aliyun)                                                │
│  ┌──────────────┐    proxy_pass     ┌────────────────────────────────┐  │
│  │ nginx :443   │ ────────────────► │ uvicorn main:app :8000         │  │
│  │ SSL cert     │    (HTTP local)   │ FastAPI + p0_logic             │  │
│  └──────────────┘                   │ POST /lark/webhook             │  │
│         ▲                             │ outbound: Lark Open API + Groq │  │
│         │ systemd                     └───────────────┬──────────────┘  │
│  lark-ops-ai.service                                  │                  │
└───────────────────────────────────────────────────────┼──────────────────┘
                                                        │ HTTPS outbound
                                                        ▼
                                          ┌─────────────────────────────┐
                                          │ Groq Cloud                    │
                                          │ https://api.groq.com/openai/v1 │
                                          │ (chat + vision completions)   │
                                          └─────────────────────────────┘
```

What we built (repo):

| Piece | Role |
|-------|------|
| `main.py` | FastAPI app: decrypt/verify Lark payloads, route VC + IM + card actions |
| `lark_logic.py` | Group message routing (P0/P1 keywords, wiki, etc.) |
| `p0_logic/` | Sessions, VC reserve, cards, DM drafts/overview |
| `p0_logic/groq_client.py` | Groq HTTP client: chat + vision (`Bearer GROQ_API_KEY`) |
| `wiki_ai_logic.py` | Optional wiki/doc Q&A (also uses `GROQ_API_KEY` when enabled) |

Public URL Lark must call: `https://<YOUR_DOMAIN>/lark/webhook`  
(Method: POST, same path as `@app.post("/lark/webhook")` in `main.py`.)

---

## 2. Groq AI (Groq Cloud)

EN: Groq is not inside nginx or Lark. Your app calls Groq outbound from the VPS whenever it needs LLM/vision (overview text, OCR on DM screenshots, issue lines, translation, etc.).

TL: Ang Groq = hiwalay na cloud. Mula sa server mo, HTTPS outbound papuntang `api.groq.com` — hindi dadaan sa domain mo o sa nginx.


### 2.1 Flow

1. User / Lark triggers work (e.g. DM with screenshots, Build overview, wiki question).
2. `p0_logic` (or `wiki_ai_logic.py`) calls Groq: `POST https://api.groq.com/openai/v1/chat/completions` with `Authorization: Bearer <GROQ_API_KEY>`.
3. Text (and optional images for vision) go to Groq; the model response is formatted into Lark cards / messages.

### 2.2 Environment variables

| Variable | Required | Notes |
|----------|----------|--------|
| `GROQ_API_KEY` | Yes for AI features | Create in [Groq Console](https://console.groq.com/) — treat like a password. |
| `GROQ_MODEL` | No | Default in `p0_logic/config.py`: `llama-3.1-8b-instant`. |
| `GROQ_VISION_MODEL` | No | Default: `llama-3.2-11b-vision-preview` (screenshots in DM). |

Base URL is fixed in code as `https://api.groq.com/openai/v1` (`GROQ_BASE` in `p0_logic/config.py`). `main.py` also reads `GROQ_API_KEY` for consistency with the rest of the app.

### 2.3 Network / firewall

- Inbound: Lark hits only `https://<YOUR_DOMAIN>/lark/webhook` (nginx → uvicorn).
- Outbound: The server must reach `api.groq.com:443` (and `open.larksuite.com` / `open-sg.larksuite.com` as already used for tokens and IM). If the ECS security group blocks all egress except Lark, add HTTPS egress to the internet or to Groq’s endpoints.

### 2.4 If `GROQ_API_KEY` is missing

Groq-backed steps degrade or skip (e.g. no OCR / no generated overview text). Lark webhooks and VC flows can still run; only AI-generated parts fail silently or with logs depending on the code path.

### 2.5 Troubleshooting (Groq)

| Symptom | Check |
|---------|--------|
| 401 / invalid API key | Rotate key in Groq console; update `.env`; `systemctl restart lark-ops-ai` |
| Rate limits / 429 | Groq tier limits; retry or upgrade plan |
| Timeouts | Increase request timeouts in `p0_logic/config.py` (`REQ_TIMEOUT`) if needed |
| Vision empty | Confirm `GROQ_VISION_MODEL` is a vision-capable model on Groq |

---

## 3. Prerequisites

- A Linux server (e.g. Alibaba Cloud ECS — typical hostname pattern).
- A DNS A record pointing `<YOUR_DOMAIN>` → server public IP.
- Ports 80 and 443 open on the security group / firewall (for HTTP challenge and HTTPS).
- Python 3.8+ on the server (match what you run in production; 3.9+ recommended per `p0_logic/README.md`).
- Lark custom app with event subscription and credentials (`LARK_APP_ID`, `LARK_APP_SECRET`, optional `LARK_ENCRYPT_KEY`).
- Groq account + `GROQ_API_KEY` if you use AI overview, OCR, wiki answers, translations (see §2).

---

## 4. Install the application

```bash
# Example paths — adjust to your layout
sudo mkdir -p /opt
sudo chown "$USER":"$USER" /opt   # or deploy as a dedicated user
cd /opt
git clone <YOUR_REPO_URL> lark-ops-ai
cd lark-ops-ai

python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r p0_logic/requirements.txt
pip install fastapi uvicorn lark-oapi pycryptodome requests python-dotenv
```

Create production env (do not commit secrets):

```bash
cp env.example .env
# Edit .env: LARK_*, GROQ_*, P0_*, group IDs, etc.
```

If you use `ENV_PATH` or load dotenv from the app, point it at this `.env` (see `p0_logic/config.py` for `ENV_PATH` behavior).

Smoke test (no nginx yet):

```bash
source .venv/bin/activate
set -a && source .env && set +a
uvicorn main:app --host 0.0.0.0 --port 8000
```

From another shell: `curl -sS http://127.0.0.1:8000/docs` (FastAPI docs) or POST a test payload (Lark will use the real challenge during verification).

---

## 5. systemd — keep uvicorn running

Create `/etc/systemd/system/lark-ops-ai.service` (adjust `User`, `WorkingDirectory`, and Python path):

```ini
[Unit]
Description=Lark Ops AI (FastAPI + uvicorn)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/lark-ops-ai
EnvironmentFile=/root/lark-ops-ai/.env
ExecStart=/root/lark-ops-ai/.venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable lark-ops-ai
sudo systemctl start lark-ops-ai
sudo systemctl status lark-ops-ai
journalctl -u lark-ops-ai -f
```

Note: Prefer a non-root service user and `WorkingDirectory` under `/opt/lark-ops-ai` in production.

---

## 6. nginx — reverse proxy + TLS termination

Install nginx (Debian/Ubuntu example):

```bash
sudo apt update
sudo apt install -y nginx
```

### 6.1 HTTP → HTTPS (after you have certificates)

Example server block — replace `lark.example.com` and upstream port if needed:

```nginx
# /etc/nginx/sites-available/lark-ops-ai
server {
    listen 80;
    listen [::]:80;
    server_name lark.example.com;
    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }
    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name lark.example.com;

    ssl_certificate     /etc/letsencrypt/live/lark.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/lark.example.com/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    # Lark can send larger JSON payloads; avoid 413
    client_max_body_size 20m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
```

Enable and test:

```bash
sudo ln -sf /etc/nginx/sites-available/lark-ops-ai /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

### 6.2 SSL certificates (Let’s Encrypt + certbot)

```bash
sudo apt install -y certbot python3-certbot-nginx
# First-time HTTP-01 with nginx plugin (needs port 80 reachable):
sudo certbot --nginx -d lark.example.com
```

Certbot can install the snippet above or you merge certificates into your own server block. Renewals are usually automatic via `certbot.timer`.

Webhook URL for Lark Developer Console: `https://lark.example.com/lark/webhook`

---

## 7. Lark Developer Console (must match this deployment)

1. Event subscription — Request URL = `https://<YOUR_DOMAIN>/lark/webhook`.
2. URL verification — App sends `type: url_verification` with `challenge`; `main.py` returns `{"challenge": "..."}` (works through nginx the same as direct uvicorn).
3. Encryption — If you enable encrypt on the subscription, set `LARK_ENCRYPT_KEY` in `.env` to match the console; `main.py` decrypts `encrypt` payloads.
4. Outbound IP — Some orgs allowlist Lark IPs; your server only needs inbound 443 from the internet (and 80 for ACME if used).

Subscribed event types (typical for this project):

- `im.message.receive_v1` (group + DM)
- `im.message.message_read_v1` (optional)
- `card.action.trigger`
- `vc.meeting.join_meeting_v1`, `vc.meeting.leave_meeting_v1`, `vc.meeting.meeting_ended_v1`, etc.

---

## 8. Security checklist

- [ ] TLS only on nginx; uvicorn bound to 127.0.0.1 if nginx is on the same host (optional hardening: `--host 127.0.0.1` + keep nginx as the only public listener).
- [ ] `.env` permissions `chmod 600`, not in git.
- [ ] Firewall: only 22 (or your SSH), 80, 443 as needed; close 8000 from the public internet if nginx proxies locally.
- [ ] Lark app secret rotation procedure documented.
- [ ] App availability in Lark includes every user who must receive bot DMs (otherwise API 230013 — “Bot has NO availability to this user”).
- [ ] `GROQ_API_KEY` in `.env` only; never commit; rotate if leaked.
- [ ] Optional: `P0_INCIDENT_GROUP_COMMAND_OPEN_IDS` — comma-separated `ou_` users allowed to cancel/end, cooldown reset, and use P1 control buttons (see `p0_logic/config.py`).

---

## 9. Operations

| Action | Command |
|--------|---------|
| Logs | `journalctl -u lark-ops-ai -f` |
| Restart app | `sudo systemctl restart lark-ops-ai` |
| Reload nginx | `sudo nginx -t && sudo systemctl reload nginx` |
| Deploy new code | `git pull`, `systemctl restart lark-ops-ai` |

If `git` reports dubious ownership, run once (as the deploy user):

`git config --global --add safe.directory /path/to/lark-ops-ai`

---

## 10. Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| Lark “URL verification failed” | Wrong URL path, TLS error, or firewall blocking 443 |
| 230013 on DM | User cannot use the bot (availability / permission); not nginx |
| 502 from nginx | uvicorn down or wrong `proxy_pass` port |
| Empty VC / token errors | Bad `LARK_APP_ID` / `LARK_APP_SECRET` or network to `open.larksuite.com` / `open-sg.larksuite.com` |
| Overview / OCR / wiki “empty” or errors | Missing or bad `GROQ_API_KEY`, blocked egress to `api.groq.com`, or rate limits — see §2 |

---

## 11. Related docs in this repo

| File | Content |
|------|---------|
| `README.md` | Local run and env overview |
| `env.example` | P0/Lark/Groq variables |
| `docs/ARCHITECTURE_AND_FLOW.md` | In-app module flow (webhook + session) |
| `docs/P0_P1_OPERATOR_GUIDE.md` | Operator-facing usage |
| `docs/HOW_IT_WORKS_AND_NAVIGATION.md` | Clicks and typing paths |
| `p0_logic/README.md` | Package modules |

---

Last updated: 2026-03 — includes Groq (§2); adjust domain, paths, and Python version to match your server.
