"""Immutable external captures and append-only human review; no execution authority."""
from contextlib import closing
import json
import sqlite3
from typing import Any

from app.db.bootstrap import connect_metadata_index
from app.domain import saved_analysis as domain
from app.domain.journal_exchange import preserved_json, sha
from app.repositories.files import iso_now_utc


def checkpoint(phase: str) -> None:
    """Failure injection before commit."""


def digest(value: Any) -> str:
    """Preserve exact Unicode for archive identity and request replay."""
    return sha(preserved_json(value))


def _receipt(identity: str, saved_at: str, bundle: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    # Imported source IDs are claims, not write authority over the source topic.
    # Keep this receipt inside its owner-scoped archive, outside shared history.
    origin = 'MANUAL_IMPORT_UNVERIFIED'
    if bundle['schema_version'] == 'SAVED_ANTHROPIC_TRIAL_ANALYSIS_V1':
        origin = 'FIXTURE_SYNTHETIC_CAPTURE' if bundle['plan']['grant']['origin'] == 'FIXTURE_SYNTHETIC' else 'LOCAL_APP_CAPTURE'
    return dict(record, receipt_id=identity + ":input", recorded_at=saved_at,
                packet=bundle["plan"]["contract"]["packet"], input_origin=origin)


def _exists(c: sqlite3.Connection) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE name='saved_analysis_captures' AND type='table'").fetchone() is not None


def _initialize(c: sqlite3.Connection) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS saved_analysis_captures (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, analysis_id TEXT NOT NULL UNIQUE,
        topic_id TEXT NOT NULL, owner_id TEXT NOT NULL, payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL)""")
    c.execute("CREATE INDEX IF NOT EXISTS saved_analysis_topic ON saved_analysis_captures(topic_id,owner_id,sequence)")
    c.execute("""CREATE TABLE IF NOT EXISTS saved_analysis_decisions (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT NOT NULL UNIQUE,
        topic_id TEXT NOT NULL, analysis_id TEXT NOT NULL, previous_id TEXT, request_id TEXT NOT NULL,
        request_digest TEXT NOT NULL, payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL,
        UNIQUE(topic_id,analysis_id,request_id))""")
    for table, prefix in (("saved_analysis_captures", "saved_analysis"), ("saved_analysis_decisions", "saved_analysis_decision")):
        for operation in ("UPDATE", "DELETE"):
            c.execute(f"""CREATE TRIGGER IF NOT EXISTS {prefix}_no_{operation.lower()} BEFORE {operation} ON {table}
                BEGIN SELECT RAISE(ABORT,'saved_analysis_immutable'); END""")
    c.execute("""CREATE TRIGGER IF NOT EXISTS saved_analysis_decision_binding BEFORE INSERT ON saved_analysis_decisions
        WHEN NOT EXISTS (SELECT 1 FROM saved_analysis_captures WHERE analysis_id=NEW.analysis_id AND topic_id=NEW.topic_id)
        OR NEW.previous_id IS NOT (SELECT decision_id FROM saved_analysis_decisions WHERE topic_id=NEW.topic_id
            AND analysis_id=NEW.analysis_id ORDER BY sequence DESC LIMIT 1)
        BEGIN SELECT RAISE(ABORT,'saved_analysis_decision_binding_invalid'); END""")


def _read(c: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    try:
        value = json.loads(row["payload_json"])
        if (digest(value) != row["payload_digest"]
                or any(value[k] != row[k] for k in ("analysis_id", "topic_id", "owner_id"))
                or value["analysis_id"] != "analysis_" + digest([value["topic_id"], value["owner_id"], value["bundle"]])):
            raise ValueError()
        bundle, record = domain.validate_bundle(value["bundle"])
        if bundle['schema_version'] == 'SAVED_ANTHROPIC_TRIAL_ANALYSIS_V1' and value['owner_id'] != bundle['plan']['grant']['actor_id']:
            raise ValueError()
        receipt = value["receipt"]
        if preserved_json(receipt) != preserved_json(_receipt(value["analysis_id"], value["saved_at"], bundle, record)):
            raise ValueError()
        decisions = []
        previous = None
        for entry in c.execute("SELECT * FROM saved_analysis_decisions WHERE topic_id=? AND analysis_id=? ORDER BY sequence", (value["topic_id"], value["analysis_id"])):
            decision = json.loads(entry["payload_json"])
            body = domain.Decision.model_validate(decision["command"])
            if (digest(decision) != entry["payload_digest"]
                    or any(decision[k] != entry[k] for k in ("topic_id", "analysis_id", "decision_id"))
                    or decision["actor_id"] != value["owner_id"] or body.expected_previous_id != previous
                    or entry["previous_id"] != previous or entry["request_id"] != body.request_id
                    or entry["request_digest"] != digest([body.model_dump(), decision["actor_id"]])
                    or decision["state"] != body.state or decision["note"] != body.note
                    or decision["decision_id"] != "decision_" + digest([value["analysis_id"], body.request_id])):
                raise ValueError()
            decisions.append(decision)
            previous = decision["decision_id"]
        return dict(value, receipt=receipt, decisions=decisions, latest_decision=decisions[-1] if decisions else None)
    except (KeyError, TypeError, ValueError):
        raise ValueError("saved_analysis_integrity_invalid") from None


def _get(c: sqlite3.Connection, topic: str, identity: str, actor: str | None = None) -> dict[str, Any]:
    row = c.execute("SELECT * FROM saved_analysis_captures WHERE topic_id=? AND analysis_id=?", (topic, identity)).fetchone() if _exists(c) else None
    if row is None or (actor is not None and row["owner_id"] != actor):
        raise KeyError("saved_analysis_not_found")
    return _read(c, row)


def save(topic: str, actor: str, value: Any) -> dict[str, Any]:
    # A user-uploaded file must never acquire server-held capture provenance.
    if not isinstance(value, dict) or value.get('schema_version') != 'SAVED_ANTHROPIC_ANALYSIS_V1':
        raise ValueError('saved_analysis_bundle_invalid')
    return _save(topic, actor, value)


def save_trial_capture(topic: str, actor: str, value: Any) -> dict[str, Any]:
    if (not isinstance(value, dict) or value.get('schema_version') != 'SAVED_ANTHROPIC_TRIAL_ANALYSIS_V1'
            or actor != value.get('plan', {}).get('grant', {}).get('actor_id')):
        raise ValueError('saved_analysis_bundle_invalid')
    return _save(topic, actor, value)


def _save(topic: str, actor: str, value: Any) -> dict[str, Any]:
    if not topic or not actor:
        raise ValueError("saved_analysis_bundle_invalid")
    bundle, record = domain.validate_bundle(value)
    identity = "analysis_" + digest([topic, actor, bundle])
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        _initialize(c)
        saved_at = iso_now_utc()
        receipt = _receipt(identity, saved_at, bundle, record)
        checkpoint("receipt_saved")
        archived = dict(analysis_id=identity, topic_id=topic, owner_id=actor, saved_at=saved_at,
                        bundle=bundle, receipt=receipt)
        c.execute("INSERT INTO saved_analysis_captures(analysis_id,topic_id,owner_id,payload_json,payload_digest) VALUES(?,?,?,?,?) ON CONFLICT(analysis_id) DO NOTHING",
                  (identity, topic, actor, preserved_json(archived), digest(archived)))
        checkpoint("archive_saved")
        return _get(c, topic, identity, actor)


def detail(topic: str, identity: str, actor: str | None = None) -> dict[str, Any]:
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        return _get(c, topic, identity, actor)


def listing(topic: str, actor: str | None = None, *, page: int = 1, limit: int = 10) -> dict[str, Any]:
    if page < 1 or not 1 <= limit <= 100:
        raise ValueError("pagination_invalid")
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        rows: list[sqlite3.Row] = []
        total = 0
        if _exists(c):
            where, params = "topic_id=?", [topic]
            if actor is not None:
                where += " AND owner_id=?"
                params.append(actor)
            total = c.execute(f"SELECT count(*) FROM saved_analysis_captures WHERE {where}", params).fetchone()[0]
            rows = c.execute(f"SELECT * FROM saved_analysis_captures WHERE {where} ORDER BY sequence DESC LIMIT ? OFFSET ?", [*params, limit, (page - 1) * limit]).fetchall()
        items = []
        for row in rows:
            value = _read(c, row)
            items.append(dict(analysis_id=value["analysis_id"], saved_at=value["saved_at"], paper_id=value["receipt"]["paper_id"],
                              model=value["bundle"]["plan"]["contract"]["model"], latest_decision=value["latest_decision"]))
        return dict(items=items, total=total, page=page, limit=limit)


def decide(topic: str, identity: str, actor: str, body: domain.Decision) -> dict[str, Any]:
    body = domain.Decision.model_validate(body.model_dump())
    request_digest = digest([body.model_dump(), actor])
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        value = _get(c, topic, identity, actor)
        old = c.execute("SELECT * FROM saved_analysis_decisions WHERE topic_id=? AND analysis_id=? AND request_id=?", (topic, identity, body.request_id)).fetchone()
        if old:
            if old["request_digest"] != request_digest:
                raise ValueError("saved_analysis_request_conflict")
            return value
        if body.expected_previous_id != (value["latest_decision"]["decision_id"] if value["latest_decision"] else None):
            raise ValueError("saved_analysis_review_conflict")
        decision = dict(decision_id="decision_" + digest([identity, body.request_id]), topic_id=topic, analysis_id=identity,
                        actor_id=actor, created_at=iso_now_utc(), state=body.state, note=body.note, command=body.model_dump())
        c.execute("INSERT INTO saved_analysis_decisions(decision_id,topic_id,analysis_id,previous_id,request_id,request_digest,payload_json,payload_digest) VALUES(?,?,?,?,?,?,?,?)",
                  (decision["decision_id"], topic, identity, body.expected_previous_id, body.request_id, request_digest, preserved_json(decision), digest(decision)))
        checkpoint("decision_saved")
        return _get(c, topic, identity, actor)
