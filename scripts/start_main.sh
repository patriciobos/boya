#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT_DIR/main.pid"
LOG_DIR="$ROOT_DIR/logs"
LOG_FILE="$LOG_DIR/main.out"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
MAIN_TERM_WAIT_SECONDS="${MAIN_TERM_WAIT_SECONDS:-20}"
ORPHAN_TERM_WAIT_SECONDS="${ORPHAN_TERM_WAIT_SECONDS:-5}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

find_main_pids() {
  ps -eo pid=,args= | awk -v self="$$" '
    $1 != self && $0 ~ /(^|[[:space:]])main[.]py([[:space:]]|$)/ {
      print $1
    }
  '
}

is_running() {
  local pid="$1"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

is_main_pid() {
  local pid="$1"
  local args
  args="$(ps -p "$pid" -o args= 2>/dev/null || true)"
  [[ "$args" =~ (^|[[:space:]])main[.]py([[:space:]]|$) ]]
}

wait_for_exit() {
  local timeout="$1"
  shift
  local pids=("$@")
  local deadline=$((SECONDS + timeout))

  while (( SECONDS < deadline )); do
    local alive=()
    local pid
    for pid in "${pids[@]}"; do
      if is_running "$pid"; then
        alive+=("$pid")
      fi
    done

    if [[ "${#alive[@]}" -eq 0 ]]; then
      return 0
    fi

    sleep 1
  done

  return 1
}

unique_pids() {
  awk '!seen[$0]++'
}

stop_pidfile_main() {
  local pid

  if [[ ! -f "$PID_FILE" ]]; then
    return
  fi

  pid="$(tr -cd '0-9' < "$PID_FILE")"
  if is_running "$pid" && is_main_pid "$pid"; then
    echo "Stopping main.py from PID file with SIGTERM: $pid"
    kill -TERM "$pid" 2>/dev/null || true
    wait_for_exit "$MAIN_TERM_WAIT_SECONDS" "$pid" || true
  fi
}

cleanup_remaining_main() {
  local pids=()
  local pid

  while IFS= read -r pid; do
    [[ -z "$pid" ]] && continue
    pids+=("$pid")
  done < <(find_main_pids)

  if [[ "${#pids[@]}" -eq 0 ]]; then
    return
  fi

  mapfile -t pids < <(printf '%s\n' "${pids[@]}" | unique_pids)

  echo "Stopping remaining main.py processes with SIGTERM: ${pids[*]}"
  kill -TERM "${pids[@]}" 2>/dev/null || true
  wait_for_exit "$ORPHAN_TERM_WAIT_SECONDS" "${pids[@]}" || true

  local stubborn=()
  for pid in "${pids[@]}"; do
    if is_running "$pid"; then
      stubborn+=("$pid")
    fi
  done

  if [[ "${#stubborn[@]}" -gt 0 ]]; then
    echo "Forcing remaining main.py processes with SIGKILL: ${stubborn[*]}"
    kill -KILL "${stubborn[@]}" 2>/dev/null || true
  fi
}

stop_existing_main() {
  stop_pidfile_main
  cleanup_remaining_main
}

mkdir -p "$LOG_DIR"
stop_existing_main

cd "$ROOT_DIR"
nohup "$PYTHON_BIN" main.py >> "$LOG_FILE" 2>&1 &
new_pid="$!"
printf '%s\n' "$new_pid" > "$PID_FILE"

echo "Started main.py with PID $new_pid"
echo "PID file: $PID_FILE"
echo "Output log: $LOG_FILE"
