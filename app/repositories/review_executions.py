"""Append-only paper execution transactions; never call an adapter under a lock."""
from contextlib import closing, contextmanager
from datetime import timedelta
import json
import secrets
import sqlite3
from typing import Any, Iterator

from app.db.bootstrap import connect_metadata_index
from app.domain import review_execution as domain
from app.domain import review_transport_execution as transport
from app.domain.review_execution import Claim, Control, Dispatch, Prepare, Scope
from app.domain.review_preparation import encoded, sha
from app.repositories import ai_review_drafts, review_preparations
from app.repositories.files import iso_now_utc

APPROVAL_SECONDS = 60
LEASE_SECONDS = 60


def checkpoint(phase: str) -> None:
    """Failure seam for testing atomic receipt publication."""


def _exists(c: sqlite3.Connection) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_review_executions'").fetchone() is not None


def _initialize(c: sqlite3.Connection) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS paper_review_executions (
        ordinal INTEGER PRIMARY KEY AUTOINCREMENT, execution_id TEXT NOT NULL UNIQUE,
        topic_id TEXT NOT NULL, paper_version_id TEXT NOT NULL, preparation_id TEXT NOT NULL,
        actor_id TEXT NOT NULL, payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL)""")
    c.execute("""CREATE INDEX IF NOT EXISTS paper_execution_scope ON paper_review_executions
        (topic_id,paper_version_id,preparation_id,actor_id,ordinal)""")
    c.execute("""CREATE TABLE IF NOT EXISTS paper_review_execution_events (
        execution_id TEXT NOT NULL, sequence INTEGER NOT NULL, payload_json TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL, PRIMARY KEY(execution_id,sequence))""")
    for table in ("paper_review_executions", "paper_review_execution_events"):
        for operation in ("UPDATE", "DELETE"):
            c.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} BEFORE {operation} ON {table}
                BEGIN SELECT RAISE(ABORT,'review_execution_immutable'); END""")


def _preparation(c: sqlite3.Connection, scope: Scope) -> dict[str, Any]:
    return review_preparations.detail_in(c, scope.topic, scope.version, scope.preparation, list(scope.occurrences))


@contextmanager
def _write(scope: Scope) -> Iterator[tuple[sqlite3.Connection, dict[str, Any]]]:
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        preparation = _preparation(c, scope)
        yield c, preparation


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    value = json.loads(row["payload_json"])
    if not isinstance(value, dict) or sha(encoded(value)) != row["payload_sha256"]:
        raise ValueError("review_execution_integrity_invalid")
    return value


def _read(c: sqlite3.Connection, scope: Scope, row: sqlite3.Row, preparation: dict[str, Any]) -> dict[str, Any]:
    try:
        value = _decode(row)
        expected = _rebuild(scope, value, preparation)
        if (encoded(value) != encoded(expected)
                or any(row[key] != value[key] for key in ("execution_id", "topic_id", "paper_version_id", "preparation_id", "actor_id"))):
            raise ValueError()
        events = []
        for item in c.execute("SELECT * FROM paper_review_execution_events WHERE execution_id=? ORDER BY sequence", (value["execution_id"],)):
            event = _decode(item)
            if event["sequence"] != item["sequence"] or event["execution_id"] != item["execution_id"]:
                raise ValueError()
            events.append(event)
        domain.validate_events(value, events)
        if value["schema_version"] == transport.SCHEMA:
            from app.repositories.review_authorities import validate_history_in
            validate_history_in(c, value, events)
        for index, event in enumerate(events):
            if event["state"] == "RESULT_SAVED":
                receipt = ai_review_drafts.get_input_receipt_in(c, scope.topic, event["receipt_id"], occurrences=list(scope.occurrences))
                record = events[index - 1]["record"]
                if (receipt is None or receipt["input_origin"] != value.get("input_origin", "FIXTURE_SYNTHETIC")
                        or receipt["packet"] != value["prepared"]["packet"]
                        or any(encoded(receipt[key]) != encoded(data) for key, data in record.items())):
                    raise ValueError()
        return dict(contract=value, events=events, current=events[-1])
    except (ValueError, KeyError, TypeError, IndexError, AttributeError, RecursionError):
        raise ValueError("review_execution_integrity_invalid") from None


def _detail(c: sqlite3.Connection, scope: Scope, identity: str, preparation: dict[str, Any]) -> dict[str, Any]:
    if _exists(c):
        row = c.execute("""SELECT * FROM paper_review_executions WHERE execution_id=? AND topic_id=?
            AND paper_version_id=? AND preparation_id=? AND actor_id=?""",
            (identity, scope.topic, scope.version, scope.preparation, scope.actor)).fetchone()
        if row is not None:
            return _read(c, scope, row, preparation)
    raise KeyError("review_execution_not_found")


def _rebuild(scope: Scope, value: dict[str, Any], preparation: dict[str, Any]) -> dict[str, Any]:
    if value["schema_version"] == domain.SCHEMA:
        return domain.contract(scope, Prepare.model_validate(value["specification"], strict=True), preparation, value["created_at"],
                               prepared_schema=value["prepared"]["schema_version"])
    if value["schema_version"] == transport.SCHEMA:
        return transport.contract(scope, transport.Prepare.model_validate(value["specification"], strict=True), preparation,
            value["created_at"], value["transport"], value["input_origin"], prepared_schema=value["prepared"]["schema_version"])
    raise ValueError("review_execution_integrity_invalid")


def _schema(saved: dict[str, Any], schema: str) -> dict[str, Any]:
    if saved["contract"]["schema_version"] != schema:
        raise KeyError("review_execution_not_found")
    return saved


def detail(scope: Scope, identity: str, *, schema: str = domain.SCHEMA) -> dict[str, Any]:
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        return _schema(_detail(c, scope, identity, _preparation(c, scope)), schema)


def _blocking(c: sqlite3.Connection, scope: Scope, preparation: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate before filtering: corrupt JSON must not hide an active attempt."""
    if not _exists(c):
        return []
    result = []
    for row in c.execute("""SELECT * FROM paper_review_executions
        WHERE topic_id=? AND paper_version_id=? AND preparation_id=? AND actor_id=?""",
        (scope.topic, scope.version, scope.preparation, scope.actor)):
        item = _read(c, scope, row, preparation)
        if item["current"]["outcome_unknown"] or item["current"]["state"] in domain.ACTIVE:
            result.append(item)
    return result


def _page_start(page: int, page_size: int) -> int:
    if type(page) is not int or type(page_size) is not int or page < 1 or not 1 <= page_size <= 50:
        raise ValueError("review_execution_page_invalid")
    start = (page - 1) * page_size
    if start > 9223372036854775807:
        raise ValueError("review_execution_page_invalid")
    return start


def _listing_in(c: sqlite3.Connection, scope: Scope, page: int, page_size: int, *, schema: str) -> dict[str, Any]:
    start = _page_start(page, page_size)
    preparation = _preparation(c, scope)
    items = []
    validated = []
    if _exists(c):
        rows = c.execute("""SELECT * FROM paper_review_executions
            WHERE topic_id=? AND paper_version_id=? AND preparation_id=? AND actor_id=?
            ORDER BY ordinal DESC""",
            (scope.topic, scope.version, scope.preparation, scope.actor)).fetchall()
        # Validate before filtering, including the other version's rows.
        validated = [_read(c, scope, row, preparation) for row in rows]
        items = [item for item in validated if item["contract"]["schema_version"] == schema][start:start + page_size + 1]
    return dict(preparation_digest=sha(encoded(preparation)), items=items[:page_size],
                page=page, pageSize=page_size, hasMore=len(items) > page_size,
                outcome_unknown_pending=any(item["current"]["outcome_unknown"] for item in validated))


def listing(scope: Scope, page: int, page_size: int, *, schema: str = domain.SCHEMA) -> dict[str, Any]:
    _page_start(page, page_size)  # Reject invalid offsets before opening storage.
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        return _listing_in(c, scope, page, page_size, schema=schema)


def _append(c: sqlite3.Connection, detail: dict[str, Any], state: str, *, command: dict[str, Any] | None = None,
            deadline: str | None = None, token: str | None = None, reason: str | None = None,
            receipt: str | None = None, record: dict[str, Any] | None = None,
            authority: dict[str, Any] | None = None, at: str | None = None) -> dict[str, Any]:
    value, events = detail["contract"], detail["events"]
    prior = events[-1] if events else None
    event = dict(schema_version=domain.EVENT_SCHEMA, execution_id=value["execution_id"], sequence=len(events),
                 state=state, at=at or (iso_now_utc() if prior else value["created_at"]), deadline=deadline,
                 claim_sha256=token, command=command, record=record, reason=reason, receipt_id=receipt,
                 outcome_unknown=bool(prior and prior["state"] == "RUNNING" and state != "RESULT_RECEIVED"),
                 previous_sha256=sha(encoded(prior if prior else value)))
    if value["schema_version"] == transport.SCHEMA:
        event.update(schema_version=transport.EVENT_SCHEMA, authority=authority)
        if state == "FAILED" and reason in transport.KNOWN_FAILURE_REASONS:
            event["outcome_unknown"] = False
    updated = [*events, event]
    domain.validate_events(value, updated)
    c.execute("INSERT INTO paper_review_execution_events VALUES (?,?,?,?)",
              (value["execution_id"], event["sequence"], encoded(event), sha(encoded(event))))
    return dict(contract=value, events=updated, current=event)


def _admit(c: sqlite3.Connection, scope: Scope, preparation: dict[str, Any], body: Prepare, *, active: bool, identity: str) -> None:
    related = _blocking(c, scope, preparation)
    if not body.acknowledge_unknown and any(item["current"]["outcome_unknown"] for item in related):
        raise ValueError("review_execution_unknown_outcome")
    if active and any(item["contract"]["execution_id"] != identity and item["current"]["state"] in domain.ACTIVE for item in related):
        raise ValueError("review_execution_busy")


def prepare(scope: Scope, body: Prepare, *, enabled: bool) -> dict[str, Any]:
    with _write(scope) as (c, preparation):
        identity = domain.identity(scope, body.request_id)
        if _exists(c):
            row = c.execute("SELECT * FROM paper_review_executions WHERE execution_id=?", (identity,)).fetchone()
            if row is not None:
                if row["preparation_id"] != scope.preparation:
                    raise ValueError("review_execution_request_conflict")
                saved = _read(c, scope, row, preparation)
                if saved["contract"]["specification"] != body.model_dump():
                    raise ValueError("review_execution_request_conflict")
                return saved
        if not enabled:
            raise ValueError("review_execution_disabled")
        value = domain.contract(scope, body, preparation, iso_now_utc())
        _admit(c, scope, preparation, body, active=False, identity=identity)
        _initialize(c)
        c.execute("""INSERT INTO paper_review_executions
            (execution_id,topic_id,paper_version_id,preparation_id,actor_id,payload_json,payload_sha256) VALUES (?,?,?,?,?,?,?)""",
            (identity, scope.topic, scope.version, scope.preparation, scope.actor, encoded(value), sha(encoded(value))))
        return _append(c, dict(contract=value, events=[]), "READY")


def _replay(detail: dict[str, Any], command: dict[str, Any]) -> bool:
    for event in detail["events"]:
        prior = event["command"]
        if prior and prior["body"]["request_id"] == command["body"]["request_id"]:
            if encoded(prior) != encoded(command):
                raise ValueError("review_execution_request_conflict")
            return True
    if detail["current"]["sequence"] != command["body"]["expected_sequence"]:
        raise ValueError("review_execution_changed")
    return False


def _expired(event: dict[str, Any]) -> bool:
    return event["deadline"] is not None and domain.timestamp(iso_now_utc()) >= domain.timestamp(event["deadline"])


def _deadline(seconds: int) -> str:
    return (domain.timestamp(iso_now_utc()) + timedelta(seconds=seconds)).isoformat()


def claim(scope: Scope, identity: str, body: Dispatch, *, enabled: bool,
          schema: str = domain.SCHEMA) -> tuple[dict[str, Any], Claim | None]:
    with _write(scope) as (c, preparation):
        saved = _schema(_detail(c, scope, identity, preparation), schema)
        command = dict(kind="dispatch", body=body.model_dump())
        if _replay(saved, command):
            return saved, None  # Never hand the first claim token to a replay.
        if not enabled:
            raise ValueError("review_execution_disabled")
        if saved["current"]["state"] != "APPROVED" or _expired(saved["current"]):
            raise ValueError("review_execution_not_approved")
        if schema == transport.SCHEMA:
            from app.repositories.review_authorities import current_in
            current_in(c, saved)
        spec = (transport.Prepare if schema == transport.SCHEMA else Prepare).model_validate(saved["contract"]["specification"], strict=True)
        _admit(c, scope, preparation, spec, active=True, identity=identity)
        token = secrets.token_hex(32)
        # Admission does not extend the approval's lifetime. A delayed worker
        # must still have a valid approval at actual start and result reception.
        deadline = min(domain.timestamp(saved["current"]["deadline"]), domain.timestamp(_deadline(LEASE_SECONDS))).isoformat()
        if schema == transport.SCHEMA:
            # V2 uses the independently issued, proof-bounded grant window
            # (at most five minutes). V1 retains its fixed synthetic lease.
            # No claim/start/replay can extend that window.
            deadline = saved["current"]["deadline"]
        leased = _append(c, saved, "LEASED", command=command, token=sha(token), deadline=deadline)
        return leased, Claim(scope, identity, token, schema)


def _publish(c: sqlite3.Connection, saved: dict[str, Any], command: dict[str, Any] | None = None) -> dict[str, Any]:
    if saved["current"]["state"] != "RESULT_RECEIVED":
        raise ValueError("review_execution_result_not_received")
    receipt = ai_review_drafts.append_input_receipt_in(c, saved["current"]["record"],
        input_packet=saved["contract"]["prepared"]["packet"], input_origin=saved["contract"].get("input_origin", "FIXTURE_SYNTHETIC"))
    checkpoint("after_receipt")
    return _append(c, saved, "RESULT_SAVED", command=command, receipt=receipt["receipt_id"])


def control(scope: Scope, identity: str, body: Control, *, enabled: bool, schema: str = domain.SCHEMA) -> dict[str, Any]:
    with _write(scope) as (c, preparation):
        saved = _schema(_detail(c, scope, identity, preparation), schema)
        if schema == transport.SCHEMA and body.action in {"approve", "revoke"}:
            raise ValueError("paper_review_trusted_authority_required")
        command = dict(kind="control", body=body.model_dump())
        if _replay(saved, command):
            return saved
        current = saved["current"]
        if body.action == "save_result":
            return _publish(c, saved, command)
        if body.action == "recover":
            if current["state"] not in domain.ACTIVE or not _expired(current):
                raise ValueError("review_execution_not_expired")
            return _append(c, saved, "INTERRUPTED" if current["state"] == "RUNNING" else "FAILED",
                           command=command, reason="deadline_expired")
        target = {"approve": "APPROVED", "cancel": "CANCELLED", "revoke": "REVOKED"}[body.action]
        if target not in domain.TRANSITIONS.get(current["state"], set()):
            raise ValueError("review_execution_transition_invalid")
        if body.action == "approve":
            if not enabled:
                raise ValueError("review_execution_disabled")
            spec = Prepare.model_validate(saved["contract"]["specification"], strict=True)
            _admit(c, scope, preparation, spec, active=True, identity=identity)
            return _append(c, saved, target, command=command, deadline=_deadline(APPROVAL_SECONDS))
        return _append(c, saved, target, command=command)


def worker_start(claim: Claim) -> dict[str, Any] | None:
    with _write(claim.scope) as (c, preparation):
        saved = _schema(_detail(c, claim.scope, claim.identity, preparation), claim.schema)
        current = saved["current"]
        if current["state"] != "LEASED" or current["claim_sha256"] != sha(claim.token):
            return None
        if _expired(current):
            _append(c, saved, "FAILED", reason="deadline_expired")
            return None
        if claim.schema == transport.SCHEMA:
            from app.repositories.review_authorities import current_in
            try:
                current_in(c, saved)
            except ValueError:
                _append(c, saved, "FAILED", reason="authority_invalid")
                return None
        return _append(c, saved, "RUNNING", token=current["claim_sha256"], deadline=current["deadline"])


def receive(claim: Claim, record: dict[str, Any]) -> dict[str, Any] | None:
    with _write(claim.scope) as (c, preparation):
        saved = _schema(_detail(c, claim.scope, claim.identity, preparation), claim.schema)
        current = saved["current"]
        if current["state"] != "RUNNING" or current["claim_sha256"] != sha(claim.token):
            return None
        if _expired(current):
            _append(c, saved, "INTERRUPTED", reason="deadline_expired")
            return None
        domain.validate_record(saved["contract"], record)
        return _append(c, saved, "RESULT_RECEIVED", record=record)


def publish(claim: Claim) -> None:
    with _write(claim.scope) as (c, preparation):
        saved = _schema(_detail(c, claim.scope, claim.identity, preparation), claim.schema)
        if (saved["current"]["state"] == "RESULT_RECEIVED"
                and saved["events"][-2]["claim_sha256"] == sha(claim.token)):
            _publish(c, saved)


def interrupt(claim: Claim, reason: str) -> None:
    with _write(claim.scope) as (c, preparation):
        saved = _schema(_detail(c, claim.scope, claim.identity, preparation), claim.schema)
        current = saved["current"]
        if current["state"] in {"LEASED", "RUNNING"} and current["claim_sha256"] == sha(claim.token):
            known = claim.schema == transport.SCHEMA and reason in transport.KNOWN_FAILURE_REASONS
            _append(c, saved, "INTERRUPTED" if current["state"] == "RUNNING" and not known else "FAILED", reason=reason)


def prepare_transport(scope: Scope, body: transport.Prepare, manifest: dict[str, Any], *, input_origin: str) -> dict[str, Any]:
    """Trusted internal composition only; no browser or environment activation."""
    with _write(scope) as (c, preparation):
        identity = transport.identity(scope, body.request_id)
        if _exists(c):
            row = c.execute("SELECT * FROM paper_review_executions WHERE execution_id=?", (identity,)).fetchone()
            if row is not None:
                if row["preparation_id"] != scope.preparation:
                    raise ValueError("review_execution_request_conflict")
                saved = _read(c, scope, row, preparation)
                expected = transport.contract(scope, body, preparation, saved["contract"]["created_at"], manifest, input_origin,
                                              prepared_schema=saved["contract"]["prepared"]["schema_version"])
                if encoded(expected) != encoded(saved["contract"]):
                    raise ValueError("review_execution_request_conflict")
                return saved
        value = transport.contract(scope, body, preparation, iso_now_utc(), manifest, input_origin)
        _admit(c, scope, preparation, body, active=False, identity=identity)
        _initialize(c)
        c.execute("""INSERT INTO paper_review_executions
            (execution_id,topic_id,paper_version_id,preparation_id,actor_id,payload_json,payload_sha256) VALUES (?,?,?,?,?,?,?)""",
            (identity, scope.topic, scope.version, scope.preparation, scope.actor, encoded(value), sha(encoded(value))))
        return _append(c, dict(contract=value, events=[]), "READY")
