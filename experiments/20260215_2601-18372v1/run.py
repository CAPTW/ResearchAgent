#!/usr/bin/env python3
"""run.py — experiment entrypoint (stub)

This is intentionally minimal. Replace with your actual training/eval loop.
"""

import argparse
import json
from pathlib import Path

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    print("[INFO] Loaded config:")
    print(json.dumps(cfg, ensure_ascii=False, indent=2))

    print("\n[TODO] Implement your experiment here.")
    print("- Load dataset")
    print("- Build model")
    print("- Train/eval")
    print("- Measure latency/power if relevant")
    print("- Write results to notes.md or a results.json")

if __name__ == "__main__":
    main()
