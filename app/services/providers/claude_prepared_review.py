"""Unregistered Claude paper adapter; fake-process qualification is not live use."""
from dataclasses import asdict, dataclass, field
import json
from typing import Any

from app.domain import prepared_review
from app.domain.journal_exchange import preserved_json
from app.services.providers.base import ProviderError, ProviderOutputError, ProviderRequest, ProviderResult
from app.services.providers.claude_prepared_review_completion import read_completion
from app.services.providers.claude_prepared_review_plan import build_plan, transport_record
from app.services.providers.claude_prepared_review_process import ClaudeRuntime


@dataclass(frozen=True, init=False)
class ClaudePreparedReviewProvider:
    model: str
    timeout_seconds: float
    _runtime: ClaudeRuntime = field(repr=False)
    _contract_json: str = field(repr=False)
    _stdin: str = field(repr=False)
    _schema: str = field(repr=False)
    _manifest_json: str = field(repr=False)

    def __init__(self, contract: dict[str, Any], *, runtime: ClaudeRuntime, timeout_seconds: float = 60) -> None:
        stdin, schema, manifest = build_plan(contract, runtime_identity=runtime.identity(), timeout_seconds=timeout_seconds)
        for key, value in dict(model=manifest["model"], timeout_seconds=float(timeout_seconds), _runtime=runtime,
                _contract_json=preserved_json(contract), _stdin=stdin, _schema=schema, _manifest_json=preserved_json(manifest)).items():
            object.__setattr__(self, key, value)

    @property
    def provider_name(self) -> str:
        return "claude_code"

    def manifest(self) -> dict[str, Any]:
        return json.loads(self._manifest_json)

    def is_configured(self) -> bool:
        return True  # Explicit runtime, not proof of login, authority or capability.

    def describe_status(self) -> dict[str, Any]:
        return dict(provider=self.provider_name, model=self.model, live_call_enabled=False,
                    activation="UNREGISTERED_ADAPTER_CONTRACT", authority="NOT_ESTABLISHED")

    def validate_local_runtime(self) -> None:
        self._runtime.verify()

    def _generate(self, request: ProviderRequest) -> tuple[ProviderResult, str]:
        if preserved_json(asdict(request)) != self._stdin:
            raise ValueError("paper_claude_adapter_mismatch")
        completed = self._runtime.run(self._stdin, self._schema, model=self.model, timeout_seconds=self.timeout_seconds)
        if completed.timed_out:
            raise ProviderError("paper_claude_process_outcome_unknown")
        response = read_completion(completed.stdout, completed.exit_code)
        try:
            prepared_review.result(json.loads(self._contract_json), response)
        except (ValueError, TypeError, KeyError, RecursionError):
            raise ProviderOutputError("paper_claude_output_invalid") from None
        return response, completed.stdout

    def generate_json(self, request: ProviderRequest) -> ProviderResult:
        return self._generate(request)[0]

    def generate_record(self, request: ProviderRequest) -> dict[str, Any]:
        response, raw = self._generate(request)
        return transport_record(json.loads(self._contract_json), response, self.manifest(), raw)
