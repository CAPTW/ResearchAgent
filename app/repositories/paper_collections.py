from __future__ import annotations

import hashlib
from typing import Any, Callable, TypeVar

from app.domain.constants import STORAGE_ROOT
from app.repositories.files import iso_now_utc, slugify
from app.repositories.shared_state import load_json_state, mutate_json_state

SCHEMA_VERSION = 1
STATE_NAME = "paper_collections"
SAVED_PAPERS_ID = "saved-papers"
SAVED_PAPERS_NAME = "Saved papers"
COLLECTION_LIBRARY_PATH = STORAGE_ROOT.parent / "paper_collections" / "collections.json"

T = TypeVar("T")


class PaperCollectionError(RuntimeError):
    code = "paper_collection_failed"

    def __init__(self, detail: str = "") -> None:
        self.detail = detail
        super().__init__(self.code if not detail else f"{self.code}: {detail}")


class CollectionNotFound(PaperCollectionError):
    code = "paper_collection_not_found"


class CollectionConflict(PaperCollectionError):
    code = "paper_collection_conflict"


class CollectionValidationError(PaperCollectionError):
    code = "paper_collection_validation_failed"


def _default_document() -> dict[str, Any]:
    return {"schemaVersion": SCHEMA_VERSION, "topics": {}}


def _validate_name(name: str) -> str:
    normalized = " ".join(str(name or "").split())
    if not normalized or len(normalized) > 80:
        raise CollectionValidationError("collection_name_invalid")
    return normalized


def _validate_member(member: Any) -> dict[str, Any]:
    if not isinstance(member, dict):
        raise CollectionValidationError("collection_member_invalid")
    canonical_paper_id = str(member.get("canonicalPaperId") or "").strip()
    paper_version_id = str(member.get("paperVersionId") or "").strip()
    if not canonical_paper_id or not paper_version_id:
        raise CollectionValidationError("collection_member_identity_missing")
    return {
        "canonicalPaperId": canonical_paper_id,
        "paperVersionId": paper_version_id,
        "addedAt": str(member.get("addedAt") or "").strip() or None,
    }


def _validate_collection(collection: Any) -> dict[str, Any]:
    if not isinstance(collection, dict):
        raise CollectionValidationError("collection_record_invalid")
    collection_id = str(collection.get("id") or "").strip()
    if not collection_id:
        raise CollectionValidationError("collection_id_missing")
    members = collection.get("members", [])
    if not isinstance(members, list):
        raise CollectionValidationError("collection_members_invalid")
    validated_members: list[dict[str, Any]] = []
    seen_versions: set[str] = set()
    for raw_member in members:
        member = _validate_member(raw_member)
        if member["paperVersionId"] in seen_versions:
            raise CollectionValidationError("collection_member_duplicate")
        seen_versions.add(member["paperVersionId"])
        validated_members.append(member)
    return {
        "id": collection_id,
        "name": _validate_name(str(collection.get("name") or "")),
        "isDefault": bool(collection.get("isDefault", False)),
        "createdAt": str(collection.get("createdAt") or "").strip() or None,
        "updatedAt": str(collection.get("updatedAt") or "").strip() or None,
        "members": validated_members,
    }


def _validate_document(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise CollectionValidationError("collection_document_invalid")
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        raise CollectionValidationError("collection_schema_version_unsupported")
    topics = payload.get("topics")
    if not isinstance(topics, dict):
        raise CollectionValidationError("collection_topics_invalid")
    validated_topics: dict[str, list[dict[str, Any]]] = {}
    for raw_topic_id, raw_collections in topics.items():
        topic_id = str(raw_topic_id or "").strip()
        if not topic_id or not isinstance(raw_collections, list):
            raise CollectionValidationError("collection_topic_record_invalid")
        collections = [_validate_collection(item) for item in raw_collections]
        ids = [item["id"] for item in collections]
        if len(ids) != len(set(ids)):
            raise CollectionValidationError("collection_id_duplicate")
        validated_topics[topic_id] = collections
    return {"schemaVersion": SCHEMA_VERSION, "topics": validated_topics}


def _load_document() -> dict[str, Any]:
    payload = load_json_state(
        COLLECTION_LIBRARY_PATH,
        state_name=STATE_NAME,
        expected_type=dict,
        default=_default_document,
    )
    return _validate_document(payload)


def _mutate(mutation: Callable[[dict[str, Any]], tuple[dict[str, Any], T]]) -> T:
    def apply(payload: dict[str, Any]) -> tuple[dict[str, Any], T]:
        validated = _validate_document(payload)
        return mutation(validated)

    return mutate_json_state(
        COLLECTION_LIBRARY_PATH,
        state_name=STATE_NAME,
        expected_type=dict,
        default=_default_document,
        mutate=apply,
    )


def _default_collection() -> dict[str, Any]:
    return {
        "id": SAVED_PAPERS_ID,
        "name": SAVED_PAPERS_NAME,
        "isDefault": True,
        "createdAt": None,
        "updatedAt": None,
        "members": [],
    }


def _topic_collections(document: dict[str, Any], topic_id: str, *, ensure_default: bool) -> list[dict[str, Any]]:
    normalized_topic_id = str(topic_id or "").strip()
    if not normalized_topic_id:
        raise CollectionValidationError("topic_id_missing")
    collections = document["topics"].setdefault(normalized_topic_id, []) if ensure_default else list(
        document["topics"].get(normalized_topic_id, [])
    )
    if not any(item["id"] == SAVED_PAPERS_ID for item in collections):
        if ensure_default:
            collections.insert(0, _default_collection())
        else:
            collections = [_default_collection(), *collections]
    return collections


def _public_collection(collection: dict[str, Any]) -> dict[str, Any]:
    members = [dict(member) for member in collection["members"]]
    return {
        "id": collection["id"],
        "name": collection["name"],
        "isDefault": bool(collection["isDefault"]),
        "createdAt": collection["createdAt"],
        "updatedAt": collection["updatedAt"],
        "memberCount": len(members),
        "members": members,
    }


def list_collections(topic_id: str) -> dict[str, Any]:
    collections = _topic_collections(_load_document(), topic_id, ensure_default=False)
    collections.sort(key=lambda item: (not item["isDefault"], item["name"].casefold(), item["id"]))
    return {
        "topicId": topic_id,
        "items": [_public_collection(item) for item in collections],
        "resource": {"state": "available", "reason": None},
    }


def get_collection(topic_id: str, collection_id: str) -> dict[str, Any]:
    collection = next(
        (
            item
            for item in _topic_collections(_load_document(), topic_id, ensure_default=False)
            if item["id"] == collection_id
        ),
        None,
    )
    if collection is None:
        raise CollectionNotFound(collection_id)
    return _public_collection(collection)


def _collection_id(topic_id: str, name: str) -> str:
    digest = hashlib.sha256(f"{topic_id}\0{name.casefold()}".encode()).hexdigest()[:8]
    return f"collection-{slugify(name)[:32]}-{digest}"


def create_collection(topic_id: str, name: str) -> dict[str, Any]:
    normalized_name = _validate_name(name)

    def apply(document: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        collections = _topic_collections(document, topic_id, ensure_default=True)
        if any(item["name"].casefold() == normalized_name.casefold() for item in collections):
            raise CollectionConflict("collection_name_exists")
        now = iso_now_utc()
        collection = {
            "id": _collection_id(topic_id, normalized_name),
            "name": normalized_name,
            "isDefault": False,
            "createdAt": now,
            "updatedAt": now,
            "members": [],
        }
        collections.append(collection)
        return document, _public_collection(collection)

    return _mutate(apply)


def rename_collection(topic_id: str, collection_id: str, name: str) -> dict[str, Any]:
    normalized_name = _validate_name(name)

    def apply(document: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        collections = _topic_collections(document, topic_id, ensure_default=True)
        collection = next((item for item in collections if item["id"] == collection_id), None)
        if collection is None:
            raise CollectionNotFound(collection_id)
        if collection["isDefault"]:
            raise CollectionConflict("default_collection_rename_forbidden")
        if any(
            item["id"] != collection_id and item["name"].casefold() == normalized_name.casefold()
            for item in collections
        ):
            raise CollectionConflict("collection_name_exists")
        collection["name"] = normalized_name
        collection["updatedAt"] = iso_now_utc()
        return document, _public_collection(collection)

    return _mutate(apply)


def delete_collection(topic_id: str, collection_id: str) -> dict[str, Any]:
    def apply(document: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        collections = _topic_collections(document, topic_id, ensure_default=True)
        collection = next((item for item in collections if item["id"] == collection_id), None)
        if collection is None:
            raise CollectionNotFound(collection_id)
        if collection["isDefault"]:
            raise CollectionConflict("default_collection_delete_forbidden")
        collections.remove(collection)
        return document, {"id": collection_id, "deleted": True}

    return _mutate(apply)


def add_member(
    topic_id: str,
    collection_id: str,
    *,
    canonical_paper_id: str,
    paper_version_id: str,
) -> dict[str, Any]:
    member = _validate_member(
        {
            "canonicalPaperId": canonical_paper_id,
            "paperVersionId": paper_version_id,
            "addedAt": iso_now_utc(),
        }
    )

    def apply(document: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        collections = _topic_collections(document, topic_id, ensure_default=True)
        collection = next((item for item in collections if item["id"] == collection_id), None)
        if collection is None:
            raise CollectionNotFound(collection_id)
        if not any(item["paperVersionId"] == paper_version_id for item in collection["members"]):
            collection["members"].append(member)
            collection["members"].sort(key=lambda item: item["paperVersionId"])
            collection["updatedAt"] = member["addedAt"]
        return document, _public_collection(collection)

    return _mutate(apply)


def add_members_atomic(
    topic_id: str,
    collection_id: str,
    members: list[dict[str, Any]],
    *,
    requested_count: int | None = None,
) -> dict[str, Any]:
    """Add a resolved set of PaperVersion members in one locked mutation.

    Callers resolve every PaperVersion before entering this repository boundary.
    This keeps malformed or cross-topic input from producing a partial write and
    lets the shared-state lock provide the same fencing as the single-member
    route.  Repeated existing members are a deterministic idempotent no-op.
    """
    if not isinstance(members, list) or not members:
        raise CollectionValidationError("bulk_members_empty")
    normalized_members = [_validate_member(member) for member in members]
    seen_versions: set[str] = set()
    deduplicated: list[dict[str, Any]] = []
    for member in normalized_members:
        paper_version_id = member["paperVersionId"]
        if paper_version_id in seen_versions:
            continue
        seen_versions.add(paper_version_id)
        deduplicated.append(member)

    def apply(document: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        collections = _topic_collections(document, topic_id, ensure_default=True)
        collection = next((item for item in collections if item["id"] == collection_id), None)
        if collection is None:
            raise CollectionNotFound(collection_id)
        existing = {item["paperVersionId"] for item in collection["members"]}
        now = iso_now_utc()
        added = 0
        for member in deduplicated:
            if member["paperVersionId"] in existing:
                continue
            member = {**member, "addedAt": member.get("addedAt") or now}
            collection["members"].append(member)
            existing.add(member["paperVersionId"])
            added += 1
        if added:
            collection["members"].sort(key=lambda item: item["paperVersionId"])
            collection["updatedAt"] = now
        public = _public_collection(collection)
        return document, {
            "collection": public,
            "requestedCount": int(requested_count if requested_count is not None else len(members)),
            "uniqueCount": len(deduplicated),
            "addedCount": added,
            "alreadyPresentCount": len(deduplicated) - added,
            "finalMemberCount": public["memberCount"],
            "destinationCollectionId": collection_id,
        }

    return _mutate(apply)


def remove_member(topic_id: str, collection_id: str, paper_version_id: str) -> dict[str, Any]:
    def apply(document: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        collections = _topic_collections(document, topic_id, ensure_default=True)
        collection = next((item for item in collections if item["id"] == collection_id), None)
        if collection is None:
            raise CollectionNotFound(collection_id)
        before = len(collection["members"])
        collection["members"] = [
            item for item in collection["members"] if item["paperVersionId"] != paper_version_id
        ]
        if len(collection["members"]) != before:
            collection["updatedAt"] = iso_now_utc()
        return document, _public_collection(collection)

    return _mutate(apply)


def collection_membership(topic_id: str) -> dict[str, set[str]]:
    return {
        collection["id"]: {member["paperVersionId"] for member in collection["members"]}
        for collection in list_collections(topic_id)["items"]
    }
