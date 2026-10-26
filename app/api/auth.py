from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from app.auth.config import AuthConfigurationError, get_auth_settings
from app.auth.security import (
    build_auth_me_response,
    build_oidc_login_redirect,
    clear_product_session_cookie,
    complete_oidc_callback,
    get_optional_product_user,
    revoke_current_product_session,
)
from app.schemas.auth import (
    AuthSessionResponse,
    LogoutResponse,
    OAuthAccountLinkHealthResponse,
    OAuthAccountLinkListResponse,
    OAuthAccountLinkMetadataRequest,
    OAuthAccountLinkOperationResponse,
    OAuthAuthorizationUrlRequest,
    OAuthAuthorizationUrlResponse,
    OAuthCallbackDryRunRequest,
    OAuthCallbackDryRunResponse,
    OAuthDryRunStatusResponse,
    OAuthFlowTimelineResponse,
    OAuthLiveExchangeReadinessRequest,
    OAuthLiveExchangeReadinessResponse,
    OAuthLiveExchangePilotRequest,
    OAuthLiveExchangePilotResultResponse,
    OAuthLiveExchangePilotStatusResponse,
    OAuthLivePilotEvidencePackageResponse,
    OAuthLivePilotReplayVerificationResponse,
    OAuthOperatorAcceptanceChecklistRequest,
    OAuthOperatorAcceptanceChecklistResponse,
    OAuthPreflightPackageResponse,
    OAuthProviderMetadataDraftRequest,
    OAuthProviderMetadataValidationResponse,
    OAuthRealProviderSmokeRequest,
    OAuthRealProviderSmokeResultResponse,
    OAuthRealProviderSmokeStatusResponse,
    OAuthReadOnlyProofResponse,
    OAuthReadinessRemediationSummaryResponse,
    OAuthSmokeEvidenceFreezeRequest,
    OAuthSmokeEvidenceFreezeResponse,
    OAuthSmokeReplaySummaryResponse,
    OAuthSmokeResultSummaryResponse,
    ProviderOrchestrationDryRunPlanRequest,
    ProviderOrchestrationDryRunPlanResponse,
    ProviderOrchestrationExecutionReplayResponse,
    ProviderOrchestrationExecutionRequest,
    ProviderOrchestrationExecutionResultResponse,
    ProviderOrchestrationExecutionSummaryResponse,
    ProviderOrchestrationPlanSummaryResponse,
    ProviderOrchestrationReplayResultResponse,
    ProviderOrchestrationSafetyMatrixResponse,
    OAuthSessionCapabilityPolicyResponse,
    OAuthTokenExchangeRequest,
    OAuthTokenExchangeResponse,
    OAuthTokenExchangeStatusResponse,
    OAuthSessionBindingResponse,
)
from app.services import (
    action_capabilities,
    auth_foundation,
    oauth_account_links,
    oauth_dry_run,
    oauth_live_evidence,
    oauth_live_readiness,
    oauth_readiness,
    oauth_smoke_results,
    oauth_token_exchange,
    operator_audit,
    provider_orchestration,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/me", response_model=AuthSessionResponse)
def get_auth_me(request: Request, response: Response):
    payload = build_auth_me_response(request)
    settings = get_auth_settings()
    if settings.auth_enabled and not payload["authenticated"] and request.cookies.get(settings.session_cookie_name):
        clear_product_session_cookie(response, settings)
    return payload


def _metadata_payload(request: OAuthProviderMetadataDraftRequest) -> dict:
    payload = request.model_dump(exclude_none=True)
    extra = getattr(request, "__pydantic_extra__", None)
    if isinstance(extra, dict):
        payload.update(extra)
    return payload


def _operator_context(request: Request) -> tuple[dict, dict]:
    user = get_optional_product_user(request)
    auth_status = auth_foundation.build_auth_foundation_status(user=user)
    return auth_status["operator"], auth_status


def _account_link_payload(payload: OAuthAccountLinkMetadataRequest) -> dict:
    request_payload = payload.model_dump(exclude_none=True)
    extra = getattr(payload, "__pydantic_extra__", None)
    if isinstance(extra, dict):
        request_payload.update(extra)
    return request_payload


def _audit_oauth_authorization_event(*, action_type: str, result: dict, request: Request) -> None:
    identity, auth_status = _operator_context(request)
    operator_audit.record_event(
        action_type=action_type,
        decision="preview_only" if action_type == "oauth_authorization_preview" else ("allowed" if not result.get("errors") else "rejected"),
        operator_id=identity["operatorId"],
        auth_mode=auth_status["authMode"],
        capability_required="view_dashboard",
        capability_present="view_dashboard" in set(identity.get("capabilities") or []),
        reason_codes=list(result.get("reasonCodes") or []),
        target_type="oauth_provider",
        target_id=result.get("providerId"),
        risk_level="read_only",
        metadata={
            "providerId": result.get("providerId"),
            "stateStored": bool(result.get("stateStored")),
            "clientIdStatus": result.get("clientIdStatus"),
            "tokenExchangeEnabled": False,
            "externalAuthCallPerformed": False,
        },
    )


@router.post("/oauth/metadata/validate", response_model=OAuthProviderMetadataValidationResponse)
def validate_oauth_metadata(payload: OAuthProviderMetadataDraftRequest, request: Request):
    metadata_payload = _metadata_payload(payload)
    result = auth_foundation.validate_oauth_provider_metadata(metadata_payload)
    secret_policy = result.get("secretPolicy") or {}
    if secret_policy.get("rejectedFields"):
        user = get_optional_product_user(request)
        identity = auth_foundation.operator_identity_for_user(user)
        operator_audit.record_event(
            action_type="secret_policy_rejection",
            decision="rejected",
            operator_id=identity["operatorId"],
            auth_mode=auth_foundation.build_auth_foundation_status(user=user)["authMode"],
            reason_codes=list(secret_policy.get("reasonCodes") or ["secret_policy_rejection"]),
            target_type="oauth_provider",
            target_id=result.get("providerId"),
            risk_level="secret_policy",
            metadata={
                "providerId": result.get("providerId"),
                "rejectedFields": secret_policy.get("rejectedFields") or [],
                "metadata": metadata_payload,
            },
        )
    return result


@router.get("/oauth/dry-run/status", response_model=OAuthDryRunStatusResponse)
def oauth_dry_run_status():
    return oauth_dry_run.build_oauth_dry_run_status()


@router.get("/oauth/read-only-proof", response_model=OAuthReadOnlyProofResponse)
def oauth_read_only_proof_status():
    return oauth_readiness.build_oauth_readonly_proof_result(metadata=True)


@router.post("/oauth/authorization/preview", response_model=OAuthAuthorizationUrlResponse)
def preview_oauth_authorization(payload: OAuthAuthorizationUrlRequest, request: Request):
    metadata_payload = _metadata_payload(payload.oauthProviderMetadata)
    result = oauth_dry_run.build_authorization_url(
        metadata_payload,
        redirect_uri=payload.redirectUri,
        ttl_seconds=payload.ttlSeconds,
        create_state=False,
    )
    _audit_oauth_authorization_event(action_type="oauth_authorization_preview", result=result, request=request)
    return result


@router.post("/oauth/authorization/prepare", response_model=OAuthAuthorizationUrlResponse)
def prepare_oauth_authorization(payload: OAuthAuthorizationUrlRequest, request: Request):
    metadata_payload = _metadata_payload(payload.oauthProviderMetadata)
    result = oauth_dry_run.build_authorization_url(
        metadata_payload,
        redirect_uri=payload.redirectUri,
        ttl_seconds=payload.ttlSeconds,
        create_state=True,
    )
    _audit_oauth_authorization_event(action_type="oauth_authorization_prepare", result=result, request=request)
    return result


@router.post("/oauth/callback/dry-run", response_model=OAuthCallbackDryRunResponse)
def oauth_callback_dry_run(payload: OAuthCallbackDryRunRequest, request: Request):
    identity, auth_status = _operator_context(request)
    return oauth_dry_run.validate_callback_dry_run(
        provider_id=payload.providerId,
        state=payload.state,
        code=payload.code,
        error=payload.error,
        redirect_uri=payload.redirectUri,
        audit=True,
        operator_id=identity["operatorId"],
        auth_mode=auth_status["authMode"],
        capability_present="view_dashboard" in set(identity.get("capabilities") or []),
    )


@router.get("/oauth/token-exchange/status", response_model=OAuthTokenExchangeStatusResponse)
def oauth_token_exchange_status():
    return oauth_token_exchange.build_token_exchange_status()


@router.post("/oauth/token-exchange/mock/validate", response_model=OAuthTokenExchangeResponse)
def validate_oauth_mock_token_exchange(payload: OAuthTokenExchangeRequest, request: Request):
    identity, auth_status = _operator_context(request)
    request_payload = payload.model_dump(exclude_none=True)
    extra = getattr(payload, "__pydantic_extra__", None)
    if isinstance(extra, dict):
        request_payload.update(extra)
    return oauth_token_exchange.validate_mock_token_exchange(
        request_payload,
        audit=True,
        operator_id=identity["operatorId"],
        auth_mode=auth_status["authMode"],
        capability_present="view_dashboard" in set(identity.get("capabilities") or []),
    )


@router.post("/oauth/token-exchange/live", response_model=OAuthTokenExchangeResponse)
def oauth_live_token_exchange_disabled(payload: OAuthTokenExchangeRequest, request: Request):
    identity, auth_status = _operator_context(request)
    request_payload = payload.model_dump(exclude_none=True)
    extra = getattr(payload, "__pydantic_extra__", None)
    if isinstance(extra, dict):
        request_payload.update(extra)
    return oauth_token_exchange.live_token_exchange_disabled(
        request_payload,
        audit=True,
        operator_id=identity["operatorId"],
        auth_mode=auth_status["authMode"],
        capability_present="view_dashboard" in set(identity.get("capabilities") or []),
    )


@router.get("/oauth/live-exchange/pilot/status", response_model=OAuthLiveExchangePilotStatusResponse)
def oauth_live_exchange_pilot_status():
    return oauth_token_exchange.build_live_exchange_pilot_status()


@router.post("/oauth/live-exchange/pilot", response_model=OAuthLiveExchangePilotResultResponse)
def oauth_live_exchange_pilot(payload: OAuthLiveExchangePilotRequest, request: Request):
    identity, auth_status = _operator_context(request)
    request_payload = payload.model_dump(exclude_none=True)
    return oauth_token_exchange.perform_live_exchange_pilot(
        request_payload,
        audit=True,
        operator_id=identity["operatorId"],
        auth_mode=auth_status["authMode"],
        capability_present="edit_topic_config" in set(identity.get("capabilities") or []),
        operator_identity=identity,
    )


@router.get("/oauth/live-exchange/smoke/real/status", response_model=OAuthRealProviderSmokeStatusResponse)
def oauth_real_provider_smoke_status():
    return oauth_token_exchange.build_real_provider_smoke_status()


@router.post("/oauth/live-exchange/smoke/real", response_model=OAuthRealProviderSmokeResultResponse)
def oauth_real_provider_smoke(payload: OAuthRealProviderSmokeRequest, request: Request):
    identity, auth_status = _operator_context(request)
    request_payload = payload.model_dump(exclude_none=True)
    extra = getattr(payload.oauthProviderMetadata, "__pydantic_extra__", None)
    if isinstance(extra, dict):
        request_payload["oauthProviderMetadata"] = {
            **request_payload.get("oauthProviderMetadata", {}),
            **extra,
        }
    return oauth_token_exchange.perform_real_provider_smoke(
        request_payload,
        audit=True,
        operator_id=identity["operatorId"],
        auth_mode=auth_status["authMode"],
        capability_present="edit_topic_config" in set(identity.get("capabilities") or []),
        operator_identity=identity,
    )


@router.get("/oauth/live-exchange/smoke/result/summary", response_model=OAuthSmokeResultSummaryResponse)
def oauth_smoke_result_summary():
    return oauth_smoke_results.build_smoke_result_summary()


@router.get("/provider-orchestration/safety-matrix", response_model=ProviderOrchestrationSafetyMatrixResponse)
def provider_orchestration_safety_matrix(request: Request):
    identity, _auth_status = _operator_context(request)
    return provider_orchestration.build_provider_orchestration_safety_matrix(operator_identity=identity)


@router.post("/provider-orchestration/plan/preview", response_model=ProviderOrchestrationDryRunPlanResponse)
def preview_provider_orchestration_dry_run_plan(payload: ProviderOrchestrationDryRunPlanRequest, request: Request):
    identity, _auth_status = _operator_context(request)
    return provider_orchestration.build_provider_orchestration_dry_run_plan(
        workflow_type=payload.workflowType,
        topic_id=payload.topicId,
        provider_config_id=payload.providerConfigId,
        operator_identity=identity,
    )


@router.post("/provider-orchestration/plan/replay/preview", response_model=ProviderOrchestrationReplayResultResponse)
def preview_provider_orchestration_plan_replay(payload: dict):
    return provider_orchestration.verify_provider_orchestration_plan(payload)


@router.get("/provider-orchestration/plan/summary", response_model=ProviderOrchestrationPlanSummaryResponse)
def provider_orchestration_plan_summary(request: Request):
    identity, _auth_status = _operator_context(request)
    plan = provider_orchestration.build_provider_orchestration_dry_run_plan(operator_identity=identity)
    return provider_orchestration.build_provider_orchestration_plan_summary(plan)


@router.post("/provider-orchestration/fixture-execution/preview", response_model=ProviderOrchestrationExecutionResultResponse)
def preview_provider_orchestration_fixture_execution(payload: ProviderOrchestrationExecutionRequest, request: Request):
    identity, _auth_status = _operator_context(request)
    return provider_orchestration.execute_provider_orchestration_fixture_plan(
        workflow_type=payload.workflowType,
        topic_id=payload.topicId,
        provider_config_id=payload.providerConfigId,
        limit=payload.limit,
        dry_run=True,
        force_refresh=payload.forceRefresh,
        operator_identity=identity,
    )


@router.post("/provider-orchestration/fixture-execution/replay/preview", response_model=ProviderOrchestrationExecutionReplayResponse)
def preview_provider_orchestration_fixture_replay(payload: dict):
    return provider_orchestration.verify_provider_orchestration_execution_result(payload)


@router.get("/provider-orchestration/fixture-execution/summary", response_model=ProviderOrchestrationExecutionSummaryResponse)
def provider_orchestration_fixture_execution_summary(request: Request):
    identity, _auth_status = _operator_context(request)
    return provider_orchestration.build_provider_orchestration_execution_summary(operator_identity=identity)


@router.post("/oauth/live-exchange/smoke/evidence-freeze/preview", response_model=OAuthSmokeEvidenceFreezeResponse)
def preview_oauth_smoke_evidence_freeze(payload: OAuthSmokeEvidenceFreezeRequest):
    return oauth_smoke_results.preview_smoke_evidence_freeze(payload.model_dump(exclude_none=True))


@router.post("/oauth/live-exchange/smoke/replay/preview", response_model=OAuthSmokeReplaySummaryResponse)
def preview_oauth_smoke_replay(payload: OAuthSmokeEvidenceFreezeRequest):
    return oauth_smoke_results.preview_smoke_replay(payload.model_dump(exclude_none=True))


@router.get("/oauth/live-exchange/evidence/package", response_model=OAuthLivePilotEvidencePackageResponse)
def oauth_live_pilot_evidence_package_status(request: Request):
    identity, _auth_status = _operator_context(request)
    return oauth_live_evidence.build_live_pilot_evidence_package(operator_identity=identity)


@router.post("/oauth/live-exchange/evidence/package/preview", response_model=OAuthLivePilotEvidencePackageResponse)
def preview_oauth_live_pilot_evidence_package(payload: OAuthLiveExchangeReadinessRequest, request: Request):
    identity, _auth_status = _operator_context(request)
    metadata_payload = _metadata_payload(payload.oauthProviderMetadata)
    return oauth_live_evidence.build_live_pilot_evidence_package(
        provider_metadata=metadata_payload,
        redirect_uri=payload.redirectUri,
        operator_identity=identity,
    )


@router.post("/oauth/live-exchange/evidence/replay/preview", response_model=OAuthLivePilotReplayVerificationResponse)
def preview_oauth_live_pilot_replay_verification(payload: dict):
    return oauth_live_evidence.verify_live_pilot_evidence_package(payload)


@router.get("/oauth/account-links/status", response_model=OAuthAccountLinkHealthResponse)
def oauth_account_link_status():
    return oauth_account_links.build_account_link_health_summary()


@router.get("/oauth/account-links/health", response_model=OAuthAccountLinkHealthResponse)
def oauth_account_link_health():
    return oauth_account_links.build_account_link_health_summary()


@router.get("/oauth/account-links", response_model=OAuthAccountLinkListResponse)
def list_oauth_account_links():
    return oauth_account_links.list_account_links()


@router.post("/oauth/account-links/preview", response_model=OAuthAccountLinkOperationResponse)
def preview_oauth_account_link(payload: OAuthAccountLinkMetadataRequest, request: Request):
    identity, auth_status = _operator_context(request)
    result = oauth_account_links.build_account_link_preview(_account_link_payload(payload), operator_identity=identity)
    operator_audit.record_event(
        action_type="oauth_account_link_preview",
        decision="preview_only" if result.get("accepted") else "rejected",
        operator_id=identity["operatorId"],
        auth_mode=auth_status["authMode"],
        capability_required="view_dashboard",
        capability_present="view_dashboard" in set(identity.get("capabilities") or []),
        reason_codes=list(result.get("reasonCodes") or []),
        target_type="oauth_account_link",
        target_id=(result.get("accountLink") or {}).get("accountLinkId"),
        risk_level="read_only",
        metadata={
            "accountLinkId": (result.get("accountLink") or {}).get("accountLinkId"),
            "providerId": (result.get("accountLink") or {}).get("providerId"),
            "linkStored": False,
            "tokenStorageEnabled": False,
            "liveTokenExchangePerformed": False,
            "externalAuthCallPerformed": False,
        },
    )
    return result


@router.post("/oauth/account-links/mock", response_model=OAuthAccountLinkOperationResponse)
def save_oauth_mock_account_link(payload: OAuthAccountLinkMetadataRequest, request: Request):
    identity, auth_status = _operator_context(request)
    capability_decision = action_capabilities.evaluate_action_capability(
        "oauth_account_link_save_mock",
        operator_capabilities=identity["capabilities"],
        fallback_risk_level="config_write",
    )
    if not capability_decision["allowed"]:
        operator_audit.record_event(
            action_type="oauth_account_link_save_mock",
            decision="denied",
            operator_id=identity["operatorId"],
            auth_mode=auth_status["authMode"],
            capability_required=capability_decision["capabilityRequired"] or "edit_topic_config",
            capability_present=bool(capability_decision["capabilityPresent"]),
            reason_codes=list(capability_decision["reasonCodes"]),
            target_type="oauth_account_link",
            target_id=payload.providerId,
            risk_level="config_write",
            metadata={"missingCapabilities": capability_decision["missingCapabilities"]},
        )
        raise HTTPException(
            status_code=403,
            detail={
                "message": "Required capability missing: edit_topic_config",
                "reason_codes": capability_decision["reasonCodes"],
                "capability_required": capability_decision["capabilityRequired"],
                "missing_capabilities": capability_decision["missingCapabilities"],
            },
        )
    result = oauth_account_links.save_mock_account_link(
        _account_link_payload(payload),
        audit=True,
        operator_id=identity["operatorId"],
        auth_mode=auth_status["authMode"],
        capability_present=True,
        operator_identity=identity,
    )
    if not result.get("accepted"):
        raise HTTPException(
            status_code=400,
            detail={
                "message": "OAuth account link metadata contains token-like or raw subject fields.",
                "reason_codes": result.get("reasonCodes") or [],
                "link_stored": False,
                "token_storage_enabled": False,
                "live_token_exchange_performed": False,
            },
        )
    return result


@router.get("/oauth/session-binding/preview", response_model=OAuthSessionBindingResponse)
def preview_oauth_session_binding(request: Request):
    identity, _auth_status = _operator_context(request)
    return oauth_account_links.build_session_binding_preview(identity)


@router.get("/oauth/session-capability-policy", response_model=OAuthSessionCapabilityPolicyResponse)
def oauth_session_capability_policy(request: Request):
    identity, _auth_status = _operator_context(request)
    return oauth_account_links.build_session_capability_policy(identity)


@router.get("/oauth/flow-timeline", response_model=OAuthFlowTimelineResponse)
def oauth_flow_timeline_status():
    return oauth_account_links.build_oauth_flow_timeline()


@router.get("/oauth/live-exchange/readiness", response_model=OAuthLiveExchangeReadinessResponse)
def oauth_live_exchange_readiness_status(request: Request):
    identity, _auth_status = _operator_context(request)
    return oauth_live_readiness.build_live_exchange_readiness(operator_identity=identity)


@router.post("/oauth/live-exchange/readiness/preview", response_model=OAuthLiveExchangeReadinessResponse)
def preview_oauth_live_exchange_readiness(payload: OAuthLiveExchangeReadinessRequest, request: Request):
    identity, auth_status = _operator_context(request)
    metadata_payload = _metadata_payload(payload.oauthProviderMetadata)
    return oauth_live_readiness.build_live_exchange_readiness(
        provider_metadata=metadata_payload,
        redirect_uri=payload.redirectUri,
        operator_identity=identity,
        audit=True,
        operator_id=identity["operatorId"],
        auth_mode=auth_status["authMode"],
        capability_present="view_dashboard" in set(identity.get("capabilities") or []),
    )


@router.get("/oauth/live-exchange/readiness/remediation", response_model=OAuthReadinessRemediationSummaryResponse)
def oauth_live_exchange_readiness_remediation_status(request: Request):
    identity, _auth_status = _operator_context(request)
    readiness = oauth_live_readiness.build_live_exchange_readiness(operator_identity=identity)
    return oauth_live_readiness.build_readiness_remediation(readiness=readiness)


@router.post("/oauth/live-exchange/readiness/remediation/preview", response_model=OAuthReadinessRemediationSummaryResponse)
def preview_oauth_live_exchange_readiness_remediation(payload: OAuthLiveExchangeReadinessRequest, request: Request):
    identity, _auth_status = _operator_context(request)
    metadata_payload = _metadata_payload(payload.oauthProviderMetadata)
    readiness = oauth_live_readiness.build_live_exchange_readiness(
        provider_metadata=metadata_payload,
        redirect_uri=payload.redirectUri,
        operator_identity=identity,
    )
    return oauth_live_readiness.build_readiness_remediation(readiness=readiness, provider_metadata=metadata_payload)


@router.get("/oauth/preflight/package", response_model=OAuthPreflightPackageResponse)
def oauth_preflight_package_status(request: Request):
    identity, _auth_status = _operator_context(request)
    return oauth_live_readiness.build_preflight_package(operator_identity=identity)


@router.post("/oauth/preflight/package/preview", response_model=OAuthPreflightPackageResponse)
def preview_oauth_preflight_package(payload: OAuthLiveExchangeReadinessRequest, request: Request):
    identity, _auth_status = _operator_context(request)
    metadata_payload = _metadata_payload(payload.oauthProviderMetadata)
    return oauth_live_readiness.build_preflight_package(
        provider_metadata=metadata_payload,
        redirect_uri=payload.redirectUri,
        operator_identity=identity,
    )


@router.post("/oauth/preflight/checklist/preview", response_model=OAuthOperatorAcceptanceChecklistResponse)
def preview_oauth_operator_acceptance_checklist(payload: OAuthOperatorAcceptanceChecklistRequest, request: Request):
    identity, _auth_status = _operator_context(request)
    return oauth_live_readiness.validate_operator_acceptance_checklist(
        payload.model_dump(exclude_none=True),
        operator_identity=identity,
    )


@router.post("/oauth/account-links/{account_link_id}/revoke-placeholder", response_model=OAuthAccountLinkOperationResponse)
def revoke_oauth_account_link_placeholder(account_link_id: str, request: Request):
    identity, auth_status = _operator_context(request)
    capability_decision = action_capabilities.evaluate_action_capability(
        "oauth_account_link_revoke_placeholder",
        operator_capabilities=identity["capabilities"],
        fallback_risk_level="config_write",
    )
    if not capability_decision["allowed"]:
        operator_audit.record_event(
            action_type="oauth_account_link_revoke_placeholder",
            decision="denied",
            operator_id=identity["operatorId"],
            auth_mode=auth_status["authMode"],
            capability_required=capability_decision["capabilityRequired"] or "edit_topic_config",
            capability_present=bool(capability_decision["capabilityPresent"]),
            reason_codes=list(capability_decision["reasonCodes"]),
            target_type="oauth_account_link",
            target_id=account_link_id,
            risk_level="config_write",
            metadata={
                "missingCapabilities": capability_decision["missingCapabilities"],
                "externalAuthCallPerformed": False,
                "tokenRevocationPerformed": False,
            },
        )
        raise HTTPException(
            status_code=403,
            detail={
                "message": "Required capability missing: edit_topic_config",
                "reason_codes": capability_decision["reasonCodes"],
                "capability_required": capability_decision["capabilityRequired"],
                "missing_capabilities": capability_decision["missingCapabilities"],
            },
        )
    return oauth_account_links.revoke_account_link_placeholder(
        account_link_id,
        audit=True,
        operator_id=identity["operatorId"],
        auth_mode=auth_status["authMode"],
        capability_present=True,
        operator_identity=identity,
    )


@router.get("/login")
def login(request: Request, return_to: str | None = Query(default=None)):
    try:
        return build_oidc_login_redirect(request, return_to=return_to)
    except AuthConfigurationError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": exc.reason_code, "message": str(exc)},
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/callback")
def auth_callback(request: Request, code: str | None = None, state: str | None = None):
    try:
        return complete_oidc_callback(request, code=code, state=state)
    except AuthConfigurationError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": exc.reason_code, "message": str(exc)},
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/logout", response_model=LogoutResponse)
def logout(request: Request):
    revoke_current_product_session(request)
    response = JSONResponse({"auth_mode": get_auth_settings().auth_mode, "logged_out": True})
    clear_product_session_cookie(response)
    return response
