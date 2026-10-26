"""Additive immutable records in the existing metadata database, never a second store."""
from __future__ import annotations

from dataclasses import asdict, fields
import json
import sqlite3
from typing import Callable, Any

from app.db.bootstrap import connect_metadata_index
from app.domain.temporal_memory import Member, TopicSnapshot, canonical_json, snapshot, diff
from app.repositories.files import iso_now_utc

_SNAPSHOT_FIELDS = tuple(f.name for f in fields(TopicSnapshot) if f.name != "members")
_MEMBER_FIELDS = tuple(f.name for f in fields(Member))
_SETS = {"occurrence_ids", "collection_ids", "evidence_ids", "evidence_digests", "source_entity_ids"}


def checkpoint(phase: str) -> None:
    """Failure injection seam; no production side effect."""


def initialize(connection: sqlite3.Connection) -> None:
    """Transactional, idempotent extension; original bootstrap schema stays exact."""
    connection.execute("""CREATE TABLE IF NOT EXISTS topic_snapshots (
        snapshot_id TEXT PRIMARY KEY, topic_id TEXT NOT NULL, created_at TEXT NOT NULL,
        schema_version INTEGER NOT NULL, source_revision TEXT NOT NULL, corpus_digest TEXT NOT NULL,
        paper_count INTEGER NOT NULL, review_state_digest TEXT NOT NULL, evidence_digest TEXT NOT NULL,
        acceptance_state TEXT NOT NULL, parent_snapshot_id TEXT,
        algorithm_version TEXT NOT NULL, confidence REAL NOT NULL, confidence_class TEXT NOT NULL,
        UNIQUE(topic_id, schema_version, corpus_digest))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS topic_snapshot_members (
        snapshot_id TEXT NOT NULL,
        canonical_paper_id TEXT NOT NULL, paper_version_id TEXT NOT NULL,
        occurrence_ids TEXT NOT NULL, collection_ids TEXT NOT NULL, review_state TEXT NOT NULL,
        full_text_state TEXT NOT NULL, full_text_content_digest TEXT,
        evidence_ids TEXT NOT NULL, evidence_digests TEXT NOT NULL, evidence_state TEXT NOT NULL,
        rights_access_state TEXT NOT NULL, rights_declaration_digest TEXT, source_entity_ids TEXT NOT NULL,
        membership_digest TEXT NOT NULL, PRIMARY KEY(snapshot_id, canonical_paper_id, paper_version_id))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS topic_change_events (
        event_id TEXT PRIMARY KEY, topic_id TEXT NOT NULL,
        from_snapshot_id TEXT NOT NULL,
        to_snapshot_id TEXT NOT NULL,
        event_type TEXT NOT NULL, ordinal INTEGER NOT NULL, payload TEXT NOT NULL,
        UNIQUE(to_snapshot_id, ordinal))""")
    # The existing FK inventory intentionally rejects additional FK declarations.
    # Equivalent insert guards plus immutable parents enforce these relationships
    # without weakening that policy or rewriting the existing schema.
    connection.execute("""CREATE TRIGGER IF NOT EXISTS topic_snapshot_parent_guard
        BEFORE INSERT ON topic_snapshots WHEN NEW.parent_snapshot_id IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM topic_snapshots WHERE snapshot_id=NEW.parent_snapshot_id AND topic_id=NEW.topic_id)
        BEGIN SELECT RAISE(ABORT, 'snapshot_parent_invalid'); END""")
    connection.execute("""CREATE TRIGGER IF NOT EXISTS topic_member_parent_guard
        BEFORE INSERT ON topic_snapshot_members WHEN NOT EXISTS
        (SELECT 1 FROM topic_snapshots WHERE snapshot_id=NEW.snapshot_id)
        BEGIN SELECT RAISE(ABORT, 'snapshot_member_parent_invalid'); END""")
    connection.execute("""CREATE TRIGGER IF NOT EXISTS topic_member_sealed_guard
        BEFORE INSERT ON topic_snapshot_members WHEN
        (SELECT count(*) FROM topic_snapshot_members WHERE snapshot_id=NEW.snapshot_id) >=
        (SELECT paper_count FROM topic_snapshots WHERE snapshot_id=NEW.snapshot_id)
        BEGIN SELECT RAISE(ABORT, 'temporal_membership_immutable'); END""")
    connection.execute("""CREATE TRIGGER IF NOT EXISTS topic_event_parent_guard
        BEFORE INSERT ON topic_change_events WHEN NOT EXISTS
        (SELECT 1 FROM topic_snapshots WHERE snapshot_id=NEW.to_snapshot_id AND topic_id=NEW.topic_id AND parent_snapshot_id=NEW.from_snapshot_id)
        BEGIN SELECT RAISE(ABORT, 'snapshot_event_parent_invalid'); END""")
    for table in ("topic_snapshots", "topic_snapshot_members", "topic_change_events"):
        for operation in ("UPDATE", "DELETE"):
            connection.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()}
                BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'temporal_record_immutable'); END""")


def _exists(connection: sqlite3.Connection) -> bool:
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='topic_snapshots'").fetchone() is not None


def _load(connection: sqlite3.Connection, snapshot_id: str) -> TopicSnapshot:
    row = connection.execute("SELECT * FROM topic_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
    if row is None:
        raise KeyError("snapshot_not_found")
    members = []
    for entry in connection.execute(
        "SELECT * FROM topic_snapshot_members WHERE snapshot_id=? ORDER BY canonical_paper_id, paper_version_id, membership_digest",
        (snapshot_id,),
    ):
        data = dict(entry)
        data.pop("snapshot_id")
        for name in _SETS:
            data[name] = tuple(json.loads(data[name]))
        data["evidence_digests"] = tuple(tuple(item) for item in data["evidence_digests"])
        members.append(Member(**data))
    return TopicSnapshot(**dict(row), members=tuple(members))


def capture(topic_id: str, project: Callable[[sqlite3.Connection], list[Member]]) -> tuple[TopicSnapshot, bool]:
    with connect_metadata_index() as connection:
        connection.execute("BEGIN IMMEDIATE")
        return capture_in_transaction(connection, topic_id, project)


def capture_in_transaction(connection: sqlite3.Connection, topic_id: str,
                           project: Callable[[sqlite3.Connection], list[Member]]) -> tuple[TopicSnapshot, bool]:
    """Caller owns BEGIN IMMEDIATE and the complete commit/rollback boundary."""
    if not connection.in_transaction:
        raise ValueError("snapshot_transaction_required")
    initialize(connection)
    current = snapshot(topic_id, project(connection), iso_now_utc())
    existing = connection.execute("SELECT snapshot_id FROM topic_snapshots WHERE snapshot_id=?", (current.snapshot_id,)).fetchone()
    if existing:
        return _load(connection, str(existing[0])), False
    previous = connection.execute(
        "SELECT snapshot_id FROM topic_snapshots WHERE topic_id=? ORDER BY rowid DESC LIMIT 1", (topic_id,),
    ).fetchone()
    parent = _load(connection, str(previous[0])) if previous else None
    current = snapshot(topic_id, current.members, current.created_at, parent.snapshot_id if parent else None)
    values = asdict(current)
    columns = ",".join(_SNAPSHOT_FIELDS)
    connection.execute(f"INSERT INTO topic_snapshots ({columns}) VALUES ({','.join('?' for _ in _SNAPSHOT_FIELDS)})",
                       [values[name] for name in _SNAPSHOT_FIELDS])
    checkpoint("after_snapshot")
    for member in current.members:
        raw = asdict(member)
        connection.execute(
            f"INSERT INTO topic_snapshot_members (snapshot_id,{','.join(_MEMBER_FIELDS)}) VALUES ({','.join('?' for _ in range(len(_MEMBER_FIELDS)+1))})",
            [current.snapshot_id, *[canonical_json(raw[name]) if name in _SETS else raw[name] for name in _MEMBER_FIELDS]],
        )
    checkpoint("after_members")
    for ordinal, event in enumerate(diff(parent, current) if parent else ()):
        connection.execute("INSERT INTO topic_change_events VALUES (?,?,?,?,?,?,?)", (
            event.event_id, topic_id, event.from_snapshot_id, event.to_snapshot_id,
            event.event_type, ordinal, canonical_json(asdict(event)),
        ))
    checkpoint("after_events")
    return current, True


def list_snapshots(topic_id: str, page: int = 1, page_size: int = 25) -> dict[str, Any]:
    if page < 1 or not 1 <= page_size <= 100:
        raise ValueError("pagination_invalid")
    with connect_metadata_index(read_only=True) as connection:
        if not _exists(connection):
            return {"items": [], "total": 0, "page": page, "pageSize": page_size}
        total = connection.execute("SELECT count(*) FROM topic_snapshots WHERE topic_id=?", (topic_id,)).fetchone()[0]
        rows = connection.execute(
            "SELECT snapshot_id FROM topic_snapshots WHERE topic_id=? ORDER BY created_at DESC, snapshot_id ASC LIMIT ? OFFSET ?",
            (topic_id, page_size, (page-1)*page_size),
        ).fetchall()
        return {"items": [_load(connection, str(row[0])) for row in rows], "total": total, "page": page, "pageSize": page_size}


def detail(topic_id: str, snapshot_id: str) -> dict[str, Any]:
    with connect_metadata_index(read_only=True) as connection:
        if not _exists(connection):
            raise KeyError("snapshot_not_found")
        current = _load(connection, snapshot_id)
        if current.topic_id != topic_id:
            raise KeyError("snapshot_not_found")
        events = [json.loads(row[0]) for row in connection.execute(
            "SELECT payload FROM topic_change_events WHERE to_snapshot_id=? ORDER BY ordinal", (snapshot_id,),
        )]
        return {"snapshot": current, "events": events}


def pair(topic_id: str, from_id: str, to_id: str) -> tuple[TopicSnapshot, TopicSnapshot]:
    with connect_metadata_index(read_only=True) as connection:
        if not _exists(connection):
            raise KeyError("snapshot_not_found")
        before, after = _load(connection, from_id), _load(connection, to_id)
        if before.topic_id != topic_id or after.topic_id != topic_id:
            raise ValueError("cross_topic_pair")
        return before, after
