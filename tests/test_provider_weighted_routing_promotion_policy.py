import importlib
import json
import socket
import subprocess

from app.services import provider_weighted_routing_promotion as promotion
from app.services.provider_proof_state import build_provider_proof_state_status
from app.services.providers import get_active_provider


def _proof_state(
    *,
    paper_proof: bool = True,
    weekly_proof: bool = True,
    review_applied: bool = True,
    report_applied: bool = True,
) -> dict:
    output_proofs = {
        "paper_review": {
            "providerFamilyId": "codex",
            "surfaceId": "codex_sdk_python",
            "proofMode": "output_class_live",
            "productOutputClass": "paper_review",
            "internalOutputClass": "research_output",
            "acceptedOutputClassProof": paper_proof,
            "artifactRecorded": paper_proof,
            "artifactRef": "output/provider-output-class-proof/paper/codex_sdk_paper_review_output_class_live_proof.json"
            if paper_proof
            else None,
        },
        "weekly_report": {
            "providerFamilyId": "codex",
            "surfaceId": "codex_sdk_python",
            "proofMode": "output_class_live",
            "productOutputClass": "weekly_report",
            "internalOutputClass": "report_draft",
            "acceptedOutputClassProof": weekly_proof,
            "artifactRecorded": weekly_proof,
            "artifactRef": "output/provider-output-class-proof/weekly/codex_sdk_weekly_report_output_class_live_proof.json"
            if weekly_proof
            else None,
        },
    }
    gates = {
        "paper_review": {
            "productOutputClass": "paper_review",
            "outputClass": "research_output",
            "allowed": paper_proof,
            "requiredProof": "output_class_live",
            "currentProof": "present" if paper_proof else "missing",
            "proofMode": "output_class_live",
        },
        "weekly_report": {
            "productOutputClass": "weekly_report",
            "outputClass": "report_draft",
            "allowed": weekly_proof,
            "requiredProof": "output_class_live",
            "currentProof": "present" if weekly_proof else "missing",
            "proofMode": "output_class_live",
        },
    }
    return {
        "providerTinyProofStatus": {
            "providerFamilyId": "codex",
            "surfaceId": "codex_sdk_python",
            "tinyProofStatus": "passed",
            "proofStatusCandidate": "live_verified_tiny",
            "acceptedTinyProof": True,
            "outputClassProof": False,
            "researchOutputAllowed": False,
            "reportDraftAllowed": False,
        },
        "providerOutputClassProofStatus": output_proofs,
        "outputClassGateStatus": gates,
        "providerReviewIntegrationStatus": {
            "status": "real_apply_applied" if review_applied else "dry_run_ready",
            "realApplyStatus": "real_apply_already_applied" if review_applied else "real_apply_blocked_pending_owner_approval",
            "providerReviewId": "provider-review-ce78f0c10898f231",
            "researchOutputAllowed": paper_proof,
            "reportDraftAllowed": False,
        },
        "providerReportIntegrationStatus": {
            "status": "real_apply_applied" if report_applied else "dry_run_ready",
            "realApplyStatus": "real_apply_already_applied" if report_applied else "real_apply_blocked_pending_owner_approval",
            "providerReportDraftId": "provider-report-draft-1c8099c4293a5274",
            "reportDraftAllowed": weekly_proof,
            "researchOutputAllowed": False,
        },
        "productionReadinessSummary": {"productionReady": False, "externalBetaReady": False},
    }


def _candidate(status: dict, surface_id: str) -> dict:
    candidates = status["weightedRoutingCandidates"]
    return next(candidate for candidate in candidates if candidate["surfaceId"] == surface_id)


def test_codex_sdk_is_proof_aware_preferred_planning_candidate_without_live_execution():
    status = promotion.describe_provider_routing_promotion_status(_proof_state())

    assert status["policyMode"] == "planning_only"
    assert status["preferredMainAiProviderFamilyId"] == "codex"
    assert status["preferredMainAiSurfaceId"] == "codex_sdk_python"
    assert status["preferredForOutputClasses"] == ["research_output", "report_draft"]
    assert status["executionWeightsAllZero"] is True
    assert status["liveExecutionAllowed"] is False
    assert status["wouldExecuteProvider"] is False
    assert status["providerCallCountPlanned"] == 0

    codex = _candidate(status, "codex_sdk_python")
    assert codex["preferredForPlanning"] is True
    assert codex["preferredForOutputClasses"] == ["research_output", "report_draft"]
    assert codex["planningWeight"] == 100
    assert codex["executionWeight"] == 0
    assert codex["mayRunNow"] is False
    assert codex["liveExecutionAllowed"] is False

    assert status["outputClassRoutes"]["research_output"]["selectedSurfaceId"] == "codex_sdk_python"
    assert status["outputClassRoutes"]["report_draft"]["selectedSurfaceId"] == "codex_sdk_python"
    assert status["outputClassRoutes"]["research_output"]["planningAllowed"] is True
    assert status["outputClassRoutes"]["report_draft"]["planningAllowed"] is True


def test_unverified_surfaces_remain_visible_but_weighted_zero_and_blocked():
    status = promotion.describe_provider_routing_promotion_status(_proof_state())
    for surface_id in [
        "codex_cli",
        "claude_agent_sdk_python",
        "claude_code_cli",
        "antigravity_sdk_python",
        "antigravity_cli",
        "openai_api_pipeline",
    ]:
        candidate = _candidate(status, surface_id)
        assert candidate["weightedRoutingCandidate"] is True
        assert candidate["preferredForPlanning"] is False
        assert candidate["planningWeight"] == 0
        assert candidate["executionWeight"] == 0
        assert candidate["liveExecutionAllowed"] is False
        assert candidate["wouldExecuteProvider"] is False
        assert "output_class_proof_missing" in candidate["reasonCodes"]

    openai = _candidate(status, "openai_api_pipeline")
    assert openai["backendRole"] == "api_pipeline_skeleton"
    assert "openai_api_pipeline_skeleton_only" in openai["reasonCodes"]


def test_tiny_proof_does_not_promote_or_unlock_output_classes():
    proof_state = _proof_state(paper_proof=False, weekly_proof=False, review_applied=False, report_applied=False)
    status = promotion.describe_provider_routing_promotion_status(proof_state)

    assert status["preferredMainAiSurfaceId"] is None
    assert status["mainAiCandidateStatus"] == "no_verified_main_ai_candidate"
    assert status["outputClassRoutes"]["research_output"]["planningAllowed"] is False
    assert status["outputClassRoutes"]["report_draft"]["planningAllowed"] is False
    assert status["outputClassRoutes"]["research_output"]["selectedSurfaceId"] is None
    assert status["outputClassRoutes"]["report_draft"]["selectedSurfaceId"] is None
    assert _candidate(status, "codex_sdk_python")["planningWeight"] == 0


def test_paper_review_proof_does_not_cross_unlock_report_draft_route():
    proof_state = _proof_state(weekly_proof=False, report_applied=False)
    status = promotion.describe_provider_routing_promotion_status(proof_state)

    assert status["preferredMainAiSurfaceId"] == "codex_sdk_python"
    assert status["preferredForOutputClasses"] == ["research_output"]
    assert status["outputClassRoutes"]["research_output"]["planningAllowed"] is True
    assert status["outputClassRoutes"]["report_draft"]["planningAllowed"] is False
    assert status["outputClassRoutes"]["report_draft"]["selectedSurfaceId"] is None


def test_output_class_proof_without_real_apply_is_not_promoted_for_workflow_planning():
    proof_state = _proof_state(review_applied=False, report_applied=False)
    status = promotion.describe_provider_routing_promotion_status(proof_state)

    assert status["preferredMainAiSurfaceId"] is None
    assert status["outputClassRoutes"]["research_output"]["planningAllowed"] is False
    assert status["outputClassRoutes"]["report_draft"]["planningAllowed"] is False
    assert "persisted_workflow_missing" in status["outputClassRoutes"]["research_output"]["reasonCodes"]
    assert "persisted_workflow_missing" in status["outputClassRoutes"]["report_draft"]["reasonCodes"]


def test_policy_status_has_only_safe_non_live_serialization(monkeypatch):
    def fail(*args, **kwargs):  # pragma: no cover - failure path proves no calls were made
        raise AssertionError("live command/import/network path should not be used")

    monkeypatch.setattr(subprocess, "run", fail)
    monkeypatch.setattr(subprocess, "Popen", fail)
    monkeypatch.setattr(importlib, "import_module", fail)
    monkeypatch.setattr(socket, "create_connection", fail)

    status = promotion.describe_provider_routing_promotion_status(_proof_state())
    serialized = json.dumps(status, sort_keys=True)

    assert "CODEX_HOME" not in serialized
    assert ".env" not in serialized
    assert ".mcp.json" not in serialized
    assert "auth.json" not in serialized
    assert "session.json" not in serialized
    assert "sk-" not in serialized
    assert "C:\\Users" not in serialized
    assert status["providerAuthIsAppOauthOidc"] is False
    assert status["localMcpIsPublicConnector"] is False
    assert status["productionReady"] is False
    assert status["externalBetaReady"] is False


def test_provider_proof_state_includes_fail_closed_routing_policy_when_no_artifacts(tmp_path):
    status = build_provider_proof_state_status(repo_root=tmp_path)
    routing = status["routingPromotionPolicy"]

    assert routing["policyMode"] == "planning_only"
    assert routing["preferredMainAiSurfaceId"] is None
    assert routing["liveExecutionAllowed"] is False
    assert routing["wouldExecuteProvider"] is False
    assert routing["outputClassRoutes"]["research_output"]["planningAllowed"] is False
    assert routing["outputClassRoutes"]["report_draft"]["planningAllowed"] is False
    assert "provider_weighted_routing_promotion_policy_nonlive" in status["reasonCodes"]


def test_runtime_provider_registry_stays_fail_closed_without_exact_runtime_enable(monkeypatch):
    monkeypatch.delenv("PROVIDER", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("XRML_ALLOW_PROVIDER_RESEARCH_OUTPUT", raising=False)
    monkeypatch.delenv("XRML_PROVIDER_LIVE_PROOF_ARTIFACT", raising=False)

    assert get_active_provider() is None

