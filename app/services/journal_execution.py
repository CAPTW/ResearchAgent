"""One explicitly injected structured-output adapter call, outside SQLite locks.

The isolated offline app route supplies only its synthetic adapter through the
dispatcher. There is no live HTTP execution route, provider registry lookup,
credential discovery or automatic retry. Live wiring must first enforce authority
and budgets. Injecting a provider here is not evidence of that authority or of a
live model run. Existing Codex prototype/proof paths are not called by this module.
"""
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from app.domain import journal_execution as domain
from app.domain.journal_dispatch import AdapterBinding
from app.repositories import journal_executions as repository
from app.services.providers.base import ProviderError, ProviderOutputError, ProviderRefusalError, ProviderReportedFailure, ProviderRequest, StructuredOutputProvider


def execute(claimed: repository.Claim, *, provider: StructuredOutputProvider, model: str,
            clock: Callable[[], datetime] | None = None) -> dict[str, Any]:
    now = clock or (lambda: datetime.now(timezone.utc))
    scope = (claimed.topic, claimed.job_id, claimed.execution_id)
    saved = repository.detail(*scope)
    specification = saved["contract"]["specification"]
    if provider.provider_name != specification["provider_name"] or model != specification["model"]:
        raise ValueError("journal_execution_adapter_mismatch")
    validate_binding = getattr(provider, "validate_journal_binding", None)
    if validate_binding is not None:
        # Bind this claim to its original reservation, including terminal replay.
        # A legacy raw claim must not start the concrete bound adapter.
        receipt = next((event.get("dispatch") for event in saved["events"]
                        if event["state"] == "LEASED" and event["attempt"] == claimed.attempt), None)
        if receipt is None:
            raise ValueError("journal_execution_adapter_mismatch")
        validate_binding(saved["contract"], model, AdapterBinding.model_validate(receipt["binding"]))
    started = repository.start(claimed, now=now())
    if started is None:
        return repository.detail(*scope)  # duplicate, cancelled or expired; no call
    try:
        configured = provider.is_configured()
        request = ProviderRequest(**started["contract"]["provider_request"])
        response = provider.generate_json(request) if configured else None
    except ProviderRefusalError:
        repository.fail(claimed, "provider_refused", unknown=False, now=now())
    except ProviderReportedFailure as exc:
        repository.fail(claimed, "provider_" + exc.status, unknown=False, now=now())
    except ProviderOutputError:
        repository.fail(claimed, "output_invalid", unknown=False, now=now())
    except (ProviderError, httpx.HTTPError, TimeoutError):
        # A timeout/error is not evidence that the remote operation never ran.
        repository.fail(claimed, "provider_error", unknown=True, now=now())
    except Exception:
        repository.fail(claimed, "adapter_error", unknown=True, now=now())
        raise
    else:
        if not configured:
            repository.fail(claimed, "provider_unavailable", unknown=False, now=now())
            return repository.detail(*scope)
        try:
            if response is None:
                raise ValueError("journal_execution_output_invalid")
            result = domain.result(started["contract"], response)
        except (ValueError, TypeError, AttributeError, KeyError):
            repository.fail(claimed, "output_invalid", unknown=False, now=now())
        else:
            repository.finish(claimed, result, now=now())
    return repository.detail(*scope)
