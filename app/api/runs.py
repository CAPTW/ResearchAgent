from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import Field

from app.api.temporal_analysis import DecisionRequest
from app.auth.resource_access import resource_actor_for_user
from app.repositories.file_locks import FileLockError
from app.repositories.jsonl import JsonlIntegrityError
from app.services import report_review
from fastapi.responses import FileResponse, StreamingResponse

from app.repositories.artifact_refs import normalize_payload_references
from app.auth.security import (
    ensure_runs_access,
    list_runs_for_request,
    owner_user_id_for_request,
    require_product_user,
    require_run_read_access,
    require_run_write_access,
)
from app.jobs.streams import iter_sse_events
from app.schemas.compare import (
    CompareCandidatesResponse,
    CompareReportDetailResponse,
    RerunPresetCreateRequest,
    RerunPresetDeleteResponse,
    RerunPresetListResponse,
    RunCompareResponse,
    RunCompareContextResponse,
    RunLineageResponse,
    RunRerunRequest,
    RunRerunResponse,
)
from app.schemas.runs import RunCreateRequest, RunExportRequest
from app.schemas.workbench import (
    ExportListResponse,
    CitationTraceResponse,
    OutlineResponse,
    OutlineUpdateRequest,
    PaperDetailResponse,
    PaperListResponse,
    PaperReviewActionResponse,
    PaperReviewActionUpdateRequest,
    PreviewResponse,
    ReviewResponse,
)
from app.schemas.workspace import (
    EditingSessionCreateRequest,
    EditingSessionDeleteResponse,
    EditingSessionRecoveryPreviewResponse,
    EditingSessionRecoverRequest,
    EditingSessionRecoverResponse,
    EditingSessionResponse,
    EditingSessionsResponse,
    EditingSessionStalenessResponse,
    OutlineActionHistoryDiffResponse,
    OutlineActionHistoryFocusContextResponse,
    OutlineActionHistoryResponse,
    WorkspaceActionApplyRequest,
    WorkspaceActionApplyResponse,
    WorkspaceApplyStagedPreflightRequest,
    WorkspaceApplyStagedPreflightResponse,
    WorkspaceActionApplyStagedRequest,
    WorkspaceActionApplyStagedResponse,
    WorkspaceActionPreviewRequest,
    WorkspaceActionPreviewResponse,
    WorkspaceActionReorderRequest,
    WorkspaceActionStageRequest,
    WorkspaceStagedActionsResponse,
    StructuredPatchResponse,
    WorkspaceSuggestionsResponse,
    WorkspaceViewCreateRequest,
    WorkspaceViewDeleteResponse,
    WorkspaceViewsResponse,
)
from app.services import compare, compare_context, editing_sessions, exports, outline_history, report_diff, report_versions, reporting, rerun_presets, reviews, workbench, workspace_actions, workspace_views
from app.services.runs import get_run, get_run_report, get_run_report_path, list_runs, rerun_run, start_run

router = APIRouter(tags=["runs"])


class ReportDecisionRequest(DecisionRequest):
    expected_binding: str = Field(pattern=r"^[a-f0-9]{64}$")


def _review_operation(operation):
    try:
        return operation()
    except FileNotFoundError:
        raise HTTPException(404, detail="report_version_not_found") from None
    except (FileLockError, JsonlIntegrityError, OSError):
        raise HTTPException(503, detail="report_review_unavailable") from None
    except ValueError as exc:
        code = str(exc)
        conflicts = {"decision_conflict", "decision_request_conflict", "report_binding_changed", "report_not_eligible"}
        if code in conflicts:
            raise HTTPException(409, detail=code) from None
        raise HTTPException(503, detail="report_review_unavailable") from None


@router.get("/runs/{run_id}/report/versions/{version_id}/decisions")
def get_report_decisions(run_id: str, version_id: str, limit: int = Query(10, ge=1, le=100),
                         page: int = Query(1, ge=1), _user=Depends(require_run_read_access)):
    return _review_operation(lambda: report_review.history(run_id, version_id, limit, page))


@router.post("/runs/{run_id}/report/versions/{version_id}/decisions")
def save_report_decision(run_id: str, version_id: str, body: ReportDecisionRequest,
                         response: Response, user=Depends(require_run_write_access)):
    def operation():
        actor = resource_actor_for_user(user)
        decision, created = report_review.decide(run_id, version_id, **body.model_dump(),
                                                actor_id=actor.subject or actor.actor_type)
        response.status_code = 201 if created else 200
        return dict(decision=decision, created=created)
    return _review_operation(operation)


def _public_artifact_refs(payload, *, run_id: str):
    """Keep internal absolute artifact paths out of product JSON responses."""

    return normalize_payload_references(payload, run_id=run_id)


@router.post("/runs")
def create_run(request: RunCreateRequest, user=Depends(require_product_user)):
    return start_run(request, owner_user_id=owner_user_id_for_request(user))


@router.get("/runs")
def get_runs(request: Request):
    return list_runs_for_request(request, list_runs())


def _resolve_compare_run_ids(
    left_run_id: str | None,
    right_run_id: str | None,
    run_a: str | None,
    run_b: str | None,
) -> tuple[str, str]:
    left = left_run_id or run_a
    right = right_run_id or run_b
    if not left or not right:
        raise HTTPException(status_code=400, detail="Compare requires left_run_id/right_run_id or runA/runB.")
    return left, right


@router.get("/compare", response_model=RunCompareResponse)
def get_run_compare(
    request: Request,
    left_run_id: str | None = Query(default=None),
    right_run_id: str | None = Query(default=None),
    run_a: str | None = Query(default=None, alias="runA"),
    run_b: str | None = Query(default=None, alias="runB"),
):
    try:
        left_run_id, right_run_id = _resolve_compare_run_ids(left_run_id, right_run_id, run_a, run_b)
        ensure_runs_access(request, [left_run_id, right_run_id])
        return compare.compare_runs(left_run_id, right_run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/compare/report", response_model=CompareReportDetailResponse)
def get_compare_report_detail(
    request: Request,
    left_run_id: str | None = Query(default=None),
    right_run_id: str | None = Query(default=None),
    run_a: str | None = Query(default=None, alias="runA"),
    run_b: str | None = Query(default=None, alias="runB"),
):
    try:
        left_run_id, right_run_id = _resolve_compare_run_ids(left_run_id, right_run_id, run_a, run_b)
        ensure_runs_access(request, [left_run_id, right_run_id])
        return report_diff.build_report_diff(left_run_id, right_run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}")
def get_run_detail(run_id: str, _user=Depends(require_run_read_access)):
    try:
        return get_run(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/papers", response_model=PaperListResponse)
def get_run_paper_list(
    run_id: str,
    _user=Depends(require_run_read_access),
    page: int | None = Query(default=None, ge=1),
    page_size: int | None = Query(default=None, ge=1, le=100),
    q: str = "",
    status: str | None = None,
    theme: str | None = None,
    score_band: str | None = None,
    provider: str | None = None,
    dedup: str = Query(default="all"),
):
    try:
        resolved_page = page or 1
        if page_size is None:
            total_items = len(workbench.load_run_triage_items(run_id))
            resolved_page_size = max(total_items, 1)
        else:
            resolved_page_size = page_size
        return _public_artifact_refs(
            workbench.list_run_papers(
                run_id,
                page=resolved_page,
                page_size=resolved_page_size,
                q=q,
                status=status,
                theme=theme,
                score_band=score_band,
                provider=provider,
                dedup=dedup,
            ),
            run_id=run_id,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/papers/{paper_id}", response_model=PaperDetailResponse)
def get_run_paper_detail(run_id: str, paper_id: str, _user=Depends(require_run_read_access)):
    try:
        return _public_artifact_refs(workbench.get_run_paper_detail(run_id, paper_id), run_id=run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/runs/{run_id}/papers/{paper_id}", response_model=PaperReviewActionResponse)
def patch_run_paper_review_action(
    run_id: str,
    paper_id: str,
    request: PaperReviewActionUpdateRequest,
    _user=Depends(require_run_write_access),
):
    try:
        return workbench.update_paper_review_action(run_id, paper_id, request.model_dump(exclude_unset=True))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/runs/{run_id}/papers/{paper_id}/review", response_model=ReviewResponse)
def get_run_paper_review(run_id: str, paper_id: str, _user=Depends(require_run_read_access)):
    try:
        return _public_artifact_refs(
            reviews.load_review_payload(run_id, workbench.get_raw_paper(run_id, paper_id)),
            run_id=run_id,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/events")
async def get_run_events(run_id: str, _user=Depends(require_run_read_access)):
    try:
        get_run(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(iter_sse_events(run_id), media_type="text/event-stream", headers=headers)


@router.get("/runs/{run_id}/report")
def get_report(run_id: str, _user=Depends(require_run_read_access)):
    try:
        return _public_artifact_refs(get_run_report(run_id), run_id=run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/report/download")
def download_report(run_id: str, _user=Depends(require_run_read_access)):
    try:
        report_path = get_run_report_path(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if not report_path:
        raise HTTPException(status_code=404, detail=f"No report markdown found for run {run_id}")
    return FileResponse(report_path, media_type="text/markdown; charset=utf-8", filename=Path(report_path).name)


@router.get("/runs/{run_id}/report/versions")
def get_report_versions(run_id: str, _user=Depends(require_run_read_access)):
    try:
        return report_versions.list_report_versions(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/report/versions/diff")
def get_report_version_diff(
    run_id: str,
    _user=Depends(require_run_read_access),
    from_version_id: str | None = None,
    to_version_id: str | None = None,
):
    try:
        return report_versions.diff_report_versions(run_id, from_version_id=from_version_id, to_version_id=to_version_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/lineage", response_model=RunLineageResponse)
def get_lineage(run_id: str, _user=Depends(require_run_read_access)):
    try:
        return compare.get_run_lineage(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/compare-candidates", response_model=CompareCandidatesResponse)
def get_compare_candidates(run_id: str, _user=Depends(require_run_read_access)):
    try:
        return compare.list_compare_candidates(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/compare-context", response_model=RunCompareContextResponse)
def get_run_compare_context(
    run_id: str,
    _user=Depends(require_run_read_access),
    editing_session_id: str | None = None,
    workspace_view_id: str | None = None,
    compare_partner_run_id: str | None = None,
    compare_mode: str | None = None,
    compare_view: str | None = None,
    selection_bucket_id: str | None = None,
    preset_id: str | None = None,
    focus_paper_id: str | None = None,
    focus_theme: str | None = None,
    focus_section_id: str | None = None,
    focus_section_title: str | None = None,
    preferred_report_side: str | None = None,
    selection_source: str | None = None,
    selected_paper_ids: str | None = None,
    seed_section: bool = False,
):
    try:
        return compare_context.resolve_run_compare_context(
            run_id,
            editing_session_id=editing_session_id,
            workspace_view_id=workspace_view_id,
            compare_partner_run_id=compare_partner_run_id,
            compare_mode=compare_mode,
            compare_view=compare_view,
            selection_bucket_id=selection_bucket_id,
            preset_id=preset_id,
            focus_paper_id=focus_paper_id,
            focus_theme=focus_theme,
            focus_section_id=focus_section_id,
            focus_section_title=focus_section_title,
            preferred_report_side=preferred_report_side,
            selection_source=selection_source,
            selected_paper_ids=selected_paper_ids,
            seed_section=seed_section,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/workspace-suggestions", response_model=WorkspaceSuggestionsResponse)
def get_workspace_suggestions(
    run_id: str,
    _user=Depends(require_run_read_access),
    editing_session_id: str | None = None,
    workspace_view_id: str | None = None,
    compare_partner_run_id: str | None = None,
    compare_mode: str | None = None,
    compare_view: str | None = None,
    selection_bucket_id: str | None = None,
    preset_id: str | None = None,
    focus_theme: str | None = None,
    focus_section_id: str | None = None,
    focus_section_title: str | None = None,
    preferred_report_side: str | None = None,
    selection_source: str | None = None,
    selected_paper_ids: str | None = None,
):
    try:
        return workspace_actions.list_workspace_suggestions(
            run_id,
            editing_session_id=editing_session_id,
            workspace_view_id=workspace_view_id,
            compare_partner_run_id=compare_partner_run_id,
            compare_mode=compare_mode,
            compare_view=compare_view,
            selection_bucket_id=selection_bucket_id,
            preset_id=preset_id,
            focus_theme=focus_theme,
            focus_section_id=focus_section_id,
            focus_section_title=focus_section_title,
            preferred_report_side=preferred_report_side,
            selection_source=selection_source,
            selected_paper_ids=selected_paper_ids,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/workspace-actions/preview", response_model=WorkspaceActionPreviewResponse)
def preview_workspace_action(run_id: str, request: WorkspaceActionPreviewRequest, _user=Depends(require_run_write_access)):
    try:
        return workspace_actions.preview_workspace_action(run_id, request.model_dump(exclude_none=True))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/runs/{run_id}/workspace-actions/apply", response_model=WorkspaceActionApplyResponse)
def apply_workspace_action(run_id: str, request: WorkspaceActionApplyRequest, _user=Depends(require_run_write_access)):
    try:
        return workspace_actions.apply_workspace_action(run_id, request.model_dump(exclude_none=True))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/runs/{run_id}/workspace-actions/staged", response_model=WorkspaceStagedActionsResponse)
def get_staged_workspace_actions(run_id: str, editing_session_id: str | None = None, _user=Depends(require_run_read_access)):
    try:
        return workspace_actions.list_staged_workspace_actions(run_id, editing_session_id=editing_session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/workspace-actions/staged/{action_id}/patch", response_model=StructuredPatchResponse)
def get_staged_workspace_action_patch(
    run_id: str,
    action_id: str,
    editing_session_id: str | None = None,
    _user=Depends(require_run_read_access),
):
    try:
        return workspace_actions.get_staged_workspace_action_patch(run_id, action_id, editing_session_id=editing_session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/workspace-actions/stage", response_model=WorkspaceStagedActionsResponse)
def stage_workspace_action(run_id: str, request: WorkspaceActionStageRequest, _user=Depends(require_run_write_access)):
    try:
        candidate = workspace_actions.stage_workspace_action(run_id, request.model_dump(exclude_none=True))
        return workspace_actions.list_staged_workspace_actions(run_id, editing_session_id=candidate.get("editing_session_id"))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/runs/{run_id}/workspace-actions/apply-staged", response_model=WorkspaceActionApplyStagedResponse)
def apply_staged_workspace_actions(run_id: str, request: WorkspaceActionApplyStagedRequest, _user=Depends(require_run_write_access)):
    try:
        return workspace_actions.apply_staged_workspace_actions(
            run_id,
            editing_session_id=request.editing_session_id,
            action_ids=request.action_ids,
            apply_all=request.apply_all,
            confirm_destructive_action_ids=request.confirm_destructive_action_ids,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/runs/{run_id}/workspace-actions/preflight-apply-staged", response_model=WorkspaceApplyStagedPreflightResponse)
def preflight_apply_staged_workspace_actions(
    run_id: str,
    request: WorkspaceApplyStagedPreflightRequest,
    _user=Depends(require_run_write_access),
):
    try:
        return workspace_actions.preflight_apply_staged_workspace_actions(
            run_id,
            editing_session_id=request.editing_session_id,
            action_ids=request.action_ids,
            apply_all=request.apply_all,
            confirm_destructive_action_ids=request.confirm_destructive_action_ids,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/runs/{run_id}/workspace-actions/staged/reorder", response_model=WorkspaceStagedActionsResponse)
def reorder_staged_workspace_actions(run_id: str, request: WorkspaceActionReorderRequest, _user=Depends(require_run_write_access)):
    try:
        return workspace_actions.reorder_staged_workspace_actions(
            run_id,
            editing_session_id=request.editing_session_id,
            ordered_action_ids=request.ordered_action_ids,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/runs/{run_id}/workspace-actions/staged/{action_id}", response_model=WorkspaceStagedActionsResponse)
def delete_staged_workspace_action(
    run_id: str,
    action_id: str,
    editing_session_id: str | None = None,
    _user=Depends(require_run_write_access),
):
    try:
        return workspace_actions.delete_staged_workspace_action(run_id, action_id, editing_session_id=editing_session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/workspace-views", response_model=WorkspaceViewsResponse)
def get_workspace_views(run_id: str, _user=Depends(require_run_read_access)):
    try:
        return workspace_views.list_workspace_views(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/editing-sessions", response_model=EditingSessionsResponse)
def get_editing_sessions(run_id: str, _user=Depends(require_run_read_access)):
    try:
        return editing_sessions.list_editing_sessions(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/editing-sessions", response_model=EditingSessionResponse)
def create_editing_session(run_id: str, request: EditingSessionCreateRequest, _user=Depends(require_run_write_access)):
    try:
        session = editing_sessions.create_editing_session(run_id, request.model_dump(exclude_none=True))
        if session.get("staged_action_ids"):
            workspace_actions.attach_staged_actions_to_editing_session(run_id, session["session_id"], session.get("staged_action_ids", []))
            session = editing_sessions.get_editing_session(run_id, session["session_id"])
        return session
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/editing-sessions/{session_id}", response_model=EditingSessionResponse)
def get_editing_session(run_id: str, session_id: str, _user=Depends(require_run_read_access)):
    try:
        return editing_sessions.get_editing_session(run_id, session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/editing-sessions/{session_id}/staleness", response_model=EditingSessionStalenessResponse)
def get_editing_session_staleness(run_id: str, session_id: str, _user=Depends(require_run_read_access)):
    try:
        return workspace_actions.inspect_editing_session_staleness(run_id, session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/editing-sessions/{session_id}/recover", response_model=EditingSessionRecoverResponse)
def recover_editing_session(
    run_id: str,
    session_id: str,
    request: EditingSessionRecoverRequest,
    _user=Depends(require_run_write_access),
):
    try:
        return workspace_actions.recover_editing_session(run_id, session_id, mode=request.mode)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/runs/{run_id}/editing-sessions/{session_id}/recover-preview", response_model=EditingSessionRecoveryPreviewResponse)
def preview_editing_session_recovery(
    run_id: str,
    session_id: str,
    request: EditingSessionRecoverRequest,
    _user=Depends(require_run_write_access),
):
    try:
        return workspace_actions.preview_editing_session_recovery(run_id, session_id, mode=request.mode)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/runs/{run_id}/editing-sessions/{session_id}", response_model=EditingSessionDeleteResponse)
def delete_editing_session(run_id: str, session_id: str, _user=Depends(require_run_write_access)):
    try:
        session = editing_sessions.get_editing_session(run_id, session_id)
        if session.get("staged_action_ids"):
            workspace_actions.delete_staged_actions_for_session(run_id, session_id)
        return editing_sessions.delete_editing_session(run_id, session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/outline-action-history", response_model=OutlineActionHistoryResponse)
def get_outline_action_history(run_id: str, _user=Depends(require_run_read_access)):
    try:
        return outline_history.list_outline_action_history(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/outline-action-history/diff", response_model=OutlineActionHistoryDiffResponse)
def get_outline_action_history_diff(
    run_id: str,
    _user=Depends(require_run_read_access),
    from_action_id: str | None = None,
    to_action_id: str | None = None,
    against_current_draft: bool = False,
):
    try:
        return outline_history.get_outline_action_history_diff(
            run_id,
            from_action_id=from_action_id,
            to_action_id=to_action_id,
            against_current_draft=against_current_draft,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/outline-action-history/{action_id}/focus-context", response_model=OutlineActionHistoryFocusContextResponse)
def get_outline_action_history_focus_context(run_id: str, action_id: str, _user=Depends(require_run_read_access)):
    try:
        return outline_history.get_outline_action_focus_context(run_id, action_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/workspace-views", response_model=WorkspaceViewsResponse)
def create_workspace_view(run_id: str, request: WorkspaceViewCreateRequest, _user=Depends(require_run_write_access)):
    try:
        workspace_views.create_workspace_view(run_id, request.model_dump(exclude_none=True))
        return workspace_views.list_workspace_views(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/runs/{run_id}/workspace-views/{view_id}", response_model=WorkspaceViewDeleteResponse)
def delete_workspace_view(run_id: str, view_id: str, _user=Depends(require_run_write_access)):
    try:
        return workspace_views.delete_workspace_view(run_id, view_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/outline", response_model=OutlineResponse)
def get_outline(run_id: str, _user=Depends(require_run_read_access)):
    try:
        return _public_artifact_refs(reporting.load_outline(run_id), run_id=run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/runs/{run_id}/outline", response_model=OutlineResponse)
def patch_outline(run_id: str, request: OutlineUpdateRequest, _user=Depends(require_run_write_access)):
    try:
        return _public_artifact_refs(
            reporting.save_outline(run_id, [section.model_dump() for section in request.sections]),
            run_id=run_id,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/reports/{report_id}/outline", response_model=OutlineResponse)
def get_report_outline(report_id: str, request: Request):
    try:
        ensure_runs_access(request, [report_id])
        return _public_artifact_refs(reporting.load_outline(report_id), run_id=report_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/reports/{report_id}/outline", response_model=OutlineResponse)
def patch_report_outline(report_id: str, outline_request: OutlineUpdateRequest, request: Request):
    try:
        ensure_runs_access(request, [report_id])
        return _public_artifact_refs(
            reporting.save_outline(report_id, [section.model_dump() for section in outline_request.sections]),
            run_id=report_id,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/preview", response_model=PreviewResponse)
def get_preview(run_id: str, _user=Depends(require_run_read_access)):
    try:
        return _public_artifact_refs(reporting.build_preview(run_id), run_id=run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/reports/{report_id}/preview", response_model=PreviewResponse)
def get_report_preview(report_id: str, request: Request):
    try:
        ensure_runs_access(request, [report_id])
        return _public_artifact_refs(reporting.build_preview(report_id), run_id=report_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/reports/{report_id}/versions")
def get_report_versions_alias(report_id: str, request: Request):
    try:
        ensure_runs_access(request, [report_id])
        return report_versions.list_report_versions(report_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/reports/{report_id}/versions")
def get_api_report_versions(report_id: str, request: Request):
    try:
        ensure_runs_access(request, [report_id])
        return report_versions.list_report_versions(report_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/reports/{report_id}/versions/diff")
def get_report_version_diff_alias(
    report_id: str,
    request: Request,
    from_version_id: str | None = None,
    to_version_id: str | None = None,
):
    try:
        ensure_runs_access(request, [report_id])
        return report_versions.diff_report_versions(report_id, from_version_id=from_version_id, to_version_id=to_version_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/reports/{report_id}/versions/diff")
def get_api_report_version_diff(
    report_id: str,
    request: Request,
    from_version_id: str | None = None,
    to_version_id: str | None = None,
):
    try:
        ensure_runs_access(request, [report_id])
        return report_versions.diff_report_versions(report_id, from_version_id=from_version_id, to_version_id=to_version_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/reports/{report_id}/versions/{version_a}/diff/{version_b}")
def get_report_version_path_diff_alias(report_id: str, version_a: str, version_b: str, request: Request):
    try:
        ensure_runs_access(request, [report_id])
        return report_versions.diff_report_versions(report_id, from_version_id=version_a, to_version_id=version_b)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/reports/{report_id}/versions/{version_a}/diff/{version_b}")
def get_api_report_version_path_diff(report_id: str, version_a: str, version_b: str, request: Request):
    try:
        ensure_runs_access(request, [report_id])
        return report_versions.diff_report_versions(report_id, from_version_id=version_a, to_version_id=version_b)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/reports/{report_id}/versions/{version_id}")
def get_report_version_alias(report_id: str, version_id: str, request: Request):
    try:
        ensure_runs_access(request, [report_id])
        return report_versions.get_report_version_detail(report_id, version_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/reports/{report_id}/versions/{version_id}")
def get_api_report_version(report_id: str, version_id: str, request: Request):
    try:
        ensure_runs_access(request, [report_id])
        return report_versions.get_report_version_detail(report_id, version_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/citations", response_model=CitationTraceResponse)
def get_run_citations(run_id: str, _user=Depends(require_run_read_access)):
    try:
        return _public_artifact_refs(reporting.build_citation_trace(run_id), run_id=run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/reports/{report_id}/citations", response_model=CitationTraceResponse)
def get_report_citations(report_id: str, request: Request):
    try:
        ensure_runs_access(request, [report_id])
        return _public_artifact_refs(reporting.build_citation_trace(report_id), run_id=report_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/export")
def export_run(run_id: str, request: RunExportRequest, _user=Depends(require_run_write_access)):
    try:
        if request.format == "report_markdown":
            payload = exports.export_report_markdown(run_id)
        elif request.format == "report_pdf":
            payload = exports.export_report_pdf(run_id)
        else:
            payload = exports.export_journal_pdf(run_id)
        return _public_artifact_refs(payload, run_id=run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/reports/{report_id}/export")
def export_report(report_id: str, export_request: RunExportRequest, request: Request):
    try:
        ensure_runs_access(request, [report_id])
        if export_request.format == "report_markdown":
            return _public_artifact_refs(exports.export_report_markdown(report_id), run_id=report_id)
        if export_request.format == "report_pdf":
            return _public_artifact_refs(exports.export_report_pdf(report_id), run_id=report_id)
        return _public_artifact_refs(exports.export_journal_pdf(report_id), run_id=report_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/runs/{run_id}/rerun", response_model=RunRerunResponse)
def create_child_run(run_id: str, request: RunRerunRequest, user=Depends(require_run_write_access)):
    try:
        return rerun_run(run_id, request, owner_user_id=owner_user_id_for_request(user))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/runs/{run_id}/rerun-presets", response_model=RerunPresetListResponse)
def get_rerun_presets(run_id: str, _user=Depends(require_run_read_access)):
    try:
        return rerun_presets.list_rerun_presets(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/rerun-presets", response_model=RerunPresetListResponse)
def create_rerun_preset(run_id: str, request: RerunPresetCreateRequest, _user=Depends(require_run_write_access)):
    try:
        rerun_presets.create_rerun_preset(run_id, request.model_dump(exclude_none=True))
        return rerun_presets.list_rerun_presets(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/runs/{run_id}/rerun-presets/{preset_id}", response_model=RerunPresetDeleteResponse)
def delete_rerun_preset(run_id: str, preset_id: str, _user=Depends(require_run_write_access)):
    try:
        return rerun_presets.delete_rerun_preset(run_id, preset_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/exports", response_model=ExportListResponse)
def get_run_exports(run_id: str, _user=Depends(require_run_read_access)):
    try:
        get_run(run_id)
        return _public_artifact_refs(exports.list_exports(run_id), run_id=run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/exports/{export_id}/download")
def download_run_export(run_id: str, export_id: str, _user=Depends(require_run_read_access)):
    try:
        output_path, media_type = exports.resolve_export_download(run_id, export_id)
        return FileResponse(output_path, media_type=media_type, filename=output_path.name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/reports/{report_id}/exports/{export_id}/download")
def download_report_export(report_id: str, export_id: str, request: Request):
    try:
        ensure_runs_access(request, [report_id])
        output_path, media_type = exports.resolve_export_download(report_id, export_id)
        return FileResponse(output_path, media_type=media_type, filename=output_path.name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
