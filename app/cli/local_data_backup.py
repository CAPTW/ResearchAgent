"""Explicit local stopped-source copies; no application startup or activation."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from app.services.local_data_backup import BackupError, create_backup, restore_backup, verify_backup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Private XRML data copies. Stop the app and all workers before create.")
    sub = parser.add_subparsers(dest="operation", required=True)
    create = sub.add_parser("create", help="Copy explicit XRML_DATA_DIR into a new private directory bundle")
    create.add_argument("--source", required=True, type=Path)
    create.add_argument("--destination", required=True, type=Path)
    create.add_argument("--source-stopped", action="store_true", help="Confirm app, sidecar, scheduler and workers stopped")
    verify = sub.add_parser("verify", help="Verify all saved files and SQLite copies")
    verify.add_argument("--bundle", required=True, type=Path)
    restore = sub.add_parser("restore", help="Restore into an absent destination; never start or activate the app")
    restore.add_argument("--bundle", required=True, type=Path)
    restore.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.operation == "create":
            result = create_backup(args.source, args.destination, source_stopped=args.source_stopped)
        elif args.operation == "verify":
            result = verify_backup(args.bundle)
        else:
            result = restore_backup(args.bundle, args.destination)
    except BackupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"operation": args.operation, "status": "verified", "files": len(result["files"]),
                      "manifest_sha256": result["manifest_sha256"], "runtime_started": False, "private_data": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
