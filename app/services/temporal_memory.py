from __future__ import annotations

from dataclasses import asdict
from typing import Any

from app.auth.resource_access import ResourceAction, ResourceActor
from app.domain.temporal_memory import ALGORITHM_VERSION, EVENT_TYPES, diff, snapshot_payload
from app.repositories import temporal_memory as repository, temporal_sources
from app.repositories.files import iso_now_utc
from app.services import topic_profiles
from app.services.temporal_destinations import CurrentDestinations


def capture(topic_id: str, actor: ResourceActor) -> dict[str, Any]:
    topic_profiles.get_topic_profile(topic_id, actor=actor, action=ResourceAction.UPDATE)
    value, created = repository.capture(topic_id, lambda connection: temporal_sources.project(connection, topic_id, actor))
    return {"snapshot": snapshot_payload(value), "created": created}


def listing(topic_id: str, actor: ResourceActor, page: int, page_size: int) -> dict[str, Any]:
    topic_profiles.get_topic_profile(topic_id, actor=actor)
    result = repository.list_snapshots(topic_id, page, page_size)
    items = []
    for item in result["items"]:
        payload = snapshot_payload(item)
        events = repository.detail(topic_id, item.snapshot_id)["events"]
        payload["adjacent_event_counts"] = {kind: sum(e["event_type"] == kind for e in events) for kind in EVENT_TYPES}
        items.append(payload)
    result["items"] = items
    return result


def detail(topic_id: str, snapshot_id: str, actor: ResourceActor) -> dict[str, Any]:
    topic_profiles.get_topic_profile(topic_id, actor=actor)
    result = repository.detail(topic_id, snapshot_id)
    result["snapshot"] = snapshot_payload(result["snapshot"], include_members=True)
    result["adjacent_event_counts"] = {kind: sum(e["event_type"] == kind for e in result["events"]) for kind in EVENT_TYPES}
    return result


def comparison(topic_id: str, from_id: str, to_id: str, actor: ResourceActor) -> dict[str, Any]:
    topic_profiles.get_topic_profile(topic_id, actor=actor)
    before, after = repository.pair(topic_id, from_id, to_id)
    events = diff(before, after)
    calculated_at = iso_now_utc()
    before_members = {(m.canonical_paper_id, m.paper_version_id): m.membership_digest for m in before.members}
    after_members = {(m.canonical_paper_id, m.paper_version_id): m.membership_digest for m in after.members}
    changed = {key[0] for key in before_members.keys() | after_members.keys() if before_members.get(key) != after_members.get(key)}
    event_payloads = []
    destinations = CurrentDestinations(topic_id, actor) if events else None
    target_members = {member.paper_version_id: member for member in after.members}
    for event in events:
        payload = asdict(event)
        # Identity/state/order remain the pure deterministic projection. A fresh
        # read-only calculation is dated now, including reverse/nonadjacent pairs;
        # never imply that an arbitrary comparison happened at the target capture.
        payload["calculated_at"] = calculated_at
        member = target_members.get(event.paper_version_id)
        assert destinations is not None
        navigation = destinations.resolve([member] if member else [], event.source_evidence_ids)
        paper_links = navigation.pop("paper_destinations")
        payload["paper_destination"] = paper_links[0]["destination"] if paper_links else None
        payload.update(navigation)
        event_payloads.append(payload)
    return {
        "from_snapshot": snapshot_payload(before), "to_snapshot": snapshot_payload(after),
        "events": event_payloads,
        "counts": {kind: sum(e.event_type == kind for e in events) for kind in EVENT_TYPES},
        "changed_paper_count": len(changed),
        "membership_only_changed_paper_count": len(changed - {event.canonical_paper_id for event in events}),
        "unchanged_paper_count": len({m.canonical_paper_id for m in (*before.members, *after.members)} - changed),
        "algorithm_version": ALGORITHM_VERSION, "calculated_at": calculated_at,
        "window": [before.created_at, after.created_at], "acceptance_state": after.acceptance_state,
    }
