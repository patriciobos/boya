#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
REPORT_DIR="$ROOT_DIR/test/reports"
RUN_LOG="$REPORT_DIR/ll_scripts_run.log"

if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

mkdir -p "$REPORT_DIR"

echo "Writing LL script runner output to $RUN_LOG"

set +e
RUN_HARDWARE_TESTS="${RUN_HARDWARE_TESTS:-1}" \
PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}" \
"$PYTHON_BIN" -m pytest "$ROOT_DIR/test/test_run_ll_scripts.py" -q -s "$@" 2>&1 | tee "$RUN_LOG"
status="${PIPESTATUS[0]}"
set -e

exit "$status"
