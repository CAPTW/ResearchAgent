"""Bind connection choices to existing immutable adapters; never grant authority.

Server-side composition only. No browser-controlled endpoint, executable, secret
name, issuer or proof. Changing the default cannot retarget an existing contract.
"""
from typing import Any

import httpx

from app.domain import prepared_review
from app.services import model_connections as connections
from app.services.providers.codex_journal import CodexJournalProvider
from app.services.providers.codex_journal_process import CliRuntime
from app.services.providers.codex_prepared_review import CodexPreparedReviewProvider
from app.services.providers.codex_journal_v2 import CodexJournalV2Provider
from app.services.providers.codex_prepared_review_v2 import CodexPreparedReviewV2Provider
from app.services.providers.openai_journal import OpenAIJournalProvider
from app.services.providers.xai_journal import XaiJournalProvider
from app.services.providers.openai_prepared_review import OpenAIPreparedReviewProvider
from app.services.providers.xai_prepared_review import XaiPreparedReviewProvider
from app.services.providers.claude_prepared_review import ClaudePreparedReviewProvider
from app.services.providers.claude_journal import ClaudeJournalProvider
from app.services.providers.claude_prepared_review_process import ClaudeRuntime
from app.services.providers.antigravity_prepared_review import AntigravityPreparedReviewProvider
from app.services.providers.antigravity_journal import AntigravityJournalProvider
from app.services.providers.antigravity_prepared_review_process import AntigravityRuntime
from app.services.providers.grok_journal import GrokJournalProvider
from app.services.providers.grok_prepared_review import GrokPreparedReviewProvider
from app.services.providers.grok_prepared_review_process import GrokRuntime
from app.services.providers.anthropic_journal import AnthropicJournalProvider
from app.services.providers.anthropic_prepared_review import AnthropicPreparedReviewProvider

OPENAI_ENDPOINT = "https://api.openai.com/v1/responses"


def prepare_paper(packet: dict[str, Any], *, max_output_tokens: int = 2200) -> dict[str, Any]:
    state = connections.read()
    identity, _ = connections.selection(state)
    connector = _connector(identity)
    model = state["models"].get(identity, "")
    if not model:
        raise ValueError("model_connection_model_required")
    return prepared_review.prepare(packet, provider_name=connector.provider_name, model=model, max_output_tokens=max_output_tokens)


def _connector(identity: str) -> connections.Connector:
    if identity not in connections.BY_ID:
        raise ValueError("model_connection_unavailable")
    connector = connections.BY_ID[identity]
    if not connector.adapter_available:
        raise ValueError("model_connection_adapter_unavailable")
    return connector


def _key(connector: connections.Connector) -> str:
    from app.services.model_credentials import resolve
    return resolve(connector.connector_id)


def bind_paper(contract: dict[str, Any], *, connector_id: str, runtime: CliRuntime | ClaudeRuntime | AntigravityRuntime | GrokRuntime | None = None,
               reasoning_effort: str = "medium", timeout_seconds: float = 60,
               transport: httpx.BaseTransport | None = None, codex_profile: str = "CODEX_JOURNAL_CLI_V1"
               ) -> CodexPreparedReviewProvider | CodexPreparedReviewV2Provider | ClaudePreparedReviewProvider | AntigravityPreparedReviewProvider | GrokPreparedReviewProvider | OpenAIPreparedReviewProvider | XaiPreparedReviewProvider | AnthropicPreparedReviewProvider:
    if type(codex_profile) is not str or codex_profile not in {"CODEX_JOURNAL_CLI_V1", "CODEX_JOURNAL_CLI_V2"}:
        raise ValueError("model_connection_codex_profile_invalid")
    frozen = prepared_review.validate_contract(contract)
    connector = _connector(connector_id)
    if frozen["provider_name"] != connector.provider_name:
        raise ValueError("model_connection_mismatch")
    if connector_id == "codex_subscription":
        if runtime is None:
            raise ValueError("model_connection_runtime_required")
        if not isinstance(runtime, CliRuntime):
            raise ValueError("model_connection_runtime_mismatch")
        if codex_profile == "CODEX_JOURNAL_CLI_V2":
            return CodexPreparedReviewV2Provider(frozen, runtime=runtime, reasoning_effort=reasoning_effort, timeout_seconds=timeout_seconds)
        return CodexPreparedReviewProvider(frozen, runtime=runtime, reasoning_effort=reasoning_effort, timeout_seconds=timeout_seconds)
    if connector_id == "claude_subscription":
        if runtime is None:
            raise ValueError("model_connection_runtime_required")
        if not isinstance(runtime, ClaudeRuntime):
            raise ValueError("model_connection_runtime_mismatch")
        return ClaudePreparedReviewProvider(frozen, runtime=runtime, timeout_seconds=timeout_seconds)
    if connector_id == "antigravity_subscription":
        if runtime is None:
            raise ValueError("model_connection_runtime_required")
        if not isinstance(runtime, AntigravityRuntime):
            raise ValueError("model_connection_runtime_mismatch")
        return AntigravityPreparedReviewProvider(frozen, runtime=runtime, timeout_seconds=timeout_seconds)
    if connector_id == "grok_subscription":
        if runtime is None:
            raise ValueError("model_connection_runtime_required")
        if not isinstance(runtime, GrokRuntime):
            raise ValueError("model_connection_runtime_mismatch")
        return GrokPreparedReviewProvider(frozen, runtime=runtime, timeout_seconds=timeout_seconds)
    if connector_id == "openai_api":
        return OpenAIPreparedReviewProvider(frozen, endpoint=OPENAI_ENDPOINT, api_key=_key(connector),
            timeout_seconds=timeout_seconds, transport=transport)
    if connector_id == "xai_api":
        return XaiPreparedReviewProvider(frozen, api_key=_key(connector), timeout_seconds=timeout_seconds, transport=transport)
    if connector_id == "anthropic_api":
        return AnthropicPreparedReviewProvider(frozen, api_key=_key(connector), timeout_seconds=timeout_seconds, transport=transport)
    raise ValueError("model_connection_adapter_unavailable")


def bind_journal(contract: dict[str, Any], *, connector_id: str, runtime: CliRuntime | ClaudeRuntime | AntigravityRuntime | GrokRuntime | None = None,
                 reasoning_effort: str = "medium", timeout_seconds: float = 60,
                 transport: httpx.BaseTransport | None = None, codex_profile: str = "CODEX_JOURNAL_CLI_V1"
                 ) -> CodexJournalProvider | CodexJournalV2Provider | ClaudeJournalProvider | AntigravityJournalProvider | GrokJournalProvider | OpenAIJournalProvider | XaiJournalProvider | AnthropicJournalProvider:
    if type(codex_profile) is not str or codex_profile not in {"CODEX_JOURNAL_CLI_V1", "CODEX_JOURNAL_CLI_V2"}:
        raise ValueError("model_connection_codex_profile_invalid")
    connector = _connector(connector_id)
    if contract.get("specification", {}).get("provider_name") != connector.provider_name:
        raise ValueError("model_connection_mismatch")
    if connector_id == "codex_subscription":
        if runtime is None:
            raise ValueError("model_connection_runtime_required")
        if not isinstance(runtime, CliRuntime):
            raise ValueError("model_connection_runtime_mismatch")
        if codex_profile == "CODEX_JOURNAL_CLI_V2":
            return CodexJournalV2Provider(contract, runtime=runtime, reasoning_effort=reasoning_effort, timeout_seconds=timeout_seconds)
        return CodexJournalProvider(contract, runtime=runtime, reasoning_effort=reasoning_effort, timeout_seconds=timeout_seconds)
    if connector_id == "claude_subscription":
        if runtime is None:
            raise ValueError("model_connection_runtime_required")
        if not isinstance(runtime, ClaudeRuntime):
            raise ValueError("model_connection_runtime_mismatch")
        return ClaudeJournalProvider(contract, runtime=runtime, timeout_seconds=timeout_seconds)
    if connector_id == "antigravity_subscription":
        if runtime is None:
            raise ValueError("model_connection_runtime_required")
        if not isinstance(runtime, AntigravityRuntime):
            raise ValueError("model_connection_runtime_mismatch")
        return AntigravityJournalProvider(contract, runtime=runtime, timeout_seconds=timeout_seconds)
    if connector_id == "grok_subscription":
        if runtime is None:
            raise ValueError("model_connection_runtime_required")
        if not isinstance(runtime, GrokRuntime):
            raise ValueError("model_connection_runtime_mismatch")
        return GrokJournalProvider(contract, runtime=runtime, timeout_seconds=timeout_seconds)
    if connector_id == "openai_api":
        return OpenAIJournalProvider(contract, endpoint=OPENAI_ENDPOINT, api_key=_key(connector),
            timeout_seconds=timeout_seconds, transport=transport)
    if connector_id == "xai_api":
        return XaiJournalProvider(contract, api_key=_key(connector), timeout_seconds=timeout_seconds, transport=transport)
    if connector_id == "anthropic_api":
        return AnthropicJournalProvider(contract, api_key=_key(connector), timeout_seconds=timeout_seconds, transport=transport)
    raise ValueError("model_connection_adapter_unavailable")
