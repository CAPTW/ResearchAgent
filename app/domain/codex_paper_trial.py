"""One first-output qualification, never an accepted proof or review receipt."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain import prepared_review
from app.domain.review_execution import Scope
from app.domain.review_preparation import Prepare as PreparationRequest, encoded, sha
from app.domain.review_range_input import build_packet
from app.services.providers.base import ProviderError, ProviderReportedFailure, ProviderResult
from app.services.providers.codex_cli_completion import read_completion
from app.services.providers.codex_journal_plan import MAX_FINAL_BYTES, MAX_STDERR_BYTES, MAX_STREAM_BYTES
from app.services.providers.codex_prepared_review_plan_v2 import build_plan

SCHEMA = "CODEX_PAPER_TRIAL_V1"
INPUT_SCHEMA = "CODEX_PAPER_TRIAL_INPUT_V1"
EVENT_SCHEMA = "CODEX_PAPER_TRIAL_EVENT_V1"
FENCES = {"UNKNOWN", "EXPIRED", "CANCELLED", "REVOKED"}
VALIDATED = {"VALIDATED_VALID", "VALIDATED_INVALID"}
ACTIVE = {"CLAIMED", "STARTED", "CAPTURED"}
STATUSES = {"NOT_ASSESSED", "STRUCTURE_VALID", "STRUCTURE_INVALID"}


class Submit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    request_id: str = Field(min_length=1, max_length=100, pattern=r"\S")
    expected_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_decision_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("request_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        return PreparationRequest.valid_text(value)


class Control(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    request_id: str = Field(min_length=1, max_length=100, pattern=r"\S")
    expected_sequence: int = Field(ge=0, le=100)
    action: Literal["cancel", "revoke", "recover"]

    @field_validator("request_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        return PreparationRequest.valid_text(value)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["CODEX_PAPER_TRIAL_DECISION_V1"] = "CODEX_PAPER_TRIAL_DECISION_V1"
    purpose: Literal["FIRST_OUTPUT_QUALIFICATION"] = "FIRST_OUTPUT_QUALIFICATION"
    issuer_id: str = Field(min_length=1, max_length=500, pattern=r"\S")
    policy_version: str = Field(min_length=1, max_length=200, pattern=r"\S")
    decision_id: str = Field(min_length=1, max_length=100, pattern=r"\S")
    decision_ref: str = Field(min_length=1, max_length=500, pattern=r"\S")
    account_ref: str = Field(min_length=1, max_length=500, pattern=r"\S")
    actor_id: str = Field(min_length=1, max_length=500, pattern=r"\S")
    binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    granted_at: datetime
    expires_at: datetime
    max_attempts: Literal[1] = 1
    reserved_output_tokens: int = Field(ge=256, le=8000)

    @field_validator("max_attempts", mode="before")
    @classmethod
    def one_attempt(cls, value: Any) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("codex_trial_decision_invalid")
        return value

    @field_validator("issuer_id", "policy_version", "decision_id", "decision_ref", "account_ref", "actor_id")
    @classmethod
    def valid_text(cls, value: str) -> str:
        return PreparationRequest.valid_text(value)


@dataclass(frozen=True)
class Claim:
    scope: Scope
    identity: str
    token: str


def timestamp(value: datetime | str) -> datetime:
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("codex_trial_clock_invalid")
    return result.astimezone(timezone.utc)


def identity(scope: Scope, request_id: str) -> str:
    return "ctrial_" + sha(encoded([SCHEMA, scope.topic, scope.version, scope.preparation, scope.actor, request_id]))


def binding_sha256(contract: dict[str, Any]) -> str:
    return sha(encoded(contract))


def build_contract(scope: Scope, preparation: dict[str, Any], prepared: dict[str, Any], manifest: dict[str, Any], origin: str) -> dict[str, Any]:
    try:
        if (origin not in {"PROVIDER_RESPONSE", "FIXTURE_SYNTHETIC"}
                or any(preparation[key] != expected for key, expected in (
                    ("topic_id", scope.topic), ("paper_version_id", scope.version),
                    ("preparation_id", scope.preparation), ("actor_id", scope.actor)))
                or prepared["schema_version"] != prepared_review.SCHEMA or prepared["provider_name"] != "codex_cli"):
            raise ValueError()
        frozen = prepared_review.prepare(build_packet(preparation), provider_name="codex_cli", model=prepared["model"],
            max_output_tokens=prepared["provider_request"]["max_output_tokens"])
        _, _, expected = build_plan(frozen, runtime_identity=manifest["runtime_identity"],
            reasoning_effort=manifest["reasoning_effort"], timeout_seconds=manifest["timeout_seconds"])
        if encoded(frozen) != encoded(prepared) or encoded(expected) != encoded(manifest):
            raise ValueError()
        return dict(schema_version=INPUT_SCHEMA, topic_id=scope.topic, paper_version_id=scope.version,
            preparation_id=scope.preparation, actor_id=scope.actor, preparation_sha256=sha(encoded(preparation)),
            prepared=frozen, manifest=expected, input_origin=origin)
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        raise ValueError("codex_trial_contract_invalid") from None


def validate_contract(scope: Scope, contract: dict[str, Any], preparation: dict[str, Any]) -> dict[str, Any]:
    try:
        expected = build_contract(scope, preparation, contract["prepared"], contract["manifest"], contract["input_origin"])
        if encoded(contract) != encoded(expected):
            raise ValueError()
        return expected
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        raise ValueError("codex_trial_contract_invalid") from None


def read_decision(value: dict[str, Any], contract: dict[str, Any], now: datetime) -> Decision:
    try:
        decision = Decision.model_validate_json(encoded(value))
        if (encoded(decision.model_dump(mode="json")) != encoded(value)
                or decision.actor_id != contract["actor_id"] or decision.binding_sha256 != binding_sha256(contract)
                or decision.reserved_output_tokens != contract["prepared"]["provider_request"]["max_output_tokens"]):
            raise ValueError()
        granted, expires, observed = timestamp(decision.granted_at), timestamp(decision.expires_at), timestamp(now)
        if not timedelta(0) < expires - granted <= timedelta(minutes=5):
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        raise ValueError("codex_trial_decision_invalid") from None
    if not granted <= observed < expires:
        raise ValueError("codex_trial_decision_expired")
    return decision


def capture_value(value: dict[str, Any]) -> dict[str, Any]:
    try:
        if (set(value) != {"exit_code", "stdout", "stderr", "timed_out", "last_message"}
                or type(value["exit_code"]) is not int or not -(2**63) <= value["exit_code"] < 2**63
                or type(value["timed_out"]) is not bool):
            raise ValueError()
        for key, limit in (("stdout", MAX_STREAM_BYTES), ("stderr", MAX_STDERR_BYTES), ("last_message", MAX_FINAL_BYTES)):
            if not isinstance(value[key], str) or len(value[key].encode("utf-8", errors="strict")) > limit:
                raise ValueError()
        return json.loads(encoded(value))
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        raise ValueError("codex_trial_capture_invalid") from None


def assess_capture(contract: dict[str, Any], capture: dict[str, Any]) -> str:
    value = capture_value(capture)
    try:
        if value["timed_out"]:
            return "STRUCTURE_INVALID"
        response_id, usage = read_completion(value["stdout"], value["last_message"], value["exit_code"], error_prefix="codex_trial")
        response = ProviderResult("codex_cli", "not_reported", response_id, json.loads(value["last_message"]),
                                  usage=usage, raw_text=value["last_message"])
        prepared_review.result(contract["prepared"], response)
        return "STRUCTURE_VALID"
    except (ProviderError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return "STRUCTURE_INVALID"


def outcome_unknown(capture: dict[str, Any]) -> bool:
    if capture["timed_out"]:
        return True
    try:
        read_completion(capture["stdout"], capture["last_message"], capture["exit_code"], error_prefix="codex_trial")
    except ProviderReportedFailure:
        return False
    except ProviderError:
        return True
    return False


def assessment() -> dict[str, str]:
    return dict(scientific_support="NOT_ASSESSED", full_document_review="NOT_ESTABLISHED",
                normal_authority="NOT_ESTABLISHED", account_status="NOT_VERIFIED", observed_model_status="NOT_ASSESSED",
                remote_call_count="NOT_ESTABLISHED", stderr_capture="NOT_CAPTURED_BY_RUNTIME")


def validate_events(value: dict[str, Any], events: list[dict[str, Any]], token_hash: str, created_at: str) -> None:
    """Check both the hash chain and the state/capture meaning on every read."""
    if not events or not re.fullmatch(r"[a-f0-9]{64}", token_hash):
        raise ValueError("codex_trial_integrity_invalid")
    keys = {"schema_version", "trial_id", "sequence", "state", "at", "deadline", "reason", "capture", "validation",
            "capture_availability", "outcome_unknown", "late_capture", "command", "previous_sha256"}
    root = sha(encoded(dict(value, token_sha256=token_hash, created_at=created_at)))
    commands = set()
    for index, event in enumerate(events):
        previous = events[index - 1] if index else None
        if (set(event) != keys or event["schema_version"] != EVENT_SCHEMA or event["trial_id"] != value["trial_id"]
                or type(event["sequence"]) is not int or event["sequence"] != index
                or event["previous_sha256"] != (sha(encoded(previous)) if previous else root)
                or type(event["outcome_unknown"]) is not bool or type(event["late_capture"]) is not bool
                or event["validation"] not in STATUSES or event["state"] not in ACTIVE | FENCES | VALIDATED
                or timestamp(event["at"]) < timestamp(previous["at"] if previous else created_at)
                or event["capture_availability"] != ("AVAILABLE" if event["capture"] is not None else "UNAVAILABLE")):
            raise ValueError("codex_trial_integrity_invalid")
        if event["capture"] is not None:
            capture_value(event["capture"])
        if event["reason"] is not None and (not isinstance(event["reason"], str) or not re.fullmatch(r"[a-z0-9_]{1,100}", event["reason"])):
            raise ValueError("codex_trial_integrity_invalid")
        if previous is None:
            if (event["state"] != "CLAIMED" or event["at"] != created_at or event["command"] is not None
                    or event["capture"] is not None or event["validation"] != "NOT_ASSESSED"
                    or event["outcome_unknown"] or event["late_capture"] or event["reason"] is not None):
                raise ValueError("codex_trial_integrity_invalid")
            expected_deadline = timestamp(event["at"]) + timedelta(seconds=value["contract"]["manifest"]["timeout_seconds"] + 20)
            if timestamp(event["deadline"]) != expected_deadline:
                raise ValueError("codex_trial_integrity_invalid")
            continue
        command = event["command"]
        state, prior = event["state"], previous["state"]
        if command is not None:
            body = Control.model_validate(command, strict=True)
            if (body.model_dump() != command or body.request_id in commands or body.expected_sequence != previous["sequence"]
                    or prior not in ACTIVE or event["capture"] != previous["capture"] or event["late_capture"] != previous["late_capture"]
                    or state != {"cancel": "CANCELLED", "revoke": "REVOKED", "recover": "UNKNOWN"}[body.action]
                    or event["reason"] != "control_" + body.action):
                raise ValueError("codex_trial_integrity_invalid")
            if body.action == "recover" and (prior not in ACTIVE or timestamp(event["at"]) < timestamp(previous["deadline"])):
                raise ValueError("codex_trial_integrity_invalid")
            commands.add(body.request_id)
        elif state == "STARTED":
            if prior != "CLAIMED" or event["capture"] is not None or timestamp(event["at"]) >= timestamp(previous["deadline"]):
                raise ValueError("codex_trial_integrity_invalid")
            read_decision(value["decision"], value["contract"], timestamp(event["at"]))
        elif state == "CAPTURED":
            if prior != "STARTED" or event["capture"] is None or timestamp(event["at"]) >= timestamp(previous["deadline"]):
                raise ValueError("codex_trial_integrity_invalid")
        elif state in VALIDATED:
            expected = "STRUCTURE_VALID" if state == "VALIDATED_VALID" else "STRUCTURE_INVALID"
            if (prior != "CAPTURED" or event["capture"] != previous["capture"] or event["validation"] != expected
                    or event["reason"] is not None or event["late_capture"]
                    or assess_capture(value["contract"], event["capture"]) != expected):
                raise ValueError("codex_trial_integrity_invalid")
        elif prior in FENCES and state == prior:
            if (previous["capture"] is not None or event["capture"] is None or not event["late_capture"]
                    or event["reason"] != previous["reason"]):
                raise ValueError("codex_trial_integrity_invalid")
        elif state in {"UNKNOWN", "EXPIRED"}:
            if (prior not in ACTIVE or event["capture"] != previous["capture"] or event["late_capture"] != previous["late_capture"]
                    or event["reason"] is None):
                raise ValueError("codex_trial_integrity_invalid")
            if state == "EXPIRED" and event["reason"] not in {"deadline_expired", "decision_expired"}:
                raise ValueError("codex_trial_integrity_invalid")
            if state == "EXPIRED" and event["reason"] == "deadline_expired" and timestamp(event["at"]) < timestamp(previous["deadline"]):
                raise ValueError("codex_trial_integrity_invalid")
            if state == "EXPIRED" and event["reason"] == "decision_expired":
                if prior != "CLAIMED" or timestamp(event["at"]) < timestamp(value["decision"]["expires_at"]):
                    raise ValueError("codex_trial_integrity_invalid")
        else:
            raise ValueError("codex_trial_integrity_invalid")
        expected_deadline = (timestamp(event["at"]) + timedelta(seconds=value["contract"]["manifest"]["timeout_seconds"] + 20)
                             if state == "STARTED" else timestamp(previous["deadline"]))
        if timestamp(event["deadline"]) != expected_deadline:
            raise ValueError("codex_trial_integrity_invalid")
        if state not in VALIDATED and event["validation"] != "NOT_ASSESSED":
            raise ValueError("codex_trial_integrity_invalid")
        if state in {"STARTED", "CAPTURED"} and (event["reason"] is not None or event["late_capture"]):
            raise ValueError("codex_trial_integrity_invalid")
        expected_unknown = (True if state == "STARTED" else outcome_unknown(event["capture"]) if state == "CAPTURED"
                            else previous["outcome_unknown"])
        if event["outcome_unknown"] != expected_unknown:
            raise ValueError("codex_trial_integrity_invalid")
