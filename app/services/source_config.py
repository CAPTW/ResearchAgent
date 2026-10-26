from __future__ import annotations

from copy import deepcopy
from typing import Any

SUPPORTED_PROVIDER_IDS = ("arxiv", "openalex", "crossref")
REGISTRY_PROVIDER_IDS = ("arxiv", "openalex", "crossref")
DEFAULT_SOURCE_PROVIDER = "arxiv"
DEFAULT_ARXIV_SORT_BY = "submittedDate"
DEFAULT_ARXIV_SORT_ORDER = "descending"
DEFAULT_OPENALEX_SORT_BY = "relevance"
DEFAULT_OPENALEX_SORT_ORDER = "descending"
DEFAULT_CROSSREF_SORT_BY = "relevance"
DEFAULT_CROSSREF_SORT_ORDER = "descending"
DEFAULT_MAX_RESULTS = 60
DEFAULT_PRIORITY = 1

PROVIDER_CATALOG: dict[str, dict[str, Any]] = {
    "arxiv": {
        "provider": "arxiv",
        "label": "arXiv",
        "displayName": "arXiv",
        "description": "arXiv Atom API를 사용해 논문 메타데이터와 원문 링크를 수집합니다.",
        "category": "academic",
        "capabilities": {
            "papers": True,
            "citations": False,
            "authors": True,
            "institutions": False,
            "abstracts": True,
            "links": True,
            "date": True,
        },
        "supported": True,
        "available": True,
        "configured": True,
        "state": "available",
        "requiresCredentials": False,
        "statusLabel": "사용 가능",
        "unavailableReason": None,
        "supportedQueryFields": ["queries", "categories", "maxResults", "sortBy", "sortOrder", "priority"],
    },
    "openalex": {
        "provider": "openalex",
        "label": "OpenAlex",
        "displayName": "OpenAlex",
        "description": "OpenAlex Works API를 사용해 논문, 인용 수, 기관/소속 메타데이터를 수집합니다.",
        "category": "academic",
        "capabilities": {
            "papers": True,
            "citations": True,
            "authors": True,
            "institutions": True,
            "abstracts": True,
            "links": True,
            "date": True,
        },
        "supported": True,
        "available": True,
        "configured": True,
        "state": "available",
        "requiresCredentials": False,
        "statusLabel": "사용 가능",
        "unavailableReason": None,
        "supportedQueryFields": ["queries", "maxResults", "sortBy", "sortOrder", "priority"],
    },
    "crossref": {
        "provider": "crossref",
        "label": "Crossref",
        "displayName": "Crossref",
        "description": "Crossref REST API를 사용해 DOI 중심 학술 메타데이터와 인용 참조 수를 수집합니다.",
        "category": "academic",
        "capabilities": {
            "papers": True,
            "citations": True,
            "authors": True,
            "institutions": False,
            "abstracts": True,
            "links": True,
            "date": True,
        },
        "supported": True,
        "available": True,
        "configured": True,
        "state": "available",
        "requiresCredentials": False,
        "statusLabel": "사용 가능",
        "unavailableReason": None,
        "supportedQueryFields": ["queries", "maxResults", "sortBy", "sortOrder", "priority"],
    },
}


def _clean_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = value.split("\n") if "\n" in value else value.split(",")
    elif isinstance(value, list):
        raw_items = value
    else:
        return []
    return [str(item).strip() for item in raw_items if str(item).strip()]


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _valid_sort_by(value: Any, default: str = DEFAULT_ARXIV_SORT_BY) -> str:
    text = str(value or "").strip()
    return text if text in {"submittedDate", "lastUpdatedDate", "relevance"} else default


def _valid_sort_order(value: Any, default: str = DEFAULT_ARXIV_SORT_ORDER) -> str:
    text = str(value or "").strip()
    return text if text in {"ascending", "descending"} else default


def _valid_openalex_sort_by(value: Any, default: str = DEFAULT_OPENALEX_SORT_BY) -> str:
    text = str(value or "").strip()
    return text if text in {"relevance", "publication_date", "cited_by_count"} else default


def _valid_crossref_sort_by(value: Any, default: str = DEFAULT_CROSSREF_SORT_BY) -> str:
    text = str(value or "").strip()
    return text if text in {"relevance", "published", "deposited", "is-referenced-by-count"} else default


def provider_registry(*, checked_at: str | None = None) -> dict[str, Any]:
    items = []
    for provider_id in REGISTRY_PROVIDER_IDS:
        catalog = dict(PROVIDER_CATALOG[provider_id])
        catalog["lastCheckedAt"] = checked_at
        items.append(catalog)
    return {"checkedAt": checked_at, "items": items}


def validate_requested_source_settings(raw: dict[str, Any] | None) -> None:
    if not isinstance(raw, dict):
        return
    raw_providers = raw.get("providers") if isinstance(raw.get("providers"), dict) else {}
    unsupported_configured = sorted(str(provider).strip() for provider in raw_providers if str(provider).strip() not in REGISTRY_PROVIDER_IDS)
    if unsupported_configured:
        raise ValueError(f"Unsupported source provider configured: {unsupported_configured[0]}")
    enabled_raw = _clean_text_list(raw.get("enabledProviders"))
    unsupported_enabled = [provider for provider in enabled_raw if provider not in REGISTRY_PROVIDER_IDS]
    if unsupported_enabled:
        raise ValueError(f"Unsupported source provider enabled: {unsupported_enabled[0]}")
    gated_enabled = [provider for provider in enabled_raw if provider not in SUPPORTED_PROVIDER_IDS]
    if gated_enabled:
        raise ValueError(f"Source provider is not available for collection: {gated_enabled[0]}")
    default_provider = str(raw.get("defaultProvider") or "").strip()
    if default_provider and default_provider not in REGISTRY_PROVIDER_IDS:
        raise ValueError(f"Unsupported default source provider: {default_provider}")
    if default_provider and default_provider not in SUPPORTED_PROVIDER_IDS:
        raise ValueError(f"Default source provider is not available for collection: {default_provider}")


def normalize_source_settings(raw: dict[str, Any] | None = None, *, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    fallback = dict(fallback or {})
    raw_settings = deepcopy(raw) if isinstance(raw, dict) else {}
    raw_providers = raw_settings.get("providers") if isinstance(raw_settings.get("providers"), dict) else {}
    raw_arxiv = raw_providers.get("arxiv") if isinstance(raw_providers.get("arxiv"), dict) else {}
    raw_openalex = raw_providers.get("openalex") if isinstance(raw_providers.get("openalex"), dict) else {}
    raw_crossref = raw_providers.get("crossref") if isinstance(raw_providers.get("crossref"), dict) else {}

    enabled_providers = _clean_text_list(raw_settings.get("enabledProviders"))
    fallback_provider = str(fallback.get("source_provider") or DEFAULT_SOURCE_PROVIDER).strip()
    arxiv_enabled = raw_arxiv.get("enabled")
    if arxiv_enabled is None:
        arxiv_enabled = "arxiv" in enabled_providers if "enabledProviders" in raw_settings else fallback_provider != "openalex"
    arxiv_enabled = bool(arxiv_enabled)
    openalex_enabled = raw_openalex.get("enabled")
    if openalex_enabled is None:
        openalex_enabled = "openalex" in enabled_providers if "enabledProviders" in raw_settings else fallback_provider == "openalex"
    openalex_enabled = bool(openalex_enabled)
    crossref_enabled = raw_crossref.get("enabled")
    if crossref_enabled is None:
        crossref_enabled = "crossref" in enabled_providers if "enabledProviders" in raw_settings else fallback_provider == "crossref"
    crossref_enabled = bool(crossref_enabled)

    queries = _clean_text_list(raw_arxiv.get("queries")) or _clean_text_list(fallback.get("queries"))
    categories = _clean_text_list(raw_arxiv.get("categories")) or _clean_text_list(fallback.get("categories"))
    max_results = _bounded_int(
        raw_arxiv.get("maxResults", fallback.get("max_results", fallback.get("maxResults", DEFAULT_MAX_RESULTS))),
        default=DEFAULT_MAX_RESULTS,
        minimum=1,
        maximum=200,
    )
    priority = _bounded_int(raw_arxiv.get("priority", DEFAULT_PRIORITY), default=DEFAULT_PRIORITY, minimum=1, maximum=10)
    sort_by = _valid_sort_by(raw_arxiv.get("sortBy", fallback.get("arxiv_sort_by", DEFAULT_ARXIV_SORT_BY)))
    sort_order = _valid_sort_order(raw_arxiv.get("sortOrder", fallback.get("arxiv_sort_order", DEFAULT_ARXIV_SORT_ORDER)))
    openalex_queries = _clean_text_list(raw_openalex.get("queries")) or _clean_text_list(fallback.get("openalex_queries")) or queries
    openalex_max_results = _bounded_int(
        raw_openalex.get("maxResults", fallback.get("openalex_max_results", max_results)),
        default=DEFAULT_MAX_RESULTS,
        minimum=1,
        maximum=200,
    )
    openalex_priority = _bounded_int(raw_openalex.get("priority", 2), default=2, minimum=1, maximum=10)
    openalex_sort_by = _valid_openalex_sort_by(raw_openalex.get("sortBy", fallback.get("openalex_sort_by", DEFAULT_OPENALEX_SORT_BY)))
    openalex_sort_order = _valid_sort_order(raw_openalex.get("sortOrder", fallback.get("openalex_sort_order", DEFAULT_OPENALEX_SORT_ORDER)))
    crossref_queries = _clean_text_list(raw_crossref.get("queries")) or _clean_text_list(fallback.get("crossref_queries")) or queries
    crossref_max_results = _bounded_int(
        raw_crossref.get("maxResults", fallback.get("crossref_max_results", max_results)),
        default=DEFAULT_MAX_RESULTS,
        minimum=1,
        maximum=200,
    )
    crossref_priority = _bounded_int(raw_crossref.get("priority", 3), default=3, minimum=1, maximum=10)
    crossref_sort_by = _valid_crossref_sort_by(raw_crossref.get("sortBy", fallback.get("crossref_sort_by", DEFAULT_CROSSREF_SORT_BY)))
    crossref_sort_order = _valid_sort_order(raw_crossref.get("sortOrder", fallback.get("crossref_sort_order", DEFAULT_CROSSREF_SORT_ORDER)))
    normalized_enabled = [
        provider
        for provider, enabled in (("arxiv", arxiv_enabled), ("openalex", openalex_enabled), ("crossref", crossref_enabled))
        if enabled
    ]
    default_provider = str(raw_settings.get("defaultProvider") or fallback_provider or DEFAULT_SOURCE_PROVIDER).strip()
    if default_provider not in normalized_enabled:
        default_provider = normalized_enabled[0] if normalized_enabled else DEFAULT_SOURCE_PROVIDER

    return {
        "defaultProvider": default_provider,
        "enabledProviders": normalized_enabled,
        "providers": {
            "arxiv": {
                "provider": "arxiv",
                "enabled": arxiv_enabled,
                "priority": priority,
                "queries": queries,
                "categories": categories,
                "maxResults": max_results,
                "sortBy": sort_by,
                "sortOrder": sort_order,
            },
            "openalex": {
                "provider": "openalex",
                "enabled": openalex_enabled,
                "priority": openalex_priority,
                "queries": openalex_queries,
                "maxResults": openalex_max_results,
                "sortBy": openalex_sort_by,
                "sortOrder": openalex_sort_order,
            },
            "crossref": {
                "provider": "crossref",
                "enabled": crossref_enabled,
                "priority": crossref_priority,
                "queries": crossref_queries,
                "maxResults": crossref_max_results,
                "sortBy": crossref_sort_by,
                "sortOrder": crossref_sort_order,
            },
        },
    }


def validate_source_settings(settings: dict[str, Any]) -> None:
    configured_providers = set(settings.get("providers", {}))
    unsupported_configured = configured_providers.difference(REGISTRY_PROVIDER_IDS)
    if unsupported_configured:
        raise ValueError(f"Unsupported source provider configured: {sorted(unsupported_configured)[0]}")
    enabled_raw = [str(provider).strip() for provider in settings.get("enabledProviders", []) if str(provider).strip()]
    unsupported_enabled = [provider for provider in enabled_raw if provider not in SUPPORTED_PROVIDER_IDS]
    if unsupported_enabled:
        raise ValueError(f"Unsupported source provider enabled: {unsupported_enabled[0]}")
    gated_enabled = [
        provider
        for provider in enabled_raw
        if provider in PROVIDER_CATALOG and provider not in SUPPORTED_PROVIDER_IDS
    ]
    if gated_enabled:
        raise ValueError(f"Source provider is not available for collection: {gated_enabled[0]}")
    enabled = [provider for provider in enabled_raw if provider in SUPPORTED_PROVIDER_IDS]
    if not enabled:
        raise ValueError("At least one supported source provider must be enabled.")
    default_provider = str(settings.get("defaultProvider") or "").strip()
    if default_provider not in enabled:
        raise ValueError("Default source provider must be enabled and supported.")


def flatten_source_settings(settings: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_source_settings(settings)
    default_provider = normalized["defaultProvider"]
    default_config = normalized["providers"].get(default_provider) or normalized["providers"]["arxiv"]
    arxiv = normalized["providers"]["arxiv"]
    return {
        "source_provider": normalized["defaultProvider"],
        "queries": list(default_config.get("queries", [])),
        "categories": list(arxiv.get("categories", [])) if default_provider == "arxiv" else [],
        "max_results": int(default_config.get("maxResults") or DEFAULT_MAX_RESULTS),
        "arxiv_sort_by": str(arxiv.get("sortBy") or DEFAULT_ARXIV_SORT_BY),
        "arxiv_sort_order": str(arxiv.get("sortOrder") or DEFAULT_ARXIV_SORT_ORDER),
    }


def run_sources_from_settings(settings: dict[str, Any], *, base_sources: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_source_settings(settings)
    validate_source_settings(normalized)
    sources: dict[str, Any] = {}
    if "arxiv" in normalized["enabledProviders"]:
        arxiv = normalized["providers"]["arxiv"]
        arxiv_config = dict(base_sources.get("arxiv", {}))
        arxiv_config["queries"] = list(arxiv.get("queries", []))
        arxiv_config["categories"] = list(arxiv.get("categories", []))
        arxiv_config["max_results"] = int(arxiv.get("maxResults") or DEFAULT_MAX_RESULTS)
        arxiv_config["sort_by"] = str(arxiv.get("sortBy") or DEFAULT_ARXIV_SORT_BY)
        arxiv_config["sort_order"] = str(arxiv.get("sortOrder") or DEFAULT_ARXIV_SORT_ORDER)
        arxiv_config["priority"] = int(arxiv.get("priority") or DEFAULT_PRIORITY)
        sources["arxiv"] = arxiv_config
    if "openalex" in normalized["enabledProviders"]:
        openalex = normalized["providers"]["openalex"]
        openalex_config = dict(base_sources.get("openalex", {}))
        openalex_config["queries"] = list(openalex.get("queries", []))
        openalex_config["max_results"] = int(openalex.get("maxResults") or DEFAULT_MAX_RESULTS)
        openalex_config["sort_by"] = str(openalex.get("sortBy") or DEFAULT_OPENALEX_SORT_BY)
        openalex_config["sort_order"] = str(openalex.get("sortOrder") or DEFAULT_OPENALEX_SORT_ORDER)
        openalex_config["priority"] = int(openalex.get("priority") or 2)
        sources["openalex"] = openalex_config
    if "crossref" in normalized["enabledProviders"]:
        crossref = normalized["providers"]["crossref"]
        crossref_config = dict(base_sources.get("crossref", {}))
        crossref_config["queries"] = list(crossref.get("queries", []))
        crossref_config["max_results"] = int(crossref.get("maxResults") or DEFAULT_MAX_RESULTS)
        crossref_config["sort_by"] = str(crossref.get("sortBy") or DEFAULT_CROSSREF_SORT_BY)
        crossref_config["sort_order"] = str(crossref.get("sortOrder") or DEFAULT_CROSSREF_SORT_ORDER)
        crossref_config["priority"] = int(crossref.get("priority") or 3)
        sources["crossref"] = crossref_config
    return sources


def source_statuses(
    settings: dict[str, Any],
    *,
    last_success_by_provider: dict[str, str | None] | None = None,
) -> list[dict[str, Any]]:
    normalized = normalize_source_settings(settings)
    last_success_by_provider = last_success_by_provider or {}
    statuses: list[dict[str, Any]] = []
    for provider_id in REGISTRY_PROVIDER_IDS:
        catalog = dict(PROVIDER_CATALOG[provider_id])
        provider_config = normalized["providers"].get(provider_id, {})
        enabled = provider_id in normalized.get("enabledProviders", [])
        statuses.append(
            {
                **catalog,
                "enabled": enabled,
                "default": normalized.get("defaultProvider") == provider_id,
                "priority": int(provider_config.get("priority") or DEFAULT_PRIORITY),
                "lastSuccessfulCollectionAt": last_success_by_provider.get(provider_id),
            }
        )
    return statuses


def source_summary(
    settings: dict[str, Any],
    *,
    recent_paper_counts: dict[str, int] | None = None,
    last_success_by_provider: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    normalized = normalize_source_settings(settings)
    recent_paper_counts = recent_paper_counts or {}
    return {
        "defaultProvider": normalized["defaultProvider"],
        "enabledProviders": list(normalized["enabledProviders"]),
        "statuses": source_statuses(normalized, last_success_by_provider=last_success_by_provider),
        "recentContributions": [
            {
                "provider": provider_id,
                "label": PROVIDER_CATALOG[provider_id]["label"],
                "paperCount": int(recent_paper_counts.get(provider_id, 0) or 0),
                "lastSuccessfulCollectionAt": (last_success_by_provider or {}).get(provider_id),
            }
            for provider_id in normalized["enabledProviders"]
            if provider_id in PROVIDER_CATALOG
        ],
    }
