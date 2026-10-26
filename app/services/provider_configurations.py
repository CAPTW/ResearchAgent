from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Any, Callable

from app.repositories.files import iso_now_utc
from app.services import provider_auth_bridge

PROVIDER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
TOPIC_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,80}$")
SECRET_KEY_PARTS = ("secret", "token", "api_key", "apikey", "password", "credential", "authorization", "bearer")
ALLOWED_COMMAND_TEMPLATE_PLACEHOLDERS = frozenset({"packet_path", "output_path", "provider_run_id", "topic_id"})
PLACEHOLDER_PATTERN = re.compile(r"{([a-zA-Z0-9_]+)}")
SHELL_OPERATOR_TOKENS = ("&&", "||", "$(", "`")
SHELL_OPERATOR_ARGS = {"|", "||", "&", "&&", ";", "<", ">", ">>", "2>", "2>>"}

PROVIDER_TYPES = {"stub", "command"}
DEFAULT_MODES = {"stub", "command_dry_run"}
REVIEW_LEVELS = {"abstract_only", "full_text_grounded"}
PROFILE_ROLES = {"stub", "generic_command", "fixture", "real_example"}
EXECUTION_SURFACES = {"sdk", "cli", "api", "mcp"}
AUTH_MODES = {
    "api_key",
    "google_adc",
    "cli_oauth",
    "chatgpt_signin",
    "claude_code_login",
    "external_cli_auth",
    "oidc",
    "none",
    "unsupported",
    "unknown",
}
CREDENTIAL_POLICIES = {
    "external_only",
    "env_presence_only",
    "operator_profile",
    "no_token_storage",
    "no_cache_inspection",
}
PROFILE_STATUSES = {
    "configured_not_verified",
    "provider_smoke_only",
    "non_live_available",
    "unsupported",
    "policy_sensitive",
    "live_verified_tiny",
    "live_verified",
}
STATUS_AFTER_PROOF_CANDIDATES = {"live_verified_tiny", "live_verified"}
CANONICAL_PROVIDER_STATUSES = {
    "live_verified_tiny",
    "live_verified",
    "configured_not_verified",
    "non_live_available",
    "unsupported",
    "policy_sensitive",
    "dry_run_verified",
    "configured_but_not_available",
    "environment_blocked_binary_missing",
    "environment_blocked_live_gate_missing",
    "environment_blocked_command_unknown",
    "unsafe_to_probe",
    "mock_only",
    "docs_only",
    "not_supported",
}
ROUTING_LABELS = {
    "provider_smoke_only",
    "not_allowed_for_research_output",
    "setup_required",
    "operator_approval_required",
    "live_proof_required",
    "research_output_blocked",
}
SAFE_CREDENTIAL_METADATA_KEYS = {
    "credentialpolicy",
    "credential_policy",
    "credentialboundarydescription",
    "credential_boundary_description",
    "forbiddencredentialpaths",
    "forbidden_credential_paths",
    "operatorprofilesecretfree",
    "operator_profile_secret_free",
}
DEFAULT_FORBIDDEN_CREDENTIAL_PATHS = (
    ".mcp.json",
    "auth.json",
    "codex/auth",
    "claude/credentials",
    "claude/cache",
    "antigravity/local-state",
    "antigravity/cache",
    "gemini/oauth",
    "browser/session",
    "browser/cookies",
    "keychain-export",
    "credential-store-export",
    "storage/",
    "storage/backups/",
    "output/",
)
REAL_PROVIDER_ALIASES = {
    "gemini-cli-example": "gemini_cli",
    "gemini_cli_example": "gemini_cli",
    "gemini_cli": "gemini_cli",
    "gemini": "gemini_cli",
    "antigravity-cli-example": "antigravity_cli",
    "antigravity_cli_example": "antigravity_cli",
    "antigravity_cli": "antigravity_cli",
    "antigravity": "antigravity_cli",
    "agy": "antigravity_cli",
    "claude-code-example": "claude_code",
    "claude_code_example": "claude_code",
    "claude_code": "claude_code",
    "claude": "claude_code",
    "codex-cli-example": "codex_cli",
    "codex_cli_example": "codex_cli",
    "codex_cli": "codex_cli",
    "codex": "codex_cli",
}
MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 300
MIN_OUTPUT_CHARS = 1000
MAX_OUTPUT_CHARS = 100_000
MIN_PACKET_CHARS = 1000
MAX_PACKET_CHARS = 100_000

PROVIDER_PROFILE_EXAMPLES: tuple[dict[str, Any], ...] = (
    {
        "providerId": "stub",
        "displayName": "Deterministic Stub Provider",
        "providerType": "stub",
        "profileRole": "stub",
        "executionSurface": "sdk",
        "authMode": "none",
        "credentialPolicy": ["no_token_storage"],
        "status": "configured_not_verified",
        "proofArtifactRequired": False,
        "allowedCommands": [],
        "forbiddenCredentialPaths": list(DEFAULT_FORBIDDEN_CREDENTIAL_PATHS),
        "statusAfterProofCandidate": "live_verified_tiny",
        "researchOutputAllowed": False,
        "credentialBoundaryDescription": "Local deterministic stub; no external credential boundary.",
        "enabled": True,
        "defaultMode": "stub",
        "commandArgvTemplate": [],
        "workingDirectory": None,
        "timeoutSeconds": 30,
        "maxOutputChars": 20_000,
        "maxPacketChars": 12_000,
        "allowedTopicIds": [],
        "allowedReviewLevels": ["full_text_grounded"],
        "allowLiveCommand": False,
        "requiresOperatorConfirmation": True,
        "requiresOperatorAcceptance": False,
        "notes": ["Deterministic local stub; no external provider is called."],
    },
    {
        "providerId": "fixture-command",
        "displayName": "Local Fixture Command Provider",
        "providerType": "command",
        "profileRole": "fixture",
        "executionSurface": "cli",
        "authMode": "none",
        "credentialPolicy": ["external_only", "no_token_storage", "no_cache_inspection"],
        "status": "configured_not_verified",
        "proofArtifactRequired": False,
        "allowedCommands": [["python", "-m", "app.cli.provider_fixture_command"]],
        "forbiddenCredentialPaths": list(DEFAULT_FORBIDDEN_CREDENTIAL_PATHS),
        "statusAfterProofCandidate": "live_verified_tiny",
        "researchOutputAllowed": False,
        "credentialBoundaryDescription": "Local fixture command; no provider credential boundary.",
        "enabled": True,
        "defaultMode": "command_dry_run",
        "commandArgvTemplate": [
            "python",
            "-m",
            "app.cli.provider_fixture_command",
            "--packet",
            "{packet_path}",
            "--output",
            "{output_path}",
            "--run-id",
            "{provider_run_id}",
            "--topic",
            "{topic_id}",
        ],
        "workingDirectory": None,
        "timeoutSeconds": 30,
        "maxOutputChars": 20_000,
        "maxPacketChars": 12_000,
        "allowedTopicIds": [],
        "allowedReviewLevels": ["full_text_grounded"],
        "allowLiveCommand": False,
        "requiresOperatorConfirmation": True,
        "requiresOperatorAcceptance": False,
        "notes": ["Deterministic local fixture command for tests and smoke; no external provider is called."],
    },
    {
        "providerId": "command",
        "displayName": "Local Command Provider",
        "providerType": "command",
        "profileRole": "generic_command",
        "executionSurface": "cli",
        "authMode": "external_cli_auth",
        "credentialPolicy": ["external_only", "no_token_storage", "no_cache_inspection"],
        "status": "configured_not_verified",
        "proofArtifactRequired": True,
        "allowedCommands": [],
        "forbiddenCredentialPaths": list(DEFAULT_FORBIDDEN_CREDENTIAL_PATHS),
        "statusAfterProofCandidate": "live_verified_tiny",
        "researchOutputAllowed": False,
        "credentialBoundaryDescription": "Operator-owned external CLI boundary; repo stores no credentials.",
        "enabled": True,
        "defaultMode": "command_dry_run",
        "commandArgvTemplate": [],
        "workingDirectory": None,
        "timeoutSeconds": 30,
        "maxOutputChars": 20_000,
        "maxPacketChars": 12_000,
        "allowedTopicIds": [],
        "allowedReviewLevels": ["full_text_grounded"],
        "allowLiveCommand": False,
        "requiresOperatorConfirmation": True,
        "requiresOperatorAcceptance": True,
        "notes": ["Configure an argument-list command locally before live provider use."],
    },
    {
        "providerId": "gemini-cli-example",
        "displayName": "Gemini CLI Example",
        "providerType": "command",
        "profileRole": "real_example",
        "executionSurface": "cli",
        "authMode": "external_cli_auth",
        "credentialPolicy": ["external_only", "env_presence_only", "no_token_storage", "no_cache_inspection"],
        "status": "provider_smoke_only",
        "proofArtifactRequired": True,
        "allowedCommands": [["gemini", "--version"], ["gemini", "--prompt", "Return exactly: provider-smoke-ok"]],
        "forbiddenCredentialPaths": list(DEFAULT_FORBIDDEN_CREDENTIAL_PATHS),
        "statusAfterProofCandidate": "live_verified_tiny",
        "researchOutputAllowed": False,
        "credentialBoundaryDescription": "Official CLI auth is operator-owned; repo may only check presence and run approved proof lanes.",
        "enabled": True,
        "defaultMode": "command_dry_run",
        "commandArgvTemplate": ["gemini", "--packet", "{packet_path}", "--output", "{output_path}"],
        "versionArgs": ["gemini", "--version"],
        "tinyPromptArgs": ["gemini", "--prompt", "Return exactly: provider-smoke-ok"],
        "workingDirectory": None,
        "timeoutSeconds": 60,
        "maxOutputChars": 20_000,
        "maxPacketChars": 12_000,
        "allowedTopicIds": [],
        "allowedReviewLevels": ["full_text_grounded"],
        "allowLiveCommand": False,
        "requiresOperatorConfirmation": True,
        "requiresOperatorAcceptance": True,
        "notes": ["Example only. Configure local authentication outside this repository and keep live execution disabled by default."],
    },
    {
        "providerId": "antigravity_sdk",
        "displayName": "Antigravity SDK Example",
        "providerType": "command",
        "profileRole": "real_example",
        "executionSurface": "sdk",
        "authMode": "google_adc",
        "credentialPolicy": ["env_presence_only", "no_token_storage"],
        "status": "configured_not_verified",
        "proofArtifactRequired": True,
        "allowedCommands": [],
        "forbiddenCredentialPaths": list(DEFAULT_FORBIDDEN_CREDENTIAL_PATHS),
        "statusAfterProofCandidate": "live_verified_tiny",
        "researchOutputAllowed": False,
        "credentialBoundaryDescription": "Antigravity SDK should use API key or Google ADC; SDK OAuth-native support is not assumed.",
        "enabled": True,
        "defaultMode": "command_dry_run",
        "commandArgvTemplate": [],
        "versionArgs": [],
        "tinyPromptArgs": [],
        "workingDirectory": None,
        "timeoutSeconds": 60,
        "maxOutputChars": 20_000,
        "maxPacketChars": 12_000,
        "allowedTopicIds": [],
        "allowedReviewLevels": ["full_text_grounded"],
        "allowLiveCommand": False,
        "requiresOperatorConfirmation": True,
        "requiresOperatorAcceptance": True,
        "notes": [
            "Example only. SDK auth must stay outside this repository; use API-key or Google ADC presence checks only.",
        ],
    },
    {
        "providerId": "antigravity_cli",
        "displayName": "Antigravity CLI External Auth Example",
        "providerType": "command",
        "profileRole": "real_example",
        "executionSurface": "cli",
        "authMode": "external_cli_auth",
        "credentialPolicy": ["external_only", "env_presence_only", "no_token_storage", "no_cache_inspection"],
        "status": "provider_smoke_only",
        "proofArtifactRequired": True,
        "allowedCommands": [["antigravity", "--version"], ["antigravity", "--prompt", "Return exactly: provider-smoke-ok"]],
        "forbiddenCredentialPaths": list(DEFAULT_FORBIDDEN_CREDENTIAL_PATHS),
        "statusAfterProofCandidate": "live_verified_tiny",
        "researchOutputAllowed": False,
        "credentialBoundaryDescription": "Official CLI login is operator-owned; repo must not inspect Antigravity local auth state.",
        "enabled": True,
        "defaultMode": "command_dry_run",
        "commandArgvTemplate": ["antigravity", "--packet", "{packet_path}", "--output", "{output_path}"],
        "versionArgs": ["antigravity", "--version"],
        "tinyPromptArgs": ["antigravity", "--prompt", "Return exactly: provider-smoke-ok"],
        "workingDirectory": None,
        "timeoutSeconds": 60,
        "maxOutputChars": 20_000,
        "maxPacketChars": 12_000,
        "allowedTopicIds": [],
        "allowedReviewLevels": ["full_text_grounded"],
        "allowLiveCommand": False,
        "requiresOperatorConfirmation": True,
        "requiresOperatorAcceptance": True,
        "notes": [
            "Example only. Antigravity CLI command shape is a conservative placeholder and must be verified locally before live use.",
            "Configure local authentication outside this repository and keep live execution disabled by default.",
        ],
    },
    {
        "providerId": "antigravity-cli-example",
        "displayName": "Antigravity CLI Example",
        "providerType": "command",
        "profileRole": "real_example",
        "executionSurface": "cli",
        "authMode": "external_cli_auth",
        "credentialPolicy": ["external_only", "env_presence_only", "no_token_storage", "no_cache_inspection"],
        "status": "provider_smoke_only",
        "proofArtifactRequired": True,
        "allowedCommands": [["antigravity", "--version"], ["antigravity", "--prompt", "Return exactly: provider-smoke-ok"]],
        "forbiddenCredentialPaths": list(DEFAULT_FORBIDDEN_CREDENTIAL_PATHS),
        "statusAfterProofCandidate": "live_verified_tiny",
        "researchOutputAllowed": False,
        "credentialBoundaryDescription": "Official CLI login is operator-owned; repo must not inspect Antigravity local auth state.",
        "enabled": True,
        "defaultMode": "command_dry_run",
        "commandArgvTemplate": ["antigravity", "--packet", "{packet_path}", "--output", "{output_path}"],
        "versionArgs": ["antigravity", "--version"],
        "tinyPromptArgs": ["antigravity", "--prompt", "Return exactly: provider-smoke-ok"],
        "workingDirectory": None,
        "timeoutSeconds": 60,
        "maxOutputChars": 20_000,
        "maxPacketChars": 12_000,
        "allowedTopicIds": [],
        "allowedReviewLevels": ["full_text_grounded"],
        "allowLiveCommand": False,
        "requiresOperatorConfirmation": True,
        "requiresOperatorAcceptance": True,
        "notes": [
            "Example only. Antigravity CLI command shape is a conservative placeholder and must be verified locally before live use.",
            "Configure local authentication outside this repository and keep live execution disabled by default.",
        ],
    },
    {
        "providerId": "claude-code-example",
        "displayName": "Claude Code Example",
        "providerType": "command",
        "profileRole": "real_example",
        "executionSurface": "cli",
        "authMode": "claude_code_login",
        "credentialPolicy": ["external_only", "no_token_storage", "no_cache_inspection"],
        "status": "policy_sensitive",
        "proofArtifactRequired": True,
        "allowedCommands": [["claude", "--version"], ["claude", "-p", "Return exactly: provider-smoke-ok"]],
        "forbiddenCredentialPaths": list(DEFAULT_FORBIDDEN_CREDENTIAL_PATHS),
        "statusAfterProofCandidate": "live_verified_tiny",
        "researchOutputAllowed": False,
        "credentialBoundaryDescription": "Claude Code login is operator-owned; subscription OAuth/session reuse remains policy-sensitive.",
        "enabled": True,
        "defaultMode": "command_dry_run",
        "commandArgvTemplate": ["claude", "-p", "@{packet_path}", "--output-format", "json", "--output", "{output_path}"],
        "versionArgs": ["claude", "--version"],
        "tinyPromptArgs": ["claude", "-p", "Return exactly: provider-smoke-ok"],
        "workingDirectory": None,
        "timeoutSeconds": 60,
        "maxOutputChars": 20_000,
        "maxPacketChars": 12_000,
        "allowedTopicIds": [],
        "allowedReviewLevels": ["full_text_grounded"],
        "allowLiveCommand": False,
        "requiresOperatorConfirmation": True,
        "requiresOperatorAcceptance": True,
        "notes": ["Example only. Configure local authentication outside this repository and keep live execution disabled by default."],
    },
    {
        "providerId": "codex-cli-example",
        "displayName": "Codex CLI Example",
        "providerType": "command",
        "profileRole": "real_example",
        "executionSurface": "cli",
        "authMode": "chatgpt_signin",
        "credentialPolicy": ["external_only", "no_token_storage", "no_cache_inspection"],
        "status": "configured_not_verified",
        "proofArtifactRequired": True,
        "allowedCommands": [["codex", "--version"]],
        "forbiddenCredentialPaths": list(DEFAULT_FORBIDDEN_CREDENTIAL_PATHS),
        "statusAfterProofCandidate": "live_verified_tiny",
        "researchOutputAllowed": False,
        "credentialBoundaryDescription": "Codex CLI/IDE auth is external; repo must not read Codex auth cache or credential store values.",
        "enabled": True,
        "defaultMode": "command_dry_run",
        "commandArgvTemplate": ["codex", "exec", "--sandbox", "read-only", "--ephemeral", "--skip-git-repo-check", "--color", "never", "-o", "{output_path}", "-"],
        "versionArgs": ["codex", "--version"],
        "tinyPromptArgs": ["codex", "exec", "--sandbox", "read-only", "--ephemeral", "--skip-git-repo-check", "--color", "never", "Return exactly: provider-smoke-ok"],
        "workingDirectory": None,
        "timeoutSeconds": 60,
        "maxOutputChars": 20_000,
        "maxPacketChars": 12_000,
        "allowedTopicIds": [],
        "allowedReviewLevels": ["full_text_grounded"],
        "allowLiveCommand": False,
        "requiresOperatorConfirmation": True,
        "requiresOperatorAcceptance": True,
        "notes": ["Example only. Configure local authentication outside this repository and keep live execution disabled by default."],
    },
    {
        "providerId": "codex_cli",
        "displayName": "Codex CLI External Auth Example",
        "providerType": "command",
        "profileRole": "real_example",
        "executionSurface": "cli",
        "authMode": "chatgpt_signin",
        "credentialPolicy": ["external_only", "no_token_storage", "no_cache_inspection"],
        "status": "configured_not_verified",
        "proofArtifactRequired": True,
        "allowedCommands": [["codex", "--version"]],
        "forbiddenCredentialPaths": list(DEFAULT_FORBIDDEN_CREDENTIAL_PATHS),
        "statusAfterProofCandidate": "live_verified_tiny",
        "researchOutputAllowed": False,
        "credentialBoundaryDescription": "Codex CLI/IDE auth is external; repo must not read Codex auth cache or credential store values.",
        "enabled": True,
        "defaultMode": "command_dry_run",
        "commandArgvTemplate": ["codex", "exec", "--sandbox", "read-only", "--ephemeral", "--skip-git-repo-check", "--color", "never", "-o", "{output_path}", "-"],
        "versionArgs": ["codex", "--version"],
        "tinyPromptArgs": ["codex", "exec", "--sandbox", "read-only", "--ephemeral", "--skip-git-repo-check", "--color", "never", "Return exactly: provider-smoke-ok"],
        "workingDirectory": None,
        "timeoutSeconds": 60,
        "maxOutputChars": 20_000,
        "maxPacketChars": 12_000,
        "allowedTopicIds": [],
        "allowedReviewLevels": ["full_text_grounded"],
        "allowLiveCommand": False,
        "requiresOperatorConfirmation": True,
        "requiresOperatorAcceptance": True,
        "notes": ["Example only. Configure local authentication outside this repository and keep live execution disabled by default."],
    },
    {
        "providerId": "claude_code",
        "displayName": "Claude Code External Auth Example",
        "providerType": "command",
        "profileRole": "real_example",
        "executionSurface": "cli",
        "authMode": "claude_code_login",
        "credentialPolicy": ["external_only", "no_token_storage", "no_cache_inspection"],
        "status": "policy_sensitive",
        "proofArtifactRequired": True,
        "allowedCommands": [["claude", "--version"], ["claude", "-p", "Return exactly: provider-smoke-ok"]],
        "forbiddenCredentialPaths": list(DEFAULT_FORBIDDEN_CREDENTIAL_PATHS),
        "statusAfterProofCandidate": "live_verified_tiny",
        "researchOutputAllowed": False,
        "credentialBoundaryDescription": "Claude Code login is operator-owned; subscription OAuth/session reuse remains policy-sensitive.",
        "enabled": True,
        "defaultMode": "command_dry_run",
        "commandArgvTemplate": ["claude", "-p", "@{packet_path}", "--output-format", "json", "--output", "{output_path}"],
        "versionArgs": ["claude", "--version"],
        "tinyPromptArgs": ["claude", "-p", "Return exactly: provider-smoke-ok"],
        "workingDirectory": None,
        "timeoutSeconds": 60,
        "maxOutputChars": 20_000,
        "maxPacketChars": 12_000,
        "allowedTopicIds": [],
        "allowedReviewLevels": ["full_text_grounded"],
        "allowLiveCommand": False,
        "requiresOperatorConfirmation": True,
        "requiresOperatorAcceptance": True,
        "notes": ["Example only. Configure local authentication outside this repository and keep live execution disabled by default."],
    },
    {
        "providerId": "claude_api",
        "displayName": "Claude API / Platform Example",
        "providerType": "command",
        "profileRole": "real_example",
        "executionSurface": "api",
        "authMode": "api_key",
        "credentialPolicy": ["env_presence_only", "no_token_storage"],
        "status": "configured_not_verified",
        "proofArtifactRequired": True,
        "allowedCommands": [],
        "forbiddenCredentialPaths": list(DEFAULT_FORBIDDEN_CREDENTIAL_PATHS),
        "statusAfterProofCandidate": "live_verified_tiny",
        "researchOutputAllowed": False,
        "credentialBoundaryDescription": "Claude API/platform integration should use env presence checks or supported cloud-provider auth; no key values in repo.",
        "enabled": True,
        "defaultMode": "command_dry_run",
        "commandArgvTemplate": [],
        "versionArgs": [],
        "tinyPromptArgs": [],
        "workingDirectory": None,
        "timeoutSeconds": 60,
        "maxOutputChars": 20_000,
        "maxPacketChars": 12_000,
        "allowedTopicIds": [],
        "allowedReviewLevels": ["full_text_grounded"],
        "allowLiveCommand": False,
        "requiresOperatorConfirmation": True,
        "requiresOperatorAcceptance": True,
        "notes": ["Example only. API/cloud-provider auth must stay outside this repository; use presence checks only."],
    },
)
DEFAULT_PROVIDER_CONFIGS = PROVIDER_PROFILE_EXAMPLES


def _snake_to_camel(raw: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "provider_id": "providerId",
        "display_name": "displayName",
        "provider_type": "providerType",
        "profile_role": "profileRole",
        "execution_surface": "executionSurface",
        "auth_mode": "authMode",
        "credential_policy": "credentialPolicy",
        "proof_artifact_required": "proofArtifactRequired",
        "allowed_commands": "allowedCommands",
        "forbidden_credential_paths": "forbiddenCredentialPaths",
        "status_after_proof_candidate": "statusAfterProofCandidate",
        "research_output_allowed": "researchOutputAllowed",
        "credential_boundary_description": "credentialBoundaryDescription",
        "operator_profile_secret_free": "operatorProfileSecretFree",
        "default_mode": "defaultMode",
        "command_argv_template": "commandArgvTemplate",
        "working_directory": "workingDirectory",
        "timeout_seconds": "timeoutSeconds",
        "max_output_chars": "maxOutputChars",
        "max_packet_chars": "maxPacketChars",
        "allowed_topic_ids": "allowedTopicIds",
        "allowed_review_levels": "allowedReviewLevels",
        "allow_live_command": "allowLiveCommand",
        "requires_operator_confirmation": "requiresOperatorConfirmation",
        "requires_operator_acceptance": "requiresOperatorAcceptance",
    }
    return {mapping.get(key, key): value for key, value in raw.items()}


def example_provider_profiles() -> list[dict[str, Any]]:
    return [deepcopy(profile) for profile in PROVIDER_PROFILE_EXAMPLES if profile.get("providerId") != "command"]


def provider_profile_by_id(provider_id: str | None) -> dict[str, Any] | None:
    normalized = str(provider_id or "").strip().lower()
    if not normalized:
        return None
    for profile in PROVIDER_PROFILE_EXAMPLES:
        if str(profile.get("providerId") or "").lower() == normalized:
            return deepcopy(profile)
    return None


def _secret_paths(value: Any, prefix: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            text_key = str(key)
            normalized = text_key.lower().replace("-", "_")
            path = f"{prefix}.{text_key}" if prefix else text_key
            if normalized not in SAFE_CREDENTIAL_METADATA_KEYS and any(part in normalized for part in SECRET_KEY_PARTS):
                paths.append(path)
            paths.extend(_secret_paths(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.extend(_secret_paths(child, f"{prefix}[{index}]"))
    return paths


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw_items = value
    elif isinstance(value, str):
        raw_items = value.splitlines() if "\n" in value else value.split(",")
    else:
        raw_items = [value]
    items: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = str(item or "").strip()
        key = text.lower()
        if text and key not in seen:
            items.append(text)
            seen.add(key)
    return items


def _command_list_collection(value: Any) -> tuple[list[list[str]], str | None]:
    if value in (None, ""):
        return [], None
    if not isinstance(value, list):
        return [], "allowed_commands must be a list of argv lists."
    commands: list[list[str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, list):
            return [], f"allowed_commands[{index}] must be an argv list."
        args = [str(arg).strip() for arg in item if str(arg).strip()]
        if args:
            commands.append(args)
    return commands, None


def _normalized_path_text(value: str) -> str:
    return str(value or "").strip().lower().replace("\\", "/")


def _contains_forbidden_credential_reference(values: list[str], forbidden_paths: list[str]) -> str | None:
    normalized_forbidden = [_normalized_path_text(path) for path in forbidden_paths if str(path or "").strip()]
    for value in values:
        normalized = _normalized_path_text(value)
        if not normalized:
            continue
        for forbidden in normalized_forbidden:
            if forbidden and forbidden in normalized:
                return value
    return None


def _contains_local_profile_path_arg(args: list[str]) -> bool:
    profile_flags = {"--profile", "--profile-json", "--config", "--config-file", "--auth-file", "--credentials"}
    for index, arg in enumerate(args):
        text = str(arg or "").strip()
        normalized = text.lower().replace("-", "_")
        if text in profile_flags:
            return True
        if any(part in normalized for part in ("profile_json", "auth_file", "credential_file")):
            return True
        if index and str(args[index - 1]).strip() in profile_flags:
            return True
    return False


def _coerce_int(value: Any, fallback: int) -> tuple[int, bool]:
    if value is None or isinstance(value, bool):
        return fallback, value is not None
    try:
        number = int(round(float(str(value).replace(",", ""))))
    except (TypeError, ValueError):
        return fallback, True
    return number, False


def _bounded_int(value: Any, fallback: int, *, minimum: int, maximum: int) -> tuple[int, bool]:
    number, invalid = _coerce_int(value, fallback)
    bounded = min(max(number, minimum), maximum)
    return bounded, invalid or bounded != number


def _command_args(value: Any) -> tuple[list[str], str | None]:
    if value in (None, ""):
        return [], None
    if not isinstance(value, list):
        return [], "command_argv_template must be an argument list, not a shell string."
    args = [str(item).strip() for item in value if str(item).strip()]
    return args, None


def _contains_secret_like_arg(args: list[str]) -> bool:
    for arg in args:
        normalized = arg.lower().replace("-", "_")
        if any(part in normalized for part in SECRET_KEY_PARTS):
            return True
        if normalized.startswith("sk-") or normalized.startswith("ya29."):
            return True
    return False


def _contains_shell_operator(arg: str) -> bool:
    text = str(arg or "").strip()
    if text in SHELL_OPERATOR_ARGS:
        return True
    if any(token in text for token in SHELL_OPERATOR_TOKENS):
        return True
    return bool(re.search(r"(^|\s)[|;&<>]($|\s)", text))


def _render_command_template_arg(arg: str, values: dict[str, str]) -> str:
    rendered = str(arg)
    for placeholder, value in values.items():
        rendered = rendered.replace(f"{{{placeholder}}}", value)
    return rendered


def validate_command_template(
    command_template: Any,
    *,
    packet_path: str = "output/provider-manual-run/packets/paper.json",
    output_path: str = "output/provider-manual-run/provider-output.jsonl",
    provider_run_id: str = "manual-provider-run",
    topic_id: str = "large-language-models",
) -> dict[str, Any]:
    """Validate and render a local command-provider template without executing it."""

    args, command_error = _command_args(command_template)
    reason_codes: list[str] = []
    warnings: list[str] = []
    if command_error:
        reason_codes.append("command_template_must_be_argument_list")
    if not args and not command_error:
        reason_codes.append("missing_command_template")

    unsupported_placeholders: list[str] = []
    for arg in args:
        for placeholder in PLACEHOLDER_PATTERN.findall(arg):
            if placeholder not in ALLOWED_COMMAND_TEMPLATE_PLACEHOLDERS:
                unsupported_placeholders.append(placeholder)
    reason_codes.extend(f"unsupported_placeholder:{placeholder}" for placeholder in sorted(set(unsupported_placeholders)))

    if any(_contains_shell_operator(arg) for arg in args):
        reason_codes.append("shell_operator_not_allowed")
    if _contains_secret_like_arg(args):
        reason_codes.append("secret_like_command_arg")
    if len(args) > 32:
        warnings.append("command_template has more than 32 arguments; preview is truncated in dashboard readiness.")

    values = {
        "packet_path": str(packet_path),
        "output_path": str(output_path),
        "provider_run_id": str(provider_run_id),
        "topic_id": str(topic_id),
    }
    rendered = [_render_command_template_arg(arg, values) for arg in args]
    return {
        "valid": not reason_codes,
        "renderedPreview": rendered,
        "redactedPreview": _redacted_command_preview(rendered, limit=None),
        "reasonCodes": list(dict.fromkeys(reason_codes)),
        "warnings": warnings,
        "allowedPlaceholders": sorted(ALLOWED_COMMAND_TEMPLATE_PLACEHOLDERS),
    }


def _normalize_provider_config(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    raw = _snake_to_camel(payload or {})
    warnings: list[str] = []
    errors: list[str] = []

    provider_type = str(raw.get("providerType") or "stub").strip().lower()
    if provider_type not in PROVIDER_TYPES:
        errors.append("provider_type must be stub or command.")
        provider_type = "stub"
    fallback_id = "command" if provider_type == "command" else "stub"
    provider_id = str(raw.get("providerId") or fallback_id).strip().lower()
    display_name = str(raw.get("displayName") or provider_id).strip() or provider_id
    profile_role = str(raw.get("profileRole") or "").strip().lower()
    if not profile_role:
        if provider_type == "stub":
            profile_role = "stub"
        elif provider_id == "fixture-command":
            profile_role = "fixture"
        elif provider_id.endswith("-example"):
            profile_role = "real_example"
        else:
            profile_role = "generic_command"
    execution_surface = str(raw.get("executionSurface") or ("cli" if provider_type == "command" else "sdk")).strip().lower()
    auth_mode_default = {
        "api": "api_key",
        "cli": "external_cli_auth",
        "mcp": "oidc",
        "sdk": "none" if provider_type == "stub" else "google_adc",
    }.get(execution_surface, "unknown")
    auth_mode = str(raw.get("authMode") or auth_mode_default).strip().lower()
    credential_policy_missing = raw.get("credentialPolicy") in (None, "", [])
    credential_policy = _string_list(raw.get("credentialPolicy"))
    if not credential_policy and provider_type == "stub":
        credential_policy = ["no_token_storage"]
    profile_status = str(raw.get("status") or "configured_not_verified").strip().lower()
    proof_artifact_required = bool(raw.get("proofArtifactRequired", profile_role == "real_example" or execution_surface in {"api", "cli", "mcp"}))
    allowed_commands, allowed_commands_error = _command_list_collection(raw.get("allowedCommands"))
    forbidden_credential_paths = _string_list(raw.get("forbiddenCredentialPaths") or list(DEFAULT_FORBIDDEN_CREDENTIAL_PATHS))
    status_after_proof_candidate = str(raw.get("statusAfterProofCandidate") or "live_verified_tiny").strip().lower()
    requested_research_output_allowed = bool(raw.get("researchOutputAllowed", False))
    credential_boundary_description = str(raw.get("credentialBoundaryDescription") or "").strip()
    operator_profile_secret_free = bool(raw.get("operatorProfileSecretFree", False))
    default_mode = str(raw.get("defaultMode") or ("command_dry_run" if provider_type == "command" else "stub")).strip()
    enabled = bool(raw.get("enabled", True))
    command_args, command_error = _command_args(raw.get("commandArgvTemplate"))
    version_args, version_args_error = _command_args(raw.get("versionArgs"))
    tiny_prompt_args, tiny_prompt_args_error = _command_args(raw.get("tinyPromptArgs"))
    working_directory = raw.get("workingDirectory")
    if working_directory is not None:
        working_directory = str(working_directory).strip() or None
    timeout_seconds, timeout_adjusted = _bounded_int(raw.get("timeoutSeconds"), 30, minimum=MIN_TIMEOUT_SECONDS, maximum=MAX_TIMEOUT_SECONDS)
    max_output_chars, output_adjusted = _bounded_int(raw.get("maxOutputChars"), 20_000, minimum=MIN_OUTPUT_CHARS, maximum=MAX_OUTPUT_CHARS)
    max_packet_chars, packet_adjusted = _bounded_int(raw.get("maxPacketChars"), 12_000, minimum=MIN_PACKET_CHARS, maximum=MAX_PACKET_CHARS)
    allow_live_command = bool(raw.get("allowLiveCommand", False))
    requires_operator_confirmation = bool(raw.get("requiresOperatorConfirmation", True))
    requires_operator_acceptance = bool(raw.get("requiresOperatorAcceptance", provider_type == "command" and profile_role != "fixture"))

    allowed_topic_ids = [item.lower() for item in _string_list(raw.get("allowedTopicIds"))]
    allowed_review_levels = _string_list(raw.get("allowedReviewLevels") or ["full_text_grounded"])
    notes = _string_list(raw.get("notes"))

    if not PROVIDER_ID_PATTERN.fullmatch(provider_id):
        errors.append("provider_id must be stable and safe for file/URL usage.")
    if execution_surface not in EXECUTION_SURFACES:
        errors.append("execution_surface must be sdk, cli, api, or mcp.")
        execution_surface = "cli" if provider_type == "command" else "sdk"
    if auth_mode not in AUTH_MODES:
        errors.append("auth_mode must be one of the supported provider auth modes.")
        auth_mode = "unknown"
    invalid_credential_policy = [policy for policy in credential_policy if policy not in CREDENTIAL_POLICIES]
    if invalid_credential_policy:
        errors.append(f"credential_policy contains unsupported values: {', '.join(invalid_credential_policy)}")
        credential_policy = [policy for policy in credential_policy if policy in CREDENTIAL_POLICIES]
    if provider_type != "stub" and credential_policy_missing:
        errors.append("credential_policy is required for external provider profiles.")
    if provider_type != "stub" and "no_token_storage" not in credential_policy:
        errors.append("credential_policy must include no_token_storage for external provider profiles.")
    if "operator_profile" in credential_policy and not operator_profile_secret_free:
        errors.append("operator_profile credential_policy requires operator_profile_secret_free=true.")
    if profile_status not in PROFILE_STATUSES:
        errors.append("status must be configured_not_verified, provider_smoke_only, non_live_available, unsupported, policy_sensitive, live_verified_tiny, or live_verified.")
        profile_status = "configured_not_verified"
    if profile_status in {"live_verified_tiny", "live_verified"}:
        errors.append("status cannot be live_verified_tiny or live_verified without an accepted proof artifact.")
        profile_status = "configured_not_verified"
    if status_after_proof_candidate not in STATUS_AFTER_PROOF_CANDIDATES:
        errors.append("status_after_proof_candidate must be live_verified_tiny or live_verified.")
        status_after_proof_candidate = "live_verified_tiny"
    if (
        status_after_proof_candidate in STATUS_AFTER_PROOF_CANDIDATES
        and not proof_artifact_required
        and provider_type != "stub"
        and profile_role != "fixture"
    ):
        errors.append("proof_artifact_required must be true when a live status is a proof candidate.")
    if requested_research_output_allowed:
        errors.append("research_output_allowed cannot be enabled by provider profile schema without accepted live proof.")
    if not credential_boundary_description and provider_type != "stub":
        errors.append("credential_boundary_description is required for external provider profiles.")
    if allowed_commands_error:
        errors.append(allowed_commands_error)
    if profile_role not in PROFILE_ROLES:
        errors.append("profile_role must be stub, generic_command, fixture, or real_example.")
        profile_role = "generic_command" if provider_type == "command" else "stub"
    if provider_type == "stub" and profile_role != "stub":
        errors.append("stub providers must use profile_role=stub.")
        profile_role = "stub"
    if provider_type == "command" and profile_role == "stub":
        errors.append("command providers cannot use profile_role=stub.")
        profile_role = "generic_command"
    if default_mode not in DEFAULT_MODES:
        errors.append("default_mode must be stub or command_dry_run.")
        default_mode = "command_dry_run" if provider_type == "command" else "stub"
    if provider_type == "command" and default_mode != "command_dry_run":
        errors.append("command providers must default to command_dry_run.")
    if provider_type == "stub" and default_mode != "stub":
        errors.append("stub providers must default to stub.")
    if command_error:
        errors.append(command_error)
    if version_args_error:
        errors.append(f"version_args {version_args_error}")
    if tiny_prompt_args_error:
        errors.append(f"tiny_prompt_args {tiny_prompt_args_error}")
    if provider_type == "command" and execution_surface == "cli" and profile_role != "generic_command" and not command_args and not command_error:
        errors.append("command providers require command_argv_template.")
    if provider_type == "command" and execution_surface != "cli" and command_args:
        warnings.append("non-CLI provider profile includes command_argv_template; command execution remains disabled by default.")
    if provider_type == "stub" and command_args:
        warnings.append("stub provider command_argv_template is ignored.")
        command_args = []
    if provider_type == "command" and command_args:
        template_validation = validate_command_template(command_args)
        if not template_validation["valid"]:
            for reason in template_validation["reasonCodes"]:
                if reason == "shell_operator_not_allowed":
                    errors.append("command_argv_template contains shell-like operators; use an argument list without shell composition.")
                elif reason == "secret_like_command_arg":
                    errors.append("command_argv_template contains secret-like values; credentials must not be stored.")
                elif reason.startswith("unsupported_placeholder:"):
                    placeholder = reason.split(":", 1)[1]
                    errors.append(f"command_argv_template contains unsupported placeholder: {placeholder}")
                elif reason == "missing_command_template":
                    errors.append("command providers require command_argv_template.")
                else:
                    errors.append(f"command_argv_template is invalid: {reason}")
        warnings.extend(str(warning) for warning in template_validation.get("warnings", []) if warning)
    if _contains_secret_like_arg(command_args):
        errors.append("command_argv_template contains secret-like values; credentials must not be stored.")
    if _contains_secret_like_arg(version_args):
        errors.append("version_args contains secret-like values; credentials must not be stored.")
    if _contains_secret_like_arg(tiny_prompt_args):
        errors.append("tiny_prompt_args contains secret-like values; credentials must not be stored.")
    if _contains_local_profile_path_arg(command_args) and "operator_profile" not in credential_policy:
        errors.append("profile/config path command arguments require operator_profile credential_policy.")
    if forbidden_hit := _contains_forbidden_credential_reference(
        [*command_args, *(str(working_directory).strip() for _ in [None] if working_directory)],
        forbidden_credential_paths,
    ):
        errors.append(f"provider command or working directory references a forbidden credential path: {forbidden_hit}")
    if _contains_forbidden_credential_reference([*version_args, *tiny_prompt_args], forbidden_credential_paths):
        errors.append("version_args and tiny_prompt_args must not reference forbidden credential paths.")
    for allowed_command in allowed_commands:
        template_validation = validate_command_template(allowed_command)
        if not template_validation["valid"]:
            errors.append("allowed_commands entries must be safe argv lists without shell, secret, or unsupported placeholder content.")
            break
        if _contains_forbidden_credential_reference(allowed_command, forbidden_credential_paths):
            errors.append("allowed_commands must not reference forbidden credential paths.")
            break
    if not allowed_commands and command_args:
        allowed_commands = [command_args]
    if bool(raw.get("shell") or raw.get("shellTrue") or raw.get("useShell")):
        errors.append("shell=True style provider configuration is not allowed.")
    invalid_topic_ids = [topic_id for topic_id in allowed_topic_ids if not TOPIC_ID_PATTERN.fullmatch(topic_id)]
    if invalid_topic_ids:
        errors.append(f"allowed_topic_ids contains invalid topic IDs: {', '.join(invalid_topic_ids)}")
    invalid_review_levels = [level for level in allowed_review_levels if level not in REVIEW_LEVELS]
    if invalid_review_levels:
        errors.append(f"allowed_review_levels contains unsupported levels: {', '.join(invalid_review_levels)}")
        allowed_review_levels = [level for level in allowed_review_levels if level in REVIEW_LEVELS] or ["full_text_grounded"]
    if timeout_adjusted:
        warnings.append("timeout_seconds was bounded to the safe range.")
    if output_adjusted:
        warnings.append("max_output_chars was bounded to the safe range.")
    if packet_adjusted:
        warnings.append("max_packet_chars was bounded to the safe range.")
    if allow_live_command and not requires_operator_confirmation:
        warnings.append("live command mode still requires explicit CLI operator confirmation.")
        requires_operator_confirmation = True
    if profile_role == "real_example" and not requires_operator_acceptance:
        warnings.append("real provider examples always require explicit operator acceptance.")
        requires_operator_acceptance = True
    if profile_status in {"policy_sensitive", "unsupported"} and allow_live_command:
        warnings.append("policy-sensitive or unsupported provider profiles cannot enable live command mode.")
        allow_live_command = False

    config = {
        "providerId": provider_id,
        "displayName": display_name,
        "providerType": provider_type,
        "profileRole": profile_role,
        "executionSurface": execution_surface,
        "authMode": auth_mode,
        "credentialPolicy": credential_policy,
        "status": profile_status,
        "proofArtifactRequired": proof_artifact_required,
        "allowedCommands": allowed_commands,
        "forbiddenCredentialPaths": forbidden_credential_paths,
        "statusAfterProofCandidate": status_after_proof_candidate,
        "researchOutputAllowed": False,
        "credentialBoundaryDescription": credential_boundary_description,
        "operatorProfileSecretFree": operator_profile_secret_free,
        "enabled": enabled,
        "defaultMode": default_mode,
        "commandArgvTemplate": command_args,
        "versionArgs": version_args,
        "tinyPromptArgs": tiny_prompt_args,
        "workingDirectory": working_directory,
        "timeoutSeconds": timeout_seconds,
        "maxOutputChars": max_output_chars,
        "maxPacketChars": max_packet_chars,
        "allowedTopicIds": allowed_topic_ids,
        "allowedReviewLevels": allowed_review_levels,
        "allowLiveCommand": allow_live_command,
        "requiresOperatorConfirmation": requires_operator_confirmation,
        "requiresOperatorAcceptance": requires_operator_acceptance,
        "notes": notes,
        "validationWarnings": [],
    }
    config["validationWarnings"] = list(dict.fromkeys(warnings))
    return config, list(dict.fromkeys(warnings)), list(dict.fromkeys(errors))


def _config_hash(config: dict[str, Any]) -> str:
    stable = {key: value for key, value in config.items() if key != "validationWarnings"}
    payload = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_provider_configuration(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    raw_payload = payload or {}
    config, warnings, errors = _normalize_provider_config(raw_payload)
    secret_paths = _secret_paths(raw_payload)
    if secret_paths:
        errors.append(f"provider secrets or credentials are not allowed in provider configuration: {', '.join(secret_paths)}")
    return {
        "providerId": config["providerId"],
        "validationStatus": "invalid" if errors else "valid",
        "providerConfiguration": config,
        "warnings": list(dict.fromkeys([*warnings, *config.get("validationWarnings", [])])),
        "errors": list(dict.fromkeys(errors)),
        "configHash": _config_hash(config),
    }


def _command_exists(command: list[str]) -> bool:
    if not command:
        return False
    executable = command[0]
    if any(separator in executable for separator in ("/", "\\")):
        return Path(executable).exists()
    return shutil.which(executable) is not None


def _redacted_command_preview(command: list[str], *, limit: int | None = 8) -> list[str]:
    preview: list[str] = []
    args = command if limit is None else command[:limit]
    redact_next = False
    for arg in args:
        normalized = arg.lower().replace("-", "_")
        if redact_next:
            preview.append("<redacted-path>")
            redact_next = False
            continue
        if arg in {"--profile", "--profile-json", "--config", "--config-file", "--auth-file", "--credentials"}:
            preview.append(arg)
            redact_next = True
        elif any(part in normalized for part in SECRET_KEY_PARTS) or len(arg) > 120:
            preview.append("<redacted>")
        elif re.match(r"^[a-zA-Z]:[\\/]", arg) or arg.startswith("/") or arg.startswith("~"):
            preview.append("<redacted-path>")
        else:
            preview.append(arg)
    return preview


def canonical_provider_alias(provider_id: str | None) -> str:
    normalized = str(provider_id or "").strip().lower().replace("-", "_")
    if not normalized:
        return ""
    return REAL_PROVIDER_ALIASES.get(normalized, normalized)


def _canonical_status_payload(
    *,
    config: dict[str, Any],
    provider_type: str,
    profile_role: str,
    readiness_status: str,
    reason_codes: list[str],
    safe_modes: list[str],
    live_allowed: bool,
) -> dict[str, Any]:
    provider_id = str(config.get("providerId") or "")
    provider_alias = canonical_provider_alias(provider_id)
    command = list(config.get("commandArgvTemplate") or [])
    profile_status = str(config.get("status") or "configured_not_verified")
    evidence = [
        f"profileRole={profile_role}",
        f"executionSurface={config.get('executionSurface')}",
        f"authMode={config.get('authMode')}",
        f"profileStatus={profile_status}",
        f"readinessStatus={readiness_status}",
        f"allowLiveCommand={bool(config.get('allowLiveCommand'))}",
        "liveProof=none",
    ]
    if command:
        evidence.append(f"binaryCandidate={command[0]}")

    binary_status = "not_applicable"
    dry_run_status = "not_applicable"
    proof_level = "not_verified"
    live_gate_status = "disabled"
    setup_required = False
    operator_action_required = False

    if readiness_status == "unsafe_config":
        canonical_status = "unsafe_to_probe"
        routing_label = "not_allowed_for_research_output"
        status_message = "Unsafe provider configuration; blocked"
        binary_status = "unknown"
        dry_run_status = "blocked"
        proof_level = "unsafe_config"
        operator_action_required = True
    elif readiness_status == "disabled":
        canonical_status = "not_supported"
        routing_label = "not_allowed_for_research_output"
        status_message = "Provider disabled"
        proof_level = "disabled"
        live_gate_status = "not_applicable"
    elif profile_status == "policy_sensitive":
        canonical_status = "policy_sensitive"
        routing_label = "not_allowed_for_research_output"
        status_message = "Policy-sensitive provider auth boundary; proof and operator policy gate required"
        binary_status = "unknown"
        dry_run_status = "blocked"
        proof_level = "policy_sensitive"
        live_gate_status = "live_proof_missing"
        operator_action_required = True
    elif profile_status == "unsupported":
        canonical_status = "unsupported"
        routing_label = "not_allowed_for_research_output"
        status_message = "Provider auth surface is unsupported by the current schema"
        proof_level = "unsupported"
        live_gate_status = "not_applicable"
        operator_action_required = True
    elif provider_type == "stub" or profile_role == "fixture":
        canonical_status = "mock_only"
        routing_label = "not_allowed_for_research_output"
        status_message = "Local mock/fixture only; not allowed for research output"
        proof_level = "mock_only"
        dry_run_status = "available"
    elif readiness_status == "not_configured":
        canonical_status = "docs_only" if profile_role == "generic_command" else "configured_but_not_available"
        routing_label = "setup_required"
        status_message = "Setup required: command template missing"
        binary_status = "missing"
        dry_run_status = "blocked"
        proof_level = "config_missing"
        setup_required = True
        operator_action_required = True
    elif readiness_status == "command_missing":
        canonical_status = "environment_blocked_binary_missing" if profile_role == "real_example" else "configured_but_not_available"
        routing_label = "setup_required"
        status_message = "Setup required: binary missing"
        binary_status = "missing"
        dry_run_status = "blocked"
        proof_level = "binary_missing"
        setup_required = True
        operator_action_required = True
    elif profile_role == "real_example" and str(config.get("executionSurface") or "") == "cli":
        canonical_status = "non_live_available"
        routing_label = "provider_smoke_only"
        status_message = "Non-live available; live proof missing"
        binary_status = "found"
        dry_run_status = "available"
        proof_level = "cli_available"
        live_gate_status = "live_proof_missing"
        operator_action_required = bool(config.get("requiresOperatorAcceptance"))
    elif readiness_status == "dry_run_only":
        canonical_status = "configured_not_verified"
        routing_label = "provider_smoke_only"
        status_message = "Dry-run available; live proof missing; research output blocked"
        binary_status = "found"
        dry_run_status = "available"
        proof_level = "dry_run_available"
        operator_action_required = True
    elif live_allowed:
        canonical_status = "configured_not_verified"
        routing_label = "live_proof_required"
        status_message = "Live command setting exists; project live proof missing"
        binary_status = "found"
        dry_run_status = "available"
        proof_level = "live_gate_configured_without_project_proof"
        operator_action_required = True
    else:
        canonical_status = "configured_but_not_available"
        routing_label = "not_allowed_for_research_output"
        status_message = "Provider is not available for research output"
        binary_status = "unknown"
        dry_run_status = "blocked"

    if "provider_command_missing" in reason_codes:
        evidence.append("binaryDiscovery=missing")
    elif binary_status == "found":
        evidence.append("binaryDiscovery=found")
    if safe_modes:
        evidence.append(f"safeModes={','.join(safe_modes)}")

    return {
        "profileId": provider_id,
        "providerAlias": provider_alias or provider_id,
        "executionSurface": str(config.get("executionSurface") or ""),
        "authMode": str(config.get("authMode") or ""),
        "credentialPolicy": list(config.get("credentialPolicy") or []),
        "status": profile_status,
        "proofArtifactRequired": bool(config.get("proofArtifactRequired", False)),
        "allowedCommands": list(config.get("allowedCommands") or []),
        "forbiddenCredentialPaths": list(config.get("forbiddenCredentialPaths") or []),
        "statusAfterProofCandidate": str(config.get("statusAfterProofCandidate") or ""),
        "credentialBoundaryDescription": str(config.get("credentialBoundaryDescription") or ""),
        "canonicalStatus": canonical_status,
        "proofLevel": proof_level,
        "binaryStatus": binary_status,
        "versionStatus": "not_checked",
        "dryRunStatus": dry_run_status,
        "liveProofStatus": "not_verified",
        "liveGateStatus": live_gate_status,
        "routingLabel": routing_label,
        "researchOutputAllowed": False,
        "reportGenerationAllowed": False,
        "orchestrationAllowed": False,
        "setupRequired": setup_required,
        "operatorActionRequired": operator_action_required,
        "statusMessage": status_message,
        "evidence": list(dict.fromkeys(evidence)),
    }


def _readiness_item_for_provider(
    provider_id: str,
    *,
    provider_readiness_summary: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    wanted = canonical_provider_alias(provider_id)
    summary = provider_readiness_summary or build_provider_readiness_summary()
    for item in summary.get("items") or []:
        if not isinstance(item, dict):
            continue
        aliases = {
            str(item.get("providerId") or ""),
            str(item.get("profileId") or ""),
            str(item.get("providerAlias") or ""),
        }
        if any(canonical_provider_alias(alias) == wanted for alias in aliases):
            return item
    return None


def build_provider_routing_decision(
    provider_id: str,
    *,
    purpose: str = "research_output",
    provider_readiness_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a fail-closed routing decision for real provider use."""

    item = _readiness_item_for_provider(provider_id, provider_readiness_summary=provider_readiness_summary)
    alias = canonical_provider_alias(provider_id)
    if not item:
        return {
            "providerId": provider_id,
            "providerAlias": alias,
            "purpose": purpose,
            "allowed": False,
            "canonicalStatus": "not_supported",
            "routingLabel": "not_allowed_for_research_output",
            "reasonCodes": ["provider_not_supported", "research_output_blocked"],
            "message": "Provider is not supported by the current routing policy.",
        }

    purpose_key = str(purpose or "research_output").strip().lower()
    allowed_field = {
        "research_output": "researchOutputAllowed",
        "research_synthesis": "researchOutputAllowed",
        "report_generation": "reportGenerationAllowed",
        "full_text_review": "researchOutputAllowed",
        "orchestration": "orchestrationAllowed",
    }.get(purpose_key, "researchOutputAllowed")
    allowed = bool(item.get(allowed_field))
    reason_codes = list(item.get("reasonCodes") or [])
    if not allowed:
        reason_codes.extend(
            [
                str(item.get("routingLabel") or "not_allowed_for_research_output"),
                "research_output_blocked" if allowed_field == "researchOutputAllowed" else f"{purpose_key}_blocked",
                "live_proof_missing" if item.get("canonicalStatus") == "non_live_available" else "",
                "provider_binary_missing" if item.get("canonicalStatus") == "environment_blocked_binary_missing" else "",
            ]
        )
    return {
        "providerId": str(item.get("providerId") or provider_id),
        "providerAlias": str(item.get("providerAlias") or alias),
        "purpose": purpose_key,
        "allowed": allowed,
        "canonicalStatus": str(item.get("canonicalStatus") or "not_supported"),
        "routingLabel": str(item.get("routingLabel") or "not_allowed_for_research_output"),
        "reasonCodes": list(dict.fromkeys(reason for reason in reason_codes if reason)),
        "message": str(item.get("statusMessage") or "Provider routing blocked."),
    }


def build_provider_readiness_summary(
    *,
    configs: list[dict[str, Any]] | None = None,
    command_exists: Callable[[list[str]], bool] | None = None,
) -> dict[str, Any]:
    exists = command_exists or _command_exists
    items: list[dict[str, Any]] = []
    warnings: list[str] = []
    for raw_config in configs or [deepcopy(config) for config in DEFAULT_PROVIDER_CONFIGS]:
        validation = validate_provider_configuration(raw_config)
        config = validation["providerConfiguration"]
        provider_type = config["providerType"]
        profile_role = config.get("profileRole") or ("stub" if provider_type == "stub" else "generic_command")
        execution_surface = str(config.get("executionSurface") or ("cli" if provider_type == "command" else "sdk"))
        profile_status = str(config.get("status") or "configured_not_verified")
        reason_codes: list[str] = []
        item_warnings = list(validation.get("warnings") or [])
        live_allowed = bool(config.get("allowLiveCommand") and config.get("requiresOperatorConfirmation"))
        operator_acceptance_required = bool(config.get("requiresOperatorAcceptance"))
        safe_modes = ["stub"] if provider_type == "stub" else ["command_dry_run"]
        readiness_status = "ready"

        if not config["enabled"]:
            readiness_status = "disabled"
            reason_codes.append("provider_disabled")
            safe_modes = []
            live_allowed = False
        elif validation["validationStatus"] == "invalid" and config.get("commandArgvTemplate"):
            readiness_status = "unsafe_config"
            reason_codes.extend(validation.get("errors") or ["invalid_provider_configuration"])
            live_allowed = False
        elif validation["validationStatus"] == "invalid":
            readiness_status = "unsafe_config"
            reason_codes.extend(validation.get("errors") or ["invalid_provider_configuration"])
            live_allowed = False
        elif profile_status in {"policy_sensitive", "unsupported"}:
            readiness_status = "dry_run_only"
            reason_codes.append(profile_status)
            live_allowed = False
            safe_modes = []
            item_warnings.append("Provider auth boundary is fail-closed until an accepted policy/proof lane changes it.")
        elif provider_type == "stub":
            readiness_status = "ready_stub"
            live_allowed = False
        elif profile_role == "fixture":
            readiness_status = "ready_fixture"
            safe_modes = ["command_dry_run", "fixture_command"]
            live_allowed = False
            item_warnings.append("Fixture command is local/synthetic; dashboard remains preview-only.")
        else:
            command = list(config.get("commandArgvTemplate") or [])
            if not command and execution_surface != "cli":
                readiness_status = "dry_run_only"
                reason_codes.append("external_auth_surface_configured_not_verified")
                live_allowed = False
                safe_modes = ["metadata_only"]
                item_warnings.append("External SDK/API/MCP surface is metadata-only until a proof artifact exists.")
            elif not command:
                readiness_status = "not_configured"
                reason_codes.append("missing_command_template")
                live_allowed = False
                item_warnings.append("Command provider has no argument-list command configured.")
            elif not exists(command):
                readiness_status = "command_missing"
                reason_codes.append("provider_command_missing")
                live_allowed = False
                item_warnings.append("Command executable was not found; dry-run previews remain available.")
            elif profile_role == "real_example" and not live_allowed:
                readiness_status = "example_profile_configured"
                reason_codes.append("live_command_disabled")
                live_allowed = False
                item_warnings.append("Example profile is configured for preview; live execution remains disabled by default.")
            elif profile_role == "real_example" and operator_acceptance_required:
                readiness_status = "operator_acceptance_required"
                reason_codes.append("operator_acceptance_required")
                safe_modes = ["command_dry_run", "live_command_requires_acceptance"]
                item_warnings.append("Live command execution requires explicit CLI operator acceptance.")
            elif not live_allowed:
                readiness_status = "dry_run_only"
                reason_codes.append("live_command_not_allowed")
                item_warnings.append("Live command execution is disabled; only dry-run previews are available.")
            else:
                readiness_status = "ready"
                safe_modes = ["command_dry_run", "live_command"]
                item_warnings.append("Dashboard remains preview-only even when live command mode is allowed in local config.")

        item = {
            "providerId": config["providerId"],
            "displayName": config["displayName"],
            "providerType": provider_type,
            "profileRole": profile_role,
            "executionSurface": config["executionSurface"],
            "authMode": config["authMode"],
            "credentialPolicy": list(config.get("credentialPolicy") or []),
            "status": config["status"],
            "proofArtifactRequired": bool(config["proofArtifactRequired"]),
            "allowedCommands": list(config.get("allowedCommands") or []),
            "forbiddenCredentialPaths": list(config.get("forbiddenCredentialPaths") or []),
            "statusAfterProofCandidate": config["statusAfterProofCandidate"],
            "credentialBoundaryDescription": config["credentialBoundaryDescription"],
            "enabled": bool(config["enabled"]),
            "readinessStatus": readiness_status,
            "reasonCodes": list(dict.fromkeys(reason_codes)),
            "safeModesAvailable": safe_modes,
            "liveAllowed": live_allowed,
            "operatorAcceptanceRequired": operator_acceptance_required,
            "warnings": list(dict.fromkeys(item_warnings)),
            "configuration": config,
        }
        bridge_status = provider_auth_bridge.evaluate_provider_auth_bridge(config)
        bridge_payload = bridge_status.to_dict()
        bridge_reason_codes = [str(issue.get("code") or "") for issue in bridge_payload.get("issues") or []]
        item["commandPreviewRedacted"] = list(bridge_payload["commandPreview"]["argvRedacted"])
        item["authBridge"] = bridge_payload
        item["reasonCodes"] = list(dict.fromkeys([*item["reasonCodes"], *[code for code in bridge_reason_codes if code]]))
        if bridge_payload["blockedReason"]:
            item["reasonCodes"] = list(dict.fromkeys([*item["reasonCodes"], str(bridge_payload["blockedReason"])]))
        item.update(
            _canonical_status_payload(
                config=config,
                provider_type=provider_type,
                profile_role=profile_role,
                readiness_status=readiness_status,
                reason_codes=item["reasonCodes"],
                safe_modes=safe_modes,
                live_allowed=live_allowed,
            )
        )
        items.append(item)
        warnings.extend(item["warnings"])
    return {
        "generatedAt": iso_now_utc(),
        "items": items,
        "warnings": list(dict.fromkeys(warnings)),
    }
