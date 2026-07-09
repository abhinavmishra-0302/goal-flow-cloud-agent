#!/usr/bin/env bash
# Run the GoalFlow cloud agent (WebSocket hub).
set -euo pipefail

# Load .env if present (WS_HOST / WS_PORT / OPENROUTER_*).
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

exec uvicorn goalflow_cloud.server:app --host "${WS_HOST:-0.0.0.0}" --port "${WS_PORT:-8000}"
