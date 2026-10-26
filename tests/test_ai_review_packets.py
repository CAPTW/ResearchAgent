from __future__ import annotations

import json

from app.cli import review_refresh
from app.db import bootstrap
from app.repositories import ai_review_drafts, paper_reviews
from app.services import ai_review


def _review(index: int, *, full_text_status: str = "reviewed", **overrides) -> dict:
    text = (
        f"Full text evidence {index} explains retrieval augmented generation, evaluation, "
        f"limitations, and large language model implications. "
    ) * 20
    payload = {
        "topic_id": "large-language-models",
        "run_id": "ai-review-run",
        "paper_id": f"ai-paper-{index}",
        "title": f"AI Review Packet Paper {index}",
        "source_provider": "arxiv",
        "authors": ["Kim", "Lee"],
        "published_at": "2026-05-01",
        "abstract_text": "This paper studies large language models and retrieval augmented generation.",
        "text_excerpt": text if full_text_status == "reviewed" else "",
        "full_text_status": full_text_status,
        "relevance_score": 90 - index,
        "novelty_score": 60 + index,
        "evidence_strength": 80 if full_text_status == "reviewed" else 45,
        "method_summary": "Deterministic baseline method summary.",
        "key_findings": ["RAG improves evidence use.", "Evaluation needs stronger grounding."],
        "limitations": ["The deterministic summary is not AI synthesis."],
        "why_it_matters": "It improves evidence-backed LLM research reporting.",
        "topic_fit_rationale": "The paper matches LLM and RAG topic terms.",
        "review_model": "deterministic-local-full-text-v1",
        "reviewer": "deterministic-local-full-text",
        "reviewed_at": "2026-05-08T00:00:00+00:00",
        "source_text_sha256": f"sha-{index}",
        "full_text_artifact_path": f"storage/runs/ai-review-run/raw/ai-paper-{index}.txt",
        "text_char_count": len(text) if full_text_status == "reviewed" else 0,
        "evidence_spans": [
            {
                "label": f"span-{index}-a",
                "text": f"Evidence span {index} supports retrieval augmented generation.",
                "source_path": f"storage/runs/ai-review-run/raw/ai-paper-{index}.txt",
                "page_number": 1,
                "char_start": 0,
                "char_end": 64,
            }
        ]
        if full_text_status == "reviewed"
        else [],
        "review_source_type": "full_text" if full_text_status == "reviewed" else "abstract",
        "full_text_reviewed_at": "2026-05-08T00:00:00+00:00" if full_text_status == "reviewed" else None,
    }
    payload.update(overrides)
    return payload


def test_ai_review_packet_builder_exports_bounded_grounded_packets(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(2, relevance_score=88, text_excerpt="B" * 2000))
    paper_reviews.upsert_review(_review(1, relevance_score=99, text_excerpt="A" * 2000))
    paper_reviews.upsert_review(
        _review(
            3,
            full_text_status="abstract_only",
            source_text_sha256="abstract-sha",
            text_excerpt="",
            relevance_score=97,
        )
    )

    full_only = ai_review.build_ai_review_packets(
        "large-language-models",
        limit=5,
        max_chars=120,
    )
    with_abstracts = ai_review.build_ai_review_packets(
        "large-language-models",
        limit=5,
        max_chars=120,
        include_abstract_only=True,
    )

    assert [packet["paper_id"] for packet in full_only["packets"]] == ["ai-paper-1", "ai-paper-2"]
    assert full_only["full_text_grounded_packet_count"] == 2
    assert full_only["abstract_only_packet_count"] == 0
    assert full_only["packets"][0]["review_level"] == "full_text_grounded"
    assert full_only["packets"][0]["source_text_sha256"] == "sha-1"
    assert full_only["packets"][0]["evidence_spans"][0]["span_id"] == "span-1-a"
    assert sum(len(item["text"]) for item in full_only["packets"][0]["selected_text_excerpts"]) <= 120
    contract = full_only["packets"][0]["provider_output_contract"]
    assert contract["validation_gate"] == "m4_grounded_validation"
    assert "source_text_sha256" in contract["required_preserved_fields"]
    assert "cited_evidence_span_ids" in contract["required_output_fields"]
    assert any("Do not invent citations" in instruction for instruction in contract["instructions"])
    assert with_abstracts["abstract_only_packet_count"] == 1
    abstract_packet = next(packet for packet in with_abstracts["packets"] if packet["paper_id"] == "ai-paper-3")
    assert abstract_packet["review_level"] == "abstract_only"
    assert abstract_packet["full_text_status"] == "abstract_only"
    assert abstract_packet["evidence_spans"] == []


def test_ai_review_draft_validation_accepts_grounded_and_rejects_bad_evidence(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1))
    packet = ai_review.build_ai_review_packets("large-language-models", limit=1)["packets"][0]
    valid = ai_review.generate_fixture_ai_review_draft(packet)
    wrong_sha = {**valid, "source_text_sha256": "wrong"}
    missing_span = {**valid, "cited_evidence_span_ids": []}
    unsupported = {**valid, "unsupported_claims": ["claims a new benchmark not present in evidence"]}
    malformed = {"paper_id": packet["paper_id"]}

    accepted = ai_review.validate_ai_review_draft(packet, valid)
    rejected_sha = ai_review.validate_ai_review_draft(packet, wrong_sha)
    rejected_span = ai_review.validate_ai_review_draft(packet, missing_span)
    needs_review = ai_review.validate_ai_review_draft(packet, unsupported)
    rejected_malformed = ai_review.validate_ai_review_draft(packet, malformed)

    assert accepted["status"] == "accepted"
    assert accepted["accepted"] is True
    assert rejected_sha["status"] == "rejected"
    assert "source_text_sha256_mismatch" in rejected_sha["reason_codes"]
    assert rejected_span["status"] == "rejected"
    assert "missing_cited_evidence_span_ids" in rejected_span["reason_codes"]
    assert needs_review["status"] == "needs_review"
    assert "unsupported_claims_present" in needs_review["reason_codes"]
    assert rejected_malformed["status"] == "rejected"
    assert "missing_required_field" in rejected_malformed["reason_codes"]


def test_ai_review_cli_export_and_import_validate_before_storing(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    paper_reviews.upsert_review(_review(1))
    packet_path = tmp_path / "packets.json"
    draft_path = tmp_path / "drafts.json"

    dry_export_code = review_refresh.main(
        [
            "--topic",
            "large-language-models",
            "--dry-run",
            "--use-full-text",
            "--export-ai-review-packets",
            str(packet_path),
            "--ai-packet-limit",
            "1",
            "--ai-packet-max-chars",
            "120",
        ]
    )
    dry_output = json.loads(capsys.readouterr().out)

    assert dry_export_code == 0
    assert dry_output["mode"] == "ai_review_packet_export"
    assert dry_output["dry_run"] is True
    assert dry_output["packet_summary"]["packet_count"] == 1
    assert not packet_path.exists()

    export_code = review_refresh.main(
        [
            "--topic",
            "large-language-models",
            "--use-full-text",
            "--export-ai-review-packets",
            str(packet_path),
            "--ai-packet-limit",
            "1",
            "--ai-packet-max-chars",
            "120",
        ]
    )
    export_output = json.loads(capsys.readouterr().out)
    packet_payload = json.loads(packet_path.read_text(encoding="utf-8"))
    packet = packet_payload["packets"][0]
    valid_draft = ai_review.generate_fixture_ai_review_draft(packet)
    invalid_draft = {**valid_draft, "paper_id": "wrong-paper"}
    draft_path.write_text(json.dumps({"drafts": [valid_draft, invalid_draft]}, ensure_ascii=False), encoding="utf-8")

    dry_import_code = review_refresh.main(
        [
            "--topic",
            "large-language-models",
            "--dry-run",
            "--use-full-text",
            "--import-ai-review-drafts",
            str(draft_path),
            "--validate-ai-review-drafts",
        ]
    )
    dry_import_output = json.loads(capsys.readouterr().out)
    stored_after_dry = ai_review_drafts.list_drafts(topic_id="large-language-models", limit=10)

    import_code = review_refresh.main(
        [
            "--topic",
            "large-language-models",
            "--use-full-text",
            "--import-ai-review-drafts",
            str(draft_path),
            "--validate-ai-review-drafts",
        ]
    )
    import_output = json.loads(capsys.readouterr().out)
    stored = ai_review_drafts.list_drafts(topic_id="large-language-models", limit=10)

    assert export_code == 0
    assert export_output["written"] is True
    assert packet_path.exists()
    assert dry_import_code == 0
    assert dry_import_output["accepted_draft_count"] == 1
    assert dry_import_output["rejected_draft_count"] == 1
    assert stored_after_dry == []
    assert import_code == 0
    assert import_output["accepted_draft_count"] == 1
    assert import_output["rejected_draft_count"] == 1
    assert import_output["stored_draft_count"] == 1
    assert len(stored) == 1
    assert stored[0]["status"] == "accepted"
    assert stored[0]["draft"]["paper_id"] == "ai-paper-1"
