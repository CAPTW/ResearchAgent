"""Read-only ranges of one immutable source observation, never review completion."""
from contextlib import closing
import sqlite3
from typing import Any

from app.db.bootstrap import connect_metadata_index
from app.domain import review_preparation as domain
from app.domain.review_range_input import build_packet
from app.repositories import ai_review_drafts, review_preparations

SCHEMA = "PAPER_REVIEW_SOURCE_COVERAGE_V1"
MAX_RECORDS = 2_000
_ORIGINS = ("PROVIDER_RESPONSE", "FIXTURE_SYNTHETIC", "MANUAL_IMPORT_UNVERIFIED")


def _ranges(records: list[dict[str, Any]]) -> list[dict[str, int]]:
    merged: list[dict[str, int]] = []
    for record in sorted(records, key=lambda row: (row["start"], row["end"])):
        start, end = record["start"], record["end"]
        if merged and start <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], end)
        else:
            merged.append(dict(start=start, end=end))
    return merged


def _group(records: list[dict[str, Any]], identity: str) -> dict[str, Any]:
    records = sorted(records, key=lambda row: (row["start"], row["end"], row[identity]))
    ranges = _ranges(records)
    return dict(record_count=len(records), covered_chars=sum(row["end"] - row["start"] for row in ranges),
                ranges=ranges, records=records)


def _gaps(ranges: list[dict[str, int]], size: int) -> list[dict[str, int]]:
    gaps = []
    end = 0
    for row in ranges:
        if row["start"] > end:
            gaps.append(dict(start=end, end=row["start"]))
        end = row["end"]
    if end < size:
        gaps.append(dict(start=end, end=size))
    return gaps


def _bounded_rows(c: sqlite3.Connection, sql: str, params: list[Any]) -> list[sqlite3.Row]:
    rows = c.execute(sql + " LIMIT ?", (*params, MAX_RECORDS + 1)).fetchall()
    if len(rows) > MAX_RECORDS:
        # A bounded, incomplete list must never become a complete coverage claim.
        raise ValueError("review_source_coverage_limit_exceeded")
    return rows


def _result_record(receipt: dict[str, Any], preparation: dict[str, Any]) -> dict[str, Any] | None:
    """Classify an already validated receipt; an origin label is not a result proof."""
    if receipt["input_origin"] != "PROVIDER_RESPONSE":
        return None
    proof = receipt["validation"].get("prepared_execution")
    classification, subtype = "UNVERIFIED_PROVIDER", "UNVERIFIED_ORIGIN"
    if proof is not None:
        # _row_to_receipt reconstructs every prepared request/response field and
        # reserved adapter marker before this projection is allowed to use it.
        classification = ("BODY_GROUNDED" if receipt["review_level"] == "full_text_grounded"
                          and receipt["draft"].get("cited_evidence_span_ids") else "OTHER_RETURNED")
        subtype = "VALIDATED_PREPARED_RESPONSE"
        marker = receipt["validation"].get("execution")
        manifest = marker.get("manifest") if isinstance(marker, dict) else None
        if (isinstance(manifest, dict) and manifest.get("schema_version") == "ANTHROPIC_PREPARED_REVIEW_WIRE_V2"
                and manifest.get("execution_surface") == "NATIVE_HTTP"):
            subtype = "NATIVE_ANTHROPIC_V2"
    selection = {key: value for key, value in preparation["selection"].items() if key != "text"}
    return dict(receipt_id=receipt["receipt_id"], preparation_id=preparation["preparation_id"],
        start=selection["start"], end=selection["end"], source=dict(preparation["source"]), selection=selection,
        **{"class": classification}, subtype=subtype, review_level=receipt["review_level"],
        prepared_schema=proof["contract"]["schema_version"] if proof is not None else None,
        independent_live_proof="NOT_VERIFIED")


def _results(records: list[dict[str, Any]], size: int) -> dict[str, Any]:
    groups = {
        "validated_returned": _group([record for record in records if record["class"] != "UNVERIFIED_PROVIDER"], "receipt_id"),
        "body_grounded": _group([record for record in records if record["class"] == "BODY_GROUNDED"], "receipt_id"),
        "other_returned": _group([record for record in records if record["class"] == "OTHER_RETURNED"], "receipt_id"),
        "unverified_provider": _group([record for record in records if record["class"] == "UNVERIFIED_PROVIDER"], "receipt_id"),
    }
    for key in ("validated_returned", "body_grounded"):
        groups[key]["gaps"] = _gaps(groups[key]["ranges"], size)
    return dict(groups, gap_semantics="NO_VALIDATED_SAVED_RESULT", attempt_history_included=False)


def overview(topic: str, version: str, identity: str, occurrences: list[tuple[str, str]]) -> dict[str, Any]:
    """Validate scoped history on one snapshot; do not resolve any artifact path."""
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        anchor = review_preparations.detail_in(c, topic, version, identity, occurrences)
        where, params = review_preparations._scope(topic, version, occurrences)
        rows = _bounded_rows(c, f"SELECT * FROM paper_review_preparations WHERE {where}", params)
        preparations: dict[str, dict[str, Any]] = {}
        for row in rows:
            value = review_preparations._read(row)
            preparations[value["preparation_id"]] = value
        source = anchor["source"]
        prepared_records = [dict(preparation_id=value["preparation_id"], start=value["selection"]["start"],
                                 end=value["selection"]["end"]) for value in preparations.values()
                            if value["source"]["source_digest"] == source["source_digest"]]
        by_origin: dict[str, list[dict[str, Any]]] = {origin: [] for origin in _ORIGINS}
        result_records = []
        unmapped = other_source = 0
        if ai_review_drafts._history_exists(c):
            where = f"topic_id=? AND (run_id,paper_id) IN (VALUES {','.join('(?,?)' for _ in occurrences)})"
            params = [topic, *(value for pair in occurrences for value in pair)]
            rows = _bounded_rows(c, f"SELECT * FROM ai_review_input_history WHERE {where}", params)
            for row in rows:
                receipt = ai_review_drafts._row_to_receipt(row)
                packet = receipt["packet"]
                if packet["packet_version"] not in {"ai-review-packet-v3", "ai-review-packet-v4", "ai-review-packet-v5"}:
                    # These records cannot be assigned original source coordinates.
                    # This is a scoped history count, not a gap in this source.
                    unmapped += 1
                    continue
                binding = packet["source_preparation"]
                canonical = preparations.get(binding["preparation_id"])
                # Packet validation establishes internal consistency only. Require
                # the exact server preparation, including its digest and Unicode
                # text, before trusting a range or excluding another observation.
                if canonical is None or packet != build_packet(canonical):
                    raise ValueError("review_source_coverage_integrity_invalid")
                if canonical["source"]["source_digest"] != source["source_digest"]:
                    other_source += 1
                    continue
                by_origin[receipt["input_origin"]].append(dict(receipt_id=receipt["receipt_id"],
                    preparation_id=canonical["preparation_id"], start=canonical["selection"]["start"],
                    end=canonical["selection"]["end"]))
                result_record = _result_record(receipt, canonical)
                if result_record is not None:
                    result_records.append(result_record)
        prepared = _group(prepared_records, "preparation_id")
        prepared["gaps"] = _gaps(prepared["ranges"], source["source_chars"])
        return dict(schema_version=SCHEMA, anchor_preparation_id=identity, source=source,
                    normalization=domain.NORMALIZATION,
                    decoding="UTF8_SIG_STRICT" if source["source_kind"] == "pages" else "UTF8_STRICT",
                    prepared=prepared,
                    inputs=[dict(input_origin=origin, **_group(by_origin[origin], "receipt_id")) for origin in _ORIGINS],
                    results=_results(result_records, source["source_chars"]),
                    unmapped_receipt_count=unmapped, other_source_receipt_count=other_source,
                    full_document_review="NOT_ESTABLISHED", scientific_support="NOT_ASSESSED")
