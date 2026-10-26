"""Prepared-selection input packets; never a claim of whole-document review.

The binding contains an opaque digest of a canonical server-loaded preparation.
This pure validator checks internal consistency, not database provenance. The
caller must bind the digest to its authorized immutable preparation before use.
"""
from copy import deepcopy
from datetime import datetime
import re
from typing import Any

from app.domain import review_preparation as preparation
from app.domain.journal_exchange import preserved_json

PACKET_VERSION = "ai-review-packet-v3"
CONTEXT_PACKET_VERSION = "ai-review-packet-v4"
DOCUMENT_PACKET_VERSION = "ai-review-packet-v5"
PACKET_VERSIONS = {PACKET_VERSION, CONTEXT_PACKET_VERSION, DOCUMENT_PACKET_VERSION}
SCOPE_VERSION = "AI_REVIEW_INPUT_SCOPE_V2"
BINDING_VERSION = "REVIEW_PREPARATION_BINDING_V1"
CONTEXT_BINDING_VERSION = "REVIEW_PREPARATION_BINDING_V2"
DOCUMENT_BINDING_VERSION = "REVIEW_PREPARATION_BINDING_V3"
COORDINATES = "PREPARED_SELECTION_CODEPOINTS"
ERROR = "review_input_scope_invalid"
_HASH = re.compile(r"[a-f0-9]{64}")
_ID = re.compile(r"rprep_[a-f0-9]{64}")


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _hash(value: Any) -> bool:
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def _binding(value: Any, text: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(ERROR)
    document = value.get("schema_version") == DOCUMENT_BINDING_VERSION
    contextual = value.get("schema_version") in {CONTEXT_BINDING_VERSION, DOCUMENT_BINDING_VERSION}
    if set(value) != {
        "schema_version", "preparation_id", "preparation_sha256", "topic_id", "paper_version_id",
        "source", "selection", "normalization", "decoding",
    } | ({"context"} if contextual else set()) | ({"analysis_unit"} if document else set()):
        raise ValueError(ERROR)
    if contextual:
        from app.domain.review_context import validate as validate_context
        validate_context(value["context"])
    if (value["schema_version"] not in {BINDING_VERSION, CONTEXT_BINDING_VERSION, DOCUMENT_BINDING_VERSION}
            or not isinstance(value["preparation_id"], str)
            or _ID.fullmatch(value["preparation_id"]) is None or not _hash(value["preparation_sha256"])
            or not _text(value["topic_id"]) or not _text(value["paper_version_id"])
            or value["normalization"] != preparation.NORMALIZATION):
        raise ValueError(ERROR)
    source, selection = value["source"], value["selection"]
    if not isinstance(source, dict) or set(source) != {
        "run_id", "paper_id", "source_digest", "source_chars", "source_kind", "artifact_sha256",
        "artifact_bytes", "artifact_ref", "extraction_truncation",
    }:
        raise ValueError(ERROR)
    if (not _text(source["run_id"]) or not _text(source["paper_id"])
            or source["source_kind"] not in {"text", "pages"}
            or not _hash(source["artifact_sha256"]) or not _hash(source["source_digest"])
            or source["source_digest"] != preparation.source_digest(source)
            or type(source["source_chars"]) is not int or not 1 <= source["source_chars"] <= preparation.MAX_SOURCE_CHARS
            or type(source["artifact_bytes"]) is not int or not 1 <= source["artifact_bytes"] <= preparation.MAX_ARTIFACT_BYTES
            or not isinstance(source["artifact_ref"], str) or not source["artifact_ref"].startswith("run:")
            or source["extraction_truncation"] not in {"UNKNOWN", "TRUNCATED", "NOT_OBSERVED"}
            or value["decoding"] != ("UTF8_SIG_STRICT" if source["source_kind"] == "pages" else "UTF8_STRICT")):
        raise ValueError(ERROR)
    if not isinstance(selection, dict) or set(selection) != {"start", "end", "chars", "sha256"}:
        raise ValueError(ERROR)
    if (not isinstance(text, str) or type(selection["start"]) is not int or type(selection["end"]) is not int
            or type(selection["chars"]) is not int or not 0 <= selection["start"] < selection["end"] <= source["source_chars"]
            or not 1 <= selection["chars"] == selection["end"] - selection["start"] == len(text)
               <= (preparation.MAX_SOURCE_CHARS if document else preparation.MAX_SELECTED_CHARS)
            or selection["sha256"] != preparation.sha(text)):
        raise ValueError(ERROR)
    if document:
        from app.domain.review_token_budget import validate_analysis_unit
        validate_analysis_unit(value["analysis_unit"], document_chars=source["source_chars"])
    preserved_json(value).encode("utf-8", errors="strict")
    return deepcopy(value)


def _render(binding: dict[str, Any], text: str) -> dict[str, Any]:
    source, selection = binding["source"], binding["selection"]
    size, text_hash = selection["chars"], selection["sha256"]
    packet = dict(
        packet_version=PACKET_VERSION, topic_id=binding["topic_id"], topic_name=binding["topic_id"],
        paper_id=source["paper_id"], title=source["paper_id"], authors=[], published_at=None,
        relevance_score=None, abstract=None, full_text_status="prepared_range", review_level="full_text_grounded",
        source_text_sha256=text_hash, selected_text_excerpts=[],
        evidence_spans=[dict(span_id="prepared-range-" + binding["preparation_id"], label="Prepared local text range",
                            text=text, source_path=source["artifact_ref"], page_number=None, char_start=0, char_end=size)],
        extraction_quality=dict(text_char_count=size, evidence_span_count=1, review_source_type="prepared_local_range",
                                full_text_artifact_path=source["artifact_ref"]),
        existing_review={}, citation_metadata=dict(full_text_artifact_path=source["artifact_ref"]),
        source_preparation=deepcopy(binding),
        provider_output_contract=dict(
            contract_version="ai-review-provider-output-v1", accepted_output_formats=["json", "jsonl"],
            validation_gate="m4_grounded_validation", required_preserved_fields=["paper_id", "source_text_sha256"],
            required_output_fields=["paper_id", "source_text_sha256", "contribution_summary", "method_summary",
                                    "evidence_summary", "limitations", "relevance_to_topic", "novelty_signal",
                                    "practical_implications", "cited_evidence_span_ids", "confidence", "review_level"],
            full_text_grounded_requires=["matching source_text_sha256", "at least one cited evidence span ID"],
            review_level="full_text_grounded",
            instructions=[
                "Return one AI review draft object as JSON. Preserve paper_id and source_text_sha256 exactly.",
                "This packet contains only an explicitly selected local excerpt, not the whole paper.",
                "Evidence span offsets are local to the preserved selection; source_preparation records the original artifact range.",
                "Cite only the provided evidence span IDs. Do not invent citations or unsupported claims.",
                "A valid structured response does not establish scientific support or full-document review.",
            ],
        ),
    )
    if binding["schema_version"] in {CONTEXT_BINDING_VERSION, DOCUMENT_BINDING_VERSION}:
        context = binding["context"]
        packet.update(packet_version=CONTEXT_PACKET_VERSION, topic_name=context["topic_name"],
                      title=context["paper_title"], authors=deepcopy(context["authors"]), published_at=context["published_at"])
        packet["provider_output_contract"]["instructions"].append(
            "Saved topic and catalog metadata provide research context, not additional reviewed body or scientific evidence.")
    if binding["schema_version"] == DOCUMENT_BINDING_VERSION:
        packet["packet_version"] = DOCUMENT_PACKET_VERSION
        packet["provider_output_contract"]["instructions"][1] = (
            "The evidence span is the selected analysis target: whole extracted text or one planned section. "
            "Extraction and a structured result do not establish complete PDF review or scientific support.")
        packet["provider_output_contract"]["instructions"].append(
            "analysis_unit records the immutable plan, local token budget, outline and neighboring context. "
            "Outline labels and context_before/context_after assist interpretation only: do not cite them, "
            "classify them as reviewed target text or count them as reviewed coverage.")
    packet["input_scope"] = dict(
        schema_version=SCOPE_VERSION,
        source=dict(run_id=source["run_id"], paper_id=source["paper_id"], recorded_source_sha256=text_hash,
                    preserved_text_sha256=text_hash, preserved_text_chars=size, document_chars=None,
                    coordinate_space=COORDINATES),
        components=[dict(json_pointer="/evidence_spans/0/text", text_sha256=text_hash, chars=size,
                         source_start=0, source_end=size, mapping="exact")],
        covered_stored_chars=size, document_coverage="UNKNOWN", full_document_review="NOT_ESTABLISHED",
        packet_payload_sha256=preparation.sha(preserved_json(packet)),
    )
    return packet


def build_packet(record: dict[str, Any]) -> dict[str, Any]:
    """Build only from a canonical server-loaded preparation; no current file IO."""
    try:
        if not isinstance(record, dict):
            raise ValueError(ERROR)
        document = record.get("schema_version") == preparation.DOCUMENT_SCHEMA
        contextual = record.get("schema_version") in {preparation.CONTEXT_SCHEMA, preparation.DOCUMENT_SCHEMA}
        if set(record) != {
            "schema_version", "preparation_id", "topic_id", "paper_version_id", "actor_id", "created_at",
            "state", "source", "selection", "execution",
        } | ({"context"} if contextual else set()) | ({"analysis_unit"} if document else set()):
            raise ValueError(ERROR)
        if (record["schema_version"] not in {preparation.SCHEMA, preparation.CONTEXT_SCHEMA, preparation.DOCUMENT_SCHEMA}
                or record["state"] != "PREPARED_NOT_EXECUTED"
                or not _text(record["actor_id"]) or not _text(record["created_at"])
                or datetime.fromisoformat(record["created_at"]).tzinfo is None
                or record["execution"] != {"started": False, "full_document_review": "NOT_ESTABLISHED"}
                or record["execution"]["started"] is not False
                or not isinstance(record["selection"], dict)
                or set(record["selection"]) != {"start", "end", "chars", "sha256", "text"}):
            raise ValueError(ERROR)
        text = record["selection"]["text"]
        binding = _binding(dict(
            schema_version=DOCUMENT_BINDING_VERSION if document else CONTEXT_BINDING_VERSION if contextual else BINDING_VERSION,
            preparation_id=record["preparation_id"],
            preparation_sha256=preparation.sha(preserved_json(record)), topic_id=record["topic_id"],
            paper_version_id=record["paper_version_id"], source=record["source"],
            selection={key: value for key, value in record["selection"].items() if key != "text"},
            normalization=preparation.NORMALIZATION,
            decoding="UTF8_SIG_STRICT" if record["source"]["source_kind"] == "pages" else "UTF8_STRICT",
            **({"context": record["context"]} if contextual else {}),
            **({"analysis_unit": record["analysis_unit"]} if document else {}),
        ), text)
        return _render(binding, text)
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        raise ValueError(ERROR) from None


def validate_packet(packet: dict[str, Any]) -> dict[str, Any]:
    """Check exact V3/V4/V5 semantics; the caller establishes DB provenance."""
    try:
        spans = packet["evidence_spans"]
        if not isinstance(spans, list) or len(spans) != 1 or not isinstance(spans[0], dict):
            raise ValueError(ERROR)
        text = spans[0]["text"]
        binding = _binding(packet["source_preparation"], text)
        expected = _render(binding, text)
        if preserved_json(packet) != preserved_json(expected):
            raise ValueError(ERROR)
        return deepcopy(expected["input_scope"])
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        raise ValueError(ERROR) from None
