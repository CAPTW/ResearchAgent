"""Append-only extraction links; artifacts are published before the DB link."""
from contextlib import closing
import json
import sqlite3
from typing import Any

from app.db.bootstrap import connect_metadata_index
from app.domain import review_pdf_extraction as domain
from app.repositories.review_preparations import _scope


def checkpoint(phase: str) -> None:
    """Fault seam for rollback and identical artifact recovery."""


def _exists(c: sqlite3.Connection) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_pdf_extractions'").fetchone() is not None


def _initialize(c: sqlite3.Connection) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS paper_pdf_extractions (
        extraction_id TEXT PRIMARY KEY, topic_id TEXT NOT NULL, paper_version_id TEXT NOT NULL,
        run_id TEXT NOT NULL, paper_id TEXT NOT NULL, payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL)""")
    c.execute("CREATE INDEX IF NOT EXISTS review_pdf_extraction_scope ON paper_pdf_extractions(topic_id,paper_version_id,run_id,paper_id)")
    for operation in ("UPDATE", "DELETE"):
        c.execute(f"""CREATE TRIGGER IF NOT EXISTS review_pdf_extractions_no_{operation.lower()}
            BEFORE {operation} ON paper_pdf_extractions
            BEGIN SELECT RAISE(ABORT,'review_pdf_extraction_immutable'); END""")


def _read(row: sqlite3.Row) -> dict[str, Any]:
    try:
        value = domain.validate(json.loads(row["payload_json"]))
        if (domain.encoded(value) != row["payload_json"] or domain.sha(domain.encoded(value)) != row["payload_sha256"]
                or any(value[key] != row[key] for key in ("extraction_id", "topic_id", "paper_version_id"))
                or any(value["source"][key] != row[key] for key in ("run_id", "paper_id"))):
            raise ValueError()
        return value
    except (KeyError, ValueError, TypeError, IndexError, RecursionError):
        raise ValueError(domain.ERROR) from None


def detail_in(c: sqlite3.Connection, topic: str, version: str, extraction_id: str,
              occurrences: list[tuple[str, str]]) -> dict[str, Any]:
    if occurrences and _exists(c):
        where, params = _scope(topic, version, occurrences)
        row = c.execute(f"SELECT * FROM paper_pdf_extractions WHERE {where} AND extraction_id=?", (*params, extraction_id)).fetchone()
        if row is not None:
            return _read(row)
    raise KeyError("review_pdf_extraction_not_found")


def detail(topic: str, version: str, extraction_id: str, occurrences: list[tuple[str, str]]) -> dict[str, Any]:
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        return detail_in(c, topic, version, extraction_id, occurrences)


def listing(topic: str, version: str, occurrences: list[tuple[str, str]]) -> list[dict[str, Any]]:
    if not occurrences:
        return []
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        if not _exists(c):
            return []
        where, params = _scope(topic, version, occurrences)
        rows = c.execute(f"SELECT * FROM paper_pdf_extractions WHERE {where} ORDER BY rowid DESC LIMIT ?",
                         (*params, domain.MAX_RECORDS + 1)).fetchall()
        if len(rows) > domain.MAX_RECORDS:
            raise ValueError("review_pdf_extraction_limit_exceeded")
        return [_read(row) for row in rows]


def save(record: dict[str, Any], occurrences: list[tuple[str, str]]) -> tuple[dict[str, Any], bool]:
    value = domain.validate(record)
    source = value["source"]
    if (source["run_id"], source["paper_id"]) not in occurrences:
        raise KeyError("review_pdf_extraction_not_found")
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        _initialize(c)
        try:
            old = detail_in(c, value["topic_id"], value["paper_version_id"], value["extraction_id"], occurrences)
        except KeyError:
            old = None
        if old is not None:
            # Creation metadata belongs to the first successful insertion.
            comparable = {**value, "created_at": old["created_at"], "created_by": old["created_by"]}
            if comparable != old:
                raise ValueError("review_pdf_extraction_conflict")
            return old, False
        count = c.execute("SELECT COUNT(*) FROM paper_pdf_extractions WHERE topic_id=? AND paper_version_id=?",
                          (value["topic_id"], value["paper_version_id"])).fetchone()[0]
        if count >= domain.MAX_RECORDS:
            raise ValueError("review_pdf_extraction_limit_exceeded")
        payload = domain.encoded(value)
        c.execute("INSERT INTO paper_pdf_extractions VALUES(?,?,?,?,?,?,?)", (value["extraction_id"], value["topic_id"],
            value["paper_version_id"], source["run_id"], source["paper_id"], payload, domain.sha(payload)))
        checkpoint("after_insert")
        return value, True
