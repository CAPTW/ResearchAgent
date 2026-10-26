"""Explicit local operator entrypoint for SQLite FK planning and migration."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from app.db import bootstrap
from app.db.sqlite_integrity import (
    STORE_METADATA,
    STORE_SCHEDULER,
    SqliteIntegrityError,
)
from app.db.sqlite_migrations import (
    SqliteForeignKeyMigrationBusy,
    apply_sqlite_foreign_key_migration,
    audit_sqlite_integrity,
    plan_sqlite_foreign_key_migration,
)

_PLAN_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _store_path(store: str) -> Path:
    if store == STORE_METADATA:
        return bootstrap.metadata_index_path(ensure_parent=False)
    return bootstrap.scheduler_db_path(ensure_parent=False)


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Plan, audit, or explicitly apply required SQLite foreign keys.",
    )
    parser.add_argument(
        "--store",
        required=True,
        choices=(STORE_METADATA, STORE_SCHEDULER),
    )
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--plan", action="store_true")
    operation.add_argument("--audit", action="store_true")
    operation.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-plan-digest")
    args = parser.parse_args(argv)

    if not args.apply and args.expected_plan_digest is not None:
        _emit(
            {
                "status": "invalid_operator_request",
                "code": "sqlite_expected_plan_digest_not_applicable",
                "store": args.store,
            }
        )
        return 2
    if args.apply and not _PLAN_DIGEST_PATTERN.fullmatch(
        str(args.expected_plan_digest or "")
    ):
        _emit(
            {
                "status": "invalid_operator_request",
                "code": "sqlite_expected_plan_digest_required",
                "store": args.store,
            }
        )
        return 2

    target = _store_path(args.store)
    try:
        if args.apply:
            result = apply_sqlite_foreign_key_migration(
                store=args.store,
                path=target,
                expected_plan_digest=args.expected_plan_digest,
            )
            _emit({"operation": "apply", **result.to_payload()})
        elif args.audit:
            result = audit_sqlite_integrity(store=args.store, path=target)
            _emit({"operation": "audit", **result.to_payload()})
        else:
            result = plan_sqlite_foreign_key_migration(
                store=args.store,
                path=target,
            )
            _emit({"operation": "plan", **result.to_payload()})
    except SqliteIntegrityError as exc:
        payload: dict[str, object] = {
            "status": "blocked",
            "code": exc.code,
            "store": exc.store,
        }
        if isinstance(exc, SqliteForeignKeyMigrationBusy):
            payload["phase"] = exc.phase
            payload["retryable"] = exc.retryable
        _emit(payload)
        return 2
    except (OSError, ValueError):
        _emit(
            {
                "status": "failed",
                "code": "sqlite_operator_request_failed",
                "store": args.store,
            }
        )
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
