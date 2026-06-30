# Jenkins pipeline — setup handoff / progress

Working doc to continue the Jenkins CI/CD setup. Read top-to-bottom; the **Current
blocker** and **Next steps** sections are where to resume.

---

## ⚠️ SECURITY — do this first
A GitHub Personal Access Token (`ghp_...`) was pasted into a chat in plaintext and is
considered **leaked**. **Revoke it now**: GitHub → Settings → Developer settings →
Personal access tokens → Tokens (classic) → delete it. Generate a fresh one only when
needed, and only paste it into the Jenkins **Credentials** field — never into chat,
code, or commits.

---

## Goal
One-click pipeline: develop in the **dev** repo, click a Jenkins build, it **tests** the
code, and on approval **promotes** (merges) the changes into the **prod** repo.

- **DEV repo:**  `https://github.com/Renzel025/lark-ops-ai-dev.git`  (source of truth)
- **PROD repo:** `https://github.com/Renzel025/lark-ops-ai.git`     (promotion target)
- **Direction:** dev → prod
- **Jenkins:** existing server at `https://ose-jenkinsaliyun.bewen.me/` (Aliyun **ECS, CentOS Linux**)

## Repo state (captured at setup)
- dev is **67 commits ahead** of prod.
- prod has **22 commits dev does NOT have** (real features: VC OAuth, recording fanout
  card, "manual P0 gets green Build overview" fix, etc.). They share history (merge-base exists).
- **Decision: MERGE** (keep both) — prod keeps its 22 commits and gains dev's 67. Nothing discarded.
- Because of the divergence, the **first** promotion may hit merge conflicts.

---

## What's already built (committed & pushed to dev `main`)
| File | Purpose |
|------|---------|
| `Jenkinsfile` (repo root) | The pipeline (declarative). dev → test → approve → merge into prod. |
| `requirements-dev.txt` | CI tooling: `ruff`, `pyright`. |
| `docs/CI_JENKINS_PIPELINE.md` | Full pipeline reference + Jenkins setup. |
| `docs/JENKINS_SETUP_HANDOFF.md` | This handoff doc. |

### Pipeline stages (in `Jenkinsfile`)
1. **Checkout dev** — Jenkins clones the dev repo (via job SCM config).
2. **Setup python** — builds `.venv`, installs `p0_logic/requirements.txt` + `requirements-dev.txt`.
3. **Tests** (parallel):
   - Compile / syntax — **HARD gate**.
   - Ruff real-errors (`E9,F63,F7,F82`) — **HARD gate**.
   - Ruff full lint — info (gate if `STRICT_LINT=true`).
   - Pyright type-check — info (gate if `STRICT_TYPECHECK=true`).
4. **Live Lark smoke tests** — runs `test_*.py` **dry-run / read-only** (no `--post`, nothing
   sent to groups). Skippable via `RUN_LIVE_LARK_TESTS=false`. **This stage is the only reason
   the 6 Lark secret credentials are needed.**
5. **Approve promotion** — pipeline pauses; a human clicks "Promote to prod".
6. **Promote dev → prod** — merges the tested commit into prod's branch and pushes.

### Parameters (set at "Build with Parameters")
- `DEV_BRANCH` (default `main`)
- `PROD_REPO_URL` (default = real prod URL above)
- `PROD_BRANCH` (default `main`)
- `RUN_LIVE_LARK_TESTS` (default true) — uncheck to skip live tests (no Lark secrets needed).
- `STRICT_TYPECHECK` / `STRICT_LINT` (default false) — flip to make those hard gates.
- `ON_CONFLICT` = `fail` (default, stop & let human resolve) | `prefer-dev` (auto-resolve to dev).

### Notes on design choices
- Pyright + full-lint are **informational by default** because making them hard gates would fail
  every build on day one from pre-existing type/style noise. Clean code first, then enable strict.
- CentOS 7 ships Python 3.6, but `pyrightconfig.json` targets 3.8 and pyright needs ≥3.7. **Verify
  `python3 --version` on the ECS is ≥3.8**; if it's 3.6, install python3.8 and point the venv at it.

---

## CURRENT BLOCKER 🛑
Creating the Jenkins job, entering the dev repo URL gives:
```
Failed to connect to repository : Error performing git command:
git ls-remote -h https://github.com/Renzel025/lark-ops-ai-dev.git HEAD
```
Key fact: **the dev repo is PUBLIC**, so this is almost certainly **NOT an auth/credential
problem** — a public repo needs no token to clone. Most likely cause: **Aliyun mainland ECS
cannot reach `github.com`** (common China-region network block).

### Diagnose (safe, read-only — changes nothing, does not affect other pipelines)
SSH into the Jenkins ECS and run:
```bash
git ls-remote https://github.com/Renzel025/lark-ops-ai-dev.git HEAD
```
- **Hangs / times out** → network block from Aliyun → GitHub. Fix = mirror to a China-accessible
  host (see below).
- **Prints a list of refs** → network is fine → the Jenkins error was a job-config glitch
  (re-select "Git", re-enter URL, save; check Manage Jenkins → Tools → Git path).
- **Asks for username/password** → unexpected for a public repo, but then add a GitHub PAT
  credential (username `Renzel025` + a NEW token as password).

Also useful: check whether any existing Jenkins job already pulls from github.com successfully.
If yes → network is fine globally → it's this job's config. If all other jobs use Gitee/internal
hosts → confirms GitHub is not reachable.

### Likely fix: mirror to Gitee (码云) — standard Aliyun solution
1. gitee.com → "+ → 从 GitHub/GitLab 导入仓库 (Import repository)".
2. Import both `lark-ops-ai-dev` and `lark-ops-ai`. Enable auto-sync from GitHub if offered.
3. In Jenkins job, use the **Gitee URLs** instead of GitHub for both the job SCM and the
   `PROD_REPO_URL` parameter (+ `prod-repo-push` credential pointing at Gitee).
Alternative: configure a proxy/DNS on the ECS so it can reach github.com directly (more involved).

---

## Jenkins job setup (once the repo is reachable)
Minimum required = **2 things**: create the job + one push credential. Everything else is optional.

1. **New Item** → name `lark-ops-promote` → **Pipeline** → OK.
2. **Pipeline** section:
   - Definition: **Pipeline script from SCM**
   - SCM: **Git**
   - Repository URL: dev repo (GitHub, or Gitee mirror if using that)
   - Credentials: needed only if the source host requires auth (public GitHub = none)
   - Branch: `*/main`
   - Script Path: `Jenkinsfile`
3. **Save** → **Build with Parameters** to run.

### Credentials (Manage Jenkins → Credentials → Global). IDs must match the `Jenkinsfile` exactly:
| ID | Kind | Needed for | Required? |
|----|------|-----------|-----------|
| `prod-repo-push` | Username+token (or SSH) | Pushing the merge to the PROD repo | **Yes** |
| `lark-app-id` | Secret text | Live Lark smoke tests | Only if `RUN_LIVE_LARK_TESTS=true` |
| `lark-app-secret` | Secret text | " | " |
| `lark-encrypt-key` | Secret text | " | " |
| `incident-group-ids` | Secret text (a DEV `oc_...` group) | " | " |
| `groq-api-key` | Secret text | " | " |
| `anthropic-api-key` | Secret text | " (optional script) | " |

> To start lean: uncheck `RUN_LIVE_LARK_TESTS` and you only need `prod-repo-push`. Add the
> 6 Lark secrets later when you want live testing.

---

## How it runs (answering "does it test automatically?")
- You click **Build with Parameters** once → **all test stages run automatically** in order.
- If any test **fails**, the pipeline **stops** and never promotes — broken code can't reach prod.
- It then **pauses** for a human to click **Promote to prod** (this approval gate can be removed
  if you want fully-automatic promotion on green).
- It does **not** currently auto-trigger on push to dev — you start it manually. A push trigger
  can be added later (GitHub/Gitee webhook or SCM polling).

---

## Next steps (resume here)
1. **Revoke the leaked GitHub token** (top of this doc).
2. On the Jenkins ECS, run the `git ls-remote` diagnostic → determine network vs config.
3. If network-blocked → mirror both repos to Gitee, use Gitee URLs in the job + `PROD_REPO_URL`.
4. Verify `python3 --version` on the ECS is ≥3.8 (else install python3.8).
5. Create the `prod-repo-push` credential (new token, never pasted in chat).
6. Decide lean vs full: skip live Lark tests for now (uncheck `RUN_LIVE_LARK_TESTS`) or add the
   6 Lark secrets.
7. First run: expect a possible merge conflict (dev 67 vs prod 22). Either resolve once by hand
   or run with `ON_CONFLICT=prefer-dev`.

## Open questions
- Do other Jenkins jobs successfully pull from github.com? (decides network vs config)
- Keep the manual "Promote" approval gate, or auto-promote when tests pass?
- Auto-trigger builds on push to dev, or keep manual "click Build"?
- Start lean (no live Lark tests) or wire all 6 Lark secrets now?
