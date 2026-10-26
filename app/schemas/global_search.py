from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

GlobalSearchKind = Literal["topic", "paper", "evidence", "run"]


class GlobalSearchQuery(BaseModel):
    raw: str
    normalized: str
    tokens: list[str] = Field(default_factory=list)


class GlobalSearchItem(BaseModel):
    kind: GlobalSearchKind
    resultId: str
    topicId: str | None = None
    paperVersionId: str | None = None
    evidenceId: str | None = None
    runId: str | None = None
    primaryLabel: str
    secondaryText: str = ""
    route: str
    rank: int = Field(ge=1, le=9)
    rankReason: str
    matchedFields: list[str] = Field(default_factory=list)
    sourceState: Literal["available", "empty", "unavailable"] = "available"
    updatedAt: str | None = None


class GlobalSearchResponse(BaseModel):
    query: GlobalSearchQuery
    queryDigest: str
    requestedKinds: list[str] = Field(default_factory=list)
    limit: int = Field(ge=1, le=100)
    total: int = Field(ge=0)
    items: list[GlobalSearchItem] = Field(default_factory=list)
    sourceStates: dict[str, Literal["available", "empty", "unavailable"]] = Field(default_factory=dict)
    unavailableKinds: dict[str, str] = Field(default_factory=dict)


class SearchHelpResponse(BaseModel):
    normalization: dict[str, str]
    limits: dict[str, int]
    syntax: dict[str, dict[str, str]]
    scopes: dict[str, str]
    examples: list[dict[str, str]] = Field(default_factory=list)
