"""Legacy normalized-prefix signatures do not certify an entire source file."""
from copy import deepcopy
import json

import pytest

from app.domain import constants
from app.services import paper_review


@pytest.fixture
def artifact(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    monkeypatch.setattr(constants, "STORAGE_ROOT", root)
    raw = root / "source-run" / "raw"
    raw.mkdir(parents=True)
    return raw


def legacy_review(artifact, kind="text", *, max_chars=24000):
    # Avoid ending the stored prefix on whitespace: existing repository writes
    # strip it, and inconsistent legacy metadata must not be guessed/repaired.
    text = "A" * 13000 + " " + "B" * 13000 + " " + "C" * 110000
    if kind == "text":
        path = artifact / "paper.txt"
        path.write_text("\n\n" + text + "\n\n", encoding="utf-8")
        captured = paper_review._read_text_file(path, max_chars)
    else:
        path = artifact / "paper.pages.json"
        path.write_text(json.dumps({"pages": [
            {"page": 1, "text": "A" * 13000}, {"page": 2, "text": "B" * 13000},
            {"page": 3, "text": "C" * 110000},
        ]}), encoding="utf-8")
        captured = paper_review._read_pages_file(path, max_chars)
    assert captured is not None
    review = dict(run_id="source-run", paper_id="paper", full_text_status="reviewed",
                  full_text_artifact_path="run:raw/" + path.name,
                  source_text_sha256=paper_review._sha256_text(captured["text"]),
                  text_excerpt=captured["text"], text_char_count=len(captured["text"]), evidence_spans=captured["spans"])
    return path, review


@pytest.mark.parametrize("kind", ("text", "pages"))
def test_unchanged_long_artifact_validates_exact_recorded_prefix(artifact, kind):
    _, review = legacy_review(artifact, kind)
    result = paper_review.validate_reviewed_artifact_content(review)
    assert result["valid"] is True
    assert result["validated_scope"] == "normalized_prefix"
    assert result["validated_char_count"] == review["text_char_count"] == 24000
    assert result["actual_source_text_sha256"] == review["source_text_sha256"]
    assert result["complete_document_integrity"] == "NOT_ESTABLISHED"


@pytest.mark.parametrize("kind", ("text", "pages"))
def test_store_diagnostic_uses_same_preserved_prefix_contract(artifact, monkeypatch, kind):
    _, review = legacy_review(artifact, kind)
    monkeypatch.setattr(paper_review.topic_profiles, "get_topic_profile", lambda _: {"profile_id": "topic"})
    monkeypatch.setattr(paper_review.paper_reviews, "list_reviews", lambda **_: [review])
    result = paper_review.validate_evidence_store_for_topic("topic", include_repair_plan=True)
    assert result["valid_count"] == 1 and result["error_count"] == 0
    assert result["issues"] == []


@pytest.mark.parametrize("kind", ("text", "pages"))
def test_changed_suffix_is_not_claimed_to_have_original_file_integrity(artifact, kind):
    path, review = legacy_review(artifact, kind)
    path.write_text(path.read_text(encoding="utf-8").replace("C" * 110000, "Different unrecorded suffix"), encoding="utf-8")
    result = paper_review.validate_reviewed_artifact_content(review)
    assert result["valid"] is True
    assert result["validated_scope"] == "normalized_prefix"
    assert result["complete_document_integrity"] == "NOT_ESTABLISHED"


@pytest.mark.parametrize("kind", ("text", "pages"))
def test_changed_prefix_fails(artifact, kind):
    path, review = legacy_review(artifact, kind)
    path.write_text(path.read_text(encoding="utf-8").replace("A" * 13000, "X" * 13000), encoding="utf-8")
    result = paper_review.validate_reviewed_artifact_content(review)
    assert result["valid"] is False and result["reason"] == "sha256_mismatch"


@pytest.mark.parametrize("damage", ("missing", "bad_hash", "bad_length", "text_changed", "bool_length", "short_prefix"))
def test_prefix_fallback_requires_consistent_preserved_text_hash_and_length(artifact, damage):
    _, review = legacy_review(artifact)
    if damage == "missing":
        review.pop("text_excerpt")
    elif damage == "bad_hash":
        review["source_text_sha256"] = "a" * 64
    elif damage == "bad_length":
        review["text_char_count"] += 1
    elif damage == "text_changed":
        review["text_excerpt"] += "altered"
    elif damage == "bool_length":
        review["text_char_count"] = True
    else:
        review["text_excerpt"] = review["text_excerpt"][:20]
        review["text_char_count"] = 20
        review["source_text_sha256"] = paper_review._sha256_text(review["text_excerpt"])
    original = deepcopy(review)
    assert paper_review.validate_reviewed_artifact_content(review)["valid"] is False
    assert review == original


@pytest.mark.parametrize("damage,reason", (("missing", "missing_artifact_file"), ("short", "too_short_extracted_text"),
                                           ("truncated", "sha256_mismatch"), ("empty", "empty_extracted_text")))
def test_missing_short_empty_or_truncated_current_file_fails(artifact, damage, reason):
    path, review = legacy_review(artifact)
    if damage == "missing":
        path.unlink()
    else:
        text = "" if damage == "empty" else "too short" if damage == "short" else review["text_excerpt"][:2000]
        path.write_text(text, encoding="utf-8")
    result = paper_review.validate_reviewed_artifact_content(review)
    assert result["valid"] is False and result["reason"] == reason


@pytest.mark.parametrize("kind", ("text", "pages"))
def test_legacy_exact_normalized_artifact_hash_remains_supported(artifact, kind):
    # Explicit caller cap preserves more than 100k; revalidation must not silently
    # impose the old unrelated 100k limit on pages JSON.
    _, review = legacy_review(artifact, kind, max_chars=300000)
    review.pop("text_excerpt")
    review.pop("text_char_count")
    result = paper_review.validate_reviewed_artifact_content(review)
    assert result["valid"] is True
    assert result["validated_scope"] == "normalized_artifact"
    assert result["complete_document_integrity"] == "NOT_ESTABLISHED"


def test_malformed_pages_remains_invalid(artifact):
    path, review = legacy_review(artifact, "pages")
    path.write_text('{"pages": invalid}', encoding="utf-8")
    result = paper_review.validate_reviewed_artifact_content(review)
    assert result["valid"] is False and result["reason"] == "empty_extracted_text"
