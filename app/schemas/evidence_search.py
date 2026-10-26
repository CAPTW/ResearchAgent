from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class CorpusKind(str, Enum):
    paper = "paper"
    review = "review"
    evidence = "evidence"
    run = "run"
    report_section = "report_section"


class ResultKind(str, Enum):
    paper = "paper"
    run = "run"
    report_section = "report_section"


class ReviewLevel(str, Enum):
    abstract_only = "abstract_only"
    full_text_reviewed = "full_text_reviewed"


class ReviewSource(str, Enum):
    full_text = "full_text"
    abstract = "abstract"
    provider_output_class_proof = "provider_output_class_proof"


class EvidenceStatus(str, Enum):
    full_text_reviewed = "full_text_reviewed"
    abstract_only = "abstract_only"
    provider_proof_backed = "provider_proof_backed"
    deterministic_local = "deterministic_local"
    missing_evidence = "missing_evidence"
    missing_artifact = "missing_artifact"
    stale = "stale"
    current = "current"
    degraded = "degraded"
    orphaned_reference = "orphaned_reference"
    unverified_reference = "unverified_reference"


class WarningCode(str, Enum):
    report_provenance_not_indexed = "report_provenance_not_indexed"
    paper_id_collision = "paper_id_collision"
    orphaned_reference = "orphaned_reference"
    missing_evidence = "missing_evidence"
    malformed_provider_report = "malformed_provider_report"
    provider_ownership_unproven = "provider_ownership_unproven"


class ProvenanceTruthLevel(str, Enum):
    exact_persisted = "exact_persisted"
    exact_indexed_projection = "exact_indexed_projection"
    exact_portable_ref = "exact_portable_ref"
    derived_deterministic = "derived_deterministic"


class ProvenanceNodeKind(str, Enum):
    topic = "topic"
    run = "run"
    paper = "paper"
    review = "review"
    evidence = "evidence"
    evidence_span = "evidence_span"
    report = "report"
    report_section = "report_section"
    artifact = "artifact"
    provider = "provider"
    provider_proof = "provider_proof"


class ProvenanceEdgeKind(str, Enum):
    topic_contains_paper = "topic_contains_paper"
    run_contains_paper = "run_contains_paper"
    paper_has_review = "paper_has_review"
    review_cites_evidence_span = "review_cites_evidence_span"
    run_has_evidence = "run_has_evidence"
    evidence_cites_paper = "evidence_cites_paper"
    review_references_artifact = "review_references_artifact"
    provider_generated_review = "provider_generated_review"
    evidence_span_references_provider_proof = "evidence_span_references_provider_proof"
    run_has_report = "run_has_report"
    report_has_section = "report_has_section"
    run_derived_from_run = "run_derived_from_run"
    provider_report_references_review = "provider_report_references_review"
    provider_report_references_paper = "provider_report_references_paper"
    provider_report_references_evidence_span = "provider_report_references_evidence_span"
    provider_report_references_proof = "provider_report_references_proof"
    run_topic_matches_profile = "run_topic_matches_profile"


class ArtifactVerificationStatus(str, Enum):
    current = "current"
    stale = "stale"
    missing_artifact = "missing_artifact"
    orphaned_reference = "orphaned_reference"
    unverified_reference = "unverified_reference"


class SafeLinks(BaseModel):
    topic: str | None = None
    run: str | None = None
    report: str | None = None
    paper: str | None = None


class EvidenceSnippet(BaseModel):
    text: str = Field(max_length=240)
    sourceField: str
    truncated: bool = False


class RankComponents(BaseModel):
    tier: int = Field(ge=1, le=9)
    tokenCoverage: int = Field(ge=0)
    effectiveTimestamp: str = ""
    kindOrder: int = Field(ge=0)


class EvidenceOccurrence(BaseModel):
    runId: str
    rawPaperId: str
    title: str
    topicId: str | None = None
    topicName: str
    runStatus: str
    paperStatus: str
    theme: str
    score: int
    published: str | None = None
    entryUrl: str | None = None
    pdfUrl: str | None = None
    updatedAt: str
    links: SafeLinks


class EvidenceReviewSummary(BaseModel):
    topicId: str
    source: ReviewSource
    providerId: str | None = None
    level: ReviewLevel
    reviewedAt: str
    reviewModel: str | None = None
    inlineEvidenceSpanCount: int = Field(ge=0)


class ProvenanceNode(BaseModel):
    nodeId: str
    nodeKind: ProvenanceNodeKind
    stableId: str
    label: str
    topicId: str | None = None
    runId: str | None = None
    paperId: str | None = None
    portableRef: str | None = None
    status: EvidenceStatus | None = None


class ProvenanceEdge(BaseModel):
    edgeId: str
    edgeKind: ProvenanceEdgeKind
    fromNodeId: str
    toNodeId: str
    truthLevel: ProvenanceTruthLevel


class EvidenceSearchItem(BaseModel):
    resultId: str
    resultKind: ResultKind
    canonicalPaperId: str | None = None
    paperIdAliases: list[str] = Field(default_factory=list)
    title: str
    titleAuthority: str
    safeSnippet: EvidenceSnippet | None = None
    matchedFields: list[str] = Field(default_factory=list)
    matchedCorpusKinds: list[CorpusKind] = Field(default_factory=list)
    matchReason: str
    rankTier: int = Field(ge=1, le=9)
    rankComponents: RankComponents
    topicIds: list[str] = Field(default_factory=list)
    reviewSummary: EvidenceReviewSummary | None = None
    inlineEvidenceSpanCount: int = Field(default=0, ge=0)
    runEvidenceCount: int = Field(default=0, ge=0)
    occurrenceCount: int = Field(default=0, ge=0)
    occurrences: list[EvidenceOccurrence] = Field(default_factory=list)
    occurrencesTruncated: bool = False
    provenancePreview: list[ProvenanceEdge] = Field(default_factory=list)
    statuses: list[EvidenceStatus] = Field(default_factory=list)
    warnings: list[WarningCode] = Field(default_factory=list)
    links: SafeLinks = Field(default_factory=SafeLinks)


class EvidenceSearchFiltersEcho(BaseModel):
    topicIds: list[str] = Field(default_factory=list)
    runIds: list[str] = Field(default_factory=list)
    corpusKinds: list[CorpusKind] = Field(default_factory=list)
    reviewSources: list[ReviewSource] = Field(default_factory=list)
    providerIds: list[str] = Field(default_factory=list)
    reviewLevels: list[ReviewLevel] = Field(default_factory=list)
    hasEvidence: bool | None = None


class EvidenceSearchQueryEcho(BaseModel):
    raw: str
    normalized: str
    tokens: list[str]
    filters: EvidenceSearchFiltersEcho


class EvidenceSearchPage(BaseModel):
    limit: int = Field(ge=1, le=100)
    returnedCount: int = Field(ge=0)
    total: int = Field(ge=0)
    totalRelation: str = "exact"
    nextCursor: str | None = None


class EvidenceSearchFacets(BaseModel):
    corpusKinds: dict[str, int] = Field(default_factory=dict)
    reviewLevels: dict[str, int] = Field(default_factory=dict)
    reviewSources: dict[str, int] = Field(default_factory=dict)
    providers: dict[str, int] = Field(default_factory=dict)
    statuses: dict[str, int] = Field(default_factory=dict)


class EvidenceSearchResponse(BaseModel):
    query: EvidenceSearchQueryEcho
    page: EvidenceSearchPage
    facets: EvidenceSearchFacets
    items: list[EvidenceSearchItem]


class ArtifactVerification(BaseModel):
    portableRef: str | None = None
    refKind: str
    status: ArtifactVerificationStatus
    exists: bool
    expectedHashPresent: bool
    hashMatches: bool | None = None


class ProviderReportSummary(BaseModel):
    draftId: str
    providerFamilyId: str
    providerSurfaceId: str
    sourceReviewIds: list[str]
    sourcePaperIds: list[str]
    evidenceSpanRefs: list[str]
    proofRefs: list[str]


class EvidenceDetailResponse(BaseModel):
    resultId: str
    resultKind: ResultKind
    summary: EvidenceSearchItem
    occurrenceCount: int = Field(ge=0)
    occurrences: list[EvidenceOccurrence]
    occurrencesTruncated: bool
    selectedOccurrenceRunId: str | None = None
    provenanceNodes: list[ProvenanceNode]
    provenanceEdges: list[ProvenanceEdge]
    artifactVerifications: list[ArtifactVerification]
    providerReport: ProviderReportSummary | None = None
    warnings: list[WarningCode]
    links: SafeLinks
