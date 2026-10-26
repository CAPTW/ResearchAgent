"""Server-only archive format for the original app trial request and event chain."""
from base64 import b64decode, b64encode
import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain import prepared_review
from app.domain.journal_exchange import preserved_json, sha
from app.domain.review_preparation import encoded
from app.services.local_anthropic_trial import LocalAnthropicTrial, SCHEMA as TRIAL_SCHEMA, trial_binding
from app.services.providers.anthropic_messages import MAX_RESPONSE_BYTES, SCHEMA_COMPILER, SCHEMA_COMPILER_V2, parse_response
from app.services.providers.base import ProviderError

SCHEMA = 'SAVED_ANTHROPIC_TRIAL_ANALYSIS_V1'
# Same bound as the existing trial ledger; base64 raw bytes and the event chain
# are retained without truncating them to the smaller manual-import allowance.
MAX_BUNDLE_BYTES = 12_000_000


class Bundle(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    schema_version: Literal['SAVED_ANTHROPIC_TRIAL_ANALYSIS_V1']
    plan: dict[str, Any]
    binding_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    events: list[dict[str, Any]] = Field(min_length=1, max_length=6)
    request_body_b64: str = Field(max_length=2_000_000)
    response_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    request_body_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    http_status: Literal[200]
    captured_at: str = Field(max_length=50)


def _read(state: dict[str, Any]) -> tuple[dict[str, Any], bytes, bytes, str, dict[str, Any]]:
    plan = state['binding']
    manifest = plan['manifest']
    version = manifest['schema_version']
    if version not in {'ANTHROPIC_PREPARED_REVIEW_WIRE_V1', 'LOCAL_ANTHROPIC_TRIAL_HTTP_WIRE_V1', 'LOCAL_ANTHROPIC_TRIAL_HTTP_WIRE_V2'}:
        raise ValueError('saved_analysis_bundle_invalid')
    read_timeout = None if version == 'ANTHROPIC_PREPARED_REVIEW_WIRE_V1' else manifest['http_timeouts']['read']
    compiler = SCHEMA_COMPILER_V2 if version == 'LOCAL_ANTHROPIC_TRIAL_HTTP_WIRE_V2' else SCHEMA_COMPILER
    body, expected = trial_binding(plan['contract'], plan['grant'], read_timeout, compiler)
    if preserved_json(plan) != preserved_json(expected) or state['binding_sha256'] != sha(encoded(expected)):
        raise ValueError('saved_analysis_bundle_invalid')
    current = LocalAnthropicTrial.validate_snapshot(state, expected, state['binding_sha256'])
    if current['state'] != 'STRUCTURE_VALID':
        raise ValueError('anthropic_trial_analysis_unavailable')
    if (current['attempts'] != 1 or type(current['http_status']) is not int or current['http_status'] != 200
            or current['error_code'] is not None):
        raise ValueError('saved_analysis_bundle_invalid')
    raw = b64decode(current['capture_b64'], validate=True)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError('saved_analysis_bundle_invalid')
    parsed = parse_response(raw, max_output_bytes=prepared_review.MAX_OUTPUT_BYTES)
    record = prepared_review.result(plan['contract'], parsed)
    for field, value in dict(draft=parsed.payload, actual_model=parsed.model, response_id=parsed.response_id, usage=parsed.usage).items():
        if preserved_json(current[field]) != preserved_json(value):
            raise ValueError('saved_analysis_bundle_invalid')
    captures = [event for event in state['events'] if 'capture_b64' in event['changes']]
    if len(captures) != 1 or captures[0]['changes']['state'] != 'CAPTURED':
        raise ValueError('saved_analysis_bundle_invalid')
    return expected, body, raw, captures[0]['at'], record


def from_snapshot(state: dict[str, Any], request_body: bytes) -> dict[str, Any]:
    plan, body, raw, captured_at, _ = _read(state)
    if request_body != body:
        raise ValueError('saved_analysis_bundle_invalid')
    bundle = dict(schema_version=SCHEMA, plan=plan, binding_sha256=state['binding_sha256'], events=state['events'],
        request_body_b64=b64encode(body).decode('ascii'), response_sha256=hashlib.sha256(raw).hexdigest(), request_body_sha256=hashlib.sha256(body).hexdigest(),
        http_status=200, captured_at=captured_at)
    validate_bundle(bundle)
    return bundle


def validate_bundle(value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        if len(preserved_json(value).encode('utf-8')) > MAX_BUNDLE_BYTES:
            raise ValueError()
        bundle = Bundle.model_validate(value)
        _, body, raw, captured_at, record = _read(dict(schema_version=TRIAL_SCHEMA, binding=bundle.plan,
            binding_sha256=bundle.binding_sha256, events=bundle.events))
        if (type(value['http_status']) is not int or bundle.captured_at != captured_at
                or b64decode(bundle.request_body_b64, validate=True) != body
                or bundle.request_body_sha256 != hashlib.sha256(body).hexdigest() or bundle.response_sha256 != hashlib.sha256(raw).hexdigest()):
            raise ValueError()
        return bundle.model_dump(), record
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError, ProviderError):
        raise ValueError('saved_analysis_bundle_invalid') from None
