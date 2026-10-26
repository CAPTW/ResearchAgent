from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.auth.resource_access import resource_actor_for_user
from app.auth.security import (
    owner_user_id_for_request,
    require_product_user,
    require_run_read_access,
    require_run_write_access,
)
from app.repositories.artifact_refs import normalize_payload_references
from app.schemas.prototype import (
    PrototypeProviderDecisionRequest,
    PrototypeProviderInvocationRequest,
    PrototypeProviderRouteRequest,
    PrototypeRealCorpusStartRequest,
    PrototypeStartResponse,
    PrototypeStatusResponse,
)
from app.schemas.prototype_workspace import (
    AttentionActionRequest,
    ChangesetDecisionRequest,
    CurationDeltaBriefingRequest,
    CurationPreviewRequest,
    GapAnalysisComparisonRequest,
    GapAnalysisRequest,
    InitialAcquisitionRequest,
    LivingScheduleDispatchRequest,
    LivingScheduleRequest,
    ProjectBundleExportConfirmRequest,
    ProjectBundleExportPreviewRequest,
    ProjectBundleImportConfirmRequest,
    RecommendationComparisonRequest,
    RecommendationDecisionRequest,
    RecommendationPromotionRequest,
    RefreshStartRequest,
    TopicCreateRequest,
    TopicRunStartRequest,
    TopicSettingsUpdateRequest,
)
from app.services import (
    prototype,
    prototype_living_topics,
    prototype_live_provider,
    prototype_project_bundle,
    prototype_real_corpus,
    prototype_research_gaps,
    prototype_workspace,
)

router = APIRouter(prefix="/prototype", tags=["prototype"])


def _raise_workspace_error(exc: prototype_workspace.PrototypeWorkspaceError) -> None:
    raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc


def _raise_bundle_error(exc: prototype_project_bundle.PrototypeProjectBundleError) -> None:
    raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc


@router.get("/workspace/bundles")
def list_project_bundles(_user=Depends(require_product_user)):
    try:
        return prototype_project_bundle.list_projects()
    except prototype_project_bundle.PrototypeProjectBundleError as exc:
        _raise_bundle_error(exc)


@router.post("/workspace/bundles/export/preview")
def preview_project_bundle_export(
    payload: ProjectBundleExportPreviewRequest,
    _user=Depends(require_product_user),
):
    try:
        return prototype_project_bundle.preview_export(payload.selected_topic_ids)
    except prototype_project_bundle.PrototypeProjectBundleError as exc:
        _raise_bundle_error(exc)


@router.post("/workspace/bundles/export")
def confirm_project_bundle_export(
    payload: ProjectBundleExportConfirmRequest,
    _user=Depends(require_product_user),
):
    try:
        return prototype_project_bundle.confirm_export(
            selected_topic_ids=payload.selected_topic_ids,
            preview_digest=payload.preview_digest,
            confirmed=payload.confirmed,
        )
    except prototype_project_bundle.PrototypeProjectBundleError as exc:
        _raise_bundle_error(exc)


@router.get("/workspace/bundles/exports/{bundle_ref}/download")
def download_project_bundle(bundle_ref: str, _user=Depends(require_product_user)):
    try:
        data = prototype_project_bundle.get_export_bytes(bundle_ref)
    except prototype_project_bundle.PrototypeProjectBundleError as exc:
        _raise_bundle_error(exc)
    return Response(
        content=data,
        media_type="application/vnd.xrml.research-project+zip",
        headers={"Content-Disposition": f'attachment; filename="{bundle_ref}.xrmlbundle"'},
    )


@router.post("/workspace/bundles/imports", status_code=202)
async def receive_project_bundle_import(request: Request, _user=Depends(require_product_user)):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > prototype_project_bundle.MAX_COMPRESSED_BYTES:
            raise HTTPException(status_code=413, detail={"code": "bundle_archive_compressed_limit_exceeded"})
    filename = request.headers.get("x-xrml-bundle-filename", "project.xrmlbundle")
    try:
        return prototype_project_bundle.receive_import(bytes(data), filename=filename)
    except prototype_project_bundle.PrototypeProjectBundleError as exc:
        _raise_bundle_error(exc)


@router.post("/workspace/bundles/imports/{import_id}/confirm")
def confirm_project_bundle_import(
    import_id: str,
    payload: ProjectBundleImportConfirmRequest,
    _user=Depends(require_product_user),
):
    try:
        return prototype_project_bundle.confirm_import(
            import_id,
            preview_digest=payload.preview_digest,
            confirmed=payload.confirmed,
        )
    except prototype_project_bundle.PrototypeProjectBundleError as exc:
        _raise_bundle_error(exc)


@router.get("/workspace/bundles/imports/{import_id}")
def get_project_bundle_import_status(import_id: str, _user=Depends(require_product_user)):
    try:
        return prototype_project_bundle.get_import_status(import_id)
    except prototype_project_bundle.PrototypeProjectBundleError as exc:
        _raise_bundle_error(exc)


@router.get("/workspace/bundles/projects/{project_id}")
def get_project_bundle(project_id: str, _user=Depends(require_product_user)):
    try:
        return prototype_project_bundle.get_project_detail(project_id)
    except prototype_project_bundle.PrototypeProjectBundleError as exc:
        _raise_bundle_error(exc)


@router.get("/workspace/bundles/runs/{run_id}")
def get_project_bundle_run(run_id: str, _user=Depends(require_product_user)):
    try:
        return prototype_project_bundle.get_portable_run(run_id)
    except prototype_project_bundle.PrototypeProjectBundleError as exc:
        _raise_bundle_error(exc)


@router.get("/workspace/bundles/runs/{run_id}/report-versions/{version_id}")
def get_project_bundle_report_version(
    run_id: str,
    version_id: str,
    _user=Depends(require_product_user),
):
    try:
        return prototype_project_bundle.get_portable_report_version(run_id, version_id)
    except prototype_project_bundle.PrototypeProjectBundleError as exc:
        _raise_bundle_error(exc)


@router.get("/workspace/bundles/runs/{run_id}/evidence/{evidence_id}")
def get_project_bundle_evidence(
    run_id: str,
    evidence_id: str,
    _user=Depends(require_product_user),
):
    try:
        return prototype_project_bundle.get_portable_evidence(run_id, evidence_id)
    except prototype_project_bundle.PrototypeProjectBundleError as exc:
        _raise_bundle_error(exc)


@router.get("/workspace")
def get_prototype_workspace(_user=Depends(require_product_user)):
    try:
        return prototype_workspace.get_workspace()
    except prototype.PrototypeModeError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code}) from exc
    except prototype_workspace.PrototypeWorkspaceError as exc:
        _raise_workspace_error(exc)


@router.post("/workspace/topics", status_code=201)
def create_workspace_topic(payload: TopicCreateRequest, _user=Depends(require_product_user)):
    try:
        return prototype_workspace.create_topic(payload.model_dump())
    except prototype_workspace.PrototypeWorkspaceError as exc:
        _raise_workspace_error(exc)


@router.get("/workspace/topics/{topic_id}")
def get_workspace_topic(topic_id: str, _user=Depends(require_product_user)):
    try:
        return prototype_workspace.get_topic(topic_id)
    except prototype_workspace.PrototypeWorkspaceError as exc:
        _raise_workspace_error(exc)


@router.patch("/workspace/topics/{topic_id}/settings")
def update_workspace_topic_settings(
    topic_id: str,
    payload: TopicSettingsUpdateRequest,
    _user=Depends(require_product_user),
):
    try:
        return prototype_workspace.update_topic_settings(topic_id, payload.model_dump(exclude_none=True))
    except prototype_workspace.PrototypeWorkspaceError as exc:
        _raise_workspace_error(exc)


@router.post("/workspace/topics/{topic_id}/curation/preview")
def preview_workspace_curation(
    topic_id: str,
    payload: CurationPreviewRequest,
    _user=Depends(require_product_user),
):
    try:
        return prototype_workspace.preview_curation(topic_id, payload.model_dump())
    except prototype_workspace.PrototypeWorkspaceError as exc:
        _raise_workspace_error(exc)


@router.post("/workspace/topics/{topic_id}/changesets/{changeset_id}/apply")
def apply_workspace_changeset(
    topic_id: str,
    changeset_id: str,
    payload: ChangesetDecisionRequest,
    _user=Depends(require_product_user),
):
    try:
        return prototype_workspace.apply_changeset(topic_id, changeset_id, confirmed=payload.confirmed)
    except prototype_workspace.PrototypeWorkspaceError as exc:
        _raise_workspace_error(exc)


@router.post("/workspace/topics/{topic_id}/changesets/{changeset_id}/discard")
def discard_workspace_changeset(
    topic_id: str,
    changeset_id: str,
    _user=Depends(require_product_user),
):
    try:
        return prototype_workspace.discard_changeset(topic_id, changeset_id)
    except prototype_workspace.PrototypeWorkspaceError as exc:
        _raise_workspace_error(exc)


@router.get("/workspace/topics/{topic_id}/snapshots/compare")
def compare_workspace_snapshots(
    topic_id: str,
    from_snapshot_id: str,
    to_snapshot_id: str,
    _user=Depends(require_product_user),
):
    try:
        return prototype_workspace.compare_snapshots(topic_id, from_snapshot_id, to_snapshot_id)
    except prototype_workspace.PrototypeWorkspaceError as exc:
        _raise_workspace_error(exc)


@router.post("/workspace/topics/{topic_id}/runs")
def start_workspace_topic_run(
    topic_id: str,
    payload: TopicRunStartRequest,
    user=Depends(require_product_user),
):
    if not payload.confirmed:
        raise HTTPException(status_code=400, detail={"code": "topic_run_confirmation_required"})
    try:
        return prototype_workspace.start_topic_run(
            topic_id,
            snapshot_id=payload.snapshot_id,
            actor=resource_actor_for_user(user),
            owner_user_id=owner_user_id_for_request(user),
        )
    except prototype_workspace.PrototypeWorkspaceError as exc:
        _raise_workspace_error(exc)


@router.post("/workspace/topics/{topic_id}/initial-acquisition")
def acquire_workspace_real_corpus(
    topic_id: str,
    payload: InitialAcquisitionRequest,
    user=Depends(require_product_user),
):
    if topic_id != prototype_workspace.REAL_TOPIC_ID:
        raise HTTPException(status_code=409, detail={"code": "topic_acquisition_not_eligible"})
    try:
        return prototype_workspace.acquire_initial_real_corpus(
            confirmed=payload.confirmed,
            network_enabled=payload.network_enabled,
            owner_user_id=owner_user_id_for_request(user),
        )
    except prototype_workspace.PrototypeWorkspaceError as exc:
        _raise_workspace_error(exc)
    except prototype_real_corpus.RealCorpusError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code}) from exc


@router.post("/workspace/topics/{topic_id}/refreshes")
def refresh_workspace_real_corpus(
    topic_id: str,
    payload: RefreshStartRequest,
    _user=Depends(require_product_user),
):
    try:
        result = prototype_workspace.check_for_corpus_updates(topic_id, confirmed=payload.confirmed)
        return prototype_living_topics.finalize_refresh_result(result)
    except prototype_workspace.PrototypeWorkspaceError as exc:
        _raise_workspace_error(exc)


@router.put("/workspace/topics/{topic_id}/schedule")
def update_workspace_living_schedule(
    topic_id: str,
    payload: LivingScheduleRequest,
    _user=Depends(require_product_user),
):
    try:
        return prototype_living_topics.save_schedule(topic_id, payload.model_dump())
    except prototype_workspace.PrototypeWorkspaceError as exc:
        _raise_workspace_error(exc)


@router.post("/workspace/topics/{topic_id}/schedule/dispatch")
def dispatch_workspace_living_schedule(
    topic_id: str,
    payload: LivingScheduleDispatchRequest,
    _user=Depends(require_product_user),
):
    if not payload.confirmed:
        raise HTTPException(status_code=400, detail={"code": "living_schedule_dispatch_confirmation_required"})
    try:
        return prototype_living_topics.execute_due_schedule(topic_id)
    except prototype_workspace.PrototypeWorkspaceError as exc:
        _raise_workspace_error(exc)


@router.post("/workspace/topics/{topic_id}/briefings/curation-delta")
def create_workspace_curation_delta_briefing(
    topic_id: str,
    payload: CurationDeltaBriefingRequest,
    _user=Depends(require_product_user),
):
    try:
        return prototype_living_topics.create_curation_delta_briefing(
            topic_id,
            payload.before_snapshot_id,
            payload.after_snapshot_id,
        )
    except prototype_workspace.PrototypeWorkspaceError as exc:
        _raise_workspace_error(exc)


@router.get("/workspace/topics/{topic_id}/briefings/compare")
def compare_workspace_briefings(
    topic_id: str,
    before_briefing_id: str,
    after_briefing_id: str,
    _user=Depends(require_product_user),
):
    try:
        return prototype_living_topics.compare_briefings(
            topic_id,
            before_briefing_id,
            after_briefing_id,
        )
    except prototype_workspace.PrototypeWorkspaceError as exc:
        _raise_workspace_error(exc)


@router.get("/workspace/topics/{topic_id}/briefings/{briefing_id}/export")
def export_workspace_briefing(
    topic_id: str,
    briefing_id: str,
    _user=Depends(require_product_user),
):
    try:
        return prototype_living_topics.export_briefing(topic_id, briefing_id)
    except prototype_workspace.PrototypeWorkspaceError as exc:
        _raise_workspace_error(exc)


@router.post("/workspace/topics/{topic_id}/briefings/{briefing_id}/attention")
def update_workspace_briefing_attention(
    topic_id: str,
    briefing_id: str,
    payload: AttentionActionRequest,
    _user=Depends(require_product_user),
):
    try:
        return prototype_living_topics.acknowledge_attention(
            topic_id,
            briefing_id,
            payload.change_event_id,
            action=payload.action,
        )
    except prototype_workspace.PrototypeWorkspaceError as exc:
        _raise_workspace_error(exc)


@router.post("/workspace/topics/{topic_id}/gap-analyses", status_code=201)
def create_workspace_gap_analysis(
    topic_id: str,
    payload: GapAnalysisRequest,
    _user=Depends(require_product_user),
):
    try:
        return prototype_research_gaps.analyze_topic(topic_id, payload.snapshot_id)
    except prototype_workspace.PrototypeWorkspaceError as exc:
        _raise_workspace_error(exc)


@router.get("/workspace/topics/{topic_id}/gap-analyses/{analysis_id}")
def get_workspace_gap_analysis(
    topic_id: str,
    analysis_id: str,
    _user=Depends(require_product_user),
):
    try:
        return prototype_research_gaps.get_analysis(topic_id, analysis_id)
    except prototype_workspace.PrototypeWorkspaceError as exc:
        _raise_workspace_error(exc)


@router.post("/workspace/topics/{topic_id}/gap-analyses/compare")
def compare_workspace_gap_analyses(
    topic_id: str,
    payload: GapAnalysisComparisonRequest,
    _user=Depends(require_product_user),
):
    try:
        return prototype_research_gaps.compare_analyses(
            topic_id, payload.before_analysis_id, payload.after_analysis_id
        )
    except prototype_workspace.PrototypeWorkspaceError as exc:
        _raise_workspace_error(exc)


@router.get("/workspace/topics/{topic_id}/gap-analyses/{analysis_id}/export")
def export_workspace_gap_analysis(
    topic_id: str,
    analysis_id: str,
    _user=Depends(require_product_user),
):
    try:
        return prototype_research_gaps.export_analysis(topic_id, analysis_id)
    except prototype_workspace.PrototypeWorkspaceError as exc:
        _raise_workspace_error(exc)


@router.post("/workspace/topics/{topic_id}/recommendations/compare")
def compare_workspace_recommendations(
    topic_id: str,
    payload: RecommendationComparisonRequest,
    _user=Depends(require_product_user),
):
    try:
        return prototype_research_gaps.compare_recommendations(topic_id, payload.recommendation_ids)
    except prototype_workspace.PrototypeWorkspaceError as exc:
        _raise_workspace_error(exc)


@router.post("/workspace/topics/{topic_id}/recommendations/{recommendation_id}/decisions", status_code=201)
def decide_workspace_recommendation(
    topic_id: str,
    recommendation_id: str,
    payload: RecommendationDecisionRequest,
    _user=Depends(require_product_user),
):
    try:
        return prototype_research_gaps.decide_recommendation(
            topic_id,
            recommendation_id,
            requested_status=payload.requested_status,
            reason_code=payload.reason_code,
            bounded_note=payload.bounded_note,
        )
    except prototype_workspace.PrototypeWorkspaceError as exc:
        _raise_workspace_error(exc)


@router.post("/workspace/topics/{topic_id}/recommendations/{recommendation_id}/promote", status_code=201)
def promote_workspace_recommendation(
    topic_id: str,
    recommendation_id: str,
    payload: RecommendationPromotionRequest,
    _user=Depends(require_product_user),
):
    try:
        return prototype_research_gaps.promote_recommendation(
            topic_id, recommendation_id, confirmed=payload.confirmed
        )
    except prototype_workspace.PrototypeWorkspaceError as exc:
        _raise_workspace_error(exc)


@router.get("/status", response_model=PrototypeStatusResponse)
def get_prototype_status(user=Depends(require_product_user)):
    return prototype.get_prototype_status(actor=resource_actor_for_user(user))


@router.post("/runs", response_model=PrototypeStartResponse)
def create_prototype_run(user=Depends(require_product_user)):
    try:
        return prototype.start_prototype_run(
            actor=resource_actor_for_user(user),
            owner_user_id=owner_user_id_for_request(user),
        )
    except prototype.PrototypeModeError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code, "reason": str(exc)}) from exc


@router.get("/real-corpus/status")
def get_real_corpus_status(_user=Depends(require_product_user)):
    return prototype_real_corpus.describe_real_corpus()


@router.post("/real-corpus/runs")
def create_real_corpus_run(
    payload: PrototypeRealCorpusStartRequest,
    user=Depends(require_product_user),
):
    if not payload.confirmed or not payload.network_enabled:
        raise HTTPException(status_code=409, detail={"code": "real_corpus_confirmation_required"})
    try:
        return prototype_real_corpus.start_real_corpus_run(
            owner_user_id=owner_user_id_for_request(user),
        )
    except prototype.PrototypeModeError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code}) from exc
    except prototype_real_corpus.RealCorpusError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code}) from exc


@router.get("/runs/{run_id}/real-corpus")
def get_real_corpus_record(run_id: str, _user=Depends(require_run_read_access)):
    try:
        return prototype_real_corpus.load_public_record(run_id)
    except prototype_real_corpus.RealCorpusError as exc:
        raise HTTPException(status_code=404, detail={"code": exc.code}) from exc


@router.get("/runs/{run_id}/real-corpus/evidence/{evidence_id}")
def get_real_corpus_evidence(
    run_id: str,
    evidence_id: str,
    _user=Depends(require_run_read_access),
):
    try:
        return prototype_real_corpus.get_evidence_detail(run_id, evidence_id)
    except prototype_real_corpus.RealCorpusError as exc:
        raise HTTPException(status_code=404, detail={"code": exc.code}) from exc


@router.post("/runs/{run_id}/real-corpus/replay")
def replay_real_corpus_run(run_id: str, _user=Depends(require_run_write_access)):
    try:
        return prototype_real_corpus.replay_real_corpus_run(run_id)
    except prototype_real_corpus.RealCorpusError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code}) from exc


def _client_host(request: Request) -> str | None:
    return request.client.host if request.client is not None else None


def _raise_live_error(exc: prototype_live_provider.PrototypeLiveProviderError) -> None:
    raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc


@router.get("/runs/{run_id}/provider-proof")
def get_provider_proof_status(run_id: str, _user=Depends(require_run_read_access)):
    try:
        return prototype_live_provider.get_live_proof_status(run_id)
    except prototype_live_provider.PrototypeLiveProviderError as exc:
        _raise_live_error(exc)


@router.post("/runs/{run_id}/provider-proof/activation")
def issue_provider_proof_activation(
    run_id: str,
    payload: PrototypeProviderRouteRequest,
    request: Request,
    _user=Depends(require_run_write_access),
):
    try:
        return prototype_live_provider.issue_activation_token(
            run_id=run_id,
            route=payload.model_dump(),
            client_host=_client_host(request),
        )
    except prototype_live_provider.PrototypeLiveProviderError as exc:
        _raise_live_error(exc)


@router.post("/runs/{run_id}/provider-proof/generate")
async def generate_provider_proof_candidate(
    run_id: str,
    payload: PrototypeProviderInvocationRequest,
    request: Request,
    _user=Depends(require_run_write_access),
):
    try:
        route = payload.model_dump(exclude={"activation_token", "confirmed"})
        return await prototype_live_provider.generate_live_candidate(
            run_id=run_id,
            route=route,
            activation_token=payload.activation_token,
            confirmed=payload.confirmed,
            client_host=_client_host(request),
        )
    except prototype_live_provider.PrototypeLiveProviderError as exc:
        _raise_live_error(exc)
    except Exception as exc:  # noqa: BLE001 - never expose SDK or local-path details.
        raise HTTPException(status_code=500, detail={"code": "prototype_live_provider_internal_error"}) from exc


@router.get("/runs/{run_id}/provider-proof/{execution_id}/candidate")
def get_provider_candidate_preview(
    run_id: str,
    execution_id: str,
    _user=Depends(require_run_read_access),
):
    try:
        return prototype_live_provider.get_candidate_preview(run_id, execution_id)
    except prototype_live_provider.PrototypeLiveProviderError as exc:
        _raise_live_error(exc)


@router.post("/runs/{run_id}/provider-proof/{execution_id}/accept")
def accept_provider_candidate(
    run_id: str,
    execution_id: str,
    payload: PrototypeProviderDecisionRequest,
    _user=Depends(require_run_write_access),
):
    try:
        return prototype_live_provider.accept_candidate(
            run_id=run_id,
            execution_id=execution_id,
            confirmed=payload.confirmed,
        )
    except prototype_live_provider.PrototypeLiveProviderError as exc:
        _raise_live_error(exc)


@router.post("/runs/{run_id}/provider-proof/{execution_id}/reject")
def reject_provider_candidate(
    run_id: str,
    execution_id: str,
    payload: PrototypeProviderDecisionRequest,
    _user=Depends(require_run_write_access),
):
    try:
        return prototype_live_provider.reject_candidate(
            run_id=run_id,
            execution_id=execution_id,
            confirmed=payload.confirmed,
        )
    except prototype_live_provider.PrototypeLiveProviderError as exc:
        _raise_live_error(exc)


@router.get("/runs/{run_id}/provider-proof/{execution_id}/comparison")
def compare_provider_candidate(
    run_id: str,
    execution_id: str,
    _user=Depends(require_run_read_access),
):
    try:
        return prototype_live_provider.compare_accepted_to_baseline(run_id, execution_id)
    except prototype_live_provider.PrototypeLiveProviderError as exc:
        _raise_live_error(exc)


@router.get("/runs/{run_id}/report-versions/{version_id}/preview")
def preview_selected_report_version(
    run_id: str,
    version_id: str,
    _user=Depends(require_run_read_access),
):
    try:
        return prototype_live_provider.preview_report_version(run_id, version_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"code": "prototype_report_version_not_found"}) from exc


@router.post("/runs/{run_id}/report-versions/{version_id}/export")
def export_selected_report_version(
    run_id: str,
    version_id: str,
    _user=Depends(require_run_write_access),
):
    try:
        return normalize_payload_references(
            prototype_live_provider.export_report_version(run_id, version_id),
            run_id=run_id,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"code": "prototype_report_version_not_found"}) from exc
