"""Append-only review snapshots and atomic reviewed-journal acceptance."""
from contextlib import closing
import json
import sqlite3
from typing import Any

from app.db.bootstrap import connect_metadata_index
from app.domain import journal_review as domain
from app.domain.journal_exchange import preserved_json, sha
from app.domain.temporal_memory import digest
from app.repositories import journal_candidates
from app.repositories.files import iso_now_utc


def checkpoint(phase: str) -> None:
    """Failure injection before atomic commit."""


def _exists(c: sqlite3.Connection) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='journal_candidate_reviews'").fetchone() is not None


def _initialize(c: sqlite3.Connection) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS journal_candidate_reviews (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, review_id TEXT NOT NULL UNIQUE,
        topic_id TEXT NOT NULL, job_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
        previous_id TEXT, request_id TEXT NOT NULL, request_digest TEXT NOT NULL,
        payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL,
        UNIQUE(topic_id,job_id,candidate_id,request_id))""")
    c.execute("CREATE INDEX IF NOT EXISTS journal_review_candidate ON journal_candidate_reviews(topic_id,job_id,candidate_id,sequence)")
    for operation in ("UPDATE", "DELETE"):
        c.execute(f"""CREATE TRIGGER IF NOT EXISTS journal_review_no_{operation.lower()} BEFORE {operation} ON journal_candidate_reviews
            BEGIN SELECT RAISE(ABORT,'journal_review_immutable'); END""")
    c.execute("""CREATE TRIGGER IF NOT EXISTS journal_review_binding BEFORE INSERT ON journal_candidate_reviews
        WHEN NOT EXISTS (SELECT 1 FROM journal_candidates WHERE candidate_id=NEW.candidate_id AND topic_id=NEW.topic_id AND job_id=NEW.job_id)
        OR NEW.previous_id IS NOT (SELECT review_id FROM journal_candidate_reviews
            WHERE topic_id=NEW.topic_id AND job_id=NEW.job_id AND candidate_id=NEW.candidate_id ORDER BY sequence DESC LIMIT 1)
        BEGIN SELECT RAISE(ABORT,'journal_review_binding_invalid'); END""")


def _read(row: sqlite3.Row, candidate: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(row["payload_json"])
        if (digest(value) != row["payload_digest"] or value["schema_version"] != domain.SCHEMA
                or any(value[key] != row[key] for key in ("review_id", "topic_id", "job_id", "candidate_id", "previous_id"))
                or any(value[key] != candidate[key] for key in ("topic_id", "job_id", "candidate_id"))):
            raise ValueError("binding")
        body = domain.Save(request_id=row["request_id"], expected_previous_id=value["previous_id"],
                           expected_candidate_digest=value["candidate_digest"], state=value["state"], note=value["note"], entries=value["entries"])
        domain.validate(candidate, body)
        if row["request_digest"] != digest([body.model_dump(), value["actor_id"]]):
            raise ValueError("request")
        report = value["report"]
        if value["state"] == "accepted":
            if (report["schema_version"] != domain.REPORT_SCHEMA or report["review_id"] != value["review_id"]
                    or report["version_id"] != "jrpt_" + digest([domain.REPORT_SCHEMA, value["review_id"]])
                    or report["candidate_digest"] != digest(candidate) or sha(report["content"]) != report["content_sha256"]):
                raise ValueError("report")
        elif report is not None:
            raise ValueError("unexpected_report")
        return value
    except (KeyError, TypeError, ValueError):
        raise ValueError("journal_review_integrity_invalid") from None


def _latest(c: sqlite3.Connection, topic: str, job: str, candidate: str) -> sqlite3.Row | None:
    return c.execute("SELECT * FROM journal_candidate_reviews WHERE topic_id=? AND job_id=? AND candidate_id=? ORDER BY sequence DESC LIMIT 1", (topic, job, candidate)).fetchone() if _exists(c) else None


def _summary(value: dict[str, Any]) -> dict[str, Any]:
    return dict(value, report={k: v for k, v in value["report"].items() if k != "content"} if value["report"] else None)


def overview(topic: str, page: int = 1, limit: int = 10) -> dict[str, Any]:
    """Newest imports, including held journals; acceptance is never current state.

    Read all bindings in one SQLite snapshot. This is a read-only projection of
    existing immutable records, not a new report or an export authorization.
    """
    if page < 1 or not 1 <= limit <= 100:
        raise ValueError("pagination_invalid")
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        items, total = [], 0
        if journal_candidates._exists(c):
            total = c.execute("SELECT count(*) FROM journal_candidates WHERE topic_id=?", (topic,)).fetchone()[0]
            rows = c.execute("SELECT job_id,candidate_id FROM journal_candidates WHERE topic_id=? ORDER BY sequence DESC LIMIT ? OFFSET ?",
                             (topic, limit, (page - 1) * limit)).fetchall()
            for row in rows:
                candidate = journal_candidates._get(c, topic, row["job_id"], row["candidate_id"])
                latest_row = _latest(c, topic, row["job_id"], row["candidate_id"])
                latest, acceptance = None, None
                if latest_row:
                    latest = _read(latest_row, candidate)
                    accepted_row = c.execute("""SELECT * FROM journal_candidate_reviews
                        WHERE topic_id=? AND job_id=? AND candidate_id=? AND json_extract(payload_json,'$.state')='accepted'
                        ORDER BY sequence DESC LIMIT 1""", (topic, row["job_id"], row["candidate_id"])).fetchone()
                    if accepted_row:
                        accepted = _read(accepted_row, candidate)
                        acceptance = dict(review_id=accepted["review_id"], created_at=accepted["created_at"],
                                          version_id=accepted["report"]["version_id"])
                items.append(dict(journal_candidates.summary(candidate), job_id=row["job_id"], last_acceptance=acceptance,
                                  latest_review={key: latest[key] for key in ("review_id", "state", "created_at")} if latest else None))
        return dict(items=items, total=total, page=page, limit=limit)


def history(topic: str, job: str, candidate_id: str, page: int = 1, limit: int = 10) -> dict[str, Any]:
    if page < 1 or not 1 <= limit <= 100:
        raise ValueError("pagination_invalid")
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        candidate = journal_candidates._get(c, topic, job, candidate_id)
        latest = _latest(c, topic, job, candidate_id)
        items, total = [], 0
        if latest:
            total = c.execute("SELECT count(*) FROM journal_candidate_reviews WHERE topic_id=? AND job_id=? AND candidate_id=?", (topic, job, candidate_id)).fetchone()[0]
            for row in c.execute("SELECT * FROM journal_candidate_reviews WHERE topic_id=? AND job_id=? AND candidate_id=? ORDER BY sequence DESC LIMIT ? OFFSET ?", (topic, job, candidate_id, limit, (page - 1) * limit)):
                items.append(_summary(_read(row, candidate)))
        return dict(items=items, total=total, page=page, limit=limit, latest=_summary(_read(latest, candidate)) if latest else None,
                    candidate_digest=digest(candidate), entries=domain.entries(candidate))


def save(topic: str, job: str, candidate_id: str, body: domain.Save, actor: str) -> tuple[dict[str, Any], bool]:
    if not actor:
        raise ValueError("journal_review_invalid")
    request_digest = digest([body.model_dump(), actor])
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        candidate = journal_candidates._get(c, topic, job, candidate_id)
        _initialize(c)
        old = c.execute("SELECT * FROM journal_candidate_reviews WHERE topic_id=? AND job_id=? AND candidate_id=? AND request_id=?", (topic, job, candidate_id, body.request_id)).fetchone()
        if old:
            if old["request_digest"] != request_digest:
                raise ValueError("journal_review_request_conflict")
            return _read(old, candidate), False
        previous = _latest(c, topic, job, candidate_id)
        if previous:
            _read(previous, candidate)
        if body.expected_previous_id != (previous["review_id"] if previous else None):
            raise ValueError("journal_review_conflict")
        domain.validate(candidate, body)
        review_id = "jreview_" + digest([domain.SCHEMA, topic, job, candidate_id, body.request_id])
        value: dict[str, Any] = dict(schema_version=domain.SCHEMA, review_id=review_id, topic_id=topic, job_id=job, candidate_id=candidate_id,
                     previous_id=body.expected_previous_id, candidate_digest=digest(candidate), actor_id=actor, created_at=iso_now_utc(),
                     state=body.state, note=body.note, entries=[entry.model_dump() for entry in body.entries], report=None)
        if body.state == "accepted":
            value["report"] = domain.report(candidate, value)
        checkpoint("after_report_prepared")
        c.execute("INSERT INTO journal_candidate_reviews(review_id,topic_id,job_id,candidate_id,previous_id,request_id,request_digest,payload_json,payload_digest) VALUES(?,?,?,?,?,?,?,?,?)",
                  (review_id, topic, job, candidate_id, body.expected_previous_id, body.request_id, request_digest, preserved_json(value), digest(value)))
        checkpoint("after_review")
        return value, True


def _get(c: sqlite3.Connection, topic: str, job: str, candidate: dict[str, Any], review_id: str) -> dict[str, Any]:
    row = c.execute("SELECT * FROM journal_candidate_reviews WHERE topic_id=? AND job_id=? AND candidate_id=? AND review_id=?", (topic, job, candidate["candidate_id"], review_id)).fetchone() if _exists(c) else None
    if row is None:
        raise KeyError(review_id)
    return _read(row, candidate)


def detail(topic: str, job: str, candidate_id: str, review_id: str) -> dict[str, Any]:
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        return _get(c, topic, job, journal_candidates._get(c, topic, job, candidate_id), review_id)


def compare(topic: str, job: str, candidate_id: str, review_id: str, from_review_id: str) -> dict[str, Any]:
    """Compare saved human judgments, never infer scientific or source changes."""
    if review_id == from_review_id:
        raise ValueError("journal_review_comparison_same")
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        candidate = journal_candidates._get(c, topic, job, candidate_id)
        before = _get(c, topic, job, candidate, from_review_id)
        after = _get(c, topic, job, candidate, review_id)
        return domain.comparison(candidate, before, after)


def export(topic: str, job: str, candidate_id: str, review_id: str, format: str) -> dict[str, Any]:
    if format not in {"markdown", "json"}:
        raise ValueError("synthesis_export_format_invalid")
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        candidate = journal_candidates._get(c, topic, job, candidate_id)
        latest = _latest(c, topic, job, candidate_id)
        if latest is None:
            raise ValueError("journal_review_export_not_accepted")
        review = _read(latest, candidate)
        if review["state"] != "accepted" or review["review_id"] != review_id:
            raise ValueError("journal_review_export_not_accepted")
        content = review["report"]["content"] if format == "markdown" else preserved_json(dict(schema_version="HUMAN_REVIEWED_JOURNAL_EXPORT_V1", candidate=candidate, review=review))
        return dict(content=content, filename=review["report"]["version_id"] + (".md" if format == "markdown" else ".json"),
                    media_type="text/markdown; charset=utf-8" if format == "markdown" else "application/json", content_sha256=sha(content))
