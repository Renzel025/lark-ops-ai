# CI/CD

Jenkins pipelines and deploy scripts live in a **separate repository**:

**`lark-ops-ai-jenkins`** (sibling folder or own git remote)

- `Jenkinsfile`, `scripts/deploy-remote.sh`, release profiles
- Start with **`AGENTS.md`** in that repo for AI agents
- Human setup: `docs/SETUP.md` there

This app repo (`lark-ops-ai-dev`) contains only application code. Deploy is:

```bash
# Via Jenkins (recommended) — job points at lark-ops-ai-jenkins repo

# Manual — from lark-ops-ai-jenkins clone:
bash scripts/deploy-remote.sh --app-dir /root/lark-ops-ai-dev --branch develop
```

`.env` on each server is **not** managed by Jenkins.
