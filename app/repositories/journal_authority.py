"""Append-only approval inputs and permanent revocation, on the execution DB.

Registration is an internal trusted-writer interface: clients cannot submit
records, proofs or grants. The isolated synthetic app issuer is not a live
authority. A live server composition must authenticate its approval source before
registering; the withdrawal API requires topic workspace write access.
Synthetic tests exercise persistence and ordering, not real approval provenance.
"""
from contextlib import closing
from datetime import datetime
import json
import sqlite3
from typing import Any

from app.db.bootstrap import connect_metadata_index
from app.domain import journal_authority as domain, journal_dispatch as dispatch
from app.domain.temporal_memory import digest
from app.domain.journal_exchange import preserved_json
from app.repositories import journal_executions as executions


def checkpoint(phase: str) -> None:
    """Transaction fault seam."""


def _exists(c: sqlite3.Connection) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='journal_authority_records'").fetchone() is not None


def requires_stored(c: sqlite3.Connection, execution_id: str) -> bool:
    return _exists(c) and c.execute("SELECT 1 FROM journal_authority_records WHERE execution_id=? LIMIT 1", (execution_id,)).fetchone() is not None


def _initialize(c: sqlite3.Connection) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS journal_authority_records (
        authorization_id TEXT PRIMARY KEY, execution_id TEXT NOT NULL,
        payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL)""")
    c.execute("CREATE INDEX IF NOT EXISTS journal_authority_execution ON journal_authority_records(execution_id)")
    c.execute("""CREATE TABLE IF NOT EXISTS journal_authority_revocations (
        authorization_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL)""")
    for table in ("journal_authority_records", "journal_authority_revocations"):
        for operation in ("UPDATE", "DELETE"):
            c.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} BEFORE {operation} ON {table}
                BEGIN SELECT RAISE(ABORT,'journal_authority_immutable'); END""")
    c.execute("""CREATE TRIGGER IF NOT EXISTS journal_authority_parent BEFORE INSERT ON journal_authority_records
        WHEN NOT EXISTS (SELECT 1 FROM journal_executions WHERE execution_id=NEW.execution_id)
        BEGIN SELECT RAISE(ABORT,'journal_authority_binding_invalid'); END""")
    c.execute("""CREATE TRIGGER IF NOT EXISTS journal_authority_revocation_parent BEFORE INSERT ON journal_authority_revocations
        WHEN NOT EXISTS (SELECT 1 FROM journal_authority_records WHERE authorization_id=NEW.authorization_id)
        BEGIN SELECT RAISE(ABORT,'journal_authority_binding_invalid'); END""")


def reference_from(record: dict[str, Any]) -> dispatch.AuthorityReference:
    return dispatch.AuthorityReference(authorization_id=record["grant"]["authorization_id"], record_digest=digest(record))


def _read(c: sqlite3.Connection, contract: dict[str, Any], authorization_id: str) -> tuple[domain.Record, domain.Revocation | None]:
    row = c.execute("SELECT * FROM journal_authority_records WHERE authorization_id=? AND execution_id=?",
                    (authorization_id, contract["execution_id"])).fetchone() if _exists(c) else None
    if row is None:
        raise KeyError("journal_authority_not_found")
    try:
        payload = json.loads(row["payload_json"])
        record = domain.read_record(payload, contract)
        if digest(payload) != row["payload_digest"] or record.grant.authorization_id != authorization_id:
            raise ValueError()
        revoked = c.execute("SELECT * FROM journal_authority_revocations WHERE authorization_id=?", (authorization_id,)).fetchone()
        revocation = domain.read_revocation(json.loads(revoked["payload_json"])) if revoked else None
        if revocation is not None and (revocation.authorization_id != authorization_id or revocation.record_digest != digest(payload)
                                      or digest(revocation.model_dump(mode="json")) != revoked["payload_digest"]
                                      or revocation.at < record.registered_at):
            raise ValueError()
        return record, revocation
    except (KeyError, ValueError, TypeError, AttributeError):
        raise ValueError("journal_authority_integrity_invalid") from None


def register(topic: str, job_id: str, execution_id: str, grant: dispatch.Grant, binding: dispatch.AdapterBinding,
             *, issuer: str, decision_ref: str, expected_sequence: int, now: datetime | None = None) -> tuple[dict[str, Any], bool]:
    stamp = executions._time(now)
    record = domain.Record(topic_id=topic, job_id=job_id, execution_id=execution_id, grant=grant, binding=binding,
                           registered_by=issuer, decision_ref=decision_ref, expected_sequence=expected_sequence, registered_at=stamp)
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        contract, events = executions._read(c, topic, job_id, execution_id)
        _initialize(c)
        if c.execute("SELECT 1 FROM journal_authority_records WHERE authorization_id=?", (grant.authorization_id,)).fetchone():
            old, _ = _read(c, contract, grant.authorization_id)
            if record.model_copy(update={"registered_at": old.registered_at}) != old:
                raise ValueError("journal_authority_conflict")
            return old.model_dump(mode="json"), False  # Never un-revoke or renew a record on replay.
        current = events[-1] if events else executions._initial()
        if current["sequence"] != expected_sequence or current["state"] not in {"READY", "FAILED", "INTERRUPTED"}:
            raise ValueError("journal_authority_conflict")
        payload = record.model_dump(mode="json")
        domain.read_record(payload, contract)
        c.execute("INSERT INTO journal_authority_records VALUES(?,?,?,?)",
                  (grant.authorization_id, execution_id, preserved_json(payload), digest(payload)))
        checkpoint("after_registration")
        return payload, True


def detail(topic: str, job_id: str, execution_id: str, authorization_id: str,
           expected_record_digest: str | None = None) -> dict[str, Any]:
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        contract, _ = executions._read(c, topic, job_id, execution_id)
        record, revoked = _read(c, contract, authorization_id)
        if expected_record_digest is not None and digest(record.model_dump(mode="json")) != expected_record_digest:
            raise ValueError("journal_authority_changed")
        return dict(record=record.model_dump(mode="json"), revocation=revoked.model_dump(mode="json") if revoked else None)


def register_record(topic: str, job_id: str, execution_id: str, value: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Register an exact trusted source record, checking current time under lock.

    Unlike legacy register(), this never substitutes the registration timestamp.
    Source authentication and application authorization belong to the caller.
    An existing identical row is readback, including after expiry or revocation.
    """
    # Freeze caller-owned objects before blocking on the writer lock.
    payload_json = preserved_json(value)
    value = json.loads(payload_json)
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        contract, events = executions._read(c, topic, job_id, execution_id)
        record = domain.read_record(value, contract)
        if (preserved_json(record.model_dump(mode="json")) != payload_json
                or record.grant.actor_id != contract["actor_id"]):
            raise ValueError("journal_authority_integrity_invalid")
        _initialize(c)
        if c.execute("SELECT 1 FROM journal_authority_records WHERE authorization_id=?",
                     (record.grant.authorization_id,)).fetchone():
            try:
                old, _ = _read(c, contract, record.grant.authorization_id)
            except KeyError:
                raise ValueError("journal_authority_conflict") from None
            if preserved_json(old.model_dump(mode="json")) != payload_json:
                raise ValueError("journal_authority_conflict")
            return old.model_dump(mode="json"), False
        current = events[-1] if events else executions._initial()
        if current["sequence"] != record.expected_sequence or current["state"] not in {"READY", "FAILED", "INTERRUPTED"}:
            raise ValueError("journal_authority_conflict")
        # Never sample before BEGIN IMMEDIATE: a grant may expire while waiting.
        stamp = executions._time(None)
        if record.registered_at > stamp:
            raise ValueError("journal_authority_clock_invalid")
        dispatch.validate_grant(record.grant, contract, contract["actor_id"], record.binding, stamp)
        c.execute("INSERT INTO journal_authority_records VALUES(?,?,?,?)",
                  (record.grant.authorization_id, execution_id, payload_json, digest(value)))
        checkpoint("after_registration")
        return value, True


def listing(topic: str, job_id: str, execution_id: str, *, limit: int = 25) -> dict[str, Any]:
    """Bounded read-only approval history, including approvals never dispatched."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("pagination_invalid")
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        contract, _ = executions._read(c, topic, job_id, execution_id)
        if not _exists(c):
            return dict(items=[], total=0)
        total = c.execute("SELECT count(*) FROM journal_authority_records WHERE execution_id=?", (execution_id,)).fetchone()[0]
        ids = c.execute("SELECT authorization_id FROM journal_authority_records WHERE execution_id=? ORDER BY rowid DESC LIMIT ?",
                        (execution_id, limit)).fetchall()
        items = []
        for row in ids:
            record, revoked = _read(c, contract, row[0])
            items.append(dict(record=record.model_dump(mode="json"), revocation=revoked.model_dump(mode="json") if revoked else None))
        return dict(items=items, total=total)


def current_grant(c: sqlite3.Connection, contract: dict[str, Any], ref: dispatch.AuthorityReference,
                  actor: str, binding: dispatch.AdapterBinding, now: datetime) -> dispatch.Grant:
    record, revoked = _read(c, contract, ref.authorization_id)
    if reference_from(record.model_dump(mode="json")) != ref or record.binding != binding:
        raise ValueError("journal_authority_changed")
    if revoked is not None:
        raise ValueError("journal_authority_revoked")
    dispatch.validate_grant(record.grant, contract, actor, binding, now)
    return record.grant


def resolve(topic: str, job_id: str, execution_id: str, authorization_id: str, actor: str,
            binding: dispatch.AdapterBinding, *, now: datetime | None = None) -> dispatch.AuthorityReference:
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        contract, _ = executions._read(c, topic, job_id, execution_id)
        record, _ = _read(c, contract, authorization_id)
        ref = reference_from(record.model_dump(mode="json"))
        current_grant(c, contract, ref, actor, binding, executions._time(now))
        return ref


def revoke(topic: str, job_id: str, execution_id: str, authorization_id: str, *, request_id: str,
           issuer: str, reason: str, now: datetime | None = None,
           expected_record_digest: str | None = None) -> tuple[dict[str, Any], bool]:
    stamp = executions._time(now)
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        contract, events = executions._read(c, topic, job_id, execution_id)
        record, old = _read(c, contract, authorization_id)
        if expected_record_digest is not None and digest(record.model_dump(mode="json")) != expected_record_digest:
            raise ValueError("journal_authority_changed")
        revoked = domain.Revocation(authorization_id=authorization_id, record_digest=digest(record.model_dump(mode="json")),
                                    request_id=request_id, actor_id=issuer, reason=reason, at=stamp)
        if old is not None:
            if revoked.model_copy(update={"at": old.at}) != old:
                raise ValueError("journal_authority_conflict")
            return old.model_dump(mode="json"), False
        if stamp < record.registered_at:
            raise ValueError("journal_authority_clock_invalid")
        payload = revoked.model_dump(mode="json")
        c.execute("INSERT INTO journal_authority_revocations VALUES(?,?,?)", (authorization_id, preserved_json(payload), digest(payload)))
        current = events[-1] if events else executions._initial()
        lease = next((row for row in reversed(events) if row["state"] == "LEASED" and row["attempt"] == current["attempt"]), None)
        receipt = dispatch.read_receipt(lease["dispatch"]) if lease and "dispatch" in lease else None
        if (current["state"] in {"LEASED", "RUNNING"} and isinstance(receipt, dispatch.RegisteredReceipt)
                and receipt.authority.authorization_id == authorization_id):
            if receipt.authority != reference_from(record.model_dump(mode="json")):
                raise ValueError("journal_authority_changed")
            started = current["state"] == "RUNNING"
            executions._append(c, contract, events, stamp, state="INTERRUPTED" if started else "FAILED", outcome_unknown=started,
                               reason="dispatch_authority_revoked_" + ("after_dispatch" if started else "before_dispatch"),
                               authority_revocation=payload)
        checkpoint("after_revocation")
        return payload, True
