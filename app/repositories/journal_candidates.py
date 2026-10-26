"""Immutable imported/generated candidates; no Provider invocation or acceptance."""
from contextlib import closing
import json
import sqlite3
from typing import Any

from app.db.bootstrap import connect_metadata_index
from app.domain import journal_exchange as domain
from app.domain import generated_journal
from app.domain import review_input_journal
from app.domain.local_journal import references
from app.domain.temporal_memory import digest
from app.repositories.files import iso_now_utc
from app.repositories.synthesis_reports import _candidate


def checkpoint(phase: str) -> None:
    """Fault injection before commit."""


def _exists(c: sqlite3.Connection) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='journal_candidates'").fetchone() is not None


def _initialize(c: sqlite3.Connection) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS journal_candidates (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id TEXT NOT NULL UNIQUE,
        topic_id TEXT NOT NULL, job_id TEXT NOT NULL, payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL)""")
    c.execute("CREATE INDEX IF NOT EXISTS journal_candidates_job ON journal_candidates(topic_id,job_id,sequence)")
    for operation in ("UPDATE", "DELETE"):
        c.execute(f"""CREATE TRIGGER IF NOT EXISTS journal_candidates_no_{operation.lower()} BEFORE {operation} ON journal_candidates
            BEGIN SELECT RAISE(ABORT,'journal_candidate_immutable'); END""")
    c.execute("""CREATE TRIGGER IF NOT EXISTS journal_candidates_binding BEFORE INSERT ON journal_candidates
        WHEN NOT EXISTS (SELECT 1 FROM local_synthesis_jobs WHERE job_id=NEW.job_id AND topic_id=NEW.topic_id AND state='CANDIDATE_READY')
        BEGIN SELECT RAISE(ABORT,'journal_candidate_binding_invalid'); END""")


def _read(row: sqlite3.Row, job: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(row["payload_json"])
        if value.get("schema_version") == review_input_journal.SCHEMA:
            if digest(value) != row["payload_digest"] or value["candidate_id"] != row["candidate_id"]:
                raise ValueError("binding")
            review_input_journal.validate(value, job)
            return value
        if value.get("schema_version") == generated_journal.SCHEMA:
            if digest(value) != row["payload_digest"] or value["candidate_id"] != row["candidate_id"]:
                raise ValueError("binding")
            generated_journal.validate(value, job)
            return value
        frozen = value["packet"]
        domain.validate_packet_binding(frozen, job)
        proposal = domain.parse(value["raw_json"])
        if (frozen.get("schema_version") == domain.REVIEW_PACKET_SCHEMA
                and domain.preserved_json(value["coverage"]) != domain.preserved_json(domain.citation_coverage(frozen, domain.validate(proposal, frozen)))):
            raise ValueError("journal_candidate_integrity_invalid")
        if (digest(value) != row["payload_digest"] or value["schema_version"] != domain.CANDIDATE_SCHEMA
                or value["candidate_id"] != row["candidate_id"] or value["job_id"] != job["job_id"]
                or value["topic_id"] != job["topic_id"] or value["input_digest"] != job["input_digest"]
                or frozen["job_id"] != job["job_id"] or frozen["topic_id"] != job["topic_id"]
                or frozen["input_digest"] != job["input_digest"] or frozen["references"] != references(job)
                or frozen["packet_id"] != "jpacket_" + digest({k: v for k, v in frozen.items() if k != "packet_id"})
                or value["proposal"] != proposal.model_dump() or value["citations"] != domain.validate(proposal, frozen)
                or value["raw_sha256"] != domain.sha(value["raw_json"])
                or value["candidate_id"] != "jcand_" + digest([job["job_id"], value["raw_sha256"]])
                or value["content_sha256"] != domain.sha(value["markdown"])):
            raise ValueError("journal_candidate_integrity_invalid")
        return value
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError("journal_candidate_integrity_invalid") from None


def input_packet(topic: str, job_id: str) -> dict[str, Any]:
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        return domain.packet(_candidate(c, topic, job_id))


def review_inputs(topic: str, job_id: str) -> dict[str, Any]:
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        return review_input_journal.inputs(_candidate(c, topic, job_id))


def save_review_input(topic: str, job_id: str, body: review_input_journal.Create, actor: str) -> tuple[dict[str, Any], bool]:
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError(review_input_journal.ERROR)
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        job = _candidate(c, topic, job_id)
        identity = review_input_journal.identity(job, body)
        _initialize(c)
        old = c.execute("SELECT * FROM journal_candidates WHERE candidate_id=? AND topic_id=? AND job_id=?", (identity, topic, job_id)).fetchone()
        if old:
            return _read(old, job), False
        value = review_input_journal.assemble(job, body, actor, iso_now_utc())
        c.execute("INSERT INTO journal_candidates(candidate_id,topic_id,job_id,payload_json,payload_digest) VALUES(?,?,?,?,?)",
                  (identity, topic, job_id, domain.preserved_json(value), digest(value)))
        checkpoint("after_candidate")
        return value, True


def save(topic: str, job_id: str, raw_json: str, actor: str) -> tuple[dict[str, Any], bool]:
    if not actor:
        raise ValueError("journal_exchange_invalid")
    domain.parse(raw_json)
    identity = "jcand_" + digest([job_id, domain.sha(raw_json)])
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        job = _candidate(c, topic, job_id)
        _initialize(c)
        old = c.execute("SELECT * FROM journal_candidates WHERE candidate_id=? AND topic_id=? AND job_id=?", (identity, topic, job_id)).fetchone()
        if old:
            return _read(old, job), False
        value = domain.assemble(job, raw_json, actor, iso_now_utc())
        c.execute("INSERT INTO journal_candidates(candidate_id,topic_id,job_id,payload_json,payload_digest) VALUES(?,?,?,?,?)",
                  (identity, topic, job_id, domain.preserved_json(value), digest(value)))
        checkpoint("after_candidate")
        return value, True


def summary(value: dict[str, Any]) -> dict[str, Any]:
    base = dict(candidate_id=value["candidate_id"], title=value["proposal"]["title"])
    if value["schema_version"] in {generated_journal.SCHEMA, review_input_journal.SCHEMA}:
        return dict(base, origin=value["origin"], preserved_at=value["preserved_at"])
    return dict(base, imported_at=value["imported_at"])  # Keep legacy response shape.


def save_execution(topic: str, job_id: str, execution_id: str, expected_result_digest: str, actor: str) -> tuple[dict[str, Any], bool]:
    from app.repositories import journal_executions
    if not actor.strip():
        raise ValueError("journal_exchange_invalid")
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        contract, events = journal_executions._read(c, topic, job_id, execution_id)
        if not events or events[-1]["state"] != "SUCCEEDED":
            raise ValueError("journal_execution_not_succeeded")
        event = events[-1]
        if digest(event["result"]) != expected_result_digest:
            raise ValueError("journal_execution_result_changed")
        _initialize(c)
        identity = generated_journal.identity(contract, event)
        job = _candidate(c, topic, job_id)
        old = c.execute("SELECT * FROM journal_candidates WHERE topic_id=? AND job_id=? AND candidate_id=?", (topic, job_id, identity)).fetchone()
        if old:
            return _read(old, job), False
        manifest = next((item.get("adapter_manifest") for item in events
            if item["state"] == "LEASED" and item["attempt"] == event["attempt"]), None)
        # These transports carry a versioned self-contained receipt.
        # Historical candidates return above, without re-rendering or ID changes.
        transport = manifest if manifest and manifest.get("schema_version") in {"CLAUDE_JOURNAL_CLI_V1", "ANTIGRAVITY_JOURNAL_CLI_V1", "GROK_JOURNAL_ACP_V1", "ANTHROPIC_JOURNAL_WIRE_V1", "CODEX_JOURNAL_CLI_V2"} else None
        value = generated_journal.assemble(contract, event, actor, iso_now_utc(), adapter_manifest=transport)
        generated_journal.validate(value, job)
        c.execute("INSERT INTO journal_candidates(candidate_id,topic_id,job_id,payload_json,payload_digest) VALUES(?,?,?,?,?)",
                  (identity, topic, job_id, domain.preserved_json(value), digest(value)))
        checkpoint("after_candidate")
        return value, True


def listing(topic: str, job_id: str, page: int = 1, limit: int = 10) -> dict[str, Any]:
    if page < 1 or not 1 <= limit <= 100:
        raise ValueError("pagination_invalid")
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        job = _candidate(c, topic, job_id)
        items, total = [], 0
        if _exists(c):
            total = c.execute("SELECT count(*) FROM journal_candidates WHERE topic_id=? AND job_id=?", (topic, job_id)).fetchone()[0]
            for row in c.execute("SELECT * FROM journal_candidates WHERE topic_id=? AND job_id=? ORDER BY sequence DESC LIMIT ? OFFSET ?", (topic, job_id, limit, (page - 1) * limit)):
                value = _read(row, job)
                items.append(summary(value))
        return dict(items=items, total=total, page=page, limit=limit)


def _get(c: sqlite3.Connection, topic: str, job_id: str, candidate_id: str) -> dict[str, Any]:
    job = _candidate(c, topic, job_id)
    row = c.execute("SELECT * FROM journal_candidates WHERE topic_id=? AND job_id=? AND candidate_id=?", (topic, job_id, candidate_id)).fetchone() if _exists(c) else None
    if row is None:
        raise KeyError(candidate_id)
    return _read(row, job)


def detail(topic: str, job_id: str, candidate_id: str) -> dict[str, Any]:
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        return _get(c, topic, job_id, candidate_id)
