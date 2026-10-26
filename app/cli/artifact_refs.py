from __future__ import annotations

import argparse
import shutil
import json
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from app.db import bootstrap
from app.repositories import artifact_refs, paper_reviews


def _backup_metadata_index() -> str | None:
    source = bootstrap.metadata_index_path()
    if not source.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = source.with_name(f"{source.stem}.artifact-refs-backup-{stamp}{source.suffix}")
    shutil.copy2(source, backup)
    return str(backup)


def _content_validation_for_review(review: dict[str, Any]) -> dict[str, Any]:
    if str(review.get("full_text_status") or "") != "reviewed":
        return {"valid": None, "reason": "not_reviewed", "actual_source_text_sha256": None}
    from app.services import paper_review

    return paper_review.validate_reviewed_artifact_content(review)


def _artifact_status_for_review(
    review: dict[str, Any],
    validation: artifact_refs.ArtifactReferenceValidation,
) -> tuple[str, dict[str, Any]]:
    content_validation = _content_validation_for_review(review)
    content_reason = str(content_validation.get("reason") or "")
    if content_reason == "sha256_mismatch":
        return "hash_mismatch", content_validation
    if content_reason in {"missing_artifact_file", "missing_artifact_path"}:
        return "missing_artifact", content_validation
    if validation.status == "nested_storage_namespace":
        return "nested_storage_namespace", content_validation
    if content_reason == "unsafe_artifact_reference":
        return "outside_allowed_roots", content_validation
    if content_reason in {"reviewed_pdf_artifact_not_revalidatable", "unsupported_artifact_type"}:
        return "unsupported_format", content_validation
    if content_reason in {"empty_extracted_text", "too_short_extracted_text", "reviewed_missing_source_text_sha256", "reviewed_missing_evidence_spans"}:
        return content_reason, content_validation
    return validation.status, content_validation


def audit_review_artifact_references(
    *,
    topic_id: str | None = None,
    apply: bool = False,
    backup_first: bool = False,
    limit: int = 100_000,
) -> dict[str, Any]:
    backup_path = _backup_metadata_index() if apply and backup_first else None
    reviews = paper_reviews.list_reviews(topic_id=topic_id, limit=limit, read_only=not apply)
    checked = 0
    legacy_absolute_count = 0
    resolvable_legacy_count = 0
    unsafe_count = 0
    missing_count = 0
    would_update_count = 0
    updated_count = 0
    hash_mismatch_count = 0
    outside_allowed_roots_count = 0
    nested_namespace_count = 0
    unsupported_format_count = 0
    status_counts: Counter[str] = Counter()
    items: list[dict[str, Any]] = []

    for review in reviews:
        status = str(review.get("full_text_status") or "")
        if status not in {"reviewed", "full_text_available"}:
            continue
        checked += 1
        current_ref = str(review.get("full_text_artifact_path") or "").strip()
        result = artifact_refs.resolve_artifact_reference(current_ref, run_id=str(review.get("run_id") or "") or None)
        validation = artifact_refs.validate_artifact_reference(current_ref, run_id=str(review.get("run_id") or "") or None)
        validation_status, content_validation = _artifact_status_for_review(review, validation)
        status_counts[validation_status] += 1
        if result.was_legacy_absolute or artifact_refs.is_legacy_absolute_path(current_ref):
            legacy_absolute_count += 1
        if (result.was_legacy_absolute or artifact_refs.is_legacy_absolute_path(current_ref)) and result.exists and result.portable_ref:
            resolvable_legacy_count += 1
        if not result.is_safe:
            unsafe_count += 1
        if result.is_safe and result.reason == "missing_artifact":
            missing_count += 1
        if validation_status == "hash_mismatch":
            hash_mismatch_count += 1
        if validation_status == "outside_allowed_roots":
            outside_allowed_roots_count += 1
        if validation_status == "nested_storage_namespace":
            nested_namespace_count += 1
        if validation_status == "unsupported_format":
            unsupported_format_count += 1
        should_update = bool(
            result.is_safe
            and result.exists
            and result.portable_ref
            and current_ref
            and current_ref != result.portable_ref
            and validation_status
            not in {
                "hash_mismatch",
                "missing_artifact",
                "outside_allowed_roots",
                "nested_storage_namespace",
                "unsupported_format",
                "empty_extracted_text",
                "too_short_extracted_text",
                "reviewed_missing_source_text_sha256",
                "reviewed_missing_evidence_spans",
            }
        )
        if should_update:
            would_update_count += 1
            if apply:
                paper_reviews.update_full_text_artifact_path(
                    str(review.get("topic_id") or ""),
                    str(review.get("run_id") or ""),
                    str(review.get("paper_id") or ""),
                    result.portable_ref,
                )
                updated_count += 1
        if (
            result.was_legacy_absolute
            or artifact_refs.is_legacy_absolute_path(current_ref)
            or should_update
            or not result.is_safe
            or result.reason == "missing_artifact"
            or validation_status != "valid_portable"
        ):
            items.append(
                {
                    "topic_id": review.get("topic_id"),
                    "run_id": review.get("run_id"),
                    "paper_id": review.get("paper_id"),
                    "status": status,
                    "display_ref": result.display_ref,
                    "portable_ref": result.portable_ref,
                    "exists": result.exists,
                    "is_safe": result.is_safe,
                    "reason": result.reason,
                    "validation_status": validation_status,
                    "content_validation_reason": content_validation.get("reason"),
                    "expected_source_text_sha256": content_validation.get("expected_source_text_sha256"),
                    "actual_source_text_sha256": content_validation.get("actual_source_text_sha256"),
                    "was_legacy_absolute": result.was_legacy_absolute,
                    "is_legacy_absolute_path": artifact_refs.is_legacy_absolute_path(current_ref),
                    "would_update": should_update,
                    "updated": bool(apply and should_update),
                }
            )

    return {
        "mode": "apply" if apply else "dry_run",
        "dry_run": not apply,
        "total_reviewed_rows_checked": checked,
        "legacy_absolute_count": legacy_absolute_count,
        "resolvable_legacy_count": resolvable_legacy_count,
        "unsafe_count": unsafe_count,
        "missing_count": missing_count,
        "hash_mismatch_count": hash_mismatch_count,
        "outside_allowed_roots_count": outside_allowed_roots_count,
        "nested_namespace_count": nested_namespace_count,
        "unsupported_format_count": unsupported_format_count,
        "status_counts": dict(sorted(status_counts.items())),
        "would_update_count": would_update_count,
        "updated_count": updated_count,
        "backup_path": backup_path,
        "items": items,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit and optionally normalize paper review artifact references.")
    parser.add_argument("--topic", default=None, help="Optional topic_id filter.")
    parser.add_argument("--limit", type=int, default=100_000, help="Maximum review rows to inspect.")
    parser.add_argument("--dry-run", action="store_true", help="Report changes only. This is the default.")
    parser.add_argument("--apply", action="store_true", help="Persist portable artifact references for safe existing artifacts.")
    parser.add_argument("--backup-first", action="store_true", help="Required with --apply; copies metadata_index.sqlite3 before writes.")
    args = parser.parse_args(argv)
    if args.dry_run and args.apply:
        parser.error("--dry-run and --apply cannot be used together")
    if args.apply and not args.backup_first:
        parser.error("--apply requires --backup-first so DB updates have a local rollback copy")
    result = audit_review_artifact_references(
        topic_id=args.topic,
        apply=bool(args.apply),
        backup_first=bool(args.backup_first),
        limit=max(1, int(args.limit)),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
