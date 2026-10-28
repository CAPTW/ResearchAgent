from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app.cli import review_refresh
from app.db import bootstrap
from app.repositories import ai_review_drafts, paper_reviews
from app.repositories.jsonl import JsonlCorrupt
from app.services import ai_provider


def _review(index: int, **overrides) -> dict:
    text = (
        f"Provider bridge evidence {index} explains retrieval augmented generation, "
        f"large language model evaluation, limitations, and grounded reporting. "
    ) * 20
    payload = {
        "topic_id": "large-language-models",
        "run_id": "provider-run",
        "paper_id": f"provider-paper-{index}",
        "title": f"Provider Bridge Paper {index}",
        "source_provider": "arxiv",
        "authors": ["Kim", "Lee"],
        "published_at": "2026-05-01",
        "abstract_text": "This paper studies LLM evidence review with retrieval augmented generation.",
        "text_excerpt": text,
        "full_text_status": "reviewed",
        "relevance_score": 95 - index,
        "novelty_score": 60 + index,
        "evidence_strength": 82,
        "method_summary": "Deterministic baseline summary.",
        "key_findings": ["Grounded evidence helps synthesis."],
        "limitations": ["Provider output must be validated."],
        "why_it_matters": "It supports safe AI-assisted review drafting.",
        "topic_fit_rationale": "Matches LLM and RAG topic terms.",
        "review_model": "deterministic-local-full-text-v1",
        "reviewer": "deterministic-local-full-text",
        "reviewed_at": "2026-05-08T00:00:00+00:00",
        "source_text_sha256": f"provider-sha-{index}",
        "full_text_artifact_path": f"storage/runs/provider-run/raw/provider-paper-{index}.txt",
        "text_char_count": len(text),
        "evidence_spans": [
            {
                "label": f"provider-span-{index}",
                "text": f"Provider span {index} supports grounded review drafting.",
                "source_path": f"storage/runs/provider-run/raw/provider-paper-{index}.txt",
                "page_number": 1,
                "char_start": 0,
                "char_end": 64,
            }
        ],
        "review_source_type": "full_text",
        "full_text_reviewed_at": "2026-05-08T00:00:00+00:00",
    }
    payload.update(overrides)
    return payload


def _provider_draft(
    index: int,
    *,
    status: str = "accepted",
    provider: dict | None = None,
    run_id: str | None = None,
) -> dict:
    return {
        "topic_id": "large-language-models",
        "run_id": run_id or f"provider-origin-run-{index}",
        "paper_id": f"provider-paper-{index}",
        "source_text_sha256": f"provider-sha-{index}",
        "review_level": "full_text_grounded",
        "status": status,
        "draft": {
            "paper_id": f"provider-paper-{index}",
            "source_text_sha256": f"provider-sha-{index}",
            "contribution_summary": f"Provider origin draft {index}.",
            "method_summary": "Grounded method.",
            "evidence_summary": f"Grounded evidence cites provider-span-{index}.",
            "limitations": ["Provider limitation."],
            "relevance_to_topic": "Relevant to LLM review coverage.",
            "novelty_signal": "moderate",
            "practical_implications": "Use only after provenance labeling.",
            "follow_up_questions": [],
            "recommended_tags": ["RAG"],
            "cited_evidence_span_ids": [f"provider-span-{index}"],
            "confidence": 0.8,
            "unsupported_claims": [],
            "review_level": "full_text_grounded",
        },
        "validation": {
            "status": status,
            "reason_codes": [] if status == "accepted" else ["fixture_validation_blocked"],
            "provider": provider or {"name": "stub", "mode": "stub", "origin": "stub", "generated": True},
        },
    }


def _assert_research_output_blocked(result: dict, *, canonical_status: str | None = None) -> None:
    assert result["invoked_count"] == 0
    assert result["failed_count"] == result["planned_count"]
    item = result["results"][0]
    reason_codes = set(item["reason_codes"])
    assert "provider_research_output_blocked" in reason_codes
    assert "not_allowed_for_research_output" in reason_codes
    assert "research_output_blocked" in reason_codes
    assert item["routing_decision"]["allowed"] is False
    if canonical_status is not None:
        assert item["routing_decision"]["canonicalStatus"] == canonical_status


def _assert_smoke_research_output_blocked(result: dict) -> None:
    reason_codes = set(result["reason_codes"])
    assert result["external_provider_executed"] is False
    assert result["invoked_count"] == 0
    assert "provider_research_output_blocked" in reason_codes
    assert "not_allowed_for_research_output" in reason_codes
    assert "research_output_blocked" in reason_codes


def test_provider_origin_coverage_summary_counts_synthetic_and_real_without_inflation(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    for index in range(1, 6):
        paper_reviews.upsert_review(_review(index))
    ai_review_drafts.upsert_draft(_provider_draft(1, provider={"name": "stub", "mode": "stub", "origin": "stub", "generated": True}))
    ai_review_drafts.upsert_draft(
        _provider_draft(
            2,
            provider={"name": "command", "mode": "live_command", "origin": "command_fixture", "generated": True},
        )
    )
    ai_review_drafts.upsert_draft(
        _provider_draft(
            3,
            provider={
                "name": "command",
                "mode": "live_command",
                "origin": "real_provider",
                "generated": True,
                "operator_accepted": True,
            },
        )
    )
    ai_review_drafts.upsert_draft(_provider_draft(4, status="needs_review", provider={"name": "manual_import", "origin": "manual_import"}))
    ai_review_drafts.upsert_draft(_provider_draft(5, status="rejected", provider={"name": "stub", "mode": "stub", "origin": "stub"}))

    from app.services import provider_review_coverage

    summary = provider_review_coverage.build_provider_review_coverage_summary(
        "large-language-models",
        full_text_reviewed_denominator=5,
    )

    assert summary["acceptedAiGroundedDraftCount"] == 3
    assert summary["acceptedByOrigin"]["stub_synthetic"] == 1
    assert summary["acceptedByOrigin"]["command_fixture_synthetic"] == 1
    assert summary["acceptedByOrigin"]["command_provider_operator_accepted"] == 1
    assert summary["needsReviewByOrigin"]["manual_import"] == 1
    assert summary["rejectedByOrigin"]["stub_synthetic"] == 1
    assert summary["fixtureSyntheticCount"] == 2
    assert summary["providerOrchestratedSyntheticCount"] == 2
    assert summary["providerOrchestratedAcceptedCount"] == 3
    assert summary["realProviderAcceptedCount"] == 1
    assert summary["fullTextReviewedDenominator"] == 5
    assert "synthetic_provider_drafts_present" in summary["reasonCodes"]


def test_provider_provenance_audit_detects_mismatch_unknown_leak_and_missing_m4(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    for index in range(1, 5):
        paper_reviews.upsert_review(_review(index))
    ai_review_drafts.upsert_draft(_provider_draft(1, provider={"name": "stub", "mode": "stub", "origin": "stub", "generated": True}))
    ai_review_drafts.upsert_draft(
        _provider_draft(2, provider={"name": "mystery-provider", "mode": "experimental", "origin": "unknown-origin", "generated": True})
    )
    missing_m4 = _provider_draft(3, provider={"name": "command", "mode": "live_command", "origin": "command_fixture", "generated": True})
    missing_m4["validation"] = {"provider": missing_m4["validation"]["provider"]}
    ai_review_drafts.upsert_draft(missing_m4)
    ai_review_drafts.upsert_draft(_provider_draft(4, status="needs_review", provider={"name": "stub", "mode": "stub", "origin": "stub"}))

    from app.services import provider_review_coverage

    coverage = provider_review_coverage.build_provider_review_coverage_summary(
        "large-language-models",
        full_text_reviewed_denominator=4,
    )
    dashboard_summary = {**coverage, "realProviderAcceptedCount": 7}
    recommendation_payload = {
        "gap_candidates": [
            {
                "supporting_evidence_refs": [
                    {
                        "paper_id": "provider-paper-4",
                        "review_level": "accepted_ai_grounded_synthetic",
                        "provider_origin": "stub_synthetic",
                    }
                ]
            }
        ],
        "new_topic_candidates": [],
    }

    audit = provider_review_coverage.build_provider_provenance_audit(
        "large-language-models",
        dashboard_summary=dashboard_summary,
        recommendation_payload=recommendation_payload,
    )

    assert audit["auditStatus"] == "failed"
    assert audit["mismatchCount"] >= 1
    assert audit["syntheticCount"] == 2
    assert audit["realProviderCount"] == 0
    assert audit["unknownOriginCount"] == 1
    assert audit["rejectedOrNeedsReviewLeakCount"] == 1
    assert audit["missingM4ValidationCount"] == 1
    assert "dashboard_provider_coverage_mismatch" in audit["reasonCodes"]
    assert "unknown_provider_origin" in audit["reasonCodes"]
    assert "rejected_or_needs_review_recommendation_leak" in audit["reasonCodes"]
    assert "accepted_provider_draft_missing_m4_validation" in audit["reasonCodes"]


def test_provider_provenance_audit_validates_consistent_synthetic_and_real_counts(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    for index in range(1, 3):
        paper_reviews.upsert_review(_review(index))
    ai_review_drafts.upsert_draft(_provider_draft(1, provider={"name": "stub", "mode": "stub", "origin": "stub", "generated": True}))
    ai_review_drafts.upsert_draft(
        _provider_draft(
            2,
            provider={
                "name": "command",
                "mode": "live_command",
                "origin": "real_provider",
                "generated": True,
                "operator_accepted": True,
            },
        )
    )

    from app.services import provider_review_coverage

    coverage = provider_review_coverage.build_provider_review_coverage_summary(
        "large-language-models",
        full_text_reviewed_denominator=2,
    )
    audit = provider_review_coverage.build_provider_provenance_audit(
        "large-language-models",
        dashboard_summary=coverage,
        recommendation_payload={
            "gap_candidates": [
                {
                    "supporting_evidence_refs": [
                        {"paper_id": "provider-paper-1", "review_level": "accepted_ai_grounded_synthetic"},
                        {"paper_id": "provider-paper-2", "review_level": "accepted_ai_grounded"},
                    ]
                }
            ],
            "new_topic_candidates": [],
        },
    )

    assert audit["auditStatus"] == "valid"
    assert audit["mismatchCount"] == 0
    assert audit["syntheticCount"] == 1
    assert audit["realProviderCount"] == 1
    assert audit["unknownOriginCount"] == 0
    assert audit["rejectedOrNeedsReviewLeakCount"] == 0
    assert audit["missingM4ValidationCount"] == 0


def test_stub_provider_dry_run_plans_invocations_and_writes_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    for index in range(1, 4):
        paper_reviews.upsert_review(_review(index))

    result = ai_provider.run_ai_review_provider(
        "large-language-models",
        provider="stub",
        mode="dry_run",
        limit=2,
        output_root=tmp_path / "provider-output",
        import_provider_drafts=True,
    )

    assert result["planned_count"] == 2
    assert result["invoked_count"] == 0
    assert result["generated_count"] == 0
    assert result["accepted_count"] == 0
    assert result["results"][0]["status"] == "planned"
    assert not (tmp_path / "provider-output").exists()
    assert ai_review_drafts.list_drafts(topic_id="large-language-models", limit=10) == []


def test_stub_provider_valid_and_invalid_outputs_are_validated_before_storage(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    for index in range(1, 6):
        paper_reviews.upsert_review(_review(index))

    result = ai_provider.run_ai_review_provider(
        "large-language-models",
        provider="stub",
        mode="stub",
        limit=5,
        output_root=tmp_path / "provider-output",
        import_provider_drafts=True,
        stub_scenarios=[
            "valid",
            "wrong_sha256",
            "missing_evidence_span",
            "unsupported_claim",
            "malformed_json",
        ],
    )
    stored = ai_review_drafts.list_drafts(topic_id="large-language-models", limit=10)
    output_files = sorted((tmp_path / "provider-output").glob("*.json"))

    assert result["planned_count"] == 5
    assert result["invoked_count"] == 5
    assert result["generated_count"] == 4
    assert result["accepted_count"] == 1
    assert result["needs_review_count"] == 1
    assert result["rejected_count"] == 3
    assert result["failed_count"] == 0
    assert len(stored) == 1
    assert stored[0]["draft"]["paper_id"] == "provider-paper-1"
    assert len(output_files) == 5
    assert any("source_text_sha256_mismatch" in item["reason_codes"] for item in result["results"])
    assert any("missing_cited_evidence_span_ids" in item["reason_codes"] for item in result["results"])
    assert any("unsupported_claims_present" in item["reason_codes"] for item in result["results"])


def test_review_refresh_cli_runs_stub_provider_and_imports_only_accepted(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    for index in range(1, 3):
        paper_reviews.upsert_review(_review(index))

    exit_code = review_refresh.main(
        [
            "--topic",
            "large-language-models",
            "--use-full-text",
            "--run-ai-review-provider",
            "--ai-provider",
            "stub",
            "--ai-provider-limit",
            "2",
            "--ai-provider-output-dir",
            str(tmp_path / "provider-output"),
            "--import-provider-drafts",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["mode"] == "ai_review_provider_run"
    assert output["provider"] == "stub"
    assert output["accepted_count"] == 2
    assert output["stored_draft_count"] == 2
    assert output["provider_origin"] == "stub"
    assert output["reason_counts"] == {}
    assert len(ai_review_drafts.list_drafts(topic_id="large-language-models", limit=10)) == 2


def test_provider_run_skips_existing_accepted_drafts_by_default(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1))
    first = ai_provider.run_ai_review_provider(
        "large-language-models",
        provider="stub",
        mode="stub",
        limit=1,
        output_root=tmp_path / "provider-output",
        import_provider_drafts=True,
        provider_run_id="provider-run-first",
        provider_origin="stub",
    )
    second = ai_provider.run_ai_review_provider(
        "large-language-models",
        provider="stub",
        mode="stub",
        limit=1,
        output_root=tmp_path / "provider-output",
        import_provider_drafts=True,
        provider_run_id="provider-run-second",
        provider_origin="stub",
    )
    stored = ai_review_drafts.list_drafts(topic_id="large-language-models", limit=10)

    assert first["accepted_count"] == 1
    assert first["stored_draft_count"] == 1
    assert second["planned_count"] == 1
    assert second["invoked_count"] == 0
    assert second["skipped_existing_accepted_count"] == 1
    assert second["results"][0]["status"] == "skipped"
    assert second["results"][0]["reason_codes"] == ["existing_accepted_draft"]
    assert len(stored) == 1
    assert stored[0]["validation"]["provider"]["run_id"] == "provider-run-first"


def test_provider_force_refresh_replaces_existing_accepted_draft_explicitly(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1))
    ai_provider.run_ai_review_provider(
        "large-language-models",
        provider="stub",
        mode="stub",
        limit=1,
        output_root=tmp_path / "provider-output",
        import_provider_drafts=True,
        provider_run_id="provider-run-first",
        provider_origin="stub",
    )
    refreshed = ai_provider.run_ai_review_provider(
        "large-language-models",
        provider="stub",
        mode="stub",
        limit=1,
        output_root=tmp_path / "provider-output",
        import_provider_drafts=True,
        provider_run_id="provider-run-second",
        provider_origin="stub",
        force_refresh=True,
    )
    stored = ai_review_drafts.list_drafts(topic_id="large-language-models", limit=10)

    assert refreshed["invoked_count"] == 1
    assert refreshed["accepted_count"] == 1
    assert refreshed["stored_draft_count"] == 1
    assert refreshed["skipped_existing_accepted_count"] == 0
    assert len(stored) == 1
    assert stored[0]["validation"]["provider"]["run_id"] == "provider-run-second"
    assert stored[0]["validation"]["provider"]["origin"] == "stub"


def test_review_refresh_cli_preserves_provider_run_policy_flags(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1))
    base_args = [
        "--topic",
        "large-language-models",
        "--use-full-text",
        "--run-ai-review-provider",
        "--ai-provider",
        "stub",
        "--ai-provider-limit",
        "1",
        "--ai-provider-output-dir",
        str(tmp_path / "provider-output"),
        "--import-provider-drafts",
    ]

    assert review_refresh.main([*base_args, "--ai-provider-run-id", "provider-cli-first", "--ai-provider-origin", "stub"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert review_refresh.main([*base_args, "--ai-provider-run-id", "provider-cli-second", "--ai-provider-origin", "stub"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert review_refresh.main(
        [
            *base_args,
            "--ai-provider-run-id",
            "provider-cli-third",
            "--ai-provider-origin",
            "stub",
            "--ai-provider-force-refresh",
        ]
    ) == 0
    third = json.loads(capsys.readouterr().out)
    stored = ai_review_drafts.list_drafts(topic_id="large-language-models", limit=10)

    assert first["provider_run_id"] == "provider-cli-first"
    assert second["skipped_existing_accepted_count"] == 1
    assert second["invoked_count"] == 0
    assert third["accepted_count"] == 1
    assert third["stored_draft_count"] == 1
    assert stored[0]["validation"]["provider"]["run_id"] == "provider-cli-third"


def test_command_provider_requires_explicit_live_allowance(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1))

    result = ai_provider.run_ai_review_provider(
        "large-language-models",
        provider="command",
        mode="live_command",
        command=["fake-ai-reviewer"],
        allow_live_provider=False,
        limit=1,
        output_root=tmp_path / "provider-output",
        import_provider_drafts=True,
    )

    assert result["planned_count"] == 1
    _assert_research_output_blocked(result, canonical_status="docs_only")
    assert result["reason_counts"]["provider_research_output_blocked"] == 1
    assert result["results"][0]["status"] == "failed"
    assert not (tmp_path / "provider-output").exists()


def test_real_command_provider_requires_operator_acceptance_before_execution(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    monkeypatch.setattr(ai_provider.provider_configurations, "_command_exists", lambda _command: True)
    paper_reviews.upsert_review(_review(1))

    def fail_if_called(*args, **kwargs):
        raise AssertionError("real provider command must not run without operator acceptance")

    monkeypatch.setattr(ai_provider.subprocess, "run", fail_if_called)
    result = ai_provider.run_ai_review_provider(
        "large-language-models",
        provider="command",
        mode="live_command",
        provider_config="gemini-cli-example",
        allow_live_provider=True,
        limit=1,
        output_root=tmp_path / "provider-output",
        import_provider_drafts=True,
    )

    assert result["planned_count"] == 1
    _assert_research_output_blocked(result, canonical_status="non_live_available")
    assert result["operator_acceptance_required"] is True
    assert result["operator_accepted"] is False
    assert "live_proof_missing" in result["results"][0]["reason_codes"]
    assert not (tmp_path / "provider-output").exists()


def test_real_command_provider_records_operator_acceptance_without_note_text(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    monkeypatch.setattr(ai_provider.provider_configurations, "_command_exists", lambda _command: True)
    paper_reviews.upsert_review(_review(1))

    def fail_if_called(*args, **kwargs):
        raise AssertionError("real provider command must not run while research output is blocked")

    monkeypatch.setattr(ai_provider.subprocess, "run", fail_if_called)
    result = ai_provider.run_ai_review_provider(
        "large-language-models",
        provider="command",
        mode="live_command",
        provider_config="gemini-cli-example",
        allow_live_provider=True,
        accept_provider_risk=True,
        provider_operator_note="operator verified local gemini wrapper",
        limit=1,
        output_root=tmp_path / "provider-output",
        run_dir=tmp_path / "provider-run",
        import_provider_drafts=True,
        provider_run_id="real-provider-accepted",
    )
    stored = ai_review_drafts.list_drafts(topic_id="large-language-models", limit=10)

    _assert_research_output_blocked(result, canonical_status="non_live_available")
    assert result["accepted_count"] == 0
    assert result["stored_draft_count"] == 0
    assert result["operator_acceptance_required"] is True
    assert result["operator_accepted"] is True
    assert result["operator_note_present"] is True
    assert result["operator_note_sha256"]
    assert "operator verified local gemini wrapper" not in json.dumps(result)
    assert stored == []


def test_command_provider_handles_timeout_and_max_output(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1))

    def fake_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout"))

    monkeypatch.setattr(ai_provider.subprocess, "run", fake_timeout)
    timed_out = ai_provider.run_ai_review_provider(
        "large-language-models",
        provider="command",
        mode="live_command",
        provider_config="fixture-command",
        command=["fake-ai-reviewer"],
        allow_live_provider=True,
        limit=1,
        timeout_seconds=1,
        output_root=tmp_path / "timeout-output",
    )

    def fake_large_output(*args, **kwargs):
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="{" + ("x" * 100), stderr="")

    monkeypatch.setattr(ai_provider.subprocess, "run", fake_large_output)
    too_large = ai_provider.run_ai_review_provider(
        "large-language-models",
        provider="command",
        mode="live_command",
        provider_config="fixture-command",
        command=["fake-ai-reviewer"],
        allow_live_provider=True,
        limit=1,
        max_output_chars=20,
        output_root=tmp_path / "large-output",
    )

    assert timed_out["timed_out_count"] == 1
    assert timed_out["results"][0]["status"] == "timed_out"
    assert too_large["rejected_count"] == 1
    assert too_large["results"][0]["status"] == "rejected"
    assert "provider_output_too_large" in too_large["results"][0]["reason_codes"]


def test_command_provider_dry_run_does_not_invoke_subprocess(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1))

    def fail_if_called(*args, **kwargs):
        raise AssertionError("subprocess must not be invoked during dry-run")

    monkeypatch.setattr(ai_provider.subprocess, "run", fail_if_called)

    result = ai_provider.run_ai_review_provider(
        "large-language-models",
        provider="command",
        mode="dry_run",
        command=["fake-ai-reviewer"],
        allow_live_provider=True,
        limit=1,
        output_root=tmp_path / "provider-output",
    )

    assert result["planned_count"] == 1
    assert result["invoked_count"] == 0
    assert result["results"][0]["status"] == "planned"
    assert result["results"][0]["planned_invocation"]["provider"] == "command"
    assert result["results"][0]["planned_invocation"]["command_present"] is True
    assert "packet" not in result["results"][0]["planned_invocation"]
    assert not (tmp_path / "provider-output").exists()


def test_command_provider_live_execution_uses_isolated_files_and_validates_before_storage(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1))
    run_dir = tmp_path / "provider-runs" / "fixture-run"
    calls: list[tuple[list[str], dict]] = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        assert isinstance(args, list)
        assert kwargs.get("shell") is False
        assert "input" not in kwargs
        assert "{packet_path}" not in args
        packet_path = Path(args[args.index("--packet") + 1])
        output_path = Path(args[args.index("--output") + 1])
        assert packet_path.is_file()
        assert output_path.parent.is_dir()
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        output_path.write_text(json.dumps(ai_provider.ai_review.generate_fixture_ai_review_draft(packet)), encoding="utf-8")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="fixture complete", stderr="")

    monkeypatch.setattr(ai_provider.subprocess, "run", fake_run)
    result = ai_provider.run_ai_review_provider(
        "large-language-models",
        provider="command",
        mode="live_command",
        provider_config="fixture-command",
        command=[
            "fixture-provider",
            "--packet",
            "{packet_path}",
            "--output",
            "{output_path}",
            "--run-id",
            "{provider_run_id}",
            "--topic",
            "{topic_id}",
        ],
        allow_live_provider=True,
        limit=1,
        output_root=tmp_path / "provider-output",
        run_dir=run_dir,
        import_provider_drafts=True,
        provider_run_id="fixture-run",
        provider_origin="command_fixture",
    )
    stored = ai_review_drafts.list_drafts(topic_id="large-language-models", limit=10)
    item = result["results"][0]

    assert len(calls) == 1
    assert result["planned_count"] == 1
    assert result["invoked_count"] == 1
    assert result["succeeded_count"] == 1
    assert result["accepted_count"] == 1
    assert result["stored_draft_count"] == 1
    assert result["provider_run_dir"] == str(run_dir)
    assert Path(item["packet_path"]).is_file()
    assert Path(item["provider_output_path"]).is_file()
    assert item["exit_code"] == 0
    assert item["status"] == "generated"
    assert stored[0]["validation"]["provider"]["origin"] == "command_fixture"
    assert stored[0]["validation"]["provider"]["provider_run_id"] == "fixture-run"


def test_command_provider_live_execution_counts_truncated_stdout_and_stderr(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1))

    def fake_run(args, **kwargs):
        packet_path = Path(args[args.index("--packet") + 1])
        output_path = Path(args[args.index("--output") + 1])
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        output_path.write_text(json.dumps(ai_provider.ai_review.generate_fixture_ai_review_draft(packet)), encoding="utf-8")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="s" * 6000, stderr="e" * 6000)

    monkeypatch.setattr(ai_provider.subprocess, "run", fake_run)
    result = ai_provider.run_ai_review_provider(
        "large-language-models",
        provider="command",
        mode="live_command",
        provider_config="fixture-command",
        command=["fixture-provider", "--packet", "{packet_path}", "--output", "{output_path}"],
        allow_live_provider=True,
        limit=1,
        max_output_chars=5000,
        output_root=tmp_path / "provider-output",
        run_dir=tmp_path / "provider-run",
        import_provider_drafts=True,
        provider_run_id="truncated-run",
    )
    item = result["results"][0]

    assert result["output_truncated_count"] == 2
    assert result["accepted_count"] == 1
    assert item["stdout_truncated"] is True
    assert item["stderr_truncated"] is True
    assert Path(item["stdout_path"]).read_text(encoding="utf-8") == "s" * 5000
    assert Path(item["stderr_path"]).read_text(encoding="utf-8") == "e" * 5000


def test_command_provider_live_execution_rejects_wrong_sha_before_storage(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1))

    def fake_run(args, **kwargs):
        packet_path = Path(args[args.index("--packet") + 1])
        output_path = Path(args[args.index("--output") + 1])
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        draft = ai_provider.ai_review.generate_fixture_ai_review_draft(packet)
        draft["source_text_sha256"] = "wrong-command-sha"
        output_path.write_text(json.dumps(draft), encoding="utf-8")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ai_provider.subprocess, "run", fake_run)
    result = ai_provider.run_ai_review_provider(
        "large-language-models",
        provider="command",
        mode="live_command",
        provider_config="fixture-command",
        command=["fixture-provider", "--packet", "{packet_path}", "--output", "{output_path}"],
        allow_live_provider=True,
        limit=1,
        output_root=tmp_path / "provider-output",
        run_dir=tmp_path / "provider-run",
        import_provider_drafts=True,
        provider_run_id="wrong-sha-run",
    )

    assert result["invoked_count"] == 1
    assert result["succeeded_count"] == 1
    assert result["accepted_count"] == 0
    assert result["rejected_count"] == 1
    assert result["stored_draft_count"] == 0
    assert result["reason_counts"]["source_text_sha256_mismatch"] == 1
    assert ai_review_drafts.list_drafts(topic_id="large-language-models", limit=10) == []


def test_review_refresh_cli_runs_fixture_command_provider_with_explicit_allowance(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1))

    exit_code = review_refresh.main(
        [
            "--topic",
            "large-language-models",
            "--use-full-text",
            "--run-ai-review-provider",
            "--ai-provider",
            "command",
            "--ai-provider-config",
            "fixture-command",
            "--ai-provider-limit",
            "1",
            "--allow-live-ai-provider",
            "--ai-provider-run-dir",
            str(tmp_path / "fixture-command-run"),
            "--import-provider-drafts",
            "--ai-provider-run-id",
            "fixture-cli-run",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["mode"] == "ai_review_provider_run"
    assert output["provider"] == "command"
    assert output["provider_origin"] == "command_fixture"
    assert output["invoked_count"] == 1
    assert output["succeeded_count"] == 1
    assert output["accepted_count"] == 1
    assert output["stored_draft_count"] == 1
    assert output["provider_run_dir"] == str(tmp_path / "fixture-command-run")


def test_review_refresh_cli_requires_acceptance_for_real_provider_config(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    monkeypatch.setattr(ai_provider.provider_configurations, "_command_exists", lambda _command: True)
    paper_reviews.upsert_review(_review(1))

    exit_code = review_refresh.main(
        [
            "--topic",
            "large-language-models",
            "--use-full-text",
            "--run-ai-review-provider",
            "--ai-provider",
            "command",
            "--ai-provider-config",
            "gemini-cli-example",
            "--ai-provider-limit",
            "1",
            "--allow-live-ai-provider",
            "--ai-provider-run-dir",
            str(tmp_path / "real-command-run"),
            "--import-provider-drafts",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["failed_count"] == 1
    _assert_research_output_blocked(output, canonical_status="non_live_available")
    assert output["operator_acceptance_required"] is True
    assert output["operator_accepted"] is False


def test_provider_acceptance_smoke_dry_run_serializes_fixture_summary(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1))

    result = ai_provider.run_provider_acceptance_smoke(
        "large-language-models",
        provider_config="fixture-command",
        dry_run=True,
        limit=1,
        run_dir=tmp_path / "fixture-smoke",
        import_provider_drafts=True,
    )

    assert result["mode"] == "provider_acceptance_smoke"
    assert result["provider_config_id"] == "fixture-command"
    assert result["provider_role"] == "fixture"
    assert result["readiness_status"] == "ready_fixture"
    assert result["validation_gate"] == "m4_grounded_validation"
    assert result["fixture_only"] is True
    assert result["external_provider_executed"] is False
    assert result["planned_count"] == 1
    assert result["invoked_count"] == 0
    assert result["operator_acceptance_required"] is False
    assert not (tmp_path / "fixture-smoke").exists()
    json.dumps(result)


def test_provider_acceptance_smoke_missing_real_binary_is_readiness_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1))

    def fail_if_called(*args, **kwargs):
        raise AssertionError("missing real provider binary must not invoke subprocess")

    monkeypatch.setattr(ai_provider.subprocess, "run", fail_if_called)
    result = ai_provider.run_provider_acceptance_smoke(
        "large-language-models",
        provider_config="gemini-cli-example",
        dry_run=False,
        limit=1,
        run_dir=tmp_path / "real-smoke",
        allow_live_provider=True,
        accept_provider_risk=True,
        provider_operator_note="operator accepted local smoke",
        import_provider_drafts=True,
        command_exists=lambda command: False,
    )

    assert result["mode"] == "provider_acceptance_smoke"
    assert result["provider_config_id"] == "gemini-cli-example"
    assert result["provider_role"] == "real_example"
    assert result["readiness_status"] == "command_missing"
    assert result["command_missing_count"] == 1
    assert result["failed_count"] == 0
    assert result["invoked_count"] == 0
    assert result["external_provider_executed"] is False
    assert "provider_command_missing" in result["reason_codes"]
    assert not (tmp_path / "real-smoke").exists()


def test_provider_acceptance_smoke_real_provider_validates_before_storage(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1))

    def fail_if_called(*args, **kwargs):
        raise AssertionError("real provider command must not run while research output is blocked")

    monkeypatch.setattr(ai_provider.subprocess, "run", fail_if_called)
    result = ai_provider.run_provider_acceptance_smoke(
        "large-language-models",
        provider_config="gemini-cli-example",
        dry_run=False,
        limit=1,
        run_dir=tmp_path / "real-smoke",
        allow_live_provider=True,
        accept_provider_risk=True,
        provider_operator_note="operator accepted local smoke",
        import_provider_drafts=True,
        command_exists=lambda command: True,
        provider_run_id="m9-real-smoke",
    )
    stored = ai_review_drafts.list_drafts(topic_id="large-language-models", limit=10)

    assert result["provider_config_id"] == "gemini-cli-example"
    assert result["operator_acceptance_required"] is True
    assert result["operator_acceptance_present"] is True
    _assert_smoke_research_output_blocked(result)
    assert result["fixture_only"] is False
    assert result["accepted_count"] == 0
    assert result["failed_count"] == 1
    assert result["validation_gate"] == "m4_grounded_validation"
    assert stored == []
    assert "operator accepted local smoke" not in json.dumps(result)


def test_review_refresh_cli_provider_acceptance_smoke_fails_closed_without_acceptance(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    monkeypatch.setattr(ai_provider.provider_configurations, "_command_exists", lambda _command: True)
    paper_reviews.upsert_review(_review(1))

    exit_code = review_refresh.main(
        [
            "--topic",
            "large-language-models",
            "--use-full-text",
            "--provider-acceptance-smoke",
            "--ai-provider-config",
            "gemini-cli-example",
            "--ai-provider-limit",
            "1",
            "--allow-live-ai-provider",
            "--ai-provider-run-dir",
            str(tmp_path / "real-command-run"),
            "--import-provider-drafts",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["mode"] == "provider_acceptance_smoke"
    assert output["failed_count"] == 1
    _assert_smoke_research_output_blocked(output)
    assert output["operator_acceptance_required"] is True
    assert output["operator_acceptance_present"] is False
    assert "live_proof_missing" in output["reason_codes"]


def test_manual_provider_run_dry_run_writes_nothing_and_previews_artifacts(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1))

    result = ai_provider.prepare_manual_provider_run(
        "large-language-models",
        output_dir=tmp_path / "manual-run",
        limit=1,
        provider_run_id="manual-run-1",
        dry_run=True,
    )

    assert result["mode"] == "provider_manual_run_prepare"
    assert result["dry_run"] is True
    assert result["packet_count"] == 1
    assert result["command_preview_count"] == 1
    assert result["written"] is False
    assert result["no_command_executed"] is True
    assert not (tmp_path / "manual-run").exists()
    assert any(path.endswith("provider_run_manifest.json") for path in result["planned_artifacts"])


def test_manual_provider_run_prepare_writes_expected_artifacts_without_executing_command(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    for index in range(1, 3):
        paper_reviews.upsert_review(_review(index))

    def fail_if_called(*args, **kwargs):
        raise AssertionError("manual provider run preparation must not execute a subprocess")

    monkeypatch.setattr(ai_provider.subprocess, "run", fail_if_called)
    result = ai_provider.prepare_manual_provider_run(
        "large-language-models",
        output_dir=tmp_path / "manual-run",
        limit=2,
        provider_run_id="manual-run-2",
        dry_run=False,
    )

    assert result["written"] is True
    assert result["packet_count"] == 2
    assert result["provider_run_id"] == "manual-run-2"
    assert (tmp_path / "manual-run" / "packets.json").exists()
    assert (tmp_path / "manual-run" / "command_preview.json").exists()
    assert (tmp_path / "manual-run" / "README.md").exists()
    assert (tmp_path / "manual-run" / "expected_output_schema.json").exists()
    assert (tmp_path / "manual-run" / "provider_run_manifest.json").exists()
    assert (tmp_path / "manual-run" / "fixture-provider-output.json").exists()
    manifest = json.loads((tmp_path / "manual-run" / "provider_run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["no_command_executed"] is True
    assert manifest["command_template_validation"]["valid"] is True


def test_manual_provider_output_import_fails_closed_on_malformed_record(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1))
    packet = ai_provider.prepare_manual_provider_run(
        "large-language-models",
        output_dir=tmp_path / "manual-run",
        limit=1,
        provider_run_id="manual-run-import",
        dry_run=False,
    )["packets"][0]
    output_path = tmp_path / "manual-output.jsonl"
    valid = ai_provider.ai_review.generate_fixture_ai_review_draft(packet)
    wrong_sha = {**valid, "source_text_sha256": "wrong-sha"}
    missing_span = {**valid, "cited_evidence_span_ids": []}
    output_path.write_text(
        "\n".join(
            [
                json.dumps(valid),
                "{not-json",
                json.dumps(wrong_sha),
                json.dumps(missing_span),
            ]
        ),
        encoding="utf-8",
    )

    before = ai_review_drafts.list_drafts(topic_id="large-language-models", limit=10)
    with pytest.raises(JsonlCorrupt) as exc_info:
        ai_provider.import_manual_provider_outputs(
            "large-language-models",
            output_path,
            provider_run_id="manual-run-import",
        )
    stored = ai_review_drafts.list_drafts(topic_id="large-language-models", limit=10)

    assert exc_info.value.code == "jsonl_corrupt"
    assert exc_info.value.line_number == 2
    assert stored == before == []


def test_manual_provider_output_import_skips_existing_accepted_by_default(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1))
    packet = ai_provider.prepare_manual_provider_run(
        "large-language-models",
        output_dir=tmp_path / "manual-run",
        limit=1,
        provider_run_id="manual-run-skip",
        dry_run=False,
    )["packets"][0]
    output_path = tmp_path / "manual-output.json"
    output_path.write_text(json.dumps([ai_provider.ai_review.generate_fixture_ai_review_draft(packet)]), encoding="utf-8")

    first = ai_provider.import_manual_provider_outputs("large-language-models", output_path, provider_run_id="manual-run-first")
    second = ai_provider.import_manual_provider_outputs("large-language-models", output_path, provider_run_id="manual-run-second")
    stored = ai_review_drafts.list_drafts(topic_id="large-language-models", limit=10)

    assert first["accepted_count"] == 1
    assert first["stored_draft_count"] == 1
    assert second["accepted_count"] == 0
    assert second["skipped_existing_accepted_count"] == 1
    assert second["stored_draft_count"] == 0
    assert len(stored) == 1
    assert stored[0]["validation"]["provider"]["provider_run_id"] == "manual-run-first"


def test_review_refresh_cli_prepares_manual_provider_run_dry_run_without_writes(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1))

    exit_code = review_refresh.main(
        [
            "--topic",
            "large-language-models",
            "--dry-run",
            "--use-full-text",
            "--prepare-provider-manual-run",
            "--ai-provider",
            "command",
            "--ai-provider-limit",
            "1",
            "--provider-manual-run-dir",
            str(tmp_path / "manual-run"),
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["mode"] == "provider_manual_run_prepare"
    assert output["dry_run"] is True
    assert output["packet_count"] == 1
    assert output["no_command_executed"] is True
    assert not (tmp_path / "manual-run").exists()


def test_review_refresh_cli_imports_manual_provider_output_path_with_validation(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1))
    packet = ai_provider.prepare_manual_provider_run(
        "large-language-models",
        output_dir=tmp_path / "manual-run",
        limit=1,
        provider_run_id="manual-cli-run",
        dry_run=False,
    )["packets"][0]
    output_path = tmp_path / "manual-output.json"
    output_path.write_text(json.dumps([ai_provider.ai_review.generate_fixture_ai_review_draft(packet)]), encoding="utf-8")

    exit_code = review_refresh.main(
        [
            "--topic",
            "large-language-models",
            "--import-provider-drafts",
            str(output_path),
            "--validate-ai-review-drafts",
            "--ai-provider-run-id",
            "manual-cli-run",
        ]
    )
    output = json.loads(capsys.readouterr().out)
    stored = ai_review_drafts.list_drafts(topic_id="large-language-models", limit=10)

    assert exit_code == 0
    assert output["mode"] == "manual_provider_output_import"
    assert output["accepted_count"] == 1
    assert output["stored_draft_count"] == 1
    assert stored[0]["validation"]["provider"]["origin"] == "manual_command_import"
