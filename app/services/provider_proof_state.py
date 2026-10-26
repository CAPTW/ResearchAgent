from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from app.services import provider_backend_scaffold
from app.services.provider_output_class_proof import build_output_class_proof
from app.services.public_mcp_connector_proof_plan import build_public_mcp_connector_proof_plan_status
from app.services.provider_report_integration import (
    REAL_APPLY_BLOCKED_STATUS as REPORT_REAL_APPLY_BLOCKED_STATUS,
    build_provider_report_integration_status,
)
from app.services.provider_review_integration import build_provider_review_integration_status
from app.services.providers.codex_sdk_provider import (
    validate_codex_sdk_paper_review_output_class_proof_artifact_contract,
    validate_codex_sdk_tiny_proof_artifact_contract,
    validate_codex_sdk_weekly_report_output_class_proof_artifact_contract,
)


PROVIDER_TINY_PROOF_ROOT = Path("output/provider-tiny-proof")
PROVIDER_OUTPUT_CLASS_PROOF_ROOT = Path("output/provider-output-class-proof")
CODEX_PROVIDER_FAMILY_ID = "codex"
CODEX_SDK_SURFACE_ID = "codex_sdk_python"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _relative_artifact_ref(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return ""


def _candidate_tiny_proof_paths(root: Path) -> list[Path]:
    proof_root = (root / PROVIDER_TINY_PROOF_ROOT).resolve()
    if not proof_root.exists() or not proof_root.is_dir():
        return []
    candidates: list[Path] = []
    for path in proof_root.glob("**/*.json"):
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if proof_root not in resolved.parents:
            continue
        if not resolved.is_file():
            continue
        candidates.append(resolved)
    return sorted(candidates, key=lambda item: (item.stat().st_mtime, item.as_posix()), reverse=True)


def _candidate_output_class_proof_paths(root: Path) -> list[Path]:
    proof_root = (root / PROVIDER_OUTPUT_CLASS_PROOF_ROOT).resolve()
    if not proof_root.exists() or not proof_root.is_dir():
        return []
    candidates: list[Path] = []
    for path in proof_root.glob("**/*.json"):
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if proof_root not in resolved.parents:
            continue
        if not resolved.is_file():
            continue
        candidates.append(resolved)
    return sorted(candidates, key=lambda item: (item.stat().st_mtime, item.as_posix()), reverse=True)


def _load_valid_codex_sdk_tiny_proof(root: Path) -> tuple[dict[str, Any] | None, list[str]]:
    reasons: list[str] = []
    for path in _candidate_tiny_proof_paths(root):
        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            reasons.append("tiny_proof_artifact_unreadable")
            continue
        if not isinstance(artifact, dict):
            reasons.append("tiny_proof_artifact_not_object")
            continue
        if artifact.get("providerFamilyId") != CODEX_PROVIDER_FAMILY_ID:
            continue
        if artifact.get("surfaceId") != CODEX_SDK_SURFACE_ID:
            continue
        validation = validate_codex_sdk_tiny_proof_artifact_contract(artifact)
        if not validation.get("valid"):
            reasons.extend(str(reason) for reason in validation.get("reasonCodes", []))
            reasons.append("tiny_proof_artifact_rejected_fail_closed")
            continue
        safe_artifact = {
            "artifactKind": artifact.get("artifactKind"),
            "providerFamilyId": artifact.get("providerFamilyId"),
            "providerId": artifact.get("providerId"),
            "profileId": artifact.get("profileId"),
            "surfaceId": artifact.get("surfaceId"),
            "executionSurfaceType": artifact.get("executionSurfaceType"),
            "adapterKind": artifact.get("adapterKind"),
            "authStrategyUsed": artifact.get("authStrategyUsed"),
            "proofMode": artifact.get("proofMode"),
            "tinyProofKind": artifact.get("tinyProofKind"),
            "proofStatusCandidate": artifact.get("proofStatusCandidate"),
            "providerSdkRunCount": int(artifact.get("providerSdkRunCount") or 0),
            "providerCommandCount": int(artifact.get("providerCommandCount") or 0),
            "providerCallCount": int(artifact.get("providerCallCount") or 0),
            "acceptedTinyProof": bool(artifact.get("acceptedTinyProof")),
            "outputClassProof": bool(artifact.get("outputClassProof")),
            "researchOutputAllowed": bool(artifact.get("researchOutputAllowed")),
            "reportDraftAllowed": bool(artifact.get("reportDraftAllowed")),
            "cannotUnlockOutputClass": bool(artifact.get("cannotUnlockOutputClass")),
            "artifactRecorded": True,
            "artifactRef": _relative_artifact_ref(path, root),
            "timestamp": artifact.get("timestamp"),
            "sentinelValidation": "passed",
        }
        return safe_artifact, []
    return None, list(dict.fromkeys(reasons))


def _load_valid_codex_sdk_paper_review_output_class_proof(root: Path) -> tuple[dict[str, Any] | None, list[str]]:
    reasons: list[str] = []
    for path in _candidate_output_class_proof_paths(root):
        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            reasons.append("output_class_proof_artifact_unreadable")
            continue
        if not isinstance(artifact, dict):
            reasons.append("output_class_proof_artifact_not_object")
            continue
        if artifact.get("providerFamilyId") != CODEX_PROVIDER_FAMILY_ID:
            continue
        if artifact.get("surfaceId") != CODEX_SDK_SURFACE_ID:
            continue
        if artifact.get("productOutputClass") != "paper_review":
            continue
        if artifact.get("internalOutputClass") != "research_output":
            continue
        validation = validate_codex_sdk_paper_review_output_class_proof_artifact_contract(artifact)
        if not validation.get("valid"):
            reasons.extend(str(reason) for reason in validation.get("reasonCodes", []))
            reasons.append("output_class_proof_artifact_rejected_fail_closed")
            continue
        safe_artifact = {
            "artifactKind": artifact.get("artifactKind"),
            "proofMode": artifact.get("proofMode"),
            "providerFamilyId": artifact.get("providerFamilyId"),
            "providerId": artifact.get("providerId"),
            "profileId": artifact.get("profileId"),
            "surfaceId": artifact.get("surfaceId"),
            "executionSurfaceType": artifact.get("executionSurfaceType"),
            "adapterKind": artifact.get("adapterKind"),
            "authStrategyUsed": artifact.get("authStrategyUsed"),
            "productOutputClass": artifact.get("productOutputClass"),
            "internalOutputClass": artifact.get("internalOutputClass"),
            "requiredProofStatus": artifact.get("requiredProofStatus"),
            "currentProofStatus": artifact.get("currentProofStatus"),
            "providerSdkRunCount": int(artifact.get("providerSdkRunCount") or 0),
            "providerCommandCount": int(artifact.get("providerCommandCount") or 0),
            "providerCallCount": int(artifact.get("providerCallCount") or 0),
            "acceptedOutputClassProof": bool(artifact.get("acceptedOutputClassProof")),
            "outputClassProof": bool(artifact.get("outputClassProof")),
            "researchOutputAllowed": bool(artifact.get("researchOutputAllowed")),
            "reportDraftAllowed": bool(artifact.get("reportDraftAllowed")),
            "weeklyReportProofStatus": artifact.get("weeklyReportProofStatus"),
            "topicId": artifact.get("topicId"),
            "paperId": artifact.get("paperId"),
            "artifactRecorded": True,
            "artifactRef": _relative_artifact_ref(path, root),
            "timestamp": artifact.get("timestamp"),
        }
        return safe_artifact, []
    return None, list(dict.fromkeys(reasons))


def _load_valid_codex_sdk_weekly_report_output_class_proof(root: Path) -> tuple[dict[str, Any] | None, list[str]]:
    reasons: list[str] = []
    for path in _candidate_output_class_proof_paths(root):
        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            reasons.append("weekly_report_output_class_proof_artifact_unreadable")
            continue
        if not isinstance(artifact, dict):
            reasons.append("weekly_report_output_class_proof_artifact_not_object")
            continue
        if artifact.get("providerFamilyId") != CODEX_PROVIDER_FAMILY_ID:
            continue
        if artifact.get("surfaceId") != CODEX_SDK_SURFACE_ID:
            continue
        if artifact.get("productOutputClass") != "weekly_report":
            continue
        if artifact.get("internalOutputClass") != "report_draft":
            continue
        validation = validate_codex_sdk_weekly_report_output_class_proof_artifact_contract(artifact)
        if not validation.get("valid"):
            reasons.extend(str(reason) for reason in validation.get("reasonCodes", []))
            reasons.append("weekly_report_output_class_proof_artifact_rejected_fail_closed")
            continue
        safe_artifact = {
            "artifactKind": artifact.get("artifactKind"),
            "proofMode": artifact.get("proofMode"),
            "providerFamilyId": artifact.get("providerFamilyId"),
            "providerId": artifact.get("providerId"),
            "profileId": artifact.get("profileId"),
            "surfaceId": artifact.get("surfaceId"),
            "executionSurfaceType": artifact.get("executionSurfaceType"),
            "adapterKind": artifact.get("adapterKind"),
            "authStrategyUsed": artifact.get("authStrategyUsed"),
            "productOutputClass": artifact.get("productOutputClass"),
            "internalOutputClass": artifact.get("internalOutputClass"),
            "requiredProofStatus": artifact.get("requiredProofStatus"),
            "currentProofStatus": artifact.get("currentProofStatus"),
            "providerSdkRunCount": int(artifact.get("providerSdkRunCount") or 0),
            "providerCommandCount": int(artifact.get("providerCommandCount") or 0),
            "providerCallCount": int(artifact.get("providerCallCount") or 0),
            "acceptedOutputClassProof": bool(artifact.get("acceptedOutputClassProof")),
            "outputClassProof": bool(artifact.get("outputClassProof")),
            "researchOutputAllowed": bool(artifact.get("researchOutputAllowed")),
            "reportDraftAllowed": bool(artifact.get("reportDraftAllowed")),
            "reportPersistenceInScope": bool(artifact.get("reportPersistenceInScope")),
            "dbWriteInScope": bool(artifact.get("dbWriteInScope")),
            "topicId": artifact.get("topicId"),
            "sourceProviderReviewIds": list(artifact.get("sourceProviderReviewIds") or []),
            "sourcePaperIds": list(artifact.get("sourcePaperIds") or []),
            "artifactRecorded": True,
            "artifactRef": _relative_artifact_ref(path, root),
            "timestamp": artifact.get("timestamp"),
        }
        return safe_artifact, []
    return None, list(dict.fromkeys(reasons))


def _provider_family_summaries() -> list[dict[str, Any]]:
    summary = provider_backend_scaffold.describe_provider_backend_families()
    families: list[dict[str, Any]] = []
    for family in summary.get("families", []):
        if not isinstance(family, Mapping):
            continue
        families.append(
            {
                "providerFamilyId": family.get("providerFamilyId"),
                "displayName": family.get("displayName"),
                "backendRole": family.get("backendRole"),
                "preferredByDefault": bool(family.get("preferredByDefault")),
                "supportsMainAiSelectionCandidate": bool(family.get("supportsMainAiSelectionCandidate")),
                "supportsWeightedRoutingCandidate": bool(family.get("supportsWeightedRoutingCandidate")),
                "outputClassCandidates": list(family.get("outputClassCandidates") or []),
                "provenOutputClasses": list(family.get("provenOutputClasses") or []),
                "defaultBlocked": bool(family.get("defaultBlocked")),
                "reasonCodes": list(family.get("reasonCodes") or []),
            }
        )
    return families


def _provider_surface_summaries() -> list[dict[str, Any]]:
    summary = provider_backend_scaffold.describe_provider_sdk_oauth_surfaces()
    surfaces: list[dict[str, Any]] = []
    for surface in summary.get("surfaces", []):
        if not isinstance(surface, Mapping):
            continue
        surfaces.append(
            {
                "providerFamilyId": surface.get("providerFamilyId"),
                "surfaceId": surface.get("surfaceId"),
                "displayName": surface.get("displayName"),
                "executionSurfaceType": surface.get("executionSurfaceType"),
                "adapterKind": surface.get("adapterKind"),
                "backendRole": surface.get("backendRole"),
                "status": surface.get("status"),
                "defaultBlocked": bool(surface.get("defaultBlocked")),
                "wouldExecute": bool(surface.get("wouldExecute")),
                "mayImportSdk": bool(surface.get("mayImportSdk")),
                "mayRunProviderCommand": bool(surface.get("mayRunProviderCommand")),
                "mayCallNetwork": bool(surface.get("mayCallNetwork")),
                "appOauthOidcInScope": bool(surface.get("appOauthOidcInScope")),
                "publicMcpConnectorInScope": bool(surface.get("publicMcpConnectorInScope")),
                "reasonCodes": list(surface.get("reasonCodes") or []),
            }
        )
    return surfaces


def _output_class_gate(
    product_output_class: str,
    accepted_proof: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if product_output_class in {"paper_review", "weekly_report"} and accepted_proof:
        contract_output_class = "research_output" if product_output_class == "paper_review" else "report_draft"
        proof_level = "research_output_verified_tiny" if product_output_class == "paper_review" else "report_draft_verified_tiny"
        paper_ids = (
            [str(accepted_proof.get("paperId") or "")]
            if product_output_class == "paper_review"
            else [str(item) for item in accepted_proof.get("sourcePaperIds") or []]
        )
        proof = build_output_class_proof(
            provider_id=CODEX_PROVIDER_FAMILY_ID,
            provider_profile_name=CODEX_SDK_SURFACE_ID,
            execution_surface="sdk",
            output_class=product_output_class,
            proof_mode="output_class_live",
            topic_id=str(accepted_proof.get("topicId") or ""),
            paper_ids=paper_ids,
            artifact_ref=str(accepted_proof.get("artifactRef") or ""),
            profile_status=proof_level,
            configured=True,
        ).to_dict()
        return {
            "productOutputClass": product_output_class,
            "outputClass": contract_output_class,
            "allowed": bool(proof.get("allowed")),
            "requiredProof": "output_class_live",
            "requiredProofLevel": proof.get("requiredProofLevel"),
            "currentProof": "present" if proof.get("accepted") else "missing",
            "currentProofLevel": proof.get("currentProofLevel"),
            "proofMode": proof.get("proofMode"),
            "status": "allowed" if proof.get("allowed") else "blocked",
            "blockedReason": proof.get("blockedReason"),
            "artifactRecorded": bool(accepted_proof.get("artifactRecorded")),
            "artifactRef": accepted_proof.get("artifactRef"),
            "reasonCodes": list(proof.get("reasonCodes") or []),
        }
    proof = build_output_class_proof(
        provider_id=CODEX_PROVIDER_FAMILY_ID,
        provider_profile_name=CODEX_SDK_SURFACE_ID,
        execution_surface="sdk",
        output_class=product_output_class,
        proof_mode="tiny_live",
        configured=True,
    ).to_dict()
    contract_output_class = "research_output" if product_output_class == "paper_review" else "report_draft"
    return {
        "productOutputClass": product_output_class,
        "outputClass": contract_output_class,
        "allowed": False,
        "requiredProof": "output_class_live",
        "requiredProofLevel": proof.get("requiredProofLevel"),
        "currentProof": "missing",
        "currentProofLevel": proof.get("currentProofLevel"),
        "proofMode": proof.get("proofMode"),
        "status": "blocked",
        "blockedReason": proof.get("blockedReason") or "output_class_proof_missing",
        "reasonCodes": list(proof.get("reasonCodes") or []),
    }


def build_provider_proof_state_status(repo_root: Path | None = None) -> dict[str, Any]:
    """Build a read-only GUI status model for provider proof state.

    This helper never imports/runs provider SDKs, starts app servers, executes provider
    commands, reads env files, or inspects credential/cache/session locations.
    """
    root = (repo_root or _repo_root()).resolve()
    artifact, artifact_reasons = _load_valid_codex_sdk_tiny_proof(root)
    output_artifact, output_artifact_reasons = _load_valid_codex_sdk_paper_review_output_class_proof(root)
    weekly_output_artifact, weekly_output_artifact_reasons = _load_valid_codex_sdk_weekly_report_output_class_proof(root)

    if artifact:
        tiny_status = {
            **artifact,
            "tinyProofStatus": "passed",
            "outputClassProof": False,
            "researchOutputAllowed": False,
            "reportDraftAllowed": False,
            "reasonCodes": [
                "codex_sdk_tiny_proof_passed",
                "provider_tiny_proof_only",
                "tiny_proof_not_output_class_proof",
                "output_class_proof_required",
            ],
        }
    else:
        tiny_status = {
            "providerFamilyId": CODEX_PROVIDER_FAMILY_ID,
            "providerId": CODEX_PROVIDER_FAMILY_ID,
            "profileId": CODEX_SDK_SURFACE_ID,
            "surfaceId": CODEX_SDK_SURFACE_ID,
            "executionSurfaceType": "sdk",
            "adapterKind": "sdk_adapter_scaffold",
            "tinyProofStatus": "missing",
            "artifactKind": "provider_tiny_proof",
            "tinyProofKind": "sdk_sentinel_prompt",
            "proofStatusCandidate": "live_verified_tiny",
            "providerSdkRunCount": 0,
            "providerCommandCount": 0,
            "providerCallCount": 0,
            "artifactRecorded": False,
            "outputClassProof": False,
            "researchOutputAllowed": False,
            "reportDraftAllowed": False,
            "cannotUnlockOutputClass": True,
            "reasonCodes": [
                "provider_tiny_proof_missing",
                "tiny_proof_not_output_class_proof",
                "output_class_proof_required",
                *artifact_reasons,
            ],
        }

    output_class_status = {
        "paper_review": output_artifact
        if output_artifact
        else {
            "providerFamilyId": CODEX_PROVIDER_FAMILY_ID,
            "providerId": CODEX_PROVIDER_FAMILY_ID,
            "profileId": CODEX_SDK_SURFACE_ID,
            "surfaceId": CODEX_SDK_SURFACE_ID,
            "executionSurfaceType": "sdk",
            "adapterKind": "sdk_adapter_scaffold",
            "productOutputClass": "paper_review",
            "internalOutputClass": "research_output",
            "proofMode": "output_class_live",
            "acceptedOutputClassProof": False,
            "outputClassProof": False,
            "researchOutputAllowed": False,
            "reportDraftAllowed": False,
            "artifactRecorded": False,
            "reasonCodes": ["paper_review_output_class_proof_missing", *output_artifact_reasons],
        },
        "weekly_report": weekly_output_artifact
        if weekly_output_artifact
        else {
            "providerFamilyId": CODEX_PROVIDER_FAMILY_ID,
            "providerId": CODEX_PROVIDER_FAMILY_ID,
            "profileId": CODEX_SDK_SURFACE_ID,
            "surfaceId": CODEX_SDK_SURFACE_ID,
            "executionSurfaceType": "sdk",
            "adapterKind": "sdk_adapter_scaffold",
            "productOutputClass": "weekly_report",
            "internalOutputClass": "report_draft",
            "proofMode": "output_class_live",
            "acceptedOutputClassProof": False,
            "outputClassProof": False,
            "researchOutputAllowed": bool(output_artifact and output_artifact.get("researchOutputAllowed")),
            "reportDraftAllowed": False,
            "artifactRecorded": False,
            "reasonCodes": ["weekly_report_output_class_proof_missing", *weekly_output_artifact_reasons],
        },
    }
    if output_artifact and output_artifact.get("artifactRef"):
        provider_review_integration = build_provider_review_integration_status(
            root / str(output_artifact["artifactRef"]),
            include_real_db_status=root == _repo_root().resolve(),
        )
    else:
        provider_review_integration = {
            "status": "real_apply_blocked_pending_owner_approval",
            "normalizedProviderReviewAvailable": False,
            "dryRunApplyPlan": {"status": "rejected_artifact", "applied": False},
            "reportContext": {
                "status": "provider_review_proof_missing",
                "providerReviewCount": 0,
                "reportDraftAllowed": False,
            },
            "realApplyStatus": "real_apply_blocked_pending_owner_approval",
            "researchOutputAllowed": False,
            "reportDraftAllowed": False,
            "providerAuthIsAppOauthOidc": False,
            "localMcpIsPublicConnector": False,
            "productionReady": False,
            "externalBetaReady": False,
            "noSecretInspection": True,
            "noTokenCacheInspection": True,
            "noCredentialPathDisclosure": True,
            "reasonCodes": ["provider_review_output_class_proof_missing"],
        }

    if weekly_output_artifact and weekly_output_artifact.get("artifactRef"):
        provider_report_integration = build_provider_report_integration_status(
            root / str(weekly_output_artifact["artifactRef"])
        )
    else:
        provider_report_integration = {
            "status": REPORT_REAL_APPLY_BLOCKED_STATUS,
            "normalizedProviderReportDraftAvailable": False,
            "dryRunApplyPlan": {"status": "rejected_artifact", "applied": False},
            "previewContext": {
                "status": "weekly_report_output_class_proof_missing",
                "providerReportDraftCount": 0,
                "reportDraftAllowed": False,
            },
            "qualityContext": {"status": "unavailable"},
            "realApplyStatus": REPORT_REAL_APPLY_BLOCKED_STATUS,
            "reportDraftAllowed": False,
            "researchOutputAllowed": False,
            "reportPersistenceInScope": False,
            "dbWriteInScope": False,
            "providerAuthIsAppOauthOidc": False,
            "localMcpIsPublicConnector": False,
            "productionReady": False,
            "externalBetaReady": False,
            "noSecretInspection": True,
            "noTokenCacheInspection": True,
            "noCredentialPathDisclosure": True,
            "reasonCodes": ["weekly_report_output_class_proof_missing"],
        }

    public_mcp_plan = build_public_mcp_connector_proof_plan_status(root)
    status = {
        "providerBackendFamilies": _provider_family_summaries(),
        "providerExecutionSurfaces": _provider_surface_summaries(),
        "providerTinyProofStatus": tiny_status,
        "providerOutputClassProofStatus": output_class_status,
        "providerReviewIntegrationStatus": provider_review_integration,
        "providerReportIntegrationStatus": provider_report_integration,
        "outputClassGateStatus": {
            "paper_review": _output_class_gate("paper_review", output_artifact),
            "weekly_report": _output_class_gate("weekly_report", weekly_output_artifact),
        },
        "appOauthOidcStatusSummary": {
            "providerAuthIsAppOauthOidc": False,
            "appOauthOidcInScope": False,
            "status": "separate_scope",
            "reasonCodes": ["provider_auth_not_app_oauth_oidc"],
        },
        "mcpConnectorStatusSummary": {
            "localMcpIsPublicConnector": False,
            "localMcpSatisfiesPublicConnector": False,
            "providerAuthSatisfiesPublicMcp": False,
            "publicMcpConnectorProofStatus": public_mcp_plan["publicMcpConnectorProofStatus"],
            "publicMcpConnectorProofSatisfied": False,
            "proofModeRequired": public_mcp_plan["proofModeRequired"],
            "status": "planned_not_proven",
            "endpointPolicyStatus": public_mcp_plan["endpointPolicyStatus"],
            "recommendedEndpointMode": public_mcp_plan["recommendedEndpointMode"],
            "endpointOptions": public_mcp_plan["endpointOptions"],
            "authPolicyStatus": public_mcp_plan["authPolicyStatus"],
            "recommendedAuthMode": public_mcp_plan["recommendedAuthMode"],
            "authPolicyOptions": public_mcp_plan["authPolicyOptions"],
            "protectedResourceMetadataPlan": public_mcp_plan["protectedResourceMetadataPlan"],
            "unauthenticatedChallengePlan": public_mcp_plan["unauthenticatedChallengePlan"],
            "authenticatedReadOnlyCallPlan": public_mcp_plan["authenticatedReadOnlyCallPlan"],
            "proofCredentialPolicy": public_mcp_plan["proofCredentialPolicy"],
            "tunnelProofPreconditions": public_mcp_plan["tunnelProofPreconditions"],
            "futureTunnelHandoffSummary": public_mcp_plan["futureTunnelHandoffSummary"],
            "runnableNow": public_mcp_plan["runnableNow"],
            "stopReason": public_mcp_plan["stopReason"],
            "localMcpConfigStatus": public_mcp_plan["localMcpConfigStatus"],
            "publicConnectorRequiresActualMcpJson": public_mcp_plan["publicConnectorRequiresActualMcpJson"],
            "preconditionStatus": public_mcp_plan["preconditionStatus"],
            "missingPreconditions": public_mcp_plan["missingPreconditions"],
            "surfaceInventory": public_mcp_plan["surfaceInventory"],
            "futurePreconditions": public_mcp_plan["futurePreconditions"],
            "futureArtifactContract": public_mcp_plan["futureArtifactContract"],
            "reasonCodes": [
                "local_mcp_not_public_connector_proof",
                "public_mcp_connector_proof_missing",
                "provider_auth_not_public_mcp_proof",
            ],
        },
        "productionReadinessSummary": {
            "productionReady": False,
            "externalBetaReady": False,
            "status": "no_go",
            "reasonCodes": ["production_external_beta_no_go"],
        },
        "providerLiveOutputPolicy": {
            # Owner-only optional framing for the provider live-output escape hatch.
            # Accepted output-class proof artifacts live under gitignored
            # ``output/provider-output-class-proof/`` and are never committed, so a
            # fresh checkout legitimately reports zero accepted proofs. That is the
            # designed default, not a regression: default runtime stays deterministic
            # fallback and live output requires explicit owner env + an accepted proof.
            "liveOutputClass": "owner_only_optional",
            "defaultRuntimeProvider": "deterministic_fallback",
            "proofArtifactStorage": "local_only_ignored",
            "proofArtifactsCommitted": False,
            "acceptedProofPresentInThisCheckout": bool(output_artifact or weekly_output_artifact),
            "freshCheckoutAcceptedProofCount": 0,
            "freshCheckoutZeroAcceptedIsExpected": True,
            "status": "owner_only_optional_local_proof",
            "reasonCodes": [
                "provider_live_output_owner_only_optional",
                "proof_artifacts_local_only_not_committed",
                "fresh_checkout_zero_accepted_expected",
                "default_runtime_deterministic_fallback",
            ],
        },
        "noSecretInspection": True,
        "noTokenCacheInspection": True,
        "noCredentialPathDisclosure": True,
        "reasonCodes": [
            "provider_proof_state_gui_status",
            "codex_sdk_provider_surface_visible",
            "tiny_proof_not_output_class_proof",
            *(
                ["paper_review_output_class_proof_present"]
                if output_artifact
                else ["output_class_proof_required"]
            ),
            *(
                ["weekly_report_output_class_proof_present"]
                if weekly_output_artifact
                else ["weekly_report_output_class_proof_missing"]
            ),
        ],
    }
    from app.services.provider_weighted_routing_promotion import describe_provider_routing_promotion_status

    status["routingPromotionPolicy"] = describe_provider_routing_promotion_status(status)
    status["reasonCodes"].append("provider_weighted_routing_promotion_policy_nonlive")
    return status
