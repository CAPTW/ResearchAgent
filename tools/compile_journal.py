#!/usr/bin/env python3
"""Legacy CLI wrapper for journal compilation with review-schema normalization."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.reviews import compile_journal_cli


if __name__ == "__main__":
    raise SystemExit(compile_journal_cli())
