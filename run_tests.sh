#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH=.
pytest test/test_fsm_mocks.py test/test_router.py test/test_central_scheduler.py test/test_ll_functional.py -q
