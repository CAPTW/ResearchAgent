from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.ownership_migration import build_ownership_audit


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Summarize ownership migration readiness for shared deployments.")
    parser.add_argument("--json", action="store_true", help="Print the full audit as JSON.")
    parser.add_argument("--csv", type=Path, default=None, help="Write problem buckets to a CSV file.")
    parser.add_argument("--only-problems", action="store_true", help="Print only problem buckets in text mode.")
    parser.add_argument("--limit", type=int, default=5, help="Maximum sample ids per problem bucket.")
    return parser


def write_csv(path: Path, audit_payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "bucket_id",
                "bucket_type",
                "count",
                "severity",
                "recommended_action",
                "sample_run_ids",
                "sample_root_run_ids",
                "explanation",
            ],
        )
        writer.writeheader()
        for bucket in audit_payload.get("problem_buckets", []):
            writer.writerow(
                {
                    "bucket_id": bucket["bucket_id"],
                    "bucket_type": bucket["bucket_type"],
                    "count": bucket["count"],
                    "severity": bucket["severity"],
                    "recommended_action": bucket["recommended_action"],
                    "sample_run_ids": ";".join(bucket.get("sample_run_ids", [])),
                    "sample_root_run_ids": ";".join(bucket.get("sample_root_run_ids", [])),
                    "explanation": bucket["explanation"],
                }
            )


def print_text_summary(audit_payload: dict, *, only_problems: bool) -> None:
    if not only_problems:
        print(f"generated_at: {audit_payload['generated_at']}")
        print(f"auth_mode: {audit_payload['auth_mode']}")
        print(f"total_runs: {audit_payload['total_runs']}")
        print(f"unowned_run_count: {audit_payload['unowned_run_count']}")
        print(f"mixed_ownership_lineage_family_count: {audit_payload['mixed_ownership_lineage_family_count']}")
        print(
            "owned_runs_missing_ownership_source_count: "
            f"{audit_payload['owned_runs_missing_ownership_source_count']}"
        )
        print(
            "owned_runs_missing_owner_assigned_at_count: "
            f"{audit_payload['owned_runs_missing_owner_assigned_at_count']}"
        )
        print(
            "owned_runs_missing_owner_assigned_by_count: "
            f"{audit_payload['owned_runs_missing_owner_assigned_by_count']}"
        )
        print(f"lineage_incomplete_family_count: {audit_payload['lineage_incomplete_family_count']}")
        print("")

    if not audit_payload.get("problem_buckets"):
        print("No ownership problems detected.")
        return

    for bucket in audit_payload["problem_buckets"]:
        print(f"[{bucket['severity']}] {bucket['bucket_type']} ({bucket['count']})")
        print(f"  explanation: {bucket['explanation']}")
        print(f"  recommended_action: {bucket['recommended_action']}")
        if bucket.get("sample_run_ids"):
            print(f"  sample_run_ids: {', '.join(bucket['sample_run_ids'])}")
        if bucket.get("sample_root_run_ids"):
            print(f"  sample_root_run_ids: {', '.join(bucket['sample_root_run_ids'])}")
        print("")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    audit = build_ownership_audit(sample_limit=max(1, args.limit))
    audit_payload = audit.model_dump()

    if args.csv is not None:
        write_csv(args.csv, audit_payload)

    if args.json:
        print(json.dumps(audit_payload, indent=2))
    else:
        print_text_summary(audit_payload, only_problems=args.only_problems)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
