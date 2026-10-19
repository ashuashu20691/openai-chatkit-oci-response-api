#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

if [[ ! -d ".venv" ]]; then
  echo "Create the virtual environment and install requirements first. See README.md."
  exit 1
fi

source .venv/bin/activate
uvicorn chatkit_server:app --host 127.0.0.1 --port 8001 &
CHATKIT_PID=$!
cleanup() {
  kill "$CHATKIT_PID" 2>/dev/null || true
  wait "$CHATKIT_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

streamlit run app.py --server.address 127.0.0.1 --server.port 8501
