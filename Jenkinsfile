// ============================================================================
//  lark-ops-ai-dev  —  Jenkins pipeline:  DEV repo  ->  test  ->  promote to PROD
// ----------------------------------------------------------------------------
//  Flow:
//    1. Checkout DEV repo
//    2. Setup Python venv + install deps
//    3. Tests (syntax + ruff real errors only)
//    4. Merge dev -> prod GitHub repo
//    5. Deploy to dev server (git pull + restart)
//    6. Approve prod deploy (24hr window)
//    7. Deploy to prod server (git pull + restart)
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
    booleanParam(name: 'RUN_LIVE_LARK_TESTS', defaultValue: false,
           description: 'Run dry-run smoke tests against live Lark (read-only). Uncheck to skip.')
    choice(name: 'ON_CONFLICT', choices: ['fail', 'prefer-dev'],
           description: 'Merge conflict policy. "fail" = stop for manual resolve. "prefer-dev" = auto-resolve to dev.')
  }

  options {
    timestamps()
    timeout(time: 25, unit: 'HOURS')
    disableConcurrentBuilds()
  }

  environment {
    VENV                     = "${WORKSPACE}/.venv"
    PATH                     = "${WORKSPACE}/.venv/bin:${PATH}"
    PYTHONDONTWRITEBYTECODE  = "1"
    DEV_SERVER_IP            = "47.84.198.163"
    DEV_SERVER_PATH          = "/root/lark-ops-ai-dev"
    DEV_SERVICE              = "lark-ops-ai"
    PROD_SERVER_IP           = "8.219.139.155"
    PROD_SERVER_PATH         = "/root/lark-ops-ai"
    PROD_SERVICE             = "lark-ops-ai"
  }

  stages {

    stage('Checkout dev') {
      steps {
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
              python -m compileall -q main.py lark_logic.py wiki_ai_logic.py p0_logic features
            '''
          }
        }

        stage('Ruff real-errors (gate)') {
          steps {
            sh '''
              set -eu
              . "$VENV/bin/activate"
              ruff check --select E9,F63,F7,F82 .
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
            echo "Running DRY-RUN smoke tests..."
            python3 features/issue_watch/scripts/test_once.py "website is loading slowly"
            python3 features/overview/scripts/test_bitable_once.py
            python3 features/monitoring/scripts/test_monitoring_once.py --kind duty
          '''
        }
      }
    }

    stage('Promote dev -> prod repo') {
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
            git checkout -B _promote FETCH_HEAD

            if [ "${ON_CONFLICT}" = "prefer-dev" ]; then
              git merge --no-ff -X theirs "${SHA}" \
                -m "Promote dev -> prod (auto-resolve to dev): ${SHA}"
            else
              if ! git merge --no-ff "${SHA}" -m "Promote dev -> prod: ${SHA}"; then
                echo "MERGE CONFLICT — nothing pushed to prod."
                echo "Files in conflict:"
                git diff --name-only --diff-filter=U || true
                git merge --abort || true
                exit 1
              fi
            fi

            git push prod "_promote:refs/heads/${PROD_BRANCH}"
          '''
        }
      }
    }

    stage('Deploy to dev server') {
      steps {
        sshagent(credentials: ['prod-server-ssh']) {
          sh """
            ssh -o StrictHostKeyChecking=no ose@${DEV_SERVER_IP} '
              git config --global --add safe.directory /root/lark-ops-ai-dev
              cd /root/lark-ops-ai-dev
              git pull
              sudo systemctl restart lark-ops-ai
              echo "Dev server deployed successfully"
            '
          """
        }
      }
    }

    stage('Approve prod deploy') {
      options {
        timeout(time: 24, unit: 'HOURS')
      }
      steps {
        input message: "Dev server deployed. Deploy to PROD?",
              ok: "Deploy to prod"
      }
    }

    stage('Deploy to prod server') {
      steps {
        sshagent(credentials: ['prod-server-ssh']) {
          sh """
            ssh -o StrictHostKeyChecking=no ose@${PROD_SERVER_IP} '
              git config --global --add safe.directory /root/lark-ops-ai
              cd /root/lark-ops-ai
              git pull
              sudo systemctl restart lark-ops-ai
              echo "Prod server deployed successfully"
            '
          """
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
