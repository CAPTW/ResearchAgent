"""Stored server-side authority records, not a permission-issuing API.

The caller must have separately obtained an exact approval/proof. Registering a
record cannot establish that a human actually approved it. No approval is inferred
from credentials, candidate acceptance, client fields or a synthetic proof.
"""
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.journal_dispatch import AdapterBinding, Grant, RegisteredReceipt, read_receipt, validate_grant
from app.domain.journal_exchange import preserved_json


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["JOURNAL_AUTHORITY_RECORD_V1"] = "JOURNAL_AUTHORITY_RECORD_V1"
    topic_id: str
    job_id: str
    execution_id: str
    grant: Grant
    binding: AdapterBinding
    registered_by: str = Field(min_length=1, max_length=500, pattern=r"\S")
    decision_ref: str = Field(min_length=1, max_length=200, pattern=r"\S")
    expected_sequence: int = Field(ge=0, le=100)
    registered_at: datetime


class Revocation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["JOURNAL_AUTHORITY_REVOCATION_V1"] = "JOURNAL_AUTHORITY_REVOCATION_V1"
    authorization_id: str = Field(min_length=1, max_length=100, pattern=r"\S")
    record_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_id: str = Field(min_length=1, max_length=100, pattern=r"\S")
    actor_id: str = Field(min_length=1, max_length=500, pattern=r"\S")
    reason: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    at: datetime


def read_record(value: dict[str, Any], contract: dict[str, Any]) -> Record:
    record = Record.model_validate_json(preserved_json(value))
    if (record.model_dump(mode="json") != value
            or any(value[key] != contract[key] for key in ("topic_id", "job_id", "execution_id"))):
        raise ValueError("journal_authority_integrity_invalid")
    # Historical validation uses the recorded time, never today's grant status.
    validate_grant(record.grant, contract, record.grant.actor_id, record.binding, record.registered_at)
    return record


def read_revocation(value: dict[str, Any]) -> Revocation:
    result = Revocation.model_validate_json(preserved_json(value))
    if result.model_dump(mode="json") != value or result.at.utcoffset() is None:
        raise ValueError("journal_authority_integrity_invalid")
    return result


def validate_revocation_event(previous: dict[str, Any], event: dict[str, Any], events: list[dict[str, Any]]) -> None:
    if "authority_revocation" not in event:
        return
    revoked = read_revocation(event["authority_revocation"])
    dispatched = previous["state"] == "RUNNING"
    lease = next((row for row in reversed(events) if row["state"] == "LEASED" and row["attempt"] == event["attempt"]), None)
    receipt = read_receipt(lease["dispatch"]) if lease and "dispatch" in lease else None
    if (not isinstance(receipt, RegisteredReceipt)
            or receipt.authority.authorization_id != revoked.authorization_id or receipt.authority.record_digest != revoked.record_digest
            or previous["state"] not in {"LEASED", "RUNNING"}
            or event["state"] != ("INTERRUPTED" if dispatched else "FAILED")
            or event["outcome_unknown"] is not dispatched
            or event["reason"] != "dispatch_authority_revoked_" + ("after_dispatch" if dispatched else "before_dispatch")
            or datetime.fromisoformat(event["at"]) != revoked.at):
        raise ValueError("journal_authority_integrity_invalid")
