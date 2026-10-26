from __future__ import annotations

import json
from typing import Any

from app.db import bootstrap
from app.repositories import ai_review_drafts, paper_reviews
from app.services import provider_review_coverage, research_operations_rc_audit


def _review(index: int, *, full_text_status: str = "reviewed", **overrides: Any) -> dict[str, Any]:
    payload = {
        "topic_id": "large-language-models",
        "run_id": "rc-audit-run",
        "paper_id": f"rc-paper-{index}",
        "title": f"RC Audit Paper {index}",
        "source_provider": "arxiv",
        "abstract_text": "This paper studies grounded LLM review coverage and provenance.",
        "text_excerpt": "Full text evidence for release candidate audit consistency. " * 8,
        "full_text_status": full_text_status,
        "relevance_score": 92,
        "novelty_score": 61,
        "evidence_strength": 82,
        "method_summary": "Grounded local review.",
        "key_findings": ["Evidence provenance must remain consistent."],
        "limitations": ["Synthetic provider evidence is not real external AI evidence."],
        "why_it_matters": "It supports release candidate audit checks.",
        "topic_fit_rationale": "Matches LLM operations and evidence audit.",
        "review_model": "deterministic-local-full-text-v1",
        "reviewer": "deterministic-local-full-text",
        "reviewed_at": "2026-05-14T00:00:00+00:00",
        "source_text_sha256": f"rc-sha-{index}",
        "full_text_artifact_path": f"storage/runs/rc-audit-run/raw/rc-paper-{index}.txt",
        "text_char_count": 640,
        "evidence_spans": [
            {
                "label": f"rc-span-{index}",
                "text": f"RC audit span {index} supports grounded provider provenance.",
                "source_path": f"storage/runs/rc-audit-run/raw/rc-paper-{index}.txt",
                "page_number": 1,
                "char_start": 0,
                "char_end": 68,
            }
        ],
        "review_source_type": "full_text",
        "full_text_reviewed_at": "2026-05-14T00:00:00+00:00",
    }
    payload.update(overrides)
    return payload


def _provider_draft(
    index: int,
    *,
    status: str = "accepted",
    provider: dict[str, Any] | None = None,
    include_m4_validation: bool = True,
) -> dict[str, Any]:
    validation: dict[str, Any] = {
        "provider": provider or {"name": "stub", "mode": "stub", "origin": "stub", "generated": True},
    }
    if include_m4_validation:
        validation.update({"status": status, "reason_codes": [] if status == "accepted" else ["fixture_validation_blocked"]})
    return {
        "topic_id": "large-language-models",
        "run_id": "rc-audit-run",
        "paper_id": f"rc-paper-{index}",
        "source_text_sha256": f"rc-sha-{index}",
        "review_level": "full_text_grounded",
        "status": status,
        "draft": {
            "paper_id": f"rc-paper-{index}",
            "source_text_sha256": f"rc-sha-{index}",
            "review_level": "full_text_grounded",
            "contribution_summary": f"RC provider draft {index}.",
            "method_summary": "Grounded local method.",
            "evidence_summary": f"Uses rc-span-{index}.",
            "limitations": ["Synthetic evidence remains local fixture evidence."],
            "relevance_to_topic": "Relevant to release candidate audit.",
            "novelty_signal": "moderate",
            "practical_implications": "Verify provenance before report use.",
            "follow_up_questions": [],
            "recommended_tags": ["audit"],
            "cited_evidence_span_ids": [f"rc-span-{index}"],
            "confidence": 0.78,
            "unsupported_claims": [],
        },
        "validation": validation,
    }


def _dashboard_operations(coverage: dict[str, Any], *, ai_count: int = 2, auth_overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    auth = {
        "tokenStorageEnabled": False,
        "providerLiveExecution": "cli_only_acceptance_gated",
        "oauthLiveExchangeReadiness": {"liveExchangeEnabled": False, "tokenStorageEnabled": False},
        "oauthLiveExchangePilot": {"liveExchangeEnabled": False, "dashboardExecutionEnabled": False, "tokenStorageEnabled": False},
        "providerOrchestrationSafetyMatrix": {
            "dashboardAllowedActionCount": 0,
            "actions": [{"actionId": "dashboard_provider_execution_forbidden", "dashboardAllowed": False}],
        },
    }
    if auth_overrides:
        auth.update(auth_overrides)
    return {
        "coverage": {
            "fullTextReviewedUniqueCount": coverage["fullTextReviewedDenominator"],
            "topicRelevantUniqueCount": coverage["fullTextReviewedDenominator"],
            "aiGroundedAcceptedCount": ai_count,
            "providerReviewCoverage": coverage,
        },
        "auth": auth,
    }


def test_rc_audit_validates_cross_surface_release_candidate_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    for index in range(1, 3):
        paper_reviews.upsert_review(_review(index))
    ai_review_drafts.upsert_draft(_provider_draft(1))
    ai_review_drafts.upsert_draft(
        _provider_draft(
            2,
            provider={"name": "command", "mode": "fixture_command", "origin": "command_fixture", "generated": True},
        )
    )

    coverage = provider_review_coverage.build_provider_review_coverage_summary(
        "large-language-models",
        full_text_reviewed_denominator=2,
    )
    recommendation_payload = {
        "summary": {"candidate_gap_count": 1, "high_confidence_count": 0},
        "gap_candidates": [
            {
                "confidence": "medium",
                "supporting_evidence_refs": [
                    {
                        "paper_id": "rc-paper-1",
                        "source_text_sha256": "rc-sha-1",
                        "review_level": "accepted_ai_grounded_synthetic",
                        "provider_origin": "stub_synthetic",
                    }
                ],
            }
        ],
        "new_topic_candidates": [],
    }
    report_markdown = (
        "Provider-orchestrated accepted draft coverage is 2 / 2 full-text reviewed papers.\n"
        "Synthetic provider accepted drafts: 2.\n"
        "Operator-accepted real provider accepted drafts: 0.\n"
        "Synthetic/local fixture provider drafts remain test artifacts.\n"
    )

    audit = research_operations_rc_audit.build_research_operations_rc_audit(
        "large-language-models",
        dashboard_operations=_dashboard_operations(coverage),
        recommendation_payload=recommendation_payload,
        report_markdown=report_markdown,
        git_status_lines=[],
    )

    assert audit["auditStatus"] == "valid"
    assert audit["failedCheckCount"] == 0
    assert audit["checkCount"] >= 10
    assert "rc_audit_valid" in audit["reasonCodes"]
    assert audit["artifactBoundary"]["boundaryStatus"] == "valid"
    assert "literal-secret-value" not in json.dumps(audit).lower()


def test_rc_audit_detects_cross_surface_failures_and_runtime_boundary(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1))
    paper_reviews.upsert_review(_review(2))
    ai_review_drafts.upsert_draft(_provider_draft(1, include_m4_validation=False))
    ai_review_drafts.upsert_draft(_provider_draft(2, status="needs_review"))

    coverage = provider_review_coverage.build_provider_review_coverage_summary(
        "large-language-models",
        full_text_reviewed_denominator=2,
    )
    dashboard_coverage = {
        **coverage,
        "realProviderAcceptedCount": 4,
        "providerOrchestratedAcceptedCount": 9,
    }
    recommendation_payload = {
        "summary": {"candidate_gap_count": 1, "high_confidence_count": 1},
        "gap_candidates": [
            {
                "confidence": "high",
                "supporting_evidence_refs": [
                    {
                        "paper_id": "rc-paper-2",
                        "source_text_sha256": "rc-sha-2",
                        "review_level": "accepted_ai_grounded_synthetic",
                        "provider_origin": "stub_synthetic",
                    }
                ],
            }
        ],
        "new_topic_candidates": [],
    }
    report_markdown = (
        "Provider-orchestrated accepted draft coverage is 9 / 2 full-text reviewed papers.\n"
        "Synthetic provider accepted drafts: 0.\n"
        "Operator-accepted real provider accepted drafts: 4.\n"
    )

    audit = research_operations_rc_audit.build_research_operations_rc_audit(
        "large-language-models",
        dashboard_operations=_dashboard_operations(
            dashboard_coverage,
            ai_count=9,
            auth_overrides={"tokenStorageEnabled": True},
        ),
        recommendation_payload=recommendation_payload,
        report_markdown=report_markdown,
        git_status_lines=[
            '?? "storage/topic_profiles/profiles (1).json"',
            "A  output/reports/research-operations-rc-audit-m16.md",
        ],
    )

    assert audit["auditStatus"] == "failed"
    assert audit["failedCheckCount"] >= 1
    assert "dashboard_ai_grounded_coverage_mismatch" in audit["reasonCodes"]
    assert "provider_origin_consistency_failed" in audit["reasonCodes"]
    assert "rejected_needs_review_leak_detected" in audit["reasonCodes"]
    assert "m4_validation_gate_failed" in audit["reasonCodes"]
    assert "oauth_secret_safety_failed" in audit["reasonCodes"]
    assert "runtime_artifact_tracked" in audit["reasonCodes"]
    assert audit["artifactBoundary"]["knownRuntimeUntrackedCount"] == 1
    assert audit["artifactBoundary"]["unexpectedRuntimeTrackedCount"] == 1


def test_runtime_artifact_boundary_classifies_known_profile_copy_as_accepted_warning() -> None:
    boundary = research_operations_rc_audit.build_runtime_artifact_boundary_summary(
        ['?? "storage/topic_profiles/profiles (1).json"']
    )

    assert boundary["boundaryStatus"] == "warnings"
    assert boundary["knownRuntimeUntrackedCount"] == 1
    assert boundary["acceptedRuntimeWarningCount"] == 1
    assert boundary["blockingReleaseWarningCount"] == 0
    assert boundary["warningClassifications"] == [
        {
            "path": "storage/topic_profiles/profiles (1).json",
            "classification": "accepted_runtime_warning",
            "artifactPolicy": "ignored_runtime_artifact",
            "severity": "warning",
            "shouldRemainUntracked": True,
            "reasonCodes": ["known_runtime_artifact_untracked"],
        }
    ]


def test_runtime_artifact_boundary_blocks_tracked_runtime_artifact() -> None:
    boundary = research_operations_rc_audit.build_runtime_artifact_boundary_summary(
        ["A  output/reports/research-operations-rc-audit-m16.md"]
    )

    assert boundary["boundaryStatus"] == "failed"
    assert boundary["blockingReleaseWarningCount"] == 1
    assert boundary["warningClassifications"][0]["classification"] == "tracking_policy_violation"
    assert boundary["warningClassifications"][0]["severity"] == "blocking"
    assert boundary["warningClassifications"][0]["shouldRemainUntracked"] is True


def test_release_freeze_summary_ready_with_accepted_runtime_warning() -> None:
    rc_audit = {
        "auditStatus": "warnings",
        "checkCount": 12,
        "failedCheckCount": 0,
        "warningCheckCount": 1,
        "artifactBoundary": research_operations_rc_audit.build_runtime_artifact_boundary_summary(
            ['?? "storage/topic_profiles/profiles (1).json"']
        ),
        "reasonCodes": ["rc_audit_warnings", "known_runtime_artifact_untracked"],
    }

    summary = research_operations_rc_audit.build_research_operations_release_freeze_summary(rc_audit)

    assert summary["releaseStatus"] == "ready_with_warnings"
    assert summary["knownWarningCount"] == 1
    assert summary["blockingIssueCount"] == 0
    assert summary["artifactBoundarySummary"]["acceptedRuntimeWarningCount"] == 1
    assert summary["knownWarnings"][0]["classification"] == "accepted_runtime_warning"
    assert summary["oauthProviderSafetySummary"]["dashboardExecutionEnabled"] is False
    assert summary["validationMatrixSummary"]["fullPythonSuite"] == "527 passed"


def test_release_freeze_summary_blocks_serious_rc_failure() -> None:
    rc_audit = {
        "auditStatus": "failed",
        "checkCount": 12,
        "failedCheckCount": 2,
        "warningCheckCount": 1,
        "artifactBoundary": research_operations_rc_audit.build_runtime_artifact_boundary_summary(
            ["A  output/reports/research-operations-rc-audit-m16.md"]
        ),
        "reasonCodes": ["rc_audit_failed", "runtime_artifact_tracked"],
    }

    summary = research_operations_rc_audit.build_research_operations_release_freeze_summary(rc_audit)

    assert summary["releaseStatus"] == "blocked"
    assert summary["blockingIssueCount"] >= 1
    assert "release_blocked_by_rc_audit" in summary["reasonCodes"]
    assert "tracking_policy_violation" in {item["classification"] for item in summary["knownWarnings"]}


def test_dashboard_api_exposes_rc_audit_summary_read_only(client) -> None:
    response = client.get("/api/topics/large-language-models/dashboard")

    assert response.status_code == 200, response.text
    rc_audit = response.json()["researchOperations"]["rcAudit"]
    assert rc_audit["auditStatus"] in {"valid", "warnings"}, json.dumps(rc_audit, indent=2)
    assert rc_audit["artifactBoundary"]["boundaryStatus"] in {"valid", "warnings"}
    assert any(check["category"] == "oauth_secret_safety" for check in rc_audit["checks"])
    assert "execute" not in json.dumps(rc_audit).lower()


def test_dashboard_api_exposes_release_freeze_summary_read_only(client) -> None:
    response = client.get("/api/topics/large-language-models/dashboard")

    assert response.status_code == 200, response.text
    release_freeze = response.json()["researchOperations"]["releaseFreeze"]
    assert release_freeze["releaseStatus"] in {"ready", "ready_with_warnings"}
    assert release_freeze["rcAuditStatus"] in {"valid", "warnings"}
    assert release_freeze["blockingIssueCount"] == 0
    assert release_freeze["knownWarningCount"] >= 0
    assert release_freeze["artifactBoundarySummary"]["blockingReleaseWarningCount"] == 0
    assert "execute" not in json.dumps(release_freeze).lower()
