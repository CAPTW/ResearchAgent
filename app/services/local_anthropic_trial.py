"""Explicit server-configured local first-output trial, never ordinary authority.

One atomic local-file claim precedes one HTTP attempt. Raw decoded HTTP bytes
are committed before parsing. Restart/readback never resumes a consumed claim.
This is an operator-selected excerpt qualification, not a Paper Bank receipt.
"""
from base64 import b64decode, b64encode
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import math
from pathlib import Path
import re
import time
from typing import Any, Callable, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.domain import prepared_review
from app.domain.review_preparation import encoded, sha
from app.repositories.shared_state import load_json_state, mutate_json_state
from app.services.providers.anthropic_messages import API_VERSION, ENDPOINT, MAX_RESPONSE_BYTES, SCHEMA_COMPILER, SCHEMA_COMPILER_V2, compile_schema, parse_response, rejection_diagnostic
from app.services.providers.anthropic_prepared_review import build_plan
from app.services.providers.base import ProviderError, ProviderOutputError, ProviderRefusalError, ProviderReportedFailure

SCHEMA = "LOCAL_ANTHROPIC_FIRST_TRIAL_V1"
TERMINAL = {"STRUCTURE_VALID", "STRUCTURE_INVALID", "INCOMPLETE", "REFUSED", "UNKNOWN", "CANCELLED"}
TRACE_EVENTS = frozenset(f"{phase}.{status}" for phase in (
    "connection.connect_tcp", "connection.start_tls", "http11.send_request_headers",
    "http11.send_request_body", "http11.receive_response_headers", "http11.receive_response_body",
    "http11.response_closed") for status in ("started", "complete", "failed"))
logger = logging.getLogger(__name__)


def build_trial_plan(contract: dict[str, Any], read_timeout_seconds: float | None = None,
                     schema_compiler: str = SCHEMA_COMPILER) -> tuple[bytes, dict[str, Any]]:
    # Keep old trial bindings and the ordinary adapter's 120-second contract.
    # A longer local qualification uses a distinct, bound manifest version.
    _, body, manifest = build_plan(contract, timeout_seconds=60)
    if schema_compiler not in {SCHEMA_COMPILER, SCHEMA_COMPILER_V2} or (schema_compiler != SCHEMA_COMPILER and read_timeout_seconds is None):
        raise ValueError("anthropic_trial_configuration_invalid")
    if read_timeout_seconds is not None:
        if (type(read_timeout_seconds) not in {int, float} or not math.isfinite(read_timeout_seconds)
                or not 0 < read_timeout_seconds <= 600):
            raise ValueError("anthropic_trial_configuration_invalid")
        manifest.pop("timeout_seconds")
        manifest["review_output_encoding"] = manifest.pop("raw_output_encoding")
        manifest.update(schema_version="LOCAL_ANTHROPIC_TRIAL_HTTP_WIRE_V1",
            adapter_id="xrml-local-anthropic-trial", raw_envelope_retained=True,
            raw_capture_encoding="HTTP_RESPONSE_DECODED_BODY_BYTES_BASE64_V1",
            http_timeouts=dict(connect=10.0, read=float(read_timeout_seconds), write=30.0, pool=10.0),
            timeout_semantics="PER_OPERATION_INACTIVITY")
    if schema_compiler == SCHEMA_COMPILER_V2:
        payload = json.loads(body)
        wire_schema = compile_schema(contract["provider_request"]["schema"], compiler=schema_compiler)
        payload["output_config"]["format"]["schema"] = wire_schema
        body = encoded(payload).encode("utf-8")
        manifest.update(schema_version="LOCAL_ANTHROPIC_TRIAL_HTTP_WIRE_V2", schema_compiler=schema_compiler,
            body_sha256=sha(body.decode("utf-8")), body_bytes=len(body), wire_schema_sha256=sha(encoded(wire_schema)),
            wire_pattern_policy="DESCRIBED_AND_VALIDATED_LOCALLY")
    return body, manifest


class _TransportTrace:
    """Allowlisted milestones only; httpcore's info contains credentials."""
    def __init__(self) -> None:
        self.started = time.monotonic()
        self.events: list[dict[str, Any]] = []

    def __call__(self, event: str, info: dict[str, Any]) -> None:
        if event not in TRACE_EVENTS or len(self.events) >= 32:
            return
        elapsed = round((time.monotonic() - self.started) * 1000, 3)
        self.events.append(dict(event=event, elapsed_ms=elapsed))
        logger.info("anthropic_trial_transport_phase", extra=dict(event=event, duration_ms=elapsed))


def _valid_trace(value: Any) -> bool:
    if value is None:
        return True  # Historical records predate tracing.
    if type(value) is not list or len(value) > 32:
        return False
    previous = 0.0
    for item in value:
        if (type(item) is not dict or set(item) != {"event", "elapsed_ms"}
                or type(item["event"]) is not str or item["event"] not in TRACE_EVENTS
                or type(item["elapsed_ms"]) not in {int, float} or not math.isfinite(item["elapsed_ms"])
                or item["elapsed_ms"] < previous):
            return False
        previous = item["elapsed_ms"]
    return True


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    title: str = Field(min_length=1, max_length=500)
    pdf_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    page: int = Field(ge=1)
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    excerpt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    excerpt: str = Field(min_length=1, max_length=20000)


class Grant(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    trial_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    actor_id: Literal["local-internal-user"]
    credential_revision: int = Field(ge=1)
    issued_at: str
    expires_at: str
    origin: Literal["PUBLIC_EXCERPT", "FIXTURE_SYNTHETIC"]
    source: Source


def _time(value: str | datetime) -> datetime:
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("anthropic_trial_clock_invalid")
    return result.astimezone(timezone.utc)


def _contains_key(raw: bytes, key: str) -> bool:
    # Also inspect JSON strings nested inside content[].text. Withhold if this
    # bounded scan cannot rule out a credential echo; never log exception text.
    pending = [(raw.decode("utf-8", errors="replace"), 0)]
    scanned = 0
    while pending:
        text, depth = pending.pop()
        scanned += len(text)
        if scanned > MAX_RESPONSE_BYTES * 4 or key in text:
            return True
        for match in re.finditer(r'"(?:[^"\\]|\\.)*"', text):
            if depth >= 8:
                return True
            try:
                value = json.loads(match.group())
            except ValueError:
                continue
            pending.append((value, depth + 1))
    return False


def trial_binding(contract: dict[str, Any], grant: dict[str, Any], read_timeout_seconds: float | None,
                  schema_compiler: str) -> tuple[bytes, dict[str, Any]]:
    """Pure historical request reconstruction; no key, path, clock or transport."""
    frozen = prepared_review.validate_contract(deepcopy(contract))
    allowed = Grant.model_validate(grant)
    if not timedelta(0) < _time(allowed.expires_at) - _time(allowed.issued_at) <= timedelta(minutes=5):
        raise ValueError('anthropic_trial_configuration_invalid')
    source, packet = allowed.source, frozen['packet']
    selection = packet['source_preparation']['selection']
    if (source.excerpt_sha256 != sha(source.excerpt) or source.excerpt_sha256 != packet['source_text_sha256']
            or source.end - source.start != len(source.excerpt)
            or selection['start'] != source.start or selection['end'] != source.end
            or len(packet['evidence_spans']) != 1 or packet['evidence_spans'][0]['text'] != source.excerpt):
        raise ValueError('anthropic_trial_source_invalid')
    body, manifest = build_trial_plan(frozen, read_timeout_seconds, schema_compiler)
    return body, dict(schema_version=SCHEMA, contract=frozen, manifest=manifest, grant=allowed.model_dump())


class LocalAnthropicTrial:
    def __init__(self, *, path: Path, contract: dict[str, Any], grant: dict[str, Any],
                 resolve_key: Callable[[int], str], transport: httpx.BaseTransport | None = None,
                 read_timeout_seconds: float | None = None,
                 schema_compiler: str = SCHEMA_COMPILER,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.path = path
        self._body, self._binding = trial_binding(contract, grant, read_timeout_seconds, schema_compiler)
        self._contract = self._binding['contract']
        self._grant = Grant.model_validate(self._binding['grant'])
        if (not path.is_absolute() or not callable(resolve_key) or not callable(clock)
                or (self._grant.origin == "FIXTURE_SYNTHETIC") != isinstance(transport, httpx.MockTransport)
                or (self._grant.origin == "PUBLIC_EXCERPT" and transport is not None)):
            raise ValueError("anthropic_trial_configuration_invalid")
        manifest = self._binding['manifest']
        self._timeouts = manifest.get("http_timeouts", dict(connect=60.0, read=60.0, write=60.0, pool=60.0))
        self._digest = sha(encoded(self._binding))
        self._resolve, self._transport, self._clock = resolve_key, transport, clock

    def _default(self) -> dict[str, Any]:
        return dict(schema_version=SCHEMA, binding=self._binding, binding_sha256=self._digest, events=[])

    def _current(self, state: dict[str, Any]) -> dict[str, Any]:
        return self.validate_snapshot(state, self._binding, self._digest)

    @staticmethod
    def validate_snapshot(state: dict[str, Any], binding: dict[str, Any], binding_sha256: str) -> dict[str, Any]:
        if state.get("binding_sha256") != binding_sha256 or state.get("binding") != binding:
            raise ValueError("anthropic_trial_binding_changed")
        try:
            if set(state) != {"schema_version", "binding", "binding_sha256", "events"} or state["schema_version"] != SCHEMA:
                raise ValueError()
            events = state["events"]
            if type(events) is not list or len(events) > 6:
                raise ValueError()
            current = dict(state="PREPARED", attempts=0, capture_b64=None, http_status=None, draft=None,
                actual_model=None, response_id=None, usage=None, error_code=None, transport_trace=None)
            previous = binding_sha256
            for sequence, event in enumerate(events):
                unsigned = {key: event[key] for key in ("sequence", "at", "previous_sha256", "changes")}
                if (set(event) != set(unsigned) | {"sha256"} or event["sequence"] != sequence
                        or event["previous_sha256"] != previous or event["sha256"] != sha(encoded(unsigned))):
                    raise ValueError()
                _time(event["at"])
                changes = event["changes"]
                if type(changes) is not dict or set(changes) - set(current):
                    raise ValueError()
                prior = current["state"]
                current.update(changes)
                selected = current["state"]
                if not ((prior == "PREPARED" and selected in {"CLAIMED", "CANCELLED"})
                        or (prior == "CLAIMED" and selected in {"CAPTURED", "UNKNOWN", "CANCELLED"})
                        or (prior == "CAPTURED" and selected in TERMINAL)
                        or (prior == "CANCELLED" and selected == "CANCELLED" and ("capture_b64" in changes
                            or "transport_trace" in changes and set(changes) <= {"transport_trace", "http_status", "error_code"}))):
                    raise ValueError()
                if not _valid_trace(current["transport_trace"]):
                    raise ValueError()
                if (type(current["attempts"]) is not int or current["attempts"] not in {0, 1}
                        or (selected != "CANCELLED" and current["attempts"] != 1)):
                    raise ValueError()
                previous = event["sha256"]
            return current
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ValueError("anthropic_trial_integrity_invalid") from None

    def _read(self) -> dict[str, Any]:
        if self.path.exists() and self.path.stat().st_size > 12_000_000:
            raise ValueError("anthropic_trial_integrity_invalid")
        state = load_json_state(self.path, state_name="anthropic_trial", expected_type=dict, default=self._default)
        self._current(state)
        return state

    def _append(self, state: dict[str, Any], changes: dict[str, Any]) -> None:
        events = state["events"]
        event = dict(sequence=len(events), at=_time(self._clock()).isoformat(),
            previous_sha256=events[-1]["sha256"] if events else self._digest, changes=changes)
        events.append(dict(event, sha256=sha(encoded(event))))
        self._current(state)

    def _mutate(self, operation: Callable[[dict[str, Any], dict[str, Any]], bool]) -> bool:
        def mutate(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            return state, operation(state, self._current(state))
        return mutate_json_state(self.path, state_name="anthropic_trial", expected_type=dict,
            default=self._default, mutate=mutate)

    def _valid_time(self) -> bool:
        return _time(self._grant.issued_at) <= _time(self._clock()) < _time(self._grant.expires_at)

    def _check_binding(self, expected: str) -> None:
        if expected != self._digest:
            raise ValueError("anthropic_trial_binding_changed")

    def inspect(self) -> dict[str, Any]:
        current = self._current(self._read())
        valid_time = self._valid_time()
        result = {key: value for key, value in current.items() if key != "capture_b64"}
        diagnostic = None
        output_diagnostic = None
        if current["capture_b64"] is not None:
            try:
                raw = b64decode(current["capture_b64"], validate=True)
                diagnostic = rejection_diagnostic(current["http_status"], raw)
                if current["state"] == "STRUCTURE_INVALID" and current["http_status"] == 200:
                    parsed = parse_response(raw, max_output_bytes=prepared_review.MAX_OUTPUT_BYTES)
                    reasons = prepared_review.output_rejection_reasons(self._contract, parsed)
                    if reasons:
                        output_diagnostic = dict(schema_version="ANTHROPIC_TRIAL_OUTPUT_DIAGNOSTIC_V1",
                            source="SAVED_RAW_RESPONSE", raw_response_sha256=hashlib.sha256(raw).hexdigest(),
                            reported_model=parsed.model, response_id=parsed.response_id,
                            usage=parsed.usage, reason_codes=reasons)
            except (ProviderError, ValueError, TypeError):
                pass
        if current["state"] == "PREPARED" and not valid_time:
            result["state"] = "EXPIRED"
        result.update(trial_id=self._grant.trial_id, binding_sha256=self._digest,
            source=self._grant.source.model_dump(), model=self._contract["model"],
            max_output_tokens=self._contract["provider_request"]["max_output_tokens"],
            http_timeouts=deepcopy(self._timeouts),
            provider_diagnostic=diagnostic,
            output_diagnostic=output_diagnostic,
            credential_revision=self._grant.credential_revision, expires_at=self._grant.expires_at,
            origin=self._grant.origin, raw_capture_available=current["capture_b64"] is not None,
            can_execute=current["state"] == "PREPARED" and valid_time,
            scientific_support="NOT_ASSESSED", full_document_review="NOT_ESTABLISHED")
        return dict(enabled=True, reason=None, trial=result)

    def read_capture(self) -> dict[str, Any]:
        captured = self._current(self._read())["capture_b64"]
        if captured is None:
            raise ValueError("anthropic_trial_capture_unavailable")
        try:
            raw = b64decode(captured, validate=True)
            text = raw.decode("utf-8", errors="strict")
        except (ValueError, UnicodeError):
            raise ValueError("anthropic_trial_capture_unavailable") from None
        return dict(trial_id=self._grant.trial_id, binding_sha256=self._digest,
                    raw_response=text, sha256=hashlib.sha256(raw).hexdigest())

    def archive_capture(self, actor: str, expected_binding: str | None = None,
                        expected_response: str | None = None) -> dict[str, Any]:
        """Copy one validated success snapshot. Never executes or mutates the trial."""
        if actor != self._grant.actor_id:
            raise PermissionError('anthropic_trial_local_actor_required')
        if expected_binding is not None:
            self._check_binding(expected_binding)
        from app.domain.captured_analysis import from_snapshot
        bundle = from_snapshot(self._read(), self._body)
        if expected_response is not None and bundle['response_sha256'] != expected_response:
            raise ValueError('anthropic_trial_capture_changed')
        return bundle

    def cancel(self, expected: str) -> dict[str, Any]:
        self._check_binding(expected)
        def cancel(state: dict[str, Any], current: dict[str, Any]) -> bool:
            if current["state"] not in TERMINAL:
                self._append(state, dict(state="CANCELLED"))
            return False
        self._mutate(cancel)
        return self.inspect()

    def execute(self, expected: str) -> dict[str, Any]:
        self._check_binding(expected)
        if self._current(self._read())["state"] != "PREPARED":
            return self.inspect()
        if not self._valid_time():
            raise ValueError("anthropic_trial_expired")
        # Pin exactly the approved stored revision; never fall back to env/account.
        key = self._resolve(self._grant.credential_revision)
        if not key or len(key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise ValueError("anthropic_trial_credential_unavailable")
        def claim(state: dict[str, Any], current: dict[str, Any]) -> bool:
            if current["state"] != "PREPARED":
                return False
            if not self._valid_time():
                raise ValueError("anthropic_trial_expired")
            self._append(state, dict(state="CLAIMED", attempts=1))
            return True
        if not self._mutate(claim):
            return self.inspect()
        updates: dict[str, Any] = dict(state="UNKNOWN", error_code="anthropic_trial_outcome_unknown")
        raw: bytes | None = None
        status: int | None = None
        trace = _TransportTrace()
        try:
            transport = self._transport if self._transport is not None else httpx.HTTPTransport(retries=0, trust_env=False)
            with httpx.Client(transport=transport, timeout=httpx.Timeout(**self._timeouts), follow_redirects=False, trust_env=False) as client:
                # Observe cancellation/expiry again at the final pre-send boundary.
                # Cancellation after this point fences the result, but cannot
                # guarantee that the provider has not received/billed the request.
                if self._current(self._read())["state"] == "CANCELLED":
                    return self.inspect()
                if not self._valid_time():
                    raise ProviderError("anthropic_trial_expired_before_send")
                with client.stream("POST", ENDPOINT, content=self._body,
                        extensions={"trace": trace},
                        headers={"x-api-key": key, "anthropic-version": API_VERSION, "Content-Type": "application/json"}) as response:
                    status = response.status_code
                    chunks, size = [], 0
                    for chunk in response.iter_bytes(chunk_size=65536):
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            raise ProviderError("anthropic_trial_capture_too_large")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
            if _contains_key(raw, key):
                raw = None
                raise ProviderError("anthropic_trial_capture_withheld")
        except httpx.TimeoutException as exc:
            # Record only a fixed class label, never exception text/request data.
            labels = {httpx.ConnectTimeout: "connect_timeout", httpx.ReadTimeout: "read_timeout",
                httpx.WriteTimeout: "write_timeout", httpx.PoolTimeout: "pool_timeout"}
            updates["error_code"] = "anthropic_trial_" + labels.get(type(exc), "transport_timeout")
            raw = None
        except httpx.HTTPError:
            updates["error_code"] = "anthropic_trial_transport_error"
            raw = None
        except ProviderError as exc:
            if str(exc) in {"anthropic_trial_capture_too_large", "anthropic_trial_capture_withheld",
                            "anthropic_trial_expired_before_send"}:
                updates["error_code"] = str(exc)
            raw = None
        finally:
            key = ""
        updates["http_status"] = status
        updates["transport_trace"] = trace.events
        if raw is not None:
            def capture(state: dict[str, Any], current: dict[str, Any]) -> bool:
                self._append(state, dict(state="CANCELLED" if current["state"] == "CANCELLED" else "CAPTURED",
                    capture_b64=b64encode(raw).decode("ascii"), http_status=status, transport_trace=trace.events))
                return False
            self._mutate(capture)
            if self._current(self._read())["state"] == "CANCELLED":
                return self.inspect()
            try:
                if status != 200:
                    raise ProviderError("anthropic_trial_http_error")
                parsed = parse_response(raw, max_output_bytes=prepared_review.MAX_OUTPUT_BYTES)
                prepared_review.result(self._contract, parsed)
                updates = dict(state="STRUCTURE_VALID", draft=parsed.payload, actual_model=parsed.model,
                    response_id=parsed.response_id, usage=parsed.usage, error_code=None)
            except ProviderRefusalError:
                updates = dict(state="REFUSED", error_code="anthropic_trial_refused")
            except ProviderReportedFailure:
                updates = dict(state="INCOMPLETE", error_code="anthropic_trial_incomplete")
            except (ProviderOutputError, ValueError, TypeError, KeyError):
                updates = dict(state="STRUCTURE_INVALID", error_code="anthropic_trial_structure_invalid")
            except ProviderError:
                updates = dict(state="UNKNOWN", error_code="anthropic_trial_http_error" if status != 200 else "anthropic_trial_outcome_unknown")
        def finish(state: dict[str, Any], current: dict[str, Any]) -> bool:
            if current["state"] != "CANCELLED":
                self._append(state, updates)
            elif raw is None:
                self._append(state, dict(transport_trace=trace.events, http_status=status, error_code=updates["error_code"]))
            return False
        self._mutate(finish)
        return self.inspect()
