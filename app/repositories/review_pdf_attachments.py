"""Append-only user PDF bindings; validated bytes precede the DB link."""
from contextlib import closing
import json
import sqlite3
from typing import Any

from app.db.bootstrap import connect_metadata_index
from app.domain import review_pdf_attachment as domain
from app.repositories.review_preparations import _scope


def checkpoint(phase: str) -> None:
    """Fault seam for rollback and identical artifact recovery."""


def _exists(c: sqlite3.Connection) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_pdf_attachments'").fetchone() is not None


def _initialize(c: sqlite3.Connection) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS paper_pdf_attachments (
        attachment_id TEXT PRIMARY KEY, topic_id TEXT NOT NULL, paper_version_id TEXT NOT NULL,
        run_id TEXT NOT NULL, paper_id TEXT NOT NULL, payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL)""")
    c.execute("CREATE INDEX IF NOT EXISTS review_pdf_attachment_scope ON paper_pdf_attachments(topic_id,paper_version_id,run_id,paper_id)")
    for operation in ("UPDATE", "DELETE"):
        c.execute(f"""CREATE TRIGGER IF NOT EXISTS review_pdf_attachments_no_{operation.lower()}
            BEFORE {operation} ON paper_pdf_attachments
            BEGIN SELECT RAISE(ABORT,'review_pdf_attachment_immutable'); END""")


def _read(row: sqlite3.Row) -> dict[str, Any]:
    try:
        value = domain.validate(json.loads(row["payload_json"]))
        if (domain.encoded(value) != row["payload_json"] or domain.sha(domain.encoded(value)) != row["payload_sha256"]
                or any(value[key] != row[key] for key in ("attachment_id", "topic_id", "paper_version_id"))
                or any(value[key] != row[key] for key in ("run_id", "paper_id"))):
            raise ValueError()
        return value
    except (KeyError, ValueError, TypeError, IndexError, RecursionError):
        raise ValueError(domain.ERROR) from None


def detail_in(c: sqlite3.Connection, topic: str, version: str, attachment_id: str,
              occurrences: list[tuple[str, str]]) -> dict[str, Any]:
    if occurrences and _exists(c):
        where, params = _scope(topic, version, occurrences)
        row = c.execute(f"SELECT * FROM paper_pdf_attachments WHERE {where} AND attachment_id=?", (*params, attachment_id)).fetchone()
        if row is not None:
            return _read(row)
    raise KeyError("review_pdf_attachment_not_found")


def detail(topic: str, version: str, attachment_id: str, occurrences: list[tuple[str, str]]) -> dict[str, Any]:
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        return detail_in(c, topic, version, attachment_id, occurrences)


def listing(topic: str, version: str, occurrences: list[tuple[str, str]]) -> list[dict[str, Any]]:
    if not occurrences:
        return []
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        if not _exists(c):
            return []
        where, params = _scope(topic, version, occurrences)
        rows = c.execute(f"SELECT * FROM paper_pdf_attachments WHERE {where} ORDER BY rowid DESC LIMIT ?",
                         (*params, domain.MAX_RECORDS + 1)).fetchall()
        if len(rows) > domain.MAX_RECORDS:
            raise ValueError("review_pdf_attachment_limit_exceeded")
        return [_read(row) for row in rows]


def save(record: dict[str, Any], occurrences: list[tuple[str, str]]) -> tuple[dict[str, Any], bool]:
    value = domain.validate(record)
    source = value
    if (source["run_id"], source["paper_id"]) not in occurrences:
        raise KeyError("review_pdf_attachment_not_found")
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        _initialize(c)
        try:
            old = detail_in(c, value["topic_id"], value["paper_version_id"], value["attachment_id"], occurrences)
        except KeyError:
            old = None
        if old is not None:
            # Creation metadata belongs to the first successful insertion.
            comparable = {**value, "created_at": old["created_at"], "created_by": old["created_by"]}
            if comparable != old:
                raise ValueError("review_pdf_attachment_conflict")
            return old, False
        count = c.execute("SELECT COUNT(*) FROM paper_pdf_attachments WHERE topic_id=? AND paper_version_id=?",
                          (value["topic_id"], value["paper_version_id"])).fetchone()[0]
        if count >= domain.MAX_RECORDS:
            raise ValueError("review_pdf_attachment_limit_exceeded")
        payload = domain.encoded(value)
        c.execute("INSERT INTO paper_pdf_attachments VALUES(?,?,?,?,?,?,?)", (value["attachment_id"], value["topic_id"],
            value["paper_version_id"], source["run_id"], source["paper_id"], payload, domain.sha(payload)))
        checkpoint("after_insert")
        return value, True
