"""Append-only local journal drafts; independent of accepted extractive reports."""
from contextlib import closing
import hashlib
import json
import sqlite3
from typing import Any

from app.db.bootstrap import connect_metadata_index
from app.domain import local_journal as domain
from app.domain.journal_exchange import preserved_json
from app.domain.temporal_memory import canonical_json, digest
from app.repositories.files import iso_now_utc
from app.repositories.synthesis_reports import _candidate


def checkpoint(phase: str) -> None:
    """Fault injection before commit."""


def _exists(c: sqlite3.Connection) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_journal_versions'").fetchone() is not None


def _initialize(c: sqlite3.Connection) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS local_journal_versions (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, version_id TEXT NOT NULL UNIQUE,
        topic_id TEXT NOT NULL, job_id TEXT NOT NULL, previous_id TEXT, request_id TEXT NOT NULL,
        request_digest TEXT NOT NULL, payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL,
        UNIQUE(topic_id,job_id,request_id))""")
    c.execute("CREATE INDEX IF NOT EXISTS local_journal_job ON local_journal_versions(topic_id,job_id,sequence)")
    for operation in ("UPDATE", "DELETE"):
        c.execute(f"""CREATE TRIGGER IF NOT EXISTS local_journal_no_{operation.lower()} BEFORE {operation} ON local_journal_versions
            BEGIN SELECT RAISE(ABORT,'journal_immutable'); END""")
    c.execute("""CREATE TRIGGER IF NOT EXISTS local_journal_binding BEFORE INSERT ON local_journal_versions
        WHEN NOT EXISTS (SELECT 1 FROM local_synthesis_jobs WHERE job_id=NEW.job_id AND topic_id=NEW.topic_id AND state='CANDIDATE_READY')
        OR NEW.previous_id IS NOT (SELECT version_id FROM local_journal_versions
            WHERE topic_id=NEW.topic_id AND job_id=NEW.job_id ORDER BY sequence DESC LIMIT 1)
        BEGIN SELECT RAISE(ABORT,'journal_binding_invalid'); END""")


def _read(row: sqlite3.Row, job: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(row["payload_json"])
        available = {r["citation_id"]: r for r in domain.references(job)}
        used = sorted({ref for entry in value["entries"] for ref in entry["citation_ids"]})
        if (digest(value) != row["payload_digest"] or value["version_id"] != row["version_id"]
                or value["schema_version"] != domain.SCHEMA or value["job_id"] != job["job_id"]
                or value["topic_id"] != job["topic_id"] or value["input_digest"] != job["input_digest"]
                or value["candidate_digest"] != digest(job["candidate"]) or value["previous_id"] != row["previous_id"]
                or value["citations"] != [available[ref] for ref in used]
                or value["content_sha256"] != hashlib.sha256(value["markdown"].encode("utf-8")).hexdigest()):
            raise ValueError("journal_integrity_invalid")
        return value
    except (KeyError, TypeError, ValueError):
        raise ValueError("journal_integrity_invalid") from None


def _latest(c: sqlite3.Connection, topic: str, job_id: str) -> sqlite3.Row | None:
    return c.execute("SELECT * FROM local_journal_versions WHERE topic_id=? AND job_id=? ORDER BY sequence DESC LIMIT 1", (topic, job_id)).fetchone() if _exists(c) else None


def listing(topic: str, job_id: str, page: int = 1, limit: int = 10) -> dict[str, Any]:
    if page < 1 or not 1 <= limit <= 100:
        raise ValueError("pagination_invalid")
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        job = _candidate(c, topic, job_id)
        latest = _latest(c, topic, job_id)
        items, total = [], 0
        if latest:
            total = c.execute("SELECT count(*) FROM local_journal_versions WHERE topic_id=? AND job_id=?", (topic, job_id)).fetchone()[0]
            for row in c.execute("SELECT * FROM local_journal_versions WHERE topic_id=? AND job_id=? ORDER BY sequence DESC LIMIT ? OFFSET ?", (topic, job_id, limit, (page - 1) * limit)):
                version = _read(row, job)
                items.append({key: version[key] for key in ("version_id", "previous_id", "title", "actor_id", "created_at")})
        return dict(items=items, total=total, page=page, limit=limit, input_digest=job["input_digest"],
                    references=domain.references(job), latest_version=_read(latest, job) if latest else None)


def save(topic: str, job_id: str, body: domain.Save, actor_id: str) -> tuple[dict[str, Any], bool]:
    if not actor_id:
        raise ValueError("journal_input_invalid")
    request_digest = digest([body.model_dump(), actor_id])
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        job = _candidate(c, topic, job_id)
        _initialize(c)
        old = c.execute("SELECT * FROM local_journal_versions WHERE topic_id=? AND job_id=? AND request_id=?", (topic, job_id, body.request_id)).fetchone()
        if old:
            if old["request_digest"] != request_digest:
                raise ValueError("journal_request_conflict")
            return _read(old, job), False
        latest = _latest(c, topic, job_id)
        if latest:
            _read(latest, job)
        if body.expected_previous_id != (latest["version_id"] if latest else None):
            raise ValueError("journal_version_conflict")
        if body.expected_input_digest != job["input_digest"]:
            raise ValueError("synthesis_binding_changed")
        version_id = "jver_" + digest([domain.SCHEMA, topic, job_id, body.request_id])
        value = domain.assemble(job, body, actor_id, iso_now_utc(), version_id)
        serialize = preserved_json if job["inputs"].get("schema_version") == "LOCAL_EVIDENCE_SYNTHESIS_V2" else canonical_json
        c.execute("""INSERT INTO local_journal_versions (version_id,topic_id,job_id,previous_id,request_id,request_digest,payload_json,payload_digest)
            VALUES (?,?,?,?,?,?,?,?)""", (version_id, topic, job_id, body.expected_previous_id, body.request_id, request_digest, serialize(value), digest(value)))
        checkpoint("after_version")
        return value, True


def _get(c: sqlite3.Connection, topic: str, job_id: str, version_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    job = _candidate(c, topic, job_id)
    row = c.execute("SELECT * FROM local_journal_versions WHERE topic_id=? AND job_id=? AND version_id=?", (topic, job_id, version_id)).fetchone() if _exists(c) else None
    if row is None:
        raise KeyError(version_id)
    return job, _read(row, job)


def detail(topic: str, job_id: str, version_id: str) -> dict[str, Any]:
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        return _get(c, topic, job_id, version_id)[1]


def export(topic: str, job_id: str, version_id: str, format: str) -> dict[str, Any]:
    if format not in {"markdown", "json"}:
        raise ValueError("synthesis_export_format_invalid")
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        job, version = _get(c, topic, job_id, version_id)
        serialize = preserved_json if job["inputs"].get("schema_version") == "LOCAL_EVIDENCE_SYNTHESIS_V2" else canonical_json
        content = version["markdown"] if format == "markdown" else serialize(dict(schema_version="LOCAL_CITED_JOURNAL_EXPORT_V1",
            version=version, frozen_input=job["inputs"], source_validation=job["steps"][-1]["output"]))
        return dict(content=content, filename=f"{version_id}.{ 'md' if format == 'markdown' else 'json'}",
                    media_type="text/markdown; charset=utf-8" if format == "markdown" else "application/json",
                    content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest())
