from __future__ import annotations

from typing import Any

from app.auth.config import get_auth_settings
from app.auth.security import ProductUser
from app.repositories.files import iso_now_utc
from app.repositories.runs import list_run_records, load_run_record, save_run_record
from app.schemas.admin import (
    LegacyOwnershipPreviewResponse,
    OwnershipAuditLineageStateCountsResponse,
    OwnershipAuditProblemBucketResponse,
    OwnershipAuditResponse,
    LegacyRunLineageSummaryResponse,
    LegacyRunsListResponse,
    LegacyRunsMigrationRequest,
    LegacyRunsMigrationResponse,
    LegacyRunSummaryResponse,
)


def _owner_user_id(run: dict[str, Any]) -> str | None:
    owner_user_id = str(run.get("owner_user_id") or "").strip()
    return owner_user_id or None


def _lineage(run: dict[str, Any]) -> dict[str, Any]:
    value = run.get("lineage")
    return value if isinstance(value, dict) else {}


def _lineage_summary(run: dict[str, Any], runs_by_id: dict[str, dict[str, Any]]) -> LegacyRunLineageSummaryResponse:
    lineage = _lineage(run)
    run_id = str(run["run_id"])
    root_run_id = str(lineage.get("root_run_id") or run_id)
    parent_run_id = str(lineage.get("parent_run_id") or "").strip() or None
    child_run_ids = sorted(
        candidate_id
        for candidate_id, candidate in runs_by_id.items()
        if str(_lineage(candidate).get("parent_run_id") or "").strip() == run_id
    )
    same_root_run_ids = sorted(
        candidate_id
        for candidate_id, candidate in runs_by_id.items()
        if str(_lineage(candidate).get("root_run_id") or candidate_id).strip() == root_run_id
    )
    return LegacyRunLineageSummaryResponse(
        parent_run_id=parent_run_id,
        root_run_id=root_run_id,
        child_run_ids=child_run_ids,
        same_root_run_ids=same_root_run_ids,
        family_size=len(same_root_run_ids) or 1,
    )


def _run_summary(run: dict[str, Any], runs_by_id: dict[str, dict[str, Any]]) -> LegacyRunSummaryResponse:
    return LegacyRunSummaryResponse(
        run_id=str(run["run_id"]),
        topic=str(run.get("topic") or ""),
        status=str(run.get("status") or ""),
        created_at=str(run.get("created_at") or ""),
        updated_at=str(run.get("updated_at") or ""),
        owner_user_id=_owner_user_id(run),
        owner_assigned_at=str(run.get("owner_assigned_at") or "") or None,
        owner_assigned_by=str(run.get("owner_assigned_by") or "") or None,
        ownership_source=str(run.get("ownership_source") or "") or None,
        is_unowned=_owner_user_id(run) is None,
        lineage=_lineage_summary(run, runs_by_id),
    )


def list_legacy_runs(*, status: str = "unowned", q: str | None = None) -> LegacyRunsListResponse:
    runs = list_run_records(limit=None)
    runs_by_id = {str(run["run_id"]): run for run in runs}
    normalized_query = str(q or "").strip().lower()
    filtered_runs: list[dict[str, Any]] = []
    unowned_count = sum(1 for run in runs if _owner_user_id(run) is None)

    for run in runs:
        if status == "unowned" and _owner_user_id(run) is not None:
            continue
        if normalized_query:
            haystack = " ".join(
                [
                    str(run.get("run_id") or ""),
                    str(run.get("topic") or ""),
                    str(run.get("status") or ""),
                ]
            ).lower()
            if normalized_query not in haystack:
                continue
        filtered_runs.append(run)

    return LegacyRunsListResponse(
        status=status,
        q=normalized_query or None,
        items=[_run_summary(run, runs_by_id) for run in filtered_runs],
        total_count=len(filtered_runs),
        unowned_count=unowned_count,
        warnings=[],
    )


def _resolve_family_run_ids(
    run: dict[str, Any],
    runs_by_id: dict[str, dict[str, Any]],
) -> tuple[list[str], list[str]]:
    lineage = _lineage(run)
    run_id = str(run["run_id"])
    root_run_id = str(lineage.get("root_run_id") or run_id).strip() or run_id
    family_ids = sorted(
        candidate_id
        for candidate_id, candidate in runs_by_id.items()
        if str(_lineage(candidate).get("root_run_id") or candidate_id).strip() == root_run_id
    )
    warnings: list[str] = []
    if not family_ids:
        warnings.append(f"Could not resolve a lineage family for {run_id}; only the requested run will be migrated.")
        return [run_id], warnings
    if run_id not in family_ids:
        family_ids.append(run_id)
        family_ids.sort()
        warnings.append(f"Resolved lineage family for {run_id} was incomplete; the requested run was forced into the set.")
    return family_ids, warnings


def preview_legacy_run_migration(
    request: LegacyRunsMigrationRequest,
    *,
    acting_user: ProductUser,
) -> LegacyRunsMigrationResponse:
    runs = list_run_records(limit=None)
    runs_by_id = {str(run["run_id"]): run for run in runs}
    requested_run_ids = [run_id for run_id in dict.fromkeys(request.run_ids)]
    target_user_id = acting_user.user_id if request.mode == "claim_to_self" else str(request.target_user_id or "").strip()

    effective_run_ids: list[str] = []
    included_lineage_run_ids: list[str] = []
    missing_run_ids: list[str] = []
    warnings: list[str] = []
    conflicts: list[str] = []
    lineage_warnings: list[str] = []

    for requested_run_id in requested_run_ids:
        run = runs_by_id.get(requested_run_id)
        if run is None:
            missing_run_ids.append(requested_run_id)
            continue
        if requested_run_id not in effective_run_ids:
            effective_run_ids.append(requested_run_id)
        if request.include_lineage_family:
            family_ids, family_warnings = _resolve_family_run_ids(run, runs_by_id)
            lineage_warnings.extend(family_warnings)
            for family_run_id in family_ids:
                if family_run_id not in effective_run_ids:
                    effective_run_ids.append(family_run_id)
                if family_run_id not in requested_run_ids and family_run_id not in included_lineage_run_ids:
                    included_lineage_run_ids.append(family_run_id)

    already_owned_run_ids: list[str] = []
    ownership_preview: list[LegacyOwnershipPreviewResponse] = []
    would_change_count = 0

    for run_id in effective_run_ids:
        run = runs_by_id[run_id]
        current_owner_user_id = _owner_user_id(run)
        included_via_lineage = run_id in included_lineage_run_ids
        explicitly_requested = run_id in requested_run_ids
        preview_warnings: list[str] = []

        if request.mode == "claim_to_self":
            if current_owner_user_id:
                action = "already_owned"
                projected_owner_user_id = current_owner_user_id
                projected_ownership_source = str(run.get("ownership_source") or "") or None
                already_owned_run_ids.append(run_id)
                preview_warnings.append(f"{run_id} is already owned by {current_owner_user_id} and will not be changed by claim_to_self.")
            else:
                action = "claim"
                projected_owner_user_id = target_user_id
                projected_ownership_source = "lineage_claim" if included_via_lineage else "admin_claim"
                would_change_count += 1
        else:
            if current_owner_user_id and request.skip_already_owned and current_owner_user_id != target_user_id:
                action = "skip_already_owned"
                projected_owner_user_id = current_owner_user_id
                projected_ownership_source = str(run.get("ownership_source") or "") or None
                already_owned_run_ids.append(run_id)
                preview_warnings.append(
                    f"{run_id} is already owned by {current_owner_user_id} and skip_already_owned=true prevented reassignment."
                )
            elif current_owner_user_id == target_user_id:
                action = "unchanged"
                projected_owner_user_id = current_owner_user_id
                projected_ownership_source = str(run.get("ownership_source") or "") or None
            else:
                action = "assign"
                projected_owner_user_id = target_user_id
                projected_ownership_source = "lineage_claim" if included_via_lineage else "admin_assign"
                would_change_count += 1
                if current_owner_user_id and current_owner_user_id != target_user_id:
                    conflicts.append(f"{run_id} will be reassigned from {current_owner_user_id} to {target_user_id}.")

        if included_via_lineage and current_owner_user_id and current_owner_user_id != target_user_id:
            lineage_warnings.append(
                f"Lineage family for {run_id} includes an already-owned run ({current_owner_user_id}); preview kept the relationship explicit."
            )

        ownership_preview.append(
            LegacyOwnershipPreviewResponse(
                run_id=run_id,
                topic=str(run.get("topic") or ""),
                status=str(run.get("status") or ""),
                action=action,
                explicitly_requested=explicitly_requested,
                included_via_lineage=included_via_lineage,
                current_owner_user_id=current_owner_user_id,
                projected_owner_user_id=projected_owner_user_id,
                current_ownership_source=str(run.get("ownership_source") or "") or None,
                projected_ownership_source=projected_ownership_source,
                parent_run_id=str(_lineage(run).get("parent_run_id") or "") or None,
                root_run_id=str(_lineage(run).get("root_run_id") or run_id),
                warnings=preview_warnings,
            )
        )

    if missing_run_ids:
        warnings.append(f"{len(missing_run_ids)} requested runs were not found and were skipped.")
    if included_lineage_run_ids:
        warnings.append(f"Lineage expansion included {len(included_lineage_run_ids)} related runs.")

    return LegacyRunsMigrationResponse(
        requested_run_ids=requested_run_ids,
        target_user_id=target_user_id or None,
        effective_run_ids=effective_run_ids,
        included_lineage_run_ids=included_lineage_run_ids,
        already_owned_run_ids=already_owned_run_ids,
        missing_run_ids=missing_run_ids,
        warnings=warnings,
        conflicts=conflicts,
        would_change_count=would_change_count,
        changed_count=0,
        ownership_preview=ownership_preview,
        lineage_warnings=lineage_warnings,
    )


def apply_legacy_run_migration(
    request: LegacyRunsMigrationRequest,
    *,
    acting_user: ProductUser,
) -> LegacyRunsMigrationResponse:
    preview = preview_legacy_run_migration(request, acting_user=acting_user)
    now = iso_now_utc()
    changed_count = 0

    for preview_item in preview.ownership_preview:
        if preview_item.action not in {"claim", "assign"}:
            continue
        run = load_run_record(preview_item.run_id)
        run["owner_user_id"] = preview_item.projected_owner_user_id
        run["owner_assigned_at"] = now
        run["owner_assigned_by"] = acting_user.user_id
        run["ownership_source"] = preview_item.projected_ownership_source
        save_run_record(run)
        changed_count += 1

    preview.changed_count = changed_count
    return preview


def _append_problem_bucket(
    buckets: list[OwnershipAuditProblemBucketResponse],
    *,
    bucket_id: str,
    bucket_type: str,
    count: int,
    sample_run_ids: list[str],
    sample_root_run_ids: list[str],
    explanation: str,
    severity: str,
    recommended_action: str,
) -> None:
    if count <= 0:
        return
    buckets.append(
        OwnershipAuditProblemBucketResponse(
            bucket_id=bucket_id,
            bucket_type=bucket_type,
            count=count,
            sample_run_ids=sample_run_ids,
            sample_root_run_ids=sample_root_run_ids,
            explanation=explanation,
            severity=severity,
            recommended_action=recommended_action,
        )
    )


def build_ownership_audit(*, sample_limit: int = 5) -> OwnershipAuditResponse:
    runs = list_run_records(limit=None)
    runs_by_id = {str(run["run_id"]): run for run in runs}
    sample_cap = max(1, sample_limit)

    unowned_runs = [run for run in runs if _owner_user_id(run) is None]
    owned_runs_missing_ownership_source = [
        run for run in runs if _owner_user_id(run) is not None and not str(run.get("ownership_source") or "").strip()
    ]
    owned_runs_missing_owner_assigned_at = [
        run for run in runs if _owner_user_id(run) is not None and not str(run.get("owner_assigned_at") or "").strip()
    ]
    owned_runs_missing_owner_assigned_by = [
        run for run in runs if _owner_user_id(run) is not None and not str(run.get("owner_assigned_by") or "").strip()
    ]

    families_by_root: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        run_id = str(run["run_id"])
        root_run_id = str(_lineage(run).get("root_run_id") or run_id).strip() or run_id
        families_by_root.setdefault(root_run_id, []).append(run)

    fully_unowned_family_count = 0
    fully_owned_single_owner_family_count = 0
    mixed_owned_and_unowned_family_count = 0
    mixed_multiple_owner_family_count = 0
    lineage_incomplete_family_count = 0
    mixed_root_run_ids: list[str] = []
    mixed_sample_run_ids: list[str] = []
    incomplete_root_run_ids: list[str] = []
    incomplete_sample_run_ids: list[str] = []
    warnings: list[str] = []

    for root_run_id, family_runs in sorted(families_by_root.items()):
        owners = sorted({_owner_user_id(run) for run in family_runs if _owner_user_id(run) is not None})
        has_unowned = any(_owner_user_id(run) is None for run in family_runs)
        family_incomplete = False

        if root_run_id not in runs_by_id and any(str(run["run_id"]) != root_run_id for run in family_runs):
            family_incomplete = True
            warnings.append(
                f"Lineage family {root_run_id} references a missing root run; audit used the declared root id without repairing it."
            )

        for run in family_runs:
            run_id = str(run["run_id"])
            lineage = _lineage(run)
            parent_run_id = str(lineage.get("parent_run_id") or "").strip()
            if parent_run_id and parent_run_id not in runs_by_id:
                family_incomplete = True
                warnings.append(f"Run {run_id} references missing parent {parent_run_id}.")
            elif parent_run_id:
                parent_root_run_id = str(_lineage(runs_by_id[parent_run_id]).get("root_run_id") or parent_run_id).strip()
                if parent_root_run_id != root_run_id:
                    family_incomplete = True
                    warnings.append(
                        f"Run {run_id} has root {root_run_id} but parent {parent_run_id} resolves to root {parent_root_run_id}."
                    )

        if not owners and has_unowned:
            fully_unowned_family_count += 1
        elif len(owners) == 1 and not has_unowned:
            fully_owned_single_owner_family_count += 1
        elif len(owners) == 1 and has_unowned:
            mixed_owned_and_unowned_family_count += 1
            mixed_root_run_ids.append(root_run_id)
            mixed_sample_run_ids.extend(str(run["run_id"]) for run in family_runs)
        elif len(owners) > 1:
            mixed_multiple_owner_family_count += 1
            mixed_root_run_ids.append(root_run_id)
            mixed_sample_run_ids.extend(str(run["run_id"]) for run in family_runs)

        if family_incomplete:
            lineage_incomplete_family_count += 1
            incomplete_root_run_ids.append(root_run_id)
            incomplete_sample_run_ids.extend(str(run["run_id"]) for run in family_runs)

    mixed_ownership_lineage_family_count = mixed_owned_and_unowned_family_count + mixed_multiple_owner_family_count
    problem_buckets: list[OwnershipAuditProblemBucketResponse] = []

    _append_problem_bucket(
        problem_buckets,
        bucket_id="unowned-runs",
        bucket_type="unowned_runs",
        count=len(unowned_runs),
        sample_run_ids=[str(run["run_id"]) for run in unowned_runs[:sample_cap]],
        sample_root_run_ids=list(
            dict.fromkeys(
                str(_lineage(run).get("root_run_id") or run["run_id"])
                for run in unowned_runs[:sample_cap]
            )
        ),
        explanation="These runs have no owner metadata and are unreadable in normal OIDC product routes until an admin claims or assigns them.",
        severity="critical",
        recommended_action="Review unowned runs in the legacy migration UI and preview claim or assign before applying.",
    )
    _append_problem_bucket(
        problem_buckets,
        bucket_id="mixed-ownership-lineage-families",
        bucket_type="mixed_ownership_lineage_families",
        count=mixed_ownership_lineage_family_count,
        sample_run_ids=list(dict.fromkeys(mixed_sample_run_ids))[:sample_cap],
        sample_root_run_ids=list(dict.fromkeys(mixed_root_run_ids))[:sample_cap],
        explanation="These same-root lineage families contain a mix of owned/unowned runs or multiple owners, which can produce confusing access behavior after login.",
        severity="warn",
        recommended_action="Review mixed-ownership families before additional reruns or compare work so the lineage stays coherent.",
    )
    _append_problem_bucket(
        problem_buckets,
        bucket_id="owned-runs-missing-ownership-source",
        bucket_type="owned_runs_missing_ownership_source",
        count=len(owned_runs_missing_ownership_source),
        sample_run_ids=[str(run["run_id"]) for run in owned_runs_missing_ownership_source[:sample_cap]],
        sample_root_run_ids=list(
            dict.fromkeys(
                str(_lineage(run).get("root_run_id") or run["run_id"])
                for run in owned_runs_missing_ownership_source[:sample_cap]
            )
        ),
        explanation="These runs have an owner but do not record how ownership was established.",
        severity="warn",
        recommended_action="Backfill ownership_source when reviewing legacy runs so future audits remain explainable.",
    )
    _append_problem_bucket(
        problem_buckets,
        bucket_id="owned-runs-missing-owner-assigned-at",
        bucket_type="owned_runs_missing_owner_assigned_at",
        count=len(owned_runs_missing_owner_assigned_at),
        sample_run_ids=[str(run["run_id"]) for run in owned_runs_missing_owner_assigned_at[:sample_cap]],
        sample_root_run_ids=list(
            dict.fromkeys(
                str(_lineage(run).get("root_run_id") or run["run_id"])
                for run in owned_runs_missing_owner_assigned_at[:sample_cap]
            )
        ),
        explanation="These runs have an owner but do not record when ownership was assigned.",
        severity="info",
        recommended_action="Backfill owner_assigned_at during a controlled metadata maintenance pass if timestamp provenance matters.",
    )
    _append_problem_bucket(
        problem_buckets,
        bucket_id="owned-runs-missing-owner-assigned-by",
        bucket_type="owned_runs_missing_owner_assigned_by",
        count=len(owned_runs_missing_owner_assigned_by),
        sample_run_ids=[str(run["run_id"]) for run in owned_runs_missing_owner_assigned_by[:sample_cap]],
        sample_root_run_ids=list(
            dict.fromkeys(
                str(_lineage(run).get("root_run_id") or run["run_id"])
                for run in owned_runs_missing_owner_assigned_by[:sample_cap]
            )
        ),
        explanation="These runs have an owner but do not record which admin or creator established ownership.",
        severity="info",
        recommended_action="Backfill owner_assigned_by during a controlled metadata maintenance pass if operator auditability matters.",
    )
    _append_problem_bucket(
        problem_buckets,
        bucket_id="lineage-incomplete-families",
        bucket_type="lineage_incomplete_families",
        count=lineage_incomplete_family_count,
        sample_run_ids=list(dict.fromkeys(incomplete_sample_run_ids))[:sample_cap],
        sample_root_run_ids=list(dict.fromkeys(incomplete_root_run_ids))[:sample_cap],
        explanation="These families contain missing or inconsistent lineage references, so the same-root grouping is only partially trustworthy.",
        severity="warn",
        recommended_action="Inspect the sampled families before using lineage-wide claim or assign operations.",
    )

    recommended_next_actions: list[str] = []
    if unowned_runs:
        recommended_next_actions.append("Review unowned runs first and decide whether to claim them to the admin user or assign them to a target owner.")
    if mixed_ownership_lineage_family_count:
        recommended_next_actions.append("Review mixed-ownership lineage families before running more compare or rerun flows on those roots.")
    if owned_runs_missing_ownership_source:
        recommended_next_actions.append("Backfill ownership_source on already-owned legacy runs so future operator audits stay explainable.")
    if lineage_incomplete_family_count:
        recommended_next_actions.append("Inspect incomplete lineage families before using include-lineage-family migration actions.")
    if not recommended_next_actions:
        recommended_next_actions.append("No ownership problems were detected in the scanned run set.")

    return OwnershipAuditResponse(
        generated_at=iso_now_utc(),
        auth_mode=get_auth_settings().auth_mode,
        total_runs=len(runs),
        unowned_run_count=len(unowned_runs),
        mixed_ownership_lineage_family_count=mixed_ownership_lineage_family_count,
        owned_runs_missing_ownership_source_count=len(owned_runs_missing_ownership_source),
        owned_runs_missing_owner_assigned_at_count=len(owned_runs_missing_owner_assigned_at),
        owned_runs_missing_owner_assigned_by_count=len(owned_runs_missing_owner_assigned_by),
        lineage_incomplete_family_count=lineage_incomplete_family_count,
        lineage_family_state_counts=OwnershipAuditLineageStateCountsResponse(
            fully_unowned_family_count=fully_unowned_family_count,
            fully_owned_single_owner_family_count=fully_owned_single_owner_family_count,
            mixed_owned_and_unowned_family_count=mixed_owned_and_unowned_family_count,
            mixed_multiple_owner_family_count=mixed_multiple_owner_family_count,
            lineage_incomplete_family_count=lineage_incomplete_family_count,
        ),
        problem_buckets=problem_buckets,
        warnings=list(dict.fromkeys(warnings)),
        recommended_next_actions=recommended_next_actions,
    )
