"""Native Anthropic Messages paper adapter; transport history is not authority."""
from dataclasses import asdict, dataclass, field
import json
import math
from typing import Any, Callable

import httpx

from app.domain import prepared_review
from app.domain.journal_exchange import preserved_json, sha
from app.services.providers.anthropic_messages import API_VERSION, ENDPOINT, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, SCHEMA_COMPILER, SCHEMA_COMPILER_V2, compile_schema, message_payload, send
from app.services.providers.base import ProviderError, ProviderOutputError, ProviderRefusalError, ProviderReportedFailure, ProviderRequest, ProviderResult, ProviderUnavailableError

ADAPTER_ID = "xrml-anthropic-prepared-review"
MANIFEST_SCHEMA = "ANTHROPIC_PREPARED_REVIEW_WIRE_V1"
MANIFEST_SCHEMA_V2 = "ANTHROPIC_PREPARED_REVIEW_WIRE_V2"
TRANSPORT_SCHEMA = "PREPARED_REVIEW_ANTHROPIC_HTTP_TRANSPORT_V1"
TRANSPORT_SCHEMA_V2 = "PREPARED_REVIEW_ANTHROPIC_HTTP_TRANSPORT_V2"
MODE = "TRUSTED_PAPER_HTTP"
_UNBOUND = object()


def build_plan(contract: dict[str, Any], *, timeout_seconds: float, profile: str = MANIFEST_SCHEMA,
               execution_surface: str = "NATIVE_HTTP", credential_revision: Any = _UNBOUND) -> tuple[str, bytes, dict[str, Any]]:
    frozen = prepared_review.validate_contract(contract)
    if (frozen["provider_name"] != "anthropic" or type(timeout_seconds) not in {int, float}
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 120
            or profile not in {MANIFEST_SCHEMA, MANIFEST_SCHEMA_V2}
            or execution_surface not in {"NATIVE_HTTP", "MOCK_TRANSPORT"}
            or (profile == MANIFEST_SCHEMA and execution_surface != "NATIVE_HTTP")):
        raise ValueError("paper_anthropic_configuration_invalid")
    if credential_revision is not _UNBOUND and (profile != MANIFEST_SCHEMA_V2 or execution_surface != "NATIVE_HTTP"
            or type(credential_revision) is not int or credential_revision < 1):
        raise ValueError("paper_anthropic_configuration_invalid")
    request = ProviderRequest(**frozen["provider_request"])
    payload = message_payload(request, frozen["model"])
    if profile == MANIFEST_SCHEMA_V2:
        # Same structured arm as the local output comparison, without importing
        # its trial/runtime layer. The original request and validator stay frozen.
        payload["output_config"]["format"]["schema"] = compile_schema(request.schema, compiler=SCHEMA_COMPILER_V2)
        payload["system"] += "\nRequired output JSON schema:\n" + preserved_json(request.schema)
    body = preserved_json(payload).encode("utf-8")
    if len(body) > MAX_REQUEST_BYTES:
        raise ValueError("paper_anthropic_configuration_invalid")
    manifest = dict(schema_version=MANIFEST_SCHEMA, adapter_id=ADAPTER_ID, adapter_version="1",
        provider_name="anthropic", endpoint=ENDPOINT, api_version=API_VERSION, method="POST", model=frozen["model"],
        contract_sha256=sha(preserved_json(frozen)), request_sha256=frozen["request_sha256"],
        body_sha256=sha(body.decode("utf-8")), body_bytes=len(body), timeout_seconds=float(timeout_seconds),
        max_response_bytes=MAX_RESPONSE_BYTES, max_review_output_bytes=prepared_review.MAX_OUTPUT_BYTES,
        schema_compiler=SCHEMA_COMPILER, schema_constraints="LOCAL_ORIGINAL_VALIDATION",
        local_schema_sha256=sha(preserved_json(request.schema)),
        wire_schema_sha256=sha(preserved_json(payload["output_config"]["format"]["schema"])),
        stream=False, tools=[], automatic_retry=False, redirects=False, environment_proxy=False,
        request_metadata="LOCAL_BINDING_ONLY_NOT_TRANSMITTED", output_token_limit_enforced=False, temperature_enforced=False,
        temperature_policy="OMITTED_PROVIDER_DEFAULT",
        output_token_scope="MESSAGES_OUTPUT_TOKENS", cost_limit_enforced=False,
        token_limit_policy="REQUESTED_NOT_LIVE_VERIFIED", model_identity="REQUESTED_NOT_INDEPENDENTLY_VERIFIED",
        live_execution="NOT_VERIFIED", billing_route="API_KEY_ACCOUNT_NOT_VERIFIED",
        raw_output_encoding="CONTENT_TEXT_JSON_UTF8_V1", raw_envelope_retained=False,
        usage_input_semantics="NONCACHED_INPUT_TOKENS_CACHE_COUNTS_SEPARATE", total_tokens="NOT_REPORTED_NOT_DERIVED")
    if profile == MANIFEST_SCHEMA_V2:
        manifest.update(schema_version=MANIFEST_SCHEMA_V2, adapter_version="2", schema_compiler=SCHEMA_COMPILER_V2,
            execution_surface=execution_surface, shared_schema_context="ORIGINAL_LOCAL_SCHEMA_IN_SYSTEM",
            effective_system_sha256=sha(payload["system"]), timeout_semantics="PER_OPERATION_INACTIVITY")
    if credential_revision is not _UNBOUND:
        manifest["credential_binding"] = dict(connector_id="anthropic_api", revision=credential_revision, storage="LOCAL_DPAPI")
    return preserved_json(frozen["provider_request"]), body, manifest


def _marker(contract: dict[str, Any], raw: str, manifest: dict[str, Any]) -> dict[str, Any]:
    profile = manifest["schema_version"]
    surface = manifest["execution_surface"] if profile == MANIFEST_SCHEMA_V2 else "NATIVE_HTTP"
    options = {"credential_revision": manifest["credential_binding"]["revision"]} if "credential_binding" in manifest else {}
    _, _, expected = build_plan(contract, timeout_seconds=manifest["timeout_seconds"], profile=profile, execution_surface=surface, **options)
    if preserved_json(manifest) != preserved_json(expected):
        raise ValueError("paper_anthropic_transport_invalid")
    return dict(schema_version=TRANSPORT_SCHEMA_V2 if profile == MANIFEST_SCHEMA_V2 else TRANSPORT_SCHEMA,
        adapter_id=ADAPTER_ID, adapter_version=expected["adapter_version"], manifest=expected,
        manifest_sha256=sha(preserved_json(expected)), response_sha256=sha(raw),
        authority="NOT_ESTABLISHED", live_execution="NOT_VERIFIED")


def validate_origin(manifest: dict[str, Any], origin: str) -> None:
    """V2 explicitly binds test/native provenance; historical V1 is unchanged."""
    if manifest.get("schema_version") == MANIFEST_SCHEMA_V2:
        expected = {"MOCK_TRANSPORT": "FIXTURE_SYNTHETIC", "NATIVE_HTTP": "PROVIDER_RESPONSE"}
        if manifest.get("execution_surface") not in expected or origin != expected[manifest["execution_surface"]]:
            raise ValueError("paper_anthropic_origin_invalid")


def transport_record(contract: dict[str, Any], response: ProviderResult, manifest: dict[str, Any]) -> dict[str, Any]:
    record = prepared_review.result(contract, response)
    record["validation"]["execution"] = _marker(contract, record["validation"]["prepared_execution"]["response"]["raw_text"], manifest)
    return record


def validate_transport(record: dict[str, Any], packet: dict[str, Any]) -> None:
    try:
        prepared_review.validate_record(record, packet)
        proof, marker = record["validation"]["prepared_execution"], record["validation"]["execution"]
        if preserved_json(marker) != preserved_json(_marker(proof["contract"], proof["response"]["raw_text"], marker["manifest"])):
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        raise ValueError("paper_anthropic_transport_invalid") from None


@dataclass(frozen=True, init=False)
class AnthropicPreparedReviewProvider:
    model: str
    timeout_seconds: float
    _api_key: str = field(repr=False)
    _transport: httpx.BaseTransport | None = field(repr=False)
    _contract_json: str = field(repr=False)
    _request_json: str = field(repr=False)
    _body: bytes = field(repr=False)
    _manifest_json: str = field(repr=False)
    _before_send: Callable[[], None] | None = field(repr=False)

    def __init__(self, contract: dict[str, Any], *, api_key: str, timeout_seconds: float,
                 transport: httpx.BaseTransport | None = None, profile: str = MANIFEST_SCHEMA,
                 execution_surface: str = "NATIVE_HTTP", credential_revision: Any = _UNBOUND,
                 before_send: Callable[[], None] | None = None) -> None:
        # A synthetic label cannot accidentally open a real network connection.
        # Conversely V2 native history cannot be produced by an injected fake.
        if profile == MANIFEST_SCHEMA_V2 and (
                (execution_surface == "MOCK_TRANSPORT" and not isinstance(transport, httpx.MockTransport))
                or (execution_surface == "NATIVE_HTTP" and transport is not None)):
            raise ValueError("paper_anthropic_configuration_invalid")
        if before_send is not None and (profile != MANIFEST_SCHEMA_V2 or execution_surface != "NATIVE_HTTP"
                or credential_revision is _UNBOUND or not callable(before_send)):
            raise ValueError("paper_anthropic_configuration_invalid")
        if not isinstance(api_key, str) or any(ord(char) < 32 or ord(char) > 126 for char in api_key):
            raise ValueError("paper_anthropic_configuration_invalid")
        request, body, manifest = build_plan(contract, timeout_seconds=timeout_seconds,
            profile=profile, execution_surface=execution_surface, credential_revision=credential_revision)
        for key, value in dict(model=manifest["model"], timeout_seconds=float(timeout_seconds), _api_key=api_key.strip(),
            _transport=transport, _contract_json=preserved_json(contract), _request_json=request,
            _body=body, _manifest_json=preserved_json(manifest), _before_send=before_send).items():
            object.__setattr__(self, key, value)

    @property
    def provider_name(self) -> str:
        return "anthropic"

    def manifest(self) -> dict[str, Any]:
        return json.loads(self._manifest_json)

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def describe_status(self) -> dict[str, Any]:
        return dict(provider="anthropic", model=self.model, configured=self.is_configured(), live_call_enabled=False,
            activation="UNREGISTERED_ADAPTER_CONTRACT", authority="NOT_ESTABLISHED")

    def validate_local_runtime(self) -> None:
        if not self.is_configured():
            raise ProviderUnavailableError("Explicit Anthropic credentials are unavailable.")

    def generate_json(self, request: ProviderRequest) -> ProviderResult:
        if preserved_json(asdict(request)) != self._request_json:
            raise ValueError("paper_anthropic_adapter_mismatch")
        self.validate_local_runtime()
        try:
            options: dict[str, Any] = {"before_send": self._before_send} if self._before_send is not None else {}
            result = send(self._body, api_key=self._api_key, timeout_seconds=self.timeout_seconds,
                          transport=self._transport, max_output_bytes=prepared_review.MAX_OUTPUT_BYTES, **options)
        except ProviderReportedFailure:
            raise
        except ProviderRefusalError:
            raise ProviderRefusalError("paper_anthropic_provider_refused") from None
        except ProviderOutputError:
            raise ProviderOutputError("paper_anthropic_output_invalid") from None
        except ProviderError:
            raise ProviderError("paper_anthropic_transport_outcome_unknown") from None
        try:
            prepared_review.result(json.loads(self._contract_json), result)
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
            raise ProviderOutputError("paper_anthropic_output_invalid") from None
        return result

    def generate_record(self, request: ProviderRequest) -> dict[str, Any]:
        return transport_record(json.loads(self._contract_json), self.generate_json(request), self.manifest())
