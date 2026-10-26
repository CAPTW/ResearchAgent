"""Non-live provider execution-surface bridge.

This module inventories provider execution surfaces and builds blocked execution
plans without executing providers, probing credential caches, or reading local
secret/config files. It is intentionally smaller than the existing command smoke
and AI review runner paths: it only answers what could be prepared later and why
the current request remains blocked.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Mapping

from app.services import provider_auth_bridge, provider_configurations
from app.services import provider_orchestration as po
from app.services.providers import codex_sdk_provider


EXECUTION_SURFACE_TYPES: tuple[str, ...] = ("api", "cli", "sdk", "mcp", "fixture", "other")
ADAPTER_KINDS: tuple[str, ...] = (
    "executable_adapter",
    "sdk_adapter_scaffold",
    "command_contract",
    "metadata_only",
    "unsupported",
)
SUPPORTED_OUTPUT_CLASS_CANDIDATES: tuple[str, ...] = ("research_output", "report_draft")
PRODUCT_OUTPUT_CLASS_MAP: dict[str, str] = {
    "paper_review": "research_output",
    "weekly_report": "report_draft",
    "research_output": "research_output",
    "report_draft": "report_draft",
}
PROOF_STATUS_REQUIRED: dict[str, str] = {
    output_class: po.OUTPUT_CLASS_REQUIRED_PROOF[output_class]
    for output_class in SUPPORTED_OUTPUT_CLASS_CANDIDATES
}

OPENAI_PROFILE_ID = "openai_api"
OPENAI_REQUIRED_ENV_NAMES: tuple[str, ...] = ("OPENAI_API_KEY",)
OPENAI_CONFIG_FIELD_NAMES: tuple[str, ...] = (
    "XRML_PROVIDER",
    "OPENAI_MODEL",
    "OPENAI_API_BASE",
    "OPENAI_TIMEOUT_SECONDS",
)
COMMAND_LIVE_APPROVAL_ENV_NAMES: tuple[str, ...] = ("ALLOW_REAL_PROVIDER_SMOKE",)
FUTURE_LIVE_APPROVAL_FIELD_NAMES: tuple[str, ...] = (
    "XRML_PROVIDER_LIVE_EXECUTION_APPROVED",
    "XRML_PROVIDER_LIVE_OUTPUT_CLASS",
)
OUTPUT_CLASS_PROOF_FIELD_NAMES: tuple[str, ...] = (
    "XRML_PROVIDER_LIVE_PROOF_STATUS",
    "XRML_PROVIDER_LIVE_PROOF_ARTIFACT",
    "XRML_ALLOW_PROVIDER_RESEARCH_OUTPUT",
)
SAFETY_REASON_CODES: tuple[str, ...] = (
    "no_secret_inspection",
    "no_token_cache_inspection",
    "app_oauth_oidc_not_in_scope",
    "public_mcp_connector_not_in_scope",
)
DETERMINISTIC_LOCAL_PROFILE_IDS: frozenset[str] = frozenset({"stub", "fixture-command"})


def _unique(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in values if str(value).strip()))


def _env_present(env: Mapping[str, str] | None, name: str) -> bool:
    source = os.environ if env is None else env
    return bool(str(source.get(name, "")).strip())


def _env_presence(env: Mapping[str, str] | None, names: tuple[str, ...]) -> dict[str, bool]:
    return {name: _env_present(env, name) for name in names}


def _contract_output_class(output_class: str) -> str | None:
    return PRODUCT_OUTPUT_CLASS_MAP.get(str(output_class or "").strip())


def _proof_level_from_proofs(
    provider_id: str,
    output_class: str,
    accepted_output_proofs: dict[str, dict[str, str]] | None,
) -> tuple[str, bool]:
    proofs = accepted_output_proofs or {}
    by_provider = proofs.get(provider_id, {})
    proof_level = str(by_provider.get(output_class) or "")
    if proof_level:
        return proof_level, True
    return "none", False


def _proven_output_classes(
    *,
    provider_id: str,
    profile_status: str,
    accepted_output_proofs: dict[str, dict[str, str]] | None,
) -> tuple[str, ...]:
    proven: list[str] = []
    for output_class in SUPPORTED_OUTPUT_CLASS_CANDIDATES:
        proof_level, proof_present = _proof_level_from_proofs(
            provider_id, output_class, accepted_output_proofs
        )
        gate = po.evaluate_provider_output_gate(
            provider_id=provider_id,
            output_class=output_class,
            current_proof_level=proof_level,
            proof_present=proof_present,
            accepted=proof_present,
            profile_status=profile_status,
        )
        if gate.allowed:
            proven.append(output_class)
    return tuple(proven)


def _safe_profile_id(profile: dict[str, Any]) -> str:
    return str(profile.get("providerId") or "").strip()


def _canonical_provider_id(profile_id: str) -> str:
    return provider_configurations.canonical_provider_alias(profile_id) or profile_id


def _command_contract_exists(profile: dict[str, Any]) -> bool:
    return any(
        bool(profile.get(key))
        for key in ("commandArgvTemplate", "allowedCommands", "versionArgs", "tinyPromptArgs")
    )


def _adapter_kind_for_profile(profile: dict[str, Any], validation_status: str) -> str:
    profile_id = _safe_profile_id(profile)
    profile_role = str(profile.get("profileRole") or "").strip()
    provider_type = str(profile.get("providerType") or "").strip()
    status = str(profile.get("status") or "").strip()
    if validation_status == "invalid" or status == "unsupported":
        return "unsupported"
    if profile_id in DETERMINISTIC_LOCAL_PROFILE_IDS or profile_role in {"stub", "fixture"} or provider_type == "stub":
        return "unsupported"
    if _command_contract_exists(profile):
        return "command_contract"
    return "metadata_only"


def _config_field_names_for_kind(adapter_kind: str) -> tuple[str, ...]:
    fields = [
        "providerId",
        "displayName",
        "executionSurface",
        "authMode",
        "credentialPolicy",
        "proofArtifactRequired",
        "status",
        "researchOutputAllowed",
    ]
    if adapter_kind == "command_contract":
        fields.extend(["commandArgvTemplate", "allowedCommands", "allowLiveCommand"])
    return tuple(fields)


@dataclass(frozen=True)
class ProviderExecutionSurface:
    provider_id: str
    display_name: str
    profile_id: str
    execution_surface_type: str
    adapter_kind: str
    profile_exists: bool
    configured: bool
    required_env_names: tuple[str, ...]
    required_config_field_names: tuple[str, ...]
    optional_command_env_var_names: tuple[str, ...]
    env_presence: dict[str, bool]
    supported_output_class_candidates: tuple[str, ...]
    proven_output_classes: tuple[str, ...]
    proof_status_required: dict[str, str]
    blocked_reason_codes: tuple[str, ...]
    profile_status: str = "configured_not_verified"
    executable_adapter_exists: bool = False
    command_contract_exists: bool = False
    no_secret_inspection: bool = True
    no_token_cache_inspection: bool = True
    app_oauth_oidc_in_scope: bool = False
    public_mcp_connector_in_scope: bool = False

    @property
    def metadata_only(self) -> bool:
        return self.adapter_kind == "metadata_only"

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_reason_codes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "providerId": self.provider_id,
            "displayName": self.display_name,
            "profileId": self.profile_id,
            "executionSurfaceType": self.execution_surface_type,
            "adapterKind": self.adapter_kind,
            "profileExists": self.profile_exists,
            "configured": self.configured,
            "executableAdapterExists": self.executable_adapter_exists,
            "commandContractExists": self.command_contract_exists,
            "metadataOnly": self.metadata_only,
            "requiredEnvNames": list(self.required_env_names),
            "requiredConfigFieldNames": list(self.required_config_field_names),
            "optionalCommandEnvVarNames": list(self.optional_command_env_var_names),
            "envPresence": dict(self.env_presence),
            "supportedOutputClassCandidates": list(self.supported_output_class_candidates),
            "provenOutputClasses": list(self.proven_output_classes),
            "proofStatusRequired": dict(self.proof_status_required),
            "profileStatus": self.profile_status,
            "blocked": self.blocked,
            "blockedReasonCodes": list(self.blocked_reason_codes),
            "noSecretInspection": self.no_secret_inspection,
            "noTokenCacheInspection": self.no_token_cache_inspection,
            "appOauthOidcInScope": self.app_oauth_oidc_in_scope,
            "publicMcpConnectorInScope": self.public_mcp_connector_in_scope,
        }


@dataclass(frozen=True)
class ProviderExecutionReadiness:
    provider_id: str
    profile_id: str
    requested_output_class: str
    output_class: str | None
    execution_surface_type: str
    adapter_kind: str
    profile_exists: bool
    configured: bool
    output_allowed: bool
    live_execution_allowed: bool
    would_execute_provider: bool
    blocked: bool
    blocked_reason: str
    reason_codes: tuple[str, ...]
    required_proof_level: str
    current_proof_level: str
    proof_present: bool
    accepted: bool
    proof_artifact_creation_required: bool
    future_live_approval_required: bool
    future_live_approval_field_names: tuple[str, ...]
    proof_artifact_field_names: tuple[str, ...]
    selected_for_future_paper_review_proof: bool = False
    no_secret_inspection: bool = True
    no_token_cache_inspection: bool = True
    app_oauth_oidc_in_scope: bool = False
    public_mcp_connector_in_scope: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "providerId": self.provider_id,
            "profileId": self.profile_id,
            "requestedOutputClass": self.requested_output_class,
            "outputClass": self.output_class,
            "executionSurfaceType": self.execution_surface_type,
            "adapterKind": self.adapter_kind,
            "profileExists": self.profile_exists,
            "configured": self.configured,
            "outputAllowed": self.output_allowed,
            "liveExecutionAllowed": self.live_execution_allowed,
            "wouldExecuteProvider": self.would_execute_provider,
            "blocked": self.blocked,
            "blockedReason": self.blocked_reason,
            "reasonCodes": list(self.reason_codes),
            "requiredProofLevel": self.required_proof_level,
            "currentProofLevel": self.current_proof_level,
            "proofPresent": self.proof_present,
            "accepted": self.accepted,
            "proofArtifactCreationRequired": self.proof_artifact_creation_required,
            "futureLiveApprovalRequired": self.future_live_approval_required,
            "futureLiveApprovalFieldNames": list(self.future_live_approval_field_names),
            "proofArtifactFieldNames": list(self.proof_artifact_field_names),
            "selectedForFuturePaperReviewProof": self.selected_for_future_paper_review_proof,
            "noSecretInspection": self.no_secret_inspection,
            "noTokenCacheInspection": self.no_token_cache_inspection,
            "appOauthOidcInScope": self.app_oauth_oidc_in_scope,
            "publicMcpConnectorInScope": self.public_mcp_connector_in_scope,
        }


class ProviderExecutionBlocked(RuntimeError):
    """Raised by future live runners when readiness is blocked."""


def _openai_surface(
    *,
    env: Mapping[str, str] | None,
    accepted_output_proofs: dict[str, dict[str, str]] | None,
) -> ProviderExecutionSurface:
    env_presence = _env_presence(env, (*OPENAI_REQUIRED_ENV_NAMES, *OPENAI_CONFIG_FIELD_NAMES))
    configured = env_presence["OPENAI_API_KEY"]
    reason_codes = [
        "profile_exists",
        "executable_adapter_exists",
        "safe_env_presence_booleans_only",
        "output_class_proof_missing",
        "live_execution_disabled_by_default",
        *SAFETY_REASON_CODES,
    ]
    if not configured:
        reason_codes.extend(["provider_unconfigured", "required_env_missing"])
    proven = _proven_output_classes(
        provider_id="openai",
        profile_status="configured_not_verified",
        accepted_output_proofs=accepted_output_proofs,
    )
    if proven:
        reason_codes = [code for code in reason_codes if code != "output_class_proof_missing"]
        reason_codes.append("output_class_proof_present_but_bridge_non_live")
    return ProviderExecutionSurface(
        provider_id="openai",
        display_name="OpenAI Responses API",
        profile_id=OPENAI_PROFILE_ID,
        execution_surface_type="api",
        adapter_kind="executable_adapter",
        profile_exists=True,
        configured=configured,
        required_env_names=OPENAI_REQUIRED_ENV_NAMES,
        required_config_field_names=OPENAI_CONFIG_FIELD_NAMES,
        optional_command_env_var_names=(),
        env_presence=env_presence,
        supported_output_class_candidates=SUPPORTED_OUTPUT_CLASS_CANDIDATES,
        proven_output_classes=proven,
        proof_status_required=PROOF_STATUS_REQUIRED,
        blocked_reason_codes=_unique(reason_codes),
        profile_status="configured_not_verified",
        executable_adapter_exists=True,
        command_contract_exists=False,
    )


def _codex_sdk_surface(
    *,
    env: Mapping[str, str] | None,
    accepted_output_proofs: dict[str, dict[str, str]] | None,
) -> ProviderExecutionSurface:
    readiness = codex_sdk_provider.describe_codex_sdk_readiness(env)
    reason_codes = [
        "profile_exists",
        "codex_sdk_adapter_scaffolded",
        "sdk_adapter_scaffold_profile",
        "safe_env_presence_booleans_only",
        "output_class_proof_missing",
        "live_execution_disabled_by_default",
        "executable_adapter_missing",
        "blocked_pending_dependency_install",
        "blocked_pending_owner_live_approval",
        "blocked_pending_provider_auth_surface_proof",
        *SAFETY_REASON_CODES,
        *[str(code) for code in readiness.get("reasonCodes", [])],
    ]
    proven = _proven_output_classes(
        provider_id=codex_sdk_provider.PROVIDER_ID,
        profile_status="configured_not_verified",
        accepted_output_proofs=accepted_output_proofs,
    )
    if proven:
        reason_codes = [code for code in reason_codes if code != "output_class_proof_missing"]
        reason_codes.append("output_class_proof_present_but_bridge_non_live")
    return ProviderExecutionSurface(
        provider_id=codex_sdk_provider.PROVIDER_ID,
        display_name="Codex SDK Python Adapter Scaffold",
        profile_id=codex_sdk_provider.PROFILE_ID,
        execution_surface_type=codex_sdk_provider.EXECUTION_SURFACE_TYPE,
        adapter_kind=codex_sdk_provider.ADAPTER_KIND,
        profile_exists=True,
        configured=False,
        required_env_names=codex_sdk_provider.AUTH_ENV_NAME_CANDIDATES,
        required_config_field_names=(
            "providerId",
            "profileId",
            "providerFamilyId",
            "executionSurfaceType",
            "adapterKind",
            "providerAuthScope",
            "authEnvNames",
            "outputClassProofInScope",
        ),
        optional_command_env_var_names=(),
        env_presence=dict(readiness["authEnvPresence"]),
        supported_output_class_candidates=SUPPORTED_OUTPUT_CLASS_CANDIDATES,
        proven_output_classes=proven,
        proof_status_required=PROOF_STATUS_REQUIRED,
        blocked_reason_codes=_unique(reason_codes),
        profile_status="configured_not_verified",
        executable_adapter_exists=False,
        command_contract_exists=False,
    )


def _surface_from_provider_profile(
    profile: dict[str, Any],
    *,
    env: Mapping[str, str] | None,
    accepted_output_proofs: dict[str, dict[str, str]] | None,
) -> ProviderExecutionSurface:
    validation = provider_configurations.validate_provider_configuration(profile)
    config = validation["providerConfiguration"]
    validation_status = str(validation.get("validationStatus") or "invalid")
    profile_id = str(config.get("providerId") or _safe_profile_id(profile))
    provider_id = _canonical_provider_id(profile_id)
    adapter_kind = _adapter_kind_for_profile(config, validation_status)
    execution_surface = str(config.get("executionSurface") or "other").strip().lower()
    if execution_surface not in EXECUTION_SURFACE_TYPES:
        execution_surface = "other"
    command_contract_exists = adapter_kind == "command_contract"
    credential_policy = {str(item) for item in config.get("credentialPolicy") or []}
    env_names: tuple[str, ...] = ()
    reason_codes = [
        "profile_exists",
        "output_class_proof_missing",
        "live_execution_disabled_by_default",
        *SAFETY_REASON_CODES,
    ]
    if validation_status == "invalid":
        reason_codes.append("invalid_provider_configuration")
    if adapter_kind == "unsupported":
        reason_codes.append("unsupported_profile")
    elif adapter_kind == "metadata_only":
        reason_codes.append("metadata_only_profile")
    elif adapter_kind == "command_contract":
        reason_codes.extend(
            [
                "command_contract_profile",
                "executable_adapter_missing",
                "command_adapter_availability_not_verified",
                "explicit_live_approval_required",
            ]
        )
    if profile_id in DETERMINISTIC_LOCAL_PROFILE_IDS:
        reason_codes.append("deterministic_local_fixture_not_live_provider")
    if str(config.get("status") or "") in {"policy_sensitive", "unsupported"}:
        reason_codes.append(f"provider_status_{config.get('status')}")
    if "env_presence_only" in credential_policy and not env_names:
        reason_codes.append("env_presence_policy_without_declared_env_name")

    bridge_payload = provider_auth_bridge.evaluate_provider_auth_bridge(config).to_dict()
    issue_codes = [
        str(issue.get("code") or "")
        for issue in bridge_payload.get("issues") or []
        if str(issue.get("code") or "").strip()
    ]
    reason_codes.extend(issue_codes)
    if bridge_payload.get("blockedReason"):
        reason_codes.append(str(bridge_payload["blockedReason"]))

    proven = _proven_output_classes(
        provider_id=provider_id,
        profile_status=str(config.get("status") or "configured_not_verified"),
        accepted_output_proofs=accepted_output_proofs,
    )
    if proven:
        reason_codes = [code for code in reason_codes if code != "output_class_proof_missing"]
        reason_codes.append("output_class_proof_present_but_bridge_non_live")

    return ProviderExecutionSurface(
        provider_id=provider_id,
        display_name=str(config.get("displayName") or profile_id),
        profile_id=profile_id,
        execution_surface_type=execution_surface,
        adapter_kind=adapter_kind,
        profile_exists=True,
        configured=False,
        required_env_names=env_names,
        required_config_field_names=_config_field_names_for_kind(adapter_kind),
        optional_command_env_var_names=COMMAND_LIVE_APPROVAL_ENV_NAMES if command_contract_exists else (),
        env_presence=_env_presence(env, env_names),
        supported_output_class_candidates=SUPPORTED_OUTPUT_CLASS_CANDIDATES,
        proven_output_classes=proven,
        proof_status_required=PROOF_STATUS_REQUIRED,
        blocked_reason_codes=_unique(reason_codes),
        profile_status=str(config.get("status") or "configured_not_verified"),
        executable_adapter_exists=False,
        command_contract_exists=command_contract_exists,
    )


def describe_provider_execution_surfaces(
    *,
    env: Mapping[str, str] | None = None,
    accepted_output_proofs: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Return a deterministic, secret-safe inventory of provider surfaces."""
    surfaces = [
        _openai_surface(env=env, accepted_output_proofs=accepted_output_proofs),
        _codex_sdk_surface(env=env, accepted_output_proofs=accepted_output_proofs),
        *[
            _surface_from_provider_profile(
                profile,
                env=env,
                accepted_output_proofs=accepted_output_proofs,
            )
            for profile in provider_configurations.DEFAULT_PROVIDER_CONFIGS
        ],
    ]
    payloads = [surface.to_dict() for surface in surfaces]
    counts = {kind: sum(1 for surface in surfaces if surface.adapter_kind == kind) for kind in ADAPTER_KINDS}
    return {
        "nonLive": True,
        "wouldExecuteProvider": False,
        "liveExecutionAllowed": False,
        "noSecretInspection": True,
        "noTokenCacheInspection": True,
        "appOauthOidcInScope": False,
        "publicMcpConnectorInScope": False,
        "executionSurfaceTypes": list(EXECUTION_SURFACE_TYPES),
        "adapterKinds": list(ADAPTER_KINDS),
        "supportedOutputClassCandidates": list(SUPPORTED_OUTPUT_CLASS_CANDIDATES),
        "proofStatusRequired": dict(PROOF_STATUS_REQUIRED),
        "futureLiveApprovalFieldNames": list(FUTURE_LIVE_APPROVAL_FIELD_NAMES),
        "proofArtifactFieldNames": list(OUTPUT_CLASS_PROOF_FIELD_NAMES),
        "surfaces": payloads,
        "summary": {
            "surfaceCount": len(payloads),
            "adapterKindCounts": counts,
            "allOutputBlocked": all(not surface.proven_output_classes for surface in surfaces),
            "defaultFailClosed": True,
        },
        "nextPaperReviewProofCandidate": select_provider_for_future_output_class_proof(
            output_class="paper_review",
            env=env,
            accepted_output_proofs=accepted_output_proofs,
        ),
    }


def _all_surfaces(
    *,
    env: Mapping[str, str] | None,
    accepted_output_proofs: dict[str, dict[str, str]] | None,
) -> list[ProviderExecutionSurface]:
    return [
        _openai_surface(env=env, accepted_output_proofs=accepted_output_proofs),
        _codex_sdk_surface(env=env, accepted_output_proofs=accepted_output_proofs),
        *[
            _surface_from_provider_profile(profile, env=env, accepted_output_proofs=accepted_output_proofs)
            for profile in provider_configurations.DEFAULT_PROVIDER_CONFIGS
        ],
    ]


def resolve_provider_execution_surface(
    provider_id: str,
    *,
    env: Mapping[str, str] | None = None,
    accepted_output_proofs: dict[str, dict[str, str]] | None = None,
) -> ProviderExecutionSurface | None:
    """Resolve by provider id, canonical alias, or profile id."""
    wanted = str(provider_id or "").strip().lower()
    wanted_alias = provider_configurations.canonical_provider_alias(wanted)
    if wanted in {"openai", OPENAI_PROFILE_ID}:
        return _openai_surface(env=env, accepted_output_proofs=accepted_output_proofs)
    matches: list[ProviderExecutionSurface] = []
    for surface in _all_surfaces(env=env, accepted_output_proofs=accepted_output_proofs):
        aliases = {
            surface.provider_id.lower(),
            surface.profile_id.lower(),
            provider_configurations.canonical_provider_alias(surface.profile_id).lower(),
        }
        if wanted in aliases or wanted_alias in aliases:
            matches.append(surface)
    if not matches:
        return None
    non_example = [surface for surface in matches if not surface.profile_id.endswith("-example")]
    return non_example[0] if non_example else matches[0]


def rank_provider_execution_candidates(
    *,
    output_class: str = "paper_review",
    weights: Mapping[str, int] | None = None,
    env: Mapping[str, str] | None = None,
    accepted_output_proofs: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Rank provider candidates for a future proof without granting execution."""
    contract_output_class = _contract_output_class(output_class)
    if contract_output_class not in SUPPORTED_OUTPUT_CLASS_CANDIDATES:
        return []
    adapter_rank = {
        "executable_adapter": 0,
        "sdk_adapter_scaffold": 1,
        "command_contract": 2,
        "metadata_only": 3,
        "unsupported": 4,
    }
    rows: list[tuple[tuple[int, int, str], dict[str, Any]]] = []
    for surface in _all_surfaces(env=env, accepted_output_proofs=accepted_output_proofs):
        if contract_output_class not in surface.supported_output_class_candidates:
            continue
        weight = int((weights or {}).get(surface.profile_id, (weights or {}).get(surface.provider_id, 0)) or 0)
        candidate = surface.to_dict()
        candidate["requestedOutputClass"] = output_class
        candidate["outputClass"] = contract_output_class
        candidate["candidateWeight"] = weight
        candidate["wouldExecuteProvider"] = False
        rows.append(((adapter_rank.get(surface.adapter_kind, 9), -weight, surface.profile_id), candidate))
    return [row for _sort_key, row in sorted(rows, key=lambda item: item[0])]


def select_provider_for_future_output_class_proof(
    *,
    output_class: str = "paper_review",
    env: Mapping[str, str] | None = None,
    accepted_output_proofs: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any] | None:
    """Select the best future proof candidate while keeping execution blocked."""
    ranked = rank_provider_execution_candidates(
        output_class=output_class,
        env=env,
        accepted_output_proofs=accepted_output_proofs,
    )
    for row in ranked:
        if row["adapterKind"] == "executable_adapter":
            return {
                "providerId": row["providerId"],
                "profileId": row["profileId"],
                "outputClass": row["outputClass"],
                "executionSurfaceType": row["executionSurfaceType"],
                "adapterKind": row["adapterKind"],
                "configured": row["configured"],
                "selected": True,
                "readyNow": False,
                "wouldExecuteProvider": False,
                "blockedReasonCodes": list(row["blockedReasonCodes"]),
                "requiredEnvNames": list(row["requiredEnvNames"]),
                "futureLiveApprovalFieldNames": list(FUTURE_LIVE_APPROVAL_FIELD_NAMES),
                "proofArtifactFieldNames": list(OUTPUT_CLASS_PROOF_FIELD_NAMES),
            }
    return None


def plan_provider_output_class_execution(
    provider_id: str,
    output_class: str,
    *,
    env: Mapping[str, str] | None = None,
    accepted_output_proofs: dict[str, dict[str, str]] | None = None,
    live_execution_approved: bool = False,
) -> ProviderExecutionReadiness:
    """Plan a provider/output-class request and return the blocked non-live result."""
    contract_output_class = _contract_output_class(output_class)
    surface = resolve_provider_execution_surface(
        provider_id,
        env=env,
        accepted_output_proofs=accepted_output_proofs,
    )
    if surface is None:
        reason_codes = _unique(
            [
                "provider_profile_missing",
                "output_class_proof_missing",
                "live_execution_disabled_by_default",
                *SAFETY_REASON_CODES,
            ]
        )
        return ProviderExecutionReadiness(
            provider_id=str(provider_id or ""),
            profile_id="",
            requested_output_class=output_class,
            output_class=contract_output_class,
            execution_surface_type="other",
            adapter_kind="unsupported",
            profile_exists=False,
            configured=False,
            output_allowed=False,
            live_execution_allowed=False,
            would_execute_provider=False,
            blocked=True,
            blocked_reason="provider_profile_missing",
            reason_codes=reason_codes,
            required_proof_level=PROOF_STATUS_REQUIRED.get(contract_output_class or "", "live_verified"),
            current_proof_level="none",
            proof_present=False,
            accepted=False,
            proof_artifact_creation_required=True,
            future_live_approval_required=True,
            future_live_approval_field_names=FUTURE_LIVE_APPROVAL_FIELD_NAMES,
            proof_artifact_field_names=OUTPUT_CLASS_PROOF_FIELD_NAMES,
        )

    if contract_output_class not in SUPPORTED_OUTPUT_CLASS_CANDIDATES:
        reason_codes = _unique(
            [
                *surface.blocked_reason_codes,
                "unknown_output_class",
                "provider_execution_refused_non_live",
            ]
        )
        return ProviderExecutionReadiness(
            provider_id=surface.provider_id,
            profile_id=surface.profile_id,
            requested_output_class=output_class,
            output_class=contract_output_class,
            execution_surface_type=surface.execution_surface_type,
            adapter_kind=surface.adapter_kind,
            profile_exists=surface.profile_exists,
            configured=surface.configured,
            output_allowed=False,
            live_execution_allowed=False,
            would_execute_provider=False,
            blocked=True,
            blocked_reason="unknown_output_class",
            reason_codes=reason_codes,
            required_proof_level="live_verified",
            current_proof_level="none",
            proof_present=False,
            accepted=False,
            proof_artifact_creation_required=True,
            future_live_approval_required=True,
            future_live_approval_field_names=FUTURE_LIVE_APPROVAL_FIELD_NAMES,
            proof_artifact_field_names=OUTPUT_CLASS_PROOF_FIELD_NAMES,
        )

    proof_level, proof_present = _proof_level_from_proofs(
        surface.provider_id, contract_output_class, accepted_output_proofs
    )
    gate = po.evaluate_provider_output_gate(
        provider_id=surface.provider_id,
        output_class=contract_output_class,
        current_proof_level=proof_level if proof_present else po.proof_level_for_profile({"status": surface.profile_status}),
        proof_present=proof_present,
        accepted=proof_present,
        profile_status=surface.profile_status,
    )
    reason_codes = [
        *surface.blocked_reason_codes,
        gate.blocked_reason or "",
        "explicit_live_approval_missing" if not live_execution_approved else "explicit_live_approval_supplied_non_live",
        "provider_execution_refused_non_live",
    ]
    if surface.adapter_kind != "executable_adapter":
        reason_codes.append(f"{surface.adapter_kind}_cannot_execute")
    if not surface.configured:
        reason_codes.append("provider_not_configured_for_live_proof")
    blocked_reason = (
        gate.blocked_reason
        or "provider_execution_refused_non_live"
    )
    return ProviderExecutionReadiness(
        provider_id=surface.provider_id,
        profile_id=surface.profile_id,
        requested_output_class=output_class,
        output_class=contract_output_class,
        execution_surface_type=surface.execution_surface_type,
        adapter_kind=surface.adapter_kind,
        profile_exists=surface.profile_exists,
        configured=surface.configured,
        output_allowed=gate.allowed,
        live_execution_allowed=False,
        would_execute_provider=False,
        blocked=True,
        blocked_reason=blocked_reason,
        reason_codes=_unique(reason_codes),
        required_proof_level=gate.required_proof_level,
        current_proof_level=gate.current_proof_level,
        proof_present=gate.proof_present,
        accepted=gate.accepted,
        proof_artifact_creation_required=True,
        future_live_approval_required=True,
        future_live_approval_field_names=FUTURE_LIVE_APPROVAL_FIELD_NAMES,
        proof_artifact_field_names=OUTPUT_CLASS_PROOF_FIELD_NAMES,
        selected_for_future_paper_review_proof=(
            output_class == "paper_review"
            and surface.provider_id == "openai"
            and surface.adapter_kind == "executable_adapter"
        ),
    )
