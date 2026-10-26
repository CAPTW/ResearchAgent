"""Append-only local-owner decisions; reads never issue or renew authority."""
from contextlib import closing
from datetime import timedelta
import json
from typing import Any, Callable

from app.db.bootstrap import connect_metadata_index
from app.domain import review_authority as authority, review_execution as domain
from app.domain.review_preparation import encoded, sha
from app.repositories import review_executions as executions
from app.repositories.files import iso_now_utc

ISSUER = "xrml-local-owner-v1"
POLICY = "LOCAL_OWNER_ANTHROPIC_ONE_ATTEMPT_V1"
TABLE = "paper_local_anthropic_decisions"
SCHEMA = "PAPER_LOCAL_ANTHROPIC_DECISION_V1"


def _row(c, identity):
    if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is None:
        return None
    return c.execute(f"SELECT * FROM {TABLE} WHERE execution_id=?", (identity,)).fetchone()


def _decode(row, contract: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(row["payload_json"])
        decision = value["decision"]
        record = authority.read_record(value["record"], contract)
        if (not isinstance(record.grant, authority.OwnerFirstOutputGrant)
                or sha(encoded(value)) != row["payload_sha256"] or decision["schema_version"] != SCHEMA
                or decision["execution_id"] != contract["execution_id"] or decision["actor_id"] != contract["actor_id"]
                or decision["contract_sha256"] != sha(encoded(contract))
                or decision["command"]["expected_contract_sha256"] != decision["contract_sha256"]
                or decision["command"]["expected_binding_sha256"] != decision["binding_sha256"]
                or record.grant.decision_sha256 != sha(encoded(decision))
                or record.issuer_id != ISSUER or record.grant.policy_version != POLICY
                or row["execution_id"] != decision["execution_id"] or row["actor_id"] != decision["actor_id"]
                or row["request_id"] != decision["command"]["request_id"]):
            raise ValueError()
        return value
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError("paper_local_owner_decision_invalid") from None


def get(contract: dict[str, Any]) -> dict[str, Any] | None:
    with closing(connect_metadata_index(read_only=True)) as c:
        row = _row(c, contract["execution_id"])
        return _decode(row, contract) if row is not None else None


def create(scope: domain.Scope, identity: str, command: dict[str, Any], validate: Callable[[], None]) -> dict[str, Any]:
    with executions._write(scope) as (c, preparation):
        saved = executions._detail(c, scope, identity, preparation)
        contract = saved["contract"]
        row = _row(c, identity)
        if row is not None:
            value = _decode(row, contract)
            if encoded(value["decision"]["command"]) != encoded(command):
                raise ValueError("paper_local_owner_decision_conflict")
            return value
        if (saved["current"]["state"] != "READY" or saved["current"]["sequence"] != command["expected_sequence"]
                or sha(encoded(contract)) != command["expected_contract_sha256"]):
            raise ValueError("paper_local_owner_decision_conflict")
        validate()  # Metadata only: no decryption or network in this transaction.
        now = domain.timestamp(iso_now_utc())
        decision = dict(schema_version=SCHEMA, execution_id=identity, actor_id=scope.actor,
            contract_sha256=sha(encoded(contract)), binding_sha256=command["expected_binding_sha256"],
            command=command, at=now.isoformat(), authorization_kind="OWNER_FIRST_OUTPUT", max_attempts=1,
            scientific_support="NOT_ASSESSED", execution_started=False)
        digest = sha(encoded(decision))
        record = authority.OwnerFirstOutputRecord(
            grant=authority.OwnerFirstOutputGrant(authorization_id="local-owner-" + digest[:40], actor_id=scope.actor,
                contract_sha256=sha(encoded(contract)), execution_id=identity, input_origin="PROVIDER_RESPONSE",
                policy_version=POLICY, granted_at=now, expires_at=now + timedelta(minutes=5), max_attempts=1,
                reserved_output_tokens=contract["prepared"]["provider_request"]["max_output_tokens"],
                decision_sha256=digest, request_sha256=contract["prepared"]["request_sha256"],
                manifest_sha256=sha(encoded(contract["transport"])), credential_binding=contract["transport"]["credential_binding"]),
            issuer_id=ISSUER, decision_ref="local-owner:" + digest, registered_at=now,
            expected_sequence=command["expected_sequence"]).model_dump(mode="json")
        authority.read_record(record, contract)
        value = dict(decision=decision, record=record)
        c.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
            execution_id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, request_id TEXT NOT NULL,
            payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL, UNIQUE(actor_id,request_id))""")
        for operation in ("UPDATE", "DELETE"):
            c.execute(f"""CREATE TRIGGER IF NOT EXISTS {TABLE}_no_{operation.lower()} BEFORE {operation} ON {TABLE}
                BEGIN SELECT RAISE(ABORT,'paper_local_owner_decision_immutable'); END""")
        if c.execute(f"SELECT 1 FROM {TABLE} WHERE actor_id=? AND request_id=?", (scope.actor, command["request_id"])).fetchone():
            raise ValueError("paper_local_owner_decision_conflict")
        c.execute(f"INSERT INTO {TABLE} VALUES (?,?,?,?,?)", (identity, scope.actor, command["request_id"], encoded(value), sha(encoded(value))))
        executions.checkpoint("after_local_owner_decision")
        return value


def receipt(value: dict[str, Any]) -> dict[str, Any]:
    decision = value["decision"]
    return dict(schema_version="PAPER_LOCAL_ANTHROPIC_DECISION_RECEIPT_V1", execution_id=decision["execution_id"],
        actor_id=decision["actor_id"], contract_sha256=decision["contract_sha256"], binding_sha256=decision["binding_sha256"],
        command=decision["command"], decision_sha256=sha(encoded(decision)), record_sha256=sha(encoded(value["record"])),
        created_at=decision["at"], execution_allowed=False)
