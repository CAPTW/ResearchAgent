from __future__ import annotations

from copy import deepcopy
from typing import Any

READ_ONLY_RISK_LEVEL = "read_only"

ACTION_CAPABILITY_REQUIREMENTS: dict[str, dict[str, Any]] = {
    "validate_topic_config": {
        "actionType": "validate_topic_config",
        "capabilityRequired": "preview_run_plan",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "save_topic_config": {
        "actionType": "save_topic_config",
        "capabilityRequired": "edit_topic_config",
        "additionalCapabilities": [],
        "riskLevel": "config_write",
        "writeCapable": True,
    },
    "validate_provider_config": {
        "actionType": "validate_provider_config",
        "capabilityRequired": "view_provider_readiness",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "provider_readiness_check": {
        "actionType": "provider_readiness_check",
        "capabilityRequired": "view_provider_readiness",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "auth_metadata_validate": {
        "actionType": "auth_metadata_validate",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "audit_summary_view": {
        "actionType": "audit_summary_view",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "audit_replay_check": {
        "actionType": "audit_replay_check",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "oauth_authorization_preview": {
        "actionType": "oauth_authorization_preview",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "oauth_authorization_prepare": {
        "actionType": "oauth_authorization_prepare",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "oauth_callback_dry_run": {
        "actionType": "oauth_callback_dry_run",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "oauth_state_validate": {
        "actionType": "oauth_state_validate",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "oauth_token_exchange_status": {
        "actionType": "oauth_token_exchange_status",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "oauth_mock_exchange_validate": {
        "actionType": "oauth_mock_exchange_validate",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "oauth_account_link_preview": {
        "actionType": "oauth_account_link_preview",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "oauth_account_link_save_mock": {
        "actionType": "oauth_account_link_save_mock",
        "capabilityRequired": "edit_topic_config",
        "additionalCapabilities": [],
        "riskLevel": "config_write",
        "writeCapable": True,
    },
    "oauth_account_link_list": {
        "actionType": "oauth_account_link_list",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "oauth_account_link_health": {
        "actionType": "oauth_account_link_health",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "oauth_account_link_revoke_placeholder": {
        "actionType": "oauth_account_link_revoke_placeholder",
        "capabilityRequired": "edit_topic_config",
        "additionalCapabilities": [],
        "riskLevel": "config_write",
        "writeCapable": True,
    },
    "oauth_session_binding_preview": {
        "actionType": "oauth_session_binding_preview",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "oauth_session_capability_policy": {
        "actionType": "oauth_session_capability_policy",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "oauth_flow_timeline_status": {
        "actionType": "oauth_flow_timeline_status",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "oauth_live_exchange_readiness": {
        "actionType": "oauth_live_exchange_readiness",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "oauth_readiness_remediation": {
        "actionType": "oauth_readiness_remediation",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "oauth_preflight_package": {
        "actionType": "oauth_preflight_package",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "oauth_operator_acceptance_checklist": {
        "actionType": "oauth_operator_acceptance_checklist",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "oauth_live_exchange_pilot_status": {
        "actionType": "oauth_live_exchange_pilot_status",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "oauth_live_pilot_evidence_package": {
        "actionType": "oauth_live_pilot_evidence_package",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "oauth_live_pilot_replay_verification": {
        "actionType": "oauth_live_pilot_replay_verification",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "oauth_real_provider_smoke_status": {
        "actionType": "oauth_real_provider_smoke_status",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "oauth_real_provider_smoke": {
        "actionType": "oauth_real_provider_smoke",
        "capabilityRequired": "edit_topic_config",
        "additionalCapabilities": [],
        "riskLevel": "oauth_live_pilot",
        "writeCapable": True,
    },
    "oauth_smoke_result_summary": {
        "actionType": "oauth_smoke_result_summary",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "oauth_smoke_evidence_freeze_preview": {
        "actionType": "oauth_smoke_evidence_freeze_preview",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "oauth_smoke_replay_verification": {
        "actionType": "oauth_smoke_replay_verification",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "provider_orchestration_safety_matrix": {
        "actionType": "provider_orchestration_safety_matrix",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "provider_orchestration_dry_run_plan": {
        "actionType": "provider_orchestration_dry_run_plan",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "provider_orchestration_replay_verification": {
        "actionType": "provider_orchestration_replay_verification",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "provider_orchestration_fixture_execution_preview": {
        "actionType": "provider_orchestration_fixture_execution_preview",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "provider_orchestration_fixture_result_replay": {
        "actionType": "provider_orchestration_fixture_result_replay",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "provider_orchestration_fixture_result_summary": {
        "actionType": "provider_orchestration_fixture_result_summary",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "oauth_live_exchange_pilot": {
        "actionType": "oauth_live_exchange_pilot",
        "capabilityRequired": "edit_topic_config",
        "additionalCapabilities": [],
        "riskLevel": "oauth_live_pilot",
        "writeCapable": True,
    },
    "oauth_live_exchange_disabled": {
        "actionType": "oauth_live_exchange_disabled",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
    "prepare_provider_manual_run": {
        "actionType": "prepare_provider_manual_run",
        "capabilityRequired": "prepare_provider_manual_run",
        "additionalCapabilities": [],
        "riskLevel": "provider_prepare",
        "writeCapable": True,
    },
    "provider_acceptance_smoke": {
        "actionType": "provider_acceptance_smoke",
        "capabilityRequired": "run_real_provider_cli",
        "additionalCapabilities": ["accept_provider_risk"],
        "riskLevel": "provider_live_cli",
        "writeCapable": True,
    },
    "provider_risk_acceptance": {
        "actionType": "provider_risk_acceptance",
        "capabilityRequired": "accept_provider_risk",
        "additionalCapabilities": [],
        "riskLevel": "provider_live_cli",
        "writeCapable": True,
    },
    "secret_policy_rejection": {
        "actionType": "secret_policy_rejection",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "secret_policy",
        "writeCapable": False,
    },
    "capability_denied": {
        "actionType": "capability_denied",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "secret_policy",
        "writeCapable": False,
    },
    "capability_allowed": {
        "actionType": "capability_allowed",
        "capabilityRequired": "view_dashboard",
        "additionalCapabilities": [],
        "riskLevel": "read_only",
        "writeCapable": False,
    },
}


def list_action_requirements() -> dict[str, dict[str, Any]]:
    return deepcopy(ACTION_CAPABILITY_REQUIREMENTS)


def action_requirement(action_type: str) -> dict[str, Any] | None:
    requirement = ACTION_CAPABILITY_REQUIREMENTS.get(str(action_type or ""))
    return deepcopy(requirement) if requirement else None


def evaluate_action_capability(
    action_type: str,
    *,
    operator_capabilities: list[str] | tuple[str, ...] | set[str],
    fallback_risk_level: str | None = None,
) -> dict[str, Any]:
    requirement = action_requirement(action_type)
    if requirement is None:
        risk_level = fallback_risk_level or "unknown"
        return {
            "actionType": action_type,
            "allowed": False,
            "capabilityRequired": None,
            "additionalCapabilities": [],
            "capabilityPresent": False,
            "missingCapabilities": [],
            "riskLevel": risk_level,
            "writeCapable": risk_level != READ_ONLY_RISK_LEVEL,
            "reasonCodes": ["missing_action_requirement"],
        }

    present = set(operator_capabilities)
    required = [requirement["capabilityRequired"], *list(requirement.get("additionalCapabilities") or [])]
    missing = [capability for capability in required if capability and capability not in present]
    return {
        "actionType": requirement["actionType"],
        "allowed": not missing,
        "capabilityRequired": requirement["capabilityRequired"],
        "additionalCapabilities": list(requirement.get("additionalCapabilities") or []),
        "capabilityPresent": requirement["capabilityRequired"] in present,
        "missingCapabilities": missing,
        "riskLevel": requirement["riskLevel"],
        "writeCapable": bool(requirement["writeCapable"]),
        "reasonCodes": ["missing_capability"] if missing else [],
    }
