"""Append-only, one-attempt qualification ledger on the metadata connection."""
from contextlib import closing
from datetime import datetime, timedelta
import hmac
import json
import re
import secrets
import sqlite3
from typing import Any, Callable

from app.db.bootstrap import connect_metadata_index
from app.domain import codex_paper_trial as domain
from app.domain.review_preparation import encoded, sha
from app.repositories import review_preparations

Clock = Callable[[], datetime]


def checkpoint(phase: str) -> None:
    """Transaction rollback seam; production performs no external work here."""


def _exists(c: sqlite3.Connection) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='codex_paper_trials'").fetchone() is not None


def _initialize(c: sqlite3.Connection) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS codex_paper_trials (
        ordinal INTEGER PRIMARY KEY AUTOINCREMENT, trial_id TEXT NOT NULL UNIQUE,
        topic_id TEXT NOT NULL, paper_version_id TEXT NOT NULL, preparation_id TEXT NOT NULL, actor_id TEXT NOT NULL,
        request_id TEXT NOT NULL, binding_sha256 TEXT NOT NULL, issuer_id TEXT NOT NULL, decision_id TEXT NOT NULL,
        created_at TEXT NOT NULL, token_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
        UNIQUE(topic_id,paper_version_id,preparation_id,actor_id,request_id),
        UNIQUE(topic_id,paper_version_id,preparation_id,actor_id,binding_sha256), UNIQUE(issuer_id,decision_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS codex_paper_trial_events (
        trial_id TEXT NOT NULL, sequence INTEGER NOT NULL, payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
        PRIMARY KEY(trial_id,sequence))""")
    for table in ("codex_paper_trials", "codex_paper_trial_events"):
        for operation in ("UPDATE", "DELETE"):
            c.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} BEFORE {operation} ON {table}
                BEGIN SELECT RAISE(ABORT,'codex_trial_immutable'); END""")


def _scope(scope: domain.Scope) -> tuple[str, tuple[str, ...]]:
    return "topic_id=? AND paper_version_id=? AND preparation_id=? AND actor_id=?", (
        scope.topic, scope.version, scope.preparation, scope.actor)


def _preparation(c: sqlite3.Connection, scope: domain.Scope) -> dict[str, Any]:
    value = review_preparations.detail_in(c, scope.topic, scope.version, scope.preparation, list(scope.occurrences))
    if value["actor_id"] != scope.actor:
        raise KeyError("codex_trial_not_found")
    return value


def _request(body: domain.Submit) -> domain.Submit:
    # Revalidate copied/model-constructed instances too.
    return domain.Submit.model_validate(body.model_dump(), strict=True)


def _read(c: sqlite3.Connection, row: sqlite3.Row, scope: domain.Scope, preparation: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(row["payload_json"])
        if (set(value) != {"schema_version", "trial_id", "contract", "decision", "specification"}
                or value["schema_version"] != domain.SCHEMA or sha(encoded(value)) != row["payload_sha256"]):
            raise ValueError()
        contract = domain.validate_contract(scope, value["contract"], preparation)
        body = domain.Submit.model_validate(value["specification"], strict=True)
        decision = domain.read_decision(value["decision"], contract, domain.timestamp(row["created_at"]))
        if (body.model_dump() != value["specification"] or body.expected_binding_sha256 != domain.binding_sha256(contract)
                or body.expected_decision_sha256 != sha(encoded(value["decision"]))
                or value["trial_id"] != domain.identity(scope, body.request_id)
                or any(row[key] != expected for key, expected in (
                    ("trial_id", value["trial_id"]), ("topic_id", scope.topic), ("paper_version_id", scope.version),
                    ("preparation_id", scope.preparation), ("actor_id", scope.actor), ("request_id", body.request_id),
                    ("binding_sha256", body.expected_binding_sha256), ("issuer_id", decision.issuer_id), ("decision_id", decision.decision_id)))):
            raise ValueError()
        events = []
        for saved in c.execute("SELECT * FROM codex_paper_trial_events WHERE trial_id=? ORDER BY sequence", (value["trial_id"],)):
            event = json.loads(saved["payload_json"])
            if (sha(encoded(event)) != saved["payload_sha256"] or event["sequence"] != saved["sequence"]
                    or event["trial_id"] != saved["trial_id"]):
                raise ValueError()
            events.append(event)
        domain.validate_events(value, events, row["token_sha256"], row["created_at"])
        return dict(value, events=events, current=events[-1], assessment=domain.assessment())
    except (KeyError, TypeError, ValueError, AttributeError, IndexError, RecursionError):
        raise ValueError("codex_trial_integrity_invalid") from None


def _row(c: sqlite3.Connection, scope: domain.Scope, identity: str) -> sqlite3.Row:
    where, args = _scope(scope)
    row = c.execute(f"SELECT * FROM codex_paper_trials WHERE {where} AND trial_id=?", (*args, identity)).fetchone() if _exists(c) else None
    if row is None:
        raise KeyError("codex_trial_not_found")
    return row


def _detail(c: sqlite3.Connection, scope: domain.Scope, identity: str) -> dict[str, Any]:
    preparation = _preparation(c, scope)
    return _read(c, _row(c, scope, identity), scope, preparation)


def _replay(c: sqlite3.Connection, scope: domain.Scope, body: domain.Submit, preparation: dict[str, Any]) -> dict[str, Any] | None:
    if not _exists(c):
        return None
    where, args = _scope(scope)
    row = c.execute(f"SELECT * FROM codex_paper_trials WHERE {where} AND request_id=?", (*args, body.request_id)).fetchone()
    if row is None:
        return None
    value = _read(c, row, scope, preparation)
    if encoded(value["specification"]) != encoded(body.model_dump()):
        raise ValueError("codex_trial_request_conflict")
    return value


def replay(scope: domain.Scope, body: domain.Submit) -> dict[str, Any] | None:
    body = _request(body)
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        return _replay(c, scope, body, _preparation(c, scope))


def _insert_event(c: sqlite3.Connection, event: dict[str, Any]) -> None:
    payload = encoded(event)
    c.execute("INSERT INTO codex_paper_trial_events(trial_id,sequence,payload_json,payload_sha256) VALUES (?,?,?,?)",
              (event["trial_id"], event["sequence"], payload, sha(payload)))
    checkpoint("after_event")


def _append(c: sqlite3.Connection, value: dict[str, Any], at: str, **changes: Any) -> None:
    previous = value["current"]
    event = dict(previous, sequence=previous["sequence"] + 1, at=at, command=None,
                 previous_sha256=sha(encoded(previous)))
    event.update(changes)
    event["capture_availability"] = "AVAILABLE" if event["capture"] is not None else "UNAVAILABLE"
    _insert_event(c, event)


def claim(scope: domain.Scope, body: domain.Submit, contract: dict[str, Any], decision: dict[str, Any], *, clock: Clock) -> tuple[dict[str, Any], domain.Claim | None]:
    body = _request(body)
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        preparation = _preparation(c, scope)
        _initialize(c)
        previous = _replay(c, scope, body, preparation)
        if previous is not None:
            return previous, None
        at = domain.timestamp(clock()).isoformat()
        contract = domain.validate_contract(scope, contract, preparation)
        selected = domain.read_decision(decision, contract, domain.timestamp(at))
        if body.expected_binding_sha256 != domain.binding_sha256(contract) or body.expected_decision_sha256 != sha(encoded(decision)):
            raise ValueError("codex_trial_preview_changed")
        identity = domain.identity(scope, body.request_id)
        value = dict(schema_version=domain.SCHEMA, trial_id=identity, contract=contract,
                     decision=selected.model_dump(mode="json"), specification=body.model_dump())
        token = secrets.token_urlsafe(32)
        token_hash = sha(token)
        payload = encoded(value)
        try:
            c.execute("""INSERT INTO codex_paper_trials
                (trial_id,topic_id,paper_version_id,preparation_id,actor_id,request_id,binding_sha256,issuer_id,decision_id,
                 created_at,token_sha256,payload_json,payload_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (identity, scope.topic, scope.version, scope.preparation, scope.actor, body.request_id,
                 body.expected_binding_sha256, selected.issuer_id, selected.decision_id, at, token_hash, payload, sha(payload)))
        except sqlite3.IntegrityError:
            raise ValueError("codex_trial_consumed") from None
        _insert_event(c, dict(schema_version=domain.EVENT_SCHEMA, trial_id=identity, sequence=0, state="CLAIMED", at=at,
            deadline=(domain.timestamp(at) + timedelta(seconds=contract["manifest"]["timeout_seconds"] + 20)).isoformat(),
            reason=None, capture=None, validation="NOT_ASSESSED", capture_availability="UNAVAILABLE",
            outcome_unknown=False, late_capture=False, command=None,
            previous_sha256=sha(encoded(dict(value, token_sha256=token_hash, created_at=at)))))
        checkpoint("after_claim")
        return _detail(c, scope, identity), domain.Claim(scope, identity, token)


def detail(scope: domain.Scope, identity: str) -> dict[str, Any]:
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        return _detail(c, scope, identity)


def listing(scope: domain.Scope, page: int = 1, page_size: int = 10) -> dict[str, Any]:
    if type(page) is not int or type(page_size) is not int or page < 1 or not 1 <= page_size <= 20 or (page - 1) * page_size > 9223372036854775807:
        raise ValueError("codex_trial_request_invalid")
    items = []
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        preparation = _preparation(c, scope)
        if _exists(c):
            where, args = _scope(scope)
            for row in c.execute(f"SELECT * FROM codex_paper_trials WHERE {where} ORDER BY ordinal DESC LIMIT ? OFFSET ?",
                                 (*args, page_size + 1, (page - 1) * page_size)):
                value = _read(c, row, scope, preparation)
                items.append(dict(trial_id=value["trial_id"], binding_sha256=row["binding_sha256"],
                    assessment=domain.assessment(), input_origin=value["contract"]["input_origin"],
                    current={key: value["current"][key] for key in ("sequence", "state", "at", "deadline", "reason", "validation",
                                                                 "capture_availability", "outcome_unknown", "late_capture")}))
    return dict(items=items[:page_size], hasMore=len(items) > page_size, page=page, pageSize=page_size)


def _worker(c: sqlite3.Connection, claimed: domain.Claim) -> dict[str, Any]:
    value = _detail(c, claimed.scope, claimed.identity)
    row = _row(c, claimed.scope, claimed.identity)
    if not hmac.compare_digest(sha(claimed.token), row["token_sha256"]):
        raise ValueError("codex_trial_claim_invalid")
    return value


def start(claimed: domain.Claim, *, clock: Clock) -> bool:
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        value = _worker(c, claimed)
        if value["current"]["state"] != "CLAIMED":
            return False
        now = domain.timestamp(clock())
        current = value["current"]
        reason = ("decision_expired" if now >= domain.timestamp(value["decision"]["expires_at"])
                  else "deadline_expired" if now >= domain.timestamp(current["deadline"]) else None)
        if reason is not None:
            _append(c, value, now.isoformat(), state="EXPIRED", reason=reason)
            _detail(c, claimed.scope, claimed.identity)
            return False
        domain.read_decision(value["decision"], value["contract"], now)
        _append(c, value, now.isoformat(), state="STARTED", outcome_unknown=True,
                deadline=(now + timedelta(seconds=value["contract"]["manifest"]["timeout_seconds"] + 20)).isoformat())
        _detail(c, claimed.scope, claimed.identity)
        return True


def capture(claimed: domain.Claim, captured: dict[str, Any], *, clock: Clock) -> dict[str, Any]:
    captured = domain.capture_value(captured)
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        value = _worker(c, claimed)
        current = value["current"]
        if current["capture"] is not None:
            if encoded(current["capture"]) != encoded(captured):
                raise ValueError("codex_trial_capture_conflict")
            return value
        now = domain.timestamp(clock())
        if current["state"] == "STARTED" and now >= domain.timestamp(current["deadline"]):
            _append(c, value, now.isoformat(), state="UNKNOWN", reason="deadline_expired")
            value = _detail(c, claimed.scope, claimed.identity)
            current = value["current"]
        if current["state"] == "STARTED":
            _append(c, value, now.isoformat(), state="CAPTURED", capture=captured, outcome_unknown=domain.outcome_unknown(captured))
        elif current["state"] in domain.FENCES:
            _append(c, value, now.isoformat(), capture=captured, late_capture=True)
        else:
            raise ValueError("codex_trial_capture_conflict")
        checkpoint("after_capture")
        return _detail(c, claimed.scope, claimed.identity)


def validate(claimed: domain.Claim, status: str, *, clock: Clock) -> dict[str, Any]:
    if status not in {"STRUCTURE_VALID", "STRUCTURE_INVALID"}:
        raise ValueError("codex_trial_validation_invalid")
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        value = _worker(c, claimed)
        current = value["current"]
        if current["state"] in domain.FENCES:
            return value
        if current["state"] in domain.VALIDATED:
            if current["validation"] != status:
                raise ValueError("codex_trial_validation_conflict")
            return value
        if current["state"] != "CAPTURED" or domain.assess_capture(value["contract"], current["capture"]) != status:
            raise ValueError("codex_trial_validation_conflict")
        _append(c, value, domain.timestamp(clock()).isoformat(),
                state="VALIDATED_VALID" if status == "STRUCTURE_VALID" else "VALIDATED_INVALID", validation=status)
        checkpoint("after_validation")
        return _detail(c, claimed.scope, claimed.identity)


def unknown(claimed: domain.Claim, reason: str, *, clock: Clock) -> dict[str, Any]:
    if not isinstance(reason, str) or not re.fullmatch(r"[a-z0-9_]{1,100}", reason):
        raise ValueError("codex_trial_reason_invalid")
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        value = _worker(c, claimed)
        current = value["current"]
        if current["state"] == "UNKNOWN":
            if current["reason"] != reason:
                raise ValueError("codex_trial_unknown_conflict")
            return value
        if current["state"] in domain.FENCES | domain.VALIDATED:
            return value
        _append(c, value, domain.timestamp(clock()).isoformat(), state="UNKNOWN", reason=reason)
        return _detail(c, claimed.scope, claimed.identity)


def control(scope: domain.Scope, identity: str, body: domain.Control, *, clock: Clock) -> dict[str, Any]:
    body = domain.Control.model_validate(body.model_dump(), strict=True)
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        value = _detail(c, scope, identity)
        for event in value["events"]:
            if event["command"] is not None and event["command"]["request_id"] == body.request_id:
                if encoded(event["command"]) != encoded(body.model_dump()):
                    raise ValueError("codex_trial_control_conflict")
                return value
        current = value["current"]
        if current["state"] not in domain.ACTIVE or body.expected_sequence != current["sequence"]:
            raise ValueError("codex_trial_control_conflict")
        now = domain.timestamp(clock())
        if body.action == "recover" and now < domain.timestamp(current["deadline"]):
            raise ValueError("codex_trial_recovery_not_due")
        _append(c, value, now.isoformat(), state={"cancel": "CANCELLED", "revoke": "REVOKED", "recover": "UNKNOWN"}[body.action],
                reason="control_" + body.action, command=body.model_dump())
        return _detail(c, scope, identity)
