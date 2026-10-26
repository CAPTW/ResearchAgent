"""One-attempt paper execution. Offline authority cannot select a live target."""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain import prepared_review
from app.domain.review_offline import ADAPTER, MODEL, MODE, VERSION
from app.domain.review_preparation import Prepare as PreparationRequest, encoded, sha
from app.domain.review_range_input import build_packet

SCHEMA = "PAPER_REVIEW_EXECUTION_V1"
EVENT_SCHEMA = "PAPER_REVIEW_EXECUTION_EVENT_V1"
ACTIVE = {"APPROVED", "LEASED", "RUNNING"}
TRANSITIONS = {
    "READY": {"APPROVED", "CANCELLED"},
    "APPROVED": {"LEASED", "CANCELLED", "REVOKED", "FAILED"},
    "LEASED": {"RUNNING", "CANCELLED", "REVOKED", "FAILED"},
    "RUNNING": {"RESULT_RECEIVED", "CANCELLED", "REVOKED", "INTERRUPTED"},
    "RESULT_RECEIVED": {"RESULT_SAVED", "CANCELLED", "REVOKED"},
}


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=100, pattern=r"\S")

    @field_validator("request_id")
    @classmethod
    def valid_text(cls, value: str) -> str:
        return PreparationRequest.valid_text(value)


class Prepare(Request):
    expected_preparation_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    acknowledge_unknown: bool = False


class Dispatch(Request):
    expected_sequence: int = Field(ge=0)


class Control(Dispatch):
    action: Literal["approve", "cancel", "revoke", "recover", "save_result"]


@dataclass(frozen=True)
class Scope:
    topic: str
    version: str
    preparation: str
    actor: str
    occurrences: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Claim:
    scope: Scope
    identity: str
    token: str
    schema: str = SCHEMA


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("review_execution_integrity_invalid")
    return result.astimezone(timezone.utc)


def identity(scope: Scope, request: str) -> str:
    return "rexec_" + sha(encoded([SCHEMA, scope.topic, scope.version, scope.actor, request]))


def contract(scope: Scope, body: Prepare, preparation: dict[str, Any], at: str,
             *, prepared_schema: str = prepared_review.SCHEMA) -> dict[str, Any]:
    if body.expected_preparation_digest != sha(encoded(preparation)):
        raise ValueError("review_execution_preparation_changed")
    return dict(schema_version=SCHEMA, execution_id=identity(scope, body.request_id),
                topic_id=scope.topic, paper_version_id=scope.version, preparation_id=scope.preparation,
                actor_id=scope.actor, created_at=at, mode=MODE, specification=body.model_dump(),
                prepared=prepared_review.prepare(build_packet(preparation), provider_name=ADAPTER, model=MODEL, schema_version=prepared_schema))


def marker(identity: str) -> dict[str, str]:
    return dict(mode=MODE, adapter_id=ADAPTER, adapter_version=VERSION,
                scientific_assessment="NOT_ASSESSED", execution_id=identity)


def validate_record(value: dict[str, Any], record: dict[str, Any]) -> None:
    # The original standalone record validator also accepts a minimal V1
    # envelope. Persisted contracts still require a version in repository._read.
    if value.get("schema_version", SCHEMA) != SCHEMA:
        from app.domain.review_transport_execution import SCHEMA as TRANSPORT_SCHEMA, validate_record as validate_transport_record
        if value["schema_version"] != TRANSPORT_SCHEMA:
            raise ValueError("review_execution_integrity_invalid")
        validate_transport_record(value, record)
        return
    # Unlike a receipt envelope, a received worker record must have no fields
    # that receipt normalization would drop during publication.
    if set(record) != {"topic_id", "run_id", "paper_id", "source_text_sha256", "review_level", "status", "draft", "validation"}:
        raise ValueError("review_execution_integrity_invalid")
    prepared_review.validate_record(record, value["prepared"]["packet"])
    proof = record["validation"]["prepared_execution"]
    if (encoded(proof["contract"]) != encoded(value["prepared"])
            or proof["response"]["model"] != MODEL
            or record["validation"].get("execution") != marker(value["execution_id"])):
        raise ValueError("review_execution_integrity_invalid")


def validate_events(value: dict[str, Any], events: list[dict[str, Any]]) -> None:
    """Recheck meaning as well as hashes, including result provenance and fences."""
    if not events:
        raise ValueError("review_execution_integrity_invalid")
    keys = {"schema_version", "execution_id", "sequence", "state", "at", "deadline", "outcome_unknown",
            "reason", "receipt_id", "claim_sha256", "command", "record", "previous_sha256"}
    from app.domain import review_authority, review_transport_execution
    transport = value["schema_version"] == review_transport_execution.SCHEMA
    if value["schema_version"] not in {SCHEMA, review_transport_execution.SCHEMA}:
        raise ValueError("review_execution_integrity_invalid")
    event_schema = review_transport_execution.EVENT_SCHEMA if transport else EVENT_SCHEMA
    if transport:
        keys.add("authority")
    seen = set()
    for index, event in enumerate(events):
        prior = events[index - 1] if index else None
        state = event["state"]
        if (set(event) != keys or event["schema_version"] != event_schema
                or event["execution_id"] != value["execution_id"] or type(event["sequence"]) is not int
                or event["sequence"] != index or type(event["outcome_unknown"]) is not bool
                or event["previous_sha256"] != (sha(encoded(prior)) if prior else sha(encoded(value)))
                or timestamp(event["at"]) < timestamp(prior["at"] if prior else value["created_at"])):
            raise ValueError("review_execution_integrity_invalid")
        if prior is None:
            if (state != "READY" or event["at"] != value["created_at"] or event["command"] is not None
                    or event["reason"] is not None):
                raise ValueError("review_execution_integrity_invalid")
        elif state not in TRANSITIONS.get(prior["state"], set()) and not (transport and prior["state"] == "RUNNING" and state == "FAILED"):
            raise ValueError("review_execution_integrity_invalid")
        if transport:
            if state == "APPROVED":
                ref = review_authority.Reference.model_validate(event["authority"], strict=True)
                if encoded(ref.model_dump()) != encoded(event["authority"]):
                    raise ValueError("review_execution_integrity_invalid")
            elif event["authority"] is not None:
                raise ValueError("review_execution_integrity_invalid")
        command = event["command"]
        if command is not None:
            if set(command) != {"kind", "body"} or prior is None:
                raise ValueError("review_execution_integrity_invalid")
            body = (Dispatch if command["kind"] == "dispatch" else Control).model_validate(command["body"], strict=True)
            action = body.action if isinstance(body, Control) else "dispatch"
            expected = {"dispatch": {"LEASED"}, "approve": {"APPROVED"}, "cancel": {"CANCELLED"},
                        "revoke": {"REVOKED"}, "recover": {"FAILED", "INTERRUPTED"}, "save_result": {"RESULT_SAVED"}}
            if (command["kind"] not in {"dispatch", "control"} or state not in expected[action]
                    or body.request_id in seen or body.expected_sequence != index - 1):
                raise ValueError("review_execution_integrity_invalid")
            seen.add(body.request_id)
        elif state in {"APPROVED", "LEASED", "CANCELLED", "REVOKED"}:
            raise ValueError("review_execution_integrity_invalid")
        known_failure = transport and state == "FAILED" and event["reason"] in review_transport_execution.KNOWN_FAILURE_REASONS
        unknown = prior is not None and prior["state"] == "RUNNING" and state != "RESULT_RECEIVED" and not known_failure
        if event["outcome_unknown"] != unknown:
            raise ValueError("review_execution_integrity_invalid")
        deadline, token = event["deadline"], event["claim_sha256"]
        if state in ACTIVE:
            if deadline is None or timestamp(deadline) <= timestamp(event["at"]):
                raise ValueError("review_execution_integrity_invalid")
        elif deadline is not None:
            raise ValueError("review_execution_integrity_invalid")
        if state in {"LEASED", "RUNNING"}:
            if not isinstance(token, str) or len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
                raise ValueError("review_execution_integrity_invalid")
        elif token is not None:
            raise ValueError("review_execution_integrity_invalid")
        if prior and state in {"LEASED", "RUNNING", "RESULT_RECEIVED"}:
            if timestamp(event["at"]) >= timestamp(prior["deadline"]):
                raise ValueError("review_execution_integrity_invalid")
            if state == "LEASED" and timestamp(deadline) > timestamp(prior["deadline"]):
                raise ValueError("review_execution_integrity_invalid")
            if state == "RUNNING" and (token != prior["claim_sha256"] or deadline != prior["deadline"]):
                raise ValueError("review_execution_integrity_invalid")
        if (state == "RESULT_SAVED") != (isinstance(event["receipt_id"], str) and bool(event["receipt_id"])):
            raise ValueError("review_execution_integrity_invalid")
        if state != "RESULT_SAVED" and event["receipt_id"] is not None:
            raise ValueError("review_execution_integrity_invalid")
        if state == "RESULT_RECEIVED":
            validate_record(value, event["record"])
        elif event["record"] is not None:
            raise ValueError("review_execution_integrity_invalid")
        if state in {"FAILED", "INTERRUPTED"}:
            reasons = {"deadline_expired", "worker_stopped", "worker_start_failed", "adapter_failed", "result_invalid"}
            if transport:
                reasons |= {"authority_invalid"} | review_transport_execution.KNOWN_FAILURE_REASONS
                if prior and prior["state"] == "RUNNING" and state == "FAILED" and not known_failure:
                    raise ValueError("review_execution_integrity_invalid")
            if event["reason"] not in reasons:
                raise ValueError("review_execution_integrity_invalid")
            if event["reason"] == "deadline_expired" and (prior is None or timestamp(event["at"]) < timestamp(prior["deadline"])):
                raise ValueError("review_execution_integrity_invalid")
        elif event["reason"] is not None:
            raise ValueError("review_execution_integrity_invalid")
