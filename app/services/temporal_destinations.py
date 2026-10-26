"""Optional live navigation for immutable research history."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

from app.auth.resource_access import ResourceActor
from app.domain.temporal_memory import Member, digest
from app.services import paper_bank


class CurrentDestinations:
    def __init__(self, topic: str, actor: ResourceActor | None = None):
        self.topic = topic
        self.reason: str | None = None
        catalog: list[dict[str, Any]] = []
        try:
            catalog, _, _, _ = paper_bank._load_catalog(topic, actor=actor)
        except (paper_bank.PaperBankError, OSError, ValueError):
            self.reason = "CURRENT_DESTINATIONS_UNAVAILABLE"
        self.versions = {item["paperVersionId"]: item for item in catalog}

    def resolve(self, members: Iterable[Member], evidence_ids: Iterable[str]) -> dict[str, Any]:
        identities = list(evidence_ids)
        paper_links = []
        evidence_links = []
        for member in members:
            current = self.versions.get(member.paper_version_id)
            if current is None:
                continue
            destination = f"/topics/{quote(self.topic, safe='')}/papers?paper={quote(member.paper_version_id, safe='')}"
            paper_links.append(dict(id=member.paper_version_id, destination=destination))
            if member.evidence_state != "available":
                continue
            for evidence in paper_bank._evidence(
                current["_selectedReview"], topic_id=self.topic, paper_version_id=member.paper_version_id
            ):
                identity = evidence["id"]
                captured_digest = digest({"content_digest": evidence["contentDigest"], "locator": evidence["locator"]})
                if identity in identities and evidence["destination"] and (identity, captured_digest) in member.evidence_digests:
                    evidence_links.append(dict(id=identity, destination=f"{destination}&evidence={quote(identity, safe='')}"))
        linked = {link["id"] for link in evidence_links}
        return {
            "paper_destinations": paper_links,
            "evidence_destinations": evidence_links,
            "unavailable_evidence_ids": [identity for identity in identities if identity not in linked],
            "destination_reason": self.reason or ("NO_CURRENT_DESTINATION" if not paper_links else None),
        }
