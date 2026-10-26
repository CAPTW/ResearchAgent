#!/usr/bin/env python3
"""Legacy CLI wrapper for non-executable experiment proposal scaffolding."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.experiments import main


if __name__ == "__main__":
    raise SystemExit(main())
