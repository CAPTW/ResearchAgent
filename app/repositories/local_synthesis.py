"""Manual bounded synthesis steps, atomically checkpointed in the metadata store.

No background worker or Provider lease is needed for these short deterministic
steps: BEGIN IMMEDIATE fences the complete computation and checkpoint. Process
death rolls the step back. A future remote/model stage needs a separate lease.
"""
from contextlib import closing
import json
import sqlite3
from typing import Any

from app.auth.resource_access import ResourceAction, ResourceActor
from app.db.bootstrap import connect_metadata_index
from app.domain import local_synthesis as domain
from app.domain.temporal_memory import digest, snapshot
from app.repositories import temporal_memory, temporal_sources
from app.repositories.files import iso_now_utc


def checkpoint(phase: str) -> None:
    """Transaction fault injection seam."""


def _exists(c: sqlite3.Connection) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_synthesis_jobs'").fetchone() is not None


def _initialize(c: sqlite3.Connection) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS local_synthesis_jobs (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL UNIQUE, topic_id TEXT NOT NULL,
        inputs_json TEXT NOT NULL, created_at TEXT NOT NULL, actor_id TEXT NOT NULL,
        total_steps INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('READY','PAUSED','CANDIDATE_READY')))""")
    c.execute("""CREATE TABLE IF NOT EXISTS local_synthesis_requests (
        topic_id TEXT NOT NULL, request_id TEXT NOT NULL, request_digest TEXT NOT NULL,
        job_id TEXT NOT NULL, PRIMARY KEY(topic_id,request_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS local_synthesis_steps (
        job_id TEXT NOT NULL, step_index INTEGER NOT NULL, input_digest TEXT NOT NULL,
        output_json TEXT NOT NULL, output_digest TEXT NOT NULL, completed_at TEXT NOT NULL,
        PRIMARY KEY(job_id,step_index))""")
    c.execute("CREATE INDEX IF NOT EXISTS local_synthesis_topic ON local_synthesis_jobs(topic_id,sequence)")
    # Follow existing extension-table policy: insert guards, no changes to the
    # shared store's declared FK inventory, immutable parents and checkpoints.
    c.execute("""CREATE TRIGGER IF NOT EXISTS local_synthesis_job_content_immutable
        BEFORE UPDATE OF job_id,topic_id,inputs_json,created_at,actor_id,total_steps ON local_synthesis_jobs
        BEGIN SELECT RAISE(ABORT,'synthesis_immutable'); END""")
    c.execute("""CREATE TRIGGER IF NOT EXISTS local_synthesis_job_no_delete BEFORE DELETE ON local_synthesis_jobs
        BEGIN SELECT RAISE(ABORT,'synthesis_immutable'); END""")
    for table in ("local_synthesis_requests", "local_synthesis_steps"):
        for operation in ("UPDATE", "DELETE"):
            c.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} BEFORE {operation} ON {table}
                BEGIN SELECT RAISE(ABORT,'synthesis_immutable'); END""")
    c.execute("""CREATE TRIGGER IF NOT EXISTS local_synthesis_request_binding BEFORE INSERT ON local_synthesis_requests
        WHEN NOT EXISTS (SELECT 1 FROM local_synthesis_jobs WHERE job_id=NEW.job_id AND topic_id=NEW.topic_id)
        BEGIN SELECT RAISE(ABORT,'synthesis_binding_invalid'); END""")
    c.execute("""CREATE TRIGGER IF NOT EXISTS local_synthesis_step_binding BEFORE INSERT ON local_synthesis_steps
        WHEN NOT EXISTS (SELECT 1 FROM local_synthesis_jobs WHERE job_id=NEW.job_id AND state='READY' AND NEW.step_index<total_steps)
        OR NEW.step_index != (SELECT count(*) FROM local_synthesis_steps WHERE job_id=NEW.job_id)
        BEGIN SELECT RAISE(ABORT,'synthesis_binding_invalid'); END""")


def _read(c: sqlite3.Connection, topic_id: str, job_id: str) -> dict[str, Any]:
    row = c.execute("SELECT * FROM local_synthesis_jobs WHERE topic_id=? AND job_id=?", (topic_id, job_id)).fetchone() if _exists(c) else None
    if row is None:
        raise KeyError("synthesis_not_found")
    job = dict(row)
    inputs = json.loads(job.pop("inputs_json"))
    if domain.identity(inputs) != job_id or inputs["topic_id"] != topic_id or domain.total_steps(inputs) != job["total_steps"]:
        raise ValueError("synthesis_integrity_invalid")
    steps: list[dict[str, Any]] = []
    digests: list[str] = []
    for record in c.execute("SELECT * FROM local_synthesis_steps WHERE job_id=? ORDER BY step_index", (job_id,)):
        part = dict(record)
        output = json.loads(part.pop("output_json"))
        if (part["step_index"] != len(steps) or part["input_digest"] != digest([job_id, len(steps), digests])
                or part["output_digest"] != domain.checkpoint_digest(output, inputs)):
            raise ValueError("synthesis_integrity_invalid")
        steps.append(dict(**part, output=output))
        digests.append(part["output_digest"])
    if len(steps) > job["total_steps"] or (job["state"] == "CANDIDATE_READY") != (len(steps) == job["total_steps"]):
        raise ValueError("synthesis_integrity_invalid")
    return dict(**job, inputs=inputs, input_digest=digest(inputs), completed_steps=len(steps), steps=steps,
                candidate=steps[-2]["output"]["candidate"] if job["state"] == "CANDIDATE_READY" else None)


def get(topic_id: str, job_id: str) -> dict[str, Any]:
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        return _read(c, topic_id, job_id)


def listing(topic_id: str, page: int = 1, limit: int = 10) -> dict[str, Any]:
    if page < 1 or not 1 <= limit <= 100:
        raise ValueError("pagination_invalid")
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        if not _exists(c):
            return dict(items=[], page=page, limit=limit, total=0)
        total = c.execute("SELECT count(*) FROM local_synthesis_jobs WHERE topic_id=?", (topic_id,)).fetchone()[0]
        rows = c.execute("SELECT job_id FROM local_synthesis_jobs WHERE topic_id=? ORDER BY sequence DESC LIMIT ? OFFSET ?", (topic_id, limit, (page - 1) * limit))
        items = []
        has_reviews = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='synthesis_report_decisions'").fetchone() is not None
        for row in rows.fetchall():
            job = _read(c, topic_id, row[0])
            review = c.execute("SELECT state FROM synthesis_report_decisions WHERE topic_id=? AND job_id=? ORDER BY sequence DESC LIMIT 1", (topic_id, row[0])).fetchone() if has_reviews else None
            version = c.execute("SELECT version_id FROM synthesis_report_versions WHERE topic_id=? AND job_id=?", (topic_id, row[0])).fetchone() if has_reviews else None
            items.append({**{key: job[key] for key in ("job_id", "state", "created_at", "actor_id", "completed_steps", "total_steps")},
                          "paper_count": len(job["inputs"]["papers"]), "source_snapshot_id": job["inputs"]["source_snapshot_id"],
                          "report_version_id": version[0] if version else None, "review_state": review[0] if review else None})
        return dict(items=items, page=page, limit=limit, total=total)


def create(topic_id: str, selection: list[dict[str, Any]], request_id: str, actor: ResourceActor) -> tuple[dict[str, Any], bool]:
    if not 1 <= len(request_id) <= 100:
        raise ValueError("synthesis_request_invalid")
    actor_id = actor.subject or actor.actor_type
    request_digest = digest([sorted(selection, key=lambda item: item["paper_version_id"]), actor_id])
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        _initialize(c)
        previous = c.execute("SELECT * FROM local_synthesis_requests WHERE topic_id=? AND request_id=?", (topic_id, request_id)).fetchone()
        if previous:
            if previous["request_digest"] != request_digest:
                raise ValueError("synthesis_request_conflict")
            return _read(c, topic_id, previous["job_id"]), False
        captured: dict[str, Any] = {}
        def consume(profile, catalog, members):
            from app.domain import journal_review_input
            from app.services import topic_profiles
            frozen_reviews = {}
            indexed = {item["paperVersionId"]: item for item in catalog}
            for selected in selection:
                if selected.get("review_input") is None:
                    continue
                topic_profiles.authorize_topic_profile(profile, actor=actor, action=ResourceAction.UPDATE)
                item = indexed.get(selected["paper_version_id"])
                if item is None or item["metadataDigest"] != selected["metadata_digest"]:
                    raise ValueError("synthesis_selection_changed")
                frozen_reviews[selected["paper_version_id"]] = journal_review_input.freeze(
                    c, topic_id, selected["review_input"], item.get("_reviewOccurrenceKeys", []))
            captured.update(domain.freeze(topic_id, profile, catalog, members, selection,
                snapshot(topic_id, members, "").snapshot_id, frozen_reviews))
        source_snapshot, _ = temporal_memory.capture_in_transaction(c, topic_id,
            lambda connection: temporal_sources.project(connection, topic_id, actor, consume=consume))
        assert captured["source_snapshot_id"] == source_snapshot.snapshot_id
        job_id = domain.identity(captured)
        existing = c.execute("SELECT 1 FROM local_synthesis_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not existing:
            c.execute("""INSERT INTO local_synthesis_jobs (job_id,topic_id,inputs_json,created_at,actor_id,total_steps,state)
                VALUES (?,?,?,?,?,?,'READY')""", (job_id, topic_id, domain.encoded(captured, captured), iso_now_utc(), actor_id, domain.total_steps(captured)))
        c.execute("INSERT INTO local_synthesis_requests VALUES (?,?,?,?)", (topic_id, request_id, request_digest, job_id))
        checkpoint("after_capture")
        return _read(c, topic_id, job_id), not bool(existing)


def advance(topic_id: str, job_id: str, expected_step: int) -> tuple[dict[str, Any], bool]:
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        job = _read(c, topic_id, job_id)
        count = job["completed_steps"]
        if not 0 <= expected_step < job["total_steps"] or expected_step > count:
            raise ValueError("synthesis_checkpoint_conflict")
        if expected_step < count:
            return job, False  # Lost-response retry must not advance another step.
        if job["state"] != "READY":
            raise ValueError("synthesis_paused")
        output = domain.step(job["inputs"], [part["output"] for part in job["steps"]])
        c.execute("INSERT INTO local_synthesis_steps VALUES (?,?,?,?,?,?)",
                  (job_id, count, digest([job_id, count, [part["output_digest"] for part in job["steps"]]]),
                   domain.encoded(output, job["inputs"]), domain.checkpoint_digest(output, job["inputs"]), iso_now_utc()))
        if count + 1 == job["total_steps"]:
            c.execute("UPDATE local_synthesis_jobs SET state='CANDIDATE_READY' WHERE job_id=?", (job_id,))
        checkpoint("after_step")
        return _read(c, topic_id, job_id), True


def set_paused(topic_id: str, job_id: str, paused: bool) -> dict[str, Any]:
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        job = _read(c, topic_id, job_id)
        if job["state"] == "CANDIDATE_READY":
            raise ValueError("synthesis_already_complete")
        c.execute("UPDATE local_synthesis_jobs SET state=? WHERE job_id=?", ("PAUSED" if paused else "READY", job_id))
        return _read(c, topic_id, job_id)
