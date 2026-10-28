"""Unregistered Ollama chat contract, restricted to explicit synthetic transport.

Loopback addressing and response metadata do not prove local inference. This
profile cannot open a default HTTP transport, register authority, or persist a
receipt. Its unsigned archive checks internal consistency, not authenticity.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass, field
import json
import math
import re
from typing import Any

import httpx

from app.domain import prepared_review, review_token_budget
from app.domain.journal_exchange import preserved_json, sha
from app.services.providers.base import ProviderError, ProviderOutputError, ProviderReportedFailure, ProviderRequest, ProviderResult

ADAPTER_ID = "xrml-ollama-prepared-review"
MANIFEST_SCHEMA = "OLLAMA_PREPARED_REVIEW_WIRE_V1"
TRANSPORT_SCHEMA = "PREPARED_REVIEW_OLLAMA_MOCK_TRANSPORT_V1"
ENDPOINT = "http://127.0.0.1:11434/api/chat"
EXECUTION_SURFACE = "MOCK_TRANSPORT"
LOCALITY = "LOCALITY_NOT_VERIFIED"
MAX_REQUEST_BYTES = 4_000_000
MAX_RESPONSE_BYTES = 4_000_000
# A local request bound, not a model's advertised or measured context capacity.
MAX_CONTEXT_TOKENS = 1_000_000
_COUNTS = ("prompt_eval_count", "eval_count", "prompt_eval_cached_count")
_DURATIONS = ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration")
_MODEL = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,199}")


def _model(value: Any) -> bool:
    return (isinstance(value, str) and _MODEL.fullmatch(value) is not None
            and not value.lower().startswith(("sk-", "xai-", "ya29."))
            and "://" not in value and "cloud" not in re.split(r"[:/._-]", value.lower()))


def build_plan(contract: dict[str, Any], *, context_tokens: int,
               timeout_seconds: float) -> tuple[str, bytes, dict[str, Any]]:
    """Freeze the original schema and request; every runtime effect is unverified."""
    try:
        frozen = prepared_review.validate_contract(contract)
        if (frozen["schema_version"] not in {prepared_review.SCHEMA, prepared_review.DOCUMENT_SCHEMA}
                or frozen["provider_name"] != "ollama" or not _model(frozen["model"])
                or type(context_tokens) is not int or not 1 <= context_tokens <= MAX_CONTEXT_TOKENS
                or type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 120):
            raise ValueError()
        request = ProviderRequest(**frozen["provider_request"])
        estimated = review_token_budget.estimate_input_tokens(frozen["provider_request"])
        if estimated + request.max_output_tokens > context_tokens:
            raise ValueError()
        budget = None
        if frozen["schema_version"] == prepared_review.DOCUMENT_SCHEMA:
            budget = review_token_budget.validate_budget(frozen["packet"]["source_preparation"]["analysis_unit"]["budget"])
            if (budget["provider_name"] != "ollama" or budget["model"] != frozen["model"]
                    or budget["context_tokens"] != context_tokens
                    or budget["max_output_tokens"] != request.max_output_tokens
                    or estimated > budget["input_tokens"]):
                raise ValueError()
        payload = dict(model=frozen["model"], messages=[
            dict(role="system", content=request.instructions), dict(role="user", content=request.prompt)],
            format=deepcopy(request.schema), stream=False, think=False, keep_alive=0, truncate=False, shift=False,
            options=dict(num_ctx=context_tokens, num_predict=request.max_output_tokens, temperature=request.temperature))
        body = preserved_json(payload).encode("utf-8", errors="strict")
        if len(body) > MAX_REQUEST_BYTES:
            raise ValueError()
        manifest = dict(schema_version=MANIFEST_SCHEMA, adapter_id=ADAPTER_ID, adapter_version="1",
            provider_name="ollama", execution_surface=EXECUTION_SURFACE, input_origin="FIXTURE_SYNTHETIC",
            locality=LOCALITY, activation="UNREGISTERED_ADAPTER_CONTRACT", endpoint=ENDPOINT, method="POST",
            model=frozen["model"], contract_sha256=sha(preserved_json(frozen)), request_sha256=frozen["request_sha256"],
            body_sha256=sha(body.decode("utf-8")), body_bytes=len(body), context_tokens=context_tokens,
            max_output_tokens=request.max_output_tokens, estimated_input_tokens=estimated,
            input_estimator=review_token_budget.ESTIMATOR, document_budget=budget,
            budget_scope="DOCUMENT_UNIT_FROZEN" if budget else "EXCERPT_EXPLICIT_CONTEXT_NO_FROZEN_INPUT_BUDGET",
            input_estimate_semantics="LOCAL_CONSERVATIVE_ESTIMATE_NOT_TOKENIZER_OR_CAPACITY_PROOF",
            timeout_seconds=float(timeout_seconds), timeout_semantics="PER_OPERATION_INACTIVITY",
            max_request_bytes=MAX_REQUEST_BYTES, max_response_bytes=MAX_RESPONSE_BYTES,
            max_review_output_bytes=prepared_review.MAX_OUTPUT_BYTES,
            schema_sha256=sha(preserved_json(request.schema)), schema_constraints="LOCAL_ORIGINAL_VALIDATION",
            stream=False, think=False, keep_alive=0, truncate=False, shift=False, tools=[], authentication="NONE",
            automatic_retry=False, redirects=False, environment_proxy=False, external_fallback=False,
            request_metadata="LOCAL_BINDING_ONLY_NOT_TRANSMITTED", request_effects="NOT_LIVE_VERIFIED",
            output_token_limit_enforced=False, context_limit_enforced=False, temperature_enforced=False,
            model_identity="REQUESTED_NOT_INDEPENDENTLY_VERIFIED", live_execution="NOT_VERIFIED",
            raw_output_encoding="MESSAGE_CONTENT_JSON_UTF8_V1", raw_envelope_retained=True,
            duration_semantics="PROVIDER_REPORTED_NANOSECONDS_NOT_LOCAL_WALL_TIME",
            usage_semantics="PROVIDER_REPORTED_COUNTS_NOT_INDEPENDENTLY_VERIFIED",
            cached_input_semantics="REPORTED_SEPARATELY_NOT_ADDED_TO_INPUT", total_tokens="NOT_REPORTED_NOT_DERIVED")
        return preserved_json(frozen["provider_request"]), body, manifest
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        raise ValueError("paper_ollama_configuration_invalid") from None


def _object(raw: str) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    def finite(value: str) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("nonfinite_json")
        return number

    value = json.loads(raw, object_pairs_hook=unique, parse_float=finite, parse_constant=finite)
    if not isinstance(value, dict):
        raise ValueError("object_required")
    json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8", errors="strict")
    return value


@dataclass(frozen=True)
class CapturedResponse:
    """Keep wire envelope separate from ProviderResult.raw_text (content only)."""
    envelope_text: str
    result: ProviderResult
    reported_metrics: dict[str, int]


def parse_response(raw: bytes) -> CapturedResponse:
    """Parse bounded nonstreaming completion without echoing response values."""
    try:
        if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError()
        text = raw.decode("utf-8", errors="strict")
        envelope = _object(text)
        message = envelope.get("message")
        if (not _model(envelope.get("model")) or "remote_host" in envelope or "remote_model" in envelope
                or "error" in envelope or envelope.get("done") is not True or not isinstance(message, dict)
                or message.get("role") != "assistant" or not isinstance(message.get("content"), str)
                or "remote_host" in message or "remote_model" in message
                or message.get("tool_calls") not in (None, [])):
            raise ValueError()
        metrics: dict[str, int] = {}
        for key in _COUNTS + _DURATIONS:
            if key in envelope:
                value = envelope[key]
                if type(value) is not int or not 0 <= value <= (10**9 if key in _COUNTS else 2**63 - 1):
                    raise ValueError()
                metrics[key] = value
        reason = envelope.get("done_reason")
        if reason not in {"stop", "length"}:
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        raise ProviderError("paper_ollama_response_outcome_unknown") from None
    if reason == "length":
        raise ProviderReportedFailure("incomplete")
    content = message["content"]
    try:
        if len(content.encode("utf-8", errors="strict")) > prepared_review.MAX_OUTPUT_BYTES:
            raise ValueError()
        payload = _object(content)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        raise ProviderOutputError("paper_ollama_output_invalid") from None
    usage = {target: metrics[source] for source, target in (
        ("prompt_eval_count", "input_tokens"), ("eval_count", "output_tokens")) if source in metrics}
    return CapturedResponse(text, ProviderResult(provider_name="ollama", model=envelope["model"], response_id=None,
        payload=payload, usage=usage, raw_text=content), metrics)


def _record(contract: dict[str, Any], captured: CapturedResponse, manifest: dict[str, Any]) -> dict[str, Any]:
    _, _, expected = build_plan(contract, context_tokens=manifest["context_tokens"], timeout_seconds=manifest["timeout_seconds"])
    if preserved_json(manifest) != preserved_json(expected):
        raise ValueError("paper_ollama_transport_invalid")
    record = prepared_review.result(contract, captured.result)
    raw = record["validation"]["prepared_execution"]["response"]["raw_text"]
    record["validation"]["execution"] = dict(schema_version=TRANSPORT_SCHEMA, adapter_id=ADAPTER_ID, adapter_version="1",
        manifest=expected, manifest_sha256=sha(preserved_json(expected)), execution_surface=EXECUTION_SURFACE,
        input_origin="FIXTURE_SYNTHETIC", locality=LOCALITY, response_sha256=sha(raw),
        http_response_text=captured.envelope_text, http_response_sha256=sha(captured.envelope_text),
        reported_metrics=captured.reported_metrics, authority="NOT_ESTABLISHED", live_execution="NOT_VERIFIED")
    return record


def transport_record(contract: dict[str, Any], raw: bytes, manifest: dict[str, Any]) -> dict[str, Any]:
    """Build an in-memory synthetic record; no persistence or authority is added."""
    return _record(contract, parse_response(raw), manifest)


def validate_transport(record: dict[str, Any], packet: dict[str, Any]) -> None:
    """Rebuild envelope, prepared result and marker; hashes are not signatures."""
    try:
        prepared_review.validate_record(record, packet)
        if "input_origin" in record and record["input_origin"] != "FIXTURE_SYNTHETIC":
            raise ValueError()
        proof, marker = record["validation"]["prepared_execution"], record["validation"]["execution"]
        raw = marker["http_response_text"].encode("utf-8", errors="strict")
        expected = transport_record(proof["contract"], raw, marker["manifest"])
        if preserved_json({key: record[key] for key in expected}) != preserved_json(expected):
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError, ProviderError):
        raise ValueError("paper_ollama_transport_invalid") from None


@dataclass(frozen=True, init=False)
class OllamaPreparedReviewProvider:
    model: str
    timeout_seconds: float
    _transport: httpx.MockTransport = field(repr=False)
    _contract_json: str = field(repr=False)
    _request_json: str = field(repr=False)
    _body: bytes = field(repr=False)
    _manifest_json: str = field(repr=False)

    def __init__(self, contract: dict[str, Any], *, context_tokens: int, timeout_seconds: float,
                 transport: httpx.MockTransport | None = None) -> None:
        # Exact type prevents a different/default network transport or subclass
        # override from being mislabeled by this fixture-only profile.
        if type(transport) is not httpx.MockTransport:
            raise ValueError("paper_ollama_configuration_invalid")
        request, body, manifest = build_plan(contract, context_tokens=context_tokens, timeout_seconds=timeout_seconds)
        for key, value in dict(model=manifest["model"], timeout_seconds=float(timeout_seconds), _transport=transport,
                _contract_json=preserved_json(contract), _request_json=request, _body=body,
                _manifest_json=preserved_json(manifest)).items():
            object.__setattr__(self, key, value)

    @property
    def provider_name(self) -> str:
        return "ollama"

    def manifest(self) -> dict[str, Any]:
        return json.loads(self._manifest_json)

    def is_configured(self) -> bool:
        return type(self._transport) is httpx.MockTransport

    def describe_status(self) -> dict[str, Any]:
        return dict(provider="ollama", model=self.model, configured=self.is_configured(), live_call_enabled=False,
            activation="UNREGISTERED_ADAPTER_CONTRACT", execution_surface=EXECUTION_SURFACE,
            locality=LOCALITY, authority="NOT_ESTABLISHED")

    def _capture(self, request: ProviderRequest) -> CapturedResponse:
        try:
            if not isinstance(request, ProviderRequest) or preserved_json(asdict(request)) != self._request_json:
                raise ValueError()
        except (ValueError, TypeError, AttributeError, RecursionError, OverflowError):
            raise ValueError("paper_ollama_adapter_mismatch") from None
        oversized = False
        try:
            with httpx.Client(timeout=self.timeout_seconds, transport=self._transport, trust_env=False,
                    follow_redirects=False) as client:
                with client.stream("POST", ENDPOINT, headers={"Content-Type": "application/json"}, content=self._body) as response:
                    if response.status_code != 200:
                        raise ProviderError("paper_ollama_transport_outcome_unknown")
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes(chunk_size=65536):
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            oversized = True
                            break
                        chunks.append(chunk)
                    raw = b"".join(chunks)
        except Exception:
            # An injected handler/iterator or cleanup error is not a confirmed
            # provider outcome. Contain all Exception diagnostics from I/O;
            # BaseException cancellation still propagates unchanged.
            raise ProviderError("paper_ollama_transport_outcome_unknown") from None
        if oversized:
            raise ProviderError("paper_ollama_response_outcome_unknown")
        # Parse only after I/O has completed. Preserve the parser's known
        # incomplete/output-invalid categories rather than reclassifying them.
        captured = parse_response(raw)
        try:
            prepared_review.result(json.loads(self._contract_json), captured.result)
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
            raise ProviderOutputError("paper_ollama_output_invalid") from None
        return captured

    def generate_json(self, request: ProviderRequest) -> ProviderResult:
        return self._capture(request).result

    def generate_record(self, request: ProviderRequest) -> dict[str, Any]:
        return _record(json.loads(self._contract_json), self._capture(request), self.manifest())
