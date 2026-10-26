from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any

from app.services import research_policy, source_config, topic_profiles

TOPIC_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,80}$")
DOMAIN_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
SECRET_KEY_PARTS = ("secret", "token", "api_key", "apikey", "password", "credential", "authorization")

CONFIG_FIELDS = (
    "topicId",
    "displayName",
    "queryTerms",
    "includeKeywords",
    "excludeKeywords",
    "minRelevance",
    "defaultUniqueLimit",
    "defaultAllowDomains",
    "defaultDownloadLimit",
    "defaultMaxPdfBytes",
    "recommendationLimit",
    "followupLimit",
    "providerModeDefault",
    "liveActionsEnabled",
    "notes",
    "acquisitionLanguage",
    "reviewReportLanguage",
    "acquisitionStartDate",
    "acquisitionEndDate",
)
EDITABLE_FIELDS = tuple(field for field in CONFIG_FIELDS if field != "topicId")
PROVIDER_MODES = {"none", "stub", "command_dry_run"}


def _dedupe_preserve_order(values: list[Any]) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = text.lower()
        if text and key not in seen:
            items.append(text)
            seen.add(key)
    return items


def _coerce_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return float(stripped.replace(",", ""))
        except ValueError:
            return None
    return None


def _coerce_int(value: Any) -> int | None:
    number = _coerce_float(value)
    if number is None:
        return None
    return int(round(number))


def _bounded_int(value: Any, fallback: int, *, minimum: int, maximum: int) -> tuple[int, bool]:
    number = _coerce_int(value)
    if number is None:
        return fallback, value is not None
    bounded = min(max(number, minimum), maximum)
    return bounded, bounded != number


def _bounded_float(value: Any, fallback: float, *, minimum: float, maximum: float) -> tuple[float, bool]:
    number = _coerce_float(value)
    if number is None:
        return fallback, value is not None
    bounded = min(max(number, minimum), maximum)
    return bounded, bounded != number


def _provider_config(profile: dict[str, Any]) -> dict[str, Any]:
    source_settings = profile.get("source_settings") if isinstance(profile.get("source_settings"), dict) else {}
    providers = source_settings.get("providers") if isinstance(source_settings.get("providers"), dict) else {}
    provider = str(profile.get("source_provider") or source_settings.get("defaultProvider") or "arxiv").strip().lower() or "arxiv"
    config = providers.get(provider) if isinstance(providers.get(provider), dict) else {}
    return config


def _default_allow_domains(profile: dict[str, Any]) -> list[str]:
    provider = str(profile.get("source_provider") or "arxiv").strip().lower()
    if provider == "arxiv":
        return ["arxiv.org"]
    return ["arxiv.org"]


def _domain_is_valid(value: str) -> bool:
    text = value.strip().lower()
    return bool(DOMAIN_PATTERN.fullmatch(text)) and "://" not in text and "/" not in text


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = value.splitlines() if "\n" in value else value.split(",")
        return _dedupe_preserve_order(raw_items)
    if isinstance(value, list):
        return _dedupe_preserve_order(value)
    return _dedupe_preserve_order([value])


def _operation_config(profile: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(profile.get("operations_config")) if isinstance(profile.get("operations_config"), dict) else {}


def _snake_to_camel_config(raw: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "min_relevance": "minRelevance",
        "default_unique_limit": "defaultUniqueLimit",
        "default_allow_domains": "defaultAllowDomains",
        "default_download_limit": "defaultDownloadLimit",
        "default_max_pdf_bytes": "defaultMaxPdfBytes",
        "recommendation_limit": "recommendationLimit",
        "followup_limit": "followupLimit",
        "provider_mode_default": "providerModeDefault",
        "live_actions_enabled": "liveActionsEnabled",
        "include_keywords": "includeKeywords",
        "exclude_keywords": "excludeKeywords",
    }
    result: dict[str, Any] = {}
    for key, value in raw.items():
        result[mapping.get(key, key)] = value
    return result


def operations_config_from_summary(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "include_keywords": list(config.get("includeKeywords") or []),
        "exclude_keywords": list(config.get("excludeKeywords") or []),
        "min_relevance": float(config.get("minRelevance") or 60),
        "default_unique_limit": int(config.get("defaultUniqueLimit") or 50),
        "default_allow_domains": list(config.get("defaultAllowDomains") or ["arxiv.org"]),
        "default_download_limit": int(config.get("defaultDownloadLimit") or 5),
        "default_max_pdf_bytes": int(config.get("defaultMaxPdfBytes") or 25_000_000),
        "recommendation_limit": int(config.get("recommendationLimit") or 5),
        "followup_limit": int(config.get("followupLimit") or 10),
        "provider_mode_default": str(config.get("providerModeDefault") or "none"),
        "live_actions_enabled": bool(config.get("liveActionsEnabled") or False),
        "notes": list(config.get("notes") or []),
    }


def build_topic_configuration_summary(profile: dict[str, Any], draft_config: dict[str, Any] | None = None) -> dict[str, Any]:
    topic_id = str(profile.get("profile_id") or "").strip()
    display_name = str(profile.get("title") or profile.get("topic") or topic_id).strip()
    provider_config = _provider_config(profile)
    query_terms = _string_list(provider_config.get("queries") if provider_config.get("queries") else profile.get("queries"))
    categories = _string_list(provider_config.get("categories") if provider_config.get("categories") else profile.get("categories"))
    raw_max_results = provider_config.get("maxResults", profile.get("max_results"))
    default_unique_limit, adjusted_unique_limit = _bounded_int(raw_max_results, 50, minimum=1, maximum=500)
    default_unique_limit = min(default_unique_limit, 50)
    config: dict[str, Any] = {
        "topicId": topic_id,
        "displayName": display_name or topic_id,
        "queryTerms": query_terms,
        "includeKeywords": _dedupe_preserve_order([*query_terms, *categories]),
        "excludeKeywords": [],
        "minRelevance": 60.0,
        "defaultUniqueLimit": default_unique_limit,
        "defaultAllowDomains": _default_allow_domains(profile),
        "defaultDownloadLimit": 5,
        "defaultMaxPdfBytes": 25_000_000,
        "recommendationLimit": 5,
        "followupLimit": 10,
        "providerModeDefault": "none",
        "liveActionsEnabled": False,
        "notes": [
            "Dashboard plans are preview-only and must be run manually in the local CLI.",
            "Live provider execution is disabled by default and not exposed from the dashboard.",
        ],
        "acquisitionLanguage": research_policy.policy_from_profile(profile)["acquisition_language"],
        "reviewReportLanguage": research_policy.policy_from_profile(profile)["review_report_language"],
        "acquisitionStartDate": research_policy.policy_from_profile(profile)["acquisition_start_date"],
        "acquisitionEndDate": research_policy.policy_from_profile(profile)["acquisition_end_date"],
        "validationWarnings": [],
    }
    config.update(_snake_to_camel_config(_operation_config(profile)))
    if draft_config:
        config.update({key: value for key, value in draft_config.items() if key in CONFIG_FIELDS})

    config["queryTerms"] = _string_list(config.get("queryTerms"))
    config["includeKeywords"] = _string_list(config.get("includeKeywords"))
    config["excludeKeywords"] = _string_list(config.get("excludeKeywords"))
    config["defaultAllowDomains"] = [domain.lower() for domain in _string_list(config.get("defaultAllowDomains"))]
    config["notes"] = _string_list(config.get("notes"))
    config["minRelevance"], min_adjusted = _bounded_float(config.get("minRelevance"), 60.0, minimum=0, maximum=100)
    config["defaultUniqueLimit"], unique_adjusted = _bounded_int(config.get("defaultUniqueLimit"), 50, minimum=1, maximum=500)
    config["defaultDownloadLimit"], download_adjusted = _bounded_int(config.get("defaultDownloadLimit"), 5, minimum=1, maximum=50)
    config["defaultMaxPdfBytes"], bytes_adjusted = _bounded_int(
        config.get("defaultMaxPdfBytes"), 25_000_000, minimum=1_048_576, maximum=100_000_000
    )
    config["recommendationLimit"], rec_adjusted = _bounded_int(config.get("recommendationLimit"), 5, minimum=1, maximum=50)
    config["followupLimit"], followup_adjusted = _bounded_int(config.get("followupLimit"), 10, minimum=1, maximum=100)
    raw_provider_mode = str(config.get("providerModeDefault") or "none").strip() or "none"
    config["providerModeDefault"] = raw_provider_mode if raw_provider_mode in PROVIDER_MODES else "none"
    config["liveActionsEnabled"] = bool(config.get("liveActionsEnabled") or False)

    warnings: list[str] = []
    if not TOPIC_ID_PATTERN.fullmatch(str(config.get("topicId") or "")):
        warnings.append("topic_id must use lowercase letters, numbers, and hyphens for safe CLI usage.")
    if not config["queryTerms"]:
        warnings.append("query_terms are empty; collection and recommendation previews may be too broad.")
    if adjusted_unique_limit or unique_adjusted:
        warnings.append("default_unique_limit was adjusted to a safe bounded value.")
    if min_adjusted:
        warnings.append("min_relevance was adjusted to the 0-100 range.")
    if download_adjusted:
        warnings.append("default_download_limit was adjusted to the 1-50 range.")
    if bytes_adjusted:
        warnings.append("default_max_pdf_bytes was adjusted to the safe byte range.")
    if rec_adjusted:
        warnings.append("recommendation_limit was adjusted to the 1-50 range.")
    if followup_adjusted:
        warnings.append("followup_limit was adjusted to the 1-100 range.")
    invalid_domains = [domain for domain in config["defaultAllowDomains"] if not _domain_is_valid(domain)]
    if invalid_domains:
        warnings.append(f"default_allow_domains contains invalid domains: {', '.join(invalid_domains)}")
    if raw_provider_mode not in PROVIDER_MODES:
        warnings.append("provider_mode_default must be none, stub, or command_dry_run.")
    if config["liveActionsEnabled"]:
        warnings.append("live_actions_enabled cannot be enabled from dashboard topic configuration.")
    config["validationWarnings"] = list(dict.fromkeys([*config.get("validationWarnings", []), *warnings]))
    return config


def _secret_paths(value: Any, prefix: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            text_key = str(key)
            normalized = text_key.lower().replace("-", "_")
            path = f"{prefix}.{text_key}" if prefix else text_key
            if any(part in normalized for part in SECRET_KEY_PARTS):
                paths.append(path)
            paths.extend(_secret_paths(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.extend(_secret_paths(child, f"{prefix}[{index}]"))
    return paths


def _normalize_draft(current: dict[str, Any], draft: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    normalized = deepcopy(current)
    warnings: list[str] = []
    for field in CONFIG_FIELDS:
        if field not in draft:
            continue
        value = draft[field]
        if field in {"queryTerms", "includeKeywords", "excludeKeywords", "defaultAllowDomains", "notes"}:
            normalized[field] = _string_list(value)
        elif field in {"minRelevance"}:
            normalized[field] = _coerce_float(value)
        elif field in {"defaultUniqueLimit", "defaultDownloadLimit", "defaultMaxPdfBytes", "recommendationLimit", "followupLimit"}:
            normalized[field] = _coerce_int(value)
        elif field == "liveActionsEnabled":
            normalized[field] = bool(value)
        elif field in {"acquisitionStartDate", "acquisitionEndDate"}:
            normalized[field] = None if value is None or not str(value).strip() else str(value).strip()
        elif field in {"acquisitionLanguage", "reviewReportLanguage"}:
            normalized[field] = str(value or "").strip()
        else:
            normalized[field] = str(value or "").strip()
    bounded = build_topic_configuration_summary(
        {
            "profile_id": current["topicId"],
            "title": current["displayName"],
            "queries": current["queryTerms"],
            "categories": [],
            "max_results": current["defaultUniqueLimit"],
            "source_provider": "arxiv",
            "operations_config": operations_config_from_summary(normalized),
        },
        draft_config=normalized,
    )
    warnings.extend(bounded.get("validationWarnings", []))
    return bounded, warnings


def _config_hash(config: dict[str, Any]) -> str:
    stable = {key: value for key, value in config.items() if key != "validationWarnings"}
    payload = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _diff(current: dict[str, Any], draft: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    changed_fields: list[str] = []
    diff: list[dict[str, Any]] = []
    for field in CONFIG_FIELDS:
        if current.get(field) == draft.get(field):
            continue
        changed_fields.append(field)
        diff.append({"field": field, "currentValue": current.get(field), "draftValue": draft.get(field)})
    return changed_fields, diff


def _validate_config(topic_id: str, current: dict[str, Any], draft: dict[str, Any], raw_payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = list(draft.get("validationWarnings", []))
    secret_paths = _secret_paths(raw_payload)
    if secret_paths:
        errors.append(f"provider secrets or credentials are not allowed in topic configuration: {', '.join(secret_paths)}")
    if not TOPIC_ID_PATTERN.fullmatch(str(draft.get("topicId") or "")):
        errors.append("topic_id must be stable and safe for file/URL usage.")
    if str(draft.get("topicId") or "") != topic_id:
        errors.append("topic_id is read-only for existing topic configuration edits.")
    if not str(draft.get("displayName") or "").strip():
        errors.append("display_name must be non-empty.")
    if not draft.get("queryTerms"):
        errors.append("query_terms must include at least one query.")
    invalid_domains = [domain for domain in draft.get("defaultAllowDomains", []) if not _domain_is_valid(str(domain))]
    if invalid_domains:
        errors.append(f"default_allow_domains contains invalid domains: {', '.join(invalid_domains)}")
    if int(draft.get("defaultDownloadLimit") or 0) <= 0:
        errors.append("default_download_limit must be present and greater than zero.")
    if int(draft.get("defaultMaxPdfBytes") or 0) < 1_048_576:
        errors.append("default_max_pdf_bytes must be at least 1048576.")
    raw_provider_mode = str(raw_payload.get("providerModeDefault") or draft.get("providerModeDefault") or "none")
    if raw_provider_mode not in PROVIDER_MODES:
        errors.append("provider_mode_default must be none, stub, or command_dry_run.")
    if bool(draft.get("liveActionsEnabled")):
        errors.append("live_actions_enabled cannot be enabled from dashboard topic configuration.")
    try:
        research_policy.normalize_research_policy(
            {
                "acquisitionLanguage": draft.get("acquisitionLanguage"),
                "reviewReportLanguage": draft.get("reviewReportLanguage"),
                "acquisitionStartDate": draft.get("acquisitionStartDate"),
                "acquisitionEndDate": draft.get("acquisitionEndDate"),
            }
        )
    except research_policy.ResearchPolicyError as exc:
        errors.append(exc.message)
    return list(dict.fromkeys(errors)), list(dict.fromkeys(warnings))


def planner_profile_from_config(profile: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    planner_profile = deepcopy(profile)
    planner_profile["profile_id"] = config.get("topicId") or profile.get("profile_id")
    planner_profile["title"] = config.get("displayName") or profile.get("title")
    planner_profile["queries"] = list(config.get("queryTerms") or [])
    planner_profile.update(
        research_policy.normalize_research_policy(
            {
                "acquisitionLanguage": config.get("acquisitionLanguage"),
                "reviewReportLanguage": config.get("reviewReportLanguage"),
                "acquisitionStartDate": config.get("acquisitionStartDate"),
                "acquisitionEndDate": config.get("acquisitionEndDate"),
            }
        )
    )
    planner_profile["operations_config"] = operations_config_from_summary(config)
    source_settings = source_config.normalize_source_settings(
        profile.get("source_settings"),
        fallback={
            "queries": planner_profile.get("queries", []),
            "categories": profile.get("categories", []),
            "max_results": int(config.get("defaultUniqueLimit") or profile.get("max_results") or 50),
            "source_provider": profile.get("source_provider") or "arxiv",
            "arxiv_sort_by": profile.get("arxiv_sort_by") or "submittedDate",
            "arxiv_sort_order": profile.get("arxiv_sort_order") or "descending",
        },
    )
    providers = source_settings.setdefault("providers", {})
    arxiv = providers.setdefault("arxiv", {})
    arxiv["queries"] = list(config.get("queryTerms") or [])
    planner_profile["source_settings"] = source_settings
    return planner_profile


def validate_topic_configuration(topic_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    profile = topic_profiles.get_topic_profile(str(topic_id or "").strip())
    resolved_topic_id = str(profile.get("profile_id") or topic_id)
    current = build_topic_configuration_summary(profile)
    raw_payload = payload or {}
    draft, warnings = _normalize_draft(current, raw_payload)
    errors, validation_warnings = _validate_config(resolved_topic_id, current, draft, raw_payload)
    draft["validationWarnings"] = list(dict.fromkeys([*warnings, *validation_warnings]))
    changed_fields, diff = _diff(current, draft)
    result = {
        "topicId": resolved_topic_id,
        "validationStatus": "invalid" if errors else "valid",
        "topicConfiguration": draft,
        "changedFields": changed_fields,
        "diff": diff,
        "warnings": draft["validationWarnings"],
        "errors": errors,
        "configHash": _config_hash(draft),
        "savedPath": None,
        "_plannerProfile": planner_profile_from_config(profile, draft),
    }
    return result


def current_topic_configuration(topic_id: str) -> dict[str, Any]:
    return validate_topic_configuration(topic_id, {})


def save_topic_configuration(topic_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    result = validate_topic_configuration(topic_id, payload)
    if result["validationStatus"] == "invalid":
        return result
    config = result["topicConfiguration"]
    topic_profiles.update_topic_profile(
        result["topicId"],
        {
            "title": config["displayName"],
            "queries": config["queryTerms"],
            "operations_config": operations_config_from_summary(config),
            "acquisition_language": config.get("acquisitionLanguage"),
            "review_report_language": config.get("reviewReportLanguage"),
            "acquisition_start_date": config.get("acquisitionStartDate"),
            "acquisition_end_date": config.get("acquisitionEndDate"),
        },
        touch_metadata=False,
    )
    saved_profile = topic_profiles.get_topic_profile(result["topicId"])
    saved_config = build_topic_configuration_summary(saved_profile)
    result["topicConfiguration"] = saved_config
    result["validationStatus"] = "saved"
    result["configHash"] = _config_hash(saved_config)
    result["savedPath"] = str(topic_profiles.PROFILE_LIBRARY_PATH)
    result["_plannerProfile"] = planner_profile_from_config(saved_profile, saved_config)
    return result


def list_topic_configurations() -> dict[str, Any]:
    items = []
    warnings: list[str] = []
    for profile in topic_profiles.list_topic_profiles().get("items", []):
        config = build_topic_configuration_summary(profile)
        items.append(
            {
                "topicId": config["topicId"],
                "topicConfiguration": config,
                "configHash": _config_hash(config),
                "warnings": config.get("validationWarnings", []),
            }
        )
        warnings.extend(config.get("validationWarnings", []))
    return {"items": items, "warnings": list(dict.fromkeys(warnings))}
