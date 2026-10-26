from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class RunPaths:
    run_dir: Path
    inputs_dir: Path
    raw_dir: Path
    triage_dir: Path
    notes_dir: Path
    reviews_dir: Path
    reports_dir: Path
    run_json_path: Path
    events_path: Path


@dataclass(frozen=True)
class EvidenceLocation:
    page: int | None = None
    section: str | None = None
    reference: str | None = None


@dataclass(frozen=True)
class EvidenceItem:
    evidence_id: str
    source_type: str
    snippet: str
    summary: str | None = None
    location: EvidenceLocation = field(default_factory=EvidenceLocation)
    quality_hint: str | None = None
    confidence: float | None = None
    artifact_path: str | None = None


@dataclass(frozen=True)
class OutlineSection:
    section_id: str
    title: str
    kind: str
    body_markdown: str = ""
    paper_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReportOutline:
    run_id: str
    sections: tuple[OutlineSection, ...]
    path: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class ReportArtifactFallback:
    run_side: str
    artifact_kind: str
    available: bool = False
    used: bool = False
    path: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ReportSectionSnapshot:
    section_key: str
    title: str
    order: int
    body: str = ""
    excerpt: str | None = None
    body_hash: str | None = None
    paper_ids: tuple[str, ...] = ()
    citations: tuple[str, ...] = ()
    references: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReportDocumentSnapshot:
    run_id: str
    source: str
    completeness: str
    confidence: float
    path: str | None = None
    content_hash: str | None = None
    sections: tuple[ReportSectionSnapshot, ...] = ()
    missing_explanations: tuple[str, ...] = ()
