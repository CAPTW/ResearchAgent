#!/usr/bin/env bash
set -euo pipefail

UV_BIN="uv"
if ! command -v "$UV_BIN" >/dev/null 2>&1; then
  if command -v "uv.exe" >/dev/null 2>&1; then
    UV_BIN="uv.exe"
  else
    echo "uv is required" >&2
    exit 127
  fi
fi

"$UV_BIN" run python tools/sync_agent_adapters.py "$@"
