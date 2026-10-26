"""Strict, offline import of an unchanged structured response and frozen input."""
from base64 import b64decode
from datetime import datetime
import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain import prepared_review
from app.domain.journal_exchange import preserved_json
from app.services.anthropic_output_comparison import validate_plan
from app.services.providers.anthropic_messages import MAX_RESPONSE_BYTES, parse_response
from app.services.providers.base import ProviderError

MAX_BUNDLE_BYTES = 2_000_000


class Bundle(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["SAVED_ANTHROPIC_ANALYSIS_V1"]
    plan: dict[str, Any]
    arm: Literal["structured"]
    response_b64: str = Field(max_length=MAX_BUNDLE_BYTES)
    response_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_body_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    http_status: Literal[200]
    captured_at: str = Field(max_length=50)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z0-9_-]+$")
    expected_previous_id: str | None = Field(max_length=100)
    state: Literal["needs_review", "reviewed", "rejected"]
    note: str = Field(max_length=8000)

    @field_validator("note")
    @classmethod
    def valid_unicode(cls, value: str) -> str:
        value.encode("utf-8")
        return value


def validate_bundle(value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if isinstance(value, dict) and value.get('schema_version') == 'SAVED_ANTHROPIC_TRIAL_ANALYSIS_V1':
        from app.domain.captured_analysis import validate_bundle as validate_capture
        return validate_capture(value)
    try:
        if len(preserved_json(value).encode("utf-8")) > MAX_BUNDLE_BYTES:
            raise ValueError()
        body = Bundle.model_validate(value)
        if type(value["http_status"]) is not int:
            raise ValueError()
        if datetime.fromisoformat(body.captured_at).utcoffset() is None:
            raise ValueError()
        plan = validate_plan(body.plan)
        raw = b64decode(body.response_b64, validate=True)
        if (len(raw) > MAX_RESPONSE_BYTES or hashlib.sha256(raw).hexdigest() != body.response_sha256
                or plan["arms"]["structured"]["body_sha256"] != body.request_body_sha256):
            raise ValueError()
        parsed = parse_response(raw, max_output_bytes=prepared_review.MAX_OUTPUT_BYTES)
        record = prepared_review.result(plan["contract"], parsed)
        return body.model_dump(), record
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError, ProviderError):
        raise ValueError("saved_analysis_bundle_invalid") from None
