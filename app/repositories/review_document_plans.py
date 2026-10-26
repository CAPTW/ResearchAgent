"""Immutable plans with atomic, actor-bound request replay on the existing DB."""
from contextlib import closing
import json
import sqlite3
from typing import Any, Callable

from app.db.bootstrap import connect_metadata_index
from app.domain.review_document_plan import Plan
from app.domain.review_preparation import encoded, sha


def checkpoint(phase: str) -> None:
    """Transaction rollback seam."""


def exists(c: sqlite3.Connection) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_document_plans'").fetchone() is not None


def _init(c: sqlite3.Connection) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS paper_document_plans (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT NOT NULL UNIQUE,
        topic_id TEXT NOT NULL, paper_version_id TEXT NOT NULL, actor_id TEXT NOT NULL,
        request_id TEXT NOT NULL, request_json TEXT NOT NULL, payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
        UNIQUE(topic_id,paper_version_id,actor_id,request_id))""")
    for operation in ("UPDATE", "DELETE"):
        c.execute(f"""CREATE TRIGGER IF NOT EXISTS document_plans_no_{operation.lower()}
            BEFORE {operation} ON paper_document_plans BEGIN SELECT RAISE(ABORT,'document_plan_immutable'); END""")


def _read(row: sqlite3.Row) -> dict[str, Any]:
    from app.services.review_document_plan import validate
    try:
        value = validate(json.loads(row["payload_json"]))
        if (sha(encoded(value)) != row["payload_sha256"]
                or any(value[k] != row[k] for k in ("plan_id", "topic_id", "paper_version_id", "actor_id"))
                or value["request"] != json.loads(row["request_json"])
                or value["request"]["request_id"] != row["request_id"]):
            raise ValueError()
        return value
    except (KeyError, TypeError, ValueError, IndexError):
        raise ValueError("document_plan_integrity_invalid") from None


def save(topic: str, version: str, actor: str, body: Plan, factory: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    # Freeze source and compute a potentially large split outside the shared
    # writer transaction. Recheck replay after acquiring the lock so duplicates
    # still commit exactly one plan. A first replay never needs the source file.
    query = "SELECT * FROM paper_document_plans WHERE topic_id=? AND paper_version_id=? AND actor_id=? AND request_id=?"
    params = (topic, version, actor, body.request_id)
    with closing(connect_metadata_index(read_only=True)) as reader:
        row = reader.execute(query, params).fetchone() if exists(reader) else None
    if row is not None:
        value = _read(row)
        if value["request"] != body.model_dump():
            raise ValueError("document_plan_request_conflict")
        return value
    value = factory()
    payload = encoded(value)
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        _init(c)
        row = c.execute(query, params).fetchone()
        if row is not None:
            value = _read(row)
            if value["request"] != body.model_dump():
                raise ValueError("document_plan_request_conflict")
            return value
        c.execute("INSERT INTO paper_document_plans(plan_id,topic_id,paper_version_id,actor_id,request_id,request_json,payload_json,payload_sha256) VALUES(?,?,?,?,?,?,?,?)",
            (value["plan_id"], topic, version, actor, body.request_id, encoded(body.model_dump()), payload, sha(payload)))
        checkpoint("after_insert")
        return value


def detail(topic: str, version: str, actor: str, identity: str) -> dict[str, Any]:
    with closing(connect_metadata_index(read_only=True)) as c:
        if exists(c):
            row = c.execute("SELECT * FROM paper_document_plans WHERE topic_id=? AND paper_version_id=? AND actor_id=? AND plan_id=?",
                (topic, version, actor, identity)).fetchone()
            if row is not None:
                return _read(row)
    raise KeyError("document_plan_not_found")


def listing(topic: str, version: str, actor: str, page: int) -> dict[str, Any]:
    with closing(connect_metadata_index(read_only=True)) as c:
        if not exists(c):
            return dict(items=[], hasMore=False)
        rows = c.execute("SELECT * FROM paper_document_plans WHERE topic_id=? AND paper_version_id=? AND actor_id=? ORDER BY sequence DESC LIMIT 11 OFFSET ?",
            (topic, version, actor, (page - 1) * 10)).fetchall()
        items = [_read(row) for row in rows[:10]]
    return dict(items=items, hasMore=len(rows) > 10)
