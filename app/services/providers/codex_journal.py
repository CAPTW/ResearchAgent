"""Unregistered Journal CLI adapter. No account selection or live approval issuer.

Construction alone does not execute anything. The dispatcher denies by default;
test approval/CLI fixtures are not evidence of live config isolation or science.
"""
from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
from typing import Any, Callable

from app.domain import journal_exchange as exchange
from app.domain.journal_dispatch import AdapterBinding
from app.domain.temporal_memory import digest
from app.services.journal_dispatch import Authority, JournalDispatcher, _unavailable
from app.services.providers.base import ProviderError, ProviderOutputError, ProviderRequest, ProviderResult
from app.services.providers.codex_cli_completion import read_completion
from app.services.providers.codex_journal_plan import ADAPTER_ID, build_plan
from app.services.providers.codex_journal_process import CliRuntime


def read_result(stdout: str, final: str, code: int) -> ProviderResult:
    thread_id, usage = read_completion(stdout, final, code, error_prefix="journal_codex")
    try:
        payload = exchange.parse(final).model_dump()
    except ValueError:
        raise ProviderOutputError("journal_codex_output_invalid") from None
    return ProviderResult("codex_cli", "not_reported", thread_id, payload, usage=usage, raw_text=final)


@dataclass(frozen=True, init=False)
class CodexJournalProvider:
    model: str
    reasoning_effort: str
    timeout_seconds: float
    _runtime: CliRuntime = field(repr=False)
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
            _runtime=runtime, _stdin=stdin, _schema=schema, _manifest_json=exchange.preserved_json(manifest)).items():
            object.__setattr__(self, key, value)

    def manifest(self) -> dict[str, Any]:
        return json.loads(self._manifest_json)

    @property
    def binding(self) -> AdapterBinding:
        return AdapterBinding(surface_id="codex_cli", adapter_id=ADAPTER_ID, adapter_version="1", command_shape_sha256=digest(self.manifest()))

    def validate_journal_binding(self, contract: dict[str, Any], model: str, binding: AdapterBinding | None = None) -> None:
        if (digest(contract) != self.manifest()["contract_digest"] or model != self.model
                or exchange.preserved_json(contract["provider_request"]) != self._stdin
                or (binding is not None and binding != self.binding)):
            raise ValueError("journal_execution_adapter_mismatch")

    def is_configured(self) -> bool:
        return True  # Explicit runtime exists; not login, capability or authority proof.

    def describe_status(self) -> dict[str, Any]:
        return dict(provider=self.provider_name, model=self.model, live_call_enabled=False,
                    activation="UNREGISTERED_ADAPTER_CONTRACT", binding=self.binding.model_dump())

    def validate_local_runtime(self) -> None:
        """Read local runtime metadata before reservation; never issue authority."""
        self._runtime.verify()

    def generate_json(self, request: ProviderRequest) -> ProviderResult:
        if exchange.preserved_json(asdict(request)) != self._stdin:
            raise ValueError("journal_execution_adapter_mismatch")
        response = self._runtime.run(self._stdin, self._schema, model=self.model,
                                     effort=self.reasoning_effort, timeout_seconds=self.timeout_seconds)
        if response.timed_out:
            raise ProviderError("journal_codex_process_outcome_unknown")
        return read_result(response.stdout, response.last_message, response.exit_code)


def journal_dispatcher(provider: CodexJournalProvider, *, authority: Authority = _unavailable,
                       clock: Callable[[], datetime] | None = None, shutdown_grace_seconds: float = 2) -> JournalDispatcher:
    return JournalDispatcher(provider=provider, model=provider.model, binding=provider.binding,
                             authority=authority, clock=clock, shutdown_grace_seconds=shutdown_grace_seconds)
