"""Unregistered Antigravity Journal adapter; explicit authority stays separate.

The persisted raw Journal is result.response exactly, not the captured NDJSON.
The reused reader applies the smaller paper response cap before Journal checks.
"""
from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
from typing import Any, Callable

from app.domain import journal_exchange as exchange, journal_execution
from app.domain.journal_dispatch import AdapterBinding
from app.domain.temporal_memory import digest
from app.services.journal_dispatch import Authority, JournalDispatcher, _unavailable
from app.services.providers.antigravity_journal_plan import ADAPTER_ID, SURFACE, build_plan
from app.services.providers.antigravity_prepared_review_completion import read_completion
from app.services.providers.antigravity_prepared_review_process import AntigravityRuntime
from app.services.providers.base import ProviderError, ProviderOutputError, ProviderReportedFailure, ProviderRequest, ProviderResult


@dataclass(frozen=True, init=False)
class AntigravityJournalProvider:
    model: str
    timeout_seconds: float
    _runtime: AntigravityRuntime = field(repr=False)
    _contract_json: str = field(repr=False)
    _request_json: str = field(repr=False)
    _stdin: str = field(repr=False)
    _schema: str = field(repr=False)
    _manifest_json: str = field(repr=False)

    def __init__(self, contract: dict[str, Any], *, runtime: AntigravityRuntime, timeout_seconds: float = 60) -> None:
        stdin, schema, manifest = build_plan(contract, runtime_identity=runtime.identity(), timeout_seconds=timeout_seconds)
        for key, value in dict(model=manifest["model"], timeout_seconds=float(timeout_seconds), _runtime=runtime,
            _contract_json=exchange.preserved_json(contract), _request_json=json.loads(stdin)["message"]["content"],
            _stdin=stdin, _schema=schema, _manifest_json=exchange.preserved_json(manifest)).items():
            object.__setattr__(self, key, value)

    @property
    def provider_name(self) -> str:
        return SURFACE

    def manifest(self) -> dict[str, Any]:
        return json.loads(self._manifest_json)

    @property
    def binding(self) -> AdapterBinding:
        return AdapterBinding(surface_id=SURFACE, adapter_id=ADAPTER_ID, adapter_version="1",
                              command_shape_sha256=digest(self.manifest()))

    def validate_journal_binding(self, contract: dict[str, Any], model: str, binding: AdapterBinding | None = None) -> None:
        if (exchange.preserved_json(contract) != self._contract_json or model != self.model
                or exchange.preserved_json(contract["provider_request"]) != self._request_json
                or (binding is not None and binding != self.binding)):
            raise ValueError("journal_execution_adapter_mismatch")

    def is_configured(self) -> bool:
        return True  # Explicit runtime, not login or execution authorization.

    def describe_status(self) -> dict[str, Any]:
        return dict(provider=SURFACE, model=self.model, live_call_enabled=False,
            activation="UNREGISTERED_ADAPTER_CONTRACT", authority="NOT_ESTABLISHED", binding=self.binding.model_dump())

    def validate_local_runtime(self) -> None:
        self._runtime.verify()

    def generate_json(self, request: ProviderRequest) -> ProviderResult:
        if exchange.preserved_json(asdict(request)) != self._request_json:
            raise ValueError("journal_execution_adapter_mismatch")
        try:
            captured = self._runtime.run(self._stdin, self._schema, model=self.model, timeout_seconds=self.timeout_seconds)
            completed = captured.process
            # An error-looking envelope cannot make a crash or signal known.
            if (completed.timed_out or completed.last_message or type(completed.exit_code) is not int
                    or completed.exit_code not in {0, 1}):
                raise ProviderError("journal_antigravity_process_outcome_unknown")
            response = read_completion(completed.stdout, completed.exit_code, model=self.model,
                                       schema=json.loads(self._schema), attempt_cwd=captured.attempt_cwd)
        except ProviderReportedFailure:
            raise
        except ProviderOutputError:
            raise ProviderOutputError("journal_antigravity_output_invalid") from None
        except ProviderError:
            raise ProviderError("journal_antigravity_process_outcome_unknown") from None
        try:
            journal_execution.result(json.loads(self._contract_json), response)
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
            raise ProviderOutputError("journal_antigravity_output_invalid") from None
        return response


def journal_dispatcher(provider: AntigravityJournalProvider, *, authority: Authority = _unavailable,
                       clock: Callable[[], datetime] | None = None, shutdown_grace_seconds: float = 2) -> JournalDispatcher:
    return JournalDispatcher(provider=provider, model=provider.model, binding=provider.binding, authority=authority,
                             clock=clock, shutdown_grace_seconds=shutdown_grace_seconds)
