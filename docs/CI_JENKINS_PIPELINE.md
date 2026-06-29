# Jenkins pipeline — DEV → test → promote to PROD

"Click a button, it tests the dev code, and if everything passes it promotes the
exact tested commit to the prod repo." That button is **Build with Parameters** on
the Jenkins job. The pipeline itself lives in the repo root: [`Jenkinsfile`](../Jenkinsfile).

## What it does (stages)

| Stage | Hard gate? | Notes |
|-------|-----------|-------|
| Checkout dev | — | Pulls the dev repo branch you chose. |
| Setup python | — | Builds a `.venv`, installs `p0_logic/requirements.txt` + `requirements-dev.txt`. |
| Compile / syntax | ✅ blocks | `compileall` — fails on any `SyntaxError`. |
| Ruff real-errors | ✅ blocks | Only bug rules (`E9,F63,F7,F82`: undefined names, broken f-strings). |
| Ruff full lint | ⚪ info | Style warnings. Flip to gate with `STRICT_LINT=true`. |
| Pyright type-check | ⚪ info | Flip to gate with `STRICT_TYPECHECK=true`. |
| Live Lark smoke tests | ✅ blocks (skippable) | Runs `test_*.py` **dry-run / read-only** — nothing is posted to groups. |
| Approve promotion | — | Pauses for a human to click **Promote**. |
| Promote dev → prod | — | Pushes the tested commit to the prod repo branch. |

### Why pyright / full-lint are "info" by default
Turning them into hard gates on an existing codebase would fail **every** build on
day one due to pre-existing type/style noise. They still run and print findings.
Clean the code up, then set `STRICT_TYPECHECK=true` / `STRICT_LINT=true` to enforce.

## One-time Jenkins setup

### 1. Create the job
- New Item → **Pipeline** (or **Multibranch** if you prefer per-branch).
- **Pipeline → Definition: "Pipeline script from SCM"** → Git → your **DEV** repo URL
  (`https://github.com/Renzel025/lark-ops-ai-dev.git`) → Script Path: `Jenkinsfile`.
- This makes Jenkins read the `Jenkinsfile` from the repo.

### 2. Add credentials (Manage Jenkins → Credentials → add **Global**)

| ID (must match exactly) | Kind | Value |
|--------------------------|------|-------|
| `prod-repo-push` | Username with password **or** SSH key | Account/token that can **push** to the PROD repo |
| `lark-app-id` | Secret text | DEV Lark app id (`cli_...`) |
| `lark-app-secret` | Secret text | DEV Lark app secret |
| `lark-encrypt-key` | Secret text | DEV `LARK_ENCRYPT_KEY` |
| `incident-group-ids` | Secret text | A **dev** group `oc_...` |
| `groq-api-key` | Secret text | Groq key |
| `anthropic-api-key` | Secret text | Anthropic key (optional) |

> Use **dev** Lark credentials here so smoke tests touch the dev app, never prod.

### 3. First run
- Open the job → **Build with Parameters** → set:
  - `DEV_BRANCH` (default `main`)
  - `PROD_REPO_URL` — **edit this to your real prod repo URL** (the default is a placeholder).
  - `PROD_BRANCH` (default `main`)
  - `RUN_LIVE_LARK_TESTS` — leave on, or uncheck for a code-only run.
- Click **Build**. When tests pass it pauses at **Approve promotion** → click **Promote to prod**.

## Notes & gotchas
- **Promotion is a MERGE** of the tested dev commit into prod's branch — prod keeps its own
  commits and gains dev's. Nothing is discarded. (dev=`lark-ops-ai-dev`, prod=`lark-ops-ai`.)
- **Conflict policy** is the `ON_CONFLICT` parameter:
  - `fail` (default) — stop and print the conflicting files; nothing is pushed. Resolve locally, or
  - `prefer-dev` — auto-resolve conflicting hunks in favor of the dev change, then push.
- As of setup, dev was 67 commits ahead and prod had 22 commits dev didn't — so expect the **first**
  merge to possibly hit conflicts. Either resolve once by hand, or run with `ON_CONFLICT=prefer-dev`.
- The smoke tests run **without `--post`**, so they validate config + API auth + classification
  logic without sending any message to a Lark group.
- Python 3.8 is assumed (matches `pyrightconfig.json`). Ensure the agent has `python3` 3.8+.
- Playwright browser binaries are **not** installed (only the pip package) — the screenshot
  feature's browser isn't exercised in CI. Add `playwright install --with-deps chromium` to the
  Setup stage if you want that too.
