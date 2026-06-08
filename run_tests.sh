#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH=.
pytest modules/test/test_fsm_mocks.py modules/test/test_router.py modules/test/test_central_scheduler.py -q
