"""Export an offline comparison proposal; deliberately no execute option."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.domain import prepared_review
from app.services.anthropic_output_comparison import prepare, write_bundle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True, help="Frozen prepared-review contract JSON")
    parser.add_argument("--output-dir", type=Path, required=True, help="New absolute directory; parent must exist")
    args = parser.parse_args(argv)
    try:
        with args.contract.open("rb") as source:
            raw = source.read(prepared_review.MAX_CONTRACT_BYTES + 1)
        if len(raw) > prepared_review.MAX_CONTRACT_BYTES:
            raise ValueError()
        plan = prepare(json.loads(raw))
        write_bundle(plan, args.output_dir)
    except (OSError, ValueError, TypeError, RecursionError):
        print("Preparation failed: invalid contract or unavailable/new output directory required.", file=sys.stderr)
        return 1
    print(json.dumps(dict(state=plan["state"], live_execution="NOT_RUN", plan_sha256=plan["plan_sha256"],
        output_dir=str(args.output_dir)), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
