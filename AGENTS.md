# AGENTS.md

## Project profile

This is a mission-critical Python project. Reliability, maintainability,
explicitness, and reproducibility are more important than speed of delivery.

## General working rules

- Do not make broad refactors unless explicitly requested.
- Do not change public APIs, file formats, CLI behavior, schemas, or persisted data formats unless the task requires it.
- Prefer small, reviewable diffs.
- Before editing, inspect the relevant call sites, tests, configuration, and existing conventions.
- Preserve backward compatibility unless instructed otherwise.
- Avoid clever code. Prefer boring, explicit, readable code.
- Never hide errors silently.
- Do not use broad `except Exception` unless the exception is logged, justified, and re-raised or handled safely.
- Do not introduce global mutable state unless absolutely necessary.
- Do not introduce network, filesystem, subprocess, threading, multiprocessing, or time-dependent behavior without explaining why.
- Do not add production dependencies without asking first.
- Do not run `main.py` with low-level mocks unless the user explicitly asks for mock mode.

## Python code standards

- Code must be compatible with the Python version used by this project.
- Use type hints for all public functions, methods, and classes.
- Use precise exception types.
- Use `logging` instead of `print` in production code.
- Keep functions small and testable.
- Avoid mutable default arguments.
- Avoid dynamic `eval`, `exec`, monkeypatching, or reflection-heavy code.
- Prefer dataclasses or typed structures where they improve clarity.
- Keep imports clean and deterministic.

## Quality gates

Before considering work complete, run the relevant checks:

```bash
PYTHONPATH=. .venv/bin/python -m pytest -m "not hardware" -q
```

Hardware checks are opt-in:

```bash
RUN_HARDWARE_TESTS=1 scripts/run_ll_scripts.sh
```
