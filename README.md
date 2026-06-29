# lark-ops-ai-dev

Lark/Feishu bot for P0/P1 incidents: VC meetings, DM overview builder, optional Issue Watch, Grafana screenshots, recording fan-out.

## Layout

```
├── main.py              # FastAPI webhook
├── lark_logic.py        # Message routing (incident / wiki / DM)
├── p0_logic/            # Shared core (config, cards, handlers, Lark client, AI clients)
├── features/            # Feature modules + manual scripts (see features/README.md)
│   ├── screenshot/
│   ├── overview/
│   ├── recording/
│   ├── issue_watch/
│   └── session/
├── scripts/             # Dev/deploy helpers (run_dev.sh, nginx/, systemd/)
└── docs/                # Operator + deploy + IT checklist
```

## Run locally

```bash
pip install -r p0_logic/requirements.txt
pip install fastapi uvicorn lark-oapi pycryptodome
cp env.dev.example .env.dev
ENV_PROFILE=dev bash scripts/run_dev.sh
```

## Docs

| Doc | Purpose |
|-----|---------|
| [docs/P0_P1_OPERATOR_GUIDE.md](docs/P0_P1_OPERATOR_GUIDE.md) | How to use the bot (operators) |
| [docs/ENV_FEATURES_TOGGLES.md](docs/ENV_FEATURES_TOGGLES.md) | Env ON/OFF per feature + prod vs dev |
| [docs/MANUAL_TEST_COMMANDS.md](docs/MANUAL_TEST_COMMANDS.md) | Python/shell commands to test each feature |
| [docs/IT_LARK_DEV_APP_CHECKLIST.md](docs/IT_LARK_DEV_APP_CHECKLIST.md) | Lark app scopes & events |
| [docs/DEPLOY.md](docs/DEPLOY.md) | Server deploy & restart |
| [features/README.md](features/README.md) | Feature folders + test scripts |
| [env.example](env.example) | Full env reference |

## Manual test scripts

See **[docs/MANUAL_TEST_COMMANDS.md](docs/MANUAL_TEST_COMMANDS.md)** for the full list. Quick examples:

```bash
python3 features/overview/scripts/test_bitable_once.py --post --chat-id=oc_YOUR_GROUP
python3 features/recording/scripts/post_card_once.py
python3 features/screenshot/scripts/grafana_screenshot_run_once.py --post-lark
python3 features/issue_watch/scripts/test_once.py "website loading"
bash features/session/scripts/diagnose_p0_incident_logs.sh
```

## Env (minimum)

`LARK_APP_ID`, `LARK_APP_SECRET`, `LARK_ENCRYPT_KEY`, `INCIDENT_GROUP_IDS`, `GROQ_API_KEY` — see `env.example`.
