"""Append-only recommendation output and decisions in the metadata extension store."""

from contextlib import closing
import json
import sqlite3
from typing import Any

from app.db.bootstrap import connect_metadata_index
from app.domain import recommendation_versions as domain
from app.domain.temporal_memory import canonical_json, digest
from app.repositories.files import iso_now_utc


def checkpoint(phase: str) -> None:
    """Transaction fault-injection seam."""


def _exists(c: sqlite3.Connection) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE name='recommendation_versions' AND type='table'").fetchone() is not None


def _initialize(c: sqlite3.Connection) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS recommendation_versions (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, version_id TEXT NOT NULL UNIQUE,
        topic_id TEXT NOT NULL, candidate_type TEXT NOT NULL CHECK(candidate_type IN ('gap','new_topic')),
        candidate_id TEXT NOT NULL, content_json TEXT NOT NULL, created_at TEXT NOT NULL, actor_id TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS recommendation_decisions (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT NOT NULL UNIQUE,
        topic_id TEXT NOT NULL, version_id TEXT NOT NULL, request_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('accepted','held','rejected')),
        note TEXT NOT NULL CHECK(length(note)<=2000), previous_id TEXT,
        actor_id TEXT NOT NULL, created_at TEXT NOT NULL,
        schema_version TEXT NOT NULL CHECK(schema_version='M3C_RECOMMENDATION_DECISION_V1'),
        UNIQUE(topic_id,version_id,request_id))""")
    c.execute("CREATE INDEX IF NOT EXISTS recommendation_versions_topic ON recommendation_versions(topic_id,sequence)")
    c.execute("CREATE INDEX IF NOT EXISTS recommendation_decisions_version ON recommendation_decisions(topic_id,version_id,sequence)")
    # The shared store reserves declared FK inventory. Match the existing M3
    # extension contract: immutable parents plus INSERT reference guards.
    for table in ("recommendation_versions", "recommendation_decisions"):
        for operation in ("UPDATE", "DELETE"):
            c.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()}
                BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'recommendation_immutable'); END""")
    c.execute("""CREATE TRIGGER IF NOT EXISTS recommendation_decisions_binding BEFORE INSERT ON recommendation_decisions
        WHEN NOT EXISTS (SELECT 1 FROM recommendation_versions WHERE version_id=NEW.version_id AND topic_id=NEW.topic_id)
        OR NEW.previous_id IS NOT (SELECT decision_id FROM recommendation_decisions
            WHERE topic_id=NEW.topic_id AND version_id=NEW.version_id ORDER BY sequence DESC LIMIT 1)
        BEGIN SELECT RAISE(ABORT,'decision_binding_invalid'); END""")


def _row(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["content"] = json.loads(value.pop("content_json"))
    return value


def _get(c: sqlite3.Connection, topic: str, version_id: str) -> dict[str, Any]:
    row = c.execute("SELECT * FROM recommendation_versions WHERE topic_id=? AND version_id=?", (topic, version_id)).fetchone() if _exists(c) else None
    if row is None:
        raise KeyError("recommendation_version_not_found")
    return _row(row)


def get(topic: str, version_id: str) -> dict[str, Any]:
    with closing(connect_metadata_index(read_only=True)) as c:
        return _get(c, topic, version_id)


def save(value: dict[str, Any], actor_id: str) -> tuple[dict[str, Any], bool]:
    domain.validate(value)
    if not actor_id:
        raise ValueError("invalid_recommendation_version")
    version_id = domain.identity(value)
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        _initialize(c)
        existing = c.execute("SELECT * FROM recommendation_versions WHERE version_id=?", (version_id,)).fetchone()
        if existing:
            return _row(existing), False
        c.execute("""INSERT INTO recommendation_versions
            (version_id,topic_id,candidate_type,candidate_id,content_json,created_at,actor_id) VALUES (?,?,?,?,?,?,?)""",
                  (version_id, value["topic_id"], value["candidate_type"], value["candidate_id"], canonical_json(value), iso_now_utc(), actor_id))
        checkpoint("after_version")
        return _get(c, value["topic_id"], version_id), True


def _page(limit: int, page: int) -> dict[str, Any]:
    if not 1 <= limit <= 100 or page < 1:
        raise ValueError("pagination_invalid")
    return dict(items=[], total=0, limit=limit, page=page)


def versions(topic: str, limit: int = 10, page: int = 1) -> dict[str, Any]:
    result = _page(limit, page)
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        if _exists(c):
            result["total"] = c.execute("SELECT count(*) FROM recommendation_versions WHERE topic_id=?", (topic,)).fetchone()[0]
            result["items"] = [_row(row) for row in c.execute(
                "SELECT * FROM recommendation_versions WHERE topic_id=? ORDER BY sequence DESC LIMIT ? OFFSET ?",
                (topic, limit, (page - 1) * limit))]
    return result


def decisions(topic: str, version_id: str, limit: int = 10, page: int = 1) -> dict[str, Any]:
    result = dict(_page(limit, page), latest_id=None)
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        _get(c, topic, version_id)
        args = (topic, version_id)
        result["total"] = c.execute("SELECT count(*) FROM recommendation_decisions WHERE topic_id=? AND version_id=?", args).fetchone()[0]
        latest = c.execute("SELECT decision_id FROM recommendation_decisions WHERE topic_id=? AND version_id=? ORDER BY sequence DESC LIMIT 1", args).fetchone()
        result["latest_id"] = latest[0] if latest else None
        result["items"] = [dict(row) for row in c.execute(
            "SELECT * FROM recommendation_decisions WHERE topic_id=? AND version_id=? ORDER BY sequence DESC LIMIT ? OFFSET ?",
            (*args, limit, (page - 1) * limit))]
    return result


def decide(topic: str, version_id: str, request_id: str, state: str, note: str,
           expected_previous_id: str | None, actor_id: str) -> tuple[dict[str, Any], bool]:
    if state not in {"accepted", "held", "rejected"} or not 1 <= len(request_id) <= 100 or len(note) > 2000 or not actor_id:
        raise ValueError("invalid_decision")
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        _get(c, topic, version_id)
        existing = c.execute("SELECT * FROM recommendation_decisions WHERE topic_id=? AND version_id=? AND request_id=?",
                             (topic, version_id, request_id)).fetchone()
        if existing:
            if (existing["state"], existing["note"], existing["previous_id"], existing["actor_id"]) != (state, note, expected_previous_id, actor_id):
                raise ValueError("decision_request_conflict")
            return dict(existing), False
        latest = c.execute("SELECT decision_id FROM recommendation_decisions WHERE topic_id=? AND version_id=? ORDER BY sequence DESC LIMIT 1", (topic, version_id)).fetchone()
        if (latest[0] if latest else None) != expected_previous_id:
            raise ValueError("decision_conflict")
        identity = "rdec_" + digest([topic, version_id, request_id])
        c.execute("""INSERT INTO recommendation_decisions
            (decision_id,topic_id,version_id,request_id,state,note,previous_id,actor_id,created_at,schema_version)
            VALUES (?,?,?,?,?,?,?,?,?,'M3C_RECOMMENDATION_DECISION_V1')""",
                  (identity, topic, version_id, request_id, state, note, expected_previous_id, actor_id, iso_now_utc()))
        checkpoint("after_decision")
        return dict(c.execute("SELECT * FROM recommendation_decisions WHERE decision_id=?", (identity,)).fetchone()), True
