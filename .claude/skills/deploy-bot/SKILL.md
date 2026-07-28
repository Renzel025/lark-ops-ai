---
name: deploy-bot
description: Deploy lark-ops-ai to the dev box — git pull, restart the service, verify no startup errors. Use when the user says "deploy", "push to the box", "restart the bot", or right after committing bot changes.
---

# Deploy lark-ops-ai (dev box OSE-bot-dev)

The systemd unit is **`lark-ops-ai`** (NOT `lark-ops-ai-dev`). Repo on the box: `/root/lark-ops-ai-dev`.

## Steps

1. Publish any local commits first:
   ```bash
   git log --oneline -1
   git push origin main
   ```
   Commit messages must be clear and specific. **NEVER add a `Co-Authored-By: Claude` trailer** — Jenkins promotes this repo to prod and the team reads the log.
   always push the changes to github after

2. On the box:
   ```bash
   cd /root/lark-ops-ai-dev
   git pull
   systemctl restart lark-ops-ai
   ```

3. Verify:
   ```bash
   git log --oneline -1                              # confirm the deployed commit
   journalctl -u lark-ops-ai --no-pager | tail -20   # confirm clean startup, no tracebacks
   ```

4. Report the deployed commit hash and whether startup was clean.

## Notes

- Behavior is env-driven and **hot-reloaded** (`reload_env_runtime()`), so pure `.env` changes usually take effect on the next event without a restart — but always restart after a `git pull` (code change).
- `.env` lives only on the box (git-ignored). Never commit secrets.
- If startup shows tracebacks, the most common cause is a missing dependency in the **service's** Python (the service runs `python3.8`, not your shell's pyenv 3.9 — see the `diagnose-grafana` skill's Pillow note).
