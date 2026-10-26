"""Isolated synthetic research vertical slice for Prototype v0.1."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.auth.resource_access import ResourceActor
from app.domain.constants import PROJECT_ROOT
from app.repositories import paper_reviews
from app.repositories.files import iso_now_utc, safe_filename, write_json, write_jsonl
from app.repositories.runs import build_run_paths, list_run_records, load_run_record, save_run_record
from app.schemas.runs import RunCreateRequest
from app.services import paper_review, reporting, topic_profiles
from app.services.prototype_provider import build_recorded_control_bundle

PROTOTYPE_SCHEMA = "prototype_research_vertical_slice_v1"
PROTOTYPE_LABEL = "Prototype v0.1 · synthetic local research"
DEMO_PROFILE_TITLE = "Prototype Spatial Evidence Lab"
DEMO_TOPIC = "Evidence-grounded spatial computing systems"
DEMO_DESCRIPTION = "Synthetic local corpus for the isolated XRML Prototype journey."
REPORT_FILE_NAME = "prototype-weekly-journal.md"
PROVIDER_RECORD_FILE_NAME = "prototype-provider-executions.json"


class PrototypeModeError(RuntimeError):
    code = "prototype_mode_unavailable"


def _canonical_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_exclusive(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            if not content.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _prototype_report_outline(report_markdown: str) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    current_title = "Overview"
    current_lines: list[str] = []

    def append_section() -> None:
        body = "\n".join(current_lines).strip()
        if not body:
            return
        sections.append(
            {
                "section_id": f"prototype-report-section-{len(sections) + 1:02d}",
                "title": current_title,
                "kind": "markdown",
                "body_markdown": body,
                "paper_ids": [],
                "claims": [],
                "provenance": [],
            }
        )

    for raw_line in report_markdown.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("# ") and not sections and not current_lines:
            continue
        if stripped.startswith("## "):
            append_section()
            current_title = stripped[3:].strip() or "Untitled Section"
            current_lines = []
            continue
        current_lines.append(raw_line)
    append_section()
    if not sections:
        raise PrototypeModeError("prototype_report_outline_empty")
    return sections


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def prototype_mode_status() -> dict[str, Any]:
    enabled = os.getenv("XRML_PROTOTYPE_MODE", "").strip() == "1"
    raw_data_root = os.getenv("XRML_DATA_DIR", "").strip()
    reasons: list[str] = []
    data_root: Path | None = None
    if not enabled:
        reasons.append("prototype_mode_disabled")
    if not raw_data_root:
        reasons.append("prototype_data_root_required")
    else:
        data_root = Path(raw_data_root).expanduser().resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
        project_root = PROJECT_ROOT.resolve()
        if not _is_within(data_root, temp_root):
            reasons.append("prototype_data_root_must_be_under_system_temp")
        if _is_within(data_root, project_root) or _is_within(project_root, data_root):
            reasons.append("prototype_data_root_overlaps_repository")
    return {
        "enabled": enabled and not reasons,
        "label": PROTOTYPE_LABEL,
        "isolation_status": "isolated" if enabled and not reasons else "unavailable",
        "reasons": reasons,
        "data_root_configured": data_root is not None,
        "data_root_under_system_temp": data_root is not None and _is_within(data_root, Path(tempfile.gettempdir()).resolve()),
    }


def require_prototype_mode() -> None:
    status = prototype_mode_status()
    if not status["enabled"]:
        raise PrototypeModeError(str((status["reasons"] or [PrototypeModeError.code])[0]))


def _demo_corpus() -> list[dict[str, Any]]:
    return [
        {
            "paper_id": "prototype-paper-1",
            "entry_id": "synthetic:prototype-paper-1",
            "title": "Latency-bounded scene understanding for lightweight mixed-reality headsets",
            "abstract": (
                "A synthetic benchmark studies an on-device scene encoder for mixed reality. "
                "The recorded evaluation reports lower interaction latency while retaining bounded uncertainty labels."
            ),
            "authors": ["Synthetic Research Group A"],
            "published": "2026-01-08",
            "source_provider": "arxiv",
            "source": "prototype_fixture",
            "theme": "Efficient XR systems",
        },
        {
            "paper_id": "prototype-paper-2",
            "entry_id": "synthetic:prototype-paper-2",
            "title": "Evidence-aware foveated rendering under gaze uncertainty",
            "abstract": (
                "A synthetic foveated-rendering study supports the latency finding under controlled gaze noise. "
                "It reports quality gains only inside the evaluated field-of-view and device envelope."
            ),
            "authors": ["Synthetic Research Group B"],
            "published": "2026-01-15",
            "source_provider": "arxiv",
            "source": "prototype_fixture",
            "theme": "Gaze/Foveation",
        },
        {
            "paper_id": "prototype-paper-3",
            "entry_id": "synthetic:prototype-paper-3",
            "title": "Private spatial maps with inspectable provenance",
            "abstract": (
                "A synthetic local-first mapping design links each spatial claim to a bounded evidence record. "
                "The study does not establish multi-device or adversarial robustness."
            ),
            "authors": ["Synthetic Research Group C"],
            "published": "2026-01-22",
            "source_provider": "arxiv",
            "source": "prototype_fixture",
            "theme": "Privacy/Security",
        },
        {
            "paper_id": "prototype-paper-4",
            "entry_id": "synthetic:prototype-paper-4",
            "title": "Reproducible evidence notebooks for spatial-computing teams",
            "abstract": (
                "A synthetic workflow connects paper reviews, evidence spans, reports, and rerun lineage. "
                "The evaluation is a small laboratory fixture and cannot support production-readiness claims."
            ),
            "authors": ["Synthetic Research Group D"],
            "published": "2026-01-29",
            "source_provider": "arxiv",
            "source": "prototype_fixture",
            "theme": "Research workflow",
        },
    ]


def _demo_pages(paper_id: str) -> list[dict[str, Any]]:
    pages = {
        "prototype-paper-1": (
            "In the synthetic device benchmark, the bounded encoder reduced median interaction latency from 48 ms to 31 ms. The result supports an efficiency relationship only for the defined local test envelope.",
            "The benchmark used four generated scenes and no human participants. Generalization to outdoor scenes, unfamiliar sensors, and production workloads remains uncertain.",
        ),
        "prototype-paper-2": (
            "The recorded foveated-rendering control retained the latency improvement when gaze noise stayed below the synthetic threshold. This independently supports the bounded systems finding.",
            "Image quality degraded outside the tested field of view. The fixture does not establish accessibility outcomes or performance for atypical gaze patterns.",
        ),
        "prototype-paper-3": (
            "Each synthetic spatial-map claim carries a source hash and a local evidence reference. The design prevents an unsupported map claim from being promoted without provenance.",
            "No adversarial, multi-user, or multi-node experiment was performed. Privacy benefits are a design property in this fixture, not a field validation result.",
        ),
        "prototype-paper-4": (
            "The synthetic notebook links deterministic reviews and evidence spans to a versioned report and child-run lineage. Reopening the parent leaves its artifacts unchanged.",
            "The exercise is deliberately small and local. It does not measure scale, power-loss recovery, external writers, or production operational readiness.",
        ),
    }
    return [{"page": index, "text": text} for index, text in enumerate(pages[paper_id], start=1)]


def _ensure_demo_profile(actor: ResourceActor) -> dict[str, Any]:
    listing = topic_profiles.list_topic_profiles(actor=actor)
    existing = next(
        (profile for profile in listing["items"] if str(profile.get("title") or "") == DEMO_PROFILE_TITLE),
        None,
    )
    if existing is not None:
        return existing
    return topic_profiles.create_topic_profile(
        {
            "title": DEMO_PROFILE_TITLE,
            "description": DEMO_DESCRIPTION,
            "topic": DEMO_TOPIC,
            "queries": ["spatial computing evidence", "mixed reality latency", "provenance"],
            "categories": ["cs.CV"],
            "max_results": 4,
            "source_provider": "arxiv",
            "top_papers": 4,
            "weekly_top": 4,
            "is_favorite": True,
        },
        actor=actor,
    )


def _seed_run(run_id: str, corpus: list[dict[str, Any]]) -> None:
    paths = build_run_paths(run_id)
    inbox_path = paths.raw_dir / "inbox.jsonl"
    if inbox_path.exists():
        raise FileExistsError("prototype_seed_inbox_already_exists")
    write_jsonl(inbox_path, corpus)
    for paper in corpus:
        paper_id = str(paper["paper_id"])
        pages_path = paths.raw_dir / f"{safe_filename(paper_id)}.pages.json"
        if pages_path.exists():
            raise FileExistsError("prototype_seed_full_text_already_exists")
        write_json(pages_path, {"schema_version": "synthetic_pages_v1", "pages": _demo_pages(paper_id)})


def start_prototype_run(
    *,
    actor: ResourceActor,
    owner_user_id: str | None,
    workspace_context: dict[str, Any] | None = None,
    selected_paper_ids: list[str] | None = None,
) -> dict[str, Any]:
    require_prototype_mode()
    profile = _ensure_demo_profile(actor)
    corpus = _demo_corpus()
    if selected_paper_ids is not None:
        requested_ids = [str(paper_id).strip() for paper_id in selected_paper_ids]
        available_ids = {str(paper["paper_id"]) for paper in corpus}
        if (
            not requested_ids
            or any(not paper_id for paper_id in requested_ids)
            or len(set(requested_ids)) != len(requested_ids)
            or not set(requested_ids).issubset(available_ids)
        ):
            raise PrototypeModeError("prototype_selected_corpus_invalid")
        requested_set = set(requested_ids)
        corpus = [paper for paper in corpus if str(paper["paper_id"]) in requested_set]
    payload = topic_profiles.to_run_request_payload(profile)
    payload.update(
        {
            "max_results": len(corpus),
            "top_papers": len(corpus),
            "weekly_top": len(corpus),
            "skip_scout": True,
            "skip_summary": False,
            "skip_synthesis": False,
            "experiment": False,
        }
    )
    # Imported lazily to avoid a module cycle: runs invokes the Prototype
    # finalizer only after the existing deterministic pipeline has completed.
    from app.services.runs import _enqueue_or_fail, create_run_from_request

    run = create_run_from_request(
        RunCreateRequest.model_validate(payload),
        source="prototype_v0_1:report",
        write_legacy=False,
        owner_user_id=owner_user_id,
        topic_profile_id=str(profile["profile_id"]),
    )
    _seed_run(run["run_id"], corpus)
    run = load_run_record(run["run_id"])
    run["prototype"] = {
        "schema_version": PROTOTYPE_SCHEMA,
        "label": PROTOTYPE_LABEL,
        "corpus_kind": "synthetic",
        "corpus_count": len(corpus),
        "pipeline_status": "queued",
        "report_status": "pending",
        "provider": {
            "stage": "report_draft_assist",
            "output_class": "report_draft",
            "route_status": "deterministic_local_fallback",
            "fallback_used": True,
            "live_provider_invoked": False,
            "accepted_control": "pending",
            "rejection_controls": "pending",
        },
    }
    if workspace_context is not None:
        run["workspace"] = deepcopy(workspace_context)
    run.setdefault("counts", {})["synthetic_corpus_items"] = len(corpus)
    save_run_record(run)
    _enqueue_or_fail(run["run_id"])
    return prototype_run_summary(load_run_record(run["run_id"]))


def _materialize_workbench_review(run_id: str, review: dict[str, Any], theme: str) -> None:
    paths = build_run_paths(run_id)
    spans = review.get("evidence_spans") if isinstance(review.get("evidence_spans"), list) else []
    evidence = [
        {
            "page": span.get("page_number"),
            "section": str(span.get("label") or "synthetic full text"),
            "reference": f"{review['paper_id']}-evidence-{index}",
            "note": str(span.get("text") or ""),
            "summary": "Synthetic full-text evidence span.",
            "source_type": "full_text",
            "quality_hint": "synthetic_local_full_text",
            "confidence": 0.9,
            "artifact_path": review.get("full_text_artifact_path"),
        }
        for index, span in enumerate(spans, start=1)
    ]
    findings = [str(item) for item in review.get("key_findings") or []]
    while len(findings) < 2:
        findings.append("The synthetic record keeps its finding bounded to available evidence.")
    limitations = [str(item) for item in review.get("limitations") or []]
    payload = {
        "paper_id": review["paper_id"],
        "title": review["title"],
        "theme": theme,
        "read_level": "SYNTHETIC_FULL_TEXT_REVIEWED",
        "review_ko": {
            "one_liner": str(review.get("why_it_matters") or findings[0]),
            "why_it_matters": [
                str(review.get("why_it_matters") or findings[0]),
                "It demonstrates evidence-to-report provenance in the local Prototype.",
            ],
            "key_contributions": findings[:2],
            "method": {
                "problem": str(review.get("method_summary") or "Synthetic evidence-grounded system evaluation."),
                "inputs_outputs": "Synthetic full-text pages to bounded review evidence.",
                "model": "deterministic local review",
                "training": "No training or external model invocation.",
                "inference": "Deterministic extraction from local synthetic text.",
                "system_constraints": ["Local-only", "Synthetic corpus"],
            },
            "experiments": {
                "datasets": ["Generated Prototype fixture"],
                "metrics": ["Evidence coverage"],
                "baselines": ["Deterministic local pipeline"],
                "key_results": [findings[0]],
                "runtime_latency": "Not evaluated as a product benchmark.",
                "hardware": "Not applicable to the synthetic control.",
            },
            "xr_takeaways": [findings[0]],
            "limitations": limitations or ["Synthetic evidence only."],
            "reproducibility": {
                "code": "Repository Prototype workflow",
                "weights": "None",
                "license": "Synthetic fixture",
                "notes": "No external corpus or Provider output used.",
            },
            "lab_actions": [
                "Inspect the cited evidence spans.",
                "Compare a child rerun without changing the parent artifact.",
            ],
            "confidence": 0.9,
            "evidence": evidence,
        },
        "generated_at_utc": iso_now_utc(),
    }
    target = paths.reviews_dir / f"{safe_filename(str(review['paper_id']))}.json"
    _write_exclusive(target, _canonical_text(payload))


def finalize_prototype_outputs(run_id: str) -> dict[str, Any]:
    """Create reviews, grounded report, and safe Provider records for one tagged run."""

    require_prototype_mode()
    run = load_run_record(run_id)
    prototype = run.get("prototype") if isinstance(run.get("prototype"), dict) else {}
    if prototype.get("schema_version") != PROTOTYPE_SCHEMA:
        raise PrototypeModeError("prototype_run_tag_missing")
    profile = run.get("profile") if isinstance(run.get("profile"), dict) else {}
    topic_id = str(run.get("topic_profile_id") or "").strip()
    if not topic_id:
        raise PrototypeModeError("prototype_topic_profile_missing")

    paper_review.refresh_reviews_for_run(
        run_id,
        topic_profile={**profile, "profile_id": topic_id, "title": run.get("topic")},
        use_full_text=True,
        prefer_existing_text=False,
        extract_existing_pdf=False,
    )
    reviews = paper_reviews.list_reviews(topic_id=topic_id, run_id=run_id, limit=20, read_only=True)
    if not reviews or any(str(review.get("full_text_status")) != "reviewed" for review in reviews):
        raise PrototypeModeError("prototype_full_text_review_incomplete")
    if any(len(review.get("evidence_spans") or []) < 2 for review in reviews):
        raise PrototypeModeError("prototype_evidence_span_coverage_incomplete")

    triage_path = build_run_paths(run_id).triage_dir / "triage.jsonl"
    triage_items = []
    if triage_path.is_file():
        from app.repositories.files import read_jsonl

        triage_items = read_jsonl(triage_path)
    themes = {str(item.get("paper_id")): str(item.get("theme") or "Synthetic research") for item in triage_items}
    for review in reviews:
        _materialize_workbench_review(run_id, review, themes.get(str(review["paper_id"]), "Synthetic research"))

    provider_bundle = build_recorded_control_bundle(
        run_id=run_id,
        topic_id=topic_id,
        topic_name=str(run.get("topic") or DEMO_TOPIC),
        reviews=reviews,
    )
    provider_path = build_run_paths(run_id).reports_dir / PROVIDER_RECORD_FILE_NAME
    _write_exclusive(provider_path, _canonical_text(provider_bundle))

    weekly = reporting.generate_weekly_journal_report_from_reviews(
        topic_profile={**profile, "profile_id": topic_id, "title": run.get("topic")},
        topic_id=topic_id,
        run_id=run_id,
        topic_relevant_unique_count=len(reviews),
        read_only=False,
    )
    if weekly.get("status") != "ok" or not str(weekly.get("markdown") or "").strip():
        raise PrototypeModeError("prototype_weekly_report_unavailable")
    provider_note = (
        "\n\n## Provider execution posture\n\n"
        "- Route: deterministic local fallback.\n"
        "- Live Provider invoked: no.\n"
        "- Recorded accepted control: passed.\n"
        "- Malformed, mismatched, unknown-evidence, and failure controls: rejected.\n"
        "- Provider control text was not inserted into this trusted report.\n"
    )
    report_paths = build_run_paths(run_id)
    report_path = report_paths.reports_dir / REPORT_FILE_NAME
    report_content = str(weekly["markdown"]).rstrip() + provider_note
    _write_exclusive(report_path, report_content)
    if (report_paths.reports_dir / reporting.OUTLINE_FILENAME).exists() or (
        report_paths.reports_dir / reporting.PREVIEW_FILENAME
    ).exists():
        raise PrototypeModeError("prototype_report_preview_collision")
    reporting.save_outline(run_id, _prototype_report_outline(report_content))

    run = load_run_record(run_id)
    evidence_count = sum(len(review.get("evidence_spans") or []) for review in reviews)
    evidence_manifest = [
        {
            "paper_id": str(review.get("paper_id") or ""),
            "source_text_sha256": str(review.get("source_text_sha256") or ""),
            "spans": [
                {
                    "span_id": str(span.get("span_id") or span.get("label") or ""),
                    "page_number": span.get("page_number"),
                    "char_start": span.get("char_start"),
                    "char_end": span.get("char_end"),
                    "text_sha256": hashlib.sha256(str(span.get("text") or "").encode("utf-8")).hexdigest(),
                }
                for span in sorted(
                    review.get("evidence_spans") or [],
                    key=lambda item: (
                        int(item.get("page_number") or 0),
                        int(item.get("char_start") or 0),
                        str(item.get("span_id") or item.get("label") or ""),
                    ),
                )
            ],
        }
        for review in sorted(reviews, key=lambda item: str(item.get("paper_id") or ""))
    ]
    evidence_set_digest = hashlib.sha256(_canonical_text(evidence_manifest).encode("utf-8")).hexdigest()
    run.setdefault("counts", {}).update(
        {
            "reviewed_items": len(reviews),
            "full_text_reviewed_items": len(reviews),
            "evidence_spans": evidence_count,
        }
    )
    run.setdefault("artifacts", {})["report_markdown"] = str(report_path)
    run["artifacts"]["prototype_provider_execution_record"] = str(provider_path)
    if isinstance(run.get("workspace"), dict) and run["workspace"].get("snapshot_id"):
        run["workspace"] = {
            **run["workspace"],
            "evidence_set_digest": evidence_set_digest,
        }
    run["prototype"] = {
        **prototype,
        "pipeline_status": "completed",
        "report_status": "ready",
        "reviewed_paper_count": len(reviews),
        "evidence_span_count": evidence_count,
        "report_quality": weekly.get("quality") or {},
        "provider": {
            "provider_id": provider_bundle["provider_id"],
            "surface_id": provider_bundle["surface_id"],
            "stage": provider_bundle["stage"],
            "output_class": provider_bundle["output_class"],
            "route_status": "deterministic_local_fallback",
            "fallback_used": True,
            "fallback_reason": provider_bundle["fallback_decision"]["fallback_reason"],
            "live_provider_invoked": False,
            "accepted_control": "passed" if provider_bundle["accepted_control_passed"] else "failed",
            "rejection_controls": "passed" if provider_bundle["rejection_controls_passed"] else "failed",
            "control_total": provider_bundle["control_total"],
            "control_passed": provider_bundle["control_passed"],
            "record_digest": provider_bundle["record_digest"],
        },
    }
    save_run_record(run)
    return run["prototype"]


def prototype_run_summary(run: dict[str, Any]) -> dict[str, Any]:
    prototype = run.get("prototype") if isinstance(run.get("prototype"), dict) else {}
    provider = prototype.get("provider") if isinstance(prototype.get("provider"), dict) else {}
    return {
        "run_id": str(run.get("run_id") or ""),
        "topic_profile_id": run.get("topic_profile_id"),
        "topic": str(run.get("topic") or ""),
        "status": str(run.get("status") or "unknown"),
        "created_at": run.get("created_at"),
        "finished_at": run.get("finished_at"),
        "lineage": run.get("lineage") or {},
        "counts": {
            "papers": int((run.get("counts") or {}).get("triage_items") or prototype.get("corpus_count") or 0),
            "reviewed": int((run.get("counts") or {}).get("reviewed_items") or 0),
            "evidence_spans": int((run.get("counts") or {}).get("evidence_spans") or 0),
        },
        "report_status": prototype.get("report_status") or "pending",
        "provider": provider,
        "report_quality": run.get("report_quality") or prototype.get("report_quality") or {},
    }


def get_prototype_status(*, actor: ResourceActor | None = None) -> dict[str, Any]:
    from app.services.prototype_live_provider import describe_live_availability

    mode = prototype_mode_status()
    profile_id = None
    if mode["enabled"] and actor is not None:
        listing = topic_profiles.list_topic_profiles(actor=actor)
        profile = next(
            (item for item in listing["items"] if str(item.get("title") or "") == DEMO_PROFILE_TITLE),
            None,
        )
        profile_id = profile.get("profile_id") if profile else None
    runs = [
        run
        for run in list_run_records(limit=None)
        if isinstance(run.get("prototype"), dict) and run["prototype"].get("schema_version") == PROTOTYPE_SCHEMA
    ]
    latest = prototype_run_summary(runs[0]) if runs else None
    return {
        **mode,
        "profile_id": profile_id,
        "topic": DEMO_TOPIC,
        "latest_run": latest,
        "can_start": bool(mode["enabled"]),
        "live_provider_proof": describe_live_availability(),
    }
