#!/usr/bin/env python3
"""
fix_bom_json.py — notes/reviews/*.json의 UTF-8 BOM 제거(선택)

Windows에서 일부 편집기/도구가 JSON을 UTF-8 with BOM으로 저장해서,
json.loads가 실패하는 문제가 발생할 수 있습니다.

사용:
  python tools/fix_bom_json.py --dir notes/reviews
"""

from __future__ import annotations
import argparse
from pathlib import Path

def strip_bom_bytes(b: bytes) -> bytes:
    bom = b"\xef\xbb\xbf"
    if b.startswith(bom):
        return b[len(bom):]
    return b

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="notes/reviews")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    d = Path(args.dir)
    if not d.exists():
        print(f"[WARN] dir not found: {d}")
        return

    changed = 0
    for p in d.glob("*.json"):
        b = p.read_bytes()
        nb = strip_bom_bytes(b)
        if nb != b:
            changed += 1
            if not args.dry_run:
                p.write_bytes(nb)
            print(f"[FIX] BOM removed: {p}")
    print(f"[DONE] changed={changed}")

if __name__ == "__main__":
    main()
