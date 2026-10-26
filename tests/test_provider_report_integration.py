from __future__ import annotations

import importlib
import json
import socket
import subprocess
from pathlib import Path

import pytest


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


def _write_weekly_artifact(root: Path, mutator=None) -> Path:
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
        timestamp="2026-07-06T08-48-15-043435-00-00",
    )
    if mutator is not None:
        mutator(artifact)
    path = root / artifact_ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifact), encoding="utf-8")
    return path


def _write_tiny_artifact(root: Path) -> Path:
    from app.services.providers import codex_sdk_provider as provider

    artifact_ref = "output/provider-tiny-proof/test/codex_sdk_python_provider_tiny_proof.json"
    artifact = provider.build_codex_sdk_tiny_proof_artifact(
        sentinel_output=provider.SENTINEL_OBJECT,
        auth_strategy_used="codex_sdk_provider_managed_auth_external",
        provider_auth_observed={
            "CODEX_ACCESS_TOKEN": False,
            "CODEX_API_KEY": False,
            "XRML_CODEX_SDK_PROVIDER_MANAGED_AUTH_APPROVED": False,
        },
        artifact_ref=artifact_ref,
        timestamp="2026-07-06T05-38-32-723556-00-00",
    )
    path = root / artifact_ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifact), encoding="utf-8")
    return path


def _write_paper_review_artifact(root: Path) -> Path:
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
        timestamp="2026-07-06T07-03-11-903914-00-00",
    )
    path = root / artifact_ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifact), encoding="utf-8")
    return path


def _configure_temp_report_apply_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from app.db import bootstrap
    from app.domain import constants
    from app.repositories import artifact_refs, runs as run_repository

    storage_root = tmp_path / "storage" / "runs"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(constants, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(run_repository, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(artifact_refs.constants, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(artifact_refs.constants, "STORAGE_ROOT", storage_root)
    with bootstrap.connect_metadata_index():
        pass
    return storage_root


def _seed_source_provider_review() -> None:
    from app.repositories import paper_reviews

    paper_reviews.upsert_review(
        {
            "topic_id": "xr-experiences",
            "run_id": "provider-review-ce78f0c10898f231",
            "paper_id": "2601.07571v1",
            "title": "GPU accelerated surface-based gaze mapping for XR experiences",
            "source_provider": "codex",
            "abstract_text": "Provider-backed source review for weekly report proof.",
            "text_excerpt": "GPU accelerated surface-based gaze mapping supports XR interaction review.",
            "full_text_status": "reviewed",
            "relevance_score": 92.0,
            "novelty_score": 72.0,
            "evidence_strength": 88.0,
            "method_summary": "Provider-reviewed local full-text evidence.",
            "key_findings": ["Gaze mapping is directly relevant to XR interaction quality."],
            "limitations": ["One local evidence span was used."],
            "why_it_matters": "The provider review supplies report-ready evidence.",
            "topic_fit_rationale": "XR experiences depend on gaze-aware interaction quality.",
            "review_model": "codex_sdk_python/output_class_live",
            "reviewer": "provider:codex_sdk_python",
            "reviewed_at": "2026-07-06T07:03:11+00:00",
            "source_text_sha256": "source-review-sha",
            "full_text_artifact_path": "project:data/papers/text/2601.07571v1.txt",
            "text_char_count": 256,
            "evidence_spans": [
                {
                    "span_id": "2601.07571v1:full_text_excerpt_1",
                    "ref": "2601.07571v1:full_text_excerpt_1",
                    "text": "GPU accelerated surface-based gaze mapping supports XR interaction review.",
                    "proof_artifact_ref": "output/provider-output-class-proof/test/codex_sdk_paper_review_output_class_live_proof.json",
                }
            ],
            "review_source_type": "provider_output_class_proof",
            "full_text_reviewed_at": "2026-07-06T07:03:11+00:00",
        }
    )


def test_accepted_codex_weekly_report_proof_loads_and_normalizes_without_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_weekly_artifact(tmp_path)
    from app.services import provider_report_integration as integration

    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name, *args, **kwargs: pytest.fail(f"unexpected SDK import: {name}"),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("provider command must not run"),
    )
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: pytest.fail("network must not open"),
    )

    record = integration.load_provider_weekly_report_output_class_proof(path)
    normalized = integration.normalize_provider_weekly_report_for_storage(record)
    serialized = json.dumps(normalized.to_dict(), sort_keys=True)

    assert record.provider_family_id == "codex"
    assert record.provider_surface_id == "codex_sdk_python"
    assert record.output_class == "weekly_report"
    assert record.internal_output_class == "report_draft"
    assert record.report_draft_allowed is True
    assert record.research_output_allowed is False
    assert normalized.provider_report_draft_id.startswith("provider-report-draft-")
    assert normalized.report_source == "provider_output_class_proof"
    assert normalized.apply_status == "dry_run_only"
    assert normalized.report_persistence_in_scope is False
    assert normalized.db_write_in_scope is False
    assert normalized.source_provider_review_ids == ("provider-review-ce78f0c10898f231",)
    assert normalized.source_paper_ids == ("2601.07571v1",)
    assert normalized.evidence_span_refs_used == ("2601.07571v1:full_text_excerpt_1",)
    assert "CODEX_HOME" not in serialized
    assert ".env" not in serialized
    assert ".mcp.json" not in serialized
    assert "auth.json" not in serialized
    assert "session.json" not in serialized
    assert "C:/Users" not in serialized


def test_tiny_and_paper_review_artifacts_are_rejected_for_report_integration(tmp_path: Path) -> None:
    tiny_path = _write_tiny_artifact(tmp_path)
    paper_path = _write_paper_review_artifact(tmp_path)
    from app.services.provider_report_integration import (
        ProviderReportIntegrationError,
        load_provider_weekly_report_output_class_proof,
    )

    with pytest.raises(ProviderReportIntegrationError) as tiny_exc:
        load_provider_weekly_report_output_class_proof(tiny_path)
    with pytest.raises(ProviderReportIntegrationError) as paper_exc:
        load_provider_weekly_report_output_class_proof(paper_path)

    assert "provider_tiny_proof_rejected_for_report_integration" in tiny_exc.value.reason_codes
    assert "field_mismatch:productOutputClass" in paper_exc.value.reason_codes
    assert "field_mismatch:internalOutputClass" in paper_exc.value.reason_codes


def test_failed_or_incomplete_weekly_artifacts_are_rejected(tmp_path: Path) -> None:
    from app.services.provider_report_integration import (
        ProviderReportIntegrationError,
        load_provider_weekly_report_output_class_proof,
    )

    failed = _write_weekly_artifact(
        tmp_path / "failed",
        lambda artifact: artifact.update({"acceptedOutputClassProof": False, "reportDraftAllowed": False}),
    )
    missing_review = _write_weekly_artifact(
        tmp_path / "missing-review",
        lambda artifact: artifact.update({"sourceProviderReviewIds": [], "sourceProviderReviews": []}),
    )
    missing_evidence = _write_weekly_artifact(
        tmp_path / "missing-evidence",
        lambda artifact: artifact.update({"evidenceSpanCount": 0, "evidenceSpanRefsUsed": []}),
    )

    with pytest.raises(ProviderReportIntegrationError) as failed_exc:
        load_provider_weekly_report_output_class_proof(failed)
    with pytest.raises(ProviderReportIntegrationError) as review_exc:
        load_provider_weekly_report_output_class_proof(missing_review)
    with pytest.raises(ProviderReportIntegrationError) as evidence_exc:
        load_provider_weekly_report_output_class_proof(missing_evidence)

    assert "field_mismatch:acceptedOutputClassProof" in failed_exc.value.reason_codes
    assert "missing_source_provider_review_ids" in review_exc.value.reason_codes
    assert "missing_evidence_refs" in evidence_exc.value.reason_codes


def test_secret_chain_of_thought_and_positive_readiness_claims_are_rejected(tmp_path: Path) -> None:
    from app.services.provider_report_integration import (
        ProviderReportIntegrationError,
        load_provider_weekly_report_output_class_proof,
    )

    secret = _write_weekly_artifact(
        tmp_path / "secret",
        lambda artifact: artifact["redactedProviderOutput"].update(
            {"executive_summary": "Read local auth from C:/Users/EXAMPLE/.codex/auth.json"}
        ),
    )
    chain = _write_weekly_artifact(
        tmp_path / "chain",
        lambda artifact: artifact["redactedProviderOutput"].update(
            {"confidence_or_caveats": "hidden reasoning shows why the report is good"}
        ),
    )
    claim = _write_weekly_artifact(
        tmp_path / "claim",
        lambda artifact: artifact["redactedProviderOutput"].update(
            {"methodology_or_scope_note": "The report was persisted report and production ready."}
        ),
    )

    with pytest.raises(ProviderReportIntegrationError) as secret_exc:
        load_provider_weekly_report_output_class_proof(secret)
    with pytest.raises(ProviderReportIntegrationError) as chain_exc:
        load_provider_weekly_report_output_class_proof(chain)
    with pytest.raises(ProviderReportIntegrationError) as claim_exc:
        load_provider_weekly_report_output_class_proof(claim)

    assert "secret_like_content_detected" in secret_exc.value.reason_codes
    assert "chain_of_thought_like_content_detected" in chain_exc.value.reason_codes
    assert "positive_report_persistence_or_readiness_claim" in claim_exc.value.reason_codes


def test_dry_run_report_apply_plan_does_not_mutate_real_db(tmp_path: Path) -> None:
    path = _write_weekly_artifact(tmp_path)
    from app.services import provider_report_integration as integration

    record = integration.load_provider_weekly_report_output_class_proof(path)
    normalized = integration.normalize_provider_weekly_report_for_storage(record)
    plan = integration.plan_provider_report_db_apply(normalized)
    preview = integration.build_provider_report_preview_context(normalized)

    assert plan["status"] == "dry_run_ready"
    assert plan["applied"] is False
    assert plan["realApplyStatus"] == "real_apply_blocked_pending_owner_approval"
    assert plan["wouldCreateReportDraftRow"] is True
    assert plan["wouldAttachSourceProviderReviewIds"] is True
    assert plan["wouldLinkWeeklyProofArtifactRef"] is True
    assert plan["wouldPersistReportArtifact"] is False
    assert plan["wouldWriteRealDb"] is False
    assert preview["status"] == "provider_weekly_report_draft_proof_available"
    assert preview["providerReportDraftCount"] == 1
    assert preview["sourceProviderReviewCount"] == 1
    assert preview["reportDraftAllowed"] is True
    assert preview["reportPersistenceStatus"] == "real_apply_blocked_pending_owner_approval"
    assert not (tmp_path / "storage" / "metadata_index.sqlite3").exists()


def test_temp_storage_apply_writes_only_temp_path_and_preserves_provider_provenance(tmp_path: Path) -> None:
    path = _write_weekly_artifact(tmp_path)
    from app.services import provider_report_integration as integration

    record = integration.load_provider_weekly_report_output_class_proof(path)
    normalized = integration.normalize_provider_weekly_report_for_storage(record)
    target = tmp_path / "synthetic-report-storage"
    result = integration.apply_provider_report_to_temp_storage(normalized, target_dir=target)

    assert result["status"] == "temp_apply_succeeded"
    assert result["tempStorageOnly"] is True
    assert result["realApplyStatus"] == "real_apply_blocked_pending_owner_approval"
    assert result["readbackMatches"] is True
    draft_payload = json.loads((target / "provider-report-draft.json").read_text(encoding="utf-8"))
    assert draft_payload["provider_family_id"] == "codex"
    assert draft_payload["provider_surface_id"] == "codex_sdk_python"
    assert draft_payload["source_provider_review_ids"] == ["provider-review-ce78f0c10898f231"]
    assert draft_payload["evidence_span_refs_used"] == ["2601.07571v1:full_text_excerpt_1"]
    assert draft_payload["report_persistence_in_scope"] is False
    assert (target / "provider-report-preview.md").is_file()


def test_real_report_apply_creates_backup_artifact_and_metadata_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_weekly_artifact(tmp_path)
    storage_root = _configure_temp_report_apply_storage(tmp_path, monkeypatch)
    _seed_source_provider_review()
    from app.db import bootstrap
    from app.services import provider_report_integration as integration

    record = integration.load_provider_weekly_report_output_class_proof(path)
    normalized = integration.normalize_provider_weekly_report_for_storage(record)
    first = integration.apply_provider_report_to_real_storage(
        normalized,
        backup_timestamp="2026-07-06T09-30-00+00:00",
    )
    second = integration.apply_provider_report_to_real_storage(normalized)

    report_path = storage_root / normalized.provider_report_draft_id / "reports" / "provider-weekly-report-draft.md"
    run_json_path = storage_root / normalized.provider_report_draft_id / "run.json"
    assert first["status"] == "real_apply_applied"
    assert first["applied"] is True
    assert first["providerReportDraftId"] == normalized.provider_report_draft_id
    assert first["backup"]["metadataBackupPath"].endswith("metadata_index-pre-provider-report-apply.sqlite3")
    assert first["backup"]["targetManifestPath"].endswith("report-target-pre-apply-manifest.json")
    assert Path(tmp_path / first["backup"]["metadataBackupPath"]).is_file()
    assert Path(tmp_path / first["backup"]["targetManifestPath"]).is_file()
    assert report_path.is_file()
    assert run_json_path.is_file()
    report_markdown = report_path.read_text(encoding="utf-8")
    assert "reviewed count: 1 DB-applied provider paper review" in report_markdown
    assert "full-text reviewed coverage: 1/1 within this provider proof context" in report_markdown
    assert "not a complete literature survey" in report_markdown
    assert first["readback"]["exactReportMatches"] is True
    assert first["readback"]["readback"]["reportArtifactSha256"]
    assert second["status"] == "already_applied"
    assert second["applied"] is False
    assert second["backup"] is None
    backups = list((tmp_path / "storage" / "backups").glob("provider-report-real-apply-*"))
    assert len(backups) == 1
    with bootstrap.connect_metadata_index(read_only=True) as connection:
        run_row = connection.execute(
            "SELECT run_id, status, report_path FROM runs WHERE run_id = ?",
            (normalized.provider_report_draft_id,),
        ).fetchone()
        sections = connection.execute(
            "SELECT section_id FROM report_sections WHERE run_id = ?",
            (normalized.provider_report_draft_id,),
        ).fetchall()
    assert run_row["status"] == "completed"
    assert str(run_row["report_path"]).replace("\\", "/").endswith(
        f"{normalized.provider_report_draft_id}/reports/provider-weekly-report-draft.md"
    )
    assert len(sections) >= 6


def test_quality_disclosure_patch_updates_legacy_persisted_report_without_provider_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_weekly_artifact(tmp_path)
    storage_root = _configure_temp_report_apply_storage(tmp_path, monkeypatch)
    _seed_source_provider_review()
    from app.services import provider_report_integration as integration

    record = integration.load_provider_weekly_report_output_class_proof(path)
    normalized = integration.normalize_provider_weekly_report_for_storage(record)
    integration.apply_provider_report_to_real_storage(
        normalized,
        backup_timestamp="2026-07-06T09-32-00+00:00",
    )
    report_path = storage_root / normalized.provider_report_draft_id / "reports" / "provider-weekly-report-draft.md"
    legacy_markdown = (
        integration.build_provider_report_preview_markdown(normalized, persisted=True)
        .replace("- reviewed count: 1 DB-applied provider paper review\n", "")
        .replace("- full-text reviewed coverage: 1/1 within this provider proof context; all collected papers were not read in full.\n", "")
        .replace("- Coverage limitation: this is not a complete literature survey and does not claim full weekly coverage of XR/AR literature.\n", "")
    )
    report_path.write_text(legacy_markdown + "\n", encoding="utf-8")

    result = integration.apply_provider_report_quality_disclosure_patch(
        normalized,
        backup_timestamp="2026-07-06T09-33-00+00:00",
    )
    second = integration.apply_provider_report_quality_disclosure_patch(normalized)

    assert result["status"] == "quality_disclosure_patched"
    assert result["patched"] is True
    assert "missing_coverage_disclosure" in result["beforeQuality"]["failureReasons"]
    assert "missing_coverage_disclosure" not in result["afterQuality"]["failureReasons"]
    assert result["afterQuality"]["criticalFailFlags"] == []
    assert result["readback"]["exactReportMatches"] is True
    assert result["beforeReportSha256"] != result["afterReportSha256"]
    assert Path(result["backup"]["reportArtifactBackupPath"]).is_file()
    assert Path(result["backup"]["runJsonBackupPath"]).is_file()
    assert Path(result["backup"]["targetManifestPath"]).is_file()
    assert second["status"] == "already_disclosed"
    assert second["patched"] is False
    assert second["backup"] is None


def test_real_report_apply_requires_source_provider_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_weekly_artifact(tmp_path)
    _configure_temp_report_apply_storage(tmp_path, monkeypatch)
    from app.services import provider_report_integration as integration

    record = integration.load_provider_weekly_report_output_class_proof(path)
    normalized = integration.normalize_provider_weekly_report_for_storage(record)
    with pytest.raises(integration.ProviderReportIntegrationError) as exc:
        integration.apply_provider_report_to_real_storage(normalized)

    assert "PROVIDER_REVIEW_SOURCE_MISSING" in exc.value.reason_codes
    assert not (tmp_path / "storage" / "backups").exists()


def test_real_report_apply_rejects_conflicting_report_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_weekly_artifact(tmp_path)
    storage_root = _configure_temp_report_apply_storage(tmp_path, monkeypatch)
    _seed_source_provider_review()
    from app.services import provider_report_integration as integration

    record = integration.load_provider_weekly_report_output_class_proof(path)
    normalized = integration.normalize_provider_weekly_report_for_storage(record)
    conflict_path = storage_root / normalized.provider_report_draft_id / "reports" / "provider-weekly-report-draft.md"
    conflict_path.parent.mkdir(parents=True, exist_ok=True)
    conflict_path.write_text("conflicting provider report", encoding="utf-8")
    with pytest.raises(integration.ProviderReportIntegrationError) as exc:
        integration.apply_provider_report_to_real_storage(normalized)

    assert "PROVIDER_REPORT_DRAFT_CONFLICT" in exc.value.reason_codes
    assert not (tmp_path / "storage" / "backups").exists()


def test_provider_report_integration_status_reports_real_apply_when_readback_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_weekly_artifact(tmp_path)
    _configure_temp_report_apply_storage(tmp_path, monkeypatch)
    _seed_source_provider_review()
    from app.services import provider_report_integration as integration
    from app.services.provider_proof_state import build_provider_proof_state_status

    record = integration.load_provider_weekly_report_output_class_proof(path)
    normalized = integration.normalize_provider_weekly_report_for_storage(record)
    integration.apply_provider_report_to_real_storage(
        normalized,
        backup_timestamp="2026-07-06T09-31-00+00:00",
    )
    status = integration.build_provider_report_integration_status(path)
    proof_state = build_provider_proof_state_status(repo_root=tmp_path)

    assert status["status"] == "real_apply_applied"
    assert status["realApplyStatus"] == "real_apply_already_applied"
    assert status["realDbStatus"]["exactReportMatches"] is True
    assert status["previewContext"]["providerReportDraftCount"] == 1
    assert status["qualityContext"]["providerCallPerformed"] is False
    assert proof_state["providerReportIntegrationStatus"]["status"] == "real_apply_applied"
    assert proof_state["providerReportIntegrationStatus"]["realApplyStatus"] == "real_apply_already_applied"


def test_report_quality_context_is_deterministic_and_non_overclaiming(tmp_path: Path) -> None:
    path = _write_weekly_artifact(tmp_path)
    from app.services import provider_report_integration as integration

    record = integration.load_provider_weekly_report_output_class_proof(path)
    normalized = integration.normalize_provider_weekly_report_for_storage(record)
    quality = integration.build_provider_report_quality_context(normalized)
    serialized = json.dumps(quality, sort_keys=True)

    assert quality["status"] == "deterministic_report_quality_evaluated"
    assert quality["evaluationMethod"] == "deterministic_rubric"
    assert quality["deterministicLocalRubricOnly"] is True
    assert quality["providerCallPerformed"] is False
    assert quality["realReportPersistencePerformed"] is False
    assert "production_ready_claim" not in quality["criticalFailFlags"]
    assert "CODEX_HOME" not in serialized
    assert ".env" not in serialized
    assert ".mcp.json" not in serialized


def test_provider_report_integration_status_is_safe_for_gui_api(tmp_path: Path) -> None:
    _write_weekly_artifact(tmp_path)
    from app.services.provider_proof_state import build_provider_proof_state_status

    status = build_provider_proof_state_status(repo_root=tmp_path)
    integration = status["providerReportIntegrationStatus"]
    serialized = json.dumps(status, sort_keys=True)

    assert integration["status"] == "dry_run_ready"
    assert integration["normalizedProviderReportDraftAvailable"] is True
    assert integration["dryRunApplyPlan"]["applied"] is False
    assert integration["realApplyStatus"] == "real_apply_blocked_pending_owner_approval"
    assert integration["previewContext"]["status"] == "provider_weekly_report_draft_proof_available"
    assert integration["reportDraftAllowed"] is True
    assert status["outputClassGateStatus"]["weekly_report"]["allowed"] is True
    assert status["outputClassGateStatus"]["paper_review"]["allowed"] is False
    assert integration["providerAuthIsAppOauthOidc"] is False
    assert integration["localMcpIsPublicConnector"] is False
    assert integration["productionReady"] is False
    assert integration["externalBetaReady"] is False
    assert "redactedProviderOutput" not in serialized
    assert '"providerOutput":' not in serialized
    assert "C:/Users" not in serialized
    assert "CODEX_HOME" not in serialized
    assert ".env" not in serialized
    assert ".mcp.json" not in serialized


def test_output_gates_and_default_runtime_selection_remain_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_weekly_artifact(tmp_path)
    from app.services import provider_report_integration as integration
    from app.services import providers
    from app.services.provider_output_class_proof import build_output_class_proof

    record = integration.load_provider_weekly_report_output_class_proof(path)
    report_proof = build_output_class_proof(
        provider_id=record.provider_family_id,
        provider_profile_name=record.provider_surface_id,
        execution_surface="sdk",
        output_class="weekly_report",
        proof_mode="output_class_live",
        profile_status="report_draft_verified_tiny",
        configured=True,
    )
    paper_proof = build_output_class_proof(
        provider_id=record.provider_family_id,
        provider_profile_name=record.provider_surface_id,
        execution_surface="sdk",
        output_class="paper_review",
        proof_mode="tiny_live",
        profile_status="live_verified_tiny",
        configured=True,
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("XRML_PROVIDER_LIVE_PROOF_STATUS", raising=False)
    monkeypatch.delenv("XRML_PROVIDER_LIVE_PROOF_ARTIFACT", raising=False)
    monkeypatch.delenv("XRML_ALLOW_PROVIDER_RESEARCH_OUTPUT", raising=False)
    runtime = providers.describe_provider_status()

    assert report_proof.accepted is True
    assert report_proof.allowed is True
    assert paper_proof.accepted is False
    assert paper_proof.allowed is False
    assert runtime["researchOutputAllowed"] is False
    assert runtime["reportGenerationAllowed"] is False
    assert providers.get_active_provider() is None
