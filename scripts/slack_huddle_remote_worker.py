#!/usr/bin/env python3
"""
Run this on a machine where Playwright + Slack SESSION_DIR actually work (e.g. office PC,
always-on Mac/Linux with a real desktop or stable headless after login).

The cloud lark-ops-ai host POSTs here (see SLACK_HUDDLE_REMOTE_URL on the bot) instead of
running slack_huddle_invite_all.py locally.

Setup on the worker:
  export SESSION_DIR=/path/to/chromium/profile   # logged-in Slack
  export SLACK_HUDDLE_REMOTE_SECRET=long-random-string   # same value as on the ECS bot (Authorization: Bearer)
  export SLACK_SUBPROCESS_PYTHON=/path/to/venv/bin/python
  # optional: SLACK_HEADLESS=1  SLACK_HUDDLE_WORKER_BIND=127.0.0.1  SLACK_HUDDLE_WORKER_PORT=8765

  python scripts/slack_huddle_remote_worker.py

Expose with TLS (nginx/Caddy) + firewall; do not expose plain HTTP to the public internet.

POST /huddle
  Authorization: Bearer <SLACK_HUDDLE_REMOTE_SECRET>   (if secret set)
  Content-Type: application/json
  {"channel_url":"https://app.slack.com/client/T.../C...","priority":"P0","incident_chat_id":"oc_..."}
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "slack_huddle_invite_all.py"
_LOG = _REPO_ROOT / "logs" / "slack_huddle_remote_worker.log"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("%s - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))

    def do_POST(self) -> None:
        if self.path not in ("/huddle", "/huddle/"):
            self.send_response(404)
            self.end_headers()
            return
        want = (
            os.getenv("SLACK_HUDDLE_REMOTE_SECRET") or os.getenv("SLACK_HUDDLE_WORKER_SECRET") or ""
        ).strip()
        if want:
            auth = (self.headers.get("Authorization") or "").strip()
            if auth != f"Bearer {want}":
                self.send_response(401)
                self.end_headers()
                return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"invalid json"}')
            return
        channel_url = (data.get("channel_url") or "").strip()
        if not channel_url:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"channel_url required"}')
            return
        session_dir = (os.getenv("SESSION_DIR") or "").strip()
        if not session_dir:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"SESSION_DIR must be set on worker"}')
            return
        if not _SCRIPT.is_file():
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"slack_huddle_invite_all.py missing"}')
            return
        py = (os.getenv("SLACK_SUBPROCESS_PYTHON") or sys.executable).strip()
        env = os.environ.copy()
        env["SLACK_CHANNEL_URL"] = channel_url
        env["SESSION_DIR"] = session_dir
        env["SLACK_HEADLESS"] = (os.getenv("SLACK_HEADLESS") or "1").strip()
        _LOG.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(_LOG, "a", encoding="utf-8", buffering=1) as lf:
                lf.write(
                    f"\n===== remote_worker channel_url={channel_url[:80]} priority={data.get('priority')} =====\n"
                )
                lf.flush()
                subprocess.Popen(
                    [py, str(_SCRIPT)],
                    cwd=str(_REPO_ROOT),
                    env=env,
                    stdout=lf,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=False,
                )
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            return
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"accepted"}')


def main() -> None:
    host = (os.getenv("SLACK_HUDDLE_WORKER_BIND") or "0.0.0.0").strip()
    port = int((os.getenv("SLACK_HUDDLE_WORKER_PORT") or "8765").strip())
    httpd = HTTPServer((host, port), _Handler)
    print(
        f"slack_huddle_remote_worker: POST http://{host}:{port}/huddle "
        f"(log: {_LOG})",
        flush=True,
    )
    httpd.serve_forever()


if __name__ == "__main__":
    main()
