#!/usr/bin/env bash
# Thin invocation wrapper for canvas_sync_usf.py.
# Runs with a timestamped log under ~/canvas-mirror/logs/.
# Pass-through args go to the python script.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TS="$(date +%Y%m%d-%H%M%S)"
LOG_DIR="${HOME}/canvas-mirror/logs"
mkdir -p "$LOG_DIR"
LOG="${LOG_DIR}/usf-sync-${TS}.log"

# Prefer venv'd python if one exists next to the script
if [[ -x "${HERE}/.venv/bin/python" ]]; then
  PY="${HERE}/.venv/bin/python"
else
  PY="python3"
fi

exec "$PY" "${HERE}/canvas_sync_usf.py" --log "$LOG" "$@"
