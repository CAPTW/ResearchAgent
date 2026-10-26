from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable

from app.auth.resource_access import (
    ResourceAction,
    ResourceActor,
    ResourceKind,
    authorize_resource_access,
    owner_user_id_for_actor,
    reject_client_authority_fields,
    resolve_resource_actor,
    resource_visible_to_actor,
)
from app.domain.constants import STORAGE_ROOT
from app.repositories.files import iso_now_utc, slugify
from app.repositories import metadata_index, shared_state
from app.services import research_policy, source_config
from app.services.profile import load_default_profile

STATE_NAME = "topic_profiles"
PROFILE_LIBRARY_DIR = STORAGE_ROOT.parent / "topic_profiles"
PROFILE_LIBRARY_PATH = PROFILE_LIBRARY_DIR / "profiles.json"
ACTIVE_TOPIC_STATUS = "active"
ARCHIVED_TOPIC_STATUS = "archived"
DEFAULT_COLLECTION_CADENCE = "daily_0900"
DEFAULT_REPORT_CADENCE = "weekly_monday_1000"
DEFAULT_SOURCE_PROVIDER = "arxiv"
DEFAULT_ARXIV_SORT_BY = "submittedDate"
DEFAULT_ARXIV_SORT_ORDER = "descending"
SYSTEM_TEMPLATE_PROFILE_IDS = frozenset({"large-language-models", "default-xrml"})


def _default_source_settings() -> tuple[str, str, str]:
    default_profile = load_default_profile()
    arxiv = dict(default_profile.get("sources", {}).get("arxiv", {}))
    source_provider = str(default_profile.get("source_provider") or DEFAULT_SOURCE_PROVIDER).strip() or DEFAULT_SOURCE_PROVIDER
    sort_by = str(arxiv.get("sort_by") or DEFAULT_ARXIV_SORT_BY).strip() or DEFAULT_ARXIV_SORT_BY
    sort_order = str(arxiv.get("sort_order") or DEFAULT_ARXIV_SORT_ORDER).strip() or DEFAULT_ARXIV_SORT_ORDER
    return source_provider, sort_by, sort_order


def _seed_default_profiles() -> list[dict[str, Any]]:
    default_profile = load_default_profile()
    arxiv = dict(default_profile.get("sources", {}).get("arxiv", {}))
    now = iso_now_utc()
    source_provider, sort_by, sort_order = _default_source_settings()
    llm_queries = [
        '"large language model" OR LLM OR "foundation model"',
        '"retrieval augmented generation" OR RAG OR "tool use"',
        '"alignment" OR "reasoning" OR "agent"',
    ]
    llm_categories = ["cs.CL", "cs.AI", "cs.LG"]
    llm_source_settings = source_config.normalize_source_settings(
        fallback={
            "queries": llm_queries,
            "categories": llm_categories,
            "max_results": 80,
            "source_provider": source_provider,
            "arxiv_sort_by": sort_by,
            "arxiv_sort_order": sort_order,
        }
    )
    default_source_settings = source_config.normalize_source_settings(
        fallback={
            "queries": list(arxiv.get("queries", [])),
            "categories": list(arxiv.get("categories", [])),
            "max_results": int(arxiv.get("max_results", 60) or 60),
            "source_provider": source_provider,
            "arxiv_sort_by": sort_by,
            "arxiv_sort_order": sort_order,
        }
    )
    return [
        {
            "profile_id": "large-language-models",
            "resource_kind": ResourceKind.SYSTEM_TEMPLATE.value,
            "owner_user_id": None,
            "title": "Large Language Models",
            "description": "LLM 논문과 기술 동향을 추적하는 기본 토픽입니다.",
            "topic": "Large Language Models",
            "queries": llm_queries,
            "categories": llm_categories,
            "max_results": 80,
            "source_provider": source_provider,
            "arxiv_sort_by": sort_by,
            "arxiv_sort_order": sort_order,
            "source_settings": llm_source_settings,
            "top_papers": 5,
            "weekly_top": 20,
            "is_favorite": True,
            "status": ACTIVE_TOPIC_STATUS,
            "archived_at": None,
            "default_collection_cadence": DEFAULT_COLLECTION_CADENCE,
            "default_report_cadence": DEFAULT_REPORT_CADENCE,
            "created_at": now,
            "updated_at": now,
        },
        {
            "profile_id": "default-xrml",
            "resource_kind": ResourceKind.SYSTEM_TEMPLATE.value,
            "owner_user_id": None,
            "title": "Default XRML Monitor",
            "description": "Current repo defaults for XR/AR x ML retrieval.",
            "topic": default_profile.get("topic", "XR/AR × ML (Image/Video)"),
            "queries": list(arxiv.get("queries", [])),
            "categories": list(arxiv.get("categories", [])),
            "max_results": int(arxiv.get("max_results", 60) or 60),
            "source_provider": source_provider,
            "arxiv_sort_by": sort_by,
            "arxiv_sort_order": sort_order,
            "source_settings": default_source_settings,
            "top_papers": 3,
            "weekly_top": 15,
            "is_favorite": False,
            "status": ACTIVE_TOPIC_STATUS,
            "archived_at": None,
            "default_collection_cadence": DEFAULT_COLLECTION_CADENCE,
            "default_report_cadence": DEFAULT_REPORT_CADENCE,
            "created_at": now,
            "updated_at": now,
        }
    ]


def _normalize_status(value: Any, *, archived_at: Any = None) -> str:
    status = str(value or "").strip().lower()
    if status not in {ACTIVE_TOPIC_STATUS, ARCHIVED_TOPIC_STATUS}:
        return ARCHIVED_TOPIC_STATUS if archived_at else ACTIVE_TOPIC_STATUS
    return status


def _normalize_profile(raw: dict[str, Any]) -> dict[str, Any]:
    now = iso_now_utc()
    created_at = str(raw.get("created_at") or now)
    archived_at = str(raw.get("archived_at") or "").strip() or None
    status = _normalize_status(raw.get("status"), archived_at=archived_at)
    source_provider, default_sort_by, default_sort_order = _default_source_settings()
    if status == ACTIVE_TOPIC_STATUS:
        archived_at = None
    try:
        policy = research_policy.normalize_research_policy(raw)
    except research_policy.ResearchPolicyError:
        # A malformed legacy profile must remain readable; invalid new writes
        # are rejected by _patched_profile before this normalization path.
        policy = research_policy.normalize_research_policy()

    title = str(raw.get("title", "")).strip() or str(raw.get("topic", "")).strip() or "Untitled profile"
    topic = str(raw.get("topic", "")).strip() or title
    flat_source_fallback = {
        "queries": [str(query).strip() for query in raw.get("queries", []) if str(query).strip()],
        "categories": [str(category).strip() for category in raw.get("categories", []) if str(category).strip()],
        "max_results": int(raw.get("max_results", 60) or 60),
        "source_provider": str(raw.get("source_provider") or source_provider).strip() or source_provider,
        "arxiv_sort_by": str(raw.get("arxiv_sort_by") or default_sort_by).strip() or default_sort_by,
        "arxiv_sort_order": str(raw.get("arxiv_sort_order") or default_sort_order).strip() or default_sort_order,
    }
    source_settings = source_config.normalize_source_settings(raw.get("source_settings"), fallback=flat_source_fallback)
    flat_source = source_config.flatten_source_settings(source_settings)
    profile_id = str(raw.get("profile_id", "")).strip() or slugify(title) or "topic-profile"
    resource_kind = (
        ResourceKind.SYSTEM_TEMPLATE
        if profile_id in SYSTEM_TEMPLATE_PROFILE_IDS
        else ResourceKind.USER_OWNED
    )
    normalized = {
        "profile_id": profile_id,
        "resource_kind": resource_kind.value,
        "owner_user_id": (
            None
            if resource_kind == ResourceKind.SYSTEM_TEMPLATE
            else str(raw.get("owner_user_id") or "").strip() or None
        ),
        "title": title,
        "description": str(raw.get("description", "")).strip() or None,
        "topic": topic,
        "queries": flat_source["queries"],
        "categories": flat_source["categories"],
        "max_results": flat_source["max_results"],
        "source_provider": flat_source["source_provider"],
        "arxiv_sort_by": flat_source["arxiv_sort_by"],
        "arxiv_sort_order": flat_source["arxiv_sort_order"],
        "source_settings": source_settings,
        "source_statuses": source_config.source_statuses(source_settings),
        "top_papers": int(raw.get("top_papers", 3) or 3),
        "weekly_top": int(raw.get("weekly_top", 15) or 15),
        "is_favorite": bool(raw.get("is_favorite") or raw.get("isFavorite") or False),
        "status": status,
        "archived_at": archived_at,
        "default_collection_cadence": str(raw.get("default_collection_cadence") or DEFAULT_COLLECTION_CADENCE).strip()
        or DEFAULT_COLLECTION_CADENCE,
        "default_report_cadence": str(raw.get("default_report_cadence") or DEFAULT_REPORT_CADENCE).strip()
        or DEFAULT_REPORT_CADENCE,
        **policy,
        "created_at": created_at,
        "updated_at": str(raw.get("updated_at") or created_at),
    }
    if isinstance(raw.get("operations_config"), dict):
        normalized["operations_config"] = deepcopy(raw["operations_config"])
    return normalized


def _materialize(profiles: Any) -> list[dict[str, Any]]:
    """Normalize a raw library, seeding defaults when there is nothing stored.

    Seeded defaults are normalized on the same path as stored ones. Returning
    them raw made derived fields such as `source_statuses` absent until the
    first mutation happened to persist a normalized library, so on a fresh
    install the topic settings screen listed providers with no availability
    state and then silently gained it after any unrelated edit.
    """
    if isinstance(profiles, list) and profiles:
        return [_normalize_profile(profile) for profile in profiles if isinstance(profile, dict)]
    return [_normalize_profile(profile) for profile in _seed_default_profiles()]


def _load_profiles() -> list[dict[str, Any]]:
    """Read the library without writing anything.

    Reading used to persist normalization and seed defaults, so a plain GET
    created the directory and the file. Seeding now happens in memory and is
    persisted by the next mutation, which keeps list/get side-effect free while
    leaving the stored format and the response identical.
    """
    return _materialize(
        shared_state.load_json_state(
            PROFILE_LIBRARY_PATH,
            state_name=STATE_NAME,
            expected_type=list,
            default=lambda: [],
        )
    )


def _mutate_profiles(mutate: Callable[[list[dict[str, Any]]], tuple[list[dict[str, Any]], Any]]) -> Any:
    """Apply a mutation to the newest library under a cross-process lock.

    The callback receives the normalized library as of *inside* the lock, so
    concurrent writers touching different profiles cannot discard one another's
    work and a stale snapshot cannot resurrect a deleted profile.
    """

    def _apply(raw: Any) -> tuple[list[dict[str, Any]], Any]:
        return mutate(_materialize(raw))

    return shared_state.mutate_json_state(
        PROFILE_LIBRARY_PATH,
        state_name=STATE_NAME,
        expected_type=list,
        default=lambda: [],
        mutate=_apply,
    )


def _profile_resource_kind(profile: dict[str, Any]) -> ResourceKind:
    if str(profile.get("profile_id") or "").strip() in SYSTEM_TEMPLATE_PROFILE_IDS:
        return ResourceKind.SYSTEM_TEMPLATE
    return ResourceKind.USER_OWNED


def authorize_topic_profile(
    profile: dict[str, Any],
    *,
    actor: ResourceActor | None,
    action: ResourceAction | str,
) -> ResourceActor:
    return authorize_resource_access(
        actor=actor,
        resource_kind=_profile_resource_kind(profile),
        action=action,
        owner_user_id=profile.get("owner_user_id"),
    )


def list_topic_profiles(*, actor: ResourceActor | None = None) -> dict[str, Any]:
    resolved_actor = resolve_resource_actor(actor)
    profiles = [
        profile
        for profile in _load_profiles()
        if resource_visible_to_actor(
            actor=resolved_actor,
            resource_kind=_profile_resource_kind(profile),
            owner_user_id=profile.get("owner_user_id"),
        )
    ]
    profiles = sorted(profiles, key=lambda profile: str(profile.get("updated_at", "")), reverse=True)
    profiles.sort(key=lambda profile: (profile.get("status") == ARCHIVED_TOPIC_STATUS, not bool(profile.get("is_favorite"))))
    return {"items": profiles}


def create_topic_profile(
    payload: dict[str, Any],
    *,
    actor: ResourceActor | None = None,
) -> dict[str, Any]:
    reject_client_authority_fields(payload)
    resolved_actor = resolve_resource_actor(actor)
    title = str(payload.get("title", "")).strip() or str(payload.get("topic", "")).strip() or "Untitled profile"
    base_id = slugify(title)
    now = iso_now_utc()
    source_provider, default_sort_by, default_sort_order = _default_source_settings()
    source_config.validate_requested_source_settings(payload.get("source_settings"))
    source_settings = source_config.normalize_source_settings(
        payload.get("source_settings"),
        fallback={
            "queries": payload.get("queries", []),
            "categories": payload.get("categories", []),
            "max_results": payload.get("max_results", 60),
            "source_provider": payload.get("source_provider") or source_provider,
            "arxiv_sort_by": payload.get("arxiv_sort_by") or default_sort_by,
            "arxiv_sort_order": payload.get("arxiv_sort_order") or default_sort_order,
        },
    )
    source_config.validate_source_settings(source_settings)
    policy = research_policy.normalize_research_policy(payload)
    flat_source = source_config.flatten_source_settings(source_settings)
    profile = {
        "profile_id": base_id,
        "title": title,
        "description": str(payload.get("description", "")).strip() or None,
        "topic": str(payload.get("topic", "")).strip() or title,
        "queries": flat_source["queries"],
        "categories": flat_source["categories"],
        "max_results": flat_source["max_results"],
        "source_provider": flat_source["source_provider"],
        "arxiv_sort_by": flat_source["arxiv_sort_by"],
        "arxiv_sort_order": flat_source["arxiv_sort_order"],
        "source_settings": source_settings,
        "top_papers": int(payload.get("top_papers", 3) or 3),
        "weekly_top": int(payload.get("weekly_top", 15) or 15),
        "is_favorite": bool(payload.get("is_favorite", False)),
        "status": ACTIVE_TOPIC_STATUS,
        "archived_at": None,
        "default_collection_cadence": str(payload.get("default_collection_cadence") or DEFAULT_COLLECTION_CADENCE).strip()
        or DEFAULT_COLLECTION_CADENCE,
        "default_report_cadence": str(payload.get("default_report_cadence") or DEFAULT_REPORT_CADENCE).strip()
        or DEFAULT_REPORT_CADENCE,
        **policy,
        "created_at": now,
        "updated_at": now,
    }
    def _apply(profiles: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        # The unique-id suffix is resolved against the library inside the lock,
        # so two concurrent creates cannot claim the same id or drop each other.
        existing_ids = {str(item.get("profile_id", "")).strip() for item in profiles}
        profile_id = base_id
        suffix = 2
        while profile_id in existing_ids:
            profile_id = f"{base_id}-{suffix}"
            suffix += 1
        # Owner assignment happens only after the latest library has been
        # loaded under the shared-state lock.
        normalized = _normalize_profile(
            {
                **profile,
                "profile_id": profile_id,
                "resource_kind": ResourceKind.USER_OWNED.value,
                "owner_user_id": owner_user_id_for_actor(resolved_actor),
            }
        )
        profiles.append(normalized)
        return profiles, normalized

    normalized = _mutate_profiles(_apply)
    metadata_index.touch_index_state("last_topic_profiles_updated_at")
    return normalized


def update_topic_profile(
    profile_id: str,
    payload: dict[str, Any],
    *,
    touch_metadata: bool = True,
    actor: ResourceActor | None = None,
) -> dict[str, Any]:
    reject_client_authority_fields(payload)
    resolved_actor = resolve_resource_actor(actor)

    def _apply(profiles: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        # Existence is re-checked against the library inside the lock: an update
        # racing a delete fails with 404 instead of resurrecting the profile,
        # and unrelated profiles in the same file are preserved untouched.
        for index, profile in enumerate(profiles):
            if profile.get("profile_id") != profile_id:
                continue
            authorize_topic_profile(profile, actor=resolved_actor, action=ResourceAction.UPDATE)
            profiles[index] = _patched_profile(deepcopy(profile), payload)
            return profiles, profiles[index]
        raise FileNotFoundError(f"Topic profile not found: {profile_id}")

    updated_profile = _mutate_profiles(_apply)
    if touch_metadata:
        metadata_index.touch_index_state("last_topic_profiles_updated_at")
    return updated_profile


def _patched_profile(updated: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Pure field-level patch of one profile; never a whole-document overwrite."""
    if True:
        if any(field in payload for field in (
            "acquisition_language", "acquisitionLanguage", "review_report_language", "reviewReportLanguage",
            "acquisition_start_date", "acquisitionStartDate", "acquisition_end_date", "acquisitionEndDate",
        )):
            updated.update(research_policy.normalize_research_policy(payload, current=updated))
        for field in ("title", "description", "topic"):
            if field in payload and payload[field] is not None:
                updated[field] = str(payload[field]).strip() or updated.get(field)
        for field in ("queries", "categories"):
            if field in payload and payload[field] is not None:
                updated[field] = [str(item).strip() for item in payload[field] if str(item).strip()]
        for field, default in (("max_results", 60), ("top_papers", 3), ("weekly_top", 15)):
            if field in payload and payload[field] is not None:
                updated[field] = int(payload[field] or default)
        for field, default in (
            ("source_provider", DEFAULT_SOURCE_PROVIDER),
            ("arxiv_sort_by", DEFAULT_ARXIV_SORT_BY),
            ("arxiv_sort_order", DEFAULT_ARXIV_SORT_ORDER),
        ):
            if field in payload and payload[field] is not None:
                updated[field] = str(payload[field]).strip() or default
        if "source_settings" in payload and payload["source_settings"] is not None:
            updated["source_settings"] = payload["source_settings"]
        if "operations_config" in payload and isinstance(payload["operations_config"], dict):
            updated["operations_config"] = deepcopy(payload["operations_config"])
        if any(
            field in payload and payload[field] is not None
            for field in ("queries", "categories", "max_results", "source_provider", "arxiv_sort_by", "arxiv_sort_order", "source_settings")
        ):
            source_config.validate_requested_source_settings(payload.get("source_settings"))
            raw_source_settings = deepcopy(updated.get("source_settings") if isinstance(updated.get("source_settings"), dict) else {})
            raw_providers = raw_source_settings.setdefault("providers", {})
            raw_arxiv = raw_providers.setdefault("arxiv", {})
            if "queries" in payload and payload["queries"] is not None:
                raw_arxiv["queries"] = updated.get("queries", [])
            if "categories" in payload and payload["categories"] is not None:
                raw_arxiv["categories"] = updated.get("categories", [])
            if "max_results" in payload and payload["max_results"] is not None:
                raw_arxiv["maxResults"] = updated.get("max_results", 60)
            if "arxiv_sort_by" in payload and payload["arxiv_sort_by"] is not None:
                raw_arxiv["sortBy"] = updated.get("arxiv_sort_by")
            if "arxiv_sort_order" in payload and payload["arxiv_sort_order"] is not None:
                raw_arxiv["sortOrder"] = updated.get("arxiv_sort_order")
            if "source_provider" in payload and payload["source_provider"] is not None:
                raw_source_settings["defaultProvider"] = updated.get("source_provider") or DEFAULT_SOURCE_PROVIDER
                raw_source_settings["enabledProviders"] = [updated.get("source_provider") or DEFAULT_SOURCE_PROVIDER]
            source_settings = source_config.normalize_source_settings(
                raw_source_settings,
                fallback={
                    "queries": updated.get("queries", []),
                    "categories": updated.get("categories", []),
                    "max_results": updated.get("max_results", 60),
                    "source_provider": updated.get("source_provider") or DEFAULT_SOURCE_PROVIDER,
                    "arxiv_sort_by": updated.get("arxiv_sort_by") or DEFAULT_ARXIV_SORT_BY,
                    "arxiv_sort_order": updated.get("arxiv_sort_order") or DEFAULT_ARXIV_SORT_ORDER,
                },
            )
            source_config.validate_source_settings(source_settings)
            flat_source = source_config.flatten_source_settings(source_settings)
            updated.update(flat_source)
            updated["source_settings"] = source_settings
        if "is_favorite" in payload and payload["is_favorite"] is not None:
            updated["is_favorite"] = bool(payload["is_favorite"])
        for field, default in (
            ("default_collection_cadence", DEFAULT_COLLECTION_CADENCE),
            ("default_report_cadence", DEFAULT_REPORT_CADENCE),
        ):
            if field in payload and payload[field] is not None:
                updated[field] = str(payload[field]).strip() or default
        if "status" in payload and payload["status"] is not None:
            next_status = _normalize_status(payload["status"], archived_at=updated.get("archived_at"))
            if next_status == ARCHIVED_TOPIC_STATUS and updated.get("status") != ARCHIVED_TOPIC_STATUS:
                updated["archived_at"] = iso_now_utc()
            if next_status == ACTIVE_TOPIC_STATUS:
                updated["archived_at"] = None
            updated["status"] = next_status
        updated["updated_at"] = iso_now_utc()
        return _normalize_profile(updated)


def delete_topic_profile(
    profile_id: str,
    *,
    actor: ResourceActor | None = None,
) -> dict[str, Any]:
    resolved_actor = resolve_resource_actor(actor)

    def _apply(profiles: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        target = next((profile for profile in profiles if profile.get("profile_id") == profile_id), None)
        if target is None:
            raise FileNotFoundError(f"Topic profile not found: {profile_id}")
        authorize_topic_profile(target, actor=resolved_actor, action=ResourceAction.DELETE)
        next_profiles = [profile for profile in profiles if profile.get("profile_id") != profile_id]
        return next_profiles, {"profile_id": profile_id, "status": "deleted"}

    result = _mutate_profiles(_apply)
    metadata_index.touch_index_state("last_topic_profiles_updated_at")
    return result


def get_topic_profile(
    profile_id: str,
    *,
    actor: ResourceActor | None = None,
    action: ResourceAction = ResourceAction.READ,
) -> dict[str, Any]:
    resolved_actor = resolve_resource_actor(actor)
    for profile in _load_profiles():
        if profile.get("profile_id") == profile_id:
            authorize_topic_profile(profile, actor=resolved_actor, action=action)
            return profile
    raise FileNotFoundError(f"Topic profile not found: {profile_id}")


def to_run_request_payload(profile: dict[str, Any]) -> dict[str, Any]:
    policy = research_policy.policy_from_profile(profile)
    return {
        "topic": profile.get("topic", ""),
        "queries": list(profile.get("queries", [])),
        "categories": list(profile.get("categories", [])),
        "max_results": int(profile.get("max_results", 60) or 60),
        "source_provider": str(profile.get("source_provider") or DEFAULT_SOURCE_PROVIDER).strip() or DEFAULT_SOURCE_PROVIDER,
        "arxiv_sort_by": str(profile.get("arxiv_sort_by") or DEFAULT_ARXIV_SORT_BY).strip() or DEFAULT_ARXIV_SORT_BY,
        "arxiv_sort_order": str(profile.get("arxiv_sort_order") or DEFAULT_ARXIV_SORT_ORDER).strip() or DEFAULT_ARXIV_SORT_ORDER,
        "source_settings": source_config.normalize_source_settings(
            profile.get("source_settings"),
            fallback={
                "queries": profile.get("queries", []),
                "categories": profile.get("categories", []),
                "max_results": profile.get("max_results", 60),
                "source_provider": profile.get("source_provider") or DEFAULT_SOURCE_PROVIDER,
                "arxiv_sort_by": profile.get("arxiv_sort_by") or DEFAULT_ARXIV_SORT_BY,
                "arxiv_sort_order": profile.get("arxiv_sort_order") or DEFAULT_ARXIV_SORT_ORDER,
            },
        ),
        "top_papers": int(profile.get("top_papers", 3) or 3),
        "weekly_top": int(profile.get("weekly_top", 15) or 15),
        **policy,
    }
