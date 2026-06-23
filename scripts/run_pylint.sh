#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH=.
PYTHON_BIN="${PYTHON:-.venv/bin/python}"
"$PYTHON_BIN" -m pylint modules test main.py
