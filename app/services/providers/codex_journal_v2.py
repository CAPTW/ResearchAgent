"""Explicit V2 Journal CLI adapter; construction never grants execution authority."""
from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
from typing import Any, Callable

from app.domain import journal_exchange as exchange, journal_execution
from app.domain.journal_dispatch import AdapterBinding
from app.domain.temporal_memory import digest
from app.services.journal_dispatch import Authority, JournalDispatcher, _unavailable
from app.services.providers.base import ProviderError, ProviderOutputError, ProviderRequest, ProviderResult
from app.services.providers.codex_cli_profile_v2 import RUNTIME_PROFILE
from app.services.providers.codex_journal import read_result
from app.services.providers.codex_journal_plan_v2 import ADAPTER_ID, ADAPTER_VERSION, build_plan
from app.services.providers.codex_journal_process import CliRuntime


@dataclass(frozen=True, init=False)
class CodexJournalV2Provider:
    model: str
    reasoning_effort: str
    timeout_seconds: float
    _runtime: CliRuntime = field(repr=False)
    _contract_json: str = field(repr=False)
    _stdin: str = field(repr=False)
    _schema: str = field(repr=False)
    _manifest_json: str = field(repr=False)

    @property
    def provider_name(self) -> str:
        return "codex_cli"

    def __init__(self, contract: dict[str, Any], *, runtime: CliRuntime, reasoning_effort: str, timeout_seconds: float) -> None:
        stdin, schema, manifest = build_plan(contract, runtime_identity=runtime.identity(),
            reasoning_effort=reasoning_effort, timeout_seconds=timeout_seconds)
        for key, value in dict(model=manifest["model"], reasoning_effort=reasoning_effort, timeout_seconds=float(timeout_seconds),
            _runtime=runtime, _contract_json=exchange.preserved_json(contract), _stdin=stdin,
            _schema=schema, _manifest_json=exchange.preserved_json(manifest)).items():
            object.__setattr__(self, key, value)

    def manifest(self) -> dict[str, Any]:
        return json.loads(self._manifest_json)

    @property
    def binding(self) -> AdapterBinding:
        return AdapterBinding(surface_id="codex_cli", adapter_id=ADAPTER_ID, adapter_version=ADAPTER_VERSION,
                              command_shape_sha256=digest(self.manifest()))

    def validate_journal_binding(self, contract: dict[str, Any], model: str, binding: AdapterBinding | None = None) -> None:
        if (exchange.preserved_json(contract) != self._contract_json or model != self.model
                or (binding is not None and binding != self.binding)
                or exchange.preserved_json(contract["provider_request"]) != self._stdin):
            raise ValueError("journal_execution_adapter_mismatch")

    def is_configured(self) -> bool:
        return True  # Explicit runtime only; no authentication or authority proof.

    def describe_status(self) -> dict[str, Any]:
        return dict(provider=self.provider_name, model=self.model, live_call_enabled=False,
            activation="UNREGISTERED_ADAPTER_CONTRACT", binding=self.binding.model_dump(), authority="NOT_ESTABLISHED")

    def validate_local_runtime(self) -> None:
        self._runtime.verify()

    def generate_json(self, request: ProviderRequest) -> ProviderResult:
        if exchange.preserved_json(asdict(request)) != self._stdin:
            raise ValueError("journal_execution_adapter_mismatch")
        response = self._runtime.run(self._stdin, self._schema, model=self.model,
            effort=self.reasoning_effort, timeout_seconds=self.timeout_seconds, profile=RUNTIME_PROFILE)
        if response.timed_out:
            raise ProviderError("journal_codex_process_outcome_unknown")
        result = read_result(response.stdout, response.last_message, response.exit_code)
        try:
            journal_execution.result(json.loads(self._contract_json), result)
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
            raise ProviderOutputError("journal_codex_output_invalid") from None
        return result


def journal_dispatcher(provider: CodexJournalV2Provider, *, authority: Authority = _unavailable,
                       clock: Callable[[], datetime] | None = None, shutdown_grace_seconds: float = 2) -> JournalDispatcher:
    return JournalDispatcher(provider=provider, model=provider.model, binding=provider.binding,
                             authority=authority, clock=clock, shutdown_grace_seconds=shutdown_grace_seconds)
