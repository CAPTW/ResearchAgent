#!/usr/bin/env python3
"""Legacy CLI wrapper for PDF fetch preparation."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.reviews import fetch_pdfs_cli


if __name__ == "__main__":
    raise SystemExit(fetch_pdfs_cli())
