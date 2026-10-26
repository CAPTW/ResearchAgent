from __future__ import annotations

from typing import Any

from app.auth.resource_access import ResourceAction, ResourceActor
from app.repositories import temporal_analysis as repository, temporal_memory
from app.services import topic_profiles
from app.services.temporal_destinations import CurrentDestinations


def authorize(topic: str, actor: ResourceActor, *, action: ResourceAction) -> None:
    topic_profiles.get_topic_profile(topic, actor=actor, action=action)


def insights(
    topic: str, limit: int, page: int, actor: ResourceActor | None = None, snapshot_id: str | None = None
) -> dict[str, Any]:
    result = repository.insights(topic, limit, page, snapshot_id)
    # Historical measurements never depend on live state. Only optional navigation
    # is resolved against the current paper/evidence authority, avoiding dead links
    # or links to evidence whose content has since changed under the same ID.
    if not result["items"]:
        return result
    destinations = CurrentDestinations(topic, actor)
    snapshots = {}
    for item in result["items"]:
        identity = item["source_snapshot_id"]
        if identity not in snapshots:
            snapshots[identity] = temporal_memory.detail(topic, identity)["snapshot"]
        snapshot = snapshots[identity]
        item.update(destinations.resolve(
            (member for member in snapshot.members if member.paper_version_id in item["source_entity_ids"]),
            item["source_evidence_ids"],
        ))
    return result
