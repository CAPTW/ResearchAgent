"""Non-live provider backend scaffold for SDK/CLI provider-auth surfaces.

This module is a contract registry only. It does not import provider SDKs, run
provider CLIs, start OAuth/device flows, inspect credential material, or call
network APIs. Output allow/block decisions delegate to provider_orchestration.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from app.services import provider_orchestration
from app.services.providers import codex_sdk_provider

PROVIDER_AUTH_SCOPE = "provider_auth_ai_backend"
OUTPUT_CLASS_CANDIDATES: tuple[str, ...] = ("research_output", "report_draft")
PRODUCT_OUTPUT_CLASS_MAP: dict[str, str] = {
    "paper_review": "research_output",
    "weekly_report": "report_draft",
    "research_output": "research_output",
    "report_draft": "report_draft",
}
PIPELINE_STAGE_OUTPUT_CLASS: dict[str, str] = {
    "paper_collection": "research_output",
    "full_text_or_evidence_reading": "research_output",
    "paper_review": "research_output",
    "weekly_report": "report_draft",
    "topic_recommendation": "research_output",
}
PIPELINE_STAGES: tuple[str, ...] = tuple(PIPELINE_STAGE_OUTPUT_CLASS)
PRIMARY_PROVIDER_FAMILY_ORDER: tuple[str, ...] = ("antigravity", "claude", "codex")
OPENAI_API_PIPELINE_FAMILY_ID = "openai_api_pipeline"

STATUS_NOTES: tuple[str, ...] = (
    "provider_auth_is_not_app_oauth_oidc",
    "sdk_cli_oauth_is_provider_auth_only",
    "api_key_or_env_presence_is_not_output_class_proof",
    "cli_logged_in_state_is_not_output_class_proof",
    "local_mcp_is_not_public_mcp_connector_proof",
    "no_credential_cache_session_inspection_occurred",
    "no_live_provider_command_ran",
    "output_blocked_pending_exact_output_class_proof",
)


@dataclass(frozen=True)
class ProviderBackendFamily:
    provider_family_id: str
    display_name: str
    preferred_by_default: bool
    backend_role: str
    supports_main_ai_selection_candidate: bool
    supports_weighted_routing_candidate: bool
    reason_codes: tuple[str, ...]
    provider_auth_scope: str = PROVIDER_AUTH_SCOPE
    output_class_candidates: tuple[str, ...] = OUTPUT_CLASS_CANDIDATES
    proven_output_classes: tuple[str, ...] = ()
    default_blocked: bool = True
    no_secret_inspection: bool = True
    no_token_cache_inspection: bool = True
    app_oauth_oidc_in_scope: bool = False
    public_mcp_connector_in_scope: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "providerFamilyId": self.provider_family_id,
            "displayName": self.display_name,
            "preferredByDefault": self.preferred_by_default,
            "backendRole": self.backend_role,
            "providerAuthScope": self.provider_auth_scope,
            "appOauthOidcInScope": self.app_oauth_oidc_in_scope,
            "publicMcpConnectorInScope": self.public_mcp_connector_in_scope,
            "supportsMainAiSelectionCandidate": self.supports_main_ai_selection_candidate,
            "supportsWeightedRoutingCandidate": self.supports_weighted_routing_candidate,
            "outputClassCandidates": list(self.output_class_candidates),
            "provenOutputClasses": list(self.proven_output_classes),
            "defaultBlocked": self.default_blocked,
            "noSecretInspection": self.no_secret_inspection,
            "noTokenCacheInspection": self.no_token_cache_inspection,
            "reasonCodes": list(self.reason_codes),
        }


@dataclass(frozen=True)
class ProviderBackendSurface:
    surface_id: str
    provider_family_id: str
    display_name: str
    surface_type: str
    adapter_kind: str
    backend_role: str
    auth_strategy_ids: tuple[str, ...]
    status: str
    reason_codes: tuple[str, ...]
    command_name: str | None = None
    package_names: tuple[str, ...] = ()
    import_names: tuple[str, ...] = ()
    server_side_only: bool = True
    default_blocked: bool = True
    would_execute: bool = False
    may_import_sdk: bool = False
    may_run_provider_command: bool = False
    may_call_network: bool = False
    may_start_browser_login: bool = False
    may_start_device_flow: bool = False
    may_inspect_credential_cache: bool = False
    may_inspect_session_file: bool = False
    no_secret_inspection: bool = True
    no_token_cache_inspection: bool = True
    app_oauth_oidc_in_scope: bool = False
    public_mcp_connector_in_scope: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "surfaceId": self.surface_id,
            "providerFamilyId": self.provider_family_id,
            "displayName": self.display_name,
            "executionSurfaceType": self.surface_type,
            "adapterKind": self.adapter_kind,
            "backendRole": self.backend_role,
            "authStrategyIds": list(self.auth_strategy_ids),
            "status": self.status,
            "reasonCodes": list(self.reason_codes),
            "commandName": self.command_name,
            "packageNames": list(self.package_names),
            "importNames": list(self.import_names),
            "serverSideOnly": self.server_side_only,
            "defaultBlocked": self.default_blocked,
            "wouldExecute": self.would_execute,
            "mayImportSdk": self.may_import_sdk,
            "mayRunProviderCommand": self.may_run_provider_command,
            "mayCallNetwork": self.may_call_network,
            "mayStartBrowserLogin": self.may_start_browser_login,
            "mayStartDeviceFlow": self.may_start_device_flow,
            "mayInspectCredentialCache": self.may_inspect_credential_cache,
            "mayInspectSessionFile": self.may_inspect_session_file,
            "noSecretInspection": self.no_secret_inspection,
            "noTokenCacheInspection": self.no_token_cache_inspection,
            "providerAuthScope": PROVIDER_AUTH_SCOPE,
            "appOauthOidcInScope": self.app_oauth_oidc_in_scope,
            "publicMcpConnectorInScope": self.public_mcp_connector_in_scope,
        }


@dataclass(frozen=True)
class ProviderAuthStrategy:
    auth_strategy_id: str
    provider_family_id: str
    surface_id: str
    auth_kind: str
    required_env_names: tuple[str, ...]
    optional_env_names: tuple[str, ...]
    required_user_action: str
    status: str
    reason_codes: tuple[str, ...]
    may_inspect_credential_cache: bool = False
    may_inspect_session_file: bool = False
    may_start_browser_login: bool = False
    may_start_device_flow: bool = False
    may_run_provider_command: bool = False
    may_call_network: bool = False
    app_oauth_oidc_equivalent: bool = False
    public_mcp_equivalent: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "authStrategyId": self.auth_strategy_id,
            "providerFamilyId": self.provider_family_id,
            "surfaceId": self.surface_id,
            "authKind": self.auth_kind,
            "requiredEnvNames": list(self.required_env_names),
            "optionalEnvNames": list(self.optional_env_names),
            "requiredUserAction": self.required_user_action,
            "mayInspectCredentialCache": self.may_inspect_credential_cache,
            "mayInspectSessionFile": self.may_inspect_session_file,
            "mayStartBrowserLogin": self.may_start_browser_login,
            "mayStartDeviceFlow": self.may_start_device_flow,
            "mayRunProviderCommand": self.may_run_provider_command,
            "mayCallNetwork": self.may_call_network,
            "appOauthOidcEquivalent": self.app_oauth_oidc_equivalent,
            "publicMcpEquivalent": self.public_mcp_equivalent,
            "status": self.status,
            "reasonCodes": list(self.reason_codes),
            "providerAuthScope": PROVIDER_AUTH_SCOPE,
        }


@dataclass(frozen=True)
class ProviderPipelineStage:
    stage_id: str
    output_class: str
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "stageId": self.stage_id,
            "outputClass": self.output_class,
            "description": self.description,
        }


@dataclass(frozen=True)
class ProviderBackendAssignment:
    main_ai_provider_family: str
    weighted_candidates: tuple[tuple[str, int], ...]
    fallback_provider_family: str
    output_class: str
    gate_status: dict[str, Any]
    proof_required: bool
    would_execute: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "mainAiProviderFamily": self.main_ai_provider_family,
            "weightedCandidates": [
                {"providerFamilyId": family_id, "weight": weight}
                for family_id, weight in self.weighted_candidates
            ],
            "fallbackProviderFamily": self.fallback_provider_family,
            "outputClass": self.output_class,
            "gateStatus": self.gate_status,
            "proofRequired": self.proof_required,
            "wouldExecute": self.would_execute,
        }


@dataclass(frozen=True)
class ProviderApiPipelinePlan:
    requested_product_output_class: str
    internal_output_class: str
    main_ai_provider_family: str
    weighted_candidates: tuple[tuple[str, int], ...]
    fallback_provider_family: str
    stages: tuple[ProviderPipelineStage, ...]
    assignments: tuple[ProviderBackendAssignment, ...]
    api_pipeline_skeleton_only: bool = True
    would_execute: bool = False
    dry_run_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "requestedProductOutputClass": self.requested_product_output_class,
            "internalOutputClass": self.internal_output_class,
            "mainAiProviderFamily": self.main_ai_provider_family,
            "weightedCandidates": [
                {"providerFamilyId": family_id, "weight": weight}
                for family_id, weight in self.weighted_candidates
            ],
            "fallbackProviderFamily": self.fallback_provider_family,
            "apiPipelineSkeletonOnly": self.api_pipeline_skeleton_only,
            "dryRunOnly": self.dry_run_only,
            "wouldExecute": self.would_execute,
            "providerAuthScope": PROVIDER_AUTH_SCOPE,
            "appOauthOidcInScope": False,
            "publicMcpConnectorInScope": False,
            "apiSkeletonCanBypassProof": False,
            "statusNotes": list(STATUS_NOTES),
            "stages": [stage.to_dict() for stage in self.stages],
            "assignments": [assignment.to_dict() for assignment in self.assignments],
        }


def _accepted_proof_for(
    provider_family_id: str,
    output_class: str,
    accepted_output_proofs: Mapping[str, Mapping[str, str]] | None,
) -> str | None:
    return (accepted_output_proofs or {}).get(provider_family_id, {}).get(output_class)


def _gate_for_family(
    provider_family_id: str,
    output_class: str,
    accepted_output_proofs: Mapping[str, Mapping[str, str]] | None = None,
) -> provider_orchestration.ProviderOutputGate:
    proof_level = _accepted_proof_for(provider_family_id, output_class, accepted_output_proofs)
    if proof_level:
        return provider_orchestration.evaluate_provider_output_gate(
            provider_id=provider_family_id,
            output_class=output_class,
            current_proof_level=proof_level,
            proof_present=True,
            accepted=True,
            profile_status="configured_not_verified",
        )
    return provider_orchestration.evaluate_provider_output_gate(
        provider_id=provider_family_id,
        output_class=output_class,
        current_proof_level="none",
        proof_present=False,
        accepted=False,
        profile_status="configured_not_verified",
    )


def _proven_output_classes(
    provider_family_id: str,
    accepted_output_proofs: Mapping[str, Mapping[str, str]] | None = None,
) -> tuple[str, ...]:
    proven = [
        output_class
        for output_class in OUTPUT_CLASS_CANDIDATES
        if _gate_for_family(provider_family_id, output_class, accepted_output_proofs).allowed
    ]
    return tuple(proven)


def _family_registry(
    accepted_output_proofs: Mapping[str, Mapping[str, str]] | None = None,
) -> tuple[ProviderBackendFamily, ...]:
    return (
        ProviderBackendFamily(
            provider_family_id="antigravity",
            display_name="Antigravity",
            preferred_by_default=True,
            backend_role="primary_ai_backend",
            supports_main_ai_selection_candidate=True,
            supports_weighted_routing_candidate=True,
            proven_output_classes=_proven_output_classes("antigravity", accepted_output_proofs),
            reason_codes=(
                "sdk_cli_contract_only",
                "provider_auth_surface_proof_missing",
                "output_class_proof_missing",
            ),
        ),
        ProviderBackendFamily(
            provider_family_id="claude",
            display_name="Claude",
            preferred_by_default=True,
            backend_role="primary_ai_backend",
            supports_main_ai_selection_candidate=True,
            supports_weighted_routing_candidate=True,
            proven_output_classes=_proven_output_classes("claude", accepted_output_proofs),
            reason_codes=(
                "sdk_cli_contract_only",
                "provider_auth_surface_proof_missing",
                "output_class_proof_missing",
            ),
        ),
        ProviderBackendFamily(
            provider_family_id="codex",
            display_name="Codex",
            preferred_by_default=True,
            backend_role="primary_ai_backend",
            supports_main_ai_selection_candidate=True,
            supports_weighted_routing_candidate=True,
            proven_output_classes=_proven_output_classes("codex", accepted_output_proofs),
            reason_codes=(
                "codex_sdk_adapter_scaffolded",
                "blocked_pending_dependency_install",
                "blocked_pending_owner_live_approval",
                "blocked_pending_provider_auth_surface_proof",
                "output_class_proof_required",
            ),
        ),
        ProviderBackendFamily(
            provider_family_id=OPENAI_API_PIPELINE_FAMILY_ID,
            display_name="OpenAI API Pipeline",
            preferred_by_default=False,
            backend_role="api_pipeline_skeleton",
            supports_main_ai_selection_candidate=False,
            supports_weighted_routing_candidate=False,
            proven_output_classes=_proven_output_classes(OPENAI_API_PIPELINE_FAMILY_ID, accepted_output_proofs),
            reason_codes=(
                "api_pipeline_skeleton_only",
                "not_default_backend",
                "output_class_proof_missing",
            ),
        ),
    )


def _surface_registry() -> tuple[ProviderBackendSurface, ...]:
    return (
        ProviderBackendSurface(
            surface_id=codex_sdk_provider.PROFILE_ID,
            provider_family_id=codex_sdk_provider.PROVIDER_FAMILY_ID,
            display_name="Codex SDK Python Adapter Scaffold",
            surface_type=codex_sdk_provider.EXECUTION_SURFACE_TYPE,
            adapter_kind=codex_sdk_provider.ADAPTER_KIND,
            backend_role="primary_ai_backend",
            auth_strategy_ids=(
                codex_sdk_provider.AUTH_STRATEGY_PROVIDER_MANAGED,
                codex_sdk_provider.AUTH_STRATEGY_ACCESS_TOKEN,
                codex_sdk_provider.AUTH_STRATEGY_API_KEY,
                codex_sdk_provider.AUTH_STRATEGY_CHATGPT_OAUTH,
                codex_sdk_provider.AUTH_STRATEGY_UNKNOWN_BLOCKED,
            ),
            status="blocked_pending_owner_live_approval",
            reason_codes=(
                "codex_sdk_adapter_scaffolded",
                "blocked_pending_dependency_install",
                "blocked_pending_owner_live_approval",
                "blocked_pending_provider_auth_surface_proof",
                "output_class_proof_required",
            ),
            package_names=(codex_sdk_provider.SDK_PACKAGE_NAME,),
            import_names=codex_sdk_provider.SDK_IMPORT_NAME_CANDIDATES,
        ),
        ProviderBackendSurface(
            surface_id="codex_sdk_typescript",
            provider_family_id="codex",
            display_name="Codex SDK TypeScript Contract",
            surface_type="sdk",
            adapter_kind="sdk_contract",
            backend_role="primary_ai_backend",
            auth_strategy_ids=(
                "codex_chatgpt_oauth_external",
                "codex_access_token_external",
                "codex_api_key_external",
            ),
            status="available_contract_only",
            reason_codes=("sdk_package_not_verified_in_repo", "server_side_only"),
            package_names=(),
            import_names=(),
        ),
        ProviderBackendSurface(
            surface_id="codex_cli",
            provider_family_id="codex",
            display_name="Codex CLI Contract",
            surface_type="cli",
            adapter_kind="cli_contract",
            backend_role="primary_ai_backend",
            command_name="codex",
            auth_strategy_ids=(
                "codex_cli_browser_oauth_external",
                "codex_cli_device_oauth_external",
                "codex_access_token_external",
                "codex_api_key_external",
            ),
            status="available_contract_only",
            reason_codes=("cli_command_not_run", "provider_auth_surface_proof_missing"),
        ),
        ProviderBackendSurface(
            surface_id="claude_agent_sdk_python",
            provider_family_id="claude",
            display_name="Claude Agent SDK Python Contract",
            surface_type="sdk",
            adapter_kind="sdk_contract",
            backend_role="primary_ai_backend",
            auth_strategy_ids=(
                "claude_api_key_external",
                "claude_bedrock_external",
                "claude_vertex_external",
                "claude_code_existing_session_external_if_policy_allows",
            ),
            status="available_contract_only",
            reason_codes=("sdk_package_not_verified_in_repo", "provider_auth_surface_proof_missing"),
            package_names=(),
            import_names=(),
        ),
        ProviderBackendSurface(
            surface_id="claude_agent_sdk_typescript",
            provider_family_id="claude",
            display_name="Claude Agent SDK TypeScript Contract",
            surface_type="sdk",
            adapter_kind="sdk_contract",
            backend_role="primary_ai_backend",
            auth_strategy_ids=(
                "claude_api_key_external",
                "claude_bedrock_external",
                "claude_vertex_external",
            ),
            status="available_contract_only",
            reason_codes=("sdk_package_not_verified_in_repo", "server_side_only"),
            package_names=(),
            import_names=(),
        ),
        ProviderBackendSurface(
            surface_id="claude_code_cli",
            provider_family_id="claude",
            display_name="Claude Code CLI Contract",
            surface_type="cli",
            adapter_kind="cli_contract",
            backend_role="primary_ai_backend",
            command_name="claude",
            auth_strategy_ids=(
                "claude_code_cli_managed_external",
                "claude_api_key_external",
                "claude_bedrock_external",
                "claude_vertex_external",
                "claude_azure_external",
            ),
            status="blocked_pending_external_auth",
            reason_codes=("cli_command_not_run", "provider_policy_review_required"),
        ),
        ProviderBackendSurface(
            surface_id="antigravity_sdk_python",
            provider_family_id="antigravity",
            display_name="Antigravity SDK Python Contract",
            surface_type="sdk",
            adapter_kind="sdk_contract",
            backend_role="primary_ai_backend",
            auth_strategy_ids=(
                "antigravity_api_key_external",
                "antigravity_google_oauth_planned",
            ),
            status="blocked_pending_official_sdk_support",
            reason_codes=(
                "sdk_oauth_not_proven_or_not_officially_supported",
                "sdk_package_not_verified_in_repo",
            ),
            package_names=(),
            import_names=(),
        ),
        ProviderBackendSurface(
            surface_id="antigravity_cli",
            provider_family_id="antigravity",
            display_name="Antigravity CLI Contract",
            surface_type="cli",
            adapter_kind="cli_contract",
            backend_role="primary_ai_backend",
            command_name="antigravity",
            auth_strategy_ids=("antigravity_cli_managed_external", "antigravity_google_oauth_planned"),
            status="available_contract_only",
            reason_codes=("cli_command_not_run", "provider_auth_surface_proof_missing"),
        ),
        ProviderBackendSurface(
            surface_id="openai_api_pipeline",
            provider_family_id=OPENAI_API_PIPELINE_FAMILY_ID,
            display_name="OpenAI API Pipeline Skeleton",
            surface_type="api",
            adapter_kind="api_pipeline_skeleton",
            backend_role="api_pipeline_skeleton",
            auth_strategy_ids=("openai_api_key_external",),
            status="blocked_pending_owner_live_approval",
            reason_codes=("api_pipeline_skeleton_only", "not_default_backend", "openai_live_proof_deferred"),
        ),
    )


def _auth_strategy_registry() -> tuple[ProviderAuthStrategy, ...]:
    blocked_contract = ("provider_auth_surface_proof_missing", "output_class_proof_missing")
    no_command = ("no_provider_command_may_run_in_scaffold",)
    return (
        ProviderAuthStrategy(
            auth_strategy_id=codex_sdk_provider.AUTH_STRATEGY_PROVIDER_MANAGED,
            provider_family_id="codex",
            surface_id=codex_sdk_provider.PROFILE_ID,
            auth_kind="provider_managed_auth_external",
            required_env_names=(),
            optional_env_names=("XRML_CODEX_SDK_PROVIDER_MANAGED_AUTH_APPROVED",),
            required_user_action="Owner approves a future Codex SDK provider-managed auth route outside this process.",
            status="blocked_pending_external_auth",
            reason_codes=(
                "codex_sdk_provider_auth_strategy_represented_unverified",
                "provider_auth_is_not_app_oauth_oidc",
            ) + blocked_contract,
        ),
        ProviderAuthStrategy(
            auth_strategy_id=codex_sdk_provider.AUTH_STRATEGY_ACCESS_TOKEN,
            provider_family_id="codex",
            surface_id=codex_sdk_provider.PROFILE_ID,
            auth_kind="access_token_external",
            required_env_names=("CODEX_ACCESS_TOKEN",),
            optional_env_names=(),
            required_user_action="Owner injects a Codex provider access token into a future approved process environment.",
            status="blocked_pending_owner_live_approval",
            reason_codes=blocked_contract,
        ),
        ProviderAuthStrategy(
            auth_strategy_id=codex_sdk_provider.AUTH_STRATEGY_API_KEY,
            provider_family_id="codex",
            surface_id=codex_sdk_provider.PROFILE_ID,
            auth_kind="api_key_external",
            required_env_names=("CODEX_API_KEY",),
            optional_env_names=(),
            required_user_action="Owner injects a Codex provider API key into a future approved process environment.",
            status="blocked_pending_owner_live_approval",
            reason_codes=blocked_contract,
        ),
        ProviderAuthStrategy(
            auth_strategy_id=codex_sdk_provider.AUTH_STRATEGY_CHATGPT_OAUTH,
            provider_family_id="codex",
            surface_id=codex_sdk_provider.PROFILE_ID,
            auth_kind="provider_oauth_external",
            required_env_names=(),
            optional_env_names=("CODEX_AUTH_PROFILE",),
            required_user_action="Owner proves Codex ChatGPT/provider OAuth externally; this scaffold cannot inspect local state.",
            status="blocked_pending_external_auth",
            reason_codes=(
                "codex_sdk_chatgpt_oauth_external_unverified",
                "provider_auth_is_not_app_oauth_oidc",
            ) + blocked_contract,
        ),
        ProviderAuthStrategy(
            auth_strategy_id=codex_sdk_provider.AUTH_STRATEGY_UNKNOWN_BLOCKED,
            provider_family_id="codex",
            surface_id=codex_sdk_provider.PROFILE_ID,
            auth_kind="unknown_auth_blocked",
            required_env_names=(),
            optional_env_names=(),
            required_user_action="Do not infer Codex SDK auth from local caches, sessions, CODEX_HOME, or OS credential stores.",
            status="blocked_unknown_auth",
            reason_codes=(
                "codex_sdk_unknown_auth_blocked",
                "no_token_cache_inspection",
                "no_credential_path_disclosure",
            ) + blocked_contract,
        ),
        ProviderAuthStrategy(
            auth_strategy_id="codex_chatgpt_oauth_external",
            provider_family_id="codex",
            surface_id="codex_sdk_python",
            auth_kind="provider_oauth_external",
            required_env_names=(),
            optional_env_names=("CODEX_AUTH_PROFILE",),
            required_user_action="Owner supplies a separately approved Codex provider-auth surface proof.",
            status="blocked_pending_external_auth",
            reason_codes=blocked_contract,
        ),
        ProviderAuthStrategy(
            auth_strategy_id="codex_cli_browser_oauth_external",
            provider_family_id="codex",
            surface_id="codex_cli",
            auth_kind="provider_oauth_external",
            required_env_names=(),
            optional_env_names=("CODEX_AUTH_PROFILE",),
            required_user_action="Owner completes any Codex browser provider-auth outside this process.",
            status="blocked_pending_external_auth",
            reason_codes=blocked_contract + no_command,
        ),
        ProviderAuthStrategy(
            auth_strategy_id="codex_cli_device_oauth_external",
            provider_family_id="codex",
            surface_id="codex_cli",
            auth_kind="provider_oauth_external",
            required_env_names=(),
            optional_env_names=("CODEX_AUTH_PROFILE",),
            required_user_action="Owner completes any Codex device provider-auth outside this process.",
            status="blocked_pending_external_auth",
            reason_codes=blocked_contract + no_command,
        ),
        ProviderAuthStrategy(
            auth_strategy_id="codex_access_token_external",
            provider_family_id="codex",
            surface_id="codex_sdk_python",
            auth_kind="access_token_external",
            required_env_names=("CODEX_ACCESS_TOKEN",),
            optional_env_names=(),
            required_user_action="Owner injects the provider access token into the future approved process environment.",
            status="blocked_pending_owner_live_approval",
            reason_codes=blocked_contract,
        ),
        ProviderAuthStrategy(
            auth_strategy_id="codex_api_key_external",
            provider_family_id="codex",
            surface_id="codex_sdk_python",
            auth_kind="api_key_external",
            required_env_names=("OPENAI_API_KEY",),
            optional_env_names=(),
            required_user_action="Owner injects the provider API key into the future approved process environment.",
            status="blocked_pending_owner_live_approval",
            reason_codes=blocked_contract,
        ),
        ProviderAuthStrategy(
            auth_strategy_id="claude_api_key_external",
            provider_family_id="claude",
            surface_id="claude_agent_sdk_python",
            auth_kind="api_key_external",
            required_env_names=("ANTHROPIC_API_KEY",),
            optional_env_names=(),
            required_user_action="Owner injects Anthropic API credentials into a future approved process environment.",
            status="blocked_pending_owner_live_approval",
            reason_codes=blocked_contract,
        ),
        ProviderAuthStrategy(
            auth_strategy_id="claude_bedrock_external",
            provider_family_id="claude",
            surface_id="claude_agent_sdk_python",
            auth_kind="cloud_provider_external",
            required_env_names=(),
            optional_env_names=("AWS_PROFILE", "AWS_REGION"),
            required_user_action="Owner supplies cloud-provider auth outside this process and later proves the surface.",
            status="blocked_pending_external_auth",
            reason_codes=blocked_contract,
        ),
        ProviderAuthStrategy(
            auth_strategy_id="claude_vertex_external",
            provider_family_id="claude",
            surface_id="claude_agent_sdk_python",
            auth_kind="cloud_provider_external",
            required_env_names=(),
            optional_env_names=("GOOGLE_CLOUD_PROJECT", "GOOGLE_APPLICATION_CREDENTIALS"),
            required_user_action="Owner supplies cloud-provider auth outside this process and later proves the surface.",
            status="blocked_pending_external_auth",
            reason_codes=blocked_contract,
        ),
        ProviderAuthStrategy(
            auth_strategy_id="claude_azure_external",
            provider_family_id="claude",
            surface_id="claude_code_cli",
            auth_kind="cloud_provider_external",
            required_env_names=(),
            optional_env_names=("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_VERSION"),
            required_user_action="Owner supplies Azure provider auth outside this process and later proves the surface.",
            status="blocked_pending_external_auth",
            reason_codes=blocked_contract + no_command,
        ),
        ProviderAuthStrategy(
            auth_strategy_id="claude_code_existing_session_external_if_policy_allows",
            provider_family_id="claude",
            surface_id="claude_agent_sdk_python",
            auth_kind="cli_managed_external",
            required_env_names=(),
            optional_env_names=("CLAUDE_CODE_PROVIDER_PROFILE",),
            required_user_action="Owner supplies a policy-approved Claude Code provider-auth surface proof without exposing files.",
            status="blocked_pending_external_auth",
            reason_codes=("provider_policy_review_required",) + blocked_contract,
        ),
        ProviderAuthStrategy(
            auth_strategy_id="claude_code_cli_managed_external",
            provider_family_id="claude",
            surface_id="claude_code_cli",
            auth_kind="cli_managed_external",
            required_env_names=(),
            optional_env_names=("CLAUDE_CODE_PROVIDER_PROFILE",),
            required_user_action="Owner proves Claude Code CLI auth externally; this scaffold cannot inspect local state.",
            status="blocked_pending_external_auth",
            reason_codes=("provider_policy_review_required",) + blocked_contract + no_command,
        ),
        ProviderAuthStrategy(
            auth_strategy_id="antigravity_api_key_external",
            provider_family_id="antigravity",
            surface_id="antigravity_sdk_python",
            auth_kind="api_key_external",
            required_env_names=("ANTIGRAVITY_API_KEY",),
            optional_env_names=(),
            required_user_action="Owner injects Antigravity provider credentials into a future approved process environment.",
            status="blocked_pending_owner_live_approval",
            reason_codes=blocked_contract,
        ),
        ProviderAuthStrategy(
            auth_strategy_id="antigravity_google_oauth_planned",
            provider_family_id="antigravity",
            surface_id="antigravity_sdk_python",
            auth_kind="planned_or_unsupported",
            required_env_names=(),
            optional_env_names=("GOOGLE_CLOUD_PROJECT",),
            required_user_action="Owner supplies official SDK/CLI OAuth support evidence before this becomes executable.",
            status="blocked_pending_official_sdk_support",
            reason_codes=("sdk_oauth_not_proven_or_not_officially_supported",) + blocked_contract,
        ),
        ProviderAuthStrategy(
            auth_strategy_id="antigravity_cli_managed_external",
            provider_family_id="antigravity",
            surface_id="antigravity_cli",
            auth_kind="cli_managed_external",
            required_env_names=(),
            optional_env_names=("ANTIGRAVITY_PROVIDER_PROFILE",),
            required_user_action="Owner proves Antigravity CLI auth externally; this scaffold cannot inspect local state.",
            status="blocked_pending_external_auth",
            reason_codes=blocked_contract + no_command,
        ),
        ProviderAuthStrategy(
            auth_strategy_id="openai_api_key_external",
            provider_family_id=OPENAI_API_PIPELINE_FAMILY_ID,
            surface_id="openai_api_pipeline",
            auth_kind="api_key_external",
            required_env_names=("OPENAI_API_KEY",),
            optional_env_names=("OPENAI_MODEL", "OPENAI_API_BASE"),
            required_user_action="Owner may later re-enable the OpenAI API lane with an explicit live proof scope.",
            status="blocked_pending_owner_live_approval",
            reason_codes=("api_pipeline_skeleton_only", "openai_live_proof_deferred", "output_class_proof_missing"),
        ),
    )


def _internal_output_class(output_class: str) -> str:
    return PRODUCT_OUTPUT_CLASS_MAP.get(output_class, output_class)


def _family_by_id(
    accepted_output_proofs: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, ProviderBackendFamily]:
    return {family.provider_family_id: family for family in _family_registry(accepted_output_proofs)}


def _normalize_weights(weights: Mapping[str, int | float] | Sequence[tuple[str, int | float]] | None = None) -> tuple[tuple[str, int], ...]:
    if weights is None:
        return tuple((family_id, 1) for family_id in PRIMARY_PROVIDER_FAMILY_ORDER)
    items = weights.items() if isinstance(weights, Mapping) else weights
    normalized: list[tuple[str, int]] = []
    seen: set[str] = set()
    for provider_family_id, raw_weight in items:
        family_id = str(provider_family_id)
        if family_id not in PRIMARY_PROVIDER_FAMILY_ORDER or family_id in seen:
            continue
        seen.add(family_id)
        if isinstance(raw_weight, bool) or not isinstance(raw_weight, (int, float)):
            continue
        if raw_weight <= 0:
            continue
        normalized.append((family_id, int(raw_weight)))
    return tuple(normalized or ((family_id, 1) for family_id in PRIMARY_PROVIDER_FAMILY_ORDER))


def describe_provider_backend_families(
    *,
    accepted_output_proofs: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    families = [family.to_dict() for family in _family_registry(accepted_output_proofs)]
    return {
        "nonLive": True,
        "wouldExecute": False,
        "providerAuthScope": PROVIDER_AUTH_SCOPE,
        "appOauthOidcInScope": False,
        "publicMcpConnectorInScope": False,
        "preferredProviderFamilyOrder": list(PRIMARY_PROVIDER_FAMILY_ORDER),
        "openAiApiPipelineOnly": True,
        "statusNotes": list(STATUS_NOTES),
        "families": families,
    }


def describe_provider_sdk_oauth_surfaces() -> dict[str, Any]:
    return {
        "nonLive": True,
        "wouldExecute": False,
        "mayImportSdk": False,
        "mayRunProviderCommand": False,
        "mayCallNetwork": False,
        "mayStartBrowserLogin": False,
        "mayStartDeviceFlow": False,
        "providerAuthScope": PROVIDER_AUTH_SCOPE,
        "appOauthOidcInScope": False,
        "publicMcpConnectorInScope": False,
        "statusNotes": list(STATUS_NOTES),
        "surfaces": [surface.to_dict() for surface in _surface_registry()],
    }


def describe_provider_auth_strategies() -> dict[str, Any]:
    return {
        "nonLive": True,
        "wouldExecute": False,
        "providerAuthScope": PROVIDER_AUTH_SCOPE,
        "appOauthOidcEquivalent": False,
        "publicMcpEquivalent": False,
        "mayInspectCredentialCache": False,
        "mayInspectSessionFile": False,
        "mayStartBrowserLogin": False,
        "mayStartDeviceFlow": False,
        "mayRunProviderCommand": False,
        "mayCallNetwork": False,
        "statusNotes": list(STATUS_NOTES),
        "strategies": [strategy.to_dict() for strategy in _auth_strategy_registry()],
    }


def describe_codex_sdk_provider_adapter(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return the Codex SDK adapter scaffold status without importing the SDK."""
    return codex_sdk_provider.describe_codex_sdk_readiness(env)


def build_codex_sdk_provider_execution_plan(
    task_kind: str,
    output_class: str | None = None,
    *,
    live: bool = False,
) -> dict[str, Any]:
    """Build a blocked future Codex SDK execution plan."""
    return codex_sdk_provider.build_codex_sdk_execution_plan(
        task_kind=task_kind,
        output_class=output_class,
        live=live,
    )


def select_main_ai_backend_candidate(
    preferred_family_id: str | None = None,
    *,
    output_class: str = "paper_review",
    accepted_output_proofs: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    family_id = preferred_family_id or PRIMARY_PROVIDER_FAMILY_ORDER[0]
    families = _family_by_id(accepted_output_proofs)
    family = families.get(family_id)
    internal_output_class = _internal_output_class(output_class)
    if family is None:
        return {
            "selected": False,
            "providerFamilyId": family_id,
            "blockedReason": "unknown_provider_family",
            "wouldExecute": False,
            "outputAllowed": False,
            "statusNotes": list(STATUS_NOTES),
        }
    gate = _gate_for_family(family_id, internal_output_class, accepted_output_proofs)
    selection_allowed = family.supports_main_ai_selection_candidate and gate.allowed
    blocked_reason = None
    if not family.supports_main_ai_selection_candidate:
        blocked_reason = "provider_family_not_main_ai_candidate"
    elif not gate.allowed:
        blocked_reason = gate.blocked_reason
    return {
        "selected": True,
        "providerFamilyId": family_id,
        "displayName": family.display_name,
        "backendRole": family.backend_role,
        "requestedProductOutputClass": output_class,
        "internalOutputClass": internal_output_class,
        "selectionAllowed": selection_allowed,
        "outputAllowed": gate.allowed,
        "blockedReason": blocked_reason,
        "gateStatus": gate.to_dict(),
        "wouldExecute": False,
        "providerAuthScope": PROVIDER_AUTH_SCOPE,
        "appOauthOidcInScope": False,
        "publicMcpConnectorInScope": False,
        "statusNotes": list(STATUS_NOTES),
    }


def plan_weighted_backend_routing(
    weights: Mapping[str, int | float] | Sequence[tuple[str, int | float]] | None = None,
    *,
    output_class: str = "paper_review",
    request_fingerprint: str = "",
    accepted_output_proofs: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    normalized = _normalize_weights(weights)
    internal_output_class = _internal_output_class(output_class)
    selected = provider_orchestration.select_weighted_provider_preview(
        normalized,
        stage="provider_backend_scaffold",
        request_fingerprint=request_fingerprint,
    )
    decision_matrix = []
    for family_id, weight in normalized:
        gate = _gate_for_family(family_id, internal_output_class, accepted_output_proofs)
        decision_matrix.append(
            {
                "providerFamilyId": family_id,
                "weight": weight,
                "outputAllowed": gate.allowed,
                "blockedReason": gate.blocked_reason,
                "currentProofLevel": gate.current_proof_level,
                "requiredProofLevel": gate.required_proof_level,
                "wouldExecute": False,
            }
        )
    selected_gate = _gate_for_family(selected or "", internal_output_class, accepted_output_proofs)
    return {
        "nonLive": True,
        "dryRunOnly": True,
        "wouldExecute": False,
        "routingMode": "weighted_round_robin",
        "requestedProductOutputClass": output_class,
        "internalOutputClass": internal_output_class,
        "selectedProviderFamilyId": selected,
        "weightedCandidates": [
            {"providerFamilyId": family_id, "weight": weight}
            for family_id, weight in normalized
        ],
        "allowed": selected_gate.allowed,
        "blockedReason": selected_gate.blocked_reason,
        "gateStatus": selected_gate.to_dict(),
        "providerDecisionMatrix": decision_matrix,
        "allOutputBlocked": all(not row["outputAllowed"] for row in decision_matrix),
        "providerAuthScope": PROVIDER_AUTH_SCOPE,
        "appOauthOidcInScope": False,
        "publicMcpConnectorInScope": False,
        "statusNotes": list(STATUS_NOTES),
    }


def plan_provider_backend_pipeline(
    *,
    main_ai_provider_family: str | None = None,
    weights: Mapping[str, int | float] | Sequence[tuple[str, int | float]] | None = None,
    output_class: str = "paper_review",
    accepted_output_proofs: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    main_family = main_ai_provider_family or PRIMARY_PROVIDER_FAMILY_ORDER[0]
    normalized = _normalize_weights(weights)
    internal_output_class = _internal_output_class(output_class)
    stages = tuple(
        ProviderPipelineStage(
            stage_id=stage_id,
            output_class=stage_output_class,
            description=f"{stage_id} provider-backend assignment preview",
        )
        for stage_id, stage_output_class in PIPELINE_STAGE_OUTPUT_CLASS.items()
    )
    assignments = []
    for stage in stages:
        gate = _gate_for_family(main_family, stage.output_class, accepted_output_proofs)
        assignments.append(
            ProviderBackendAssignment(
                main_ai_provider_family=main_family,
                weighted_candidates=normalized,
                fallback_provider_family=OPENAI_API_PIPELINE_FAMILY_ID,
                output_class=stage.output_class,
                gate_status=gate.to_dict(),
                proof_required=not gate.allowed,
                would_execute=False,
            )
        )
    plan = ProviderApiPipelinePlan(
        requested_product_output_class=output_class,
        internal_output_class=internal_output_class,
        main_ai_provider_family=main_family,
        weighted_candidates=normalized,
        fallback_provider_family=OPENAI_API_PIPELINE_FAMILY_ID,
        stages=stages,
        assignments=tuple(assignments),
    )
    payload = plan.to_dict()
    payload["mainAiCandidate"] = select_main_ai_backend_candidate(
        main_family,
        output_class=output_class,
        accepted_output_proofs=accepted_output_proofs,
    )
    payload["weightedRouting"] = plan_weighted_backend_routing(
        normalized,
        output_class=output_class,
        request_fingerprint=f"pipeline:{output_class}:{main_family}",
        accepted_output_proofs=accepted_output_proofs,
    )
    return payload


TINY_PROOF_APPROVAL_ENV_CONTRACT: tuple[dict[str, Any], ...] = (
    {
        "envName": "XRML_PROVIDER_TINY_PROOF_APPROVED",
        "expectedValue": "1",
        "secret": False,
        "requiredForFutureLiveTinyProof": True,
    },
    {
        "envName": "XRML_PROVIDER_TINY_PROOF_PROVIDER",
        "allowedValues": ["codex", "claude", "antigravity"],
        "secret": False,
        "requiredForFutureLiveTinyProof": True,
    },
    {
        "envName": "XRML_PROVIDER_TINY_PROOF_SURFACE",
        "allowedValues": [
            "codex_sdk_python",
            "codex_cli",
            "claude_agent_sdk_python",
            "claude_code_cli",
            "antigravity_sdk_python",
            "antigravity_cli",
        ],
        "secret": False,
        "requiredForFutureLiveTinyProof": True,
    },
    {
        "envName": "XRML_PROVIDER_TINY_PROOF_MODE",
        "expectedValue": "tiny_live",
        "secret": False,
        "requiredForFutureLiveTinyProof": True,
    },
    {
        "envName": "XRML_PROVIDER_TINY_PROOF_MAX_CALLS",
        "expectedValue": "1",
        "secret": False,
        "requiredForFutureLiveTinyProof": True,
    },
    {
        "envName": "XRML_PROVIDER_TINY_PROOF_ARTIFACT",
        "valuePolicy": "ignored_generated_artifact_path_only",
        "secret": False,
        "requiredForFutureLiveTinyProof": True,
    },
    {
        "envName": "XRML_PROVIDER_TINY_PROOF_PROMPT_CLASS",
        "expectedValue": "sentinel_only",
        "secret": False,
        "requiredForFutureLiveTinyProof": True,
    },
)
TINY_PROOF_APPROVAL_ENV_NAMES: tuple[str, ...] = tuple(
    item["envName"] for item in TINY_PROOF_APPROVAL_ENV_CONTRACT
)


@dataclass(frozen=True)
class ProviderTinyProofAuthRequirement:
    auth_strategy_id: str
    provider_family_id: str
    surface_id: str
    auth_kind: str
    official_support_state: str
    required_env_name_candidates: tuple[str, ...]
    optional_env_name_candidates: tuple[str, ...]
    status: str
    reason_codes: tuple[str, ...]
    may_inspect_credential_cache: bool = False
    may_inspect_session_file: bool = False
    may_start_browser_login: bool = False
    may_start_device_flow: bool = False
    may_run_provider_command: bool = False
    may_call_network: bool = False
    app_oauth_oidc_equivalent: bool = False
    public_mcp_equivalent: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "authStrategyId": self.auth_strategy_id,
            "providerFamilyId": self.provider_family_id,
            "surfaceId": self.surface_id,
            "authKind": self.auth_kind,
            "officialSupportState": self.official_support_state,
            "requiredEnvNameCandidates": list(self.required_env_name_candidates),
            "optionalEnvNameCandidates": list(self.optional_env_name_candidates),
            "status": self.status,
            "reasonCodes": list(self.reason_codes),
            "mayInspectCredentialCache": self.may_inspect_credential_cache,
            "mayInspectSessionFile": self.may_inspect_session_file,
            "mayStartBrowserLogin": self.may_start_browser_login,
            "mayStartDeviceFlow": self.may_start_device_flow,
            "mayRunProviderCommand": self.may_run_provider_command,
            "mayCallNetwork": self.may_call_network,
            "appOauthOidcEquivalent": self.app_oauth_oidc_equivalent,
            "publicMcpEquivalent": self.public_mcp_equivalent,
            "providerAuthScope": PROVIDER_AUTH_SCOPE,
        }


@dataclass(frozen=True)
class ProviderTinyProofSurfaceCandidate:
    provider_family_id: str
    surface_id: str
    execution_surface_type: str
    adapter_kind: str
    tiny_proof_kind: str
    official_support_state: str
    auth_requirements: tuple[ProviderTinyProofAuthRequirement, ...]
    future_stop_reason_codes: tuple[str, ...]
    policy_notes: tuple[str, ...]
    recommendation_score: int
    recommendation_factors: tuple[tuple[str, str], ...]
    reason_codes: tuple[str, ...]
    max_live_calls_for_future_proof: int = 1
    requires_owner_approval: bool = True
    requires_process_env_only: bool = True
    output_class_proof_in_scope: bool = False
    may_run_now: bool = False
    would_execute_provider: bool = False
    default_blocked: bool = True
    no_secret_inspection: bool = True
    no_token_cache_inspection: bool = True
    no_credential_path_disclosure: bool = True
    no_browser_login_in_this_task: bool = True
    no_device_flow_in_this_task: bool = True
    no_provider_command_in_this_task: bool = True
    no_sdk_import_in_this_task: bool = True
    app_oauth_oidc_in_scope: bool = False
    public_mcp_connector_in_scope: bool = False

    def provider_auth_env_name_candidates(self) -> tuple[str, ...]:
        names: list[str] = []
        for requirement in self.auth_requirements:
            for name in (*requirement.required_env_name_candidates, *requirement.optional_env_name_candidates):
                if name not in names:
                    names.append(name)
        return tuple(names)

    def to_dict(self) -> dict[str, Any]:
        return {
            "providerFamilyId": self.provider_family_id,
            "surfaceId": self.surface_id,
            "executionSurfaceType": self.execution_surface_type,
            "adapterKind": self.adapter_kind,
            "tinyProofKind": self.tiny_proof_kind,
            "officialSupportState": self.official_support_state,
            "providerAuthScope": PROVIDER_AUTH_SCOPE,
            "appOauthOidcInScope": self.app_oauth_oidc_in_scope,
            "publicMcpConnectorInScope": self.public_mcp_connector_in_scope,
            "outputClassProofInScope": self.output_class_proof_in_scope,
            "mayRunNow": self.may_run_now,
            "wouldExecuteProvider": self.would_execute_provider,
            "maxLiveCallsForFutureProof": self.max_live_calls_for_future_proof,
            "requiresOwnerApproval": self.requires_owner_approval,
            "requiresProcessEnvOnly": self.requires_process_env_only,
            "noSecretInspection": self.no_secret_inspection,
            "noTokenCacheInspection": self.no_token_cache_inspection,
            "noCredentialPathDisclosure": self.no_credential_path_disclosure,
            "noBrowserLoginInThisTask": self.no_browser_login_in_this_task,
            "noDeviceFlowInThisTask": self.no_device_flow_in_this_task,
            "noProviderCommandInThisTask": self.no_provider_command_in_this_task,
            "noSdkImportInThisTask": self.no_sdk_import_in_this_task,
            "defaultBlocked": self.default_blocked,
            "authRequirements": [requirement.to_dict() for requirement in self.auth_requirements],
            "providerAuthEnvNameCandidates": list(self.provider_auth_env_name_candidates()),
            "futureApprovalEnvNames": list(TINY_PROOF_APPROVAL_ENV_NAMES),
            "futureStopReasonCodes": list(self.future_stop_reason_codes),
            "policyNotes": list(self.policy_notes),
            "recommendationScore": self.recommendation_score,
            "recommendationFactors": dict(self.recommendation_factors),
            "reasonCodes": list(self.reason_codes),
            "postTinyProofOutputGates": _tiny_proof_output_gate_summary(self.provider_family_id),
        }


@dataclass(frozen=True)
class ProviderTinyProofArtifactContract:
    artifact_kind: str = "provider_tiny_proof"
    proof_mode: str = "tiny_live"
    proof_status_candidate: str = "live_verified_tiny"
    prompt_class: str = "sentinel_only"
    input_size_class: str = "tiny"
    provider_call_count: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifactKind": self.artifact_kind,
            "requiredFields": [
                "artifactKind",
                "providerFamilyId",
                "providerId",
                "profileId",
                "surfaceId",
                "executionSurfaceType",
                "adapterKind",
                "authStrategyId",
                "proofMode",
                "futureLiveApprovalEnvObserved",
                "futureProviderAuthEnvObserved",
                "promptClass",
                "inputSizeClass",
                "providerCallCount",
                "acceptedTinyProof",
                "outputClassProof",
                "researchOutputAllowed",
                "reportDraftAllowed",
                "cannotUnlockOutputClass",
                "proofStatusCandidate",
                "reasonCodes",
                "noSecretInspection",
                "noTokenCacheInspection",
                "noCredentialPathDisclosure",
                "appOauthOidcInScope",
                "publicMcpConnectorInScope",
                "notInScope",
                "exactCommandOrSdkCallCount",
                "no_chain_of_thought",
                "redactionStatus",
            ],
            "fixedValues": {
                "artifactKind": self.artifact_kind,
                "proofMode": self.proof_mode,
                "promptClass": self.prompt_class,
                "inputSizeClass": self.input_size_class,
                "providerCallCount": self.provider_call_count,
                "acceptedTinyProof": "boolean_future_value",
                "outputClassProof": False,
                "researchOutputAllowed": False,
                "reportDraftAllowed": False,
                "cannotUnlockOutputClass": True,
                "proofStatusCandidate": self.proof_status_candidate,
                "noSecretInspection": True,
                "noTokenCacheInspection": True,
                "noCredentialPathDisclosure": True,
                "appOauthOidcInScope": False,
                "publicMcpConnectorInScope": False,
                "notInScope": [
                    "app_oauth_oidc_not_in_scope",
                    "public_mcp_not_in_scope",
                    "output_class_proof_not_in_scope",
                ],
                "exactCommandOrSdkCallCount": 1,
                "no_chain_of_thought": True,
                "redactionStatus": "required_clean_or_fail_closed",
            },
            "forbiddenFieldsOrContent": [
                "api keys",
                "tokens",
                "credential/cache/session file contents",
                "browser auth values",
                "raw env secret values",
                "actual env file contents",
                "actual MCP config contents",
                "local absolute auth cache paths",
                "provider credential paths",
                "broad logs",
                "chain-of-thought",
                "paper review content",
                "report content",
            ],
            "cannotUnlockOutputClass": True,
            "researchOutputAllowed": False,
            "reportDraftAllowed": False,
            "providerAuthScope": PROVIDER_AUTH_SCOPE,
            "appOauthOidcInScope": False,
            "publicMcpConnectorInScope": False,
        }


@dataclass(frozen=True)
class ProviderTinyProofPlan:
    candidate: ProviderTinyProofSurfaceCandidate | None
    future_live_approval_env_observed: dict[str, bool]
    future_provider_auth_env_observed: dict[str, bool]
    reason_codes: tuple[str, ...]
    recommendation_status: str

    def to_dict(self) -> dict[str, Any]:
        candidate_payload = self.candidate.to_dict() if self.candidate else None
        return {
            "nonLive": True,
            "mayRunNow": False,
            "wouldExecuteProvider": False,
            "requiresOwnerApproval": True,
            "requiresProcessEnvOnly": True,
            "providerAuthScope": PROVIDER_AUTH_SCOPE,
            "appOauthOidcInScope": False,
            "publicMcpConnectorInScope": False,
            "outputClassProofInScope": False,
            "futureLiveApprovalEnvObserved": self.future_live_approval_env_observed,
            "futureProviderAuthEnvObserved": self.future_provider_auth_env_observed,
            "candidate": candidate_payload,
            "artifactContract": describe_provider_tiny_proof_artifact_contract(),
            "promptContract": describe_provider_tiny_proof_prompt_contract(),
            "postTinyProofOutputGates": _tiny_proof_output_gate_summary(
                self.candidate.provider_family_id if self.candidate else "none"
            ),
            "recommendationStatus": self.recommendation_status,
            "reasonCodes": list(self.reason_codes),
            "statusNotes": list(STATUS_NOTES),
        }


def _tiny_auth_requirement(
    *,
    auth_strategy_id: str,
    provider_family_id: str,
    surface_id: str,
    auth_kind: str,
    official_support_state: str,
    required_env_name_candidates: tuple[str, ...] = (),
    optional_env_name_candidates: tuple[str, ...] = (),
    status: str = "blocked_pending_external_auth",
    reason_codes: tuple[str, ...] = (),
) -> ProviderTinyProofAuthRequirement:
    base_reasons = (
        "provider_auth_is_not_app_oauth_oidc",
        "provider_auth_surface_proof_missing",
        "tiny_proof_not_output_class_proof",
    )
    return ProviderTinyProofAuthRequirement(
        auth_strategy_id=auth_strategy_id,
        provider_family_id=provider_family_id,
        surface_id=surface_id,
        auth_kind=auth_kind,
        official_support_state=official_support_state,
        required_env_name_candidates=required_env_name_candidates,
        optional_env_name_candidates=optional_env_name_candidates,
        status=status,
        reason_codes=tuple(dict.fromkeys((*base_reasons, *reason_codes))),
    )


def _tiny_proof_candidates() -> tuple[ProviderTinyProofSurfaceCandidate, ...]:
    common_reasons = (
        "non_live_precondition_matrix_only",
        "future_owner_approval_required",
        "provider_auth_surface_proof_missing",
        "tiny_proof_cannot_unlock_output_class",
    )
    return (
        ProviderTinyProofSurfaceCandidate(
            provider_family_id="codex",
            surface_id="codex_cli",
            execution_surface_type="cli",
            adapter_kind="cli_contract",
            tiny_proof_kind="cli_sentinel_prompt",
            official_support_state="contract_only_pending_doc_confirmation",
            auth_requirements=(
                _tiny_auth_requirement(
                    auth_strategy_id="codex_access_token_external",
                    provider_family_id="codex",
                    surface_id="codex_cli",
                    auth_kind="access_token_external",
                    official_support_state="contract_only_pending_doc_confirmation",
                    required_env_name_candidates=("CODEX_ACCESS_TOKEN",),
                    status="blocked_pending_owner_live_approval",
                ),
                _tiny_auth_requirement(
                    auth_strategy_id="api_key_external",
                    provider_family_id="codex",
                    surface_id="codex_cli",
                    auth_kind="api_key_external",
                    official_support_state="contract_only_pending_doc_confirmation",
                    required_env_name_candidates=("OPENAI_API_KEY",),
                    status="blocked_pending_owner_live_approval",
                ),
                _tiny_auth_requirement(
                    auth_strategy_id="chatgpt_oauth_external",
                    provider_family_id="codex",
                    surface_id="codex_cli",
                    auth_kind="provider_oauth_external",
                    official_support_state="planned_or_external_only",
                    optional_env_name_candidates=("CODEX_AUTH_PROFILE",),
                    reason_codes=("would_require_codex_auth_cache_inspection_if_not_external",),
                ),
            ),
            future_stop_reason_codes=(
                "CODEX_PROVIDER_AUTH_NOT_AVAILABLE",
                "WOULD_REQUIRE_CODEX_AUTH_CACHE_INSPECTION",
                "WOULD_REQUIRE_INTERACTIVE_LOGIN",
                "WOULD_REQUIRE_MORE_THAN_ONE_LIVE_CALL",
            ),
            policy_notes=("Existing repo contract has the narrowest non-interactive CLI prompt shape.",),
            recommendation_score=84,
            recommendation_factors=(
                ("officialSupportConfidence", "contract_pending_doc_confirmation"),
                ("nonInteractiveFeasibility", "highest_project_contract_confidence"),
                ("credentialCacheInspectionRequirement", "none_allowed"),
                ("browserDeviceLoginRequirement", "not_allowed_during_proof"),
                ("oneCallFeasibility", "high_if_cli_contract_is_confirmed"),
                ("projectOwnerPriority", "primary_backend_family"),
                ("policyRisk", "medium"),
                ("implementationBlastRadius", "low"),
            ),
            reason_codes=common_reasons + ("recommended_first_candidate_when_no_surface_is_unsafe",),
        ),
        ProviderTinyProofSurfaceCandidate(
            provider_family_id="codex",
            surface_id="codex_sdk_python",
            execution_surface_type="sdk",
            adapter_kind=codex_sdk_provider.ADAPTER_KIND,
            tiny_proof_kind="sdk_sentinel_prompt",
            official_support_state="contract_only_pending_doc_confirmation",
            auth_requirements=(
                _tiny_auth_requirement(
                    auth_strategy_id=codex_sdk_provider.AUTH_STRATEGY_ACCESS_TOKEN,
                    provider_family_id="codex",
                    surface_id="codex_sdk_python",
                    auth_kind="access_token_external",
                    official_support_state="contract_only_pending_doc_confirmation",
                    required_env_name_candidates=("CODEX_ACCESS_TOKEN",),
                    status="blocked_pending_owner_live_approval",
                ),
                _tiny_auth_requirement(
                    auth_strategy_id=codex_sdk_provider.AUTH_STRATEGY_API_KEY,
                    provider_family_id="codex",
                    surface_id="codex_sdk_python",
                    auth_kind="api_key_external",
                    official_support_state="contract_only_pending_doc_confirmation",
                    required_env_name_candidates=("CODEX_API_KEY",),
                    status="blocked_pending_owner_live_approval",
                ),
                _tiny_auth_requirement(
                    auth_strategy_id=codex_sdk_provider.AUTH_STRATEGY_PROVIDER_MANAGED,
                    provider_family_id="codex",
                    surface_id="codex_sdk_python",
                    auth_kind="provider_managed_auth_external",
                    official_support_state="planned_or_external_only",
                    optional_env_name_candidates=("XRML_CODEX_SDK_PROVIDER_MANAGED_AUTH_APPROVED",),
                ),
                _tiny_auth_requirement(
                    auth_strategy_id=codex_sdk_provider.AUTH_STRATEGY_CHATGPT_OAUTH,
                    provider_family_id="codex",
                    surface_id="codex_sdk_python",
                    auth_kind="provider_oauth_external",
                    official_support_state="planned_or_external_only",
                    optional_env_name_candidates=("CODEX_AUTH_PROFILE",),
                ),
            ),
            future_stop_reason_codes=(
                "CODEX_SDK_NOT_INSTALLED",
                "CODEX_PROVIDER_AUTH_NOT_AVAILABLE",
                "WOULD_REQUIRE_CODEX_AUTH_CACHE_INSPECTION",
                "WOULD_START_CODEX_APPSERVER_WITHOUT_APPROVAL",
            ),
            policy_notes=("SDK package/import support is metadata-only until a docs-confirmation task.",),
            recommendation_score=70,
            recommendation_factors=(
                ("officialSupportConfidence", "contract_pending_doc_confirmation"),
                ("nonInteractiveFeasibility", "medium"),
                ("credentialCacheInspectionRequirement", "none_allowed"),
                ("browserDeviceLoginRequirement", "not_allowed_during_proof"),
                ("oneCallFeasibility", "medium_pending_sdk_contract"),
                ("projectOwnerPriority", "primary_backend_family"),
                ("policyRisk", "medium"),
                ("implementationBlastRadius", "medium"),
            ),
            reason_codes=common_reasons,
        ),
        ProviderTinyProofSurfaceCandidate(
            provider_family_id="claude",
            surface_id="claude_agent_sdk_python",
            execution_surface_type="sdk",
            adapter_kind="sdk_contract",
            tiny_proof_kind="sdk_sentinel_prompt",
            official_support_state="contract_only_pending_doc_confirmation",
            auth_requirements=(
                _tiny_auth_requirement(
                    auth_strategy_id="anthropic_api_key_external",
                    provider_family_id="claude",
                    surface_id="claude_agent_sdk_python",
                    auth_kind="api_key_external",
                    official_support_state="contract_only_pending_doc_confirmation",
                    required_env_name_candidates=("ANTHROPIC_API_KEY",),
                    status="blocked_pending_owner_live_approval",
                ),
                _tiny_auth_requirement(
                    auth_strategy_id="anthropic_auth_token_external",
                    provider_family_id="claude",
                    surface_id="claude_agent_sdk_python",
                    auth_kind="access_token_external",
                    official_support_state="planned_or_external_only",
                    required_env_name_candidates=("ANTHROPIC_AUTH_TOKEN",),
                    status="blocked_pending_owner_live_approval",
                ),
                _tiny_auth_requirement(
                    auth_strategy_id="bedrock_external",
                    provider_family_id="claude",
                    surface_id="claude_agent_sdk_python",
                    auth_kind="cloud_provider_external",
                    official_support_state="contract_only_pending_doc_confirmation",
                    optional_env_name_candidates=("CLAUDE_CODE_USE_BEDROCK", "AWS_PROFILE", "AWS_REGION"),
                ),
                _tiny_auth_requirement(
                    auth_strategy_id="vertex_external",
                    provider_family_id="claude",
                    surface_id="claude_agent_sdk_python",
                    auth_kind="cloud_provider_external",
                    official_support_state="contract_only_pending_doc_confirmation",
                    optional_env_name_candidates=(
                        "CLAUDE_CODE_USE_VERTEX",
                        "GOOGLE_CLOUD_PROJECT",
                        "ANTHROPIC_VERTEX_PROJECT_ID",
                    ),
                ),
                _tiny_auth_requirement(
                    auth_strategy_id="foundry_external",
                    provider_family_id="claude",
                    surface_id="claude_agent_sdk_python",
                    auth_kind="cloud_provider_external",
                    official_support_state="contract_only_pending_doc_confirmation",
                    optional_env_name_candidates=("CLAUDE_CODE_USE_FOUNDRY", "FOUNDRY_WORKSPACE_ID"),
                ),
            ),
            future_stop_reason_codes=(
                "CLAUDE_SDK_NOT_INSTALLED",
                "CLAUDE_PROVIDER_AUTH_NOT_AVAILABLE",
                "CLAUDE_AI_LOGIN_POLICY_NOT_APPROVED",
                "WOULD_REQUIRE_CLAUDE_CREDENTIAL_FILE_INSPECTION",
            ),
            policy_notes=(
                "Prefer API key or cloud-provider auth for product integration.",
                "Do not offer claude.ai login/rate limits unless owner/legal explicitly approves.",
            ),
            recommendation_score=76,
            recommendation_factors=(
                ("officialSupportConfidence", "contract_pending_doc_confirmation"),
                ("nonInteractiveFeasibility", "high_with_api_key_or_cloud_provider"),
                ("credentialCacheInspectionRequirement", "none_allowed"),
                ("browserDeviceLoginRequirement", "not_allowed_during_proof"),
                ("oneCallFeasibility", "high_if_sdk_contract_is_confirmed"),
                ("projectOwnerPriority", "primary_backend_family"),
                ("policyRisk", "medium_low_without_claude_ai_login"),
                ("implementationBlastRadius", "medium"),
            ),
            reason_codes=common_reasons,
        ),
        ProviderTinyProofSurfaceCandidate(
            provider_family_id="claude",
            surface_id="claude_code_cli",
            execution_surface_type="cli",
            adapter_kind="cli_contract",
            tiny_proof_kind="cli_sentinel_prompt",
            official_support_state="planned_or_external_only",
            auth_requirements=(
                _tiny_auth_requirement(
                    auth_strategy_id="anthropic_api_key_external",
                    provider_family_id="claude",
                    surface_id="claude_code_cli",
                    auth_kind="api_key_external",
                    official_support_state="contract_only_pending_doc_confirmation",
                    required_env_name_candidates=("ANTHROPIC_API_KEY",),
                    status="blocked_pending_owner_live_approval",
                ),
                _tiny_auth_requirement(
                    auth_strategy_id="claude_code_cli_managed_external",
                    provider_family_id="claude",
                    surface_id="claude_code_cli",
                    auth_kind="cli_managed_external",
                    official_support_state="planned_or_external_only",
                    optional_env_name_candidates=("CLAUDE_CODE_PROVIDER_PROFILE",),
                    reason_codes=("claude_ai_login_policy_not_approved",),
                ),
            ),
            future_stop_reason_codes=(
                "CLAUDE_PROVIDER_AUTH_NOT_AVAILABLE",
                "CLAUDE_AI_LOGIN_POLICY_NOT_APPROVED",
                "WOULD_REQUIRE_CLAUDE_CREDENTIAL_FILE_INSPECTION",
                "WOULD_REQUIRE_INTERACTIVE_LOGIN",
            ),
            policy_notes=("No /login, /logout, status command, browser login, or credential file inspection.",),
            recommendation_score=52,
            recommendation_factors=(
                ("officialSupportConfidence", "planned_or_external_only"),
                ("nonInteractiveFeasibility", "medium_pending_policy"),
                ("credentialCacheInspectionRequirement", "none_allowed"),
                ("browserDeviceLoginRequirement", "not_allowed_during_proof"),
                ("oneCallFeasibility", "medium_if_policy_safe_prompt_exists"),
                ("projectOwnerPriority", "primary_backend_family"),
                ("policyRisk", "high_if_claude_ai_login_required"),
                ("implementationBlastRadius", "low"),
            ),
            reason_codes=common_reasons + ("provider_policy_review_required",),
        ),
        ProviderTinyProofSurfaceCandidate(
            provider_family_id="antigravity",
            surface_id="antigravity_cli",
            execution_surface_type="cli",
            adapter_kind="cli_contract",
            tiny_proof_kind="cli_sentinel_prompt",
            official_support_state="contract_only_pending_doc_confirmation",
            auth_requirements=(
                _tiny_auth_requirement(
                    auth_strategy_id="google_oauth_external_or_cli_managed_external",
                    provider_family_id="antigravity",
                    surface_id="antigravity_cli",
                    auth_kind="cli_managed_external",
                    official_support_state="contract_only_pending_doc_confirmation",
                    optional_env_name_candidates=("ANTIGRAVITY_PROVIDER_PROFILE", "GOOGLE_CLOUD_PROJECT"),
                    reason_codes=("do_not_infer_readiness_from_cli_auth_cache",),
                ),
            ),
            future_stop_reason_codes=(
                "ANTIGRAVITY_PROVIDER_AUTH_NOT_AVAILABLE",
                "WOULD_REQUIRE_GOOGLE_CREDENTIAL_FILE_INSPECTION",
                "WOULD_REQUIRE_INTERACTIVE_LOGIN",
                "WOULD_REQUIRE_MORE_THAN_ONE_LIVE_CALL",
            ),
            policy_notes=("Do not inspect token/keyring/cache state; future proof must be one approved prompt invocation.",),
            recommendation_score=72,
            recommendation_factors=(
                ("officialSupportConfidence", "contract_pending_doc_confirmation"),
                ("nonInteractiveFeasibility", "medium_high_if_cli_contract_is_confirmed"),
                ("credentialCacheInspectionRequirement", "none_allowed"),
                ("browserDeviceLoginRequirement", "not_allowed_during_proof"),
                ("oneCallFeasibility", "medium_high"),
                ("projectOwnerPriority", "primary_backend_family"),
                ("policyRisk", "medium"),
                ("implementationBlastRadius", "low"),
            ),
            reason_codes=common_reasons,
        ),
        ProviderTinyProofSurfaceCandidate(
            provider_family_id="antigravity",
            surface_id="antigravity_sdk_python",
            execution_surface_type="sdk",
            adapter_kind="sdk_contract",
            tiny_proof_kind="unsupported",
            official_support_state="planned_or_external_only",
            auth_requirements=(
                _tiny_auth_requirement(
                    auth_strategy_id="api_key_external",
                    provider_family_id="antigravity",
                    surface_id="antigravity_sdk_python",
                    auth_kind="api_key_external",
                    official_support_state="planned_or_external_only",
                    required_env_name_candidates=("ANTIGRAVITY_API_KEY",),
                    status="blocked_pending_official_sdk_support",
                ),
                _tiny_auth_requirement(
                    auth_strategy_id="google_oauth_external",
                    provider_family_id="antigravity",
                    surface_id="antigravity_sdk_python",
                    auth_kind="planned_or_unsupported",
                    official_support_state="planned_or_external_only",
                    optional_env_name_candidates=("GOOGLE_CLOUD_PROJECT",),
                    status="blocked_pending_official_sdk_support",
                ),
            ),
            future_stop_reason_codes=(
                "ANTIGRAVITY_SDK_NOT_INSTALLED",
                "ANTIGRAVITY_SDK_AUTH_NOT_OFFICIALLY_SUPPORTED",
                "WOULD_REQUIRE_GOOGLE_CREDENTIAL_FILE_INSPECTION",
            ),
            policy_notes=("Do not assume CLI OAuth can be reused by SDK.",),
            recommendation_score=35,
            recommendation_factors=(
                ("officialSupportConfidence", "planned_or_external_only"),
                ("nonInteractiveFeasibility", "low_until_sdk_contract_confirmed"),
                ("credentialCacheInspectionRequirement", "none_allowed"),
                ("browserDeviceLoginRequirement", "not_allowed_during_proof"),
                ("oneCallFeasibility", "unknown"),
                ("projectOwnerPriority", "primary_backend_family"),
                ("policyRisk", "medium"),
                ("implementationBlastRadius", "medium"),
            ),
            reason_codes=common_reasons + ("sdk_oauth_not_proven_or_not_officially_supported",),
        ),
    )


def _tiny_proof_output_gate_summary(provider_family_id: str) -> dict[str, Any]:
    research_gate = provider_orchestration.evaluate_provider_output_gate(
        provider_id=provider_family_id,
        output_class="research_output",
        current_proof_level="live_verified_tiny",
        proof_present=True,
        accepted=True,
        profile_status="live_verified_tiny",
    )
    report_gate = provider_orchestration.evaluate_provider_output_gate(
        provider_id=provider_family_id,
        output_class="report_draft",
        current_proof_level="live_verified_tiny",
        proof_present=True,
        accepted=True,
        profile_status="live_verified_tiny",
    )
    return {
        "proofStatusCandidate": "live_verified_tiny",
        "tinyProofCanUnlockResearchOutput": research_gate.allowed,
        "tinyProofCanUnlockReportDraft": report_gate.allowed,
        "researchOutputGate": research_gate.to_dict(),
        "reportDraftGate": report_gate.to_dict(),
        "paperReviewAndWeeklyReportRemainSeparate": True,
    }


def describe_provider_tiny_proof_artifact_contract() -> dict[str, Any]:
    return ProviderTinyProofArtifactContract().to_dict()


def describe_provider_tiny_proof_prompt_contract() -> dict[str, Any]:
    return {
        "promptClass": "sentinel_only",
        "oneSentenceOnly": True,
        "fixedJsonObjectOnly": True,
        "containsUserSecrets": False,
        "containsRepoSource": False,
        "containsPaperData": False,
        "containsStorageData": False,
        "requestsChainOfThought": False,
        "expectedSentinelJsonShape": {
            "provider_family_id": "<codex|claude|antigravity>",
            "surface_id": "<surface>",
            "sentinel": "xrml-provider-tiny-proof",
            "no_chain_of_thought": True,
        },
        "doNotHardcodeProviderOutputAsProof": True,
        "doNotGenerateFakeProofArtifact": True,
    }


def _env_presence_map(names: Sequence[str], env: Mapping[str, str] | None) -> dict[str, bool]:
    current = env or {}
    return {name: name in current for name in names}


def _future_approval_env_observed(
    env: Mapping[str, str] | None,
    *,
    candidate: ProviderTinyProofSurfaceCandidate | None = None,
) -> dict[str, bool]:
    current = env or {}
    provider_id = candidate.provider_family_id if candidate else None
    surface_id = candidate.surface_id if candidate else None
    expected: dict[str, str | None] = {
        "XRML_PROVIDER_TINY_PROOF_APPROVED": "1",
        "XRML_PROVIDER_TINY_PROOF_PROVIDER": provider_id,
        "XRML_PROVIDER_TINY_PROOF_SURFACE": surface_id,
        "XRML_PROVIDER_TINY_PROOF_MODE": "tiny_live",
        "XRML_PROVIDER_TINY_PROOF_MAX_CALLS": "1",
        "XRML_PROVIDER_TINY_PROOF_ARTIFACT": None,
        "XRML_PROVIDER_TINY_PROOF_PROMPT_CLASS": "sentinel_only",
    }
    observed: dict[str, bool] = {}
    for name in TINY_PROOF_APPROVAL_ENV_NAMES:
        expected_value = expected[name]
        if expected_value is None:
            observed[name] = bool(current.get(name))
        else:
            observed[name] = current.get(name) == expected_value
    return observed


def recommend_provider_tiny_proof_candidates(
    *,
    unsafe_surface_ids: Sequence[str] = (),
    candidates: Sequence[ProviderTinyProofSurfaceCandidate] | None = None,
) -> dict[str, Any]:
    unsafe = set(unsafe_surface_ids)
    pool = list(_tiny_proof_candidates() if candidates is None else candidates)
    eligible = [
        candidate
        for candidate in pool
        if candidate.surface_id not in unsafe
        and candidate.tiny_proof_kind != "unsupported"
        and candidate.official_support_state not in {"unsupported", "unknown_do_not_run"}
    ]
    ordered = sorted(
        eligible,
        key=lambda candidate: (-candidate.recommendation_score, candidate.provider_family_id, candidate.surface_id),
    )
    if not ordered:
        return {
            "recommendationStatus": "NO_TINY_PROOF_CANDIDATE_READY",
            "recommendedFirstCandidate": None,
            "orderedCandidates": [],
            "reasonCodes": ["no_safe_tiny_proof_candidate_ready", "non_live_precondition_matrix_only"],
        }
    return {
        "recommendationStatus": "blocked_pending_future_approval_and_provider_auth",
        "recommendedFirstCandidate": ordered[0].to_dict(),
        "orderedCandidates": [candidate.to_dict() for candidate in ordered],
        "reasonCodes": [
            "candidate_visible_but_not_executable",
            "future_owner_approval_required",
            "provider_auth_surface_proof_missing",
        ],
    }


def plan_provider_tiny_proof(
    provider_family_id: str | None = None,
    surface_id: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    unsafe_surface_ids: Sequence[str] = (),
) -> dict[str, Any]:
    candidates = list(_tiny_proof_candidates())
    if provider_family_id:
        candidates = [candidate for candidate in candidates if candidate.provider_family_id == provider_family_id]
    if surface_id:
        candidates = [candidate for candidate in candidates if candidate.surface_id == surface_id]
    recommendation = recommend_provider_tiny_proof_candidates(
        unsafe_surface_ids=unsafe_surface_ids,
        candidates=candidates,
    )
    first = recommendation.get("recommendedFirstCandidate")
    selected_surface_id = first.get("surfaceId") if first else None
    selected = next((candidate for candidate in candidates if candidate.surface_id == selected_surface_id), None)
    auth_env_names = selected.provider_auth_env_name_candidates() if selected else ()
    reason_codes = (
        "non_live_precondition_plan_only",
        "tiny_proof_not_executed",
        "provider_command_not_run",
        "sdk_import_not_run",
        "output_class_proof_not_in_scope",
    )
    if selected is None:
        reason_codes = (*reason_codes, "NO_TINY_PROOF_CANDIDATE_READY")
    else:
        reason_codes = (*reason_codes, *selected.reason_codes)
    plan = ProviderTinyProofPlan(
        candidate=selected,
        future_live_approval_env_observed=_future_approval_env_observed(env, candidate=selected),
        future_provider_auth_env_observed=_env_presence_map(auth_env_names, env),
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        recommendation_status=str(recommendation["recommendationStatus"]),
    )
    payload = plan.to_dict()
    payload["recommendation"] = recommendation
    return payload


def describe_provider_tiny_proof_preconditions(
    *,
    env: Mapping[str, str] | None = None,
    unsafe_surface_ids: Sequence[str] = (),
) -> dict[str, Any]:
    candidates = [candidate.to_dict() for candidate in _tiny_proof_candidates()]
    auth_requirements = [
        requirement.to_dict()
        for candidate in _tiny_proof_candidates()
        for requirement in candidate.auth_requirements
    ]
    recommendation = recommend_provider_tiny_proof_candidates(unsafe_surface_ids=unsafe_surface_ids)
    return {
        "nonLive": True,
        "mayRunNow": False,
        "wouldExecuteProvider": False,
        "mayRunProviderCommand": False,
        "mayImportSdk": False,
        "mayCallNetwork": False,
        "providerAuthScope": PROVIDER_AUTH_SCOPE,
        "appOauthOidcInScope": False,
        "publicMcpConnectorInScope": False,
        "outputClassProofInScope": False,
        "openAiApiPipelineOnly": True,
        "excludedSurfaces": [
            {
                "providerFamilyId": OPENAI_API_PIPELINE_FAMILY_ID,
                "surfaceId": "openai_api_pipeline",
                "reasonCode": "api_pipeline_skeleton_not_tiny_proof_candidate",
            }
        ],
        "futureApprovalEnvContract": list(TINY_PROOF_APPROVAL_ENV_CONTRACT),
        "futureApprovalEnvObserved": _future_approval_env_observed(env),
        "artifactContract": describe_provider_tiny_proof_artifact_contract(),
        "promptContract": describe_provider_tiny_proof_prompt_contract(),
        "candidateProviderFamilyIds": ["codex", "claude", "antigravity"],
        "candidates": candidates,
        "authRequirements": auth_requirements,
        "recommendation": recommendation,
        "postTinyProofOutputGates": _tiny_proof_output_gate_summary("provider_tiny_proof"),
        "statusNotes": list(STATUS_NOTES),
        "reasonCodes": [
            "non_live_precondition_matrix_only",
            "future_owner_approval_required",
            "provider_command_not_run",
            "sdk_import_not_run",
            "tiny_proof_cannot_unlock_output_class",
        ],
    }
