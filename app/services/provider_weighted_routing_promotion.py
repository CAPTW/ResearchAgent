"""Proof-aware provider routing promotion status.

This module is deliberately non-live. It does not import provider SDKs, run
provider commands, read credentials, or make network calls. It only converts the
existing provider proof/status payload into a deterministic planning policy.
"""

from __future__ import annotations

from typing import Any, Mapping


CODEX_PROVIDER_FAMILY_ID = "codex"
CODEX_SDK_SURFACE_ID = "codex_sdk_python"

PRODUCT_TO_INTERNAL_OUTPUT_CLASS = {
    "paper_review": "research_output",
    "weekly_report": "report_draft",
}
INTERNAL_TO_PRODUCT_OUTPUT_CLASS = {value: key for key, value in PRODUCT_TO_INTERNAL_OUTPUT_CLASS.items()}
OUTPUT_CLASS_PROOF_LEVELS = {
    "research_output": "research_output_verified_tiny",
    "report_draft": "report_draft_verified_tiny",
}


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _product_output_class(output_class: str) -> str | None:
    if output_class in PRODUCT_TO_INTERNAL_OUTPUT_CLASS:
        return output_class
    return INTERNAL_TO_PRODUCT_OUTPUT_CLASS.get(output_class)


def _internal_output_class(product_output_class: str) -> str:
    return PRODUCT_TO_INTERNAL_OUTPUT_CLASS[product_output_class]


def _output_proof(proof_state: Mapping[str, Any], product_output_class: str) -> Mapping[str, Any]:
    proofs = _as_mapping(proof_state.get("providerOutputClassProofStatus"))
    return _as_mapping(proofs.get(product_output_class))


def _output_gate(proof_state: Mapping[str, Any], product_output_class: str) -> Mapping[str, Any]:
    gates = _as_mapping(proof_state.get("outputClassGateStatus"))
    return _as_mapping(gates.get(product_output_class))


def _output_class_proof_accepted(proof_state: Mapping[str, Any], product_output_class: str) -> bool:
    proof = _output_proof(proof_state, product_output_class)
    gate = _output_gate(proof_state, product_output_class)
    return (
        proof.get("providerFamilyId") == CODEX_PROVIDER_FAMILY_ID
        and proof.get("surfaceId") == CODEX_SDK_SURFACE_ID
        and proof.get("proofMode") == "output_class_live"
        and bool(proof.get("acceptedOutputClassProof"))
        and bool(gate.get("allowed"))
        and gate.get("currentProof") == "present"
    )


def _real_apply_applied(value: Mapping[str, Any]) -> bool:
    nested = _as_mapping(value.get("realDbStatus"))
    apply_statuses = {
        str(value.get("status") or ""),
        str(value.get("realApplyStatus") or ""),
        str(nested.get("status") or ""),
        str(nested.get("realApplyStatus") or ""),
    }
    return bool(
        apply_statuses
        & {
            "real_apply_applied",
            "real_apply_already_applied",
            "already_applied",
        }
    )


def _persisted_workflow_ready(proof_state: Mapping[str, Any], product_output_class: str) -> bool:
    if product_output_class == "paper_review":
        return _real_apply_applied(_as_mapping(proof_state.get("providerReviewIntegrationStatus")))
    if product_output_class == "weekly_report":
        return _real_apply_applied(_as_mapping(proof_state.get("providerReportIntegrationStatus")))
    return False


def _persisted_workflow_status(proof_state: Mapping[str, Any], product_output_class: str) -> str:
    if product_output_class == "paper_review":
        status = _as_mapping(proof_state.get("providerReviewIntegrationStatus"))
    else:
        status = _as_mapping(proof_state.get("providerReportIntegrationStatus"))
    if _real_apply_applied(status):
        return "real_apply_applied"
    return str(status.get("realApplyStatus") or status.get("status") or "not_applied")


def _output_class_capability(proof_state: Mapping[str, Any], product_output_class: str) -> dict[str, Any]:
    internal_output_class = _internal_output_class(product_output_class)
    proof = _output_proof(proof_state, product_output_class)
    gate = _output_gate(proof_state, product_output_class)
    proof_accepted = _output_class_proof_accepted(proof_state, product_output_class)
    workflow_ready = _persisted_workflow_ready(proof_state, product_output_class)
    planning_allowed = proof_accepted and workflow_ready
    reason_codes = [
        "output_class_proof_accepted" if proof_accepted else "output_class_proof_missing",
        "persisted_workflow_available" if workflow_ready else "persisted_workflow_missing",
        "planning_only_non_live_policy",
        "live_execution_requires_owner_approval",
        "provider_auth_not_app_oauth_oidc",
        "local_mcp_not_public_connector_proof",
        "production_external_beta_no_go",
    ]
    return {
        "productOutputClass": product_output_class,
        "internalOutputClass": internal_output_class,
        "proofMode": proof.get("proofMode") if proof_accepted else "output_class_live",
        "requiredProof": "output_class_live",
        "requiredProofLevel": OUTPUT_CLASS_PROOF_LEVELS[internal_output_class],
        "currentProof": gate.get("currentProof") if proof_accepted else "missing",
        "artifactRecorded": bool(proof.get("artifactRecorded")) if proof_accepted else False,
        "artifactRef": proof.get("artifactRef") if proof_accepted else None,
        "proofAccepted": proof_accepted,
        "persistedWorkflowStatus": _persisted_workflow_status(proof_state, product_output_class),
        "persistedWorkflowReady": workflow_ready,
        "planningAllowed": planning_allowed,
        "liveExecutionAllowed": False,
        "wouldExecuteProvider": False,
        "executionWeight": 0,
        "planningWeight": 100 if planning_allowed else 0,
        "reasonCodes": reason_codes,
    }


def _codex_sdk_candidate(proof_state: Mapping[str, Any]) -> dict[str, Any]:
    capabilities = [
        _output_class_capability(proof_state, "paper_review"),
        _output_class_capability(proof_state, "weekly_report"),
    ]
    planning_output_classes = [
        str(capability["internalOutputClass"]) for capability in capabilities if capability["planningAllowed"]
    ]
    return {
        "providerFamilyId": CODEX_PROVIDER_FAMILY_ID,
        "surfaceId": CODEX_SDK_SURFACE_ID,
        "displayName": "Codex SDK",
        "executionSurfaceType": "sdk",
        "adapterKind": "sdk_adapter_scaffold",
        "backendRole": "primary_ai_backend",
        "mainAiCandidate": True,
        "weightedRoutingCandidate": True,
        "preferredForPlanning": bool(planning_output_classes),
        "preferredForOutputClasses": planning_output_classes,
        "outputClassCapabilities": capabilities,
        "defaultBlocked": True,
        "mayRunNow": False,
        "liveExecutionAllowed": False,
        "wouldExecuteProvider": False,
        "executionWeight": 0,
        "planningWeight": 100 if planning_output_classes else 0,
        "providerAuthIsAppOauthOidc": False,
        "localMcpIsPublicConnector": False,
        "productionReady": False,
        "externalBetaReady": False,
        "reasonCodes": [
            "codex_sdk_output_class_proof_aware_candidate",
            "proof_aware_planning_only",
            "live_execution_requires_owner_approval",
            "runtime_provider_selection_fail_closed",
            "production_external_beta_no_go",
        ],
    }


def _blocked_candidate(
    *,
    provider_family_id: str,
    surface_id: str,
    display_name: str,
    execution_surface_type: str,
    adapter_kind: str,
    backend_role: str = "primary_ai_backend_candidate",
    reason_codes: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "providerFamilyId": provider_family_id,
        "surfaceId": surface_id,
        "displayName": display_name,
        "executionSurfaceType": execution_surface_type,
        "adapterKind": adapter_kind,
        "backendRole": backend_role,
        "mainAiCandidate": backend_role != "api_pipeline_skeleton",
        "weightedRoutingCandidate": True,
        "preferredForPlanning": False,
        "preferredForOutputClasses": [],
        "outputClassCapabilities": [],
        "defaultBlocked": True,
        "mayRunNow": False,
        "liveExecutionAllowed": False,
        "wouldExecuteProvider": False,
        "executionWeight": 0,
        "planningWeight": 0,
        "providerAuthIsAppOauthOidc": False,
        "localMcpIsPublicConnector": False,
        "productionReady": False,
        "externalBetaReady": False,
        "reasonCodes": [
            "output_class_proof_missing",
            "provider_surface_visible_but_blocked",
            "proof_aware_planning_only",
            "live_execution_requires_owner_approval",
            "provider_auth_not_app_oauth_oidc",
            "local_mcp_not_public_connector_proof",
            "production_external_beta_no_go",
            *(reason_codes or []),
        ],
    }


def _weighted_candidates(proof_state: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        _codex_sdk_candidate(proof_state),
        _blocked_candidate(
            provider_family_id="codex",
            surface_id="codex_cli",
            display_name="Codex CLI",
            execution_surface_type="cli",
            adapter_kind="cli_contract",
        ),
        _blocked_candidate(
            provider_family_id="claude",
            surface_id="claude_agent_sdk_python",
            display_name="Claude Agent SDK",
            execution_surface_type="sdk",
            adapter_kind="sdk_contract",
        ),
        _blocked_candidate(
            provider_family_id="claude",
            surface_id="claude_code_cli",
            display_name="Claude Code CLI",
            execution_surface_type="cli",
            adapter_kind="cli_contract",
        ),
        _blocked_candidate(
            provider_family_id="antigravity",
            surface_id="antigravity_sdk_python",
            display_name="Antigravity SDK",
            execution_surface_type="sdk",
            adapter_kind="sdk_contract",
        ),
        _blocked_candidate(
            provider_family_id="antigravity",
            surface_id="antigravity_cli",
            display_name="Antigravity CLI",
            execution_surface_type="cli",
            adapter_kind="cli_contract",
        ),
        _blocked_candidate(
            provider_family_id="openai_api_pipeline",
            surface_id="openai_api_pipeline",
            display_name="OpenAI API pipeline skeleton",
            execution_surface_type="api",
            adapter_kind="api_pipeline_skeleton",
            backend_role="api_pipeline_skeleton",
            reason_codes=["openai_api_pipeline_skeleton_only"],
        ),
    ]


def plan_provider_weighted_route(
    output_class: str,
    proof_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a non-live weighted routing plan for one output class.

    The returned route may identify a proof-aware planning candidate, but it
    never authorizes runtime provider execution.
    """

    state = _as_mapping(proof_state)
    product_output_class = _product_output_class(output_class)
    if not product_output_class:
        return {
            "outputClass": output_class,
            "productOutputClass": None,
            "planningAllowed": False,
            "selectedProviderFamilyId": None,
            "selectedSurfaceId": None,
            "liveExecutionAllowed": False,
            "wouldExecuteProvider": False,
            "providerCallCountPlanned": 0,
            "reasonCodes": ["unknown_output_class", "live_execution_requires_owner_approval"],
        }

    internal_output_class = _internal_output_class(product_output_class)
    capability = _output_class_capability(state, product_output_class)
    planning_allowed = bool(capability["planningAllowed"])
    return {
        "outputClass": internal_output_class,
        "productOutputClass": product_output_class,
        "planningAllowed": planning_allowed,
        "selectedProviderFamilyId": CODEX_PROVIDER_FAMILY_ID if planning_allowed else None,
        "selectedSurfaceId": CODEX_SDK_SURFACE_ID if planning_allowed else None,
        "selectedAdapterKind": "sdk_adapter_scaffold" if planning_allowed else None,
        "selectedExecutionSurfaceType": "sdk" if planning_allowed else None,
        "planningWeight": 100 if planning_allowed else 0,
        "executionWeight": 0,
        "liveExecutionAllowed": False,
        "wouldExecuteProvider": False,
        "providerCallCountPlanned": 0,
        "mayRunNow": False,
        "executionBlockedReason": "live_execution_requires_owner_approval",
        "reasonCodes": list(capability["reasonCodes"]),
    }


def describe_provider_routing_promotion_status(
    proof_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the safe GUI/API routing promotion status payload."""

    state = _as_mapping(proof_state)
    candidates = _weighted_candidates(state)
    routes = {
        "research_output": plan_provider_weighted_route("research_output", state),
        "report_draft": plan_provider_weighted_route("report_draft", state),
    }
    preferred_output_classes = [
        output_class for output_class, route in routes.items() if route.get("selectedSurfaceId") == CODEX_SDK_SURFACE_ID
    ]
    preferred_surface_id = CODEX_SDK_SURFACE_ID if preferred_output_classes else None
    return {
        "policyName": "provider_weighted_routing_promotion_policy",
        "policyMode": "planning_only",
        "availablePolicyModes": [
            "status_only",
            "planning_only",
            "proof_aware_dry_run",
            "owner_approved_live_required",
        ],
        "preferredMainAiProviderFamilyId": CODEX_PROVIDER_FAMILY_ID if preferred_surface_id else None,
        "preferredMainAiSurfaceId": preferred_surface_id,
        "preferredForOutputClasses": preferred_output_classes,
        "mainAiCandidateStatus": "proof_aware_preferred_for_planning" if preferred_surface_id else "no_verified_main_ai_candidate",
        "weightedRoutingCandidates": candidates,
        "outputClassRoutes": routes,
        "executionWeightsAllZero": True,
        "liveExecutionAllowed": False,
        "wouldExecuteProvider": False,
        "providerCallCountPlanned": 0,
        "mayRunNow": False,
        "requiresOwnerLiveApproval": True,
        "providerAuthIsAppOauthOidc": False,
        "localMcpIsPublicConnector": False,
        "productionReady": False,
        "externalBetaReady": False,
        "noSecretInspection": True,
        "noTokenCacheInspection": True,
        "noCredentialPathDisclosure": True,
        "reasonCodes": [
            "provider_weighted_routing_promotion_policy_nonlive",
            "codex_sdk_preferred_only_for_verified_planning" if preferred_surface_id else "output_class_proof_required",
            "all_live_execution_weights_zero",
            "runtime_provider_selection_fail_closed",
            "provider_auth_not_app_oauth_oidc",
            "local_mcp_not_public_connector_proof",
            "production_external_beta_no_go",
        ],
    }


def select_provider_main_ai_candidate(proof_state: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return the proof-aware main-AI planning candidate, if one exists."""

    policy = describe_provider_routing_promotion_status(proof_state)
    preferred_surface_id = policy.get("preferredMainAiSurfaceId")
    candidates = policy.get("weightedRoutingCandidates")
    if isinstance(candidates, list):
        for candidate in candidates:
            if isinstance(candidate, Mapping) and candidate.get("surfaceId") == preferred_surface_id:
                return dict(candidate)
    return {
        "providerFamilyId": None,
        "surfaceId": None,
        "preferredForPlanning": False,
        "liveExecutionAllowed": False,
        "wouldExecuteProvider": False,
        "executionWeight": 0,
        "planningWeight": 0,
        "reasonCodes": ["no_verified_main_ai_candidate", "output_class_proof_required"],
    }
