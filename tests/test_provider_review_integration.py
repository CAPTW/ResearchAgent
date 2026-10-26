from __future__ import annotations

import importlib
import json
import socket
import sqlite3
import subprocess
from pathlib import Path

import pytest


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
        "concise_review_summary": "The accepted proof supports a focused provider paper review.",
        "methodology_or_core_approach": "The review uses the supplied excerpt to describe gaze mapping for XR.",
        "key_findings": ["The method is relevant to XR interaction quality."],
        "limitations": ["Only one local excerpt is represented in this proof fixture."],
        "relevance_to_topic": "The paper is relevant because gaze mapping shapes XR interaction.",
        "follow_up_topic_suggestions": ["Compare gaze mapping accuracy across XR hardware."],
        "confidence_or_caveats": "Medium confidence because the fixture uses one excerpt.",
        "no_chain_of_thought": True,
    }


def _write_output_class_artifact(root: Path, mutator=None) -> Path:
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


def _write_project_evidence(root: Path) -> Path:
    evidence_path = root / "data" / "papers" / "text" / "2601.07571v1.txt"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(
        "GPU accelerated surface-based gaze mapping supports XR interaction review.",
        encoding="utf-8",
    )
    return evidence_path


def _configure_temp_metadata_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    include_paper: bool = True,
) -> Path:
    from app.db import bootstrap

    storage_root = tmp_path / "storage" / "runs"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", storage_root)
    db_path = bootstrap.metadata_index_path()
    with bootstrap.connect_metadata_index() as connection:
        if include_paper:
            connection.execute(
                """
                INSERT INTO runs (
                  run_id, topic, topic_slug, status, created_at, updated_at,
                  root_run_id, lineage_mode
                ) VALUES (
                  ?, 'Gaze/Foveation', 'gaze-foveation', 'completed',
                  '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', ?, 'root'
                )
                """,
                ("real-source-run", "real-source-run"),
            )
            connection.execute(
                """
                INSERT INTO papers (
                  run_id, paper_id, title, theme, score, status, published,
                  entry_url, pdf_url, dedup_cluster_id, is_primary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "real-source-run",
                    "2601.07571v1",
                    "GPU accelerated surface-based gaze mapping for XR experiences",
                    "Gaze/Foveation",
                    90,
                    "reviewed",
                    "2026-01-01",
                    None,
                    None,
                    None,
                    1,
                ),
            )
    return db_path


def test_accepted_codex_paper_review_proof_loads_and_normalizes_without_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_output_class_artifact(tmp_path)
    from app.services import provider_review_integration as integration

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

    record = integration.load_provider_paper_review_output_class_proof(path)
    normalized = integration.normalize_provider_paper_review_for_storage(record)
    serialized = json.dumps(normalized.to_dict(), sort_keys=True)

    assert record.provider_family_id == "codex"
    assert record.provider_surface_id == "codex_sdk_python"
    assert record.output_class == "paper_review"
    assert record.internal_output_class == "research_output"
    assert record.research_output_allowed is True
    assert record.report_draft_allowed is False
    assert normalized.provider_review_id.startswith("provider-review-")
    assert normalized.review_source == "provider_output_class_proof"
    assert normalized.apply_status == "dry_run_only"
    assert normalized.repository_payload["full_text_status"] == "reviewed"
    assert normalized.repository_payload["review_source_type"] == "provider_output_class_proof"
    assert normalized.repository_payload["source_provider"] == "codex"
    assert normalized.repository_payload["paper_id"] == "2601.07571v1"
    assert normalized.repository_payload["evidence_spans"][0]["proof_artifact_ref"].startswith(
        "output/provider-output-class-proof/"
    )
    assert "CODEX_HOME" not in serialized
    assert ".env" not in serialized
    assert ".mcp.json" not in serialized
    assert "auth.json" not in serialized
    assert "session.json" not in serialized
    assert "C:/Users" not in serialized


def test_provider_tiny_proof_is_rejected_for_review_integration(tmp_path: Path) -> None:
    path = _write_tiny_artifact(tmp_path)
    from app.services.provider_review_integration import (
        ProviderReviewIntegrationError,
        load_provider_paper_review_output_class_proof,
    )

    with pytest.raises(ProviderReviewIntegrationError) as exc:
        load_provider_paper_review_output_class_proof(path)

    assert "provider_tiny_proof_rejected_for_review_integration" in exc.value.reason_codes


def test_weekly_report_or_report_draft_artifact_is_rejected(tmp_path: Path) -> None:
    def mutate(artifact: dict[str, object]) -> None:
        artifact["productOutputClass"] = "weekly_report"
        artifact["internalOutputClass"] = "report_draft"
        artifact["reportDraftAllowed"] = True

    path = _write_output_class_artifact(tmp_path, mutate)
    from app.services.provider_review_integration import (
        ProviderReviewIntegrationError,
        load_provider_paper_review_output_class_proof,
    )

    with pytest.raises(ProviderReviewIntegrationError) as exc:
        load_provider_paper_review_output_class_proof(path)

    assert "not_paper_review_output_class" in exc.value.reason_codes
    assert "not_research_output_internal_class" in exc.value.reason_codes


def test_missing_evidence_spans_secret_and_chain_of_thought_are_rejected(tmp_path: Path) -> None:
    from app.services.provider_review_integration import (
        ProviderReviewIntegrationError,
        load_provider_paper_review_output_class_proof,
    )

    missing = _write_output_class_artifact(
        tmp_path / "missing",
        lambda artifact: artifact.update({"evidenceSpanCount": 0, "evidenceSpans": [], "evidenceSpanRefsUsed": []}),
    )
    secret = _write_output_class_artifact(
        tmp_path / "secret",
        lambda artifact: artifact["redactedProviderOutput"].update({"concise_review_summary": "sk-secret-value"}),
    )
    chain = _write_output_class_artifact(
        tmp_path / "chain",
        lambda artifact: artifact["redactedProviderOutput"].update({"confidence_or_caveats": "hidden reasoning"}),
    )

    with pytest.raises(ProviderReviewIntegrationError) as missing_exc:
        load_provider_paper_review_output_class_proof(missing)
    with pytest.raises(ProviderReviewIntegrationError) as secret_exc:
        load_provider_paper_review_output_class_proof(secret)
    with pytest.raises(ProviderReviewIntegrationError) as chain_exc:
        load_provider_paper_review_output_class_proof(chain)

    assert "missing_evidence_spans" in missing_exc.value.reason_codes
    assert "secret_like_content_detected" in secret_exc.value.reason_codes
    assert "chain_of_thought_like_content_detected" in chain_exc.value.reason_codes


def test_dry_run_plan_and_report_context_keep_report_draft_blocked(tmp_path: Path) -> None:
    path = _write_output_class_artifact(tmp_path)
    from app.services import provider_review_integration as integration

    record = integration.load_provider_paper_review_output_class_proof(path)
    normalized = integration.normalize_provider_paper_review_for_storage(record)
    plan = integration.plan_provider_review_db_apply(normalized)
    context = integration.build_provider_review_report_context(normalized)

    assert plan["status"] == "dry_run_ready"
    assert plan["applied"] is False
    assert plan["realApplyStatus"] == "real_apply_blocked_pending_owner_approval"
    assert plan["wouldCreateOrUpdatePaperReviewRow"] is True
    assert plan["wouldAttachEvidenceSpans"] is True
    assert plan["wouldLinkProofArtifactRef"] is True
    assert plan["wouldUpdateRunReportContext"] is False
    assert context["status"] == "provider_backed_paper_review_evidence_available"
    assert context["providerReviewCount"] == 1
    assert context["evidenceSpanCount"] == 1
    assert context["reportDraftAllowed"] is False
    assert context["providerWeeklyReportBlocked"] is True
    assert "provider-backed paper review evidence available" in context["messages"]
    assert "provider-backed weekly report still blocked" in context["messages"]


def test_temp_db_apply_writes_only_temp_db_and_preserves_provider_provenance(tmp_path: Path) -> None:
    path = _write_output_class_artifact(tmp_path)
    from app.services import provider_review_integration as integration

    record = integration.load_provider_paper_review_output_class_proof(path)
    normalized = integration.normalize_provider_paper_review_for_storage(record)
    db_path = tmp_path / "synthetic" / "provider-review.sqlite3"

    result = integration.apply_provider_review_to_temp_db(normalized, db_path=db_path)

    assert result["status"] == "temp_apply_succeeded"
    assert result["applied"] is True
    assert result["realApplyStatus"] == "real_apply_blocked_pending_owner_approval"
    assert db_path.is_file()
    with sqlite3.connect(str(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM paper_reviews").fetchone()
    assert row["topic_id"] == "xr-experiences"
    assert row["run_id"] == normalized.provider_review_id
    assert row["paper_id"] == "2601.07571v1"
    assert row["source_provider"] == "codex"
    assert row["review_model"] == "codex_sdk_python/output_class_live"
    assert row["reviewer"] == "provider:codex_sdk_python"
    assert row["review_source_type"] == "provider_output_class_proof"
    assert json.loads(row["evidence_spans_json"])[0]["proof_artifact_ref"].startswith(
        "output/provider-output-class-proof/"
    )


def test_provider_review_integration_status_is_safe_for_gui_api(tmp_path: Path) -> None:
    _write_output_class_artifact(tmp_path)
    from app.services.provider_proof_state import build_provider_proof_state_status

    status = build_provider_proof_state_status(repo_root=tmp_path)
    integration = status["providerReviewIntegrationStatus"]
    serialized = json.dumps(status, sort_keys=True)

    assert integration["status"] == "dry_run_ready"
    assert integration["normalizedProviderReviewAvailable"] is True
    assert integration["dryRunApplyPlan"]["applied"] is False
    assert integration["realApplyStatus"] == "real_apply_blocked_pending_owner_approval"
    assert integration["reportContext"]["status"] == "provider_backed_paper_review_evidence_available"
    assert integration["reportDraftAllowed"] is False
    assert status["outputClassGateStatus"]["paper_review"]["allowed"] is True
    assert status["outputClassGateStatus"]["weekly_report"]["allowed"] is False
    assert "redactedProviderOutput" not in serialized
    assert '"providerOutput":' not in serialized
    assert "CODEX_HOME" not in serialized
    assert ".env" not in serialized
    assert ".mcp.json" not in serialized
    assert "C:/Users" not in serialized


def test_real_db_apply_creates_one_backup_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_output_class_artifact(tmp_path)
    _write_project_evidence(tmp_path)
    _configure_temp_metadata_index(tmp_path, monkeypatch)
    from app.services import provider_review_integration as integration

    record = integration.load_provider_paper_review_output_class_proof(path)
    normalized = integration.normalize_provider_paper_review_for_storage(record)

    first = integration.apply_provider_review_to_real_db(
        normalized,
        repo_root=tmp_path,
        backup_timestamp="2026-07-06T07-30-00+00:00",
    )
    second = integration.apply_provider_review_to_real_db(normalized, repo_root=tmp_path)

    assert first["status"] == "real_apply_applied"
    assert first["applied"] is True
    assert first["providerReviewId"] == normalized.provider_review_id
    assert first["backup"]["backupPath"].endswith("metadata_index-pre-provider-review-apply.sqlite3")
    assert Path(first["backup"]["backupPath"]).is_file()
    assert first["readback"]["exactReviewMatches"] is True
    assert first["readback"]["row"]["sourceProvider"] == "codex"
    assert first["readback"]["row"]["reviewModel"] == "codex_sdk_python/output_class_live"
    assert first["readback"]["row"]["proofArtifactRefs"] == [normalized.proof_artifact_ref]
    assert second["status"] == "already_applied"
    assert second["applied"] is False
    assert second["backup"] is None
    backups = list((tmp_path / "storage" / "backups").glob("provider-review-real-apply-*"))
    assert len(backups) == 1


def test_real_db_apply_rejects_missing_real_paper_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_output_class_artifact(tmp_path)
    _write_project_evidence(tmp_path)
    _configure_temp_metadata_index(tmp_path, monkeypatch, include_paper=False)
    from app.services import provider_review_integration as integration

    record = integration.load_provider_paper_review_output_class_proof(path)
    normalized = integration.normalize_provider_paper_review_for_storage(record)

    with pytest.raises(integration.ProviderReviewIntegrationError) as exc:
        integration.apply_provider_review_to_real_db(normalized, repo_root=tmp_path)

    assert "PROOF_ARTIFACT_NOT_REAL_DB_APPLICABLE" in exc.value.reason_codes
    assert not (tmp_path / "storage" / "backups").exists()


def test_real_db_apply_rejects_conflicting_provider_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_output_class_artifact(tmp_path)
    _write_project_evidence(tmp_path)
    _configure_temp_metadata_index(tmp_path, monkeypatch)
    from app.repositories import paper_reviews
    from app.services import provider_review_integration as integration

    conflict_payload = {
        "topic_id": "xr-experiences",
        "run_id": "provider-review-conflict",
        "paper_id": "2601.07571v1",
        "title": "GPU accelerated surface-based gaze mapping for XR experiences",
        "source_provider": "codex",
        "full_text_status": "reviewed",
        "relevance_score": 80.0,
        "review_model": "codex_sdk_python/output_class_live",
        "reviewer": "provider:codex_sdk_python",
        "review_source_type": "provider_output_class_proof",
        "full_text_artifact_path": "data/papers/text/2601.07571v1.txt",
        "evidence_spans": [],
    }
    paper_reviews.upsert_review(conflict_payload)
    record = integration.load_provider_paper_review_output_class_proof(path)
    normalized = integration.normalize_provider_paper_review_for_storage(record)

    with pytest.raises(integration.ProviderReviewIntegrationError) as exc:
        integration.apply_provider_review_to_real_db(normalized, repo_root=tmp_path)

    assert "PROVIDER_REVIEW_CONFLICT" in exc.value.reason_codes
    assert not (tmp_path / "storage" / "backups").exists()


def test_provider_review_integration_status_reports_real_apply_when_readback_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_output_class_artifact(tmp_path)
    _write_project_evidence(tmp_path)
    _configure_temp_metadata_index(tmp_path, monkeypatch)
    from app.services import provider_review_integration as integration

    record = integration.load_provider_paper_review_output_class_proof(path)
    normalized = integration.normalize_provider_paper_review_for_storage(record)
    integration.apply_provider_review_to_real_db(
        normalized,
        repo_root=tmp_path,
        backup_timestamp="2026-07-06T07-31-00+00:00",
    )

    status = integration.build_provider_review_integration_status(path, include_real_db_status=True)
    serialized = json.dumps(status, sort_keys=True)

    assert status["status"] == "real_apply_applied"
    assert status["realApplyStatus"] == "real_apply_already_applied"
    assert status["realDbStatus"]["exactReviewMatches"] is True
    assert status["reportDraftAllowed"] is False
    assert "redactedProviderOutput" not in serialized
    assert "CODEX_HOME" not in serialized
    assert ".env" not in serialized
    assert ".mcp.json" not in serialized


def test_output_gates_and_default_runtime_selection_remain_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_output_class_artifact(tmp_path)
    from app.services import provider_review_integration as integration
    from app.services import providers
    from app.services.provider_output_class_proof import build_output_class_proof

    record = integration.load_provider_paper_review_output_class_proof(path)
    paper_proof = build_output_class_proof(
        provider_id=record.provider_family_id,
        provider_profile_name=record.provider_surface_id,
        execution_surface="sdk",
        output_class="paper_review",
        proof_mode="output_class_live",
        profile_status="research_output_verified_tiny",
        configured=True,
    )
    report_proof = build_output_class_proof(
        provider_id=record.provider_family_id,
        provider_profile_name=record.provider_surface_id,
        execution_surface="sdk",
        output_class="weekly_report",
        proof_mode="tiny_live",
        profile_status="live_verified_tiny",
        configured=True,
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("XRML_PROVIDER_LIVE_PROOF_STATUS", raising=False)
    monkeypatch.delenv("XRML_PROVIDER_LIVE_PROOF_ARTIFACT", raising=False)
    monkeypatch.delenv("XRML_ALLOW_PROVIDER_RESEARCH_OUTPUT", raising=False)
    runtime = providers.describe_provider_status()

    assert paper_proof.accepted is True
    assert paper_proof.allowed is True
    assert report_proof.accepted is False
    assert report_proof.allowed is False
    assert runtime["researchOutputAllowed"] is False
    assert runtime["reportGenerationAllowed"] is False
    assert providers.get_active_provider() is None
