"""Unregistered paper-review CLI adapter. No live authority, discovery or retry.

The app's isolated synthetic approval cannot invoke this adapter. Construction
requires an explicit runtime; callers must supply a separately authorized live
composition before using a real CLI. Current qualification uses fake processes.
"""
from dataclasses import asdict, dataclass, field
import json
from typing import Any

from app.domain import prepared_review
from app.domain.journal_exchange import preserved_json
from app.services.providers.base import ProviderError, ProviderOutputError, ProviderRequest, ProviderResult
from app.services.providers.codex_cli_completion import read_completion
from app.services.providers.codex_journal_process import CliRuntime
from app.services.providers.codex_prepared_review_plan import build_plan, transport_record


@dataclass(frozen=True, init=False)
class CodexPreparedReviewProvider:
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
            _runtime=runtime, _contract_json=preserved_json(contract), _stdin=stdin, _schema=schema,
            _manifest_json=preserved_json(manifest)).items():
            object.__setattr__(self, key, value)

    def manifest(self) -> dict[str, Any]:
        return json.loads(self._manifest_json)

    def is_configured(self) -> bool:
        return True  # Explicit runtime only, not login or execution permission.

    def describe_status(self) -> dict[str, Any]:
        return dict(provider=self.provider_name, model=self.model, live_call_enabled=False,
                    activation="UNREGISTERED_ADAPTER_CONTRACT", authority="NOT_ESTABLISHED")

    def validate_local_runtime(self) -> None:
        """Read local runtime metadata before reservation; never issue authority."""
        self._runtime.verify()

    def generate_json(self, request: ProviderRequest) -> ProviderResult:
        if preserved_json(asdict(request)) != self._stdin:
            raise ValueError("paper_codex_adapter_mismatch")
        response = self._runtime.run(self._stdin, self._schema, model=self.model,
                                     effort=self.reasoning_effort, timeout_seconds=self.timeout_seconds)
        if response.timed_out:
            raise ProviderError("paper_codex_process_outcome_unknown")
        thread_id, usage = read_completion(response.stdout, response.last_message, response.exit_code, error_prefix="paper_codex")
        try:
            raw = response.last_message
            result = ProviderResult(self.provider_name, "not_reported", thread_id, json.loads(raw), usage=usage, raw_text=raw)
            # Reparse strictly: duplicate keys, numeric/type/size constraints and
            # exact source/citation identity are enforced by the paper contract.
            prepared_review.result(json.loads(self._contract_json), result)
            return result
        except (ValueError, TypeError, KeyError, RecursionError):
            raise ProviderOutputError("paper_codex_output_invalid") from None

    def generate_record(self, request: ProviderRequest) -> dict[str, Any]:
        """One attempt, yielding an unadopted record for the existing receipt store."""
        response = self.generate_json(request)
        return transport_record(json.loads(self._contract_json), response, self.manifest())
