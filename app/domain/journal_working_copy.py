"""Partial user writing, separate from citation-validated journal versions."""
import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA = "JOURNAL_WORKING_COPY_V1"


def encoded(value: Any) -> str:
    # Recovery preserves exact codepoints, not NFC-normalized prose.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["observation", "interpretation", "question"]
    text: str = Field(max_length=3000)
    citation_ids: list[str] = Field(max_length=20)

    @field_validator("citation_ids")
    @classmethod
    def references(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(not 1 <= len(ref) <= 100 for ref in value):
            raise ValueError("journal_citation_invalid")
        return value


class Content(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    base_version_id: str | None = Field(max_length=100)
    title: str = Field(max_length=200)
    entries: list[Entry] = Field(min_length=1, max_length=30)


class Save(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=100, pattern=r"\S")
    expected_copy_id: str | None = Field(default=None, max_length=100)
    expected_input_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    content: Content | None  # None releases the current recovery checkpoint.
