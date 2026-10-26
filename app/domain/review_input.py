"""Versioned packet scope, distinct from extraction completeness or model review.

Exact source mapping is established while the preserved review text is available.
Independent packet validation checks the receipt's bindings and range arithmetic;
the source hash cannot prove omitted source text without its separate archive.
"""

from copy import deepcopy
import hashlib
import re
from typing import Any, TypeGuard

from app.domain.journal_exchange import preserved_json
from app.domain.review_extraction import validate_provenance

PACKET_VERSION = "ai-review-packet-v2"
SCOPE_VERSION = "AI_REVIEW_INPUT_SCOPE_V1"
ERROR = "review_input_scope_invalid"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _integer(value: Any) -> TypeGuard[int]:
    return type(value) is int and value >= 0


def _hash(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _slots(packet: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    slots: list[tuple[str, dict[str, Any]]] = []
    for key in ("selected_text_excerpts", "evidence_spans"):
        values = packet.get(key, [])
        if not isinstance(values, list):
            raise ValueError(ERROR)
        for index, value in enumerate(values):
            if not isinstance(value, dict) or not isinstance(value.get("text"), str):
                raise ValueError(ERROR)
            slots.append((f"/{key}/{index}/text", value))
    return slots


def _span_range(span: dict[str, Any], source: str) -> tuple[int, int] | None:
    start, end, text = span.get("char_start"), span.get("char_end"), span.get("text")
    if (
        _integer(start)
        and _integer(end)
        and start <= end <= len(source)
        and isinstance(text, str)
        and end - start == len(text)
        and source[start:end] == text
    ):
        return start, end
    return None


def _span_id(span: dict[str, Any], index: int) -> str:
    for key in ("span_id", "id", "label"):
        value = str(span.get(key) or "").strip()
        if value:
            return value
    return f"span-{index}"


def _mapping(pointer: str, slot: dict[str, Any], review: dict[str, Any], source: str) -> tuple[int, int] | None:
    text = slot["text"]
    if pointer.startswith("/evidence_spans/"):
        return _span_range(slot, source)
    origin = slot.get("source")
    if origin == "text_excerpt" and source.startswith(text):
        return 0, len(text)
    if not isinstance(origin, str) or not origin.startswith("evidence_span:"):
        return None
    spans = review.get("evidence_spans") or []
    matches = [
        span
        for index, span in enumerate(spans, 1)
        if isinstance(span, dict) and _span_id(span, index) == origin.removeprefix("evidence_span:")
    ]
    if len(matches) != 1:
        return None
    candidate = matches[0]
    located = _span_range(candidate, source)
    if located is None or not candidate["text"].startswith(text):
        return None
    return located[0], located[0] + len(text)


def _covered(components: list[dict[str, Any]]) -> int:
    intervals = sorted((item["source_start"], item["source_end"]) for item in components if item["mapping"] == "exact")
    total, boundary = 0, 0
    for start, end in intervals:
        total += max(0, end - max(boundary, start))
        boundary = max(boundary, end)
    return total


def enrich_packet(review: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any]:
    """Attach scope to a new V2 packet without transforming its transmitted text."""
    try:
        if packet.get("packet_version") != PACKET_VERSION or "input_scope" in packet:
            raise ValueError(ERROR)
        source_text = review.get("text_excerpt")
        if source_text is None:
            source_text = ""
        if not isinstance(source_text, str) or str(review.get("paper_id") or "").strip() != packet.get("paper_id"):
            raise ValueError(ERROR)
        # Keep the packet builder's existing optional source identifier semantics.
        recorded_source = str(review.get("source_text_sha256") or "").strip() or None
        if packet.get("source_text_sha256") != recorded_source:
            raise ValueError(ERROR)
        result = deepcopy(packet)
        if review.get("extraction_provenance") is not None:
            result["source_extraction"] = validate_provenance(
                review["extraction_provenance"], text_sha256=_sha(source_text), text_chars=len(source_text)
            )
        components = []
        for pointer, slot in _slots(result):
            located = _mapping(pointer, slot, review, source_text)
            components.append(
                dict(
                    json_pointer=pointer,
                    text_sha256=_sha(slot["text"]),
                    chars=len(slot["text"]),
                    source_start=located[0] if located else None,
                    source_end=located[1] if located else None,
                    mapping="exact" if located else "unknown",
                )
            )
        result["input_scope"] = dict(
            schema_version=SCOPE_VERSION,
            source=dict(
                run_id=str(review.get("run_id") or "").strip(),
                paper_id=packet["paper_id"],
                recorded_source_sha256=packet.get("source_text_sha256"),
                preserved_text_sha256=_sha(source_text),
                preserved_text_chars=len(source_text),
                document_chars=None,
            ),
            components=components,
            covered_stored_chars=_covered(components),
            document_coverage="UNKNOWN",
            full_document_review="NOT_ESTABLISHED",
            packet_payload_sha256=_sha(preserved_json(result)),
        )
        validate_packet(result)
        return result
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ValueError(ERROR) from None


def validate_packet(packet: dict[str, Any]) -> dict[str, Any]:
    """Validate a V2 scope; identify a V1 packet without inventing provenance."""
    try:
        if packet.get("packet_version") in {"ai-review-packet-v3", "ai-review-packet-v4", "ai-review-packet-v5"}:
            from app.domain.review_range_input import validate_packet as validate_range_packet
            return validate_range_packet(packet)
        if packet.get("packet_version") == "ai-review-packet-v1" and "input_scope" not in packet:
            return dict(
                schema_version="LEGACY_REVIEW_INPUT_UNKNOWN",
                document_coverage="UNKNOWN",
                full_document_review="NOT_ESTABLISHED",
            )
        if packet.get("packet_version") != PACKET_VERSION:
            raise ValueError(ERROR)
        scope = packet["input_scope"]
        if not isinstance(scope, dict) or set(scope) != {
            "schema_version",
            "source",
            "components",
            "covered_stored_chars",
            "document_coverage",
            "full_document_review",
            "packet_payload_sha256",
        }:
            raise ValueError(ERROR)
        if (
            scope["schema_version"] != SCOPE_VERSION
            or scope["document_coverage"] != "UNKNOWN"
            or scope["full_document_review"] != "NOT_ESTABLISHED"
            or scope["packet_payload_sha256"]
            != _sha(preserved_json({key: value for key, value in packet.items() if key != "input_scope"}))
        ):
            raise ValueError(ERROR)
        source = scope["source"]
        if (
            not isinstance(source, dict)
            or set(source)
            != {
                "run_id",
                "paper_id",
                "recorded_source_sha256",
                "preserved_text_sha256",
                "preserved_text_chars",
                "document_chars",
            }
            or not isinstance(source["run_id"], str)
            or not source["run_id"].strip()
            or source["paper_id"] != packet.get("paper_id")
            or not isinstance(source["paper_id"], str)
            or not source["paper_id"].strip()
            or source["recorded_source_sha256"] != packet.get("source_text_sha256")
            or not _hash(source["preserved_text_sha256"])
            or not _integer(source["preserved_text_chars"])
            or source["document_chars"] is not None
        ):
            raise ValueError(ERROR)
        if "source_extraction" in packet:
            validate_provenance(packet["source_extraction"], text_sha256=source["preserved_text_sha256"], text_chars=source["preserved_text_chars"])
        components, slots = scope["components"], _slots(packet)
        if not isinstance(components, list) or len(components) != len(slots):
            raise ValueError(ERROR)
        for component, (pointer, slot) in zip(components, slots):
            if (
                not isinstance(component, dict)
                or set(component) != {"json_pointer", "text_sha256", "chars", "source_start", "source_end", "mapping"}
                or component["json_pointer"] != pointer
                or component["text_sha256"] != _sha(slot["text"])
                or not _integer(component["chars"])
                or component["chars"] != len(slot["text"])
            ):
                raise ValueError(ERROR)
            start, end = component["source_start"], component["source_end"]
            if component["mapping"] == "exact":
                if (
                    not _integer(start)
                    or not _integer(end)
                    or not 0 <= start <= end <= source["preserved_text_chars"]
                    or end - start != component["chars"]
                ):
                    raise ValueError(ERROR)
                if pointer.startswith("/evidence_spans/") and (
                    not _integer(slot.get("char_start"))
                    or not _integer(slot.get("char_end"))
                    or slot.get("char_start") != start
                    or slot.get("char_end") != end
                ):
                    raise ValueError(ERROR)
                if pointer.startswith("/selected_text_excerpts/") and (
                    slot.get("source") == "text_excerpt" and start != 0
                ):
                    raise ValueError(ERROR)
                if pointer.startswith("/selected_text_excerpts/") and not (
                    slot.get("source") == "text_excerpt"
                    or isinstance(slot.get("source"), str)
                    and slot["source"].startswith("evidence_span:")
                ):
                    raise ValueError(ERROR)
            elif component["mapping"] != "unknown" or start is not None or end is not None:
                raise ValueError(ERROR)
        if not _integer(scope["covered_stored_chars"]) or scope["covered_stored_chars"] != _covered(components):
            raise ValueError(ERROR)
        return deepcopy(scope)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ValueError(ERROR) from None
