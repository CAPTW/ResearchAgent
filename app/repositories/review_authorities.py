"""Trusted internal paper approvals. Registration and execution fences are atomic.

These functions are not HTTP endpoints. A structurally valid unsigned record is
not an authenticated issuer; only the server's independently trusted resolver
may supply it. No resolver is configured by the application.
"""
import json
import sqlite3
from datetime import datetime
from typing import Any

from app.domain import review_authority as authority, review_execution as domain, review_transport_execution as transport
from app.domain.review_preparation import encoded, sha
from app.repositories import review_executions as executions
from app.repositories.files import iso_now_utc


def _initialize(c: sqlite3.Connection) -> None:
    for table in ("paper_review_authorities", "paper_review_authority_revocations"):
        c.execute(f"""CREATE TABLE IF NOT EXISTS {table} (
            authorization_id TEXT PRIMARY KEY, execution_id TEXT NOT NULL,
            payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL)""")
        for operation in ("UPDATE", "DELETE"):
            c.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} BEFORE {operation} ON {table}
                BEGIN SELECT RAISE(ABORT,'paper_review_authority_immutable'); END""")


def _row(c: sqlite3.Connection, table: str, identity: str) -> sqlite3.Row | None:
    if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
        return None
    return c.execute(f"SELECT * FROM {table} WHERE authorization_id=?", (identity,)).fetchone()


def _payload(row: sqlite3.Row, execution: str) -> dict[str, Any]:
    value = json.loads(row["payload_json"])
    if row["execution_id"] != execution or not isinstance(value, dict) or sha(encoded(value)) != row["payload_sha256"]:
        raise ValueError("paper_review_authority_integrity_invalid")
    return value


def _record(c: sqlite3.Connection, value: dict[str, Any], ref: dict[str, Any]) -> authority.AuthorityRecord:
    reference = authority.Reference.model_validate(ref, strict=True)
    row = _row(c, "paper_review_authorities", reference.authorization_id)
    if row is None:
        raise ValueError("paper_review_authority_missing")
    payload = _payload(row, value["execution_id"])
    record = authority.read_record(payload, value)
    if authority.reference_from(payload) != reference:
        raise ValueError("paper_review_authority_integrity_invalid")
    return record


def validate_history_in(c: sqlite3.Connection, value: dict[str, Any], events: list[dict[str, Any]]) -> None:
    for event in events:
        if event["state"] != "APPROVED":
            continue
        record = _record(c, value, event["authority"])
        authority.validate_grant(record.grant, value, domain.timestamp(event["at"]))
        if (record.expected_sequence != event["sequence"] - 1
                or record.registered_at > domain.timestamp(event["at"])
                or domain.timestamp(event["deadline"]) != record.grant.expires_at):
            raise ValueError("paper_review_authority_integrity_invalid")
        revoked = _row(c, "paper_review_authority_revocations", record.grant.authorization_id)
        if revoked is not None:
            payload = _payload(revoked, value["execution_id"])
            revocation = authority.read_revocation(payload)
            if (revocation.authorization_id != record.grant.authorization_id
                    or revocation.record_sha256 != event["authority"]["record_sha256"]
                    or revocation.actor_id != value["actor_id"] or revocation.at < domain.timestamp(event["at"])):
                raise ValueError("paper_review_authority_integrity_invalid")
            fenced = [item for item in events if item["state"] == "REVOKED"]
            if fenced:
                fence = fenced[0]
                if (domain.timestamp(fence["at"]) != revocation.at
                        or fence["command"]["body"]["request_id"] != revocation.request_id):
                    raise ValueError("paper_review_authority_integrity_invalid")
            elif events[-1]["state"] in domain.ACTIVE | {"RESULT_RECEIVED"}:
                raise ValueError("paper_review_authority_integrity_invalid")
            elif revocation.at < domain.timestamp(events[-1]["at"]):
                raise ValueError("paper_review_authority_integrity_invalid")
        elif any(item["state"] == "REVOKED" for item in events):
            raise ValueError("paper_review_authority_integrity_invalid")


def current_in(c: sqlite3.Connection, saved: dict[str, Any]) -> authority.AuthorityRecord:
    event = next((item for item in saved["events"] if item["state"] == "APPROVED"), None)
    if event is None:
        raise ValueError("paper_review_authority_missing")
    record = _record(c, saved["contract"], event["authority"])
    if _row(c, "paper_review_authority_revocations", record.grant.authorization_id) is not None:
        raise ValueError("paper_review_authority_revoked")
    authority.validate_grant(record.grant, saved["contract"], domain.timestamp(iso_now_utc()))
    return record


def describe_in(c: sqlite3.Connection, saved: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    """Projection of already validated history, never an execution permission.

    Use the caller's existing read snapshot. In particular, a revocation after a
    saved result has no new execution event, but still belongs in this view.
    """
    event = next((item for item in saved["events"] if item["state"] == "APPROVED"), None)
    if event is None:
        return None
    record = _record(c, saved["contract"], event["authority"])
    grant = record.grant
    row = _row(c, "paper_review_authority_revocations", grant.authorization_id)
    revoked_at = (authority.read_revocation(_payload(row, saved["contract"]["execution_id"])).at if row is not None
        else grant.proof.revoked_at if isinstance(grant, authority.Grant) else None)
    if row is not None or (revoked_at is not None and revoked_at <= now):
        validity = "REVOKED"
    elif now >= grant.expires_at:
        validity = "EXPIRED"
    elif now < grant.granted_at:
        validity = "NOT_YET_VALID"
    else:
        validity = "WITHIN_VALIDITY_WINDOW"
    return dict(**event["authority"], issuer_id=record.issuer_id, policy_version=grant.policy_version,
        granted_at=grant.granted_at.isoformat(), expires_at=grant.expires_at.isoformat(),
        revoked_at=revoked_at.isoformat() if revoked_at is not None else None, validity=validity,
        **authority.first_output_projection(grant))


def register(scope: domain.Scope, identity: str, command: domain.Control, value: dict[str, Any]) -> dict[str, Any]:
    if command.action != "approve":
        raise ValueError("paper_review_authority_command_invalid")
    with executions._write(scope) as (c, preparation):
        saved = executions._schema(executions._detail(c, scope, identity, preparation), transport.SCHEMA)
        body = dict(kind="control", body=command.model_dump())
        record = authority.read_record(value, saved["contract"])
        ref = authority.reference_from(value).model_dump()
        existing = _row(c, "paper_review_authorities", record.grant.authorization_id)
        if executions._replay(saved, body):
            approval = next(item for item in saved["events"] if item["state"] == "APPROVED")
            if existing is None or approval["authority"] != ref or encoded(_payload(existing, identity)) != encoded(value):
                raise ValueError("paper_review_authority_conflict")
            return saved
        if existing is not None or saved["current"]["state"] != "READY" or record.expected_sequence != command.expected_sequence:
            raise ValueError("paper_review_authority_conflict")
        now = iso_now_utc()
        authority.validate_grant(record.grant, saved["contract"], domain.timestamp(now))
        if record.registered_at > domain.timestamp(now):
            raise ValueError("paper_review_authority_future")
        spec = transport.Prepare.model_validate(saved["contract"]["specification"], strict=True)
        if isinstance(record.grant, authority.OwnerFirstOutputGrant):
            # A first-output decision cannot be rotated onto another request or
            # authorization ID, even after revocation. The writer lock makes
            # this check and insertion atomic without changing old table bytes.
            _initialize(c)
            used = c.execute("""SELECT 1 FROM paper_review_authorities
                WHERE json_extract(payload_json, '$.schema_version') = ?
                  AND json_extract(payload_json, '$.grant.decision_sha256') = ? LIMIT 1""",
                (record.schema_version, record.grant.decision_sha256)).fetchone()
            if used is not None:
                raise ValueError("paper_review_authority_conflict")
        executions._admit(c, scope, preparation, spec, active=True, identity=identity)
        _initialize(c)
        c.execute("INSERT INTO paper_review_authorities VALUES (?,?,?,?)", (record.grant.authorization_id, identity, encoded(value), sha(encoded(value))))
        executions.checkpoint("after_authority")
        return executions._append(c, saved, "APPROVED", command=body, authority=ref,
            deadline=record.grant.expires_at.isoformat(), at=now)


def revoke(scope: domain.Scope, identity: str, command: domain.Control, *, reason: str) -> dict[str, Any]:
    if command.action != "revoke":
        raise ValueError("paper_review_authority_command_invalid")
    with executions._write(scope) as (c, preparation):
        saved = executions._schema(executions._detail(c, scope, identity, preparation), transport.SCHEMA)
        approved = next((item for item in saved["events"] if item["state"] == "APPROVED"), None)
        if approved is None:
            raise ValueError("paper_review_authority_missing")
        record = _record(c, saved["contract"], approved["authority"])
        existing = _row(c, "paper_review_authority_revocations", record.grant.authorization_id)
        if existing is not None:
            prior = authority.read_revocation(_payload(existing, identity))
            if prior.request_id == command.request_id and prior.reason == reason:
                # Also bind the original sequence, even after a lost response.
                revoked = next((event for event in saved["events"] if event["state"] == "REVOKED"), None)
                sequence = revoked["sequence"] - 1 if revoked else saved["current"]["sequence"]
                if command.expected_sequence == sequence:
                    return saved
            raise ValueError("paper_review_authority_conflict")
        body = dict(kind="control", body=command.model_dump())
        executions._replay(saved, body)
        now = iso_now_utc()
        if domain.timestamp(now) < domain.timestamp(saved["current"]["at"]):
            raise ValueError("paper_review_authority_chronology_invalid")
        value = authority.Revocation(**approved["authority"], request_id=command.request_id,
            actor_id=scope.actor, reason=reason, at=domain.timestamp(now)).model_dump(mode="json")
        c.execute("INSERT INTO paper_review_authority_revocations VALUES (?,?,?,?)",
            (record.grant.authorization_id, identity, encoded(value), sha(encoded(value))))
        executions.checkpoint("after_revocation")
        if saved["current"]["state"] in domain.ACTIVE | {"RESULT_RECEIVED"}:
            return executions._append(c, saved, "REVOKED", command=body, at=now)
        return saved  # Completed evidence remains readable; authority stays revoked.


def replay_revocation(scope: domain.Scope, identity: str, command: domain.Control, *, reason: str) -> bool:
    """Recognize a completed local revocation without granting a new mutation.

    Terminal executions retain their events, so their exact revocation replay
    must use the separate append-only authority record, even with runtime off.
    """
    from contextlib import closing
    from app.db.bootstrap import connect_metadata_index

    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        saved = executions._schema(executions._detail(c, scope, identity, executions._preparation(c, scope)), transport.SCHEMA)
        approved = next((event for event in saved["events"] if event["state"] == "APPROVED"), None)
        if approved is None:
            return False
        row = _row(c, "paper_review_authority_revocations", approved["authority"]["authorization_id"])
        if row is None:
            return False
        prior = authority.read_revocation(_payload(row, identity))
        revoked = next((event for event in saved["events"] if event["state"] == "REVOKED"), None)
        sequence = revoked["sequence"] - 1 if revoked else saved["current"]["sequence"]
        return (command.action == "revoke" and prior.actor_id == scope.actor and prior.request_id == command.request_id
                and prior.reason == reason and command.expected_sequence == sequence)
