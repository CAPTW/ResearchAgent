"""Exact immutable review-input selections for a versioned Journal input.

Receipt binding establishes preserved input and output identity. It neither
certifies a model run nor turns the result into an original-source quotation.
"""
from copy import deepcopy
import hashlib
import json
import re
import sqlite3
from typing import Any

SCHEMA = "FROZEN_REVIEW_INPUT_V1"
ATTACHMENT_SCHEMA = "JOURNAL_REVIEW_INPUT_V1"
ERROR = "synthesis_review_input_invalid"
NOT_FOUND = "synthesis_review_input_not_found"
_POINTER = re.compile(r"^/(selected_text_excerpts|evidence_spans)/(0|[1-9][0-9]*)/text$")
_COMMON = ("paper_version_id", "canonical_paper_id", "title", "source_identity", "metadata_digest", "review_state", "rights")


def _encoded(value: Any) -> str:
    # No import of journal_exchange here: it imports local_journal, which calls
    # this module. Preserve exact Unicode rather than its NFC identity transform.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _selected(value: Any) -> list[str]:
    if (not isinstance(value, list) or not value
            or any(not isinstance(pointer, str) or _POINTER.fullmatch(pointer) is None for pointer in value)
            or len(set(value)) != len(value)):
        raise ValueError(ERROR)
    return sorted(value)


def _slot(packet: dict[str, Any], pointer: str) -> dict[str, Any]:
    match = _POINTER.fullmatch(pointer)
    if match is None:
        raise ValueError(ERROR)
    values = packet[match[1]]
    index = int(match[2])
    if not isinstance(values, list) or index >= len(values):
        raise ValueError(ERROR)
    slot = values[index]
    if not isinstance(slot, dict) or not isinstance(slot.get("text"), str) or not slot["text"].strip():
        raise ValueError(ERROR)
    return slot


def validate_frozen(value: Any) -> dict[str, Any]:
    """Recheck the full original receipt and exact selected component bindings."""
    # Lazy imports prevent local_journal -> receipt -> journal_exchange cycles.
    from app.repositories import ai_review_drafts as repository

    try:
        if (not isinstance(value, dict) or set(value) != {"schema_version", "receipt", "selected_component_ids"}
                or value["schema_version"] != SCHEMA):
            raise ValueError(ERROR)
        receipt = value["receipt"]
        if not isinstance(receipt, dict) or set(receipt) != {
            "schema_version", "receipt_id", "recorded_at", "input_origin", "topic_id", "run_id", "paper_id",
            "source_text_sha256", "review_level", "status", "packet", "draft", "validation",
        }:
            raise ValueError(ERROR)
        if (not isinstance(receipt["recorded_at"], str) or not receipt["recorded_at"].strip()
                or receipt["review_level"] not in repository.AI_REVIEW_LEVELS
                or receipt["status"] not in repository.AI_REVIEW_STATUSES):
            raise ValueError(ERROR)
        raw = {key: item for key, item in receipt.items() if key not in {"packet", "draft", "validation"}}
        raw.update(packet_json=_encoded(receipt["packet"]), draft_json=_encoded(receipt["draft"]),
                   validation_json=_encoded(receipt["validation"]))
        # The same decoder verifies both stored rows and this frozen copy. Its
        # returned complete shape also prevents unbound extra receipt metadata.
        if repository._row_to_receipt(raw) != receipt:
            raise ValueError(ERROR)
        selected = _selected(value["selected_component_ids"])
        if selected != value["selected_component_ids"]:
            raise ValueError(ERROR)
        packet = receipt["packet"]
        components = {component["json_pointer"]: component for component in packet["input_scope"]["components"]}
        for pointer in selected:
            if pointer not in components:
                raise ValueError(ERROR)
            slot, component = _slot(packet, pointer), components[pointer]
            if component["text_sha256"] != _sha(slot["text"]) or component["chars"] != len(slot["text"]):
                raise ValueError(ERROR)
        return deepcopy(value)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError):
        raise ValueError(ERROR) from None


def freeze(connection: sqlite3.Connection, topic_id: str, selection: Any,
           occurrences: list[tuple[str, str]]) -> dict[str, Any]:
    """Use already-authorized occurrence keys and the caller-owned transaction."""
    from app.repositories import ai_review_drafts as repository

    try:
        if (not isinstance(selection, dict) or set(selection) != {"receipt_id", "component_ids"}
                or not isinstance(selection["receipt_id"], str)
                or re.fullmatch(r"aireceipt_[a-f0-9]{64}", selection["receipt_id"]) is None):
            raise ValueError(ERROR)
        selected = _selected(selection["component_ids"])
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise ValueError(ERROR) from None
    # Database corruption is an unavailable store, not an invalid client
    # selection. The repository filters authorized pairs before decoding it.
    receipt = repository.get_input_receipt_in(connection, topic_id, selection["receipt_id"], occurrences=occurrences)
    if receipt is None:
        raise KeyError(NOT_FOUND)
    return validate_frozen(dict(schema_version=SCHEMA, receipt=receipt, selected_component_ids=selected))


def _evidence_id(pointer: str, slot: dict[str, Any]) -> str | None:
    if pointer.startswith("/evidence_spans/"):
        for key in ("span_id", "id", "label"):
            value = slot.get(key)
            if isinstance(value, str) and value.strip():
                return value
    origin = slot.get("source")
    if isinstance(origin, str) and origin.startswith("evidence_span:"):
        return origin.removeprefix("evidence_span:") or None
    return None


def references(paper: dict[str, Any]) -> list[dict[str, Any]]:
    if paper.get("review_input") is None:
        return []
    value = validate_frozen(paper["review_input"])
    receipt, selected = value["receipt"], value["selected_component_ids"]
    packet = receipt["packet"]
    components = {component["json_pointer"]: component for component in packet["input_scope"]["components"]}
    common = {key: deepcopy(paper[key]) for key in _COMMON}
    result = []
    for pointer in selected:
        component, slot = components[pointer], _slot(packet, pointer)
        result.append(dict(
            **common, kind="review_input_excerpt",
            citation_id="riref_" + _sha(_encoded([paper["paper_version_id"], receipt["receipt_id"], pointer])),
            text=slot["text"], review_source_run_id=receipt["run_id"], source_text_digest=receipt["source_text_sha256"],
            source_evidence_id=_evidence_id(pointer, slot), recorded_content_digest=_sha(slot["text"]),
            locator=dict(kind="review_input_component", json_pointer=pointer, mapping=component["mapping"],
                         char_start=component["source_start"], char_end=component["source_end"]),
            review_receipt_id=receipt["receipt_id"], input_origin=receipt["input_origin"],
            input_component=deepcopy(component), source_extraction=deepcopy(packet.get("source_extraction")),
            **({"source_preparation": deepcopy(packet["source_preparation"])} if "source_preparation" in packet else {}),
        ))
    return result


def attachment(paper: dict[str, Any]) -> dict[str, Any] | None:
    """Include result/provenance context without unrelated original packet text."""
    if paper.get("review_input") is None:
        return None
    value = validate_frozen(paper["review_input"])
    receipt, packet = value["receipt"], value["receipt"]["packet"]
    validation = deepcopy(receipt["validation"])
    schema = ATTACHMENT_SCHEMA
    if "prepared_execution" in validation:
        # The full original request/response stays in the frozen receipt. Only
        # selected components belong in the next model's references; forwarding
        # the embedded original prompt would silently re-add source text.
        proof = validation["prepared_execution"]
        contract, response = proof["contract"], proof["response"]
        schema = "JOURNAL_REVIEW_INPUT_V2"
        validation["prepared_execution"] = dict(
            schema_version="PREPARED_REVIEW_RESULT_SUMMARY_V1",
            request_sha256=contract["request_sha256"], contract_sha256=contract["contract_sha256"],
            raw_text_sha256=response["raw_text_sha256"],
            requested={key: contract[key] for key in ("provider_name", "model")},
            reported={key: response[key] for key in ("provider_name", "model", "response_id")},
            **{key: proof[key] for key in ("reported_model_identity", "independent_live_proof", "scientific_support", "full_document_review", "human_adoption")},
        )
    return deepcopy(dict(
        schema_version=schema, paper_version_id=paper["paper_version_id"], validation=validation,
        **{key: receipt[key] for key in ("receipt_id", "input_origin", "run_id", "paper_id", "review_level", "status")},
        selected_component_ids=value["selected_component_ids"], input_scope=packet["input_scope"],
        source_extraction=packet.get("source_extraction"), review_result=receipt["draft"],
        **({"source_preparation": packet["source_preparation"]} if "source_preparation" in packet else {}),
    ))
