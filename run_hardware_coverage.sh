#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH=.
export RUN_HARDWARE_TESTS="${RUN_HARDWARE_TESTS:-1}"
PYTHON_BIN="${PYTHON:-.venv/bin/python}"
"$PYTHON_BIN" -m pytest -m hardware --cov --cov-config=.coveragerc.hardware --cov-report=term-missing -q -rs
