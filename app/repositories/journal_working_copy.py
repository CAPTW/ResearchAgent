"""Actor-scoped manual recovery checkpoints; never journal acceptance or execution."""
from contextlib import closing
from datetime import datetime
import json
import sqlite3
from typing import Any

from app.db.bootstrap import connect_metadata_index
from app.domain import journal_working_copy as domain
from app.domain.local_journal import references
from app.repositories import local_journal
from app.repositories.files import iso_now_utc
from app.repositories.synthesis_reports import _candidate


def checkpoint(phase: str) -> None:
    """Fault injection before commit."""


def _exists(c: sqlite3.Connection) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='journal_working_copies'").fetchone() is not None


def _initialize(c: sqlite3.Connection) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS journal_working_copies (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, copy_id TEXT NOT NULL UNIQUE,
        topic_id TEXT NOT NULL, job_id TEXT NOT NULL, actor_id TEXT NOT NULL,
        previous_id TEXT, request_id TEXT NOT NULL, request_digest TEXT NOT NULL,
        payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL,
        UNIQUE(topic_id,job_id,actor_id,request_id))""")
    c.execute("CREATE INDEX IF NOT EXISTS journal_working_scope ON journal_working_copies(topic_id,job_id,actor_id,sequence)")
    for operation in ("UPDATE", "DELETE"):
        c.execute(f"""CREATE TRIGGER IF NOT EXISTS journal_working_copies_no_{operation.lower()} BEFORE {operation} ON journal_working_copies
            BEGIN SELECT RAISE(ABORT,'journal_working_copy_immutable'); END""")
    c.execute("""CREATE TRIGGER IF NOT EXISTS journal_working_copy_binding BEFORE INSERT ON journal_working_copies
        WHEN NOT EXISTS (SELECT 1 FROM local_synthesis_jobs WHERE job_id=NEW.job_id AND topic_id=NEW.topic_id AND state='CANDIDATE_READY')
        OR NEW.previous_id IS NOT (SELECT copy_id FROM journal_working_copies
            WHERE topic_id=NEW.topic_id AND job_id=NEW.job_id AND actor_id=NEW.actor_id ORDER BY sequence DESC LIMIT 1)
        BEGIN SELECT RAISE(ABORT,'journal_working_copy_binding_invalid'); END""")


def _validate(c: sqlite3.Connection, job: dict[str, Any], body: domain.Save) -> None:
    if body.expected_input_digest != job["input_digest"]:
        raise ValueError("synthesis_binding_changed")
    try:
        size = len(domain.encoded(body.model_dump()).encode("utf-8"))
    except UnicodeError:
        raise ValueError("journal_working_copy_invalid") from None
    if size > 512_000:
        raise ValueError("journal_working_copy_invalid")
    if body.content is not None:
        available = {ref["citation_id"] for ref in references(job)}
        if any(ref not in available for entry in body.content.entries for ref in entry.citation_ids):
            raise ValueError("journal_citation_invalid")
        if body.content.base_version_id is not None:
            try:
                local_journal._get(c, job["topic_id"], job["job_id"], body.content.base_version_id)
            except KeyError:
                raise ValueError("journal_working_copy_base_invalid") from None


def _head(c: sqlite3.Connection, topic: str, job: str, actor: str) -> sqlite3.Row | None:
    return c.execute("SELECT * FROM journal_working_copies WHERE topic_id=? AND job_id=? AND actor_id=? ORDER BY sequence DESC LIMIT 1",
                     (topic, job, actor)).fetchone() if _exists(c) else None


def _identity(topic: str, job: str, actor: str, request: str) -> str:
    return "jwork_" + domain.fingerprint([domain.SCHEMA, topic, job, actor, request])


def _read(c: sqlite3.Connection, row: sqlite3.Row, job: dict[str, Any], actor: str) -> dict[str, Any]:
    try:
        value = json.loads(row["payload_json"])
        body = domain.Save.model_validate({key: value[key] for key in domain.Save.model_fields})
        expected = dict(schema_version=domain.SCHEMA, copy_id=_identity(job["topic_id"], job["job_id"], actor, body.request_id),
                        topic_id=job["topic_id"], job_id=job["job_id"], actor_id=actor, created_at=value["created_at"], **body.model_dump())
        if (value != expected or domain.fingerprint(value) != row["payload_digest"] or value["copy_id"] != row["copy_id"]
                or row["actor_id"] != actor or row["topic_id"] != job["topic_id"] or row["job_id"] != job["job_id"]
                or row["previous_id"] != body.expected_copy_id or row["request_id"] != body.request_id
                or row["request_digest"] != domain.fingerprint([body.model_dump(), actor])
                or datetime.fromisoformat(value["created_at"]).utcoffset() is None):
            raise ValueError()
        _validate(c, job, body)
        return value
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ValueError("journal_working_copy_integrity_invalid") from None


def _public(value: dict[str, Any] | None) -> dict[str, Any]:
    return dict(head_id=value["copy_id"] if value else None, copy=value if value and value["content"] is not None else None)


def latest(topic: str, job_id: str, actor: str) -> dict[str, Any]:
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        job = _candidate(c, topic, job_id)
        row = _head(c, topic, job_id, actor)
        return _public(_read(c, row, job, actor) if row else None)


def save(topic: str, job_id: str, body: domain.Save, actor: str) -> tuple[dict[str, Any], bool]:
    if not actor.strip() or len(actor) > 500:
        raise ValueError("journal_working_copy_invalid")
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        job = _candidate(c, topic, job_id)
        _validate(c, job, body)
        request_digest = domain.fingerprint([body.model_dump(), actor])
        _initialize(c)
        old = c.execute("SELECT * FROM journal_working_copies WHERE topic_id=? AND job_id=? AND actor_id=? AND request_id=?",
                        (topic, job_id, actor, body.request_id)).fetchone()
        if old:
            if old["request_digest"] != request_digest:
                raise ValueError("journal_working_copy_request_conflict")
            return _public(_read(c, old, job, actor)), False
        previous = _head(c, topic, job_id, actor)
        if previous:
            _read(c, previous, job, actor)
        if body.expected_copy_id != (previous["copy_id"] if previous else None):
            raise ValueError("journal_working_copy_conflict")
        value = dict(schema_version=domain.SCHEMA, copy_id=_identity(topic, job_id, actor, body.request_id),
                     topic_id=topic, job_id=job_id, actor_id=actor, created_at=iso_now_utc(), **body.model_dump())
        c.execute("""INSERT INTO journal_working_copies (copy_id,topic_id,job_id,actor_id,previous_id,request_id,request_digest,payload_json,payload_digest)
            VALUES (?,?,?,?,?,?,?,?,?)""", (value["copy_id"], topic, job_id, actor, body.expected_copy_id, body.request_id,
                                           request_digest, domain.encoded(value), domain.fingerprint(value)))
        checkpoint("after_working_copy")
        return _public(value), True
