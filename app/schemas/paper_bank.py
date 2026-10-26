from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResourceStateResponse(StrictModel):
    state: Literal["available", "empty", "unavailable"]
    reason: str | None = None


class PaperSourceIdentityResponse(StrictModel):
    provider: str
    nativeId: str | None = None
    doi: str | None = None


class PaperOccurrenceResponse(StrictModel):
    runId: str
    paperId: str
    metadataDigest: str
    observedAt: str | None = None


class PaperCompatibilityResponse(StrictModel):
    runId: str
    paperId: str
    occurrences: list[PaperOccurrenceResponse] = Field(default_factory=list)


class PaperReviewProjectionResponse(StrictModel):
    state: Literal[
        "not_reviewed",
        "abstract_reviewed",
        "full_text_available",
        "full_text_reviewed",
        "unavailable",
    ]
    updatedAt: str | None = None
    evidenceCount: int = 0


class PaperRightsResponse(StrictModel):
    state: Literal["open_access", "restricted", "declared", "unknown"]
    label: str | None = None


class PaperFacetOptionResponse(StrictModel):
    value: str
    label: str
    count: int


class PaperFacetResponse(StrictModel):
    state: Literal["available", "empty", "unavailable"]
    options: list[PaperFacetOptionResponse] = Field(default_factory=list)
    selected: str | None = None
    unknownCount: int = 0
    reason: str | None = None


class PaperBankFacetsResponse(StrictModel):
    year: PaperFacetResponse
    reviewLevel: PaperFacetResponse
    access: PaperFacetResponse
    rights: PaperFacetResponse


class PaperBankItemResponse(StrictModel):
    canonicalPaperId: str
    paperVersionId: str
    sourceIdentity: PaperSourceIdentityResponse
    compatibility: PaperCompatibilityResponse
    metadataDigest: str
    title: str
    authors: list[str] = Field(default_factory=list)
    publishedAt: str | None = None
    sourceProvider: str
    sourceUrl: str | None = None
    sourceUrlState: Literal["available", "unavailable"]
    theme: str | None = None
    publicationYear: int | None = None
    publicationYearState: Literal["known", "unknown"] = "unknown"
    accessState: Literal["open_access", "restricted", "declared", "unknown"] = "unknown"
    accessLabel: str | None = None
    review: PaperReviewProjectionResponse
    collectionIds: list[str] = Field(default_factory=list)


class PaperBankCanonicalQueryResponse(StrictModel):
    topicId: str
    q: str = ""
    provider: str | None = None
    collectionId: str | None = None
    year: str | None = None
    reviewState: str | None = None
    openAccess: str | None = None
    sort: str
    page: int
    pageSize: int


class PaperBankProviderCountResponse(StrictModel):
    provider: str
    label: str
    paperCount: int


class PaperReviewSummaryResponse(StrictModel):
    notReviewed: int = 0
    abstractReviewed: int = 0
    fullTextAvailable: int = 0
    fullTextReviewed: int = 0


class UnsupportedPaperFilterResponse(StrictModel):
    id: str
    state: Literal["unavailable"]
    reason: str


class PaperBankResponse(StrictModel):
    topicId: str
    items: list[PaperBankItemResponse] = Field(default_factory=list)
    total: int
    returnedCount: int
    page: int
    pageSize: int
    totalPages: int
    canonicalQuery: PaperBankCanonicalQueryResponse
    queryDigest: str
    providerCounts: list[PaperBankProviderCountResponse] = Field(default_factory=list)
    reviewResource: ResourceStateResponse
    reviewSummary: PaperReviewSummaryResponse | None = None
    collectionResource: ResourceStateResponse
    resource: ResourceStateResponse
    facets: PaperBankFacetsResponse
    unsupportedFilters: list[UnsupportedPaperFilterResponse] = Field(default_factory=list)


class PaperFullTextResponse(StrictModel):
    state: str
    artifactRef: str | None = None
    contentDigest: str | None = None


class PaperEvidenceResponse(StrictModel):
    id: str
    locator: dict[str, Any] | None = None
    contentDigest: str | None = None
    text: str | None = None
    destination: str | None = None
    destinationState: Literal["available", "unavailable"]


class PaperProvenanceResponse(StrictModel):
    runIds: list[str] = Field(default_factory=list)
    observedAt: str | None = None


class PaperBankDetailResponse(PaperBankItemResponse):
    topicId: str
    abstract: str | None = None
    abstractKind: Literal["provider_abstract", "unavailable"]
    fullText: PaperFullTextResponse
    rights: PaperRightsResponse
    evidence: list[PaperEvidenceResponse] = Field(default_factory=list)
    evidenceResource: ResourceStateResponse
    provenance: PaperProvenanceResponse


class PaperBankExportRequest(StrictModel):
    q: str = ""
    provider: str | None = None
    collectionId: str | None = None
    year: str | None = None
    reviewState: str | None = None
    openAccess: str | None = None
    sort: str = "published_desc"
    page: int = Field(default=1, ge=1)
    pageSize: int = Field(default=25, ge=1, le=100)


class PaperBankExportResponse(StrictModel):
    exportId: str
    topicId: str
    canonicalQuery: PaperBankCanonicalQueryResponse
    queryDigest: str
    rowCount: int
    paperVersionIds: list[str] = Field(default_factory=list)
    generatedAt: str
    format: Literal["csv"]
    mediaType: str
    fileName: str
    contentSha256: str
    artifactRef: str
    downloadUrl: str


class PaperCollectionBulkSaveRequest(StrictModel):
    selectedPaperVersionIds: list[str] = Field(min_length=1, max_length=500)


class PaperCollectionMemberResponse(StrictModel):
    canonicalPaperId: str
    paperVersionId: str
    addedAt: str | None = None


class PaperCollectionResponse(StrictModel):
    id: str
    name: str
    isDefault: bool
    createdAt: str | None = None
    updatedAt: str | None = None
    memberCount: int
    members: list[PaperCollectionMemberResponse] = Field(default_factory=list)


class PaperCollectionBulkSaveResponse(StrictModel):
    collection: PaperCollectionResponse
    requestedCount: int
    uniqueCount: int
    addedCount: int
    alreadyPresentCount: int
    finalMemberCount: int
    destinationCollectionId: str


class PaperCollectionListResponse(StrictModel):
    topicId: str
    items: list[PaperCollectionResponse] = Field(default_factory=list)
    resource: ResourceStateResponse


class PaperCollectionCreateRequest(StrictModel):
    name: str = Field(min_length=1, max_length=80)


class PaperCollectionRenameRequest(StrictModel):
    name: str = Field(min_length=1, max_length=80)


class PaperCollectionDeleteResponse(StrictModel):
    id: str
    deleted: bool
