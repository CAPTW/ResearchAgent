from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.schemas.compare import CompareCandidatesResponse, RunCompareContextResponse
from app.schemas.workspace import (
    EditingSessionRecoverResponse,
    EditingSessionRecoveryPreviewResponse,
    EditingSessionStalenessResponse,
    OutlineActionHistoryDiffResponse,
    OutlineActionHistoryFocusContextResponse,
    StructuredPatchResponse,
    WorkspaceApplyStagedPreflightResponse,
    WorkspaceCandidateConflictSummaryResponse,
    WorkspaceConflictDetailResponse,
    WorkspaceStagedActionsResponse,
)
from app.services import outline_history


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _staged_actions_path(repo: dict[str, Any]) -> Path:
    return repo["primary_run_dir"] / "inputs" / "staged_workspace_actions.json"


def _editing_sessions_path(repo: dict[str, Any]) -> Path:
    return repo["primary_run_dir"] / "inputs" / "editing_sessions.json"


def _history_path(repo: dict[str, Any]) -> Path:
    return repo["primary_run_dir"] / "reports" / "outline_action_history.json"


def _clone_candidate(
    base: dict[str, Any],
    *,
    candidate_id: str,
    target_section_id: str | None = None,
    target_section_title: str | None = None,
    source_section_id: str | None = None,
    source_section_title: str | None = None,
    merge_intent: str | None = None,
    requires_confirmation: bool | None = None,
    destructive: bool | None = None,
    editing_session_id: str,
) -> dict[str, Any]:
    candidate = deepcopy(base)
    candidate["candidate_id"] = candidate_id
    candidate["created_at"] = "2026-04-09T00:00:00+00:00"
    candidate["editing_session_id"] = editing_session_id
    candidate["warnings"] = []
    candidate["conflict_flags"] = []
    candidate["missing_artifacts"] = []
    candidate.setdefault("provenance", {})
    candidate["provenance"]["action_id"] = candidate_id
    candidate.setdefault("request", {})
    candidate["request"]["editing_session_id"] = editing_session_id
    if target_section_id is not None:
        candidate["target_section_id"] = target_section_id
        candidate["request"]["target_section_id"] = target_section_id
        target_indexes = {
            "executive-summary": 0,
            "trend-signals": 1,
            "top-papers": 2,
            "research-gaps": 3,
            "next-experiments": 4,
        }
        if target_section_id in target_indexes:
            candidate["target_index"] = target_indexes[target_section_id]
            candidate["request"]["target_index"] = target_indexes[target_section_id]
    if target_section_title is not None:
        candidate["target_section_title"] = target_section_title
    if source_section_id is not None:
        candidate["source_section_id"] = source_section_id
        candidate["request"]["source_section_id"] = source_section_id
    if source_section_title is not None:
        candidate["source_section_title"] = source_section_title
        candidate["request"]["source_section_title"] = source_section_title
    if merge_intent is not None:
        candidate["merge_intent"] = merge_intent
    if requires_confirmation is not None:
        candidate["requires_confirmation"] = requires_confirmation
    if destructive is not None:
        candidate["destructive"] = destructive
    return candidate


def _write_staged_candidates(repo: dict[str, Any], candidates: list[dict[str, Any]]) -> None:
    for index, candidate in enumerate(candidates, start=1):
        candidate["order"] = index
    _write_json(_staged_actions_path(repo), candidates)


def _update_session_queue(
    repo: dict[str, Any],
    candidate_ids: list[str],
    *,
    outline_hash: str | None = None,
    pending_extra_ids: list[str] | None = None,
) -> None:
    sessions_path = _editing_sessions_path(repo)
    sessions = _read_json(sessions_path)
    for session in sessions:
        if session.get("session_id") == repo["session_id"]:
            session["staged_action_ids"] = list(candidate_ids)
            session["pending_merge_order"] = list(candidate_ids) + list(pending_extra_ids or [])
            if outline_hash is not None:
                session["outline_draft_hash"] = outline_hash
                session["current_outline_draft_hash"] = outline_hash
            session["warnings"] = []
            session["stale"] = False
            break
    _write_json(sessions_path, sessions)


@pytest.mark.parametrize(
    ("conflict_kind", "severity", "can_recover_by_rebase", "can_proceed_with_confirmation"),
    [
        ("missing_target_section", "blocker", False, False),
        ("missing_source_section", "blocker", False, False),
        ("weak_anchor_match", "warn", False, False),
        ("ambiguous_target_match", "warn", False, False),
        ("duplicate_target_mutation", "blocker", False, False),
        ("destructive_replace_requires_confirmation", "confirmation_required", False, True),
        ("stale_outline_hash", "warn", False, False),
        ("citation_overlap_or_reference_risk", "warn", False, False),
        ("target_order_instability", "warn", True, False),
        ("source_artifact_missing", "blocker", False, False),
        ("dropped_dependency", "recoverable", True, False),
        ("recoverable_by_rebase", "recoverable", True, False),
    ],
)
def test_build_conflict_detail_supports_all_normalized_kinds(
    conflict_kind: str,
    severity: str,
    can_recover_by_rebase: bool,
    can_proceed_with_confirmation: bool,
) -> None:
    detail = outline_history.build_conflict_detail(
        conflict_id=f"candidate-1::{conflict_kind}",
        candidate_id="candidate-1",
        conflict_kind=conflict_kind,
        severity=severity,
        title=conflict_kind.replace("_", " "),
        message=f"{conflict_kind} detected",
        related_section_ids=["executive-summary", "executive-summary"],
        related_candidate_ids=["candidate-1", "candidate-2", "candidate-2"],
        related_paper_ids=["2601.18372v1", "2601.18372v1"],
        anchor_summary="Anchor resolution was derived from a coarse fallback.",
        target_resolution_summary="The candidate resolved against the current outline.",
        suggested_recovery_mode="rebase_staged_candidates" if can_recover_by_rebase else "none",
        suggested_manual_actions=["Inspect conflict drilldown", "Inspect conflict drilldown"],
        can_recover_by_rebase=can_recover_by_rebase,
        can_proceed_with_confirmation=can_proceed_with_confirmation,
        warnings=["contract-lock", "contract-lock"],
        provenance={"source": "test"},
    )

    validated = WorkspaceConflictDetailResponse.model_validate(detail)

    assert validated.conflict_kind == conflict_kind
    assert validated.severity == severity
    assert validated.related_section_ids == ["executive-summary"]
    assert validated.related_candidate_ids == ["candidate-1", "candidate-2"]
    assert validated.related_paper_ids == ["2601.18372v1"]
    assert validated.warnings == ["contract-lock"]
    assert validated.can_recover_by_rebase is can_recover_by_rebase
    assert validated.can_proceed_with_confirmation is can_proceed_with_confirmation


def test_candidate_conflict_summary_aggregates_normalized_conflicts() -> None:
    conflicts = [
        outline_history.build_conflict_detail(
            conflict_id="candidate-1::missing_target_section",
            candidate_id="candidate-1",
            conflict_kind="missing_target_section",
            severity="blocker",
            title="Missing target section",
            message="The staged target section no longer resolves.",
            related_section_ids=["executive-summary"],
            related_paper_ids=["2601.18372v1"],
        ),
        outline_history.build_conflict_detail(
            conflict_id="candidate-1::recoverable_by_rebase",
            candidate_id="candidate-1",
            conflict_kind="recoverable_by_rebase",
            severity="recoverable",
            title="Candidate can be rebased",
            message="Rebase can retarget the candidate against the current outline.",
            related_section_ids=["research-gaps"],
            related_paper_ids=["2602.01674v1"],
            suggested_recovery_mode="rebase_staged_candidates",
            suggested_manual_actions=["Preview rebase before apply-all."],
            can_recover_by_rebase=True,
        ),
    ]
    summary = outline_history.build_candidate_conflict_summary(
        {
            "candidate_id": "candidate-1",
            "source_section_id": "executive-summary",
            "target_section_id": "research-gaps",
            "warnings": ["candidate warning", "candidate warning"],
            "blocks": [
                {
                    "block_id": "block-1",
                    "related_paper_ids": ["2601.18372v1", "2602.01674v1", "2602.01674v1"],
                }
            ],
        },
        conflicts=conflicts,
        status="recoverable_by_rebase",
        suggested_recovery_mode="rebase_staged_candidates",
        suggested_manual_actions=["Preview rebase before apply-all."],
        can_recover_by_rebase=True,
    )

    validated = WorkspaceCandidateConflictSummaryResponse.model_validate(summary)

    assert validated.status == "recoverable_by_rebase"
    assert validated.total_conflicts == 2
    assert validated.highest_severity == "blocker"
    assert validated.severity_counts["blocker"] == 1
    assert validated.severity_counts["recoverable"] == 1
    assert set(validated.affected_section_ids) == {"executive-summary", "research-gaps"}
    assert set(validated.related_paper_ids) == {"2601.18372v1", "2602.01674v1"}
    assert validated.suggested_recovery_mode == "rebase_staged_candidates"
    assert validated.suggested_manual_actions == ["Preview rebase before apply-all."]
    assert validated.can_recover_by_rebase is True
    assert validated.warnings == ["candidate warning"]


def test_workspace_api_smoke_routes_return_current_contracts(client: TestClient, isolated_run_repo: dict[str, Any]) -> None:
    run_id = isolated_run_repo["run_id"]
    session_id = isolated_run_repo["session_id"]
    staged_action_id = isolated_run_repo["staged_action_id"]
    history_action_id = isolated_run_repo["history_action_id"]

    compare_context_response = client.get(f"/runs/{run_id}/compare-context")
    compare_candidates_response = client.get(f"/runs/{run_id}/compare-candidates")
    staleness_response = client.get(f"/runs/{run_id}/editing-sessions/{session_id}/staleness")
    staged_response = client.get(f"/runs/{run_id}/workspace-actions/staged", params={"editing_session_id": session_id})
    patch_response = client.get(
        f"/runs/{run_id}/workspace-actions/staged/{staged_action_id}/patch",
        params={"editing_session_id": session_id},
    )
    preflight_response = client.post(
        f"/runs/{run_id}/workspace-actions/preflight-apply-staged",
        json={"editing_session_id": session_id, "apply_all": True},
    )
    recovery_response = client.post(
        f"/runs/{run_id}/editing-sessions/{session_id}/recover-preview",
        json={"mode": "rebase_staged_candidates"},
    )
    focus_response = client.get(f"/runs/{run_id}/outline-action-history/{history_action_id}/focus-context")
    diff_response = client.get(f"/runs/{run_id}/outline-action-history/diff", params={"to_action_id": history_action_id})

    assert compare_context_response.status_code == 200, compare_context_response.text
    assert compare_candidates_response.status_code == 200, compare_candidates_response.text
    assert staleness_response.status_code == 200, staleness_response.text
    assert staged_response.status_code == 200, staged_response.text
    assert patch_response.status_code == 200, patch_response.text
    assert preflight_response.status_code == 200, preflight_response.text
    assert recovery_response.status_code == 200, recovery_response.text
    assert focus_response.status_code == 200, focus_response.text
    assert diff_response.status_code == 200, diff_response.text

    compare_context = RunCompareContextResponse.model_validate(compare_context_response.json())
    compare_candidates = CompareCandidatesResponse.model_validate(compare_candidates_response.json())
    staleness = EditingSessionStalenessResponse.model_validate(staleness_response.json())
    staged = WorkspaceStagedActionsResponse.model_validate(staged_response.json())
    patch = StructuredPatchResponse.model_validate(patch_response.json())
    preflight = WorkspaceApplyStagedPreflightResponse.model_validate(preflight_response.json())
    recovery = EditingSessionRecoveryPreviewResponse.model_validate(recovery_response.json())
    focus = OutlineActionHistoryFocusContextResponse.model_validate(focus_response.json())
    diff = OutlineActionHistoryDiffResponse.model_validate(diff_response.json())

    assert compare_context.run_id == run_id
    assert compare_candidates.run_id == run_id
    assert staleness.session_id == session_id
    assert staged.items
    assert patch.candidate_conflict_summary is not None
    assert preflight.items
    assert recovery.candidate_results
    assert recovery.candidate_results[0].rebase_diff is not None
    assert focus.ready is True
    assert focus.route_restore_hints.compare_view == "report"
    assert diff.items


def test_preflight_groups_candidates_by_current_contract_state(client: TestClient, isolated_run_repo: dict[str, Any]) -> None:
    staged_path = _staged_actions_path(isolated_run_repo)
    base_candidate = _read_json(staged_path)[0]
    session_id = isolated_run_repo["session_id"]

    safe_candidate = _clone_candidate(
        base_candidate,
        candidate_id="safe-candidate",
        target_section_id="trend-signals",
        target_section_title="Trend Signals",
        editing_session_id=session_id,
    )
    warn_candidate = _clone_candidate(
        base_candidate,
        candidate_id="warn-legacy-candidate",
        target_section_id="missing-warn-target",
        target_section_title="Missing warn target",
        merge_intent="create_candidate_section",
        editing_session_id=session_id,
    )
    recoverable_candidate = _clone_candidate(
        base_candidate,
        candidate_id="recoverable-source-candidate",
        source_section_id="missing-source-section",
        source_section_title="Missing Source Section",
        target_section_id="research-gaps",
        target_section_title="Research Gaps / Open Questions",
        editing_session_id=session_id,
    )
    confirmation_candidate = _clone_candidate(
        base_candidate,
        candidate_id="confirmation-candidate",
        target_section_id="next-experiments",
        target_section_title="Next Experiments",
        requires_confirmation=True,
        destructive=True,
        editing_session_id=session_id,
    )
    duplicate_a = _clone_candidate(
        base_candidate,
        candidate_id="duplicate-target-a",
        target_section_id="top-papers",
        target_section_title="Top Papers",
        editing_session_id=session_id,
    )
    duplicate_b = _clone_candidate(
        base_candidate,
        candidate_id="duplicate-target-b",
        target_section_id="top-papers",
        target_section_title="Top Papers",
        editing_session_id=session_id,
    )

    _write_staged_candidates(
        isolated_run_repo,
        [
            safe_candidate,
            warn_candidate,
            recoverable_candidate,
            confirmation_candidate,
            duplicate_a,
            duplicate_b,
        ],
    )
    _update_session_queue(
        isolated_run_repo,
        [
            "safe-candidate",
            "warn-legacy-candidate",
            "recoverable-source-candidate",
            "confirmation-candidate",
            "duplicate-target-a",
            "duplicate-target-b",
        ],
        outline_hash="stale-outline-hash",
        pending_extra_ids=["missing-queue-dependency"],
    )

    response = client.post(
        f"/runs/{isolated_run_repo['run_id']}/workspace-actions/preflight-apply-staged",
        json={"editing_session_id": session_id, "apply_all": True},
    )

    assert response.status_code == 200, response.text

    payload = WorkspaceApplyStagedPreflightResponse.model_validate(response.json())
    conflict_kinds = {
        conflict.conflict_kind
        for group in [*payload.grouped_warnings, *payload.grouped_blockers]
        for conflict in group.conflicts
    }

    assert payload.status == "blocked"
    assert "safe-candidate" in payload.safe_candidate_ids
    assert "warn-legacy-candidate" in payload.warn_candidate_ids
    assert "recoverable-source-candidate" in payload.rebaseable_candidate_ids
    assert "confirmation-candidate" in payload.confirmation_required_ids
    assert {"duplicate-target-a", "duplicate-target-b"}.issubset(set(payload.blocked_candidate_ids))
    assert any(group.conflict_type == "duplicate_target_mutation" for group in payload.queue_conflict_groups)
    assert any(item.conflict_summary.total_conflicts >= 1 for item in payload.items)
    assert "stale_outline_hash" in conflict_kinds
    assert "dropped_dependency" in conflict_kinds
    assert payload.recommended_recovery_plan.mode == "rebase_staged_candidates"
    assert any(group.candidate_summaries for group in [*payload.grouped_warnings, *payload.grouped_blockers])


def test_patch_detail_degrades_gracefully_for_missing_target_and_source(client: TestClient, isolated_run_repo: dict[str, Any]) -> None:
    staged_path = _staged_actions_path(isolated_run_repo)
    base_candidate = _read_json(staged_path)[0]
    session_id = isolated_run_repo["session_id"]

    missing_target_candidate = _clone_candidate(
        base_candidate,
        candidate_id="missing-target-candidate",
        target_section_id="missing-target-section",
        target_section_title="Missing Target Section",
        editing_session_id=session_id,
    )
    missing_source_candidate = _clone_candidate(
        base_candidate,
        candidate_id="missing-source-candidate",
        source_section_id="missing-source-section",
        source_section_title="Missing Source Section",
        target_section_id="trend-signals",
        target_section_title="Trend Signals",
        editing_session_id=session_id,
    )

    _write_staged_candidates(isolated_run_repo, [missing_target_candidate, missing_source_candidate])
    _update_session_queue(isolated_run_repo, ["missing-target-candidate", "missing-source-candidate"])

    staged_response = client.get(
        f"/runs/{isolated_run_repo['run_id']}/workspace-actions/staged",
        params={"editing_session_id": session_id},
    )
    missing_target_response = client.get(
        f"/runs/{isolated_run_repo['run_id']}/workspace-actions/staged/missing-target-candidate/patch",
        params={"editing_session_id": session_id},
    )
    missing_source_response = client.get(
        f"/runs/{isolated_run_repo['run_id']}/workspace-actions/staged/missing-source-candidate/patch",
        params={"editing_session_id": session_id},
    )

    assert staged_response.status_code == 200, staged_response.text
    assert missing_target_response.status_code == 200, missing_target_response.text
    assert missing_source_response.status_code == 200, missing_source_response.text

    staged = WorkspaceStagedActionsResponse.model_validate(staged_response.json())
    missing_target_patch = StructuredPatchResponse.model_validate(missing_target_response.json())
    missing_source_patch = StructuredPatchResponse.model_validate(missing_source_response.json())

    assert all(item.conflict_summary is not None for item in staged.items)
    assert "missing_target_section" in {conflict.conflict_kind for conflict in missing_target_patch.conflict_details}
    assert {"missing_source_section", "source_artifact_missing"} & {
        conflict.conflict_kind for conflict in missing_source_patch.conflict_details
    }
    assert missing_target_patch.candidate_conflict_summary.status in {"blocked", "recoverable_by_rebase"}
    assert missing_source_patch.candidate_conflict_summary.status in {"warn", "blocked", "recoverable_by_rebase"}


def test_recover_preview_is_non_mutating_and_surfaces_rebase_diff(client: TestClient, isolated_run_repo: dict[str, Any]) -> None:
    staged_path = _staged_actions_path(isolated_run_repo)
    sessions_path = _editing_sessions_path(isolated_run_repo)
    base_candidate = _read_json(staged_path)[0]
    session_id = isolated_run_repo["session_id"]

    recoverable_candidate = _clone_candidate(
        base_candidate,
        candidate_id="recoverable-rebase-candidate",
        source_section_id="missing-source-section",
        source_section_title="Missing Source Section",
        target_section_id="research-gaps",
        target_section_title="Research Gaps / Open Questions",
        editing_session_id=session_id,
    )
    _write_staged_candidates(isolated_run_repo, [recoverable_candidate])
    _update_session_queue(isolated_run_repo, ["recoverable-rebase-candidate"], outline_hash="stale-outline-hash")

    staged_before = staged_path.read_text(encoding="utf-8")
    sessions_before = sessions_path.read_text(encoding="utf-8")

    response = client.post(
        f"/runs/{isolated_run_repo['run_id']}/editing-sessions/{session_id}/recover-preview",
        json={"mode": "rebase_staged_candidates"},
    )

    assert response.status_code == 200, response.text
    assert staged_path.read_text(encoding="utf-8") == staged_before
    assert sessions_path.read_text(encoding="utf-8") == sessions_before

    preview = EditingSessionRecoveryPreviewResponse.model_validate(response.json())

    assert preview.recovery_mode == "rebase_staged_candidates"
    assert preview.candidate_results
    assert preview.candidate_results[0].rebase_diff is not None
    assert preview.projected_preflight_summary is not None
    for item in preview.candidate_results:
        if item.candidate_id in preview.rebased_candidate_ids:
            assert item.result == "rebased"
        if item.candidate_id in preview.dropped_candidate_ids:
            assert item.result == "dropped"
        if item.candidate_id in preview.preserved_candidate_ids:
            assert item.result == "preserved"


def test_recover_apply_rebases_without_preview_only_state(client: TestClient, isolated_run_repo: dict[str, Any]) -> None:
    staged_path = _staged_actions_path(isolated_run_repo)
    base_candidate = _read_json(staged_path)[0]
    session_id = isolated_run_repo["session_id"]

    recoverable_candidate = _clone_candidate(
        base_candidate,
        candidate_id="recoverable-apply-candidate",
        source_section_id="missing-source-section",
        source_section_title="Missing Source Section",
        target_section_id="research-gaps",
        target_section_title="Research Gaps / Open Questions",
        editing_session_id=session_id,
    )
    _write_staged_candidates(isolated_run_repo, [recoverable_candidate])
    _update_session_queue(isolated_run_repo, ["recoverable-apply-candidate"], outline_hash="stale-outline-hash")

    response = client.post(
        f"/runs/{isolated_run_repo['run_id']}/editing-sessions/{session_id}/recover",
        json={"mode": "rebase_staged_candidates"},
    )

    assert response.status_code == 200, response.text

    payload = EditingSessionRecoverResponse.model_validate(response.json())

    assert payload.mode == "rebase_staged_candidates"
    assert payload.session.session_id == session_id
    assert payload.staged.run_id == isolated_run_repo["run_id"]
    assert payload.staleness.session_id == session_id


def test_recovery_preview_exposes_preserved_and_dropped_results(client: TestClient, isolated_run_repo: dict[str, Any]) -> None:
    run_id = isolated_run_repo["run_id"]
    session_id = isolated_run_repo["session_id"]

    refresh_response = client.post(
        f"/runs/{run_id}/editing-sessions/{session_id}/recover-preview",
        json={"mode": "refresh_session_metadata"},
    )
    discard_response = client.post(
        f"/runs/{run_id}/editing-sessions/{session_id}/recover-preview",
        json={"mode": "discard_staged_candidates"},
    )

    assert refresh_response.status_code == 200, refresh_response.text
    assert discard_response.status_code == 200, discard_response.text

    refresh_preview = EditingSessionRecoveryPreviewResponse.model_validate(refresh_response.json())
    discard_preview = EditingSessionRecoveryPreviewResponse.model_validate(discard_response.json())

    assert refresh_preview.candidate_results
    assert discard_preview.candidate_results
    assert all(item.result == "preserved" for item in refresh_preview.candidate_results)
    assert all(item.result == "dropped" for item in discard_preview.candidate_results)


def test_history_focus_restore_hints_propagate_and_degrade_gracefully(client: TestClient, isolated_run_repo: dict[str, Any]) -> None:
    run_id = isolated_run_repo["run_id"]
    base_action_id = isolated_run_repo["history_action_id"]
    base_response = client.get(f"/runs/{run_id}/outline-action-history/{base_action_id}/focus-context")

    assert base_response.status_code == 200, base_response.text

    focus = OutlineActionHistoryFocusContextResponse.model_validate(base_response.json())

    assert focus.ready is True
    assert focus.focus_section_id == "executive-summary"
    assert focus.route_restore_hints.compare_view == "report"

    history_path = _history_path(isolated_run_repo)
    history = _read_json(history_path)
    degraded_entry = deepcopy(history[0])
    degraded_entry["action_id"] = "history-missing-artifacts"
    degraded_entry["source_run_id"] = "missing-source-run"
    degraded_entry["compare_partner_run_id"] = "missing-compare-run"
    degraded_entry.setdefault("provenance", {})
    degraded_entry["provenance"]["source_run_id"] = "missing-source-run"
    degraded_entry["provenance"]["compare_partner_run_id"] = "missing-compare-run"
    degraded_entry["affected_sections"][0]["related_paper_ids"] = ["2601.18372v1"]
    history.insert(0, degraded_entry)
    _write_json(history_path, history)

    degraded_response = client.get(f"/runs/{run_id}/outline-action-history/history-missing-artifacts/focus-context")

    assert degraded_response.status_code == 200, degraded_response.text

    degraded = OutlineActionHistoryFocusContextResponse.model_validate(degraded_response.json())

    assert degraded.ready is True
    assert degraded.related_paper_ids == ["2601.18372v1"]
    assert degraded.source_artifacts_available is False
    assert degraded.route_restore_hints.focus_paper_id == "2601.18372v1"
    assert degraded.route_restore_hints.warnings


def test_history_diff_supports_single_between_and_current_draft_modes(client: TestClient, isolated_run_repo: dict[str, Any]) -> None:
    run_id = isolated_run_repo["run_id"]
    history = _read_json(_history_path(isolated_run_repo))
    first_action_id = history[0]["action_id"]
    second_action_id = history[1]["action_id"]

    single_response = client.get(f"/runs/{run_id}/outline-action-history/diff", params={"to_action_id": first_action_id})
    between_response = client.get(
        f"/runs/{run_id}/outline-action-history/diff",
        params={"from_action_id": first_action_id, "to_action_id": second_action_id},
    )
    current_draft_response = client.get(
        f"/runs/{run_id}/outline-action-history/diff",
        params={"to_action_id": second_action_id, "against_current_draft": True},
    )

    assert single_response.status_code == 200, single_response.text
    assert between_response.status_code == 200, between_response.text
    assert current_draft_response.status_code == 200, current_draft_response.text

    single = OutlineActionHistoryDiffResponse.model_validate(single_response.json())
    between = OutlineActionHistoryDiffResponse.model_validate(between_response.json())
    current = OutlineActionHistoryDiffResponse.model_validate(current_draft_response.json())

    assert single.items
    assert between.items
    assert current.ready is True

    history_path = _history_path(isolated_run_repo)
    modified_history = _read_json(history_path)
    fallback_entry = deepcopy(modified_history[-1])
    fallback_entry["action_id"] = "history-fallback-diff"
    fallback_entry["affected_sections"] = [
        {
            "section_id": "executive-summary",
            "title": "Executive Summary",
            "before_excerpt": None,
            "after_excerpt": None,
            "before_paper_ids": [],
            "after_paper_ids": [],
            "changed_block_count": 0,
            "completeness": "metadata_only",
            "preview_blocks": [],
            "warnings": ["Structured section snapshots are unavailable for this history entry."],
        }
    ]
    modified_history.append(fallback_entry)
    _write_json(history_path, modified_history)

    fallback_response = client.get(
        f"/runs/{run_id}/outline-action-history/diff",
        params={"to_action_id": "history-fallback-diff"},
    )

    assert fallback_response.status_code == 200, fallback_response.text

    fallback = OutlineActionHistoryDiffResponse.model_validate(fallback_response.json())

    assert fallback.items
    assert fallback.warnings or any(item.warnings for item in fallback.items) or fallback.ready is False
