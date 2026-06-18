#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT_DIR/main.pid"
MAIN_TERM_WAIT_SECONDS="${MAIN_TERM_WAIT_SECONDS:-20}"
ORPHAN_TERM_WAIT_SECONDS="${ORPHAN_TERM_WAIT_SECONDS:-5}"
MOCK_ENV_PATTERN='^USE_(LL_MOCKS|MOCK_)='

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

unique_pids() {
  awk '!seen[$0]++'
}

find_main_pids() {
  ps -eo pid=,args= | awk -v self="$$" '
    $1 != self && $0 ~ /(^|[[:space:]])main[.]py([[:space:]]|$)/ {
      print $1
    }
  '
}

find_descendants() {
  local root_pid="$1"
  local children=()
  local child

  while IFS= read -r child; do
    [[ -z "$child" ]] && continue
    children+=("$child")
  done < <(ps -eo pid=,ppid= | awk -v ppid="$root_pid" '$2 == ppid { print $1 }')

  for child in "${children[@]}"; do
    printf '%s\n' "$child"
    find_descendants "$child"
  done
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

collect_target_pids() {
  local pids=()
  local pid

  if [[ -f "$PID_FILE" ]]; then
    pid="$(tr -cd '0-9' < "$PID_FILE")"
    if is_running "$pid" && is_main_pid "$pid"; then
      pids+=("$pid")
      while IFS= read -r child; do
        [[ -z "$child" ]] && continue
        pids+=("$child")
      done < <(find_descendants "$pid")
    fi
  fi

  while IFS= read -r pid; do
    [[ -z "$pid" ]] && continue
    pids+=("$pid")
    while IFS= read -r child; do
      [[ -z "$child" ]] && continue
      pids+=("$child")
    done < <(find_descendants "$pid")
  done < <(find_main_pids)

  if [[ "${#pids[@]}" -gt 0 ]]; then
    printf '%s\n' "${pids[@]}" | unique_pids
  fi
}

stop_pids() {
  local pids=("$@")
  if [[ "${#pids[@]}" -eq 0 ]]; then
    echo "No main.py processes found."
    return
  fi

  echo "Stopping main.py process tree with SIGTERM: ${pids[*]}"
  kill -TERM "${pids[@]}" 2>/dev/null || true
  wait_for_exit "$MAIN_TERM_WAIT_SECONDS" "${pids[@]}" || true

  local alive=()
  local pid
  for pid in "${pids[@]}"; do
    if is_running "$pid"; then
      alive+=("$pid")
    fi
  done

  if [[ "${#alive[@]}" -gt 0 ]]; then
    echo "Forcing remaining processes with SIGKILL: ${alive[*]}"
    kill -KILL "${alive[@]}" 2>/dev/null || true
    wait_for_exit "$ORPHAN_TERM_WAIT_SECONDS" "${alive[@]}" || true
  fi
}

cleanup_pid_file() {
  if [[ ! -f "$PID_FILE" ]]; then
    return
  fi

  local pid
  pid="$(tr -cd '0-9' < "$PID_FILE")"
  if [[ -z "$pid" ]] || ! is_running "$pid" || ! is_main_pid "$pid"; then
    rm -f "$PID_FILE"
    echo "Removed stale PID file: $PID_FILE"
  fi
}

check_mock_environment() {
  local found=0

  while IFS= read -r entry; do
    echo "Mock variable is exported in this stop script environment: $entry"
    found=1
  done < <(env | grep -E "$MOCK_ENV_PATTERN" || true)

  local pid
  while IFS= read -r pid; do
    [[ -z "$pid" ]] && continue
    if [[ -r "/proc/$pid/environ" ]]; then
      while IFS= read -r entry; do
        echo "Mock variable is still present in process $pid: $entry"
        found=1
      done < <(tr '\0' '\n' < "/proc/$pid/environ" | grep -E "$MOCK_ENV_PATTERN" || true)
    fi
  done < <(find_main_pids)

  if [[ "$found" -eq 0 ]]; then
    echo "No exported USE_LL_MOCKS/USE_MOCK_* variables found in this environment or remaining main.py processes."
    return 0
  fi

  return 1
}

mapfile -t target_pids < <(collect_target_pids)
stop_pids "${target_pids[@]}"
cleanup_pid_file

mapfile -t remaining_pids < <(find_main_pids)
if [[ "${#remaining_pids[@]}" -gt 0 ]]; then
  echo "WARNING: main.py processes still running: ${remaining_pids[*]}"
  exit_code=1
else
  echo "No main.py processes remain."
  exit_code=0
fi

if ! check_mock_environment; then
  exit_code=1
fi
exit "$exit_code"
