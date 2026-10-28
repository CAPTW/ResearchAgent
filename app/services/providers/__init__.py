from __future__ import annotations

from app.services.providers.base import ProviderError, ProviderRefusalError, ProviderRequest, ProviderResult, ProviderUnavailableError, StructuredOutputProvider
from app.services.providers.codex_sdk_provider import describe_codex_sdk_readiness
from app.services.providers.openai_provider import OpenAIProvider

__all__ = [
    "ProviderError",
    "ProviderRefusalError",
    "ProviderRequest",
    "ProviderResult",
    "ProviderUnavailableError",
    "StructuredOutputProvider",
    "OpenAIProvider",
    "describe_codex_sdk_readiness",
    "get_active_provider",
    "describe_provider_status",
]


def get_active_provider() -> StructuredOutputProvider | None:
    # This no-argument factory cannot bind a trusted provider, execution surface,
    # stage, output class, accepted proof identity, or proof constraints. External
    # activation therefore remains blocked until that integration exists.
    return None


def describe_provider_status() -> dict[str, object]:
    from app.services.model_connections import selected_provider_name
    from app.repositories.shared_state import SharedStateError
    try:
        provider_name = selected_provider_name()
    except (SharedStateError, OSError):
        message = "Model connection settings are unavailable; repair the saved preferences before selecting a provider."
        return dict(provider=None, provider_mode="connection_settings_unavailable", configured=False,
            live_call_enabled=False, canonicalStatus="configured_but_not_available", routingLabel="setup_required",
            researchOutputAllowed=False, reportGenerationAllowed=False, orchestrationAllowed=False,
            statusMessage=message, warnings=[message])
    if provider_name == "disabled":
        return {
            "provider": None,
            "provider_mode": "disabled",
            "configured": False,
            "live_call_enabled": False,
            "canonicalStatus": "not_supported",
            "routingLabel": "not_allowed_for_research_output",
            "researchOutputAllowed": False,
            "reportGenerationAllowed": False,
            "orchestrationAllowed": False,
            "statusMessage": "Provider disabled",
            "warnings": ["XRML_PROVIDER=disabled"],
        }
    configured = False
    if provider_name in {"openai", "xai", "anthropic"}:
        from app.services.model_credentials import CredentialUnavailable, status as credential_status
        identity = {"openai": "openai_api", "xai": "xai_api", "anthropic": "anthropic_api"}[provider_name]
        try:
            configured = credential_status(identity)["source"] != "none"
        except CredentialUnavailable:
            message = "Encrypted API key storage is unavailable; inspect local model connection settings."
            return dict(provider=provider_name, provider_mode="credentials_unavailable", configured=False,
                live_call_enabled=False, canonicalStatus="configured_but_not_available", routingLabel="setup_required",
                researchOutputAllowed=False, reportGenerationAllowed=False, orchestrationAllowed=False,
                statusMessage=message, warnings=[message])
    if provider_name == "openai":
        status_message = (
            "Provider credential detected; runtime research/report output is blocked pending trusted stage and output-class proof integration."
            if configured
            else "OpenAI provider key is missing; heuristic fallback stays active."
        )
        return {
            "provider": "openai",
            "provider_mode": "configured_no_project_live_proof" if configured else "fallback_only",
            "configured": configured,
            "live_call_enabled": False,
            "canonicalStatus": "configured_not_verified" if configured else "configured_but_not_available",
            "routingLabel": "live_proof_required" if configured else "not_allowed_for_research_output",
            "researchOutputAllowed": False,
            "reportGenerationAllowed": False,
            "orchestrationAllowed": False,
            "statusMessage": status_message,
            "model": None,
            "api_base": None,
            "warnings": [status_message],
        }
    if provider_name == "codex_cli":
        message = "Codex CLI selected; official ChatGPT login, explicit model and request-bound execution authority are required."
        return dict(provider="codex_cli", provider_mode="subscription_cli_not_verified", configured=False,
            live_call_enabled=False, canonicalStatus="configured_not_verified", routingLabel="live_proof_required",
            researchOutputAllowed=False, reportGenerationAllowed=False, orchestrationAllowed=False,
            statusMessage=message, warnings=[message])
    if provider_name == "ollama":
        message = "Ollama selected for local document planning only; runtime, model identity and local inference remain unverified. Execution is unavailable."
        return dict(provider="ollama", provider_mode="local_planning_only", configured=False,
            live_call_enabled=False, canonicalStatus="configured_not_verified", routingLabel="setup_required",
            researchOutputAllowed=False, reportGenerationAllowed=False, orchestrationAllowed=False,
            locality="LOCALITY_NOT_VERIFIED", runtime_presence="NOT_CHECKED", statusMessage=message, warnings=[message])
    if provider_name in {"claude_code", "antigravity_cli", "grok_cli", "xai", "anthropic"}:
        from app.services.model_connections import CONNECTORS
        connector = next(item for item in CONNECTORS if item.provider_name == provider_name)
        return dict(provider=provider_name, provider_mode="connection_not_verified", configured=configured,
            live_call_enabled=False, canonicalStatus="configured_not_verified", routingLabel="live_proof_required",
            researchOutputAllowed=False, reportGenerationAllowed=False, orchestrationAllowed=False,
            statusMessage=connector.availability_reason, warnings=[connector.availability_reason])
    if provider_name in {"codex_sdk", "codex_sdk_python"}:
        status = describe_codex_sdk_readiness()
        status["provider"] = "codex"
        status["provider_mode"] = "codex_sdk_scaffold_blocked"
        status["configured"] = False
        status["live_call_enabled"] = False
        status["canonicalStatus"] = "configured_not_verified"
        status["routingLabel"] = "live_proof_required"
        status["researchOutputAllowed"] = False
        status["reportGenerationAllowed"] = False
        status["orchestrationAllowed"] = False
        status["statusMessage"] = "Codex SDK provider scaffold is visible but blocked pending provider auth and exact output-class proof."
        status["warnings"] = [str(status["statusMessage"])]
        return status
    return {
        "provider": provider_name,
        "provider_mode": "unknown",
        "configured": False,
        "live_call_enabled": False,
        "canonicalStatus": "not_supported",
        "routingLabel": "not_allowed_for_research_output",
        "researchOutputAllowed": False,
        "reportGenerationAllowed": False,
        "orchestrationAllowed": False,
        "statusMessage": f"Unsupported provider: {provider_name}",
        "warnings": [f"Unsupported provider: {provider_name}"],
    }
