#!/usr/bin/env bash
set -euo pipefail
python3 tools/pipeline.py --top 3 --weekly-top 15 --experiment --rank 1
