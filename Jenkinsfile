// ============================================================================
//  lark-ops-ai-dev  —  Jenkins pipeline:  DEV repo  ->  test  ->  promote to PROD
// ----------------------------------------------------------------------------
//  Flow:
//    1. Jenkins checks out the DEV repo (this repo) on the chosen branch.
//    2. Builds a Python venv and installs deps + dev tools.
//    3. TEST STAGES (run in parallel where safe):
//         - Compile / syntax       (HARD gate — real breakage)
//         - Ruff "real errors"      (HARD gate — undefined names, syntax)
//         - Ruff full lint          (informational by default)
//         - Pyright type-check      (informational unless STRICT_TYPECHECK=true)
//         - Live Lark smoke tests   (dry-run, read-only; gate, skippable)
//    4. Manual approval ("Promote dev -> prod?").
//    5. Push the tested commit to the PROD repo's target branch.
//
//  Trigger: run manually from Jenkins ("Build with Parameters") = your
//  "click the pipeline" button. (You can also wire an SCM/webhook trigger later.)
//
//  PREREQUISITES in Jenkins (see docs/CI_JENKINS_PIPELINE.md):
//    - Credentials:
//        prod-repo-push      : Git creds (username+token or SSH) that can PUSH to PROD repo
//        lark-app-id         : Secret text  (DEV Lark app id)
//        lark-app-secret     : Secret text
//        lark-encrypt-key    : Secret text
//        incident-group-ids  : Secret text  (a DEV oc_... group)
//        groq-api-key        : Secret text
//        anthropic-api-key   : Secret text  (optional; issue_watch test)
//    - Python 3.8+ available on the agent.
// ============================================================================

pipeline {
  agent any

  parameters {
    string(name: 'DEV_BRANCH',  defaultValue: 'main',
           description: 'Branch in the DEV repo to test and promote.')
    string(name: 'PROD_REPO_URL', defaultValue: 'https://github.com/Renzel025/lark-ops-ai.git',
           description: 'HTTPS/SSH URL of the PROD repo to push to.')
    string(name: 'PROD_BRANCH', defaultValue: 'main',
           description: 'Branch in the PROD repo to update.')
    booleanParam(name: 'RUN_LIVE_LARK_TESTS', defaultValue: true,
           description: 'Run dry-run smoke tests against live Lark (read-only). Uncheck to skip.')
    booleanParam(name: 'STRICT_TYPECHECK', defaultValue: false,
           description: 'Make Pyright a HARD gate (fails build on any type error). Leave off until code is clean.')
    booleanParam(name: 'STRICT_LINT', defaultValue: false,
           description: 'Make the FULL ruff lint a HARD gate. Leave off until code is clean.')
    choice(name: 'ON_CONFLICT', choices: ['fail', 'prefer-dev'],
           description: 'Merge conflict policy when promoting. "fail" = stop and let a human resolve (safe). "prefer-dev" = auto-resolve conflicts in favor of the dev change.')
  }

  options {
    timestamps()
    timeout(time: 30, unit: 'MINUTES')
    disableConcurrentBuilds()
  }

  environment {
    VENV = "${WORKSPACE}/.venv"
    PATH = "${WORKSPACE}/.venv/bin:${PATH}"
    PYTHONDONTWRITEBYTECODE = "1"
  }

  stages {

    stage('Checkout dev') {
      steps {
        // Uses the SCM configured on the Jenkins job (the DEV repo).
        // If you parameterize the branch, make sure the job SCM uses ${DEV_BRANCH}.
        checkout scm
        sh 'git rev-parse HEAD > .git_sha && echo "Commit under test: $(cat .git_sha)"'
      }
    }

    stage('Setup python') {
      steps {
        sh '''
          set -eu
          python3.9 --version
          python3.9 -m venv "$VENV"
          . "$VENV/bin/activate"
          python -m pip install --upgrade pip wheel
          pip install -r p0_logic/requirements.txt
          pip install -r requirements-dev.txt
          echo "Installed:"; pip list | grep -Ei "ruff|pyright|requests|playwright" || true
        '''
      }
    }

    stage('Tests') {
      parallel {

        stage('Compile / syntax (gate)') {
          steps {
            sh '''
              set -eu
              . "$VENV/bin/activate"
              # Compile every source file we ship; fails on any SyntaxError.
              python -m compileall -q main.py lark_logic.py wiki_ai_logic.py p0_logic features
            '''
          }
        }

        stage('Ruff real-errors (gate)') {
          steps {
            sh '''
              set -eu
              . "$VENV/bin/activate"
              # Only the rules that flag genuine bugs (undefined names, broken syntax,
              # f-string mistakes). These almost never false-positive.
              ruff check --select E9,F63,F7,F82 .
            '''
          }
        }

        stage('Ruff full lint (info)') {
          steps {
            sh '''
              set -eu
              . "$VENV/bin/activate"
              if [ "${STRICT_LINT}" = "true" ]; then
                ruff check .
              else
                echo "(informational) full ruff lint — not failing the build:"
                ruff check . || true
              fi
            '''
          }
        }

        stage('Pyright type-check') {
          steps {
            sh '''
              set -eu
              . "$VENV/bin/activate"
              if [ "${STRICT_TYPECHECK}" = "true" ]; then
                pyright
              else
                echo "(informational) pyright — not failing the build:"
                pyright || true
              fi
            '''
          }
        }
      }
    }

    stage('Live Lark smoke tests') {
      when { expression { return params.RUN_LIVE_LARK_TESTS } }
      steps {
        withCredentials([
          string(credentialsId: 'lark-app-id',        variable: 'LARK_APP_ID'),
          string(credentialsId: 'lark-app-secret',    variable: 'LARK_APP_SECRET'),
          string(credentialsId: 'lark-encrypt-key',   variable: 'LARK_ENCRYPT_KEY'),
          string(credentialsId: 'incident-group-ids', variable: 'INCIDENT_GROUP_IDS'),
          string(credentialsId: 'groq-api-key',       variable: 'GROQ_API_KEY'),
          string(credentialsId: 'anthropic-api-key',  variable: 'ANTHROPIC_API_KEY'),
        ]) {
          sh '''
            set -eu
            . "$VENV/bin/activate"
            echo "Running DRY-RUN (read-only) smoke tests — no --post, nothing is sent to groups."
            # Each exits non-zero on failure -> fails the stage -> blocks promotion.
            python3 features/issue_watch/scripts/test_once.py "website is loading slowly"
            python3 features/overview/scripts/test_bitable_once.py
            python3 features/monitoring/scripts/test_monitoring_once.py --kind duty
          '''
        }
      }
    }

    stage('Approve promotion') {
      steps {
        // Pauses the pipeline; a human clicks "Promote" to continue.
        input message: "All tests passed. Promote ${params.DEV_BRANCH} -> PROD (${params.PROD_BRANCH})?",
              ok: "Promote to prod"
      }
    }

    stage('Promote dev -> prod') {
      steps {
        withCredentials([gitUsernamePassword(credentialsId: 'prod-repo-push', gitToolName: 'Default')]) {
          sh '''
            set -eu
            git config user.email "jenkins@ci.local"
            git config user.name  "Jenkins CI"

            SHA="$(cat .git_sha)"
            git remote remove prod 2>/dev/null || true
            git remote add prod "${PROD_REPO_URL}"
            git fetch prod "${PROD_BRANCH}" --no-tags

            # Start from prod's current branch, then MERGE the tested dev commit into it.
            # Prod keeps its own commits; dev's changes are merged on top. Nothing is discarded.
            git checkout -B _promote "prod/${PROD_BRANCH}"

            if [ "${ON_CONFLICT}" = "prefer-dev" ]; then
              echo "Merging dev ${SHA} into prod/${PROD_BRANCH} (conflicts auto-resolved in favor of DEV)..."
              # '-X theirs': on a conflicting hunk, take the side being merged in (= dev).
              git merge --no-ff -X theirs "${SHA}" \
                -m "Promote dev -> prod (auto-resolve conflicts to dev): ${SHA}"
            else
              echo "Merging dev ${SHA} into prod/${PROD_BRANCH} (will FAIL on conflict for manual resolution)..."
              if ! git merge --no-ff "${SHA}" -m "Promote dev -> prod: ${SHA}"; then
                echo "----------------------------------------------------------------"
                echo "MERGE CONFLICT — nothing pushed to prod."
                echo "Files in conflict:"
                git diff --name-only --diff-filter=U || true
                echo "Resolve locally (merge dev into prod, commit, push), or re-run this"
                echo "job with ON_CONFLICT=prefer-dev to auto-resolve conflicts to dev."
                echo "----------------------------------------------------------------"
                git merge --abort || true
                exit 1
              fi
            fi

            echo "Pushing merged result to prod/${PROD_BRANCH}"
            git push prod "_promote:refs/heads/${PROD_BRANCH}"
          '''
        }
      }
    }
  }

  post {
    success { echo "✅ Promotion complete: ${params.DEV_BRANCH} -> prod/${params.PROD_BRANCH}" }
    failure { echo "❌ Pipeline failed — nothing was promoted to prod." }
    always  { sh 'rm -rf "$VENV" || true' }
  }
}
