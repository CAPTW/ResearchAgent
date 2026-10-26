"""Immutable local-state projections. No clock, IO or model participates in identity."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any, Iterable
import unicodedata

SCHEMA_VERSION = 1
ALGORITHM_VERSION = "xrml-local-snapshot-diff-v1"
ACCEPTANCE = "ACCEPTED_LOCAL_DETERMINISTIC"
EVENT_TYPES = (
    "PAPER_ADDED", "PAPER_REMOVED", "PAPER_VERSION_CHANGED", "COLLECTION_MEMBERSHIP_CHANGED",
    "REVIEW_STATE_CHANGED", "FULL_TEXT_STATE_CHANGED", "EVIDENCE_CHANGED", "RIGHTS_STATE_CHANGED",
)


def _normalize(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("canonical_key_invalid")
            normalized = _normalize(key)
            if normalized in result:
                raise ValueError("canonical_key_collision")
            result[normalized] = _normalize(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise ValueError("canonical_type_invalid")


def canonical_json(value: Any) -> str:
    return json.dumps(_normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8", errors="strict")).hexdigest()


@dataclass(frozen=True)
class Member:
    canonical_paper_id: str
    paper_version_id: str
    occurrence_ids: tuple[str, ...] = ()
    collection_ids: tuple[str, ...] = ()
    review_state: str = "unavailable"
    full_text_state: str = "unavailable"
    full_text_content_digest: str | None = None
    evidence_ids: tuple[str, ...] = ()
    evidence_digests: tuple[tuple[str, str], ...] = ()
    evidence_state: str = "unavailable"
    rights_access_state: str = "unknown"
    rights_declaration_digest: str | None = None
    source_entity_ids: tuple[str, ...] = ()
    membership_digest: str = ""


def normalized_member(member: Member) -> Member:
    raw = _normalize(asdict(member))
    raw.pop("membership_digest")
    for key in ("occurrence_ids", "collection_ids", "evidence_ids", "source_entity_ids"):
        raw[key] = tuple(sorted(set(raw[key])))
    raw["evidence_digests"] = tuple(sorted(set(tuple(item) for item in raw["evidence_digests"])))
    if not raw["canonical_paper_id"] or not raw["paper_version_id"]:
        raise ValueError("membership_identity_missing")
    return Member(**raw, membership_digest=digest(raw))


@dataclass(frozen=True)
class TopicSnapshot:
    snapshot_id: str
    topic_id: str
    created_at: str
    schema_version: int
    source_revision: str
    corpus_digest: str
    paper_count: int
    review_state_digest: str
    evidence_digest: str
    acceptance_state: str
    parent_snapshot_id: str | None
    members: tuple[Member, ...]
    algorithm_version: str = ALGORITHM_VERSION
    confidence: float = 1.0
    confidence_class: str = "DETERMINISTIC_EXACT"

    @property
    def source_snapshot_id(self) -> str:
        return self.snapshot_id


def snapshot(topic_id: str, members: Iterable[Member], created_at: str, parent: str | None = None) -> TopicSnapshot:
    ordered = tuple(sorted((normalized_member(m) for m in members), key=lambda m: (
        m.canonical_paper_id, m.paper_version_id, m.membership_digest,
    )))
    identities = [(m.canonical_paper_id, m.paper_version_id) for m in ordered]
    if len(set(identities)) != len(identities):
        raise ValueError("membership_identity_duplicate")
    projection = [asdict(m) for m in ordered]
    corpus = digest(projection)
    topic = unicodedata.normalize("NFC", topic_id)
    identity = digest({"topic_id": topic, "schema_version": SCHEMA_VERSION, "members": projection})
    # No unified source revision exists. Member digests are revisions of the exact
    # authoritative ID/state projection; presentation labels and observation times
    # deliberately do not change this revision or corpus identity.
    revision = digest([(m.canonical_paper_id, m.paper_version_id, m.membership_digest) for m in ordered])
    return TopicSnapshot(
        "tsnap_" + identity, topic, created_at, SCHEMA_VERSION, revision, corpus, len(ordered),
        digest([(m.paper_version_id, m.review_state) for m in ordered]),
        digest([(m.paper_version_id, m.evidence_state, m.evidence_ids, m.evidence_digests) for m in ordered]),
        ACCEPTANCE, parent, ordered,
    )


@dataclass(frozen=True)
class TopicChangeEvent:
    event_id: str
    topic_id: str
    from_snapshot_id: str
    to_snapshot_id: str
    event_type: str
    entity_kind: str
    entity_id: str
    canonical_paper_id: str
    paper_version_id: str
    before_digest: str
    after_digest: str
    before_state: str  # canonical typed-state JSON; never text/body or an arbitrary workspace blob
    after_state: str
    calculated_at: str
    source_snapshot_id: str
    source_entity_ids: tuple[str, ...]
    source_evidence_ids: tuple[str, ...]
    window: tuple[str, str]
    algorithm_version: str = ALGORITHM_VERSION
    confidence: float = 1.0
    confidence_class: str = "DETERMINISTIC_EXACT"
    acceptance_state: str = ACCEPTANCE


def diff(before: TopicSnapshot, after: TopicSnapshot) -> tuple[TopicChangeEvent, ...]:
    if before.topic_id != after.topic_id:
        raise ValueError("cross_topic_pair")
    if before.schema_version != after.schema_version or before.schema_version != SCHEMA_VERSION:
        raise ValueError("snapshot_schema_conflict")
    events: list[TopicChangeEvent] = []

    def emit(kind: str, old: Member | None, new: Member | None, left: Any, right: Any) -> None:
        member = new or old
        assert member is not None
        entity = member.paper_version_id
        bd, ad = digest(left), digest(right)
        preimage = {"topic_id": before.topic_id, "from_snapshot_id": before.snapshot_id,
                    "to_snapshot_id": after.snapshot_id, "event_type": kind, "entity_kind": "paper_version",
                    "entity_id": entity, "before_digest": bd, "after_digest": ad, "algorithm_version": ALGORITHM_VERSION}
        involved = tuple(m for m in (old, new) if m is not None)
        events.append(TopicChangeEvent(
            "tevent_" + digest(preimage), before.topic_id, before.snapshot_id, after.snapshot_id,
            kind, "paper_version", entity, member.canonical_paper_id, entity, bd, ad,
            canonical_json(left), canonical_json(right), after.created_at, after.snapshot_id,
            tuple(sorted({identifier for m in involved for identifier in
                          (m.canonical_paper_id, m.paper_version_id, *m.occurrence_ids, *m.source_entity_ids)})),
            tuple(sorted({identifier for m in involved for identifier in m.evidence_ids})),
            (before.created_at, after.created_at),
        ))

    def compare(old: Member, new: Member) -> None:
        if old.paper_version_id != new.paper_version_id:
            emit("PAPER_VERSION_CHANGED", old, new, old.paper_version_id, new.paper_version_id)
        fields = (
            ("COLLECTION_MEMBERSHIP_CHANGED", ("collection_ids",)),
            ("REVIEW_STATE_CHANGED", ("review_state",)),
            ("FULL_TEXT_STATE_CHANGED", ("full_text_state", "full_text_content_digest")),
            ("EVIDENCE_CHANGED", ("evidence_state", "evidence_ids", "evidence_digests")),
            ("RIGHTS_STATE_CHANGED", ("rights_access_state", "rights_declaration_digest")),
        )
        for kind, names in fields:
            left = {name: getattr(old, name) for name in names}
            right = {name: getattr(new, name) for name in names}
            if left != right:
                emit(kind, old, new, left, right)

    works = sorted({m.canonical_paper_id for m in (*before.members, *after.members)})
    for work in works:
        old = {m.paper_version_id: m for m in before.members if m.canonical_paper_id == work}
        new = {m.paper_version_id: m for m in after.members if m.canonical_paper_id == work}
        for version in sorted(old.keys() & new.keys()):
            compare(old[version], new[version])
        removed = sorted(old.keys() - new.keys())
        added = sorted(new.keys() - old.keys())
        # One unambiguous replacement; multiple historical versions remain exact
        # memberships. Never guess a mapping across several unmatched versions.
        if len(removed) == len(added) == 1:
            compare(old[removed[0]], new[added[0]])
        else:
            for version in removed:
                emit("PAPER_REMOVED", old[version], None, asdict(old[version]), None)
            for version in added:
                emit("PAPER_ADDED", None, new[version], None, asdict(new[version]))
    return tuple(sorted(events, key=lambda e: (EVENT_TYPES.index(e.event_type), e.canonical_paper_id, e.entity_id, e.event_id)))


def snapshot_payload(value: TopicSnapshot, *, include_members: bool = False) -> dict[str, Any]:
    result = asdict(value)
    if not include_members:
        result.pop("members")
    else:
        result["members"] = [dict(asdict(m), snapshot_id=value.snapshot_id) for m in value.members]
    result.update(source_snapshot_id=value.snapshot_id, from_snapshot_id=value.parent_snapshot_id,
                  to_snapshot_id=value.snapshot_id, calculated_at=value.created_at,
                  source_entity_ids=sorted({i for m in value.members for i in
                                           (m.canonical_paper_id, m.paper_version_id, *m.occurrence_ids, *m.source_entity_ids)}),
                  source_evidence_ids=sorted({i for m in value.members for i in m.evidence_ids}), window=None)
    return result
