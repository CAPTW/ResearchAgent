from __future__ import annotations

import threading
import os
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable

from app.auth.resource_access import (
    ResourceAccessDenied,
    ResourceAction,
    ResourceActor,
    ResourceKind,
    authorize_resource_access,
    owner_user_id_for_actor,
    reject_client_authority_fields,
    resolve_resource_actor,
    scoped_system_actor,
)
from app.domain.constants import STORAGE_ROOT
from app.repositories import metadata_index
from app.repositories import shared_state
from app.repositories.files import iso_now_utc, slugify
from app.repositories.runs import build_run_paths
from app.services import topic_profiles, webhook_egress, workbench

NOTIFICATION_LIBRARY_DIR = STORAGE_ROOT.parent / "topic_notifications"
NOTIFICATION_SETTINGS_PATH = NOTIFICATION_LIBRARY_DIR / "settings.json"
NOTIFICATION_EVENTS_PATH = NOTIFICATION_LIBRARY_DIR / "notifications.json"
NOTIFICATION_DELIVERIES_PATH = NOTIFICATION_LIBRARY_DIR / "deliveries.json"
NOTIFICATION_LOCK = threading.RLock()
MAX_NOTIFICATION_RECORDS = 1000
MAX_DELIVERY_RECORDS = 1000
HIGH_IMPACT_SCORE_THRESHOLD = 60
HIGH_CITATION_THRESHOLD = 25
EMAIL_ADDRESS_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EMAIL_CHANNEL_AVAILABLE = "available"
EMAIL_CHANNEL_MISSING_ENV = "unavailable_missing_env"
EMAIL_CHANNEL_MISCONFIGURED = "misconfigured"
EMAIL_CHANNEL_FAILED_DELIVERY = "failed_delivery"
EMAIL_CHANNEL_CONNECTION_FAILED = "connection_failed"
EMAIL_CHANNEL_DELIVERY_FAILED = "delivery_failed"
WEBHOOK_CHANNEL_DISABLED = "disabled"
SMTP_READINESS_AVAILABLE = "available"
SMTP_READINESS_MISSING_ENV = "unavailable_missing_env"
SMTP_READINESS_INVALID_CONFIG = "invalid_config"
SMTP_READINESS_CONNECTION_FAILED = "connection_failed"
SMTP_READINESS_DELIVERY_FAILED = "delivery_failed"

RULE_CATALOG = [
    {
        "rule_id": "report_completed",
        "label": "보고서 생성 완료",
        "description": "보고서 생성이 끝나고 다운로드 가능한 결과가 준비되면 알려줍니다.",
        "default_enabled": True,
    },
    {
        "rule_id": "run_failed",
        "label": "실행 실패",
        "description": "토픽 파이프라인 실행이 실패했을 때 알려줍니다.",
        "default_enabled": True,
    },
    {
        "rule_id": "high_impact_paper",
        "label": "고영향력/고인용 논문 감지",
        "description": "영향력 점수 또는 인용 수가 높은 논문이 감지되면 알려줍니다.",
        "default_enabled": True,
    },
    {
        "rule_id": "digest_available",
        "label": "주간/정기 digest 준비 완료",
        "description": "주간 또는 정기 리포트가 생성되어 확인 가능할 때 알려줍니다.",
        "default_enabled": True,
    },
    {
        "rule_id": "automation_task_failure",
        "label": "예약 작업 실패",
        "description": "자동 예약 실행이 시작되지 못했거나 실행 중 실패했을 때 알려줍니다.",
        "default_enabled": True,
    },
]

CITATION_FIELDS = (
    "citations",
    "citation_count",
    "citationCount",
    "cited_by_count",
    "citedByCount",
    "num_citations",
    "numCitations",
    "influential_citation_count",
    "influentialCitationCount",
)
CITATION_CONTAINER_FIELDS = (
    "metadata",
    "metrics",
    "citation_metrics",
    "openalex",
    "semantic_scholar",
    "semanticscholar",
    "crossref",
)


# Keyed by file name rather than by the resolved path so the logical name is
# stable when the library location is redirected.
_STATE_NAMES = {
    "settings.json": "topic_notification_settings",
    "notifications.json": "topic_notifications",
    "deliveries.json": "topic_notification_deliveries",
}


def _state_name(path: Path) -> str:
    return _STATE_NAMES.get(path.name, "topic_notification_state")


def _load_json_list(path: Path) -> list[dict[str, Any]]:
    # Reading must not create the parent directory: status/readiness probes call
    # this, and observation may not have filesystem side effects. Writers create
    # the directory at the mutation boundary instead. A malformed or wrongly
    # shaped file raises rather than reading as empty, so the next write cannot
    # overwrite recoverable records with an empty list.
    payload = shared_state.load_json_state(
        path, state_name=_state_name(path), expected_type=list, default=lambda: []
    )
    return [item for item in payload if isinstance(item, dict)]


def _retain(records: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    return sorted(records, key=lambda item: str(item.get("created_at") or ""), reverse=True)[:limit]


def _mutate_records(
    path: Path,
    mutate: Callable[[list[dict[str, Any]]], tuple[list[dict[str, Any]], Any]],
    *,
    touch_key: str,
) -> Any:
    """Apply a mutation to the newest records of one notification state file.

    The callback sees the list as of *inside* the lock, so two appends from
    different processes (a manual test delivery and a scheduler delivery, say)
    both survive instead of one overwriting the other. Retention is likewise
    applied to the latest list, never to a stale snapshot.
    """

    def _apply(raw: Any) -> tuple[list[dict[str, Any]], Any]:
        current = [item for item in raw if isinstance(item, dict)]
        return mutate(current)

    result = shared_state.mutate_json_state(
        path,
        state_name=_state_name(path),
        expected_type=list,
        default=lambda: [],
        mutate=_apply,
    )
    metadata_index.touch_index_state(touch_key)
    return result


def notification_delivery_health_summary(
    limit: int = 200,
    *,
    actor: ResourceActor | None = None,
    include_resource_activity: bool = True,
) -> dict[str, Any]:
    with NOTIFICATION_LOCK:
        deliveries = _load_json_list(NOTIFICATION_DELIVERIES_PATH)

    resolved_actor: ResourceActor | None = None
    if include_resource_activity:
        try:
            resolved_actor = resolve_resource_actor(actor)
        except ResourceAccessDenied:
            # Principal-free readiness/operator probes expose only redacted
            # transport health, never delivery activity.
            include_resource_activity = False
    if resolved_actor is not None and resolved_actor.auth_mode != "disabled":
        owner_user_id = owner_user_id_for_actor(resolved_actor)
        deliveries = [
            item
            for item in deliveries
            if str(item.get("owner_user_id") or "").strip() == owner_user_id
        ]
    if not include_resource_activity:
        deliveries = []
    recent = sorted(deliveries, key=lambda item: str(item.get("created_at") or ""), reverse=True)[:limit]
    status_counts: dict[str, int] = {}
    channel_counts: dict[str, int] = {}
    latest_delivery_at: str | None = None
    latest_failure_at: str | None = None
    latest_failure_message: str | None = None
    for item in recent:
        status = str(item.get("status") or "unknown")
        channel = str(item.get("channel_id") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        channel_counts[channel] = channel_counts.get(channel, 0) + 1
        created_at = str(item.get("created_at") or "")
        if created_at and latest_delivery_at is None:
            latest_delivery_at = created_at
        if status == "failed" and latest_failure_at is None:
            latest_failure_at = created_at or None
            if channel == "webhook":
                latest_failure_message = webhook_egress.safe_webhook_delivery_message(item)
            else:
                latest_failure_message = str(item.get("message") or "") or None
    smtp_diagnostics = smtp_config_diagnostics()
    smtp_state = str(smtp_diagnostics["state"])
    smtp_unavailable_reason = smtp_diagnostics["unavailable_reason"]
    smtp_available = smtp_state == EMAIL_CHANNEL_AVAILABLE
    smtp_readiness_state = str(smtp_diagnostics["readiness_state"])
    for item in recent:
        if str(item.get("channel_id") or "") != "email" or str(item.get("status") or "") == "sent":
            continue
        failure_state = str(item.get("state") or "")
        if failure_state in {EMAIL_CHANNEL_CONNECTION_FAILED, EMAIL_CHANNEL_DELIVERY_FAILED}:
            smtp_readiness_state = failure_state
            break
    webhook_available, webhook_unavailable_reason = _webhook_env_status()

    return {
        "total_recent_deliveries": len(recent),
        "status_counts": status_counts,
        "channel_counts": channel_counts,
        "latest_delivery_at": latest_delivery_at,
        "latest_failure_at": latest_failure_at,
        "latest_failure_message": latest_failure_message,
        "smtp_state": smtp_state,
        "smtp_readiness_state": smtp_readiness_state,
        "smtp_available": smtp_available,
        "smtp_configured": bool(smtp_diagnostics["configured"]),
        "smtp_missing_env": list(smtp_diagnostics["missing_env"]),
        "smtp_invalid_config": list(smtp_diagnostics["invalid_config"]),
        "smtp_tls_mode": smtp_diagnostics["tls_mode"],
        "smtp_unavailable_reason": smtp_unavailable_reason,
        "webhook_available": webhook_available,
        "webhook_unavailable_reason": webhook_unavailable_reason,
    }


def _default_rules_map() -> dict[str, bool]:
    return {str(rule["rule_id"]): bool(rule.get("default_enabled", False)) for rule in RULE_CATALOG}


def _default_settings_raw(
    profile: dict[str, Any],
    *,
    owner_user_id: str | None,
) -> dict[str, Any]:
    now = iso_now_utc()
    return {
        "profile_id": str(profile.get("profile_id") or "").strip(),
        "owner_user_id": owner_user_id,
        "in_app_enabled": True,
        "channels": {
            "email": {"enabled": False, "destination": ""},
            "webhook": {"enabled": False, "destination": ""},
        },
        "rules": _default_rules_map(),
        "created_at": now,
        "updated_at": now,
    }


def _smtp_config_with_state() -> tuple[dict[str, Any] | None, str, str | None]:
    host = str(os.environ.get("NOTIFICATION_EMAIL_SMTP_HOST") or os.environ.get("SMTP_HOST") or "").strip()
    from_address = str(os.environ.get("NOTIFICATION_EMAIL_FROM") or os.environ.get("SMTP_FROM") or "").strip()
    if not host or not from_address:
        return None, EMAIL_CHANNEL_MISSING_ENV, "SMTP host/from 환경 설정이 없어 이메일 전송을 사용할 수 없습니다."
    if not EMAIL_ADDRESS_PATTERN.match(from_address):
        return None, EMAIL_CHANNEL_MISCONFIGURED, "SMTP from 이메일 주소 형식이 올바르지 않습니다."
    try:
        port = int(os.environ.get("NOTIFICATION_EMAIL_SMTP_PORT") or os.environ.get("SMTP_PORT") or "587")
    except ValueError:
        return None, EMAIL_CHANNEL_MISCONFIGURED, "SMTP port 환경 설정이 숫자가 아닙니다."
    if port <= 0 or port > 65535:
        return None, EMAIL_CHANNEL_MISCONFIGURED, "SMTP port 환경 설정이 유효하지 않습니다."
    username = os.environ.get("NOTIFICATION_EMAIL_SMTP_USERNAME") or os.environ.get("SMTP_USERNAME")
    password = os.environ.get("NOTIFICATION_EMAIL_SMTP_PASSWORD") or os.environ.get("SMTP_PASSWORD")
    if bool(username) != bool(password):
        return None, EMAIL_CHANNEL_MISCONFIGURED, "SMTP username/password 환경 설정은 둘 다 설정하거나 둘 다 비워야 합니다."
    use_ssl = (os.environ.get("NOTIFICATION_EMAIL_SMTP_SSL") or os.environ.get("SMTP_SSL") or "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    use_tls = (os.environ.get("NOTIFICATION_EMAIL_SMTP_TLS") or os.environ.get("SMTP_TLS") or ("false" if use_ssl else "true")).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    if use_ssl and use_tls:
        return None, EMAIL_CHANNEL_MISCONFIGURED, "SMTP TLS와 SSL은 동시에 활성화할 수 없습니다."
    return {
        "host": host,
        "port": port,
        "from_address": from_address,
        "username": username,
        "password": password,
        "use_tls": use_tls,
        "use_ssl": use_ssl,
    }, EMAIL_CHANNEL_AVAILABLE, None


def smtp_config_diagnostics() -> dict[str, Any]:
    host = str(os.environ.get("NOTIFICATION_EMAIL_SMTP_HOST") or os.environ.get("SMTP_HOST") or "").strip()
    from_address = str(os.environ.get("NOTIFICATION_EMAIL_FROM") or os.environ.get("SMTP_FROM") or "").strip()
    port_raw = str(os.environ.get("NOTIFICATION_EMAIL_SMTP_PORT") or os.environ.get("SMTP_PORT") or "587").strip()
    username = os.environ.get("NOTIFICATION_EMAIL_SMTP_USERNAME") or os.environ.get("SMTP_USERNAME")
    password = os.environ.get("NOTIFICATION_EMAIL_SMTP_PASSWORD") or os.environ.get("SMTP_PASSWORD")
    ssl_raw = str(os.environ.get("NOTIFICATION_EMAIL_SMTP_SSL") or os.environ.get("SMTP_SSL") or "false").strip().lower()
    ssl_enabled = ssl_raw in {"1", "true", "yes", "on"}
    tls_raw = str(
        os.environ.get("NOTIFICATION_EMAIL_SMTP_TLS") or os.environ.get("SMTP_TLS") or ("false" if ssl_enabled else "true")
    ).strip().lower()
    tls_enabled = tls_raw not in {"0", "false", "no", "off"}

    missing_env: list[str] = []
    invalid_config: list[str] = []
    if not host:
        missing_env.append("NOTIFICATION_EMAIL_SMTP_HOST")
    if not from_address:
        missing_env.append("NOTIFICATION_EMAIL_FROM")
    elif not EMAIL_ADDRESS_PATTERN.match(from_address):
        invalid_config.append("NOTIFICATION_EMAIL_FROM")
    try:
        port = int(port_raw)
        if port <= 0 or port > 65535:
            invalid_config.append("NOTIFICATION_EMAIL_SMTP_PORT")
    except ValueError:
        invalid_config.append("NOTIFICATION_EMAIL_SMTP_PORT")
    if bool(username) != bool(password):
        invalid_config.append("NOTIFICATION_EMAIL_SMTP_USERNAME/NOTIFICATION_EMAIL_SMTP_PASSWORD")
    if ssl_enabled and tls_enabled:
        invalid_config.append("NOTIFICATION_EMAIL_SMTP_TLS/NOTIFICATION_EMAIL_SMTP_SSL")

    tls_mode = "invalid" if ssl_enabled and tls_enabled else "ssl" if ssl_enabled else "starttls" if tls_enabled else "plain"
    _, state, error = _smtp_config_with_state()
    if missing_env:
        readiness_state = SMTP_READINESS_MISSING_ENV
    elif invalid_config or state == EMAIL_CHANNEL_MISCONFIGURED:
        readiness_state = SMTP_READINESS_INVALID_CONFIG
    else:
        readiness_state = SMTP_READINESS_AVAILABLE
    return {
        "state": state,
        "readiness_state": readiness_state,
        "configured": readiness_state == SMTP_READINESS_AVAILABLE,
        "available": state == EMAIL_CHANNEL_AVAILABLE,
        "missing_env": missing_env,
        "invalid_config": invalid_config,
        "tls_mode": tls_mode,
        "unavailable_reason": error,
    }


def _smtp_config() -> tuple[dict[str, Any] | None, str | None]:
    config, state, error = _smtp_config_with_state()
    if state != EMAIL_CHANNEL_AVAILABLE:
        return None, error
    return config, None


def _email_env_status() -> tuple[bool, str | None]:
    _, state, error = _smtp_config_with_state()
    if state != EMAIL_CHANNEL_AVAILABLE:
        return False, error
    return True, None


def _email_env_state() -> tuple[str, str | None]:
    _, state, error = _smtp_config_with_state()
    return state, error


def _webhook_env_status() -> tuple[bool, str | None]:
    status = webhook_egress.webhook_policy_status()
    return bool(status["available"]), status["unavailable_reason"]


def _stored_channel(raw: dict[str, Any], channel_id: str) -> dict[str, Any]:
    channels = raw.get("channels")
    if isinstance(channels, dict) and isinstance(channels.get(channel_id), dict):
        return dict(channels[channel_id])
    return {"enabled": False, "destination": ""}


def _redact_destination(channel_id: str, destination: str) -> str:
    destination = destination.strip()
    if not destination:
        return ""
    if channel_id == "email":
        if "@" not in destination:
            return destination
        local, domain = destination.split("@", 1)
        return f"{local[:2]}***@{domain}"
    return webhook_egress.redact_webhook_destination(destination)


def _latest_failed_delivery_state(
    profile_id: str,
    channel_id: str,
    *,
    owner_user_id: str | None,
) -> tuple[str | None, str | None]:
    if not profile_id:
        return None, None
    deliveries = _load_json_list(NOTIFICATION_DELIVERIES_PATH)
    recent = sorted(deliveries, key=lambda item: str(item.get("created_at") or ""), reverse=True)
    for item in recent:
        if str(item.get("profile_id") or "").strip() != profile_id:
            continue
        if str(item.get("owner_user_id") or "").strip() != str(owner_user_id or ""):
            continue
        if str(item.get("channel_id") or "").strip() != channel_id:
            continue
        if str(item.get("status") or "").strip() == "failed":
            state = str(item.get("state") or EMAIL_CHANNEL_FAILED_DELIVERY)
            return state, str(item.get("message") or "") or None
        return None, None
    return None, None


def _channels(
    in_app_enabled: bool,
    raw: dict[str, Any] | None = None,
    *,
    owner_user_id: str | None = None,
) -> list[dict[str, Any]]:
    raw = raw or {}
    email_config = _stored_channel(raw, "email")
    webhook_config = _stored_channel(raw, "webhook")
    email_diagnostics = smtp_config_diagnostics()
    email_env_state = str(email_diagnostics["state"])
    email_readiness_state = str(email_diagnostics["readiness_state"])
    email_unavailable_reason = email_diagnostics["unavailable_reason"]
    email_available = email_env_state == EMAIL_CHANNEL_AVAILABLE
    webhook_available, webhook_unavailable_reason = _webhook_env_status()
    webhook_policy = webhook_egress.webhook_policy_status()
    email_destination = str(email_config.get("destination") or "").strip()
    webhook_destination = str(webhook_config.get("destination") or "").strip()
    webhook_destination_label = _redact_destination("webhook", webhook_destination)
    email_enabled = bool(email_config.get("enabled")) and email_available
    webhook_enabled = bool(webhook_config.get("enabled")) and webhook_available
    profile_id = str(raw.get("profile_id") or "").strip()
    latest_email_failure_state, _latest_email_failure = _latest_failed_delivery_state(
        profile_id,
        "email",
        owner_user_id=owner_user_id,
    )
    email_state = latest_email_failure_state if email_available and latest_email_failure_state else email_env_state
    if email_enabled and email_state in {EMAIL_CHANNEL_FAILED_DELIVERY, EMAIL_CHANNEL_CONNECTION_FAILED, EMAIL_CHANNEL_DELIVERY_FAILED}:
        email_status_label = "최근 전송 실패"
    elif email_enabled:
        email_status_label = "사용 중"
    elif email_state == EMAIL_CHANNEL_MISSING_ENV:
        email_status_label = "SMTP 설정 없음"
    elif email_state == EMAIL_CHANNEL_MISCONFIGURED:
        email_status_label = "SMTP 설정 오류"
    else:
        email_status_label = "꺼짐"
    return [
        {
            "channel_id": "in_app",
            "label": "인앱 알림 센터",
            "description": "앱 내부 최근 이벤트 피드에 실시간으로 기록합니다.",
            "available": True,
            "enabled": in_app_enabled,
            "state": EMAIL_CHANNEL_AVAILABLE,
            "readiness_state": SMTP_READINESS_AVAILABLE,
            "status_label": "사용 중" if in_app_enabled else "꺼짐",
            "destination": "",
            "destination_label": "앱 내부 피드",
            "unavailable_reason": None,
            "supports_test": False,
        },
        {
            "channel_id": "email",
            "label": "이메일",
            "description": "SMTP 환경 설정이 있는 경우 지정한 이메일 주소로 실제 이벤트와 digest를 전송합니다.",
            "available": email_available,
            "enabled": email_enabled,
            "state": email_state,
            "readiness_state": latest_email_failure_state if email_available and latest_email_failure_state else email_readiness_state,
            "status_label": email_status_label,
            "destination": email_destination,
            "destination_label": _redact_destination("email", email_destination) or "수신 이메일 미설정",
            "unavailable_reason": email_unavailable_reason,
            "supports_test": email_available,
        },
        {
            "channel_id": "webhook",
            "label": "Webhook",
            "description": "허용된 HTTPS endpoint로만 알림 JSON을 POST하며 응답 상태를 delivery log에 기록합니다.",
            "available": webhook_available,
            "enabled": webhook_enabled,
            "state": (
                EMAIL_CHANNEL_AVAILABLE
                if webhook_available
                else WEBHOOK_CHANNEL_DISABLED
                if not bool(webhook_policy["enabled"]) and webhook_policy["error_code"] is None
                else EMAIL_CHANNEL_MISCONFIGURED
            ),
            "readiness_state": (
                EMAIL_CHANNEL_AVAILABLE
                if webhook_available
                else WEBHOOK_CHANNEL_DISABLED
                if not bool(webhook_policy["enabled"]) and webhook_policy["error_code"] is None
                else EMAIL_CHANNEL_MISCONFIGURED
            ),
            "status_label": "사용 중" if webhook_enabled else "비활성" if webhook_available else "환경 설정에서 비활성",
            "destination": webhook_destination_label,
            "destination_label": webhook_destination_label or "Webhook URL 미설정",
            "unavailable_reason": webhook_unavailable_reason,
            "supports_test": webhook_available,
        },
    ]


def _normalize_settings(
    raw: dict[str, Any],
    profile: dict[str, Any],
    *,
    owner_user_id: str | None,
) -> dict[str, Any]:
    created_at = str(raw.get("created_at") or iso_now_utc())
    updated_at = str(raw.get("updated_at") or created_at)
    rules_map = _default_rules_map()
    stored_rules = raw.get("rules")
    if isinstance(stored_rules, dict):
        for rule_id, enabled in stored_rules.items():
            rules_map[str(rule_id)] = bool(enabled)

    in_app_enabled = bool(raw.get("in_app_enabled", True))
    return {
        "profile_id": str(profile.get("profile_id") or "").strip(),
        "topic_id": str(profile.get("profile_id") or "").strip(),
        "topic_name": str(profile.get("title") or profile.get("topic") or profile.get("profile_id") or "").strip(),
        "in_app_enabled": in_app_enabled,
        "channels": _channels(
            in_app_enabled,
            raw,
            owner_user_id=owner_user_id,
        ),
        "rules": [
            {
                "rule_id": str(rule["rule_id"]),
                "label": str(rule["label"]),
                "description": str(rule["description"]),
                "enabled": bool(rules_map.get(str(rule["rule_id"]), False)),
            }
            for rule in RULE_CATALOG
        ],
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _entry_for(
    raw_settings: list[dict[str, Any]],
    profile_id: str,
    *,
    owner_user_id: str | None,
) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in raw_settings
            if str(item.get("profile_id") or "").strip() == profile_id
            and str(item.get("owner_user_id") or "").strip() == str(owner_user_id or "")
        ),
        None,
    )


def _notification_owner_for_profile(
    profile: dict[str, Any],
    *,
    actor: ResourceActor,
    action: ResourceAction | str,
) -> str | None:
    topic_profiles.authorize_topic_profile(
        profile,
        actor=actor,
        action=ResourceAction.READ,
    )
    owner_user_id = owner_user_id_for_actor(actor)
    resource_kind = (
        ResourceKind.USER_OWNED
        if topic_profiles._profile_resource_kind(profile) == ResourceKind.SYSTEM_TEMPLATE
        else ResourceKind.INHERITED_OWNER
    )
    if resource_kind == ResourceKind.INHERITED_OWNER:
        profile_owner = str(profile.get("owner_user_id") or "").strip() or None
        if owner_user_id != profile_owner and actor.auth_mode != "disabled":
            raise ResourceAccessDenied()
    authorize_resource_access(
        actor=actor,
        resource_kind=resource_kind,
        action=action,
        owner_user_id=owner_user_id,
    )
    return owner_user_id


def _materialize_settings_raw(
    profile_id: str,
    *,
    actor: ResourceActor | None = None,
    action: ResourceAction | str = ResourceAction.READ,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], str | None]:
    """Resolve this profile's settings entry without writing anything.

    Defaults are materialized in memory only; they are persisted by the next
    real mutation, so reading settings no longer writes the settings file.
    """
    resolved_actor = resolve_resource_actor(actor)
    profile = topic_profiles.get_topic_profile(profile_id, actor=resolved_actor)
    owner_user_id = _notification_owner_for_profile(
        profile,
        actor=resolved_actor,
        action=action,
    )
    raw_settings = _load_json_list(NOTIFICATION_SETTINGS_PATH)
    existing = _entry_for(
        raw_settings,
        profile_id,
        owner_user_id=owner_user_id,
    )
    if existing is None:
        existing = _default_settings_raw(
            profile,
            owner_user_id=owner_user_id,
        )
        raw_settings.append(existing)
    return raw_settings, existing, profile, owner_user_id


def get_topic_notification_settings(
    profile_id: str,
    *,
    actor: ResourceActor | None = None,
) -> dict[str, Any]:
    resolved_actor = resolve_resource_actor(actor)
    with NOTIFICATION_LOCK:
        _, existing, profile, owner_user_id = _materialize_settings_raw(
            str(profile_id or "").strip(),
            actor=resolved_actor,
        )
        return _normalize_settings(
            existing,
            profile,
            owner_user_id=owner_user_id,
        )


def update_topic_notification_settings(
    profile_id: str,
    payload: dict[str, Any],
    *,
    actor: ResourceActor | None = None,
) -> dict[str, Any]:
    reject_client_authority_fields(payload)
    resolved_actor = resolve_resource_actor(actor)
    profile_id = str(profile_id or "").strip()
    with NOTIFICATION_LOCK:
        # Resolve and validate outside the state lock: webhook validation may
        # perform DNS/network work, which must never run while the lock is held.
        _, preview, profile, owner_user_id = _materialize_settings_raw(
            profile_id,
            actor=resolved_actor,
            action=ResourceAction.UPDATE,
        )
        validated_destinations = _validate_channel_payload(preview, payload)

        def _apply(raw_settings: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
            # Re-resolve this profile's entry inside the lock and patch only its
            # fields, so a concurrent update to a different profile - or to the
            # other channel of this profile - is preserved.
            current_profile = topic_profiles.get_topic_profile(
                profile_id,
                actor=resolved_actor,
            )
            current_owner = _notification_owner_for_profile(
                current_profile,
                actor=resolved_actor,
                action=ResourceAction.UPDATE,
            )
            if current_owner != owner_user_id:
                raise ResourceAccessDenied()
            existing = _entry_for(
                raw_settings,
                profile_id,
                owner_user_id=current_owner,
            )
            if existing is None:
                existing = _default_settings_raw(
                    current_profile,
                    owner_user_id=current_owner,
                )
                raw_settings.append(existing)
            _patch_settings_entry(existing, payload, validated_destinations)
            return raw_settings, dict(existing)

        existing = _mutate_records(
            NOTIFICATION_SETTINGS_PATH,
            _apply,
            touch_key="last_topic_notification_settings_updated_at",
        )
        return _normalize_settings(
            existing,
            profile,
            owner_user_id=owner_user_id,
        )


def _validate_channel_payload(existing: dict[str, Any], payload: dict[str, Any]) -> dict[str, str]:
    """Validate requested channel destinations before any lock is taken.

    Returns the destinations that passed validation, keyed by channel. The empty
    string marks "keep whatever is stored", which was validated when it was set.
    """
    resolved: dict[str, str] = {}
    channels = payload.get("channels")
    if not isinstance(channels, list):
        return resolved
    stored_channels = existing.get("channels") if isinstance(existing.get("channels"), dict) else {}
    for entry in channels:
        if not isinstance(entry, dict):
            continue
        channel_id = str(entry.get("channel_id") or "").strip()
        if channel_id not in {"email", "webhook"}:
            continue
        destination = str(entry.get("destination") or "").strip()
        enabled = bool(entry.get("enabled", False))
        if channel_id == "webhook":
            current = str((stored_channels.get("webhook") or {}).get("destination") or "").strip()
            unchanged = bool(current) and destination == _redact_destination("webhook", current)
            candidate = current if unchanged else destination
            if enabled or (candidate and not unchanged):
                webhook_egress.validate_webhook_configuration(candidate, enabled=enabled)
            resolved[channel_id] = "" if unchanged else destination
        else:
            resolved[channel_id] = destination
    return resolved


def _patch_settings_entry(
    existing: dict[str, Any], payload: dict[str, Any], validated_destinations: dict[str, str]
) -> None:
    """Pure, network-free patch of one profile's settings entry."""
    if True:
        if "in_app_enabled" in payload and payload["in_app_enabled"] is not None:
            existing["in_app_enabled"] = bool(payload["in_app_enabled"])

        channels = payload.get("channels")
        if isinstance(channels, list):
            stored_channels = existing.get("channels") if isinstance(existing.get("channels"), dict) else {}
            next_channels = {
                "email": dict(stored_channels.get("email") or {"enabled": False, "destination": ""}),
                "webhook": dict(stored_channels.get("webhook") or {"enabled": False, "destination": ""}),
            }
            for entry in channels:
                if not isinstance(entry, dict):
                    continue
                channel_id = str(entry.get("channel_id") or "").strip()
                if channel_id not in {"email", "webhook"}:
                    continue
                destination = str(entry.get("destination") or "").strip()
                enabled = bool(entry.get("enabled", False))
                if channel_id == "webhook":
                    current_destination = str(next_channels[channel_id].get("destination") or "").strip()
                    current_redacted = _redact_destination("webhook", current_destination)
                    destination_unchanged = bool(current_destination) and destination == current_redacted
                    candidate_destination = current_destination if destination_unchanged else destination
                    if not destination_unchanged:
                        # Only a destination already validated outside the lock
                        # may be stored; re-validating here would mean network
                        # work while the state lock is held.
                        if validated_destinations.get("webhook") != candidate_destination:
                            raise ValueError("Webhook destination changed concurrently; retry the update.")
                    destination = candidate_destination
                next_channels[channel_id]["destination"] = destination
                next_channels[channel_id]["enabled"] = enabled
            existing["channels"] = next_channels

        rules = payload.get("rules")
        if isinstance(rules, list):
            rules_map = _default_rules_map()
            stored_rules = existing.get("rules")
            if isinstance(stored_rules, dict):
                for rule_id, enabled in stored_rules.items():
                    rules_map[str(rule_id)] = bool(enabled)
            for entry in rules:
                if not isinstance(entry, dict):
                    continue
                rule_id = str(entry.get("rule_id") or "").strip()
                if not rule_id or rule_id not in rules_map:
                    continue
                rules_map[rule_id] = bool(entry.get("enabled", False))
            existing["rules"] = rules_map

        existing["updated_at"] = iso_now_utc()


def list_topic_notifications(
    profile_id: str,
    *,
    limit: int = 20,
    actor: ResourceActor | None = None,
) -> dict[str, Any]:
    resolved_actor = resolve_resource_actor(actor)
    profile = topic_profiles.get_topic_profile(profile_id, actor=resolved_actor)
    owner_user_id = _notification_owner_for_profile(
        profile,
        actor=resolved_actor,
        action=ResourceAction.READ,
    )
    with NOTIFICATION_LOCK:
        notifications = [
            _normalize_notification(item, profile)
            for item in _load_json_list(NOTIFICATION_EVENTS_PATH)
            if str(item.get("profile_id") or "").strip() == profile_id
            and str(item.get("owner_user_id") or "").strip() == str(owner_user_id or "")
        ]
    notifications.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return {
        "profile_id": profile_id,
        "topic_name": str(profile.get("title") or profile.get("topic") or profile_id),
        "items": notifications[:limit],
        "total": len(notifications),
    }


def list_topic_notification_deliveries(
    profile_id: str,
    *,
    limit: int = 20,
    actor: ResourceActor | None = None,
) -> dict[str, Any]:
    resolved_actor = resolve_resource_actor(actor)
    profile = topic_profiles.get_topic_profile(profile_id, actor=resolved_actor)
    owner_user_id = _notification_owner_for_profile(
        profile,
        actor=resolved_actor,
        action=ResourceAction.READ,
    )
    with NOTIFICATION_LOCK:
        deliveries = [
            _normalize_delivery(item, profile)
            for item in _load_json_list(NOTIFICATION_DELIVERIES_PATH)
            if str(item.get("profile_id") or "").strip() == profile_id
            and str(item.get("owner_user_id") or "").strip() == str(owner_user_id or "")
        ]
    deliveries.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return {
        "profile_id": profile_id,
        "topic_name": str(profile.get("title") or profile.get("topic") or profile_id),
        "items": deliveries[:limit],
        "total": len(deliveries),
    }


def _normalize_notification(raw: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "notification_id": str(raw.get("notification_id") or ""),
        "profile_id": str(raw.get("profile_id") or ""),
        "topic_name": str(profile.get("title") or profile.get("topic") or profile.get("profile_id") or ""),
        "rule_id": str(raw.get("rule_id") or ""),
        "event_type": str(raw.get("event_type") or ""),
        "severity": str(raw.get("severity") or "info"),
        "title": str(raw.get("title") or ""),
        "message": str(raw.get("message") or ""),
        "created_at": str(raw.get("created_at") or ""),
        "run_id": raw.get("run_id"),
        "schedule_id": raw.get("schedule_id"),
        "download_url": raw.get("download_url"),
    }


def _normalize_delivery(raw: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    raw_attempts = raw.get("attempts")
    attempts = int(raw_attempts) if raw_attempts is not None else 1
    channel_id = str(raw.get("channel_id") or "")
    destination_label = str(raw.get("destination_label") or "")
    message = str(raw.get("message") or "")
    if channel_id == "webhook":
        destination_label = webhook_egress.redact_webhook_destination(destination_label)
        message = webhook_egress.safe_webhook_delivery_message(raw)
    return {
        "delivery_id": str(raw.get("delivery_id") or ""),
        "notification_id": raw.get("notification_id"),
        "profile_id": str(raw.get("profile_id") or ""),
        "topic_name": str(profile.get("title") or profile.get("topic") or profile.get("profile_id") or ""),
        "channel_id": channel_id,
        "rule_id": str(raw.get("rule_id") or ""),
        "event_type": str(raw.get("event_type") or ""),
        "state": str(raw.get("state") or (EMAIL_CHANNEL_AVAILABLE if str(raw.get("status") or "") == "sent" else EMAIL_CHANNEL_FAILED_DELIVERY)),
        "status": str(raw.get("status") or "failed"),
        "destination_label": destination_label,
        "status_code": raw.get("status_code"),
        "error_code": raw.get("error_code"),
        "message": message,
        "attempts": attempts,
        "attempted": bool(raw.get("attempted", attempts > 0)),
        "created_at": str(raw.get("created_at") or ""),
        "updated_at": str(raw.get("updated_at") or raw.get("created_at") or ""),
    }


def _rule_enabled(settings_raw: dict[str, Any], rule_id: str) -> bool:
    stored_rules = settings_raw.get("rules")
    if isinstance(stored_rules, dict) and rule_id in stored_rules:
        return bool(stored_rules[rule_id])
    return bool(_default_rules_map().get(rule_id, False))


def _retain_owner_records(
    records: list[dict[str, Any]],
    *,
    owner_user_id: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    owner_key = str(owner_user_id or "")
    owned = [
        item
        for item in records
        if str(item.get("owner_user_id") or "").strip() == owner_key
    ]
    unrelated = [
        item
        for item in records
        if str(item.get("owner_user_id") or "").strip() != owner_key
    ]
    return [
        *unrelated,
        *_retain(owned, limit),
    ]


def _append_delivery_record(
    record: dict[str, Any],
    *,
    actor: ResourceActor | None = None,
) -> dict[str, Any]:
    """Append one delivery record to the newest history under a short lock.

    The network send has already happened by the time this runs; only the
    bounded append and retention trim are performed while the lock is held, so
    a concurrent append from another process is preserved rather than lost.
    """
    resolved_actor = resolve_resource_actor(actor)
    owner_user_id = str(record.get("owner_user_id") or "").strip() or None
    authorize_resource_access(
        actor=resolved_actor,
        resource_kind=ResourceKind.USER_OWNED,
        action=ResourceAction.TEST_DELIVER,
        owner_user_id=owner_user_id,
    )

    def _apply(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        authorize_resource_access(
            actor=resolved_actor,
            resource_kind=ResourceKind.USER_OWNED,
            action=ResourceAction.TEST_DELIVER,
            owner_user_id=owner_user_id,
        )
        records.append(record)
        return (
            _retain_owner_records(
                records,
                owner_user_id=owner_user_id,
                limit=MAX_DELIVERY_RECORDS,
            ),
            record,
        )

    return _mutate_records(
        NOTIFICATION_DELIVERIES_PATH,
        _apply,
        touch_key="last_topic_notification_deliveries_updated_at",
    )


def _delivery_payload(profile: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    return {
        "notificationId": event.get("notification_id"),
        "profileId": event.get("profile_id"),
        "topicName": str(profile.get("title") or profile.get("topic") or event.get("profile_id") or ""),
        "ruleId": event.get("rule_id"),
        "eventType": event.get("event_type"),
        "severity": event.get("severity"),
        "title": event.get("title"),
        "message": event.get("message"),
        "runId": event.get("run_id"),
        "scheduleId": event.get("schedule_id"),
        "downloadUrl": event.get("download_url"),
        "createdAt": event.get("created_at"),
    }


def _send_email(destination: str, payload: dict[str, Any]) -> tuple[str, int | None, str, str]:
    if not EMAIL_ADDRESS_PATTERN.match(destination.strip()):
        return "failed", None, "수신 이메일 주소 형식이 올바르지 않습니다.", EMAIL_CHANNEL_MISCONFIGURED
    config, config_error = _smtp_config()
    if config_error or config is None:
        return "failed", None, config_error or "SMTP 환경 설정이 없습니다.", EMAIL_CHANNEL_MISCONFIGURED

    message = EmailMessage()
    message["From"] = str(config["from_address"])
    message["To"] = destination
    message["Subject"] = f"[AI Research Agent] {payload.get('title') or payload.get('eventType')}"
    message.set_content(
        "\n".join(
            [
                str(payload.get("title") or ""),
                "",
                str(payload.get("message") or ""),
                "",
                f"Topic: {payload.get('topicName')}",
                f"Event: {payload.get('eventType')}",
                f"Run: {payload.get('runId') or '-'}",
                f"Report: {payload.get('downloadUrl') or '-'}",
            ]
        )
    )

    try:
        smtp_class = smtplib.SMTP_SSL if bool(config["use_ssl"]) else smtplib.SMTP
        with smtp_class(str(config["host"]), int(config["port"]), timeout=10) as smtp:
            if bool(config["use_tls"]) and not bool(config["use_ssl"]):
                smtp.starttls()
            if config.get("username") and config.get("password"):
                smtp.login(str(config["username"]), str(config["password"]))
            smtp.send_message(message)
        return "sent", None, "이메일 전송 완료", EMAIL_CHANNEL_AVAILABLE
    except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, ConnectionError, TimeoutError) as exc:
        return "failed", None, str(exc), EMAIL_CHANNEL_CONNECTION_FAILED
    except smtplib.SMTPException as exc:
        return "failed", None, str(exc), EMAIL_CHANNEL_DELIVERY_FAILED
    except OSError as exc:
        return "failed", None, str(exc), EMAIL_CHANNEL_CONNECTION_FAILED
    except Exception as exc:  # pragma: no cover - network failure shape depends on environment
        return "failed", None, str(exc), EMAIL_CHANNEL_DELIVERY_FAILED


def _deliver_event(
    profile: dict[str, Any],
    settings_raw: dict[str, Any],
    event: dict[str, Any],
    *,
    actor: ResourceActor,
    owner_user_id: str | None,
    only_channel_id: str | None = None,
) -> list[dict[str, Any]]:
    authorize_resource_access(
        actor=actor,
        resource_kind=ResourceKind.USER_OWNED,
        action=ResourceAction.TEST_DELIVER,
        owner_user_id=owner_user_id,
    )
    profile_id = str(event.get("profile_id") or "").strip()
    payload = _delivery_payload(profile, event)
    channel_statuses = {
        item["channel_id"]: item
        for item in _channels(
            bool(settings_raw.get("in_app_enabled", True)),
            settings_raw,
            owner_user_id=owner_user_id,
        )
    }
    deliveries: list[dict[str, Any]] = []
    for channel_id in ("email", "webhook"):
        if only_channel_id is not None and channel_id != only_channel_id:
            continue
        channel = channel_statuses[channel_id]
        stored = _stored_channel(settings_raw, channel_id)
        destination = str(stored.get("destination") or "").strip()
        if only_channel_id is None and not bool(stored.get("enabled", False)):
            continue

        created_at = iso_now_utc()
        status = "failed"
        state = EMAIL_CHANNEL_FAILED_DELIVERY
        status_code: int | None = None
        error_code: str | None = None
        message = ""
        attempts = 0
        if channel_id == "email":
            if not channel["available"]:
                state = str(channel.get("state") or EMAIL_CHANNEL_MISCONFIGURED)
                message = str(channel.get("unavailable_reason") or "채널을 사용할 수 없습니다.")
            elif not destination:
                state = EMAIL_CHANNEL_MISCONFIGURED
                message = "전송 대상이 설정되지 않았습니다."
            else:
                attempts = 1
                status, status_code, message, state = _send_email(destination, payload)
        else:
            webhook_result = webhook_egress.deliver_webhook(destination, payload)
            status = webhook_result.status
            status_code = webhook_result.status_code
            error_code = webhook_result.error_code
            message = webhook_result.message
            attempts = 1 if webhook_result.attempted else 0
            if status == "sent":
                state = EMAIL_CHANNEL_AVAILABLE
            elif status == "disabled":
                state = WEBHOOK_CHANNEL_DISABLED
            elif webhook_result.attempted:
                state = EMAIL_CHANNEL_FAILED_DELIVERY
            else:
                state = EMAIL_CHANNEL_MISCONFIGURED

        delivery_slug_source = f"{profile_id}-{channel_id}-{event.get('rule_id')}-{created_at}"
        delivery = {
            "delivery_id": f"delivery-{slugify(delivery_slug_source)}",
            "notification_id": event.get("notification_id"),
            "profile_id": profile_id,
            "owner_user_id": owner_user_id,
            "channel_id": channel_id,
            "rule_id": event.get("rule_id"),
            "event_type": event.get("event_type"),
            "status": status,
            "state": state,
            "destination_label": _redact_destination(channel_id, destination),
            "status_code": status_code,
            "error_code": error_code,
            "message": message,
            "attempts": attempts,
            "attempted": attempts > 0,
            "created_at": created_at,
            "updated_at": created_at,
        }
        _append_delivery_record(delivery, actor=actor)
        deliveries.append(_normalize_delivery(delivery, profile))
    return deliveries


def create_topic_notification(
    profile_id: str,
    *,
    rule_id: str,
    event_type: str,
    severity: str,
    title: str,
    message: str,
    run_id: str | None = None,
    schedule_id: str | None = None,
    download_url: str | None = None,
    dedupe_key: str | None = None,
    actor: ResourceActor | None = None,
) -> dict[str, Any] | None:
    profile_id = str(profile_id or "").strip()
    if not profile_id:
        return None

    resolved_actor = resolve_resource_actor(actor)
    with NOTIFICATION_LOCK:
        _, settings_raw, profile, owner_user_id = _materialize_settings_raw(
            profile_id,
            actor=resolved_actor,
            action=ResourceAction.TEST_DELIVER,
        )
        if not _rule_enabled(settings_raw, rule_id):
            return None

        resolved_dedupe_key = str(dedupe_key or f"{profile_id}:{rule_id}:{run_id or schedule_id or slugify(title)}").strip()
        created_at = iso_now_utc()
        notification = {
            "notification_id": f"notification-{slugify(f'{profile_id}-{rule_id}-{created_at}')}",
            "profile_id": profile_id,
            "owner_user_id": owner_user_id,
            "rule_id": rule_id,
            "event_type": event_type,
            "severity": severity,
            "title": title,
            "message": message,
            "created_at": created_at,
            "run_id": run_id,
            "schedule_id": schedule_id,
            "download_url": download_url,
            "dedupe_key": resolved_dedupe_key,
        }

        def _register(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
            # Dedupe against the newest events inside the lock, so two writers
            # cannot both decide the key is new and emit a duplicate.
            duplicate = next(
                (
                    item
                    for item in records
                    if str(item.get("dedupe_key") or "").strip() == resolved_dedupe_key
                    and str(item.get("owner_user_id") or "").strip() == str(owner_user_id or "")
                ),
                None,
            )
            if duplicate is not None:
                return records, duplicate
            records.append(notification)
            return (
                _retain_owner_records(
                    records,
                    owner_user_id=owner_user_id,
                    limit=MAX_NOTIFICATION_RECORDS,
                ),
                None,
            )

        # Read-only pre-check: an already-emitted event needs no mutation, so it
        # must not enter the mutation boundary and create a lock file. The
        # authoritative dedupe still runs inside the lock below.
        duplicate = next(
            (
                item
                for item in _load_json_list(NOTIFICATION_EVENTS_PATH)
                if str(item.get("dedupe_key") or "").strip() == resolved_dedupe_key
                and str(item.get("owner_user_id") or "").strip() == str(owner_user_id or "")
            ),
            None,
        )
        if duplicate is None and bool(settings_raw.get("in_app_enabled", True)):
            duplicate = _mutate_records(
                NOTIFICATION_EVENTS_PATH, _register, touch_key="last_topic_notifications_updated_at"
            )
        if duplicate is not None:
            return _normalize_notification(duplicate, profile)

    # Delivery performs SMTP/webhook I/O with no state or process-local state
    # lock held; each result is appended under its own short mutation lock.
    _deliver_event(
        profile,
        settings_raw,
        notification,
        actor=resolved_actor,
        owner_user_id=owner_user_id,
    )
    return _normalize_notification(notification, profile)


def test_topic_notification_delivery(
    profile_id: str,
    *,
    channel_id: str,
    actor: ResourceActor | None = None,
) -> dict[str, Any]:
    profile_id = str(profile_id or "").strip()
    if channel_id not in {"email", "webhook"}:
        raise ValueError(f"Unsupported notification delivery channel: {channel_id}")
    resolved_actor = resolve_resource_actor(actor)
    with NOTIFICATION_LOCK:
        _, settings_raw, profile, owner_user_id = _materialize_settings_raw(
            profile_id,
            actor=resolved_actor,
            action=ResourceAction.TEST_DELIVER,
        )
        created_at = iso_now_utc()
        event = {
            "notification_id": f"notification-test-{slugify(f'{profile_id}-{channel_id}-{created_at}')}",
            "profile_id": profile_id,
            "owner_user_id": owner_user_id,
            "rule_id": "test_delivery",
            "event_type": "notification.test_delivery",
            "severity": "info",
            "title": "테스트 알림",
            "message": "알림 전달 채널 테스트입니다.",
            "created_at": created_at,
            "run_id": None,
            "schedule_id": None,
            "download_url": None,
        }
    deliveries = _deliver_event(
        profile,
        settings_raw,
        event,
        actor=resolved_actor,
        owner_user_id=owner_user_id,
        only_channel_id=channel_id,
    )
    return {"profile_id": profile_id, "channel_id": channel_id, "deliveries": deliveries}


def _topic_profile_ids_for_run(
    run: dict[str, Any],
    *,
    actor: ResourceActor,
) -> list[str]:
    explicit_profile_id = str(run.get("topic_profile_id") or "").strip()
    if explicit_profile_id:
        return [explicit_profile_id]

    topic_name = str(run.get("topic") or "").strip().lower()
    matched: list[str] = []
    for profile in topic_profiles.list_topic_profiles(actor=actor)["items"]:
        if topic_name in {
            str(profile.get("topic") or "").strip().lower(),
            str(profile.get("title") or "").strip().lower(),
        }:
            matched.append(str(profile.get("profile_id") or "").strip())
    return [profile_id for profile_id in matched if profile_id]


def _report_download_url(run: dict[str, Any]) -> str | None:
    artifacts = run.get("artifacts") or {}
    for field in ("research_plan_markdown", "report_markdown"):
        raw_path = str(artifacts.get(field) or "").strip()
        if raw_path and Path(raw_path).exists():
            return f"/runs/{run.get('run_id')}/report/download"

    reports_dir = build_run_paths(str(run.get("run_id") or "")).reports_dir
    if reports_dir.exists():
        candidates = sorted(
            [
                path
                for path in reports_dir.glob("*.md")
                if path.name != "preview.md" and not path.name.startswith("exported_")
            ]
        )
        if candidates:
            return f"/runs/{run.get('run_id')}/report/download"
    return None


def _metadata_containers(item: dict[str, Any]) -> list[dict[str, Any]]:
    containers: list[dict[str, Any]] = []
    pending = [item]
    seen: set[int] = set()
    while pending:
        container = pending.pop(0)
        container_id = id(container)
        if container_id in seen:
            continue
        seen.add(container_id)
        containers.append(container)
        for field in CITATION_CONTAINER_FIELDS:
            nested = container.get(field)
            if isinstance(nested, dict):
                pending.append(nested)
    return containers


def _coerce_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(round(value)))
    if isinstance(value, str):
        stripped = value.strip().replace(",", "")
        if not stripped:
            return None
        try:
            return max(0, int(float(stripped)))
        except ValueError:
            return None
    return None


def _citation_count(item: dict[str, Any]) -> int:
    for container in _metadata_containers(item):
        for field in CITATION_FIELDS:
            value = _coerce_int(container.get(field))
            if value is not None:
                return value
    return 0


def _high_impact_candidate(run_id: str) -> dict[str, Any] | None:
    try:
        items = workbench.load_run_triage_items(run_id)
    except FileNotFoundError:
        return None

    best_item: dict[str, Any] | None = None
    best_key: tuple[int, float] | None = None
    for item in items:
        citations = _citation_count(item)
        score = float(item.get("score", 0) or 0)
        if score < HIGH_IMPACT_SCORE_THRESHOLD and citations < HIGH_CITATION_THRESHOLD:
            continue
        candidate_key = (citations, score)
        if best_key is None or candidate_key > best_key:
            best_item = item
            best_key = candidate_key

    if best_item is None:
        return None

    return {
        "paper_id": str(best_item.get("paper_id") or "").strip(),
        "title": str(best_item.get("title") or "제목 없음").strip() or "제목 없음",
        "score": round(float(best_item.get("score", 0) or 0), 1),
        "citations": _citation_count(best_item),
    }


def _is_digest_run(run: dict[str, Any]) -> bool:
    source = str(run.get("source") or "").strip().lower()
    schedule_context = run.get("schedule_context") if isinstance(run.get("schedule_context"), dict) else {}
    task_type = str(schedule_context.get("task_type") or "").strip().lower()
    return task_type in {"daily_report", "weekly_report", "monthly_report"} or source.endswith(":report")


def _digest_label(run: dict[str, Any]) -> str:
    schedule_context = run.get("schedule_context") if isinstance(run.get("schedule_context"), dict) else {}
    task_type = str(schedule_context.get("task_type") or "").strip().lower()
    if task_type == "daily_report":
        return "일간 digest"
    if task_type == "weekly_report":
        return "주간 digest"
    if task_type == "monthly_report":
        return "월간 digest"
    return "정기 digest"


def emit_notifications_for_run(run: dict[str, Any]) -> list[dict[str, Any]]:
    run_id = str(run.get("run_id") or "").strip()
    status = str(run.get("status") or "").strip().lower()
    if not run_id or status not in {"completed", "failed"}:
        return []
    try:
        actor = scoped_system_actor(run.get("owner_user_id"))
    except ResourceAccessDenied:
        return []

    created: list[dict[str, Any]] = []
    profile_ids = _topic_profile_ids_for_run(run, actor=actor)
    if not profile_ids:
        return created

    download_url = _report_download_url(run)
    schedule_context = run.get("schedule_context") if isinstance(run.get("schedule_context"), dict) else {}
    schedule_id = str(schedule_context.get("schedule_id") or "").strip() or None
    topic_name = str(run.get("topic") or "토픽").strip() or "토픽"

    for profile_id in profile_ids:
        if status == "completed":
            if download_url:
                created_notification = create_topic_notification(
                    profile_id,
                    rule_id="report_completed",
                    event_type="report.completed",
                    severity="success",
                    title="보고서 생성 완료",
                    message=f"{topic_name} 주제의 보고서가 생성되었습니다.",
                    run_id=run_id,
                    schedule_id=schedule_id,
                    download_url=download_url,
                    dedupe_key=f"run:{run_id}:report_completed",
                    actor=actor,
                )
                if created_notification is not None:
                    created.append(created_notification)

            if download_url and _is_digest_run(run):
                digest_label = _digest_label(run)
                digest_notification = create_topic_notification(
                    profile_id,
                    rule_id="digest_available",
                    event_type="digest.available",
                    severity="success",
                    title=f"{digest_label} 준비 완료",
                    message=f"{topic_name} 주제의 {digest_label}가 준비되었습니다.",
                    run_id=run_id,
                    schedule_id=schedule_id,
                    download_url=download_url,
                    dedupe_key=f"run:{run_id}:digest_available",
                    actor=actor,
                )
                if digest_notification is not None:
                    created.append(digest_notification)

            high_impact = _high_impact_candidate(run_id)
            if high_impact is not None:
                citations = int(high_impact["citations"])
                score = float(high_impact["score"])
                citation_text = f"인용 {citations}" if citations > 0 else "인용 데이터 없음"
                high_impact_notification = create_topic_notification(
                    profile_id,
                    rule_id="high_impact_paper",
                    event_type="paper.high_impact_detected",
                    severity="info",
                    title="고영향력 논문 감지",
                    message=f"\"{high_impact['title']}\" 논문이 영향력 점수 {score} / {citation_text}로 감지되었습니다.",
                    run_id=run_id,
                    schedule_id=schedule_id,
                    dedupe_key=f"run:{run_id}:high_impact:{high_impact['paper_id'] or slugify(high_impact['title'])}",
                    actor=actor,
                )
                if high_impact_notification is not None:
                    created.append(high_impact_notification)

        if status == "failed":
            failed_notification = create_topic_notification(
                profile_id,
                rule_id="run_failed",
                event_type="run.failed",
                severity="error",
                title="실행 실패",
                message=f"{topic_name} 주제 실행이 실패했습니다. {str(run.get('error') or '').strip()}".strip(),
                run_id=run_id,
                schedule_id=schedule_id,
                dedupe_key=f"run:{run_id}:run_failed",
                actor=actor,
            )
            if failed_notification is not None:
                created.append(failed_notification)

            if schedule_id:
                automation_failure = create_topic_notification(
                    profile_id,
                    rule_id="automation_task_failure",
                    event_type="automation.failed",
                    severity="error",
                    title="예약 작업 실패",
                    message=f"{topic_name} 예약 작업이 실패했습니다. {str(run.get('error') or '').strip()}".strip(),
                    run_id=run_id,
                    schedule_id=schedule_id,
                    dedupe_key=f"run:{run_id}:automation_task_failure",
                    actor=actor,
                )
                if automation_failure is not None:
                    created.append(automation_failure)

    return created


def emit_schedule_failure_notification(
    schedule: dict[str, Any],
    *,
    error: str,
    run_id: str | None = None,
) -> dict[str, Any] | None:
    profile_id = str(schedule.get("profile_id") or "").strip()
    if not profile_id:
        return None
    try:
        actor = scoped_system_actor(schedule.get("owner_user_id"))
    except ResourceAccessDenied:
        return None
    title = str(schedule.get("title") or "예약 작업").strip() or "예약 작업"
    return create_topic_notification(
        profile_id,
        rule_id="automation_task_failure",
        event_type="automation.failed",
        severity="error",
        title="예약 작업 실패",
        message=f"{title} 자동 실행에 실패했습니다. {str(error or '').strip()}".strip(),
        run_id=run_id,
        schedule_id=str(schedule.get("schedule_id") or "").strip() or None,
        dedupe_key=f"schedule:{schedule.get('schedule_id')}:automation_task_failure:{slugify(str(error or 'error'))}",
        actor=actor,
    )
