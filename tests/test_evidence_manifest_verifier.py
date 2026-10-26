from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.cli import evidence_manifest
from app.db import bootstrap
from app.repositories import artifact_refs, paper_reviews
from app.services import evidence_manifest_verifier


MANIFEST_PATH = Path("data/evidence_manifests/m19.3-large-language-models-2604.26498v1.json")


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _manifest(**overrides):
    payload = {
        "schema_version": 1,
        "manifest_kind": "paper_full_text_evidence_replay",
        "topic_id": "large-language-models",
        "paper_id": "paper-1",
        "title": "Manifest Test Paper",
        "pdf_url": "https://arxiv.org/pdf/paper-1",
        "pdf_domain": "arxiv.org",
        "pdf_sha256": "0" * 64,
        "source_text_sha256": _hash_text("Full text evidence for manifest verifier."),
        "artifact_ref": "run:raw/paper-1.txt",
        "pages_artifact_ref": "run:raw/paper-1.pages.json",
        "pdf_artifact_ref": "run:raw/paper-1.pdf",
        "extraction": {
            "page_count": 1,
            "extracted_chars": 42,
            "extractor": "test",
        },
        "review": {
            "expected_status": "reviewed",
            "evidence_span_count": 1,
            "evidence_span_metadata": [
                {
                    "index": 1,
                    "label": "full_text_excerpt_1",
                    "char_start": 0,
                    "char_end": 20,
                    "text_sha256": _hash_text("span evidence"),
                }
            ],
        },
        "acquisition": {
            "command_provenance": "python -m app.cli.review_refresh --topic large-language-models --fetch-missing-pdf",
            "max_pdf_bytes": 25000000,
            "download_limit": 1,
            "allow_domain": "arxiv.org",
            "timeout_seconds": 30,
            "operator_gate_required": True,
        },
        "known_exclusions": [{"paper_id": "oversized-1", "reason": "file_too_large"}],
        "coverage": {
            "before_full_text_reviewed_unique_count": 27,
            "after_full_text_reviewed_unique_count": 28,
            "topic_relevant_unique_count": 524,
        },
        "portability": {
            "do_not_commit_pdf": True,
            "do_not_commit_full_text": True,
            "replay_requires_bounded_network": True,
        },
        "created_from_commit": "test",
        "notes": ["test manifest"],
    }
    payload.update(overrides)
    return payload


def _write_manifest(tmp_path: Path, payload: dict | None = None) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload or _manifest(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _patch_storage(tmp_path: Path, monkeypatch) -> Path:
    storage_root = tmp_path / "storage" / "runs"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(artifact_refs.constants, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(artifact_refs.constants, "STORAGE_ROOT", storage_root)
    return storage_root


def _seed_review(tmp_path: Path, monkeypatch, *, artifact_text: str | None = None, **review_overrides) -> Path:
    storage_root = _patch_storage(tmp_path, monkeypatch)
    artifact = storage_root / "run-1" / "raw" / "paper-1.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    text = artifact_text if artifact_text is not None else "Full text evidence for manifest verifier."
    artifact.write_text(text, encoding="utf-8")
    paper_reviews.upsert_review(
        {
            "topic_id": "large-language-models",
            "run_id": "run-1",
            "paper_id": "paper-1",
            "title": "Manifest Test Paper",
            "source_provider": "arxiv",
            "abstract_text": "Abstract.",
            "full_text_status": "reviewed",
            "relevance_score": 90,
            "novelty_score": 60,
            "evidence_strength": 70,
            "method_summary": "Method.",
            "key_findings": ["Finding."],
            "limitations": ["Limitation."],
            "why_it_matters": "Why.",
            "topic_fit_rationale": "Fit.",
            "review_model": "deterministic-test",
            "reviewer": "test",
            "reviewed_at": "2026-05-19T00:00:00+00:00",
            "source_text_sha256": _hash_text(text),
            "full_text_artifact_path": str(artifact),
            "text_char_count": len(text),
            "evidence_spans": [
                {
                    "label": "full_text_excerpt_1",
                    "char_start": 0,
                    "char_end": 20,
                    "text": "span evidence",
                }
            ],
            "review_source_type": "full_text",
            "full_text_reviewed_at": "2026-05-19T00:00:00+00:00",
            **review_overrides,
        }
    )
    return artifact


def test_valid_manifest_loads() -> None:
    manifest = evidence_manifest_verifier.load_manifest(MANIFEST_PATH)

    assert manifest["paper_id"] == "2604.26498v1"
    assert manifest["source_text_sha256"] == "306a9924fc26625edf05e89028cdb000a5a14c1ba9b796ac551ba7c149679049"


def test_missing_manifest_returns_missing_manifest(tmp_path: Path) -> None:
    result = evidence_manifest_verifier.verify_manifest(tmp_path / "missing.json")

    assert result["status"] == "missing_manifest"
    assert result["safe_to_claim_local_verified"] is False


def test_invalid_schema_returns_invalid_manifest(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, {"schema_version": 1, "manifest_kind": "wrong"})

    result = evidence_manifest_verifier.verify_manifest(path)

    assert result["status"] == "invalid_manifest"
    assert result["safe_to_claim_local_verified"] is False


def test_manifest_rejects_full_text_or_pdf_payload_fields(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, _manifest(full_text="forbidden", pdf_bytes="forbidden"))

    result = evidence_manifest_verifier.verify_manifest(path)

    assert result["status"] == "invalid_manifest"
    assert "forbidden_manifest_field:full_text" in result["reasons"]
    assert "forbidden_manifest_field:pdf_bytes" in result["reasons"]


def test_matching_local_review_and_artifact_returns_verified(tmp_path: Path, monkeypatch) -> None:
    _seed_review(tmp_path, monkeypatch)
    path = _write_manifest(tmp_path)

    result = evidence_manifest_verifier.verify_manifest(path)

    assert result["status"] == "verified"
    assert result["local_status"] == "reviewed"
    assert result["local_source_text_sha256"] == result["expected_source_text_sha256"]
    assert result["local_evidence_span_count"] == 1
    assert result["artifact_resolved"] is True
    assert result["replay_required"] is False
    assert result["safe_to_claim_local_verified"] is True
    assert result["safe_to_claim_clean_clone_reproducible"] is False


def test_missing_review_row_returns_replay_required(tmp_path: Path, monkeypatch) -> None:
    _patch_storage(tmp_path, monkeypatch)
    path = _write_manifest(tmp_path)

    result = evidence_manifest_verifier.verify_manifest(path)

    assert result["status"] == "missing_review_row"
    assert result["replay_required"] is True


def test_status_mismatch_returns_status_mismatch(tmp_path: Path, monkeypatch) -> None:
    _seed_review(tmp_path, monkeypatch, full_text_status="abstract_only")
    path = _write_manifest(tmp_path)

    result = evidence_manifest_verifier.verify_manifest(path)

    assert result["status"] == "status_mismatch"


def test_source_hash_mismatch_returns_hash_mismatch(tmp_path: Path, monkeypatch) -> None:
    _seed_review(tmp_path, monkeypatch, source_text_sha256="bad")
    path = _write_manifest(tmp_path)

    result = evidence_manifest_verifier.verify_manifest(path)

    assert result["status"] == "hash_mismatch"


def test_missing_evidence_spans_returns_missing_evidence_spans(tmp_path: Path, monkeypatch) -> None:
    _seed_review(tmp_path, monkeypatch, evidence_spans=[])
    path = _write_manifest(tmp_path)

    result = evidence_manifest_verifier.verify_manifest(path)

    assert result["status"] == "missing_evidence_spans"


def test_missing_artifact_returns_missing_artifact(tmp_path: Path, monkeypatch) -> None:
    artifact = _seed_review(tmp_path, monkeypatch)
    artifact.unlink()
    path = _write_manifest(tmp_path)

    result = evidence_manifest_verifier.verify_manifest(path)

    assert result["status"] == "missing_artifact"


def test_artifact_text_hash_mismatch_returns_artifact_hash_mismatch(tmp_path: Path, monkeypatch) -> None:
    artifact = _seed_review(tmp_path, monkeypatch)
    artifact.write_text("changed artifact text", encoding="utf-8")
    path = _write_manifest(tmp_path)

    result = evidence_manifest_verifier.verify_manifest(path)

    assert result["status"] == "artifact_hash_mismatch"


def test_pdf_hash_mismatch_returns_pdf_hash_mismatch(tmp_path: Path, monkeypatch) -> None:
    _seed_review(tmp_path, monkeypatch)
    pdf_path = tmp_path / "storage" / "runs" / "run-1" / "raw" / "paper-1.pdf"
    pdf_path.write_bytes(b"not matching pdf")
    path = _write_manifest(tmp_path)

    result = evidence_manifest_verifier.verify_manifest(path)

    assert result["status"] == "pdf_hash_mismatch"


def test_cli_verify_local_outputs_json(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_review(tmp_path, monkeypatch)
    path = _write_manifest(tmp_path)

    exit_code = evidence_manifest.main(["--manifest", str(path), "--verify-local", "--json"])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["status"] == "verified"
    assert output["safe_to_claim_clean_clone_reproducible"] is False


def test_cli_plan_replay_prints_bounded_command_without_network(tmp_path: Path, capsys) -> None:
    path = _write_manifest(tmp_path)

    exit_code = evidence_manifest.main(["--manifest", str(path), "--plan-replay"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "--fetch-missing-pdf" in output
    assert "--allow-bounded-pdf-fetch" in output
    assert "requires ALLOW_BOUNDED_PDF_FETCH=1" in output
    assert "does not run network" in output


def test_cli_verify_does_not_mutate_temp_db_or_artifact(tmp_path: Path, monkeypatch, capsys) -> None:
    artifact = _seed_review(tmp_path, monkeypatch)
    path = _write_manifest(tmp_path)
    db_path = bootstrap.metadata_index_path()
    before_db = db_path.read_bytes()
    before_artifact = artifact.read_bytes()

    exit_code = evidence_manifest.main(["--manifest", str(path), "--verify-local", "--json"])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "verified"
    assert db_path.read_bytes() == before_db
    assert artifact.read_bytes() == before_artifact


def test_cli_does_not_require_bounded_fetch_env(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("ALLOW_BOUNDED_PDF_FETCH", raising=False)
    _seed_review(tmp_path, monkeypatch)
    path = _write_manifest(tmp_path)

    exit_code = evidence_manifest.main(["--manifest", str(path), "--verify-local", "--json"])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "verified"
