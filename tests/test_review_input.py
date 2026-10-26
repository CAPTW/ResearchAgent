"""Offline review packet scope: stored text coverage is not whole-paper review."""

from copy import deepcopy
import hashlib

import pytest

from app.domain.journal_exchange import preserved_json
from app.domain.review_input import enrich_packet, validate_packet


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def inputs(text="ABCDE ABCDE"):
    review = dict(
        run_id="source-run",
        paper_id="paper-v1",
        source_text_sha256="legacy-source-hash",
        text_excerpt=text,
        evidence_spans=[dict(label="second", text="ABCDE", char_start=6, char_end=11)],
    )
    packet = dict(
        packet_version="ai-review-packet-v2",
        topic_id="topic",
        paper_id="paper-v1",
        source_text_sha256="legacy-source-hash",
        selected_text_excerpts=[dict(source="text_excerpt", text=text[:7], char_count=7)],
        evidence_spans=[dict(span_id="second", text="ABCDE", char_start=6, char_end=11)],
    )
    return review, packet


def test_scope_unions_overlapping_prefix_and_declared_span_without_claiming_document_coverage():
    review, packet = inputs()
    saved = enrich_packet(review, packet)
    scope = validate_packet(saved)
    assert packet.get("input_scope") is None
    assert scope["source"] == dict(
        run_id="source-run",
        paper_id="paper-v1",
        recorded_source_sha256="legacy-source-hash",
        preserved_text_sha256=sha(review["text_excerpt"]),
        preserved_text_chars=11,
        document_chars=None,
    )
    assert scope["covered_stored_chars"] == 11
    assert scope["document_coverage"] == "UNKNOWN" and scope["full_document_review"] == "NOT_ESTABLISHED"
    assert scope["packet_payload_sha256"] == sha(preserved_json(packet))
    assert scope["components"][0] == dict(
        json_pointer="/selected_text_excerpts/0/text",
        text_sha256=sha("ABCDE A"),
        chars=7,
        source_start=0,
        source_end=7,
        mapping="exact",
    )
    assert scope["components"][1]["source_start"] == 6


def test_unicode_is_preserved_and_character_counts_are_not_bytes():
    text = "e\u0301 한글🙂"
    review, packet = inputs(text)
    packet["selected_text_excerpts"] = [dict(source="text_excerpt", text=text, char_count=len(text))]
    packet["evidence_spans"] = []
    saved = enrich_packet(review, packet)
    scope = validate_packet(saved)
    assert saved["selected_text_excerpts"][0]["text"] == text
    assert scope["covered_stored_chars"] == len(text) < len(text.encode("utf-8"))
    assert scope["source"]["preserved_text_sha256"] != sha(text.replace("e\u0301", "é"))


@pytest.mark.parametrize(
    "span",
    [
        dict(span_id="missing", text="ABCDE"),
        dict(span_id="wrong", text="wrong", char_start=6, char_end=11),
        dict(span_id="bool", text="A", char_start=False, char_end=1),
    ],
)
def test_unlocated_mismatched_or_ambiguous_span_is_unknown_without_substring_search(span):
    review, packet = inputs()
    packet["selected_text_excerpts"] = []
    packet["evidence_spans"] = [span]
    scope = validate_packet(enrich_packet(review, packet))
    assert scope["covered_stored_chars"] == 0
    assert scope["components"][0]["mapping"] == "unknown"
    assert scope["components"][0]["source_start"] is None and scope["components"][0]["source_end"] is None


def test_partial_prefix_and_abstract_are_distinguished_from_complete_document():
    review, packet = inputs()
    packet["evidence_spans"] = []
    packet["selected_text_excerpts"].append(dict(source="abstract", text="ABCDE", char_count=5))
    scope = validate_packet(enrich_packet(review, packet))
    assert scope["covered_stored_chars"] == 7
    assert scope["components"][1]["mapping"] == "unknown"
    assert scope["source"]["document_chars"] is None


def test_selected_evidence_excerpt_uses_only_verified_declared_span_prefix():
    review, packet = inputs()
    packet["selected_text_excerpts"] = [dict(source="evidence_span:second", text="ABC", char_count=3)]
    packet["evidence_spans"] = []
    scope = validate_packet(enrich_packet(review, packet))
    assert scope["covered_stored_chars"] == 3
    assert scope["components"][0]["source_start"] == 6


def test_duplicate_span_identifiers_do_not_select_an_arbitrary_source_range():
    review, packet = inputs()
    review["evidence_spans"].append(dict(label="second", text="ABCDE", char_start=0, char_end=5))
    packet["selected_text_excerpts"] = [dict(source="evidence_span:second", text="ABC", char_count=3)]
    packet["evidence_spans"] = []
    scope = validate_packet(enrich_packet(review, packet))
    assert scope["covered_stored_chars"] == 0
    assert scope["components"][0]["mapping"] == "unknown"


def test_already_normalized_selected_text_is_preserved_but_not_guessed_as_a_source_prefix():
    review, packet = inputs("A  B\nC")
    packet["selected_text_excerpts"] = [dict(source="text_excerpt", text="A B C", char_count=5)]
    packet["evidence_spans"] = []
    saved = enrich_packet(review, packet)
    assert saved["selected_text_excerpts"] == packet["selected_text_excerpts"]
    assert validate_packet(saved)["components"][0]["mapping"] == "unknown"


def test_enrichment_cannot_bind_a_different_recorded_source_identifier():
    review, packet = inputs()
    packet["source_text_sha256"] = "another-source"
    with pytest.raises(ValueError, match="review_input_scope_invalid"):
        enrich_packet(review, packet)


@pytest.mark.parametrize(
    "change",
    [
        "packet_text",
        "slot_hash",
        "slot_chars",
        "range",
        "union",
        "document",
        "review",
        "missing_slot",
        "extra_slot",
        "source_id",
        "source_hash",
        "version",
    ],
)
def test_tampered_packet_or_scope_is_rejected(change):
    review, packet = inputs()
    saved = enrich_packet(review, packet)
    scope = saved["input_scope"]
    if change == "packet_text":
        saved["evidence_spans"][0]["text"] = "tampered"
    elif change == "slot_hash":
        scope["components"][0]["text_sha256"] = "0" * 64
    elif change == "slot_chars":
        scope["components"][0]["chars"] = 4
    elif change == "range":
        scope["components"][0]["source_end"] = 12
    elif change == "union":
        scope["covered_stored_chars"] = 4
    elif change == "document":
        scope["source"]["document_chars"] = 11
    elif change == "review":
        scope["full_document_review"] = "COMPLETE"
    elif change == "missing_slot":
        scope["components"].pop()
    elif change == "extra_slot":
        scope["components"].append(deepcopy(scope["components"][0]))
    elif change == "source_id":
        scope["source"]["paper_id"] = "other"
    elif change == "source_hash":
        scope["source"]["recorded_source_sha256"] = "other"
    elif change == "version":
        saved["packet_version"] = "ai-review-packet-v1"
    with pytest.raises(ValueError, match="review_input_scope_invalid"):
        validate_packet(saved)


def test_legacy_packet_has_explicit_unknown_scope_without_fabricating_a_receipt():
    _, packet = inputs()
    packet["packet_version"] = "ai-review-packet-v1"
    before = deepcopy(packet)
    legacy = validate_packet(packet)
    assert legacy == dict(
        schema_version="LEGACY_REVIEW_INPUT_UNKNOWN",
        document_coverage="UNKNOWN",
        full_document_review="NOT_ESTABLISHED",
    )
    assert packet == before


def test_ai_packet_hook_keeps_max_chars_scoped_to_excerpts_and_uses_frozen_run(monkeypatch):
    from app.services import ai_review

    review, _ = inputs()
    review.update(
        title="Paper",
        full_text_status="reviewed",
        abstract_text="Large abstract outside selected excerpt limit",
        review_source_type="full_text",
    )
    packet = ai_review._packet_from_review(review, topic_id="topic", topic_name="Topic", max_chars=3)
    assert packet is not None and packet["packet_version"] == "ai-review-packet-v2"
    scope = validate_packet(packet)
    assert packet["selected_text_excerpts"][0]["text"] == "ABC"
    assert packet["evidence_spans"][0]["text"] == "ABCDE"
    assert sum(component["chars"] for component in scope["components"]) > 3
    monkeypatch.setattr(
        ai_review.paper_reviews, "list_reviews", lambda **_: pytest.fail("Do not rebind the saved source run")
    )
    assert ai_review._run_id_for_packet(packet, "topic") == "source-run"


@pytest.mark.parametrize("legacy", [False, True])
def test_manual_import_records_current_packet_without_claiming_actual_model_input(monkeypatch, legacy):
    from app.services import ai_review

    review, original = inputs()
    original["existing_review"] = {"run_id": "legacy-run"}
    packet = enrich_packet(review, original)
    if legacy:
        packet = original
        packet["packet_version"] = "ai-review-packet-v1"
    monkeypatch.setattr(
        ai_review, "build_ai_review_packets", lambda *_, **__: {"topic_id": "topic", "packets": [packet]}
    )
    monkeypatch.setattr(ai_review, "_drafts_from_path", lambda _: [{"paper_id": "paper-v1"}])
    monkeypatch.setattr(
        ai_review,
        "validate_ai_review_draft",
        lambda *_: {"status": "accepted", "normalized_draft": {"source_text_sha256": "legacy-source-hash"}},
    )
    calls = []
    monkeypatch.setattr(
        ai_review.ai_review_drafts, "upsert_draft", lambda record, **kwargs: calls.append((record, kwargs)) or record
    )
    result = ai_review.validate_and_import_ai_review_drafts("topic", "unused.json")
    assert result["stored_draft_count"] == 1
    assert calls[0][0]["run_id"] == ("legacy-run" if legacy else "source-run")
    assert calls[0][1] == ({} if legacy else {"input_packet": packet, "input_origin": "MANUAL_IMPORT_UNVERIFIED"})


def test_false_boolean_evidence_locator_cannot_be_promoted_to_exact_mapping():
    review, packet = inputs()
    packet["selected_text_excerpts"] = []
    packet["evidence_spans"] = [dict(text="A", char_start=False, char_end=1)]
    saved = enrich_packet(review, packet)
    component = saved["input_scope"]["components"][0]
    component.update(mapping="exact", source_start=0, source_end=1)
    saved["input_scope"]["covered_stored_chars"] = 1
    with pytest.raises(ValueError, match="review_input_scope_invalid"):
        validate_packet(saved)
