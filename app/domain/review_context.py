"""Bounded catalog context captured with a range, not extra source evidence."""
from copy import deepcopy
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.review_preparation import encoded

SCHEMA = "REVIEW_CONTEXT_V1"


class Context(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["REVIEW_CONTEXT_V1"]
    topic_title: str = Field(min_length=1, max_length=8000)
    topic_name: str = Field(min_length=1, max_length=8000)
    paper_title: str = Field(min_length=1, max_length=8000)
    authors: list[str] = Field(max_length=500)
    published_at: str | None
    metadata_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    topic_updated_at: str = Field(min_length=1, max_length=100)


def publication(value: Any) -> str | None:
    """Keep valid observed date text exactly; never invent a publication date."""
    if not isinstance(value, str) or not value or len(value) > 100:
        return None
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return None
    return value


def validate(value: Any) -> dict[str, Any]:
    try:
        result = Context.model_validate(value, strict=True).model_dump()
        if (encoded(result) != encoded(value) or len(encoded(result).encode("utf-8", errors="strict")) > 65536
                or any(not result[key].strip() for key in ("topic_title", "topic_name", "paper_title", "topic_updated_at"))
                or any(not name.strip() or len(name) > 1000 for name in result["authors"])
                or publication(result["published_at"]) != result["published_at"]):
            raise ValueError()
        return deepcopy(result)
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise ValueError("review_preparation_context_invalid") from None


def capture(profile: dict[str, Any], paper: dict[str, Any]) -> dict[str, Any]:
    """Call only with the authorized topic and its selected catalog version."""
    return validate(dict(schema_version=SCHEMA, topic_title=profile["title"], topic_name=profile["topic"],
        paper_title=paper["title"], authors=paper["authors"], published_at=publication(paper["_raw"].get("published")),
        metadata_digest=paper["metadataDigest"], topic_updated_at=profile["updated_at"]))
