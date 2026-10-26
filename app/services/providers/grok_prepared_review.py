"""Explicit, unregistered Grok ACP adapter for one frozen paper request."""
from dataclasses import asdict, dataclass, field
import json
from typing import Any

from app.domain import prepared_review
from app.domain.journal_exchange import preserved_json
from app.services.providers.base import ProviderOutputError, ProviderRequest, ProviderResult
from app.services.providers.grok_prepared_review_plan import build_plan, transport_record
from app.services.providers.grok_prepared_review_process import GrokRuntime
from app.services.providers.grok_prepared_review_protocol import replay


@dataclass(frozen=True, init=False)
class GrokPreparedReviewProvider:
    model: str
    timeout_seconds: float
    _runtime: GrokRuntime = field(repr=False)
    _contract_json: str = field(repr=False)
    _request_text: str = field(repr=False)
    _manifest_json: str = field(repr=False)

    def __init__(self, contract: dict[str, Any], *, runtime: GrokRuntime, timeout_seconds: float = 60) -> None:
        request_text, manifest = build_plan(contract, runtime_identity=runtime.identity(), timeout_seconds=timeout_seconds)
        for key, value in dict(model=manifest["model"], timeout_seconds=float(timeout_seconds), _runtime=runtime,
                _contract_json=preserved_json(contract), _request_text=request_text, _manifest_json=preserved_json(manifest)).items():
            object.__setattr__(self, key, value)

    @property
    def provider_name(self) -> str:
        return "grok_cli"

    def manifest(self) -> dict[str, Any]:
        return json.loads(self._manifest_json)

    def is_configured(self) -> bool:
        return True  # Explicit runtime only; not an account/authority assertion.

    def describe_status(self) -> dict[str, Any]:
        return dict(provider=self.provider_name, model=self.model, live_call_enabled=False,
                    activation="UNREGISTERED_ADAPTER_CONTRACT", authority="NOT_ESTABLISHED")

    def validate_local_runtime(self) -> None:
        self._runtime.verify()

    def _generate(self, request: ProviderRequest) -> tuple[ProviderResult, list[dict[str, str]], str]:
        if preserved_json(asdict(request)) != self._request_text:
            raise ValueError("paper_grok_adapter_mismatch")
        captured = self._runtime.run(self._request_text, model=self.model, timeout_seconds=self.timeout_seconds)
        response = replay(captured.transcript, request_text=self._request_text, model=self.model, cwd=captured.attempt_cwd)
        try:
            prepared_review.result(json.loads(self._contract_json), response)
        except (ValueError, TypeError, KeyError, RecursionError):
            raise ProviderOutputError("paper_grok_output_invalid") from None
        return response, captured.transcript, captured.attempt_cwd

    def generate_json(self, request: ProviderRequest) -> ProviderResult:
        return self._generate(request)[0]

    def generate_record(self, request: ProviderRequest) -> dict[str, Any]:
        response, transcript, cwd = self._generate(request)
        return transport_record(json.loads(self._contract_json), response, self.manifest(), transcript, cwd)
