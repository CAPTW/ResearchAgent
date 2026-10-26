"""Read preserved provider inputs through the visible Paper Bank occurrence set."""
from typing import Any

from app.auth.resource_access import ResourceAction, ResourceActor
from app.repositories import ai_review_drafts
from app.services import paper_bank, topic_profiles


def visible_paper(topic: str, version: str, actor: ResourceActor) -> tuple[dict[str, Any], dict[str, Any]]:
    profile = topic_profiles.get_topic_profile(topic, actor=actor)
    # Like frozen Journal sources, these bodies belong to the topic workspace.
    # Public READ on a system template does not expose private review inputs.
    topic_profiles.authorize_topic_profile(profile, actor=actor, action=ResourceAction.UPDATE)
    catalog, _, _, _ = paper_bank._load_catalog(topic, actor=actor)
    item = next((item for item in catalog if item["paperVersionId"] == version), None)
    if item is None:
        raise paper_bank.PaperVersionNotFound(version)
    return profile, item


def visible_occurrences(topic: str, version: str, actor: ResourceActor) -> list[tuple[str, str]]:
    _, item = visible_paper(topic, version, actor)
    # Reuse the catalog's ambiguity check for historical review aliases. These
    # internal keys never become public paper identities or global paper lookup.
    return list(item["_reviewOccurrenceKeys"])


def _summary(receipt: dict[str, Any]) -> dict[str, Any]:
    packet = receipt["packet"]
    return {
        **{key: receipt[key] for key in ("receipt_id", "recorded_at", "input_origin", "run_id", "paper_id", "source_text_sha256")},
        "review_level": receipt["review_level"],
        "validation_status": receipt["validation"].get("status", "unknown"),
        "input_scope": packet["input_scope"],
    }


def listing(topic: str, version: str, actor: ResourceActor, *, page: int = 1, page_size: int = 10) -> dict[str, Any]:
    occurrences = visible_occurrences(topic, version, actor)
    receipts = ai_review_drafts.list_input_history(
        topic_id=topic, occurrences=occurrences, limit=page_size + 1, offset=(page - 1) * page_size,
    )
    return dict(topicId=topic, paperVersionId=version, page=page, pageSize=page_size,
                hasMore=len(receipts) > page_size, items=[_summary(record) for record in receipts[:page_size]])


def detail(topic: str, version: str, receipt_id: str, actor: ResourceActor) -> dict[str, Any]:
    occurrences = visible_occurrences(topic, version, actor)
    receipt = ai_review_drafts.get_input_receipt(topic, receipt_id, occurrences=occurrences)
    if receipt is None or (receipt["run_id"], receipt["paper_id"]) not in occurrences:
        raise KeyError("review_input_not_found")
    return dict(**_summary(receipt), packet=receipt["packet"], draft=receipt["draft"],
                **({"validation": receipt["validation"]} if "prepared_execution" in receipt["validation"] else {}))
