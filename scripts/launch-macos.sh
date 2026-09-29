#!/usr/bin/env bash
# macOS launcher. Mirrors scripts/Launch.ps1 without Windows assumptions.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

EXE="bin/skate3rust"
if [[ ! -x "$EXE" ]]; then EXE="target/debug/skate3rust"; fi
if [[ ! -x "$EXE" ]]; then
  echo "Game is not built. Run ./scripts/build-macos.sh first." >&2
  exit 1
fi

# Optional map argument: ./scripts/launch-macos.sh /path/to/University.skate
ARGS=(--assets "$ROOT/assets")
if [[ $# -gt 0 ]]; then
  case "$1" in
    *.skate) ARGS=(--assets "$ROOT/assets" --map "$1"); shift;;
    --*) ARGS=("$@"); set --;;
  esac
  if [[ $# -gt 0 && "$1" == *.skate ]]; then ARGS+=(--map "$1"); fi
fi

mkdir -p logs
LOG="logs/game-$(date +%Y%m%d-%H%M%S).log"
echo "Starting Skate 3 Rust Engine (Metal). Use any SDL-compatible controller; Esc opens menus."
echo "Log: $LOG"
"$EXE" "${ARGS[@]}" 2>&1 | tee "$LOG"
