"""Unregistered Antigravity paper adapter; fake-child tests are not live use."""
from dataclasses import asdict, dataclass, field
import json
from typing import Any

from app.domain import prepared_review
from app.domain.journal_exchange import preserved_json
from app.services.providers.antigravity_prepared_review_completion import read_completion
from app.services.providers.antigravity_prepared_review_plan import build_plan, transport_record
from app.services.providers.antigravity_prepared_review_process import AntigravityRuntime
from app.services.providers.base import ProviderError, ProviderOutputError, ProviderRequest, ProviderResult


@dataclass(frozen=True, init=False)
class AntigravityPreparedReviewProvider:
    model: str
    timeout_seconds: float
    _runtime: AntigravityRuntime = field(repr=False)
    _contract_json: str = field(repr=False)
    _stdin: str = field(repr=False)
    _schema: str = field(repr=False)
    _manifest_json: str = field(repr=False)

    def __init__(self, contract: dict[str, Any], *, runtime: AntigravityRuntime, timeout_seconds: float = 60) -> None:
        stdin, schema, manifest = build_plan(contract, runtime_identity=runtime.identity(), timeout_seconds=timeout_seconds)
        for key, value in dict(model=manifest["model"], timeout_seconds=float(timeout_seconds), _runtime=runtime,
                _contract_json=preserved_json(contract), _stdin=stdin, _schema=schema, _manifest_json=preserved_json(manifest)).items():
            object.__setattr__(self, key, value)

    @property
    def provider_name(self) -> str:
        return "antigravity_cli"

    def manifest(self) -> dict[str, Any]:
        return json.loads(self._manifest_json)

    def is_configured(self) -> bool:
        return True  # Explicit runtime, not proof of login, authority or capability.

    def describe_status(self) -> dict[str, Any]:
        return dict(provider=self.provider_name, model=self.model, live_call_enabled=False,
                    activation="UNREGISTERED_ADAPTER_CONTRACT", authority="NOT_ESTABLISHED")

    def validate_local_runtime(self) -> None:
        self._runtime.verify()

    def _generate(self, request: ProviderRequest) -> tuple[ProviderResult, str, str]:
        if preserved_json(asdict(request)) != preserved_json(json.loads(self._contract_json)["provider_request"]):
            raise ValueError("paper_antigravity_adapter_mismatch")
        captured = self._runtime.run(self._stdin, self._schema, model=self.model, timeout_seconds=self.timeout_seconds)
        completed = captured.process
        if completed.timed_out:
            raise ProviderError("paper_antigravity_process_outcome_unknown")
        response = read_completion(completed.stdout, completed.exit_code, model=self.model,
                                   schema=json.loads(self._schema), attempt_cwd=captured.attempt_cwd)
        try:
            prepared_review.result(json.loads(self._contract_json), response)
        except (ValueError, TypeError, KeyError, RecursionError):
            raise ProviderOutputError("paper_antigravity_output_invalid") from None
        return response, completed.stdout, captured.attempt_cwd

    def generate_json(self, request: ProviderRequest) -> ProviderResult:
        return self._generate(request)[0]

    def generate_record(self, request: ProviderRequest) -> dict[str, Any]:
        response, raw, cwd = self._generate(request)
        return transport_record(json.loads(self._contract_json), response, self.manifest(), raw, cwd)
