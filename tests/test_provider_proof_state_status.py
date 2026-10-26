from __future__ import annotations

import importlib
import json
import socket
import subprocess
from pathlib import Path

import pytest


def _write_codex_tiny_artifact(root: Path) -> Path:
    from app.services.providers import codex_sdk_provider as provider

    artifact_ref = "output/provider-tiny-proof/test/codex_sdk_python_provider_tiny_proof.json"
    artifact = provider.build_codex_sdk_tiny_proof_artifact(
        sentinel_output=provider.SENTINEL_OBJECT,
        auth_strategy_used="codex_sdk_provider_managed_auth_external",
        provider_auth_observed={
            "CODEX_ACCESS_TOKEN": False,
            "OPENAI_API_KEY": False,
            "CODEX_AUTH_PROFILE": False,
        },
        artifact_ref=artifact_ref,
        timestamp="2026-07-06T05-38-32-723556-00-00",
    )
    path = root / artifact_ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifact), encoding="utf-8")
    return path


def _paper_review_evidence() -> dict[str, object]:
    return {
        "topic_id": "xr-experiences",
        "topic_name": "XR experiences",
        "paper_id": "2601.07571v1",
        "paper_title": "GPU accelerated surface-based gaze mapping for XR experiences",
        "evidence_source_ref": "data/papers/text/2601.07571v1.txt",
        "evidence_spans": [
            {
                "ref": "2601.07571v1:full_text_excerpt_1",
                "text": "GPU accelerated surface-based gaze mapping supports XR interaction review.",
            }
        ],
    }


def _paper_review_output() -> dict[str, object]:
    return {
        "output_class": "paper_review",
        "internal_output_class": "research_output",
        "provider_family_id": "codex",
        "surface_id": "codex_sdk_python",
        "topic_id": "xr-experiences",
        "topic_name": "XR experiences",
        "paper_id": "2601.07571v1",
        "paper_title": "GPU accelerated surface-based gaze mapping for XR experiences",
        "evidence_span_refs_used": ["2601.07571v1:full_text_excerpt_1"],
        "concise_review_summary": "The excerpt supports a focused paper review on gaze mapping for XR.",
        "methodology_or_core_approach": "The paper centers on GPU accelerated surface-based gaze mapping.",
        "key_findings": ["The topic and method are directly tied to XR interaction quality."],
        "limitations": ["Only one local excerpt is available in this proof fixture."],
        "relevance_to_topic": "Gaze mapping is a direct XR experience signal.",
        "follow_up_topic_suggestions": ["Review gaze mapping accuracy across XR hardware."],
        "confidence_or_caveats": "Medium confidence because the proof uses one excerpt.",
        "no_chain_of_thought": True,
    }


def _weekly_report_context() -> dict[str, object]:
    return {
        "topic_id": "xr-experiences",
        "topic_name": "XR experiences",
        "report_scope": "provider_review_context_only",
        "source_provider_review_ids": ["provider-review-ce78f0c10898f231"],
        "source_paper_ids": ["2601.07571v1"],
        "evidence_span_refs_used": ["2601.07571v1:full_text_excerpt_1"],
        "proof_artifact_refs_used": [
            "output/provider-output-class-proof/test/codex_sdk_paper_review_output_class_live_proof.json"
        ],
        "provider_reviews": [
            {
                "provider_review_id": "provider-review-ce78f0c10898f231",
                "paper_id": "2601.07571v1",
                "paper_title": "GPU accelerated surface-based gaze mapping for XR experiences",
                "concise_review_summary": "The provider review ties gaze mapping to XR interaction quality.",
                "key_findings": ["The method focuses on GPU accelerated surface-based gaze mapping."],
                "limitations": ["Only one proof fixture evidence span is available."],
                "relevance_to_topic": "Gaze mapping is a direct XR interaction signal.",
                "confidence_or_caveats": "Medium confidence from one DB-applied provider review.",
                "provider_family_id": "codex",
                "surface_id": "codex_sdk_python",
                "output_class": "paper_review",
                "internal_output_class": "research_output",
            }
        ],
    }


def _weekly_report_output() -> dict[str, object]:
    return {
        "output_class": "weekly_report",
        "internal_output_class": "report_draft",
        "provider_family_id": "codex",
        "surface_id": "codex_sdk_python",
        "topic_id": "xr-experiences",
        "topic_name": "XR experiences",
        "report_scope": "provider_review_context_only",
        "source_provider_review_ids": ["provider-review-ce78f0c10898f231"],
        "source_paper_ids": ["2601.07571v1"],
        "evidence_span_refs_used": ["2601.07571v1:full_text_excerpt_1"],
        "proof_artifact_refs_used": [
            "output/provider-output-class-proof/test/codex_sdk_paper_review_output_class_live_proof.json"
        ],
        "weekly_report_title": "XR Provider Review Digest",
        "executive_summary": "This draft summarizes one provider-backed review about gaze mapping for XR.",
        "reviewed_papers_summary": [
            {
                "paper_id": "2601.07571v1",
                "paper_title": "GPU accelerated surface-based gaze mapping for XR experiences",
                "main_takeaway": "Gaze mapping remains central to XR interaction quality.",
                "evidence_refs": ["2601.07571v1:full_text_excerpt_1"],
            }
        ],
        "cross_paper_synthesis": "The current proof scope contains one reviewed paper.",
        "methodology_or_scope_note": "The draft uses only supplied provider review context and does not store a report.",
        "limitations": ["The scope is intentionally limited to one provider-backed review."],
        "recommended_next_research_topics": ["Compare gaze mapping latency across XR displays."],
        "confidence_or_caveats": "Medium confidence from one provider review.",
        "no_chain_of_thought": True,
    }


def _write_codex_paper_review_output_class_artifact(root: Path) -> Path:
    from app.services.providers import codex_sdk_provider as provider

    artifact_ref = "output/provider-output-class-proof/test/codex_sdk_paper_review_output_class_live_proof.json"
    artifact = provider.build_codex_sdk_paper_review_output_class_proof_artifact(
        paper_review_output=_paper_review_output(),
        evidence=_paper_review_evidence(),
        auth_strategy_used="codex_sdk_provider_managed_auth_external",
        provider_auth_observed={
            "CODEX_ACCESS_TOKEN": False,
            "CODEX_API_KEY": False,
            "XRML_CODEX_SDK_PROVIDER_MANAGED_AUTH_APPROVED": False,
        },
        artifact_ref=artifact_ref,
        timestamp="2026-07-06T06-30-00-000000-00-00",
    )
    path = root / artifact_ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifact), encoding="utf-8")
    return path


def _write_codex_weekly_report_output_class_artifact(root: Path) -> Path:
    from app.services.providers import codex_sdk_provider as provider

    artifact_ref = "output/provider-output-class-proof/test/codex_sdk_weekly_report_output_class_live_proof.json"
    artifact = provider.build_codex_sdk_weekly_report_output_class_proof_artifact(
        weekly_report_output=_weekly_report_output(),
        report_context=_weekly_report_context(),
        auth_strategy_used="codex_sdk_provider_managed_auth_external",
        provider_auth_observed={
            "CODEX_ACCESS_TOKEN": False,
            "CODEX_API_KEY": False,
            "XRML_CODEX_SDK_PROVIDER_MANAGED_AUTH_APPROVED": False,
        },
        artifact_ref=artifact_ref,
        timestamp="2026-07-06T08-30-00-000000-00-00",
    )
    path = root / artifact_ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifact), encoding="utf-8")
    return path


def test_provider_proof_state_surfaces_codex_tiny_proof_without_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_codex_tiny_artifact(tmp_path)
    from app.services.provider_proof_state import build_provider_proof_state_status

    def fail_import(name: str, *args: object, **kwargs: object) -> object:
        raise AssertionError(f"unexpected import attempt: {name}")

    def fail_subprocess(*args: object, **kwargs: object) -> object:
        raise AssertionError("provider status must not run subprocess commands")

    def fail_network(*args: object, **kwargs: object) -> object:
        raise AssertionError("provider status must not open network connections")

    monkeypatch.setattr(importlib, "import_module", fail_import)
    monkeypatch.setattr(subprocess, "run", fail_subprocess)
    monkeypatch.setattr(subprocess, "Popen", fail_subprocess)
    monkeypatch.setattr(socket, "create_connection", fail_network)

    status = build_provider_proof_state_status(repo_root=tmp_path)
    tiny = status["providerTinyProofStatus"]
    gates = status["outputClassGateStatus"]

    assert tiny["tinyProofStatus"] == "passed"
    assert tiny["artifactKind"] == "provider_tiny_proof"
    assert tiny["tinyProofKind"] == "sdk_sentinel_prompt"
    assert tiny["proofStatusCandidate"] == "live_verified_tiny"
    assert tiny["providerSdkRunCount"] == 1
    assert tiny["providerCommandCount"] == 0
    assert tiny["providerCallCount"] == 1
    assert tiny["outputClassProof"] is False
    assert tiny["researchOutputAllowed"] is False
    assert tiny["reportDraftAllowed"] is False
    assert gates["paper_review"]["allowed"] is False
    assert gates["paper_review"]["requiredProof"] == "output_class_live"
    assert gates["paper_review"]["currentProof"] == "missing"
    assert gates["weekly_report"]["allowed"] is False
    assert gates["weekly_report"]["requiredProof"] == "output_class_live"
    assert gates["weekly_report"]["currentProof"] == "missing"
    assert status["appOauthOidcStatusSummary"]["providerAuthIsAppOauthOidc"] is False
    assert status["mcpConnectorStatusSummary"]["localMcpIsPublicConnector"] is False
    assert status["productionReadinessSummary"]["productionReady"] is False
    assert status["productionReadinessSummary"]["externalBetaReady"] is False
    assert any(surface["surfaceId"] == "codex_sdk_python" for surface in status["providerExecutionSurfaces"])

    serialized = json.dumps(status, sort_keys=True)
    assert '"providerOutput":' not in serialized
    assert "redactedProviderOutput" not in serialized
    assert "sk-secret-value" not in serialized
    assert "CODEX_HOME" not in serialized
    assert ".env" not in serialized
    assert ".mcp.json" not in serialized
    assert "auth.json" not in serialized
    assert "session.json" not in serialized
    assert "C:/Users" not in serialized
    assert ".codex" not in serialized


def test_provider_proof_state_surfaces_paper_review_output_class_proof_without_raw_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_codex_tiny_artifact(tmp_path)
    _write_codex_paper_review_output_class_artifact(tmp_path)
    from app.services.provider_proof_state import build_provider_proof_state_status

    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name, *args, **kwargs: pytest.fail(f"unexpected import attempt: {name}"),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("provider status must not run subprocess commands"),
    )
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: pytest.fail("provider status must not open network connections"),
    )

    status = build_provider_proof_state_status(repo_root=tmp_path)
    paper = status["providerOutputClassProofStatus"]["paper_review"]
    weekly = status["providerOutputClassProofStatus"]["weekly_report"]
    gates = status["outputClassGateStatus"]

    assert paper["artifactKind"] == "provider_output_class_proof"
    assert paper["proofMode"] == "output_class_live"
    assert paper["productOutputClass"] == "paper_review"
    assert paper["internalOutputClass"] == "research_output"
    assert paper["acceptedOutputClassProof"] is True
    assert paper["outputClassProof"] is True
    assert paper["researchOutputAllowed"] is True
    assert paper["reportDraftAllowed"] is False
    assert paper["providerSdkRunCount"] == 1
    assert paper["providerCommandCount"] == 0
    assert gates["paper_review"]["allowed"] is True
    assert gates["paper_review"]["currentProof"] == "present"
    assert gates["paper_review"]["currentProofLevel"] == "research_output_verified_tiny"
    assert gates["weekly_report"]["allowed"] is False
    assert gates["weekly_report"]["currentProof"] == "missing"
    assert weekly["acceptedOutputClassProof"] is False
    assert weekly["reportDraftAllowed"] is False

    serialized = json.dumps(status, sort_keys=True)
    assert "redactedProviderOutput" not in serialized
    assert '"providerOutput":' not in serialized
    assert "GPU accelerated surface-based gaze mapping supports" not in serialized
    assert "report_draft_verified_tiny" in serialized
    assert "CODEX_HOME" not in serialized
    assert ".env" not in serialized
    assert ".mcp.json" not in serialized
    assert "C:/Users" not in serialized


def test_provider_proof_state_surfaces_weekly_report_output_class_proof_without_cross_unlock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_codex_tiny_artifact(tmp_path)
    _write_codex_weekly_report_output_class_artifact(tmp_path)
    from app.services.provider_proof_state import build_provider_proof_state_status

    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name, *args, **kwargs: pytest.fail(f"unexpected import attempt: {name}"),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("provider status must not run subprocess commands"),
    )
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: pytest.fail("provider status must not open network connections"),
    )

    status = build_provider_proof_state_status(repo_root=tmp_path)
    paper = status["providerOutputClassProofStatus"]["paper_review"]
    weekly = status["providerOutputClassProofStatus"]["weekly_report"]
    gates = status["outputClassGateStatus"]
    report_integration = status["providerReportIntegrationStatus"]

    assert weekly["artifactKind"] == "provider_output_class_proof"
    assert weekly["proofMode"] == "output_class_live"
    assert weekly["productOutputClass"] == "weekly_report"
    assert weekly["internalOutputClass"] == "report_draft"
    assert weekly["acceptedOutputClassProof"] is True
    assert weekly["outputClassProof"] is True
    assert weekly["researchOutputAllowed"] is False
    assert weekly["reportDraftAllowed"] is True
    assert weekly["providerSdkRunCount"] == 1
    assert weekly["providerCommandCount"] == 0
    assert weekly["reportPersistenceInScope"] is False
    assert weekly["dbWriteInScope"] is False
    assert gates["weekly_report"]["allowed"] is True
    assert gates["weekly_report"]["currentProof"] == "present"
    assert gates["weekly_report"]["currentProofLevel"] == "report_draft_verified_tiny"
    assert gates["paper_review"]["allowed"] is False
    assert gates["paper_review"]["currentProof"] == "missing"
    assert paper["acceptedOutputClassProof"] is False
    assert paper["researchOutputAllowed"] is False
    assert report_integration["status"] == "dry_run_ready"
    assert report_integration["normalizedProviderReportDraftAvailable"] is True
    assert report_integration["realApplyStatus"] == "real_apply_blocked_pending_owner_approval"
    assert report_integration["reportDraftAllowed"] is True
    assert report_integration["dryRunApplyPlan"]["applied"] is False
    assert report_integration["previewContext"]["status"] == "provider_weekly_report_draft_proof_available"
    assert report_integration["providerAuthIsAppOauthOidc"] is False
    assert report_integration["localMcpIsPublicConnector"] is False
    assert report_integration["productionReady"] is False
    assert report_integration["externalBetaReady"] is False

    serialized = json.dumps(status, sort_keys=True)
    assert "redactedProviderOutput" not in serialized
    assert '"providerOutput":' not in serialized
    assert "This draft summarizes" not in serialized
    assert "CODEX_HOME" not in serialized
    assert ".env" not in serialized
    assert ".mcp.json" not in serialized
    assert "C:/Users" not in serialized


def test_provider_proof_state_missing_artifact_remains_fail_closed(tmp_path: Path) -> None:
    from app.services.provider_proof_state import build_provider_proof_state_status

    status = build_provider_proof_state_status(repo_root=tmp_path)
    tiny = status["providerTinyProofStatus"]

    assert tiny["tinyProofStatus"] == "missing"
    assert tiny["providerSdkRunCount"] == 0
    assert tiny["providerCommandCount"] == 0
    assert tiny["outputClassProof"] is False
    assert tiny["researchOutputAllowed"] is False
    assert tiny["reportDraftAllowed"] is False
    assert status["outputClassGateStatus"]["paper_review"]["allowed"] is False
    assert status["outputClassGateStatus"]["weekly_report"]["allowed"] is False
    assert status["providerReportIntegrationStatus"]["normalizedProviderReportDraftAvailable"] is False
    assert status["providerReportIntegrationStatus"]["reportDraftAllowed"] is False
    assert status["providerReportIntegrationStatus"]["realApplyStatus"] == "real_apply_blocked_pending_owner_approval"


def test_settings_status_includes_safe_provider_proof_state(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import dashboard
    from app.schemas.auth import AuthFoundationStatusResponse
    from app.schemas.dashboard import SettingsStatusResponse

    proof_state = {
        "providerTinyProofStatus": {
            "surfaceId": "codex_sdk_python",
            "tinyProofStatus": "passed",
            "providerSdkRunCount": 1,
            "providerCommandCount": 0,
            "outputClassProof": False,
            "researchOutputAllowed": False,
            "reportDraftAllowed": False,
        },
        "outputClassGateStatus": {
            "paper_review": {"allowed": False, "requiredProof": "output_class_live", "currentProof": "missing"},
            "weekly_report": {"allowed": False, "requiredProof": "output_class_live", "currentProof": "missing"},
        },
        "appOauthOidcStatusSummary": {"providerAuthIsAppOauthOidc": False},
        "mcpConnectorStatusSummary": {"localMcpIsPublicConnector": False},
        "productionReadinessSummary": {"productionReady": False, "externalBetaReady": False},
        "noSecretInspection": True,
        "noTokenCacheInspection": True,
        "noCredentialPathDisclosure": True,
    }

    monkeypatch.setattr(
        dashboard,
        "build_auth_me_response",
        lambda request: {
            "auth_mode": "disabled",
            "authenticated": True,
            "product_session_backend": "local_internal",
            "warnings": [],
            "mcp_url": None,
        },
    )
    monkeypatch.setattr(dashboard, "get_optional_product_user", lambda request: None)
    monkeypatch.setattr(
        dashboard.auth_foundation,
        "build_auth_foundation_status",
        lambda user=None: AuthFoundationStatusResponse(),
    )
    monkeypatch.setattr(
        dashboard,
        "describe_mcp_oauth_readiness",
        lambda: {
            "transport": "streamable-http",
            "protocol_version": "2025-03-26",
            "auth_required": False,
            "public_https": False,
            "cli_ready": False,
            "chatgpt_ready": False,
            "readiness_label": "local_cli_only",
            "authorization_servers": [],
            "public_tools": [],
            "protected_read_tools": [],
            "protected_write_tools": [],
            "warnings": [],
        },
    )
    monkeypatch.setattr(dashboard, "get_mcp_tool_access_summary", lambda: {})
    monkeypatch.setattr(
        dashboard,
        "describe_provider_status",
        lambda: {
            "provider": "openai",
            "provider_mode": "configured_no_project_live_proof",
            "configured": True,
            "live_call_enabled": False,
            "researchOutputAllowed": False,
            "reportGenerationAllowed": False,
            "warnings": [],
        },
    )
    monkeypatch.setattr(dashboard, "build_provider_proof_state_status", lambda: proof_state)
    monkeypatch.setattr(dashboard.topic_schedules, "get_scheduler_runtime_status", lambda: {})
    monkeypatch.setattr(dashboard.topic_notifications, "notification_delivery_health_summary", lambda: {})
    monkeypatch.setattr(dashboard, "build_deployment_status", lambda provider, scheduler, notification_delivery: {})
    monkeypatch.setattr(dashboard, "list_runs", lambda limit=200: [])
    monkeypatch.setattr(dashboard.metadata_index, "get_index_state", lambda: {})
    monkeypatch.setattr(dashboard, "iso_now_utc", lambda: "2026-07-06T00:00:00+00:00")

    result = dashboard.get_settings_status(object())  # type: ignore[arg-type]
    validated = SettingsStatusResponse.model_validate(result)

    assert validated.providerProofState["providerTinyProofStatus"]["tinyProofStatus"] == "passed"
    assert validated.providerProofState["providerTinyProofStatus"]["outputClassProof"] is False
    assert validated.providerProofState["outputClassGateStatus"]["paper_review"]["allowed"] is False
    assert validated.providerProofState["outputClassGateStatus"]["weekly_report"]["allowed"] is False


def test_provider_live_output_policy_frames_owner_only_optional_fresh_checkout(tmp_path: Path) -> None:
    from app.services.provider_proof_state import build_provider_proof_state_status

    # A fresh checkout carries no local proof artifacts (they are gitignored and never
    # committed), so accepted proofs are absent by design rather than by regression.
    status = build_provider_proof_state_status(repo_root=tmp_path)
    policy = status["providerLiveOutputPolicy"]

    assert policy["liveOutputClass"] == "owner_only_optional"
    assert policy["defaultRuntimeProvider"] == "deterministic_fallback"
    assert policy["proofArtifactStorage"] == "local_only_ignored"
    assert policy["proofArtifactsCommitted"] is False
    assert policy["acceptedProofPresentInThisCheckout"] is False
    assert policy["freshCheckoutAcceptedProofCount"] == 0
    assert policy["freshCheckoutZeroAcceptedIsExpected"] is True
    assert "provider_live_output_owner_only_optional" in policy["reasonCodes"]
    assert "fresh_checkout_zero_accepted_expected" in policy["reasonCodes"]
    # The fail-closed gate is unchanged: a fresh checkout still blocks live output.
    assert status["outputClassGateStatus"]["paper_review"]["allowed"] is False
    assert status["outputClassGateStatus"]["weekly_report"]["allowed"] is False
