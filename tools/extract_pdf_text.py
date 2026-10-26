#!/usr/bin/env python3
"""Legacy CLI wrapper for PDF text extraction."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.reviews import extract_pdf_text_cli


if __name__ == "__main__":
    raise SystemExit(extract_pdf_text_cli())
