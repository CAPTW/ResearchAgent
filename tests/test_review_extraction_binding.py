import hashlib
import json
from pathlib import Path

import pytest

from app.domain.review_input import enrich_packet, validate_packet
from app.domain.review_extraction import validate_provenance
from app.services import paper_review, pdf_acquisition


def _read(tmp_path, monkeypatch, *, pages=False, limit=401):
    text = "abc한글 " * 200
    target = tmp_path / ("paper.pages.json" if pages else "paper.txt")
    target.write_bytes(json.dumps({"pages": [{"page": 2, "text": text}]}, ensure_ascii=False).encode() if pages else text.encode())
    monkeypatch.setattr(paper_review.legacy_reviews, "resolve_paper_artifacts", lambda *_: {"pages_json" if pages else "text_txt": str(target)})
    result = paper_review.read_local_full_text_evidence(run_id="run", paper_id="paper", max_text_chars=limit)
    return result, target, text.strip()


@pytest.mark.parametrize("pages", [False, True])
def test_read_preserves_byte_identity_and_selection_limit(tmp_path, monkeypatch, pages):
    result, path, text = _read(tmp_path, monkeypatch, pages=pages)
    provenance = result["extraction_provenance"]
    assert provenance["artifact"] == {"kind": "pages" if pages else "text", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
    assert provenance["selection"] == {"sha256": result["source_text_sha256"], "chars": len(result["text_excerpt"]), "max_chars": 401, "available_chars": len(text), "truncation": "TRUNCATED"}
    assert provenance["pdf_extraction"] is None


def test_new_boundary_does_not_hash_text_repository_would_strip(tmp_path, monkeypatch):
    result, _, _ = _read(tmp_path, monkeypatch, limit=402)
    assert result["text_excerpt"] == result["text_excerpt"].strip()
    assert result["source_text_sha256"] == hashlib.sha256(result["text_excerpt"].encode()).hexdigest()


def test_new_record_requires_exact_source_bytes_on_revalidation(tmp_path, monkeypatch):
    result, path, _ = _read(tmp_path, monkeypatch)
    result["run_id"] = "run"
    monkeypatch.setattr(paper_review.artifact_refs, "resolve_artifact_reference", lambda *_a, **_k: type("Ref", (), {"is_safe": True, "exists": True, "resolved_path": path})())
    assert paper_review.validate_reviewed_artifact_content(result)["valid"]
    path.write_bytes(path.read_bytes() + b" changed suffix")
    validation = paper_review.validate_reviewed_artifact_content(result)
    assert not validation["valid"]
    assert validation["reason"] == "extraction_artifact_bytes_mismatch"


@pytest.mark.parametrize("pages", [False, True])
def test_byte_integrity_and_text_validation_share_one_observation(tmp_path, monkeypatch, pages):
    result, path, _ = _read(tmp_path, monkeypatch, pages=pages)
    result["run_id"] = "run"
    monkeypatch.setattr(paper_review.artifact_refs, "resolve_artifact_reference", lambda *_a, **_k: type("Ref", (), {"is_safe": True, "exists": True, "resolved_path": path})())
    original = Path.read_bytes
    observations = []

    def observed(target):
        raw = original(target)
        if target == path:
            observations.append(raw)
        return raw

    monkeypatch.setattr(Path, "read_bytes", observed)
    validation = paper_review.validate_reviewed_artifact_content(result)
    assert validation["valid"] and validation["artifact_bytes_integrity"] == "VERIFIED"
    assert len(observations) == 1


def test_old_extraction_receipt_survives_latest_review_replaced_without_provenance(tmp_path, monkeypatch):
    from app.db import bootstrap
    from app.repositories import ai_review_drafts, paper_reviews
    from app.services import ai_review
    from tests.test_ai_review_provider import _review

    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    evidence, _, _ = _read(tmp_path, monkeypatch)
    source = {**_review(1), **evidence}
    stored = paper_reviews.upsert_review(source)
    packet = ai_review._packet_from_review(stored, topic_id=source["topic_id"], topic_name="Synthetic", max_chars=200)
    draft = dict(topic_id=source["topic_id"], run_id=source["run_id"], paper_id=source["paper_id"], source_text_sha256=source["source_text_sha256"], review_level="full_text_grounded", status="accepted", draft={"summary": "Synthetic result"}, validation={"status": "accepted", "reason_codes": []})
    receipt = ai_review_drafts.upsert_draft(draft, input_packet=packet, input_origin="FIXTURE_SYNTHETIC")
    source.pop("extraction_provenance")
    assert "extraction_provenance" not in paper_reviews.upsert_review(source)
    old = ai_review_drafts.get_input_receipt(source["topic_id"], receipt["validation"]["input_receipt_id"])
    assert old["packet"] == packet
    assert old["packet"]["source_extraction"] == evidence["extraction_provenance"]


def test_packet_preserves_extraction_observation_without_claiming_full_review(tmp_path, monkeypatch):
    result, _, _ = _read(tmp_path, monkeypatch)
    result.update(run_id="run", paper_id="paper")
    packet = enrich_packet(result, {"packet_version": "ai-review-packet-v2", "paper_id": "paper", "source_text_sha256": result["source_text_sha256"], "selected_text_excerpts": [{"source": "text_excerpt", "text": result["text_excerpt"][:200]}], "evidence_spans": []})
    assert packet["source_extraction"] == result["extraction_provenance"]
    assert packet["input_scope"]["full_document_review"] == "NOT_ESTABLISHED"
    packet["source_extraction"]["selection"]["chars"] = 0
    with pytest.raises(ValueError, match="review_input_scope_invalid"):
        validate_packet(packet)


@pytest.mark.parametrize("damage", ["hash", "bool_count", "false_complete", "bad_limit", "unknown_available", "extra", "wrong_decode"])
def test_provenance_rejects_inconsistent_bindings(tmp_path, monkeypatch, damage):
    result, _, _ = _read(tmp_path, monkeypatch)
    value = result["extraction_provenance"]
    if damage == "hash":
        value["selection"]["sha256"] = "a" * 64
    elif damage == "bool_count":
        value["artifact"]["bytes"] = True
    elif damage == "false_complete":
        value["selection"]["truncation"] = "NOT_OBSERVED"
    elif damage == "bad_limit":
        value["selection"]["max_chars"] = 1
    elif damage == "unknown_available":
        value["selection"]["available_chars"] = None
    elif damage == "extra":
        value["complete_review"] = True
    else:
        value["decoding"] = "PDF_ENGINE"
    with pytest.raises(ValueError, match="review_extraction_provenance_invalid"):
        validate_provenance(value, text_sha256=result["source_text_sha256"], text_chars=result["text_char_count"])


def test_pdf_to_review_packet_preserves_two_distinct_limits_and_missing_pdf_fails(tmp_path, monkeypatch):
    fitz = pytest.importorskip("fitz")
    path = tmp_path / "paper.v1.pdf"
    with fitz.open() as document:
        for _ in range(3):
            document.new_page().insert_text((72, 72), "Synthetic local evidence for extraction.\n" * 25)
        document.save(path)
    assert pdf_acquisition.extract_pdf_to_text_artifacts(pdf_path=path, paper_id="paper.v1", max_text_chars=500)["status"] == "extracted"
    paths = {"pdf": str(path), "text_txt": str(path.with_suffix(".txt")), "pages_json": str(path.with_suffix(".pages.json"))}
    monkeypatch.setattr(paper_review.legacy_reviews, "resolve_paper_artifacts", lambda *_: paths)
    result = paper_review.read_local_full_text_evidence(run_id="run", paper_id="paper.v1", max_text_chars=200)
    assert result["extraction_provenance"]["selection"]["max_chars"] == 200
    pdf = result["extraction_provenance"]["pdf_extraction"]
    assert pdf["max_chars"] == 500 and pdf["document_page_count"] == 3
    assert pdf["visited_page_numbers"] == [1] and pdf["truncation"] == "TRUNCATED"
    result.update(run_id="run", paper_id="paper.v1")
    packet = enrich_packet(result, {"packet_version": "ai-review-packet-v2", "paper_id": "paper.v1", "source_text_sha256": result["source_text_sha256"], "evidence_spans": [], "selected_text_excerpts": []})
    assert packet["source_extraction"]["pdf_extraction"] == pdf
    path.unlink()
    paths["pdf"] = None
    with pytest.raises(ValueError, match="extraction_provenance_invalid"):
        paper_review.read_local_full_text_evidence(run_id="run", paper_id="paper.v1")


def test_direct_pdf_records_byte_identity_without_claiming_extraction_completeness(tmp_path, monkeypatch):
    fitz = pytest.importorskip("fitz")
    path = tmp_path / "direct.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "Synthetic direct PDF text.\n" * 25)
        document.save(path)
    monkeypatch.setattr(paper_review.legacy_reviews, "resolve_paper_artifacts", lambda *_: {"pdf": str(path)})
    result = paper_review.read_local_full_text_evidence(run_id="run", paper_id="direct", extract_existing_pdf=True, max_text_chars=200)
    provenance = result["extraction_provenance"]
    assert provenance["artifact"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert provenance["selection"]["truncation"] == "UNKNOWN"
    assert provenance["pdf_extraction"] is None
    assert result["text_excerpt"] == result["text_excerpt"].strip()


@pytest.mark.parametrize("damage", ["empty_and_broken", "missing_pair"])
def test_marked_extraction_cannot_fallback_to_pdf_after_pair_damage(tmp_path, monkeypatch, damage):
    fitz = pytest.importorskip("fitz")
    path = tmp_path / "paper.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "Synthetic original for no fallback.\n" * 25)
        document.save(path)
    assert pdf_acquisition.extract_pdf_to_text_artifacts(pdf_path=path, paper_id="paper")["status"] == "extracted"
    text, pages = path.with_suffix(".txt"), path.with_suffix(".pages.json")
    if damage == "missing_pair":
        text.unlink()
        pages.unlink()
    else:
        text.write_bytes(b"")
        pages.write_bytes(b"invalid json")
    monkeypatch.setattr(paper_review.legacy_reviews, "resolve_paper_artifacts", lambda *_: {
        "pdf": str(path), "text_txt": str(text) if text.exists() else None,
        "pages_json": str(pages) if pages.exists() else None,
    })
    with pytest.raises(ValueError, match="extraction_provenance_invalid"):
        paper_review.read_local_full_text_evidence(run_id="run", paper_id="paper", extract_existing_pdf=True)
