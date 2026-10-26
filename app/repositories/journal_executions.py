"""Append-only journal attempts with short transactions and fenced result acceptance.

No worker, scheduler, research run, provider selection or live activation is registered.
An interrupted RUNNING attempt has an unknown remote outcome and never auto-retries.
"""
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import secrets
import sqlite3
from typing import Any

from app.db.bootstrap import connect_metadata_index
from app.domain import journal_execution as domain
from app.domain import journal_dispatch as dispatch_domain
from app.domain import journal_authority as authority_domain
from app.domain.journal_exchange import preserved_json
from app.domain.temporal_memory import digest
from app.repositories.synthesis_reports import _candidate


@dataclass(frozen=True)
class Claim:
    topic: str
    job_id: str
    execution_id: str
    attempt: int
    token: str


def checkpoint(phase: str) -> None:
    """Fault injection before the transaction commits."""


def _time(now: datetime | None) -> datetime:
    value = now if now is not None else datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("journal_execution_clock_invalid")
    return value.astimezone(timezone.utc)


def _exists(c: sqlite3.Connection) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='journal_executions'").fetchone() is not None


def _initialize(c: sqlite3.Connection) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS journal_executions (
        execution_id TEXT PRIMARY KEY, topic_id TEXT NOT NULL, job_id TEXT NOT NULL,
        payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL)""")
    c.execute("CREATE INDEX IF NOT EXISTS journal_executions_scope ON journal_executions(topic_id,job_id)")
    c.execute("""CREATE TABLE IF NOT EXISTS journal_execution_events (
        execution_id TEXT NOT NULL, sequence INTEGER NOT NULL, payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL,
        PRIMARY KEY(execution_id,sequence))""")
    for table in ("journal_executions", "journal_execution_events"):
        for operation in ("UPDATE", "DELETE"):
            c.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} BEFORE {operation} ON {table}
                BEGIN SELECT RAISE(ABORT,'journal_execution_immutable'); END""")
    c.execute("""CREATE TRIGGER IF NOT EXISTS journal_executions_binding BEFORE INSERT ON journal_executions
        WHEN NOT EXISTS (SELECT 1 FROM local_synthesis_jobs WHERE job_id=NEW.job_id AND topic_id=NEW.topic_id AND state='CANDIDATE_READY')
        BEGIN SELECT RAISE(ABORT,'journal_execution_binding_invalid'); END""")
    c.execute("""CREATE TRIGGER IF NOT EXISTS journal_execution_events_binding BEFORE INSERT ON journal_execution_events
        WHEN NOT EXISTS (SELECT 1 FROM journal_executions WHERE execution_id=NEW.execution_id)
        OR NEW.sequence != (SELECT count(*)+1 FROM journal_execution_events WHERE execution_id=NEW.execution_id)
        BEGIN SELECT RAISE(ABORT,'journal_execution_binding_invalid'); END""")


def _initial() -> dict[str, Any]:
    return dict(sequence=0, state="READY", attempt=0, deadline=None, token_digest=None,
                outcome_unknown=False, reason=None, result=None, at=None, previous_digest=None)


def _transition(previous: dict[str, Any], event: dict[str, Any], maximum: int) -> None:
    before, after = previous["state"], event["state"]
    allowed = {"READY": {"LEASED", "CANCELLED"}, "LEASED": {"RUNNING", "FAILED", "CANCELLED"},
               "RUNNING": domain.TERMINAL, "FAILED": {"LEASED", "CANCELLED"}, "INTERRUPTED": {"LEASED", "CANCELLED"}}
    attempt = previous["attempt"] + (after == "LEASED")
    if (after not in allowed.get(before, set()) or event["attempt"] != attempt or attempt > maximum
            or event["sequence"] != previous["sequence"] + 1
            or (after != "LEASED" and (event["token_digest"] != previous["token_digest"] or event["deadline"] != previous["deadline"]))
            or (after == "SUCCEEDED") != (event["result"] is not None)):
        raise ValueError("journal_execution_integrity_invalid")
    domain.validate_control(previous, event)


def _read(c: sqlite3.Connection, topic: str, job_id: str, execution_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    job = _candidate(c, topic, job_id)
    row = c.execute("SELECT * FROM journal_executions WHERE topic_id=? AND job_id=? AND execution_id=?",
                    (topic, job_id, execution_id)).fetchone() if _exists(c) else None
    if row is None:
        raise KeyError("journal_execution_not_found")
    try:
        contract = json.loads(row["payload_json"])
        if digest(contract) != row["payload_digest"] or contract["execution_id"] != execution_id:
            raise ValueError()
        domain.validate_contract(contract, job)
        events: list[dict[str, Any]] = []
        control_ids: set[str] = set()
        dispatch_ids: set[str] = set()
        registered_dispatch = False
        previous, prior_digest = _initial(), row["payload_digest"]
        for record in c.execute("SELECT * FROM journal_execution_events WHERE execution_id=? ORDER BY sequence", (execution_id,)):
            event = json.loads(record["payload_json"])
            if (digest(event) != record["payload_digest"] or event["previous_digest"] != prior_digest
                    or event["sequence"] != record["sequence"]):
                raise ValueError()
            _transition(previous, event, contract["specification"]["max_attempts"])
            dispatch_domain.validate_receipt(contract, previous, event)
            authority_domain.validate_revocation_event(previous, event, events)
            if dispatch_ids and event["state"] == "LEASED" and "dispatch" not in event:
                raise ValueError()
            if "dispatch" in event:
                registered = event["dispatch"]["schema_version"] == "JOURNAL_DISPATCH_RECEIPT_V2"
                if registered_dispatch and not registered:
                    raise ValueError()
                registered_dispatch = registered_dispatch or registered
                dispatch_id = event["dispatch"]["request"]["request_id"]
                if dispatch_id in dispatch_ids:
                    raise ValueError()
                dispatch_ids.add(dispatch_id)
            if "control" in event:
                request_id = event["control"]["request_id"]
                if request_id in control_ids:
                    raise ValueError()
                control_ids.add(request_id)
            if event["result"] is not None:
                domain.validate_result(event["result"], contract)
            events.append(event)
            previous, prior_digest = event, record["payload_digest"]
        return contract, events
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError("journal_execution_integrity_invalid") from None


def _append(c: sqlite3.Connection, contract: dict[str, Any], events: list[dict[str, Any]], now: datetime, **changes: Any) -> None:
    previous = events[-1] if events else _initial()
    event = previous.copy()
    event.pop("control", None)  # A human actor belongs only to the event they requested.
    event.pop("dispatch", None)  # Reservation belongs only to this attempt's LEASED event.
    event.pop("adapter_manifest", None)
    event.pop("authority_revocation", None)
    event.update(changes, sequence=previous["sequence"] + 1, at=now.isoformat(),
                 previous_digest=digest(previous) if events else digest(contract))
    _transition(previous, event, contract["specification"]["max_attempts"])
    dispatch_domain.validate_receipt(contract, previous, event)
    authority_domain.validate_revocation_event(previous, event, events)
    c.execute("INSERT INTO journal_execution_events VALUES(?,?,?,?)",
              (contract["execution_id"], event["sequence"], preserved_json(event), digest(event)))
    checkpoint("after_event")
    events.append(event)


def _public(contract: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    # A claim token is never returned by readers; its hash is an internal fence.
    public = [{k: v for k, v in event.items() if k != "token_digest"} for event in events]
    current = public[-1] if public else {k: v for k, v in _initial().items() if k != "token_digest"}
    return dict(contract=contract, current=current, events=public)


def prepare(topic: str, job_id: str, body: domain.Prepare, actor: str, *, now: datetime | None = None) -> tuple[dict[str, Any], bool]:
    stamp = _time(now)
    identity = "jexec_" + digest([topic, job_id, body.request_id])
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        job = _candidate(c, topic, job_id)
        _initialize(c)
        if c.execute("SELECT 1 FROM journal_executions WHERE execution_id=?", (identity,)).fetchone():
            old, events = _read(c, topic, job_id, identity)
            if old["specification"] != body.model_dump() or old["actor_id"] != actor:
                raise ValueError("journal_execution_request_conflict")
            return _public(old, events), False
        contract = domain.prepare(job, body, actor, stamp.isoformat())
        c.execute("INSERT INTO journal_executions VALUES(?,?,?,?,?)", (identity, topic, job_id, preserved_json(contract), digest(contract)))
        checkpoint("after_prepare")
        return _public(contract, []), True


def detail(topic: str, job_id: str, execution_id: str) -> dict[str, Any]:
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        return _public(*_read(c, topic, job_id, execution_id))


def listing(topic: str, job_id: str, page: int = 1, limit: int = 10) -> dict[str, Any]:
    """Read-only discovery; neither lease recovery nor adapter dispatch occurs."""
    if page < 1 or not 1 <= limit <= 100:
        raise ValueError("pagination_invalid")
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        _candidate(c, topic, job_id)
        items, total = [], 0
        if _exists(c):
            total = c.execute("SELECT count(*) FROM journal_executions WHERE topic_id=? AND job_id=?", (topic, job_id)).fetchone()[0]
            rows = c.execute("""SELECT execution_id FROM journal_executions WHERE topic_id=? AND job_id=?
                ORDER BY json_extract(payload_json,'$.created_at') DESC,execution_id DESC LIMIT ? OFFSET ?""",
                (topic, job_id, limit, (page - 1) * limit)).fetchall()
            for row in rows:
                contract, events = _read(c, topic, job_id, row["execution_id"])
                current = events[-1] if events else _initial()
                result = current["result"]
                items.append(dict(execution_id=row["execution_id"], created_at=contract["created_at"],
                                  requested_provider=contract["specification"]["provider_name"], requested_model=contract["specification"]["model"],
                                  state=current["state"], sequence=current["sequence"], attempt=current["attempt"], reason=current["reason"],
                                  outcome_unknown=current["outcome_unknown"], deadline=current["deadline"],
                                  result_digest=digest(result) if result else None,
                                  title=result["proposal"]["title"] if result else None,
                                  provider_reported=result["provider_reported"] if result else None))
        return dict(items=items, total=total, page=page, limit=limit)


def claim(topic: str, job_id: str, execution_id: str, *, expected_attempt: int, acknowledge_unknown: bool = False,
          lease_seconds: int = 120, now: datetime | None = None) -> Claim:
    stamp = _time(now)
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        contract, events = _read(c, topic, job_id, execution_id)
        return _claim(c, contract, events, expected_attempt=expected_attempt, acknowledge_unknown=acknowledge_unknown,
                      lease_seconds=lease_seconds, stamp=stamp)


def _claim(c: sqlite3.Connection, contract: dict[str, Any], events: list[dict[str, Any]], *,
           expected_attempt: int, acknowledge_unknown: bool, lease_seconds: int, stamp: datetime,
           dispatch: dict[str, Any] | None = None, adapter_manifest: dict[str, Any] | None = None) -> Claim:
    from app.repositories import journal_authority
    if type(lease_seconds) is not int or not 5 <= lease_seconds <= 300 or type(expected_attempt) is not int or type(acknowledge_unknown) is not bool:
        raise ValueError("journal_execution_invalid")
    current = events[-1] if events else _initial()
    if (journal_authority.requires_stored(c, contract["execution_id"])
            or any(event.get("dispatch", {}).get("schema_version") == "JOURNAL_DISPATCH_RECEIPT_V2" for event in events)):
        receipt = dispatch_domain.read_receipt(dispatch) if dispatch else None
        if not isinstance(receipt, dispatch_domain.RegisteredReceipt):
            raise ValueError("journal_dispatch_stored_authority_required")
        grant = journal_authority.current_grant(c, contract, receipt.authority, receipt.actor_id, receipt.binding, stamp)
        if grant != receipt.grant:
            raise ValueError("journal_authority_changed")
    if dispatch is None and any("dispatch" in event for event in events):
        raise ValueError("journal_dispatch_authority_required")
    if current["attempt"] != expected_attempt or current["state"] not in {"READY", "FAILED", "INTERRUPTED"}:
        raise ValueError("journal_execution_conflict")
    if current["attempt"] >= contract["specification"]["max_attempts"]:
        raise ValueError("journal_execution_attempts_exhausted")
    if current["outcome_unknown"] and not acknowledge_unknown:
        raise ValueError("journal_execution_unknown_outcome")
    token = secrets.token_hex(32)
    _append(c, contract, events, stamp, state="LEASED", attempt=current["attempt"] + 1,
            token_digest=digest(token), deadline=(stamp + timedelta(seconds=lease_seconds)).isoformat(),
            outcome_unknown=False, reason=None, result=None,
            retry_acknowledged_unknown=current["outcome_unknown"] and acknowledge_unknown,
            **({"dispatch": dispatch} if dispatch is not None else {}),
            **({"adapter_manifest": adapter_manifest} if adapter_manifest is not None else {}))
    return Claim(contract["topic_id"], contract["job_id"], contract["execution_id"], current["attempt"] + 1, token)


def _expired(current: dict[str, Any], now: datetime) -> bool:
    return current["state"] in domain.ACTIVE and now >= datetime.fromisoformat(current["deadline"])


def _recover(c: sqlite3.Connection, contract: dict[str, Any], events: list[dict[str, Any]], now: datetime,
             *, control: dict[str, Any] | None = None) -> bool:
    current = events[-1] if events else _initial()
    if not _expired(current, now):
        return False
    started = current["state"] == "RUNNING"
    _append(c, contract, events, now, state="INTERRUPTED" if started else "FAILED",
            outcome_unknown=started, reason="lease_expired_after_dispatch" if started else "lease_expired_before_dispatch",
            **({"control": control} if control is not None else {}))
    return True


def recover(topic: str, job_id: str, execution_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        contract, events = _read(c, topic, job_id, execution_id)
        _recover(c, contract, events, _time(now))
        return _public(contract, events)


def _fenced(claimed: Claim, state: str, *, result: dict[str, Any] | None = None, reason: str | None = None,
            now: datetime | None = None) -> dict[str, Any] | None:
    stamp = _time(now)
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        contract, events = _read(c, claimed.topic, claimed.job_id, claimed.execution_id)
        current = events[-1] if events else _initial()
        required = "LEASED" if state == "RUNNING" else "RUNNING"
        if (current["state"] != required or current["attempt"] != claimed.attempt or current["token_digest"] != digest(claimed.token)):
            return None
        if _recover(c, contract, events, stamp):
            return None
        if state == "RUNNING" and "dispatch" in current:
            receipt = dispatch_domain.read_receipt(current["dispatch"])
            try:
                if isinstance(receipt, dispatch_domain.RegisteredReceipt):
                    from app.repositories import journal_authority
                    # Same write transaction as RUNNING; revocation shares this lock.
                    grant = journal_authority.current_grant(c, contract, receipt.authority, receipt.actor_id, receipt.binding, stamp)
                    if grant != receipt.grant:
                        raise ValueError("journal_authority_changed")
                dispatch_domain.validate_grant(receipt.grant, contract, receipt.actor_id, receipt.binding, stamp)
            except ValueError as exc:
                if str(exc) not in {"journal_dispatch_authority_expired", "journal_dispatch_proof_rejected"}:
                    raise  # Missing/corrupt authority is not an ordinary expiry.
                _append(c, contract, events, stamp, state="FAILED", outcome_unknown=False,
                        reason="dispatch_authority_expired" if str(exc) == "journal_dispatch_authority_expired" else "dispatch_proof_invalidated")
                return None
        if result is not None:
            domain.validate_result(result, contract)
        _append(c, contract, events, stamp, state=state, result=result, reason=reason, outcome_unknown=state == "INTERRUPTED")
        return _public(contract, events)


def start(claimed: Claim, *, now: datetime | None = None) -> dict[str, Any] | None:
    return _fenced(claimed, "RUNNING", now=now)


def finish(claimed: Claim, result: dict[str, Any], *, now: datetime | None = None) -> bool:
    return _fenced(claimed, "SUCCEEDED", result=result, now=now) is not None


def fail(claimed: Claim, reason: str, *, unknown: bool, now: datetime | None = None) -> bool:
    outcomes = {"provider_unavailable": False, "provider_refused": False, "provider_error": True,
                "adapter_error": True, "output_invalid": False, "provider_incomplete": False,
                "provider_failed": False, "provider_cancelled": False}
    if reason not in outcomes or type(unknown) is not bool or unknown != outcomes[reason]:
        raise ValueError("journal_execution_invalid")
    return _fenced(claimed, "INTERRUPTED" if unknown else "FAILED", reason=reason, now=now) is not None


def abandon(claimed: Claim, *, reason: str, now: datetime | None = None) -> bool:
    """Fence only this worker's active attempt; never cancel the whole request.

    Before dispatch this is a known unstarted failure. After dispatch the remote
    outcome is unknown. A later retry still needs the existing explicit claim
    and unknown-outcome acknowledgment; this operation never claims new work.
    """
    if reason not in {"shutdown", "worker_failed"}:
        raise ValueError("journal_execution_invalid")
    stamp = _time(now)
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        contract, events = _read(c, claimed.topic, claimed.job_id, claimed.execution_id)
        current = events[-1] if events else _initial()
        if (current["state"] not in domain.ACTIVE or current["attempt"] != claimed.attempt
                or current["token_digest"] != digest(claimed.token)):
            return False
        if _recover(c, contract, events, stamp):
            return True
        started = current["state"] == "RUNNING"
        code = "worker_stopped" if reason == "shutdown" else "worker_failed"
        _append(c, contract, events, stamp, state="INTERRUPTED" if started else "FAILED", outcome_unknown=started,
                reason=code + ("_after_dispatch" if started else "_before_dispatch"))
        return True


def cancel(topic: str, job_id: str, execution_id: str, *, expected_attempt: int, now: datetime | None = None) -> dict[str, Any]:
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        contract, events = _read(c, topic, job_id, execution_id)
        current = events[-1] if events else _initial()
        if type(expected_attempt) is not int or current["attempt"] != expected_attempt:
            raise ValueError("journal_execution_conflict")
        if current["state"] == "CANCELLED":
            return _public(contract, events)
        if current["state"] not in domain.ACTIVE | {"READY", "FAILED", "INTERRUPTED"}:
            raise ValueError("journal_execution_conflict")
        # Cancellation fences publication. It cannot promise transport abort.
        _append(c, contract, events, _time(now), state="CANCELLED", outcome_unknown=current["state"] == "RUNNING" or current["outcome_unknown"],
                reason="result_acceptance_cancelled")
        return _public(contract, events)


def control(topic: str, job_id: str, execution_id: str, body: domain.Control, actor: str,
            *, now: datetime | None = None) -> tuple[dict[str, Any], bool]:
    """Human cancellation/expiry check only: no claim, retry, dispatch or transport abort.

    Match the exact observed event, including transitions within one attempt.
    Check retry identity first so a lost response never acts on a newer attempt.
    """
    receipt = domain.ControlReceipt(**body.model_dump(), actor_id=actor).model_dump()
    stamp = _time(now)
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        contract, events = _read(c, topic, job_id, execution_id)
        for event in events:
            old = event.get("control")
            if old is not None and old["request_id"] == body.request_id:
                if old != receipt:
                    raise ValueError("journal_execution_request_conflict")
                return _public(contract, events), False
        current = events[-1] if events else _initial()
        if current["sequence"] != body.expected_sequence:
            raise ValueError("journal_execution_conflict")
        if body.action == "cancel":
            if current["state"] not in domain.ACTIVE | {"READY", "FAILED", "INTERRUPTED"}:
                raise ValueError("journal_execution_conflict")
            _append(c, contract, events, stamp, state="CANCELLED", reason="result_acceptance_cancelled",
                    outcome_unknown=current["state"] == "RUNNING" or current["outcome_unknown"], control=receipt)
        else:
            if current["state"] not in domain.ACTIVE:
                raise ValueError("journal_execution_conflict")
            if not _recover(c, contract, events, stamp, control=receipt):
                raise ValueError("journal_execution_not_expired")
        return _public(contract, events), True
