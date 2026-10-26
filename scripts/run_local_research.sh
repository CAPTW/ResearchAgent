#!/usr/bin/env bash
set -euo pipefail

TOPIC=""
PROFILE_NAME=""
STORAGE_ROOT=""
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --topic)
      TOPIC="${2:-}"
      shift 2
      ;;
    --profile-name)
      PROFILE_NAME="${2:-}"
      shift 2
      ;;
    --storage-root)
      STORAGE_ROOT="${2:-}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$TOPIC" ]]; then
  echo "--topic is required" >&2
  exit 2
fi

UV_BIN="uv"
if ! command -v "$UV_BIN" >/dev/null 2>&1; then
  if command -v "uv.exe" >/dev/null 2>&1; then
    UV_BIN="uv.exe"
  else
    echo "uv is required" >&2
    exit 127
  fi
fi

ARGS=(run python tools/local_research_workflow.py --topic "$TOPIC")
if [[ -n "$PROFILE_NAME" ]]; then
  ARGS+=(--profile-name "$PROFILE_NAME")
fi
if [[ -n "$STORAGE_ROOT" ]]; then
  ARGS+=(--storage-root "$STORAGE_ROOT")
fi
if [[ "$DRY_RUN" -eq 1 ]]; then
  ARGS+=(--dry-run)
fi

"$UV_BIN" "${ARGS[@]}"
