"""Content identities for local recommendation output, not corpus snapshots."""

from typing import Any

from app.domain.temporal_memory import digest
from app.schemas.recommendations import NewTopicCandidate, ResearchGapCandidate

SCHEMA = "M3C_RECOMMENDATION_VERSION_V1"


def content(topic: str, payload: dict[str, Any], candidate: dict[str, Any], display: dict[str, Any]) -> dict[str, Any]:
    summary = payload.get("summary") or {}
    return dict(schema_version=SCHEMA, topic_id=topic, candidate_type=display["type"], candidate_id=display["id"],
                generator_version=payload.get("recommendation_version"), topic_title=payload.get("topic_title"),
                coverage_warning=payload.get("coverage_warning") or summary.get("coverage_warning"),
                scope="LOCAL_REVIEW_OUTPUT", global_novelty="NOT_ASSESSED",
                candidate=candidate, display=display)


def identity(value: dict[str, Any]) -> str:
    return "rver_" + digest(value)


def validate(value: dict[str, Any]) -> None:
    if value["schema_version"] != SCHEMA or not value["generator_version"]:
        raise ValueError("invalid_recommendation_version")
    if value["candidate_type"] == "gap":
        gap = ResearchGapCandidate.model_validate(value["candidate"])
        if gap.topic_id != value["topic_id"] or gap.gap_id != value["candidate_id"]:
            raise ValueError("invalid_recommendation_version")
    elif value["candidate_type"] == "new_topic":
        topic = NewTopicCandidate.model_validate(value["candidate"])
        if topic.topic_candidate_id != value["candidate_id"]:
            raise ValueError("invalid_recommendation_version")
    else:
        raise ValueError("invalid_recommendation_version")
