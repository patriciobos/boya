#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH=.
PYTHON_BIN="${PYTHON:-.venv/bin/python}"
"$PYTHON_BIN" -m pytest test/test_fsm_mocks.py test/test_router.py test/test_central_scheduler.py test/test_ll_functional.py -q
