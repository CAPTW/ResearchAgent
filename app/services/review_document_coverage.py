"""Align saved input/result ranges to one frozen extraction without running models.

Cross-source alignment establishes exact normalized text equality only. It does
not establish original PDF identity, whole-paper review or scientific support.
"""
from contextlib import closing
import sqlite3
from typing import Any

from app.auth.resource_access import ResourceActor
from app.db.bootstrap import connect_metadata_index
from app.domain import review_preparation as preparation_domain
from app.domain.review_range_input import build_packet
from app.repositories import ai_review_drafts, review_preparations
from app.repositories import review_source_coverage as coverage

SCHEMA = "PAPER_REVIEW_DOCUMENT_COVERAGE_V1"


def _align(preparation: dict[str, Any], source: dict[str, Any], text: str) -> dict[str, Any]:
    selection = preparation["selection"]
    selected = selection["text"]
    if preparation["source"]["source_digest"] == source["source_digest"]:
        start, end = selection["start"], selection["end"]
        if not 0 <= start < end <= len(text) or text[start:end] != selected:
            raise ValueError("review_document_coverage_integrity_invalid")
        return dict(start=start, end=end, mapping="DIRECT_SOURCE_COORDINATES")
    start = text.find(selected)
    if start < 0:
        return dict(reason="TEXT_NOT_FOUND")
    if text.find(selected, start + 1) >= 0:
        return dict(reason="TEXT_AMBIGUOUS")
    return dict(start=start, end=start + len(selected), mapping="EXACT_UNIQUE_TEXT_MATCH")


def _history_in(c: sqlite3.Connection, topic: str, version: str, occurrences: list[tuple[str, str]]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    preparations: dict[str, dict[str, Any]] = {}
    receipts: list[dict[str, Any]] = []
    if not occurrences:
        return preparations, receipts
    if review_preparations._exists(c):
        where, params = review_preparations._scope(topic, version, occurrences)
        for row in coverage._bounded_rows(c, f"SELECT * FROM paper_review_preparations WHERE {where}", params):
            value = review_preparations._read(row)
            preparations[value["preparation_id"]] = value
    if ai_review_drafts._history_exists(c):
        where = f"topic_id=? AND (run_id,paper_id) IN (VALUES {','.join('(?,?)' for _ in occurrences)})"
        # The receipt table predates paper-version columns. Filter the bound
        # version before decoding full receipt fields; malformed/unbound rows
        # remain visible for fail-closed validation or explicit legacy reporting.
        where += """ AND CASE WHEN json_valid(packet_json) THEN CASE
            WHEN json_extract(packet_json,'$.packet_version') IN ('ai-review-packet-v3','ai-review-packet-v4','ai-review-packet-v5')
                AND json_type(packet_json,'$.source_preparation.paper_version_id')='text'
            THEN json_extract(packet_json,'$.source_preparation.paper_version_id')=?
            ELSE 1 END ELSE 1 END"""
        params = [topic, *(value for pair in occurrences for value in pair), version]
        for row in coverage._bounded_rows(c, f"SELECT * FROM ai_review_input_history WHERE {where}", params):
            receipts.append(ai_review_drafts._row_to_receipt(row))
    return preparations, receipts


def _intersections(ranges: list[dict[str, int]], start: int, end: int) -> list[dict[str, int]]:
    return [dict(start=max(start, row["start"]), end=min(end, row["end"])) for row in ranges
            if row["start"] < end and row["end"] > start]


def _page_gaps(ranges: list[dict[str, int]], start: int, end: int) -> list[dict[str, int]]:
    gaps, cursor = [], start
    for row in ranges:
        if row["start"] > cursor:
            gaps.append(dict(start=cursor, end=row["start"]))
        cursor = row["end"]
    if cursor < end:
        gaps.append(dict(start=cursor, end=end))
    return gaps


def _project(extraction: dict[str, Any], text: str, preparations: dict[str, dict[str, Any]],
             receipts: list[dict[str, Any]]) -> dict[str, Any]:
    source = extraction["source"]
    if len(text) != source["source_chars"]:
        raise ValueError("review_document_coverage_integrity_invalid")
    prepared_records, result_records = [], []
    unaligned: dict[str, list[dict[str, Any]]] = dict(preparations=[], receipts=[])
    alignments = {}
    for identity, preparation in preparations.items():
        alignment = _align(preparation, source, text)
        alignments[identity] = alignment
        original = dict(preparation_id=identity, source=dict(preparation["source"]),
            selection={key: value for key, value in preparation["selection"].items() if key != "text"})
        if "reason" in alignment:
            unaligned["preparations"].append(dict(original, **alignment))
        else:
            prepared_records.append(dict(original, **alignment))
    for receipt in receipts:
        packet = receipt["packet"]
        if packet["packet_version"] not in {"ai-review-packet-v3", "ai-review-packet-v4", "ai-review-packet-v5"}:
            unaligned["receipts"].append(dict(receipt_id=receipt["receipt_id"], input_origin=receipt["input_origin"],
                preparation_id=None, reason="LEGACY_BINDING_UNAVAILABLE"))
            continue
        identity = packet["source_preparation"]["preparation_id"]
        canonical = preparations.get(identity)
        if canonical is None or packet != build_packet(canonical):
            raise ValueError("review_document_coverage_integrity_invalid")
        alignment = alignments[identity]
        result = coverage._result_record(receipt, canonical)
        if "reason" in alignment:
            original = dict(receipt_id=receipt["receipt_id"], input_origin=receipt["input_origin"], preparation_id=identity,
                source=dict(canonical["source"]),
                selection={key: value for key, value in canonical["selection"].items() if key != "text"})
            unaligned["receipts"].append(dict(original, **alignment))
        elif result is not None:
            result_records.append(dict(result, **alignment))
    prepared = coverage._group(prepared_records, "preparation_id")
    prepared["gaps"] = coverage._gaps(prepared["ranges"], len(text))
    results = coverage._results(result_records, len(text))
    pages = []
    for page in extraction["page_ranges"]:
        start, end = page["start"], page["end"]
        selected = _intersections(prepared["ranges"], start, end)
        returned = _intersections(results["body_grounded"]["ranges"], start, end)
        pages.append(dict(page, text_status="TEXT_EXTRACTED", prepared_chars=sum(row["end"] - row["start"] for row in selected),
            body_result_chars=sum(row["end"] - row["start"] for row in returned), body_result_ranges=returned,
            gaps=_page_gaps(returned, start, end)))
    for key, identity_key in (("preparations", "preparation_id"), ("receipts", "receipt_id")):
        unaligned[key].sort(key=lambda record: record[identity_key])
    return dict(schema_version=SCHEMA, extraction=extraction, coordinate_space="NORMALIZED_EXTRACTION_CODEPOINTS",
        normalization=preparation_domain.NORMALIZATION, prepared=prepared, results=results, pages=pages,
        no_text_page_numbers=extraction["no_text_page_numbers"], unvisited_page_numbers=extraction["unvisited_page_numbers"],
        unaligned=unaligned, alignment_claim="EXACT_NORMALIZED_TEXT_ONLY_NOT_PDF_IDENTITY",
        full_document_review="NOT_ESTABLISHED", scientific_support="NOT_ASSESSED")


def overview(topic: str, version: str, extraction_id: str, actor: ResourceActor) -> dict[str, Any]:
    # Imports stay local so the pure alignment helper needs no extraction runtime.
    from app.services import review_pdf_extraction, review_preparation
    from app.repositories import review_pdf_extractions
    occurrences = sorted(set(review_preparation._occurrences(topic, version, actor)))
    with closing(connect_metadata_index(read_only=True)) as connection:
        connection.execute("BEGIN")
        extraction = review_pdf_extractions.detail_in(connection, topic, version, extraction_id, occurrences)
        text = review_pdf_extraction.read_text(extraction)
        preparations, receipts = _history_in(connection, topic, version, occurrences)
        return _project(extraction, text, preparations, receipts)
