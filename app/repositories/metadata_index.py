from __future__ import annotations

import sqlite3
import threading
from typing import Any

from app.db.bootstrap import connect_metadata_index, metadata_index_path
from app.repositories.files import iso_now_utc, slugify
from app.repositories.runs import list_run_records, load_run_record
from app.services import evidence, research_plan, reviews, workbench

_TOUCH_LOCK = threading.Lock()


def touch_index_state(key: str, value: str | None = None) -> dict[str, str]:
    """Stamp a single index-state key without racing same-process writers.

    Concurrent topic-profile creates all write the same timestamp row after
    releasing the JSON library lock. Serialize that cheap upsert in-process so
    SQLite does not surface ``database is locked`` as a product error.
    """

    with _TOUCH_LOCK:
        with connect_metadata_index() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO index_state (key, value) VALUES (?, ?)",
                (key, value or iso_now_utc()),
            )
        return get_index_state()


def ensure_index_ready() -> dict[str, Any]:
    with connect_metadata_index() as connection:
        indexed_run_count = int(connection.execute("SELECT COUNT(*) AS count FROM runs").fetchone()["count"] or 0)
    if indexed_run_count > 0:
        return {"status": "ready", "indexed_runs": indexed_run_count, "rebuilt": False}
    result = backfill_all_runs()
    result["status"] = "rebuilt"
    result["rebuilt"] = True
    return result


def _load_section_rows(run_id: str, run: dict[str, Any]) -> list[dict[str, Any]]:
    plan = research_plan.load_plan_if_present(run_id)
    if plan is not None and plan.sections:
        return [
            {
                "section_id": section.section_id,
                "title": section.title,
                "claim_count": len(section.claims),
                "citation_count": sum(len(claim.citations) for claim in section.claims),
                "updated_at": plan.generated_at,
            }
            for section in plan.sections
        ]

    compare_ready_sections = run.get("compare_ready", {}).get("outline_sections", []) or []
    return [
        {
            "section_id": str(section.get("section_id", "")).strip(),
            "title": str(section.get("title", "")).strip() or "Untitled Section",
            "claim_count": 0,
            "citation_count": 0,
            "updated_at": run.get("updated_at", iso_now_utc()),
        }
        for section in compare_ready_sections
        if str(section.get("section_id", "")).strip()
    ]


def index_run(run_id: str) -> dict[str, Any]:
    run = load_run_record(run_id)
    triage_items = workbench.load_run_triage_items(run_id)
    evidence_rows = evidence.collect_run_evidence(run_id)
    reviewed_items = sum(1 for item in triage_items if reviews.get_review_status(run_id, item) in {"reviewed", "partial_review", "note_only"})
    report_sections = _load_section_rows(run_id, run)

    with connect_metadata_index() as connection:
        connection.execute("DELETE FROM run_topics WHERE run_id = ?", (run_id,))
        connection.execute("DELETE FROM papers WHERE run_id = ?", (run_id,))
        connection.execute("DELETE FROM evidence WHERE run_id = ?", (run_id,))
        connection.execute("DELETE FROM report_sections WHERE run_id = ?", (run_id,))
        connection.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))

        connection.execute(
            """
            INSERT INTO runs (
              run_id, topic, topic_slug, status, created_at, updated_at, started_at, finished_at,
              root_run_id, parent_run_id, lineage_mode, preview_hash, triage_items, reviewed_items,
              evidence_count, claim_count, report_section_count, research_plan_path, report_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                run.get("topic", ""),
                slugify(run.get("topic", "")),
                run.get("status", "unknown"),
                run.get("created_at", ""),
                run.get("updated_at", ""),
                run.get("started_at"),
                run.get("finished_at"),
                (run.get("lineage") or {}).get("root_run_id") or run_id,
                (run.get("lineage") or {}).get("parent_run_id"),
                (run.get("lineage") or {}).get("lineage_mode") or "standalone",
                (run.get("compare_ready") or {}).get("preview_hash"),
                int((run.get("counts") or {}).get("triage_items", len(triage_items)) or 0),
                reviewed_items,
                len(evidence_rows),
                sum(section["claim_count"] for section in report_sections),
                len(report_sections),
                (run.get("artifacts") or {}).get("research_plan_json"),
                (run.get("artifacts") or {}).get("research_plan_markdown") or (run.get("artifacts") or {}).get("report_markdown"),
            ),
        )
        connection.execute("INSERT INTO run_topics (run_id, topic) VALUES (?, ?)", (run_id, run.get("topic", "")))

        indexed_paper_ids: set[str] = set()
        for item in triage_items:
            paper_id = str(item.get("paper_id", "")).strip()
            if not paper_id:
                continue
            indexed_paper_ids.add(paper_id)
            dedup = item.get("dedup") or {}
            connection.execute(
                """
                INSERT INTO papers (
                  run_id, paper_id, title, theme, score, status, published, entry_url, pdf_url, dedup_cluster_id, is_primary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, paper_id) DO UPDATE SET
                  title = excluded.title,
                  theme = excluded.theme,
                  score = excluded.score,
                  status = excluded.status,
                  published = excluded.published,
                  entry_url = excluded.entry_url,
                  pdf_url = excluded.pdf_url,
                  dedup_cluster_id = excluded.dedup_cluster_id,
                  is_primary = excluded.is_primary
                """,
                (
                    run_id,
                    paper_id,
                    item.get("title", ""),
                    item.get("theme", "Uncategorized"),
                    int(item.get("score", 0) or 0),
                    reviews.get_review_status(run_id, item),
                    item.get("published"),
                    item.get("entry_url"),
                    item.get("pdf_url"),
                    dedup.get("cluster_id"),
                    1 if dedup.get("is_primary", True) else 0,
                ),
            )

        for row in evidence_rows:
            connection.execute(
                """
                INSERT INTO evidence (
                  run_id, evidence_id, source_id, paper_id, title, source_type, quality_hint, confidence, artifact_path, citation_text, snippet
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, evidence_id) DO UPDATE SET
                  source_id = excluded.source_id,
                  paper_id = excluded.paper_id,
                  title = excluded.title,
                  source_type = excluded.source_type,
                  quality_hint = excluded.quality_hint,
                  confidence = excluded.confidence,
                  artifact_path = excluded.artifact_path,
                  citation_text = excluded.citation_text,
                  snippet = excluded.snippet
                """,
                (
                    run_id,
                    row.evidence_id,
                    row.source_id,
                    row.paper_id,
                    row.title,
                    row.source_type,
                    row.quality_hint,
                    row.confidence,
                    row.artifact_path,
                    row.citation_text,
                    row.snippet,
                ),
            )

        for section in report_sections:
            connection.execute(
                """
                INSERT INTO report_sections (run_id, section_id, title, claim_count, citation_count, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    section["section_id"],
                    section["title"],
                    section["claim_count"],
                    section["citation_count"],
                    section["updated_at"],
                ),
            )
        connection.execute(
            "INSERT OR REPLACE INTO index_state (key, value) VALUES (?, ?)",
            ("last_indexed_at", iso_now_utc()),
        )
        connection.execute(
            "INSERT OR REPLACE INTO index_state (key, value) VALUES (?, ?)",
            ("last_indexed_run_id", run_id),
        )
    return {
        "run_id": run_id,
        "papers_indexed": len(indexed_paper_ids),
        "evidence_indexed": len(evidence_rows),
        "report_sections_indexed": len(report_sections),
    }


def refresh_run(run_id: str, *, reason: str = "incremental") -> dict[str, Any]:
    result = index_run(run_id)
    touch_index_state("last_incremental_refresh_at")
    touch_index_state("last_incremental_reason", reason)
    return result


def backfill_all_runs() -> dict[str, Any]:
    runs = list_run_records(limit=None)
    results = [index_run(run["run_id"]) for run in runs]
    with connect_metadata_index() as connection:
        connection.execute("INSERT OR REPLACE INTO index_state (key, value) VALUES (?, ?)", ("last_backfill_at", iso_now_utc()))
        connection.execute("INSERT OR REPLACE INTO index_state (key, value) VALUES (?, ?)", ("last_incremental_reason", "full_backfill"))
    return {"indexed_runs": len(results), "runs": results}


def _where_clause(field: str, values: list[str] | None) -> tuple[str, list[str]]:
    normalized = [value.strip() for value in values or [] if value and value.strip()]
    if not normalized:
        return "", []
    placeholders = ", ".join("?" for _ in normalized)
    return f" WHERE {field} IN ({placeholders})", normalized


def _scoped_where_clause(
    filters: list[tuple[str, list[str] | None]],
) -> tuple[str, list[str]]:
    clauses: list[str] = []
    params: list[str] = []
    for field, values in filters:
        if values is None:
            continue
        normalized = [value.strip() for value in values if value and value.strip()]
        if not normalized:
            clauses.append("1 = 0")
            continue
        placeholders = ", ".join("?" for _ in normalized)
        clauses.append(f"{field} IN ({placeholders})")
        params.extend(normalized)
    return (f" WHERE {' AND '.join(clauses)}" if clauses else ""), params


def get_index_state(*, observe_only: bool = False) -> dict[str, str]:
    """Return metadata index_state pairs.

    ``observe_only=True`` is the contract for system probes: it never creates
    the DB or its schema, and reports an absent/uninitialised store as an empty
    mapping instead of bootstrapping one as a side effect of being read.
    """
    if observe_only:
        if not metadata_index_path(ensure_parent=False).exists():
            return {}
        try:
            with connect_metadata_index(read_only=True) as connection:
                rows = connection.execute("SELECT key, value FROM index_state").fetchall()
        except (FileNotFoundError, sqlite3.Error):
            return {}
        return {str(row["key"]): str(row["value"]) for row in rows}
    with connect_metadata_index() as connection:
        rows = connection.execute("SELECT key, value FROM index_state").fetchall()
    return {str(row["key"]): str(row["value"]) for row in rows}


def get_overview(
    *,
    topics: list[str] | None = None,
    run_ids: list[str] | None = None,
) -> dict[str, Any]:
    empty_response = {
        "totals": {
            "runs": 0,
            "topics": 0,
            "papers": 0,
            "evidence": 0,
            "report_sections": 0,
        },
        "status_counts": [],
        "top_topics": [],
        "top_themes": [],
        "recent_runs": [],
        "index_state": {},
    }
    if not metadata_index_path(ensure_parent=False).exists():
        return empty_response
    clause, params = _scoped_where_clause(
        [
            ("topic", topics or None),
            ("run_id", run_ids),
        ]
    )
    with connect_metadata_index(read_only=True) as connection:
        totals = connection.execute(
            f"""
            SELECT
              COUNT(*) AS total_runs,
              COUNT(DISTINCT topic) AS total_topics,
              COALESCE(SUM(triage_items), 0) AS total_papers,
              COALESCE(SUM(evidence_count), 0) AS total_evidence,
              COALESCE(SUM(report_section_count), 0) AS total_report_sections
            FROM runs
            {clause}
            """,
            params,
        ).fetchone()
        recent_runs = connection.execute(
            f"""
            SELECT run_id, topic, status, created_at, updated_at, triage_items, reviewed_items, evidence_count, claim_count
            FROM runs
            {clause}
            ORDER BY created_at DESC
            LIMIT 8
            """,
            params,
        ).fetchall()
        status_rows = connection.execute(
            f"SELECT status, COUNT(*) AS count FROM runs {clause} GROUP BY status ORDER BY count DESC",
            params,
        ).fetchall()
        topic_rows = connection.execute(
            f"SELECT topic, COUNT(*) AS count FROM runs {clause} GROUP BY topic ORDER BY count DESC LIMIT 8",
            params,
        ).fetchall()
        theme_rows = connection.execute(
            """
            SELECT theme, COUNT(*) AS count
            FROM papers
            WHERE run_id IN (SELECT run_id FROM runs{subclause})
            GROUP BY theme
            ORDER BY count DESC
            LIMIT 8
            """.format(subclause=clause.replace(" WHERE ", " WHERE ") if clause else ""),
            params,
        ).fetchall()
    return {
        "totals": {
            "runs": int(totals["total_runs"] or 0),
            "topics": int(totals["total_topics"] or 0),
            "papers": int(totals["total_papers"] or 0),
            "evidence": int(totals["total_evidence"] or 0),
            "report_sections": int(totals["total_report_sections"] or 0),
        },
        "status_counts": [{"status": str(row["status"]), "count": int(row["count"])} for row in status_rows],
        "top_topics": [{"topic": str(row["topic"]), "count": int(row["count"])} for row in topic_rows],
        "top_themes": [{"theme": str(row["theme"]), "count": int(row["count"])} for row in theme_rows],
        "recent_runs": [dict(row) for row in recent_runs],
        "index_state": get_index_state(observe_only=True),
    }


def search_runs(
    *,
    q: str = "",
    topics: list[str] | None = None,
    limit: int = 20,
    run_ids: list[str] | None = None,
) -> dict[str, Any]:
    if not metadata_index_path(ensure_parent=False).exists():
        return {"items": []}
    topic_clause, params = _scoped_where_clause(
        [
            ("topic", topics or None),
            ("run_id", run_ids),
        ]
    )
    clauses: list[str] = []
    if topic_clause:
        clauses.append(topic_clause.replace(" WHERE ", ""))
    if q.strip():
        clauses.append("(topic LIKE ? OR run_id LIKE ?)")
        params.extend([f"%{q.strip()}%", f"%{q.strip()}%"])
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with connect_metadata_index(read_only=True) as connection:
        rows = connection.execute(
            f"""
            SELECT run_id, topic, status, created_at, updated_at, triage_items, reviewed_items, evidence_count, claim_count
            FROM runs
            {where_sql}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
    return {"items": [dict(row) for row in rows]}


def search_papers(
    *,
    q: str = "",
    themes: list[str] | None = None,
    limit: int = 20,
    run_ids: list[str] | None = None,
) -> dict[str, Any]:
    if not metadata_index_path(ensure_parent=False).exists():
        return {"items": []}
    theme_clause, params = _scoped_where_clause(
        [
            ("theme", themes or None),
            ("run_id", run_ids),
        ]
    )
    clauses: list[str] = []
    if theme_clause:
        clauses.append(theme_clause.replace(" WHERE ", ""))
    if q.strip():
        clauses.append("(title LIKE ? OR paper_id LIKE ?)")
        params.extend([f"%{q.strip()}%", f"%{q.strip()}%"])
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with connect_metadata_index(read_only=True) as connection:
        rows = connection.execute(
            f"""
            SELECT run_id, paper_id, title, theme, score, status, published
            FROM papers
            {where_sql}
            ORDER BY score DESC, published DESC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
    return {"items": [dict(row) for row in rows]}
