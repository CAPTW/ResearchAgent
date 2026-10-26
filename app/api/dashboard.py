from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.mcp import get_mcp_tool_access_summary
from app.auth.resource_access import (
    ResourceAction, ResourceActor,
    reject_client_authority_fields,
    resource_actor_context,
    resource_actor_for_user,
)
from app.auth.security import (
    build_auth_me_response,
    describe_mcp_oauth_readiness,
    get_optional_product_user,
    list_runs_for_request,
    owner_user_id_for_request,
    require_product_user,
)
from app.schemas.dashboard import (
    ApiReportsResponse,
    ApiProviderConfigurationDraftRequest,
    ApiProviderConfigurationValidationResponse,
    ApiProviderReadinessListResponse,
    ApiReviewRefreshRequest,
    ApiReviewRefreshResponse,
    ApiScheduledTaskResponse,
    ApiTopicConfigurationDraftRequest,
    ApiTopicConfigurationValidationResponse,
    ApiTopicConfigurationsResponse,
    ApiTopicDashboardResponse,
    ApiTopicPapersResponse,
    ApiTopicRecommendationsResponse,
    ApiTopicsOverviewResponse,
    ApiTopicsResponse,
    DashboardOverviewResponse,
    DashboardPaperSearchResponse,
    DashboardRunSearchResponse,
    DashboardTopicActionRequest,
    DashboardTopicActionResponse,
    DashboardStartRunsRequest,
    DashboardStartRunsResponse,
    SettingsStatusResponse,
    SourceProviderRegistryResponse,
    TopicDashboardResponse,
    TopicProfileCreateRequest,
    TopicProfileDeleteResponse,
    TopicNotificationSettingsResponse,
    TopicNotificationSettingsUpdateRequest,
    TopicNotificationDeliveriesResponse,
    TopicNotificationTestDeliveryRequest,
    TopicNotificationTestDeliveryResponse,
    TopicNotificationsResponse,
    TopicScheduleCreateRequest,
    TopicScheduleDeleteResponse,
    TopicScheduleRunNowResponse,
    TopicScheduleResponse,
    TopicSchedulesResponse,
    TopicScheduleUpdateRequest,
    TopicProfileUpdateRequest,
    TopicProfilesResponse,
)
from app.schemas.runs import RunCreateRequest
from app.repositories import metadata_index
from app.repositories.files import iso_now_utc
from app.ops.runtime import build_deployment_status
from app.services.providers import describe_provider_status
from app.services.provider_proof_state import build_provider_proof_state_status
from app.services.report_draft_assist_registry import describe_report_draft_assist
from app.services.runs import list_runs, start_run
from app.services import (
    action_capabilities,
    app_oauth_oidc_decision,
    auth_foundation,
    operator_audit,
    paper_review,
    paper_occurrences,
    provider_configurations,
    report_hub,
    source_config,
    topic_configurations,
    topic_dashboard,
    topic_notifications,
    topic_profiles,
    topic_schedules,
)
router = APIRouter(tags=["dashboard"])


def _csv_list(raw_value: str | None) -> list[str]:
    return [item.strip() for item in (raw_value or "").split(",") if item and item.strip()]


def _resource_actor(user) -> ResourceActor:
    return resource_actor_for_user(user)


def _run_ids_for_actor(actor: ResourceActor) -> list[str] | None:
    if actor.auth_mode == "disabled":
        return None
    owner_user_id = (
        actor.scoped_owner_user_id
        if actor.actor_type == "system"
        else actor.subject
    )
    return [
        str(run.get("run_id") or "").strip()
        for run in list_runs(limit=None)
        if str(run.get("run_id") or "").strip()
        and str(run.get("owner_user_id") or "").strip() == str(owner_user_id or "")
    ]


def _raise_schedule_error(exc: RuntimeError) -> None:
    message = str(exc)
    if isinstance(exc, topic_schedules.ScheduleTimezoneUnavailable):
        status_code = 400
    elif isinstance(exc, topic_schedules.ScheduleStoreLockedError) or "잠겨" in message or "lock" in message.lower():
        status_code = 423
    else:
        status_code = 409 if "이미 실행 중" in message else 400
    raise HTTPException(status_code=status_code, detail=message) from exc


@router.get("/dashboard/overview", response_model=DashboardOverviewResponse)
def get_dashboard_overview(
    topics: str | None = Query(default=None),
    user=Depends(require_product_user),
):
    actor = _resource_actor(user)
    return metadata_index.get_overview(
        topics=_csv_list(topics),
        run_ids=_run_ids_for_actor(actor),
    )


@router.get("/dashboard/search-runs", response_model=DashboardRunSearchResponse)
def get_dashboard_run_search(
    q: str = "",
    topics: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    user=Depends(require_product_user),
):
    actor = _resource_actor(user)
    return metadata_index.search_runs(
        q=q,
        topics=_csv_list(topics),
        limit=limit,
        run_ids=_run_ids_for_actor(actor),
    )


@router.get("/dashboard/search-papers", response_model=DashboardPaperSearchResponse)
def get_dashboard_paper_search(
    q: str = "",
    themes: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    user=Depends(require_product_user),
):
    actor = _resource_actor(user)
    return metadata_index.search_papers(
        q=q,
        themes=_csv_list(themes),
        limit=limit,
        run_ids=_run_ids_for_actor(actor),
    )


@router.get("/dashboard/topic-dashboard", response_model=TopicDashboardResponse)
def get_topic_dashboard(
    profile_id: str | None = Query(default=None),
    user=Depends(require_product_user),
):
    return topic_dashboard.build_topic_dashboard(
        profile_id=profile_id,
        actor=_resource_actor(user),
    )


@router.get("/api/topics", response_model=ApiTopicsResponse)
def get_api_topics(user=Depends(require_product_user)):
    actor = _resource_actor(user)
    with resource_actor_context(actor):
        return topic_dashboard.build_api_topics()


@router.get("/api/topics/overview", response_model=ApiTopicsOverviewResponse)
def get_api_topics_overview(user=Depends(require_product_user)):
    actor = _resource_actor(user)
    with resource_actor_context(actor):
        return topic_dashboard.build_api_topics_overview()


@router.get("/api/topics/{topic_id}/recommendations", response_model=ApiTopicRecommendationsResponse)
def get_api_topic_recommendations(topic_id: str, user=Depends(require_product_user)):
    from app.api.recommendation_versions import invoke

    return invoke(topic_id, user, lambda: topic_dashboard.build_api_topic_recommendations(topic_id), action=ResourceAction.READ)


@router.get("/api/topics/{topic_id}/dashboard", response_model=ApiTopicDashboardResponse)
def get_api_topic_dashboard(topic_id: str, _user=Depends(require_product_user)):
    try:
        actor = _resource_actor(_user)
        with resource_actor_context(actor):
            return topic_dashboard.build_api_topic_dashboard(
                topic_id, operator_user=_user, actor=actor
            )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except paper_occurrences.OccurrenceIdentityInvalid as exc:
        raise HTTPException(status_code=503, detail=exc.code) from exc


@router.get("/api/topics/{topic_id}/papers", response_model=ApiTopicPapersResponse)
def get_api_topic_papers(
    topic_id: str,
    q: str = Query(default=""),
    provider: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    _user=Depends(require_product_user),
):
    try:
        actor = _resource_actor(_user)
        with resource_actor_context(actor):
            return topic_dashboard.build_api_topic_papers(
                topic_id,
                q=q,
                provider=provider,
                limit=limit,
                actor=actor,
            )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _topic_configuration_payload(request: ApiTopicConfigurationDraftRequest) -> dict:
    payload = request.model_dump(exclude_none=True)
    extra = getattr(request, "__pydantic_extra__", None)
    if isinstance(extra, dict):
        payload.update(extra)
    try:
        reject_client_authority_fields(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return payload


def _provider_configuration_payload(request: ApiProviderConfigurationDraftRequest) -> dict:
    payload = request.model_dump(exclude_none=True)
    extra = getattr(request, "__pydantic_extra__", None)
    if isinstance(extra, dict):
        payload.update(extra)
    return payload


def _with_operations_planner(result: dict) -> dict:
    planner_profile = result.pop("_plannerProfile", None)
    if planner_profile:
        result["operationsPlanner"] = topic_dashboard.build_topic_operations_planner(planner_profile)
    return result


@router.get("/api/topic-configurations", response_model=ApiTopicConfigurationsResponse)
def get_api_topic_configurations(user=Depends(require_product_user)):
    actor = _resource_actor(user)
    with resource_actor_context(actor):
        return topic_configurations.list_topic_configurations()


@router.get("/api/provider-configurations/readiness", response_model=ApiProviderReadinessListResponse)
def get_api_provider_configuration_readiness(_user=Depends(require_product_user)):
    return provider_configurations.build_provider_readiness_summary()


@router.post("/api/provider-configurations/validate", response_model=ApiProviderConfigurationValidationResponse)
def validate_api_provider_configuration(
    request: ApiProviderConfigurationDraftRequest,
    _user=Depends(require_product_user),
):
    return provider_configurations.validate_provider_configuration(_provider_configuration_payload(request))


@router.get("/api/topics/{topic_id}/configuration", response_model=ApiTopicConfigurationValidationResponse)
def get_api_topic_configuration(topic_id: str, user=Depends(require_product_user)):
    try:
        actor = _resource_actor(user)
        with resource_actor_context(actor):
            return _with_operations_planner(topic_configurations.current_topic_configuration(topic_id))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/topics/{topic_id}/configuration/validate", response_model=ApiTopicConfigurationValidationResponse)
def validate_api_topic_configuration(
    topic_id: str,
    request: ApiTopicConfigurationDraftRequest,
    user=Depends(require_product_user),
):
    try:
        actor = _resource_actor(user)
        with resource_actor_context(actor):
            return _with_operations_planner(
                topic_configurations.validate_topic_configuration(
                    topic_id,
                    _topic_configuration_payload(request),
                )
            )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/topics/{topic_id}/configuration/preview", response_model=ApiTopicConfigurationValidationResponse)
def preview_api_topic_configuration(
    topic_id: str,
    request: ApiTopicConfigurationDraftRequest,
    user=Depends(require_product_user),
):
    try:
        actor = _resource_actor(user)
        with resource_actor_context(actor):
            return _with_operations_planner(
                topic_configurations.validate_topic_configuration(
                    topic_id,
                    _topic_configuration_payload(request),
                )
            )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/api/topics/{topic_id}/configuration", response_model=ApiTopicConfigurationValidationResponse)
def save_api_topic_configuration(
    topic_id: str,
    request: ApiTopicConfigurationDraftRequest,
    user=Depends(require_product_user),
):
    auth_status = auth_foundation.build_auth_foundation_status(user=user)
    identity = auth_status["operator"]
    capability_decision = action_capabilities.evaluate_action_capability(
        "save_topic_config",
        operator_capabilities=identity["capabilities"],
        fallback_risk_level="config_write",
    )
    capability = capability_decision["capabilityRequired"] or "edit_topic_config"
    if not capability_decision["allowed"]:
        operator_audit.record_event(
            action_type="save_topic_config",
            decision="denied",
            operator_id=identity["operatorId"],
            auth_mode=auth_status["authMode"],
            capability_required=capability,
            capability_present=bool(capability_decision["capabilityPresent"]),
            reason_codes=list(capability_decision["reasonCodes"]),
            target_type="topic",
            target_id=topic_id,
            risk_level="config_write",
            metadata={"missingCapabilities": capability_decision["missingCapabilities"]},
        )
        raise HTTPException(
            status_code=403,
            detail={
                "message": f"Required capability missing: {capability}",
                "reason_codes": capability_decision["reasonCodes"],
                "capability_required": capability,
                "missing_capabilities": capability_decision["missingCapabilities"],
            },
        )
    try:
        actor = _resource_actor(user)
        with resource_actor_context(actor):
            result = _with_operations_planner(
                topic_configurations.save_topic_configuration(
                    topic_id,
                    _topic_configuration_payload(request),
                )
            )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    decision = "allowed" if result.get("validationStatus") == "saved" else "rejected"
    operator_audit.record_event(
        action_type="save_topic_config",
        decision=decision,
        operator_id=identity["operatorId"],
        auth_mode=auth_status["authMode"],
        capability_required=capability,
        capability_present=True,
        reason_codes=list(result.get("errors") or []) if decision == "rejected" else [],
        target_type="topic",
        target_id=topic_id,
        risk_level="config_write",
        metadata={"changedFields": result.get("changedFields") or [], "configHash": result.get("configHash")},
    )
    return result


@router.post("/api/topics/{topic_id}/reviews/refresh", response_model=ApiReviewRefreshResponse)
def refresh_api_topic_reviews(
    topic_id: str,
    request: ApiReviewRefreshRequest,
    user=Depends(require_product_user),
):
    if request.fetch_missing_pdf and request.download_limit <= 0:
        raise HTTPException(status_code=422, detail="fetch_missing_pdf requires download_limit > 0")
    try:
        actor = _resource_actor(user)
        with resource_actor_context(actor):
            return paper_review.refresh_reviews_for_topic(
                topic_id,
                limit=request.limit,
                force=request.force,
                dry_run=request.dry_run,
                window_days=request.window_days,
                min_relevance=request.min_relevance,
                unique_limit=request.unique_limit,
                use_full_text=request.use_full_text,
                max_text_chars=request.max_text_chars,
                prefer_existing_text=request.prefer_existing_text,
                extract_existing_pdf=request.extract_existing_pdf,
                fetch_missing_pdf=request.fetch_missing_pdf,
                download_limit=request.download_limit,
                provider=request.provider,
                allow_domains=request.allow_domains,
                max_pdf_bytes=request.max_pdf_bytes,
                download_timeout=request.download_timeout,
                extract_after_fetch=request.extract_after_fetch,
            )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/reports", response_model=ApiReportsResponse)
def get_api_reports(
    request: Request,
    topic_id: str | None = Query(default=None, alias="topicId"),
    limit: int = Query(default=50, ge=1, le=200),
    _user=Depends(require_product_user),
):
    try:
        visible_runs = list_runs_for_request(request, list_runs(limit=None))
        with resource_actor_context(_resource_actor(_user)):
            return report_hub.build_api_reports(visible_runs, topic_id=topic_id, limit=limit)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/source-providers", response_model=SourceProviderRegistryResponse)
def get_api_source_providers(_user=Depends(require_product_user)):
    checked_at = iso_now_utc()
    return source_config.provider_registry(checked_at=checked_at)


@router.get("/topic-profiles", response_model=TopicProfilesResponse)
def get_topic_profiles(user=Depends(require_product_user)):
    return topic_profiles.list_topic_profiles(actor=_resource_actor(user))


@router.post("/topic-profiles", response_model=TopicProfilesResponse)
def create_topic_profile(request: TopicProfileCreateRequest, user=Depends(require_product_user)):
    actor = _resource_actor(user)
    try:
        topic_profiles.create_topic_profile(request.model_dump(), actor=actor)
        return topic_profiles.list_topic_profiles(actor=actor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/topic-profiles/{profile_id}", response_model=TopicProfilesResponse)
def patch_topic_profile(profile_id: str, request: TopicProfileUpdateRequest, user=Depends(require_product_user)):
    actor = _resource_actor(user)
    try:
        topic_profiles.update_topic_profile(
            profile_id,
            request.model_dump(exclude_none=True),
            actor=actor,
        )
        return topic_profiles.list_topic_profiles(actor=actor)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/topic-profiles/{profile_id}", response_model=TopicProfileDeleteResponse)
def remove_topic_profile(profile_id: str, user=Depends(require_product_user)):
    try:
        return topic_profiles.delete_topic_profile(
            profile_id,
            actor=_resource_actor(user),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/dashboard/start-runs", response_model=DashboardStartRunsResponse)
def start_runs_from_profiles(request: DashboardStartRunsRequest, user=Depends(require_product_user)):
    items = []
    actor = _resource_actor(user)
    for profile_id in request.profile_ids:
        try:
            profile = topic_profiles.get_topic_profile(profile_id, actor=actor)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        run = start_run(
            RunCreateRequest.model_validate(topic_profiles.to_run_request_payload(profile)),
            source="dashboard_profile",
            owner_user_id=owner_user_id_for_request(user),
            topic_profile_id=profile_id,
        )
        items.append({"profile_id": profile_id, "run_id": run["run_id"], "topic": run["topic"], "status": run["status"]})
    return {"items": items}


def _action_run_payload(profile: dict, action: str) -> tuple[dict, list[str]]:
    payload = topic_profiles.to_run_request_payload(profile)
    if action == "collect":
        payload["skip_summary"] = True
        payload["skip_synthesis"] = True
        return payload, ["논문 수집", "run-scoped 저장", "랭킹/triage 저장"]
    if action == "analyze":
        payload["skip_synthesis"] = True
        return payload, ["논문 수집", "저장", "랭킹/요약 분석"]
    if action == "report":
        return payload, ["논문 수집", "저장", "분석", "보고서 생성"]
    return payload, ["논문 수집", "저장", "분석", "보고서 생성"]


@router.post("/dashboard/topic-action", response_model=DashboardTopicActionResponse)
def run_topic_action(request: DashboardTopicActionRequest, user=Depends(require_product_user)):
    try:
        profile = topic_profiles.get_topic_profile(
            request.profile_id,
            actor=_resource_actor(user),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    payload, execution_plan = _action_run_payload(profile, request.action)
    run = start_run(
        RunCreateRequest.model_validate(payload),
        source=f"dashboard_topic_action:{request.action}",
        owner_user_id=owner_user_id_for_request(user),
        topic_profile_id=request.profile_id,
    )
    return {
        "profile_id": request.profile_id,
        "action": request.action,
        "run_id": run["run_id"],
        "topic": run["topic"],
        "status": run["status"],
        "message": "토픽 액션이 기존 run 파이프라인으로 시작되었습니다.",
        "execution_plan": execution_plan,
    }


def _run_api_topic_action(topic_id: str, action: str, user) -> dict:
    try:
        profile = topic_profiles.get_topic_profile(
            topic_id,
            actor=_resource_actor(user),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    payload, execution_plan = _action_run_payload(profile, action)
    run = start_run(
        RunCreateRequest.model_validate(payload),
        source=f"api_topic_action:{action}",
        owner_user_id=owner_user_id_for_request(user),
        topic_profile_id=topic_id,
    )
    return {
        "profile_id": topic_id,
        "action": action,
        "run_id": run["run_id"],
        "topic": run["topic"],
        "status": run["status"],
        "message": "토픽 액션이 기존 run 파이프라인으로 시작되었습니다.",
        "execution_plan": execution_plan,
    }


@router.post("/api/topics/{topic_id}/actions/collect", response_model=DashboardTopicActionResponse)
def collect_api_topic(topic_id: str, user=Depends(require_product_user)):
    return _run_api_topic_action(topic_id, "collect", user)


@router.post("/api/topics/{topic_id}/actions/analyze", response_model=DashboardTopicActionResponse)
def analyze_api_topic(topic_id: str, user=Depends(require_product_user)):
    return _run_api_topic_action(topic_id, "analyze", user)


@router.post("/api/topics/{topic_id}/actions/report", response_model=DashboardTopicActionResponse)
def report_api_topic(topic_id: str, user=Depends(require_product_user)):
    return _run_api_topic_action(topic_id, "report", user)


@router.get("/topic-notifications/settings", response_model=TopicNotificationSettingsResponse)
def get_topic_notification_settings(
    profile_id: str = Query(...),
    user=Depends(require_product_user),
):
    try:
        return topic_notifications.get_topic_notification_settings(
            profile_id,
            actor=_resource_actor(user),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/topic-notifications/settings/{profile_id}", response_model=TopicNotificationSettingsResponse)
def put_topic_notification_settings(
    profile_id: str,
    request: TopicNotificationSettingsUpdateRequest,
    user=Depends(require_product_user),
):
    try:
        return topic_notifications.update_topic_notification_settings(
            profile_id,
            request.model_dump(exclude_none=True),
            actor=_resource_actor(user),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/topic-notifications", response_model=TopicNotificationsResponse)
def get_topic_notifications(
    profile_id: str = Query(...),
    limit: int = Query(default=20, ge=1, le=100),
    user=Depends(require_product_user),
):
    try:
        return topic_notifications.list_topic_notifications(
            profile_id,
            limit=limit,
            actor=_resource_actor(user),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/topic-notifications/deliveries", response_model=TopicNotificationDeliveriesResponse)
def get_topic_notification_deliveries(
    profile_id: str = Query(...),
    limit: int = Query(default=20, ge=1, le=100),
    user=Depends(require_product_user),
):
    try:
        return topic_notifications.list_topic_notification_deliveries(
            profile_id,
            limit=limit,
            actor=_resource_actor(user),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/topic-notifications/settings/{profile_id}/test-delivery",
    response_model=TopicNotificationTestDeliveryResponse,
)
def test_topic_notification_delivery(
    profile_id: str,
    request: TopicNotificationTestDeliveryRequest,
    user=Depends(require_product_user),
):
    try:
        return topic_notifications.test_topic_notification_delivery(
            profile_id,
            channel_id=request.channel_id,
            actor=_resource_actor(user),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/topic-schedules", response_model=TopicSchedulesResponse)
def get_topic_schedules(
    profile_id: str | None = Query(default=None),
    user=Depends(require_product_user),
):
    actor = _resource_actor(user)
    response = topic_schedules.list_topic_schedules_safe(actor=actor)
    items = response["items"]
    if profile_id:
        items = [item for item in items if item.get("profile_id") == profile_id]
    return {
        "items": items,
        "scheduler": topic_schedules.get_scheduler_runtime_status(actor=actor),
        "degraded": bool(response.get("degraded")),
        "read_only": bool(response.get("read_only")),
        "warnings": response.get("warnings", []),
    }


@router.post("/topic-schedules", response_model=TopicScheduleResponse)
def create_topic_schedule(request: TopicScheduleCreateRequest, user=Depends(require_product_user)):
    try:
        return topic_schedules.create_topic_schedule(
            request.model_dump(exclude_none=True),
            actor=_resource_actor(user),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        _raise_schedule_error(exc)


@router.patch("/topic-schedules/{schedule_id}", response_model=TopicSchedulesResponse)
def patch_topic_schedule(schedule_id: str, request: TopicScheduleUpdateRequest, user=Depends(require_product_user)):
    actor = _resource_actor(user)
    try:
        topic_schedules.update_topic_schedule(
            schedule_id,
            request.model_dump(exclude_none=True),
            actor=actor,
        )
        response = topic_schedules.list_topic_schedules(actor=actor)
        response["scheduler"] = topic_schedules.get_scheduler_runtime_status(actor=actor)
        return response
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        _raise_schedule_error(exc)


@router.delete("/topic-schedules/{schedule_id}", response_model=TopicScheduleDeleteResponse)
def remove_topic_schedule(schedule_id: str, user=Depends(require_product_user)):
    try:
        return topic_schedules.delete_topic_schedule(
            schedule_id,
            actor=_resource_actor(user),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        _raise_schedule_error(exc)


@router.patch("/api/tasks/{task_id}", response_model=ApiScheduledTaskResponse)
def patch_api_task(task_id: str, request: TopicScheduleUpdateRequest, user=Depends(require_product_user)):
    try:
        schedule = topic_schedules.update_topic_schedule(
            task_id,
            request.model_dump(exclude_none=True),
            actor=_resource_actor(user),
        )
        return topic_dashboard.contract_task(schedule)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        _raise_schedule_error(exc)


@router.post("/topic-schedules/{schedule_id}/run-now", response_model=TopicScheduleRunNowResponse)
def run_topic_schedule_now(schedule_id: str, user=Depends(require_product_user)):
    try:
        schedule, run = topic_schedules.trigger_topic_schedule_run(
            schedule_id,
            trigger="manual",
            actor=_resource_actor(user),
        )
        profile = topic_profiles.get_topic_profile(
            schedule["profile_id"],
            actor=_resource_actor(user),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        _raise_schedule_error(exc)
    return {
        "schedule": schedule,
        "run": {"profile_id": profile["profile_id"], "run_id": run["run_id"], "topic": run["topic"], "status": run["status"]},
    }


@router.post("/api/tasks/{task_id}/run", response_model=TopicScheduleRunNowResponse)
def run_api_task_now(task_id: str, user=Depends(require_product_user)):
    return run_topic_schedule_now(task_id, user)


@router.get("/settings/status", response_model=SettingsStatusResponse)
def get_settings_status(request: Request):
    auth_me = build_auth_me_response(request)
    product_user = get_optional_product_user(request)
    actor = _resource_actor(product_user) if product_user is not None else None
    auth_foundation_status = auth_foundation.build_auth_foundation_status(user=product_user)
    app_oauth_oidc_decision_status = app_oauth_oidc_decision.build_app_oauth_oidc_decision_status()
    mcp_status = describe_mcp_oauth_readiness()
    mcp_status.update(get_mcp_tool_access_summary())
    provider_status = describe_provider_status()
    provider_proof_state = build_provider_proof_state_status()
    report_draft_assist = describe_report_draft_assist(refresh=False)
    if actor is None:
        scheduler_status = topic_schedules.get_scheduler_runtime_status()
        notification_delivery_status = (
            topic_notifications.notification_delivery_health_summary()
        )
    else:
        with resource_actor_context(actor):
            scheduler_status = topic_schedules.get_scheduler_runtime_status()
            notification_delivery_status = (
                topic_notifications.notification_delivery_health_summary()
            )
    deployment_status = build_deployment_status(
        provider=provider_status,
        scheduler=scheduler_status,
        notification_delivery=notification_delivery_status,
    )
    recent_runs = list_runs(limit=200)
    if actor is None:
        recent_runs = []
    elif actor.auth_mode != "disabled":
        owner_user_id = actor.subject
        recent_runs = [
            run
            for run in recent_runs
            if str(run.get("owner_user_id") or "").strip() == str(owner_user_id or "")
        ]
    run_counts: dict[str, int] = {}
    for run in recent_runs:
        status = str(run.get("status") or "unknown")
        run_counts[status] = run_counts.get(status, 0) + 1
    latest_run = recent_runs[0] if recent_runs else None
    successful_runs = [run for run in recent_runs if run.get("status") == "completed"]
    failed_runs = [run for run in recent_runs if run.get("status") == "failed"]
    last_success = successful_runs[0] if successful_runs else None
    last_failure = failed_runs[0] if failed_runs else None
    return {
        "health_status": "ok",
        "auth_mode": auth_me["auth_mode"],
        "authenticated": auth_me["authenticated"],
        "product_session_backend": auth_me["product_session_backend"],
        "auth_warnings": auth_me["warnings"],
        "authFoundation": auth_foundation_status,
        "appOauthOidcDecision": app_oauth_oidc_decision_status,
        "mcp_url": auth_me.get("mcp_url"),
        "provider": provider_status,
        "providerProofState": provider_proof_state,
        "reportDraftAssist": report_draft_assist,
        "mcp": mcp_status,
        "deployment": deployment_status,
        "metadata_index_state": metadata_index.get_index_state(),
        "ops": {
            "generated_at": iso_now_utc(),
            "run_counts": run_counts,
            "latest_run_id": latest_run.get("run_id") if latest_run else None,
            "latest_run_status": latest_run.get("status") if latest_run else None,
            "latest_run_updated_at": latest_run.get("updated_at") if latest_run else None,
            "last_successful_run_id": last_success.get("run_id") if last_success else None,
            "last_successful_run_at": (last_success.get("finished_at") or last_success.get("updated_at")) if last_success else None,
            "last_failed_run_id": last_failure.get("run_id") if last_failure else None,
            "last_failed_run_at": (last_failure.get("finished_at") or last_failure.get("updated_at")) if last_failure else None,
            "last_failure_message": last_failure.get("error") if last_failure else None,
            "scheduler": scheduler_status,
            "notification_delivery": notification_delivery_status,
        },
        "stitch_status": "seam_prepared",
    }
