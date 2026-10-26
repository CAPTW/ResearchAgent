from __future__ import annotations

from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.services import reviews, workbench


ClaimImportance = Literal["high", "medium", "low"]


class EvidenceLocation(BaseModel):
    page: int | None = None
    section: str | None = None
    reference: str | None = None


class EvidenceReference(BaseModel):
    evidence_id: str
    source_id: str
    paper_id: str
    title: str
    source_type: str
    snippet: str
    summary: str | None = None
    location: EvidenceLocation = Field(default_factory=EvidenceLocation)
    quality_hint: str | None = None
    confidence: float | None = None
    artifact_path: str | None = None
    citation_text: str
    entry_url: str | None = None
    pdf_url: str | None = None
    theme: str | None = None
    score: int | None = None


class ClaimCitation(BaseModel):
    evidence_id: str
    source_id: str
    paper_id: str
    artifact_path: str | None = None
    citation_text: str
    snippet: str


class EvidenceBackedClaim(BaseModel):
    claim_id: str
    claim_text: str
    importance: ClaimImportance = "medium"
    source_ids: list[str] = Field(default_factory=list)
    citations: list[ClaimCitation] = Field(default_factory=list)


class ResearchPlanSection(BaseModel):
    section_id: str
    title: str
    summary: str = ""
    paper_ids: list[str] = Field(default_factory=list)
    claims: list[EvidenceBackedClaim] = Field(default_factory=list)


class ResearchPlanGeneratorInfo(BaseModel):
    mode: Literal["provider", "heuristic"] = "heuristic"
    provider_name: str | None = None
    model: str | None = None
    response_id: str | None = None
    warnings: list[str] = Field(default_factory=list)


class ResearchPlanArtifact(BaseModel):
    run_id: str
    topic: str
    generated_at: str
    evidence_count: int = 0
    source_count: int = 0
    generator: ResearchPlanGeneratorInfo = Field(default_factory=ResearchPlanGeneratorInfo)
    sections: list[ResearchPlanSection] = Field(default_factory=list)


def build_citation_text(paper_item: dict[str, Any]) -> str:
    title = str(paper_item.get("title", "")).strip() or "Untitled paper"
    paper_id = str(paper_item.get("paper_id", "")).strip()
    published = str(paper_item.get("published", "")).strip()
    if published:
        return f"{title} [{paper_id}] ({published})"
    return f"{title} [{paper_id}]"


def collect_run_evidence(run_id: str, *, max_papers: int = 8, max_evidence_per_paper: int = 3) -> list[EvidenceReference]:
    items = workbench.load_run_triage_items(run_id)
    selected_items = [item for item in items if item.get("paper_id")][:max_papers]
    evidence_rows: list[EvidenceReference] = []
    for item in selected_items:
        paper_id = str(item.get("paper_id", "")).strip()
        if not paper_id:
            continue
        review_payload = reviews.load_review_payload(run_id, item)
        for evidence_item in list(review_payload.get("evidence") or [])[:max_evidence_per_paper]:
            evidence_rows.append(
                EvidenceReference.model_validate(
                    {
                        "evidence_id": evidence_item.get("evidence_id") or f"{paper_id}-evidence",
                        "source_id": paper_id,
                        "paper_id": paper_id,
                        "title": item.get("title", ""),
                        "source_type": evidence_item.get("source_type", "unknown"),
                        "snippet": evidence_item.get("snippet", ""),
                        "summary": evidence_item.get("summary"),
                        "location": evidence_item.get("location") or {},
                        "quality_hint": evidence_item.get("quality_hint"),
                        "confidence": evidence_item.get("confidence"),
                        "artifact_path": evidence_item.get("artifact_path"),
                        "citation_text": build_citation_text(item),
                        "entry_url": item.get("entry_url"),
                        "pdf_url": item.get("pdf_url"),
                        "theme": item.get("theme"),
                        "score": int(item.get("score", 0) or 0),
                    }
                )
            )
    return evidence_rows


def collect_research_context(run_id: str, *, max_papers: int = 8, max_evidence_per_paper: int = 3) -> dict[str, Any]:
    items = workbench.load_run_triage_items(run_id)
    selected_items = [item for item in items if item.get("paper_id")][:max_papers]
    theme_counts = Counter(item.get("theme") or "Uncategorized" for item in items[:25])
    evidence_rows = collect_run_evidence(run_id, max_papers=max_papers, max_evidence_per_paper=max_evidence_per_paper)
    return {
        "papers": [
            {
                "paper_id": str(item.get("paper_id", "")).strip(),
                "title": item.get("title", ""),
                "theme": item.get("theme", "Uncategorized"),
                "score": int(item.get("score", 0) or 0),
                "abstract_excerpt": str(item.get("abstract", "")).strip()[:480],
                "citation_text": build_citation_text(item),
                "entry_url": item.get("entry_url"),
                "pdf_url": item.get("pdf_url"),
            }
            for item in selected_items
        ],
        "themes": [{"theme": theme, "count": count} for theme, count in theme_counts.most_common(6)],
        "evidence": [row.model_dump() for row in evidence_rows],
    }


def resolve_citations(
    evidence_catalog: dict[str, EvidenceReference],
    citation_rows: list[dict[str, Any]] | None,
) -> list[ClaimCitation]:
    resolved: list[ClaimCitation] = []
    seen_ids: set[str] = set()
    for row in citation_rows or []:
        evidence_id = str((row or {}).get("evidence_id", "")).strip()
        if not evidence_id or evidence_id in seen_ids:
            continue
        evidence_row = evidence_catalog.get(evidence_id)
        if evidence_row is None:
            continue
        seen_ids.add(evidence_id)
        resolved.append(
            ClaimCitation(
                evidence_id=evidence_row.evidence_id,
                source_id=evidence_row.source_id,
                paper_id=evidence_row.paper_id,
                artifact_path=evidence_row.artifact_path,
                citation_text=evidence_row.citation_text,
                snippet=evidence_row.snippet,
            )
        )
    return resolved
