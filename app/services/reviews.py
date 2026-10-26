from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.domain import constants
from app.domain.constants import LEGACY_NOTES_ROOT, LEGACY_PDF_ROOT, LEGACY_TEXT_ROOT
from app.repositories import artifact_refs
from app.repositories.runs import build_run_paths
from app.repositories.files import read_jsonl, safe_filename, write_json
from app.services import research_domain, research_transport

LEGACY_PDF_ALLOWED_HOSTS = frozenset({"arxiv.org"})
LEGACY_PDF_MAX_BYTES = 25_000_000
LEGACY_PDF_TIMEOUT_SECONDS = 60


def _legacy_pdf_transport_client() -> research_transport.ResearchHttpClient:
    policy = research_transport.ResearchTransportPolicy.create(
        LEGACY_PDF_ALLOWED_HOSTS,
        total_timeout_seconds=float(LEGACY_PDF_TIMEOUT_SECONDS),
    )
    return research_transport.ResearchHttpClient(policy)


def _legacy_artifact_ref(path: Path) -> str:
    portable = artifact_refs.normalize_path_to_ref(
        path,
        repo_root=constants.PROJECT_ROOT,
        storage_roots=[constants.STORAGE_ROOT],
    )
    if portable and artifact_refs.is_portable_ref(portable):
        return portable
    return f"artifact:{path.name}"


def _read_json_with_bom(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8-sig", errors="ignore")
    return json.loads(text)


def _review_template_for_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "paper_id": item.get("paper_id", ""),
        "title": item.get("title", ""),
        "links": {
            "entry_url": item.get("entry_url") or item.get("entry_id", ""),
            "pdf_url": item.get("pdf_url", ""),
            "doi": item.get("doi", ""),
        },
        "theme": item.get("theme", ""),
        "read_level": "FULLTEXT_REQUIRED",
        "review_ko": {
            "one_liner": "",
            "why_it_matters": [],
            "key_contributions": [],
            "method": {
                "problem": "",
                "inputs_outputs": "",
                "model": "",
                "training": "",
                "inference": "",
                "system_constraints": [],
            },
            "experiments": {
                "datasets": [],
                "metrics": [],
                "baselines": [],
                "key_results": [],
                "runtime_latency": "",
                "hardware": "",
            },
            "xr_takeaways": [],
            "limitations": [],
            "reproducibility": {
                "code": "",
                "weights": "",
                "license": "",
                "notes": "",
            },
            "lab_actions": [],
            "confidence": 0.0,
            "evidence": [
                {"page": 0, "section": "", "note": "여기에 페이지 근거를 남기세요(직접 인용은 짧게)."}
            ],
        },
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def normalize_review(review_obj: dict[str, Any]) -> dict[str, Any]:
    review = dict(review_obj or {})
    review_ko = dict(review.get("review_ko") or {})
    evidence = review_ko.get("evidence") or []
    normalized_evidence: list[dict[str, Any]] = []
    for item in evidence:
        if isinstance(item, dict):
            normalized_evidence.append(
                {
                    "page": item.get("page"),
                    "section": item.get("section", ""),
                    "reference": item.get("reference", ""),
                    "note": item.get("note") or item.get("snippet", "") or item.get("summary", ""),
                    "summary": item.get("summary", ""),
                    "source_type": item.get("source_type", "review_note"),
                    "quality_hint": item.get("quality_hint", "manual_review"),
                    "confidence": item.get("confidence"),
                    "artifact_path": item.get("artifact_path"),
                }
            )
        elif isinstance(item, str):
            normalized_evidence.append(
                {
                    "page": None,
                    "section": "",
                    "reference": "",
                    "note": item,
                    "summary": "",
                    "source_type": "review_note",
                    "quality_hint": "manual_review",
                    "confidence": None,
                    "artifact_path": None,
                }
            )
    review_ko["evidence"] = normalized_evidence
    review["review_ko"] = review_ko
    return review


def _first_existing_path(candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def resolve_paper_artifacts(run_id: str, paper_id: str) -> dict[str, str | None]:
    run_paths = build_run_paths(run_id)
    file_name = f"{safe_filename(paper_id)}"
    return {
        "review_json": (
            _first_existing_path(
                [
                    run_paths.reviews_dir / f"{file_name}.json",
                    LEGACY_NOTES_ROOT / "reviews" / f"{file_name}.json",
                ]
            )
            or None
        )
        and str(
            _first_existing_path(
                [
                    run_paths.reviews_dir / f"{file_name}.json",
                    LEGACY_NOTES_ROOT / "reviews" / f"{file_name}.json",
                ]
            )
        ),
        "note_markdown": (
            _first_existing_path(
                [
                    run_paths.notes_dir / "papers" / f"{file_name}.md",
                    LEGACY_NOTES_ROOT / "papers" / f"{file_name}.md",
                ]
            )
            or None
        )
        and str(
            _first_existing_path(
                [
                    run_paths.notes_dir / "papers" / f"{file_name}.md",
                    LEGACY_NOTES_ROOT / "papers" / f"{file_name}.md",
                ]
            )
        ),
        "pdf": (
            _first_existing_path(
                [
                    run_paths.raw_dir / f"{file_name}.pdf",
                    LEGACY_PDF_ROOT / f"{file_name}.pdf",
                ]
            )
            or None
        )
        and str(
            _first_existing_path(
                [
                    run_paths.raw_dir / f"{file_name}.pdf",
                    LEGACY_PDF_ROOT / f"{file_name}.pdf",
                ]
            )
        ),
        "text_txt": (
            _first_existing_path(
                [
                    run_paths.raw_dir / f"{file_name}.txt",
                    LEGACY_TEXT_ROOT / f"{file_name}.txt",
                ]
            )
            or None
        )
        and str(
            _first_existing_path(
                [
                    run_paths.raw_dir / f"{file_name}.txt",
                    LEGACY_TEXT_ROOT / f"{file_name}.txt",
                ]
            )
        ),
        "pages_json": (
            _first_existing_path(
                [
                    run_paths.raw_dir / f"{file_name}.pages.json",
                    LEGACY_TEXT_ROOT / f"{file_name}.pages.json",
                ]
            )
            or None
        )
        and str(
            _first_existing_path(
                [
                    run_paths.raw_dir / f"{file_name}.pages.json",
                    LEGACY_TEXT_ROOT / f"{file_name}.pages.json",
                ]
            )
        ),
    }


def _coerce_float(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except Exception:
        return default


def _first_sentence(text: str) -> str:
    cleaned = " ".join((text or "").strip().split())
    if not cleaned:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", cleaned, maxsplit=1)
    return parts[0][:320]


def _parse_evidence_location(note: str, existing: dict[str, Any]) -> dict[str, Any]:
    page = existing.get("page")
    if page is None:
        match = re.search(r"\bp\.?\s*(\d+)\b", note, re.IGNORECASE)
        page = int(match.group(1)) if match else None

    section = existing.get("section") or None
    if not section:
        section_match = re.search(r"\bSection\s+([A-Za-z0-9.\-]+)", note, re.IGNORECASE)
        section = section_match.group(0) if section_match else None

    reference = existing.get("reference") or None
    if not reference:
        reference_match = re.search(r"\b(Fig(?:ure)?|Table)\s+([A-Za-z0-9.\-]+)", note, re.IGNORECASE)
        reference = reference_match.group(0) if reference_match else None

    return {"page": page, "section": section, "reference": reference}


def _load_page_excerpt(pages_json_path: str | None, page: int | None) -> str | None:
    if not pages_json_path or not page:
        return None
    pages_obj = _read_json_with_bom(Path(pages_json_path)) if Path(pages_json_path).exists() else {}
    for page_obj in pages_obj.get("pages", []):
        if page_obj.get("page") == page:
            text = " ".join(str(page_obj.get("text", "")).split())
            return text[:320] if text else None
    return None


def _build_evidence_items(
    *,
    paper_id: str,
    paper_title: str,
    evidence_items: list[dict[str, Any]],
    artifacts: dict[str, str | None],
    overall_confidence: float | None,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    citation_text = f"{paper_title} [{paper_id}]" if paper_title else paper_id
    for index, evidence in enumerate(evidence_items, start=1):
        note = str(evidence.get("note", "")).strip()
        location = _parse_evidence_location(note, evidence)
        page_excerpt = _load_page_excerpt(artifacts.get("pages_json"), location.get("page"))
        normalized.append(
            {
                "evidence_id": f"{paper_id}-evidence-{index}",
                "source_id": paper_id,
                "paper_id": paper_id,
                "title": paper_title,
                "source_type": evidence.get("source_type", "review_note"),
                "snippet": note or page_excerpt or "근거 텍스트가 비어 있습니다.",
                "summary": evidence.get("summary") or page_excerpt,
                "location": location,
                "quality_hint": evidence.get("quality_hint", "manual_review"),
                "confidence": _coerce_float(evidence.get("confidence"), overall_confidence),
                "artifact_path": evidence.get("artifact_path") or artifacts.get("pages_json") or artifacts.get("review_json"),
                "citation_text": citation_text,
            }
        )
    return normalized


def _fallback_evidence(
    *,
    paper_id: str,
    paper_item: dict[str, Any],
    note_content: str,
    artifacts: dict[str, str | None],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    paper_title = str(paper_item.get("title", "")).strip()
    citation_text = f"{paper_title} [{paper_id}]" if paper_title else paper_id
    abstract = str(paper_item.get("abstract", "")).strip()
    if abstract:
        items.append(
            {
                "evidence_id": f"{paper_id}-abstract",
                "source_id": paper_id,
                "paper_id": paper_id,
                "title": paper_title,
                "source_type": "abstract",
                "snippet": _first_sentence(abstract) or abstract[:320],
                "summary": "초록 기반 fallback evidence",
                "location": {"page": None, "section": "Abstract", "reference": None},
                "quality_hint": "abstract_fallback",
                "confidence": 0.25,
                "artifact_path": paper_item.get("entry_url") or paper_item.get("pdf_url"),
                "citation_text": citation_text,
            }
        )
    if note_content:
        items.append(
            {
                "evidence_id": f"{paper_id}-note",
                "source_id": paper_id,
                "paper_id": paper_id,
                "title": paper_title,
                "source_type": "paper_note",
                "snippet": _first_sentence(note_content) or note_content[:320],
                "summary": "요약 노트 기반 fallback evidence",
                "location": {"page": None, "section": "Paper Note", "reference": None},
                "quality_hint": "note_fallback",
                "confidence": 0.3,
                "artifact_path": artifacts.get("note_markdown"),
                "citation_text": citation_text,
            }
        )
    if not items:
        items.append(
            {
                "evidence_id": f"{paper_id}-missing",
                "source_id": paper_id,
                "paper_id": paper_id,
                "title": paper_title,
                "source_type": "missing",
                "snippet": "아직 structured review/evidence artifact가 없습니다.",
                "summary": "review artifact missing",
                "location": {"page": None, "section": None, "reference": None},
                "quality_hint": "missing_artifact",
                "confidence": 0.0,
                "artifact_path": None,
                "citation_text": citation_text,
            }
        )
    return items


def load_review_payload(run_id: str, paper_item: dict[str, Any]) -> dict[str, Any]:
    paper_id = str(paper_item.get("paper_id", "")).strip()
    artifacts = resolve_paper_artifacts(run_id, paper_id)
    review_path = artifacts.get("review_json")
    note_path = artifacts.get("note_markdown")
    note_content = Path(note_path).read_text(encoding="utf-8", errors="ignore") if note_path else ""

    if review_path:
        try:
            raw_review = normalize_review(_read_json_with_bom(Path(review_path)))
            review_ok, missing_fields = review_is_filled(raw_review)
            status = "complete" if review_ok else "partial"
            completeness = 1.0 if review_ok else round(max(0.0, 1.0 - (len(missing_fields) / 10.0)), 2)
            review_ko = raw_review.get("review_ko") or {}
            evidence = _build_evidence_items(
                paper_id=paper_id,
                paper_title=str(paper_item.get("title", "")).strip(),
                evidence_items=list(review_ko.get("evidence") or []),
                artifacts=artifacts,
                overall_confidence=_coerce_float(review_ko.get("confidence")),
            )
        except Exception:
            raw_review = {}
            review_ko = {}
            missing_fields = ["invalid_review_json"]
            status = "partial"
            completeness = 0.0
            evidence = []
    else:
        raw_review = {}
        review_ko = {}
        missing_fields = ["review_json"]
        status = "missing"
        completeness = 0.0
        evidence = []

    if not evidence:
        evidence = _fallback_evidence(
            paper_id=paper_id,
            paper_item=paper_item,
            note_content=note_content,
            artifacts=artifacts,
        )

    one_liner = str(review_ko.get("one_liner", "")).strip() or _first_sentence(str(paper_item.get("abstract", "")))
    return {
        "run_id": run_id,
        "paper_id": paper_id,
        "status": status,
        "completeness": completeness,
        "missing_fields": missing_fields,
        "title": raw_review.get("title") or paper_item.get("title", ""),
        "theme": raw_review.get("theme") or paper_item.get("theme", "Uncategorized"),
        "one_liner": one_liner or None,
        "why_it_matters": list(review_ko.get("why_it_matters") or []),
        "key_contributions": list(review_ko.get("key_contributions") or []),
        "xr_takeaways": list(review_ko.get("xr_takeaways") or []),
        "limitations": list(review_ko.get("limitations") or []),
        "lab_actions": list(review_ko.get("lab_actions") or []),
        "confidence": _coerce_float(review_ko.get("confidence")),
        "method": dict(review_ko.get("method") or {}),
        "experiments": dict(review_ko.get("experiments") or {}),
        "evidence": evidence,
        "artifacts": artifacts,
        "raw_path": review_path,
    }


def get_review_status(run_id: str, paper_item: dict[str, Any]) -> str:
    artifacts = resolve_paper_artifacts(run_id, str(paper_item.get("paper_id", "")))
    review_path = artifacts.get("review_json")
    if review_path:
        try:
            review = normalize_review(_read_json_with_bom(Path(review_path)))
            is_filled, _ = review_is_filled(review)
            return "reviewed" if is_filled else "partial_review"
        except Exception:
            return "partial_review"
    if artifacts.get("note_markdown"):
        return "note_only"
    return "unreviewed"


def review_is_filled(review_obj: dict[str, Any]) -> tuple[bool, list[str]]:
    review = normalize_review(review_obj)
    missing: list[str] = []
    review_ko = review.get("review_ko") if isinstance(review, dict) else None
    if not isinstance(review_ko, dict):
        return False, ["review_ko"]

    def ok_text(value: Any, min_len: int = 10) -> bool:
        return isinstance(value, str) and len(value.strip()) >= min_len

    def ok_list(value: Any, min_len: int = 2) -> bool:
        return isinstance(value, list) and len([item for item in value if isinstance(item, str) and item.strip()]) >= min_len

    if not ok_text(review_ko.get("one_liner", ""), 10):
        missing.append("review_ko.one_liner")
    if not ok_list(review_ko.get("why_it_matters", []), 2):
        missing.append("review_ko.why_it_matters>=2")
    if not ok_list(review_ko.get("key_contributions", []), 2):
        missing.append("review_ko.key_contributions>=2")

    method = review_ko.get("method", {})
    if not isinstance(method, dict) or not ok_text(method.get("problem", ""), 10):
        missing.append("review_ko.method.problem")
    if not isinstance(method, dict) or not ok_text(method.get("model", ""), 8):
        missing.append("review_ko.method.model")

    experiments = review_ko.get("experiments", {})
    if not isinstance(experiments, dict) or not ok_list(experiments.get("key_results", []), 1):
        missing.append("review_ko.experiments.key_results>=1")
    if not ok_list(review_ko.get("limitations", []), 1):
        missing.append("review_ko.limitations>=1")
    if not ok_list(review_ko.get("lab_actions", []), 2):
        missing.append("review_ko.lab_actions>=2")

    try:
        confidence = float(review_ko.get("confidence", 0.0))
    except Exception:
        confidence = 0.0
    if confidence < 0.35:
        missing.append("review_ko.confidence>=0.35")

    if len(review_ko.get("evidence", [])) < 2:
        missing.append("review_ko.evidence>=2")

    return len(missing) == 0, missing


def _infer_theme(title: str, abstract: str) -> str:
    blob = f"{(title or '').lower()} {(abstract or '').lower()}"
    if any(word in blob for word in ["gaze", "eye tracking", "foveat", "pupil", "saccad", "fixation"]):
        return "Gaze/Foveation"
    if any(word in blob for word in ["avatar", "gaussian", "splatting", "3dgs", "nerf", "radiance field", "novel view"]):
        return "Avatars/3DGS"
    if any(word in blob for word in ["slam", "vio", "odometry", "localization", "mapping", "bundle adjustment", "imu"]):
        return "SLAM/VIO"
    if any(word in blob for word in ["egocentric", "first-person", "body pose", "full-body", "hand tracking", "gesture"]):
        return "Egocentric/Body"
    if any(word in blob for word in ["user study", "interaction", "hci", "interface", "collaboration"]):
        return "HCI/Interaction"
    if any(word in blob for word in ["privacy", "secure", "security", "private spatial"]):
        return "Privacy/Security"
    return "Uncategorized"


def fetch_pdfs(*, triage_path: Path, out_dir: Path, top_n: int, rank_offset: int = 0, overwrite: bool = False) -> dict[str, Any]:
    items = read_jsonl(triage_path)
    if not items:
        return {"status": "empty", "message": "Triage input was missing or empty."}

    selected = items[rank_offset : rank_offset + max(0, top_n)]
    out_dir.mkdir(parents=True, exist_ok=True)
    ok: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for item in selected:
        paper_id = item.get("paper_id") or ""
        if not paper_id:
            continue
        output_path = out_dir / f"{safe_filename(paper_id)}.pdf"
        if output_path.exists() and not overwrite:
            skipped.append({"paper_id": paper_id, "path": _legacy_artifact_ref(output_path)})
            continue

        url = item.get("pdf_url") or f"https://arxiv.org/pdf/{paper_id.replace('arXiv:', '').strip()}.pdf"
        try:
            result = _legacy_pdf_transport_client().download_pdf(
                str(url),
                target_path=output_path,
                max_bytes=LEGACY_PDF_MAX_BYTES,
                replace_existing=bool(overwrite),
            )
            ok.append(
                {
                    "paper_id": paper_id,
                    "title": item.get("title", ""),
                    "url": research_transport.redact_retrieval_url(str(url)),
                    "path": _legacy_artifact_ref(output_path),
                    "bytes_written": result.bytes_written,
                    "sha256": result.sha256,
                }
            )
        except research_transport.RetrievalTransportError as exc:
            failed.append(
                {
                    "paper_id": paper_id,
                    "url": research_transport.redact_retrieval_url(str(url)),
                    "reason": exc.code,
                }
            )
        except OSError:
            failed.append(
                {
                    "paper_id": paper_id,
                    "url": research_transport.redact_retrieval_url(str(url)),
                    "reason": "pdf_acquisition_failed",
                }
            )

    manifest = {
        "status": "ok",
        "triage": _legacy_artifact_ref(triage_path),
        "selected": len(selected),
        "downloaded": len(ok),
        "skipped": len(skipped),
        "failed": len(failed),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "items_ok": ok,
        "items_skipped": skipped,
        "items_failed": failed,
    }
    manifest_path = out_dir / f"manifest_{datetime.now(timezone.utc).strftime('%Y%m%d')}.json"
    write_json(manifest_path, manifest)
    return {
        "status": "ok",
        "manifest": _legacy_artifact_ref(manifest_path),
        "downloaded": len(ok),
        "skipped": len(skipped),
        "failed": len(failed),
    }


def extract_pdf_text(*, pdf_dir: Path, out_dir: Path, triage_path: Path, top_n: int, paper_id: str | None = None) -> dict[str, Any]:
    try:
        import fitz  # type: ignore
    except Exception:
        return {"status": "missing_dep", "message": "PyMuPDF not installed. Run: pip install pymupdf"}

    out_dir.mkdir(parents=True, exist_ok=True)
    target_ids = [paper_id.strip()] if paper_id else [item.get("paper_id", "") for item in read_jsonl(triage_path)[: max(0, top_n)]]
    target_ids = [value for value in target_ids if value]
    if not target_ids:
        return {"status": "empty", "message": "No target paper ids found."}

    def blocks_to_text(page: Any) -> str:
        try:
            blocks = page.get_text("blocks")
        except Exception:
            return page.get_text("text") or ""
        parts: list[str] = []
        for block in sorted(blocks, key=lambda value: (round(value[1], 1), round(value[0], 1))):
            text = (block[4] or "").strip()
            if text:
                parts.append(text)
        return "\n\n".join(parts).replace("\x00", "")

    results: list[dict[str, Any]] = []
    for target_id in target_ids:
        pdf_path = pdf_dir / f"{safe_filename(target_id)}.pdf"
        if not pdf_path.exists():
            results.append({"paper_id": target_id, "status": "missing_pdf", "pdf": str(pdf_path)})
            continue

        doc = fitz.open(str(pdf_path))
        pages: list[dict[str, Any]] = []
        joined_parts: list[str] = []
        for index in range(doc.page_count):
            page = doc.load_page(index)
            text = blocks_to_text(page)
            pages.append({"page": index + 1, "text": text})
            joined_parts.append(f"\n\n===== PAGE {index + 1} =====\n\n{text}")

        pages_path = out_dir / f"{safe_filename(target_id)}.pages.json"
        text_path = out_dir / f"{safe_filename(target_id)}.txt"
        write_json(
            pages_path,
            {
                "paper_id": target_id,
                "pdf": str(pdf_path),
                "page_count": doc.page_count,
                "extracted_at_utc": datetime.now(timezone.utc).isoformat(),
                "extraction": "blocks_sorted(y,x)",
                "pages": pages,
            },
        )
        text_path.write_text("".join(joined_parts), encoding="utf-8")
        results.append({"paper_id": target_id, "status": "ok", "page_count": doc.page_count, "out_pages": str(pages_path), "out_txt": str(text_path)})

    return {"status": "ok", "results": results}


def generate_review_templates(*, triage_path: Path, top_n: int, out_dir: Path, overwrite: bool = False) -> dict[str, Any]:
    triage = read_jsonl(triage_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    for item in triage[: max(0, top_n)]:
        paper_id = item.get("paper_id")
        if not paper_id:
            continue
        output_path = out_dir / f"{safe_filename(paper_id)}.json"
        if output_path.exists() and not overwrite:
            continue
        write_json(output_path, _review_template_for_item(item))
        created.append(str(output_path))
    return {"status": "ok", "created": created}


def compile_journal(
    *,
    run_date: str,
    triage_path: Path,
    shortlist: int,
    deep_dive: int,
    reviews_dir: Path,
    out_dir: Path,
    require_fulltext: bool,
    domain_context: research_domain.ResearchDomainContext | None = None,
) -> dict[str, Any]:
    triage = read_jsonl(triage_path)
    if not triage:
        return {"status": "empty", "message": "triage.jsonl missing or empty"}

    dt = datetime.fromisoformat(run_date)
    week = f"{dt.isocalendar().year}-W{dt.isocalendar().week:02d}"
    resolved_domain_context = (
        domain_context
        or research_domain.build_research_domain_context(
            source=research_domain.DomainContextSource.LEGACY_NEUTRAL
        )
    )
    shortlist_items = triage[: max(0, shortlist)]
    deep_items = triage[: max(0, deep_dive)]

    deep_reviews: list[dict[str, Any]] = []
    missing_or_empty: list[dict[str, Any]] = []
    for item in deep_items:
        paper_id = item.get("paper_id", "")
        if not paper_id:
            continue
        review_path = reviews_dir / f"{safe_filename(paper_id)}.json"
        if not review_path.exists():
            missing_or_empty.append({"paper_id": paper_id, "reason": "missing_review_file"})
            continue
        try:
            review = normalize_review(_read_json_with_bom(review_path))
        except json.JSONDecodeError as exc:
            missing_or_empty.append({"paper_id": paper_id, "reason": "invalid_json", "error": str(exc), "file": str(review_path)})
            continue
        valid, missing_fields = review_is_filled(review)
        if not valid:
            missing_or_empty.append({"paper_id": paper_id, "reason": "empty_or_incomplete", "missing_fields": missing_fields})
            continue
        if not review.get("theme"):
            review["theme"] = _infer_theme(review.get("title", ""), review.get("abstract", ""))
        deep_reviews.append(review)

    if require_fulltext and missing_or_empty:
        return {
            "status": "missing_or_empty_reviews",
            "week": week,
            "message": "Deep-dive reviews are missing, invalid, or incomplete. Fix notes/reviews/*.json and retry.",
            "details": missing_or_empty,
        }

    def theme_for(item: dict[str, Any]) -> str:
        return item.get("theme") or item.get("triage", {}).get("theme") or _infer_theme(item.get("title", ""), item.get("abstract", ""))

    theme_counts: dict[str, int] = {}
    for item in shortlist_items:
        theme = theme_for(item)
        theme_counts[theme] = theme_counts.get(theme, 0) + 1

    journal = {
        "schema": "xrml_weekly_journal_v1",
        "domain_context": resolved_domain_context.to_snapshot(),
        "week": week,
        "run_date": run_date,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "kpi": {
            "triage_items": len(triage),
            "shortlist": len(shortlist_items),
            "deep_dive": len(deep_items),
            "deep_reviews_ready": len(deep_reviews),
            "deep_reviews_expected": len([item for item in deep_items if item.get("paper_id")]),
        },
        "editorial_ko": {
            "this_week_takeaway": (
                f"이번 주 {research_domain.markdown_inline(resolved_domain_context.display_name)}: "
                "Deep Dive 본문 기반 리뷰 준비 필요"
            ),
            "bullets": [
                "Shortlist는 메타데이터 기반으로 빠르게 훑고, Deep Dive는 PDF 본문 기반 리뷰를 반영합니다.",
                "Deep Dive 리뷰가 비어 있으면 저널 PDF 생성은 차단됩니다(품질 게이트).",
            ],
        },
        "themes": [{"theme": key, "count": value} for key, value in sorted(theme_counts.items(), key=lambda pair: pair[1], reverse=True)[:8]],
        "shortlist": [
            {
                "rank": index + 1,
                "score": item.get("score", 0),
                "paper_id": item.get("paper_id", ""),
                "title": item.get("title", ""),
                "date": (item.get("published", "") or "")[:10],
                "theme": theme_for(item),
                "url": item.get("entry_url") or item.get("entry_id", ""),
            }
            for index, item in enumerate(shortlist_items)
        ],
        "paper_of_the_week": deep_reviews[0] if deep_reviews else None,
        "deep_dive": deep_reviews,
        "notable_mentions": [
            {
                "paper_id": item.get("paper_id", ""),
                "title": item.get("title", ""),
                "one_liner_ko": f"[{theme_for(item)}] 제목/초록 기준 관련성 높음(본문 확인 필요).",
            }
            for item in shortlist_items[max(0, deep_dive) : max(0, shortlist)]
        ],
        "quality_gate": {"require_fulltext": bool(require_fulltext), "missing_or_empty_reviews": missing_or_empty},
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{week}.json"
    write_json(out_path, journal)
    return {"status": "ok", "output": str(out_path), "week": week, "missing_or_empty": missing_or_empty}


def fetch_pdfs_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--triage", default="data/queues/triage.jsonl")
    parser.add_argument("--top", type=int, default=12)
    parser.add_argument("--rank-offset", type=int, default=0)
    parser.add_argument("--out-dir", default="data/papers/pdf")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    result = fetch_pdfs(triage_path=Path(args.triage), out_dir=Path(args.out_dir), top_n=args.top, rank_offset=args.rank_offset, overwrite=args.overwrite)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") != "empty" else 2


def extract_pdf_text_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf-dir", default="data/papers/pdf")
    parser.add_argument("--out-dir", default="data/papers/text")
    parser.add_argument("--triage", default="data/queues/triage.jsonl")
    parser.add_argument("--top", type=int, default=3)
    parser.add_argument("--paper", default=None)
    args = parser.parse_args(argv)
    result = extract_pdf_text(pdf_dir=Path(args.pdf_dir), out_dir=Path(args.out_dir), triage_path=Path(args.triage), top_n=args.top, paper_id=args.paper)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "ok" else 2


def review_template_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--triage", default="data/queues/triage.jsonl")
    parser.add_argument("--top", type=int, default=3)
    parser.add_argument("--out-dir", default="notes/reviews")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    result = generate_review_templates(triage_path=Path(args.triage), top_n=args.top, out_dir=Path(args.out_dir), overwrite=args.overwrite)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def compile_journal_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-date", default=None)
    parser.add_argument("--triage", default="data/queues/triage.jsonl")
    parser.add_argument("--shortlist", type=int, default=12)
    parser.add_argument("--deep-dive", type=int, default=3)
    parser.add_argument("--reviews-dir", default="notes/reviews")
    parser.add_argument("--out-dir", default="notes/journal")
    parser.add_argument("--require-fulltext", action="store_true")
    parser.add_argument("--topic", default=None)
    args = parser.parse_args(argv)
    run_date = args.run_date or datetime.now().date().isoformat()
    result = compile_journal(
        run_date=run_date,
        triage_path=Path(args.triage),
        shortlist=args.shortlist,
        deep_dive=args.deep_dive,
        reviews_dir=Path(args.reviews_dir),
        out_dir=Path(args.out_dir),
        require_fulltext=args.require_fulltext,
        domain_context=research_domain.build_research_domain_context(
            run_topic=args.topic,
            source=(
                research_domain.DomainContextSource.RUN_TOPIC
                if args.topic
                else research_domain.DomainContextSource.LEGACY_NEUTRAL
            ),
        ),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "ok" else 2
