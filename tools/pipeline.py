#!/usr/bin/env python3
"""Legacy one-shot pipeline wrapper backed by the importable run service."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.schemas.runs import RunCreateRequest
from app.services.runs import get_run, run_sync


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=3, help="How many paper notes to generate.")
    parser.add_argument("--weekly-top", type=int, default=15, help="How many papers to include in weekly synthesis.")
    parser.add_argument(
        "--experiment",
        action="store_true",
        help="Generate a non-executable experiment design proposal scaffold at the end.",
    )
    parser.add_argument(
        "--rank",
        type=int,
        default=1,
        help="Which ranked paper to use as the proposal basis (1-based).",
    )
    parser.add_argument("--skip-scout", action="store_true", help="Skip Scout even if network is available.")
    parser.add_argument("--skip-synthesis", action="store_true", help="Skip weekly synthesis.")
    parser.add_argument("--skip-summary", action="store_true", help="Skip paper note generation.")
    args = parser.parse_args(argv)

    request = RunCreateRequest(
        topic="XR/AR × ML (Image/Video)",
        top_papers=args.top,
        weekly_top=args.weekly_top,
        experiment=args.experiment,
        rank=args.rank,
        skip_scout=args.skip_scout,
        skip_summary=args.skip_summary,
        skip_synthesis=args.skip_synthesis,
    )
    run = run_sync(request, source="legacy_cli", write_legacy=True)
    run = get_run(run["run_id"])
    summary = {
        "run_id": run["run_id"],
        "status": run["status"],
        "generated_at_utc": run["updated_at"],
        "steps": [
            {
                "name": step["name"].title(),
                "ok": step.get("status")
                in {
                    "completed",
                    "completed_with_warnings",
                    "skipped",
                    "scaffold_created",
                    "not_generated",
                },
                "output_hint": step.get("output_hint"),
                "note": step.get("note"),
            }
            for step in run.get("steps", [])
        ],
        "experiment_dir": run.get("artifacts", {}).get("experiment_dir"),
        "experiment_status": run.get("experiment_status"),
        "daily_report": run.get("artifacts", {}).get("daily_report"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if run.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
