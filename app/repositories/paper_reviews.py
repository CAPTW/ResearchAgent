from __future__ import annotations

from contextlib import closing
import hashlib
import json
import sqlite3
from typing import Any

from app.db.bootstrap import connect_metadata_index
from app.domain.journal_exchange import preserved_json
from app.domain.review_extraction import validate_provenance
from app.repositories import artifact_refs
from app.repositories.files import iso_now_utc

FULL_TEXT_STATUSES = {"unavailable", "abstract_only", "full_text_available", "reviewed"}


def _json_list(value: Any) -> str:
    if value is None:
        return "[]"
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
    else:
        items = [str(value).strip()] if str(value).strip() else []
    return json.dumps(items, ensure_ascii=False)


def _decode_json_list(value: Any) -> list[str]:
    if not value:
        return []
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError:
        return [str(value)]
    if not isinstance(decoded, list):
        return [str(decoded)]
    return [str(item) for item in decoded if str(item).strip()]


def _json_payload(value: Any, default: Any) -> str:
    if value is None:
        value = default
    return json.dumps(value, ensure_ascii=False)


def _decode_json_payload(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return default


def _coerce_score(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _row_to_review(row: Any) -> dict[str, Any]:
    data = dict(row)
    extraction_json = data.pop("_extraction_json", None)
    extraction_hash = data.pop("_extraction_sha256", None)
    data["authors"] = _decode_json_list(data.pop("authors_json", "[]"))
    data["key_findings"] = _decode_json_list(data.pop("key_findings_json", "[]"))
    data["limitations"] = _decode_json_list(data.pop("limitations_json", "[]"))
    data["evidence_spans"] = _decode_json_payload(data.pop("evidence_spans_json", "[]"), [])
    for key in ("relevance_score", "novelty_score", "evidence_strength"):
        data[key] = _coerce_score(data.get(key))
    data["text_char_count"] = int(data.get("text_char_count") or 0)
    if extraction_json is not None or extraction_hash is not None:
        try:
            provenance = _validated_extraction(data, json.loads(extraction_json))
            if _extraction_checksum(data, provenance) != extraction_hash:
                raise ValueError("review_extraction_storage_corrupt")
            data["extraction_provenance"] = provenance
        except (ValueError, TypeError, KeyError, RecursionError):
            raise ValueError("review_extraction_storage_corrupt") from None
    return data


def _validated_extraction(payload: dict[str, Any], value: Any) -> dict[str, Any]:
    text = payload.get("text_excerpt")
    if (payload.get("full_text_status") != "reviewed" or not isinstance(text, str)
            or payload.get("text_char_count") != len(text)):
        raise ValueError("review_extraction_provenance_invalid")
    actual_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if payload.get("source_text_sha256") != actual_hash:
        raise ValueError("review_extraction_provenance_invalid")
    return validate_provenance(value, text_sha256=actual_hash, text_chars=len(text))


def _extraction_checksum(payload: dict[str, Any], provenance: dict[str, Any]) -> str:
    value = dict(topic_id=payload["topic_id"], run_id=payload["run_id"], paper_id=payload["paper_id"],
                 provenance=provenance)
    return hashlib.sha256(preserved_json(value).encode("utf-8")).hexdigest()


def _extraction_exists(connection: sqlite3.Connection) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_review_extraction'"
    ).fetchone() is not None


def _initialize_extraction(connection: sqlite3.Connection) -> None:
    # Current projection, not another history system. Old records remain in
    # immutable review-input receipts; a legacy replacement clears this row.
    connection.execute("""CREATE TABLE IF NOT EXISTS paper_review_extraction (
        topic_id TEXT NOT NULL, run_id TEXT NOT NULL, paper_id TEXT NOT NULL,
        provenance_json TEXT NOT NULL, provenance_sha256 TEXT NOT NULL,
        PRIMARY KEY(topic_id,run_id,paper_id)
    )""")


def _review_read_source(connection: sqlite3.Connection) -> tuple[str, str]:
    if not _extraction_exists(connection):
        return "r.*", "paper_reviews r"
    return ("r.*,e.provenance_json AS _extraction_json,e.provenance_sha256 AS _extraction_sha256",
            "paper_reviews r LEFT JOIN paper_review_extraction e ON "
            "e.topic_id=r.topic_id AND e.run_id=r.run_id AND e.paper_id=r.paper_id")


def _normalize_review(review: dict[str, Any]) -> dict[str, Any]:
    topic_id = str(review.get("topic_id") or "").strip()
    run_id = str(review.get("run_id") or "").strip()
    paper_id = str(review.get("paper_id") or "").strip()
    if not topic_id:
        raise ValueError("topic_id is required")
    if not run_id:
        raise ValueError("run_id is required")
    if not paper_id:
        raise ValueError("paper_id is required")

    full_text_status = str(review.get("full_text_status") or "unavailable").strip()
    if full_text_status not in FULL_TEXT_STATUSES:
        raise ValueError(f"invalid full_text_status: {full_text_status}")
    full_text_artifact_path = artifact_refs.normalize_path_reference(
        str(review.get("full_text_artifact_path") or "").strip() or None,
        run_id=run_id,
    )

    return {
        "topic_id": topic_id,
        "run_id": run_id,
        "paper_id": paper_id,
        "title": str(review.get("title") or "Untitled paper").strip() or "Untitled paper",
        "source_provider": str(review.get("source_provider") or "").strip() or None,
        "authors_json": _json_list(review.get("authors")),
        "published_at": str(review.get("published_at") or review.get("published") or "").strip() or None,
        "abstract_text": str(review.get("abstract_text") or review.get("abstract") or "").strip() or None,
        "text_excerpt": str(review.get("text_excerpt") or "").strip() or None,
        "full_text_status": full_text_status,
        "relevance_score": _coerce_score(review.get("relevance_score")),
        "novelty_score": _coerce_score(review.get("novelty_score")),
        "evidence_strength": _coerce_score(review.get("evidence_strength")),
        "method_summary": str(review.get("method_summary") or "").strip() or None,
        "key_findings_json": _json_list(review.get("key_findings")),
        "limitations_json": _json_list(review.get("limitations")),
        "why_it_matters": str(review.get("why_it_matters") or "").strip() or None,
        "topic_fit_rationale": str(review.get("topic_fit_rationale") or "").strip() or None,
        "review_model": str(review.get("review_model") or "").strip() or None,
        "reviewer": str(review.get("reviewer") or "").strip() or None,
        "reviewed_at": str(review.get("reviewed_at") or "").strip() or iso_now_utc(),
        "source_text_sha256": str(review.get("source_text_sha256") or "").strip() or None,
        "full_text_artifact_path": full_text_artifact_path,
        "text_char_count": int(review.get("text_char_count") or 0),
        "evidence_spans_json": _json_payload(review.get("evidence_spans"), []),
        "review_source_type": str(review.get("review_source_type") or "abstract").strip() or "abstract",
        "full_text_reviewed_at": str(review.get("full_text_reviewed_at") or "").strip() or None,
    }


def upsert_review(review: dict[str, Any]) -> dict[str, Any]:
    payload = _normalize_review(review)
    raw_provenance = review.get("extraction_provenance")
    provenance = _validated_extraction(payload, raw_provenance) if raw_provenance is not None else None
    with closing(connect_metadata_index()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO paper_reviews (
              topic_id, run_id, paper_id, title, source_provider, authors_json, published_at,
              abstract_text, text_excerpt, full_text_status, relevance_score, novelty_score,
              evidence_strength, method_summary, key_findings_json, limitations_json,
              why_it_matters, topic_fit_rationale, review_model, reviewer, reviewed_at,
              source_text_sha256, full_text_artifact_path, text_char_count, evidence_spans_json,
              review_source_type, full_text_reviewed_at
            ) VALUES (
              :topic_id, :run_id, :paper_id, :title, :source_provider, :authors_json, :published_at,
              :abstract_text, :text_excerpt, :full_text_status, :relevance_score, :novelty_score,
              :evidence_strength, :method_summary, :key_findings_json, :limitations_json,
              :why_it_matters, :topic_fit_rationale, :review_model, :reviewer, :reviewed_at,
              :source_text_sha256, :full_text_artifact_path, :text_char_count, :evidence_spans_json,
              :review_source_type, :full_text_reviewed_at
            )
            ON CONFLICT(topic_id, run_id, paper_id) DO UPDATE SET
              title = excluded.title,
              source_provider = excluded.source_provider,
              authors_json = excluded.authors_json,
              published_at = excluded.published_at,
              abstract_text = excluded.abstract_text,
              text_excerpt = excluded.text_excerpt,
              full_text_status = excluded.full_text_status,
              relevance_score = excluded.relevance_score,
              novelty_score = excluded.novelty_score,
              evidence_strength = excluded.evidence_strength,
              method_summary = excluded.method_summary,
              key_findings_json = excluded.key_findings_json,
              limitations_json = excluded.limitations_json,
              why_it_matters = excluded.why_it_matters,
              topic_fit_rationale = excluded.topic_fit_rationale,
              review_model = excluded.review_model,
              reviewer = excluded.reviewer,
              reviewed_at = excluded.reviewed_at,
              source_text_sha256 = excluded.source_text_sha256,
              full_text_artifact_path = excluded.full_text_artifact_path,
              text_char_count = excluded.text_char_count,
              evidence_spans_json = excluded.evidence_spans_json,
              review_source_type = excluded.review_source_type,
              full_text_reviewed_at = excluded.full_text_reviewed_at
            """,
            payload,
        )
        if provenance is not None:
            _initialize_extraction(connection)
            connection.execute("""INSERT INTO paper_review_extraction VALUES (?,?,?,?,?)
                ON CONFLICT(topic_id,run_id,paper_id) DO UPDATE SET
                provenance_json=excluded.provenance_json,provenance_sha256=excluded.provenance_sha256""", (
                    payload["topic_id"], payload["run_id"], payload["paper_id"],
                    preserved_json(provenance), _extraction_checksum(payload, provenance),
                ))
        elif _extraction_exists(connection):
            connection.execute("DELETE FROM paper_review_extraction WHERE topic_id=? AND run_id=? AND paper_id=?",
                               (payload["topic_id"], payload["run_id"], payload["paper_id"]))
        columns, source = _review_read_source(connection)
        row = connection.execute(f"""SELECT {columns} FROM {source}
            WHERE r.topic_id=? AND r.run_id=? AND r.paper_id=?""",
            (payload["topic_id"], payload["run_id"], payload["paper_id"])).fetchone()
        if row is None:
            raise RuntimeError("paper review upsert did not persist")
        # Keep the writer lock until this call's exact review/provenance pair
        # has been read; a concurrent replacement must not change our response.
        stored = _row_to_review(row)
    return stored


def update_full_text_artifact_path(
    topic_id: str,
    run_id: str,
    paper_id: str,
    artifact_path: str | None,
) -> dict[str, Any] | None:
    normalized_path = artifact_refs.normalize_path_reference(artifact_path, run_id=run_id)
    with connect_metadata_index() as connection:
        connection.execute(
            """
            UPDATE paper_reviews
            SET full_text_artifact_path = ?
            WHERE topic_id = ? AND run_id = ? AND paper_id = ?
            """,
            (normalized_path, topic_id, run_id, paper_id),
        )
    return get_review(topic_id, run_id, paper_id)


def get_review(topic_id: str, run_id: str, paper_id: str, *, read_only: bool = False) -> dict[str, Any] | None:
    with closing(connect_metadata_index(read_only=read_only)) as connection, connection:
        # Inspect the optional table and read the row from the same snapshot.
        connection.execute("BEGIN")
        columns, source = _review_read_source(connection)
        row = connection.execute(
            f"""
            SELECT {columns} FROM {source}
            WHERE r.topic_id = ? AND r.run_id = ? AND r.paper_id = ?
            """,
            (topic_id, run_id, paper_id),
        ).fetchone()
    return _row_to_review(row) if row else None


def list_reviews(
    *,
    topic_id: str | None = None,
    run_id: str | None = None,
    paper_ids: list[str] | None = None,
    limit: int = 100,
    read_only: bool = False,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if topic_id:
        clauses.append("r.topic_id = ?")
        params.append(topic_id)
    if run_id:
        clauses.append("r.run_id = ?")
        params.append(run_id)
    if paper_ids:
        placeholders = ", ".join("?" for _ in paper_ids)
        clauses.append(f"r.paper_id IN ({placeholders})")
        params.extend(paper_ids)
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with closing(connect_metadata_index(read_only=read_only)) as connection, connection:
        connection.execute("BEGIN")
        columns, source = _review_read_source(connection)
        rows = connection.execute(
            f"""
            SELECT {columns} FROM {source}
            {where_sql}
            ORDER BY r.relevance_score DESC, r.reviewed_at DESC
            LIMIT ?
            """,
            [*params, max(1, int(limit))],
        ).fetchall()
    return [_row_to_review(row) for row in rows]


def get_review_coverage(
    topic_id: str,
    *,
    run_ids: list[str] | None = None,
    total_papers: int | None = None,
    read_only: bool = False,
) -> dict[str, Any]:
    clauses = ["topic_id = ?"]
    params: list[Any] = [topic_id]
    if run_ids:
        placeholders = ", ".join("?" for _ in run_ids)
        clauses.append(f"run_id IN ({placeholders})")
        params.extend(run_ids)
    where_sql = f"WHERE {' AND '.join(clauses)}"
    with connect_metadata_index(read_only=read_only) as connection:
        row = connection.execute(
            f"""
            SELECT
              COUNT(*) AS reviewed_count,
              SUM(CASE WHEN full_text_status = 'abstract_only' THEN 1 ELSE 0 END) AS abstract_only_count,
              SUM(CASE WHEN full_text_status IN ('full_text_available', 'reviewed') THEN 1 ELSE 0 END) AS full_text_reviewed_count
            FROM paper_reviews
            {where_sql}
            """,
            params,
        ).fetchone()
    reviewed_count = int(row["reviewed_count"] or 0)
    denominator = int(total_papers) if total_papers is not None else reviewed_count
    denominator = max(denominator, reviewed_count)
    return {
        "total_papers": denominator,
        "reviewed_count": reviewed_count,
        "abstract_only_count": int(row["abstract_only_count"] or 0),
        "full_text_reviewed_count": int(row["full_text_reviewed_count"] or 0),
        "coverage_percentage": round((reviewed_count / denominator) * 100, 1) if denominator else 0.0,
    }
