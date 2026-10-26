from __future__ import annotations

import logging
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.domain.constants import LEGACY_NOTES_ROOT, LEGACY_QUEUE_ROOT, STEP_ORDER
from app.repositories.artifact_refs import normalize_path_to_ref, normalize_payload_references
from app.repositories.files import append_jsonl, copy_file, iso_now_utc, read_jsonl, safe_filename, write_jsonl, write_text
from app.repositories.jsonl import JsonlIntegrityError
from app.repositories.runs import build_run_paths, create_run_record, list_run_records, load_run_record, save_run_record
from app.repositories import metadata_index
from app.schemas.compare import RunRerunRequest
from app.schemas.runs import RunCreateRequest
from app.services import (
    experiments,
    notes,
    ranking,
    report_versions,
    reporting,
    research_domain,
    research_plan,
    retrieval,
    synthesis,
    topic_notifications,
    workbench,
)
from app.services.profile import build_run_profile, load_profile_snapshot, snapshot_profile

def _enqueue_or_fail(run_id: str) -> None:
    """Durably enqueue a created run, or mark it failed truthfully.

    The pipeline is never started here: only the managed run-executor worker
    may invoke it. If the durable enqueue fails we do not report success.
    """
    from app.services import run_executor

    try:
        run_executor.enqueue_run(run_id)
    except Exception:
        try:
            run = load_run_record(run_id)
            run["status"] = "failed"
            run["error"] = "executor_enqueue_failed"
            save_run_record(run)
        except Exception:
            pass
        raise


def execute_pipeline_for_worker(run_id: str) -> None:
    """Worker-only pipeline entrypoint. Not for request handlers."""
    _execute_pipeline(run_id)
REPORT_TASK_TYPES = {"weekly_report", "monthly_report"}
REPORT_REQUEST_INTENT_VALUES = {"report", "research_report", "weekly_report", "monthly_report"}
REPORT_ARTIFACT_UNAVAILABLE_ERROR = "보고서 산출물을 찾을 수 없어 완료 처리하지 않았습니다."
PIPELINE_EXECUTION_FAILED_ERROR = "pipeline_execution_failed"
PROTOTYPE_SCHEMA_VERSION = "prototype_research_vertical_slice_v1"
logger = logging.getLogger("app.runs")


def _append_event(run_id: str, event_type: str, payload: dict[str, Any]) -> None:
    event = {
        "run_id": run_id,
        "type": event_type,
        "timestamp": iso_now_utc(),
        "payload": payload,
    }
    append_jsonl(build_run_paths(run_id).events_path, [event])


def _upsert_step(run: dict[str, Any], step_name: str, **fields: Any) -> None:
    steps = run.setdefault("steps", [])
    existing = next((step for step in steps if step.get("name") == step_name), None)
    if existing is None:
        existing = {"name": step_name}
        steps.append(existing)
        steps.sort(key=lambda step: STEP_ORDER.index(step["name"]) if step["name"] in STEP_ORDER else len(STEP_ORDER))
    existing.update(fields)


def _write_daily_report(run: dict[str, Any]) -> str:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = LEGACY_NOTES_ROOT / "daily" / f"{day}.md"
    lines: list[str] = []
    lines.append(f"# Daily Pipeline Run — {day} (UTC)\n")
    lines.append(f"- Generated at: {run.get('updated_at')}\n")
    lines.append("")
    lines.append("## Step results\n")
    for step in run.get("steps", []):
        status = step.get("status", "pending")
        if status == experiments.SCAFFOLD_GENERATION_STATUS:
            label = "🧭 proposal scaffold created (not executed)"
        elif status == "not_generated":
            label = "➖ proposal scaffold not generated"
        else:
            label = "✅ ok" if status in {"completed", "skipped", "completed_with_warnings"} else "⚠️ failed"
        lines.append(f"- **{step['name'].title()}**: {label}")
        if step.get("output_hint"):
            lines.append(f"  - output: {step['output_hint']}")
        if step.get("note"):
            lines.append(f"  - note: {step['note']}")
    lines.append("")
    lines.append("## Next actions\n")
    lines.append("- Open `data/queues/triage.md` to review the shortlist.")
    lines.append("- Review generated notes in `notes/papers/` and weekly brief in `notes/weekly/`.")
    scaffold = run.get("artifacts", {}).get("experiment_scaffold")
    if isinstance(scaffold, dict):
        lines.append(
            "- Review the non-executable experiment design proposal "
            f"(`{scaffold.get('artifact_ref') or 'artifact reference unavailable'}`); "
            "execution status remains `not_executed`."
        )
    lines.append("")
    write_text(out_path, "\n".join(lines))
    return str(out_path)


def _mark_run_started(run_id: str) -> dict[str, Any]:
    run = load_run_record(run_id)
    run["status"] = "running"
    run["started_at"] = iso_now_utc()
    save_run_record(run)
    _append_event(run_id, "run.started", {"status": "running"})
    return run


def _mark_step_started(run_id: str, step_name: str) -> None:
    run = load_run_record(run_id)
    _upsert_step(run, step_name, status="running", started_at=iso_now_utc())
    save_run_record(run)
    _append_event(run_id, "step.started", {"step": step_name})


def _record_legacy_mirror_warnings(run_id: str, step_name: str, warnings: list[str] | None) -> None:
    entries = [warning for warning in (warnings or []) if warning]
    if not entries:
        return
    run = load_run_record(run_id)
    run.setdefault("warnings", []).extend(f"[{step_name}] {warning}" for warning in entries)
    step = next((item for item in run.get("steps", []) if item.get("name") == step_name), None)
    note = "Legacy mirror write failed; run-scoped artifacts remain authoritative."
    if step is not None:
        existing_note = step.get("note")
        step["note"] = f"{existing_note}; {note}" if existing_note else note
    save_run_record(run)
    _append_event(run_id, "step.warning", {"step": step_name, "warnings": entries})


def _request_indicates_report(request: dict[str, Any]) -> bool:
    for field in ("kind", "task_type"):
        value = str(request.get(field) or "").strip().lower()
        if value in REPORT_REQUEST_INTENT_VALUES or value.endswith("_report"):
            return True
    return False


def _run_has_report_intent(run: dict[str, Any]) -> bool:
    source = str(run.get("source") or "").strip().lower()
    if source.endswith(":report"):
        return True
    schedule_context = run.get("schedule_context") if isinstance(run.get("schedule_context"), dict) else {}
    task_type = str(schedule_context.get("task_type") or "").strip().lower()
    if task_type in REPORT_TASK_TYPES:
        return True
    request = run.get("request") if isinstance(run.get("request"), dict) else {}
    return _request_indicates_report(request)


def _existing_report_artifact_path(run: dict[str, Any]) -> str | None:
    artifacts = run.get("artifacts") if isinstance(run.get("artifacts"), dict) else {}
    prototype = run.get("prototype") if isinstance(run.get("prototype"), dict) else {}
    fields = (
        ("report_markdown", "research_plan_markdown")
        if prototype.get("schema_version") == PROTOTYPE_SCHEMA_VERSION
        else ("research_plan_markdown", "report_markdown")
    )
    for field in fields:
        raw_path = str(artifacts.get(field) or "").strip()
        if raw_path and Path(raw_path).is_file():
            return raw_path

    reports_dir = build_run_paths(str(run.get("run_id") or "")).reports_dir
    if not reports_dir.exists():
        return None
    candidates = sorted(
        [
            path
            for path in reports_dir.glob("*.md")
            if path.name != "preview.md" and not path.name.startswith("exported_")
        ]
    )
    return str(candidates[-1]) if candidates else None


def _finalize_run(run_id: str, *, status: str, error: str | None = None) -> dict[str, Any]:
    run = load_run_record(run_id)
    jsonl_integrity_failed = False
    if status == "completed":
        try:
            reporting.ensure_compare_ready_metadata(run_id)
            run = load_run_record(run_id)
        except JsonlIntegrityError as exc:
            jsonl_integrity_failed = True
            status = "failed"
            error = exc.code
            run.setdefault("warnings", []).append(f"[jsonl_integrity] {exc.code}")
        except Exception:
            run.setdefault("warnings", []).append("[compare_ready] compare_ready_metadata_failed")
    if status == "completed" and _run_has_report_intent(run) and not _existing_report_artifact_path(run):
        status = "failed"
        error = error or REPORT_ARTIFACT_UNAVAILABLE_ERROR
        run.setdefault("warnings", []).append("[report_artifact] completed report intent run did not produce a report markdown artifact.")
    run["status"] = status
    run["finished_at"] = iso_now_utc()
    run["error"] = error
    if run.get("write_legacy", True) and not jsonl_integrity_failed:
        run.setdefault("artifacts", {})["daily_report"] = _write_daily_report(run)
    save_run_record(run)
    if status == "completed":
        try:
            report_versions.snapshot_completed_report_if_available(run_id)
            run = load_run_record(run_id)
        except JsonlIntegrityError as exc:
            jsonl_integrity_failed = True
            status = "failed"
            error = exc.code
            run = load_run_record(run_id)
            run["status"] = status
            run["error"] = error
            run.setdefault("warnings", []).append(f"[jsonl_integrity] {exc.code}")
            save_run_record(run)
        except Exception:
            run = load_run_record(run_id)
            run.setdefault("warnings", []).append("[report_versions] report_version_snapshot_failed")
            save_run_record(run)
    if status == "completed":
        try:
            report_markdown_path = _existing_report_artifact_path(run)
            if report_markdown_path:
                report_markdown = Path(report_markdown_path).read_text(encoding="utf-8", errors="ignore")
                run["report_quality"] = reporting.evaluate_report_quality_for_run(
                    run_id, report_markdown=report_markdown, evaluated_at=run.get("finished_at")
                )
                save_run_record(run)
        except JsonlIntegrityError as exc:
            jsonl_integrity_failed = True
            status = "failed"
            error = exc.code
            run = load_run_record(run_id)
            run["status"] = status
            run["error"] = error
            run.setdefault("warnings", []).append(f"[jsonl_integrity] {exc.code}")
            save_run_record(run)
        except Exception:
            run = load_run_record(run_id)
            run.setdefault("warnings", []).append("[report_quality] report_quality_evaluation_failed")
            save_run_record(run)
    metadata_index.refresh_run(run_id, reason="run_finalize")
    if not jsonl_integrity_failed:
        try:
            from app.services import topic_analytics

            topic_analytics.refresh_topic_analytics_for_run(run)
        except JsonlIntegrityError as exc:
            jsonl_integrity_failed = True
            status = "failed"
            error = exc.code
            run = load_run_record(run_id)
            run["status"] = status
            run["error"] = error
            run.setdefault("warnings", []).append(f"[jsonl_integrity] {exc.code}")
            save_run_record(run)
        except Exception:
            run = load_run_record(run_id)
            run.setdefault("warnings", []).append("[topic_analytics] topic_analytics_refresh_failed")
            save_run_record(run)
    try:
        topic_notifications.emit_notifications_for_run(run)
    except Exception:
        run.setdefault("warnings", []).append("[notifications] notification_dispatch_failed")
        save_run_record(run)
    _append_event(run_id, "run.finished", {"status": status, "error": error})
    return run


def _copy_legacy_inbox_to_run(run_id: str) -> str | None:
    legacy_inbox = LEGACY_QUEUE_ROOT / "inbox.jsonl"
    if not legacy_inbox.exists():
        return None
    run_paths = build_run_paths(run_id)
    target = run_paths.raw_dir / "inbox.jsonl"
    copy_file(legacy_inbox, target)
    return str(target)


def _run_inbox_has_items(run_id: str) -> bool:
    inbox_path = build_run_paths(run_id).raw_dir / "inbox.jsonl"
    return bool(read_jsonl(inbox_path))


def _generate_experiment_scaffold_for_run(
    *,
    run_id: str,
    paths: Any,
    request: dict[str, Any],
    domain_context: research_domain.ResearchDomainContext,
) -> None:
    """Generate the run-scoped planning artifact without any execution dispatch."""

    _mark_step_started(run_id, "experiment")
    experiment_result = experiments.create_experiment_scaffold(
        source_path=paths.triage_dir / "triage.jsonl",
        rank=request.get("rank", 1),
        out_root=paths.run_dir / "experiments",
        domain_context=domain_context,
    )
    run = load_run_record(run_id)
    generation_status = experiment_result.get("generation_status") or "not_generated"
    experiment_dir = experiment_result.get("experiment_dir")
    artifact_ref = normalize_path_to_ref(experiment_dir, run_id=run_id)
    if experiment_dir and isinstance(experiment_result.get("artifact"), dict):
        stored_contract = deepcopy(experiment_result["artifact"])
        stored_contract["artifact_ref"] = artifact_ref
        run["artifacts"]["experiment_dir"] = experiment_dir
        run["artifacts"]["experiment_scaffold"] = stored_contract
    _upsert_step(
        run,
        "experiment",
        status=generation_status,
        output_hint=artifact_ref,
        note=(
            "Non-executable research design proposal created; execution status is not_executed."
            if generation_status == experiments.SCAFFOLD_GENERATION_STATUS
            else "No proposal scaffold was generated because the selected source set was empty."
        ),
        execution_status=experiments.SCAFFOLD_EXECUTION_STATUS,
        executable=False,
        runner_available=False,
    )
    save_run_record(run)
    _append_event(
        run_id,
        (
            "experiment.scaffold_created"
            if generation_status == experiments.SCAFFOLD_GENERATION_STATUS
            else "experiment.scaffold_not_generated"
        ),
        {
            "step": "experiment",
            "generation_status": generation_status,
            "execution_status": experiments.SCAFFOLD_EXECUTION_STATUS,
            "executable": False,
            "runner_available": False,
            "artifact_ref": artifact_ref,
        },
    )


def _merge_request_payload(base_request: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    merged = deepcopy(base_request)
    for key, value in (overrides or {}).items():
        if value is None:
            continue
        merged[key] = value
    return merged


def _load_parent_profile_snapshot(parent_run: dict[str, Any]) -> dict[str, Any]:
    profile_path = Path(parent_run.get("artifacts", {}).get("profile", {}).get("profile") or "")
    if profile_path.exists():
        return load_profile_snapshot(profile_path)
    return deepcopy(parent_run.get("profile") or {})


def _execution_plan_for_rerun(mode: str, *, skip_retrieval_when_branching: bool) -> list[str]:
    if mode == "full_rerun_from_profile":
        return [
            "Reuse the saved profile snapshot from the parent run.",
            "Run scout, triage, summary, synthesis, and optionally generate a non-executable experiment proposal scaffold in a new child run.",
        ]
    if mode == "rerun_with_profile_overrides":
        return [
            "Reuse the saved profile snapshot from the parent run as the baseline.",
            "Apply profile overrides to the child run only.",
            "Run scout, triage, summary, synthesis, and optionally generate a non-executable experiment proposal scaffold using the overridden profile.",
        ]
    if mode == "branch_from_selected_papers" and skip_retrieval_when_branching:
        return [
            "Create a child run from selected parent papers only.",
            "Skip retrieval and seed the child run inbox from the parent run selection.",
            "Run triage, summary, synthesis, and optionally generate a non-executable experiment proposal scaffold using the seeded selection.",
        ]
    return [
        "Create a child run linked to the parent run and selected papers.",
        "Retrieval is still enabled for this rerun, so the child run may include newly fetched papers.",
        "Run scout, triage, summary, synthesis, and optionally generate a non-executable experiment proposal scaffold using the child run profile.",
    ]


def _seed_selected_papers(parent_run_id: str, child_run_id: str, selected_paper_ids: list[str]) -> str:
    parent_items = workbench.load_run_triage_items(parent_run_id)
    selected_ids = set(selected_paper_ids)
    selected = [item for item in parent_items if item.get("paper_id") in selected_ids]
    if not selected:
        raise FileNotFoundError("Selected papers were not found in the parent run.")
    found_ids = {str(item.get("paper_id")) for item in selected}
    missing_ids = sorted(selected_ids - found_ids)
    if missing_ids:
        raise FileNotFoundError(f"Selected papers were not found in the parent run: {', '.join(missing_ids)}")

    inbox_path = build_run_paths(child_run_id).raw_dir / "inbox.jsonl"
    write_jsonl(inbox_path, selected)

    parent_paths = build_run_paths(parent_run_id)
    child_paths = build_run_paths(child_run_id)
    copied_full_text = 0
    for paper_id in sorted(found_ids):
        file_name = safe_filename(paper_id)
        for suffix in (".txt", ".pages.json"):
            source = parent_paths.raw_dir / f"{file_name}{suffix}"
            if not source.is_file():
                continue
            target = child_paths.raw_dir / source.name
            if target.exists():
                raise FileExistsError("Child full-text seed artifact already exists.")
            copy_file(source, target)
            copied_full_text += 1

    child_run = load_run_record(child_run_id)
    child_run.setdefault("artifacts", {})["seed_inbox_jsonl"] = str(inbox_path)
    child_run.setdefault("counts", {})["seeded_papers"] = len(selected)
    child_run["counts"]["seeded_full_text_artifacts"] = copied_full_text
    save_run_record(child_run)
    _append_event(child_run_id, "run.seeded", {"parent_run_id": parent_run_id, "selected_paper_ids": selected_paper_ids})
    return str(inbox_path)


def _validate_selected_papers(parent_run_id: str, selected_paper_ids: list[str]) -> None:
    parent_items = workbench.load_run_triage_items(parent_run_id)
    available_ids = {str(item.get("paper_id")) for item in parent_items if item.get("paper_id")}
    missing_ids = sorted(set(selected_paper_ids) - available_ids)
    if missing_ids:
        raise RuntimeError(f"Selected papers were not found in the parent run: {', '.join(missing_ids)}")


def create_run_from_request(
    request: RunCreateRequest,
    *,
    source: str = "api",
    write_legacy: bool = True,
    base_profile: dict[str, Any] | None = None,
    lineage: dict[str, Any] | None = None,
    owner_user_id: str | None = None,
    topic_profile_id: str | None = None,
    schedule_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = request.model_dump()
    profile = build_run_profile(payload, base_profile=base_profile)
    run = create_run_record(
        topic=profile["topic"],
        request_payload=payload,
        source=source,
        write_legacy=write_legacy,
        lineage=lineage,
        owner_user_id=owner_user_id,
    )
    paths = build_run_paths(run["run_id"])
    snapshots = snapshot_profile(paths.inputs_dir, profile)
    run["profile"] = profile
    run["domain_context"] = deepcopy(profile["domain_context"])
    run["artifacts"]["profile"] = snapshots
    run["legacy_compatibility"] = {
        "authoritative_root": str(paths.run_dir),
        "legacy_views": {
            "triage": "data/queues/triage.{jsonl,md}",
            "notes": "notes/papers/*.md",
            "weekly": "notes/weekly/*.md",
            "daily": "notes/daily/*.md",
        },
    }
    run["topic_profile_id"] = str(topic_profile_id).strip() or None if topic_profile_id is not None else None
    run["schedule_context"] = {
        key: value
        for key, value in dict(schedule_context or {}).items()
        if value is not None
    }
    save_run_record(run)
    metadata_index.refresh_run(run["run_id"], reason="run_create")
    _append_event(run["run_id"], "run.created", {"topic": profile["topic"], "source": source})
    return run


def attach_run_context(
    run_id: str,
    *,
    topic_profile_id: str | None = None,
    schedule_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    try:
        run = load_run_record(run_id)
    except FileNotFoundError:
        return None

    if topic_profile_id is not None:
        resolved_profile_id = str(topic_profile_id).strip()
        run["topic_profile_id"] = resolved_profile_id or None
    if schedule_context is not None:
        run["schedule_context"] = {
            key: value
            for key, value in dict(schedule_context).items()
            if value is not None
        }
    save_run_record(run)
    return load_run_record(run_id)


def _execute_pipeline(run_id: str) -> None:
    run = _mark_run_started(run_id)
    paths = build_run_paths(run_id)
    profile = run["profile"]
    request = run["request"]
    domain_context = research_domain.research_domain_context_for_run(run)

    try:
        if request.get("skip_scout"):
            existing_inbox = paths.raw_dir / "inbox.jsonl"
            if existing_inbox.exists() and read_jsonl(existing_inbox):
                fallback_path = str(existing_inbox)
                scout_note = "Skipped via request. Using pre-seeded run inbox."
            else:
                fallback_path = _copy_legacy_inbox_to_run(run_id)
                scout_note = "Skipped via request."
            run = load_run_record(run_id)
            _upsert_step(run, "scout", status="skipped", note=scout_note, output_hint=fallback_path)
            save_run_record(run)
            _append_event(run_id, "step.skipped", {"step": "scout", "output_hint": fallback_path, "note": scout_note})
        else:
            _mark_step_started(run_id, "scout")
            result = retrieval.retrieve_from_profile(
                profile=profile,
                raw_path=paths.raw_dir / f"sources_{datetime.now(timezone.utc).strftime('%Y%m%d')}.jsonl",
                inbox_path=paths.raw_dir / "inbox.jsonl",
                sleep_seconds=0.5,
                mirror_legacy=run.get("write_legacy", True),
            )
            run = load_run_record(run_id)
            run["counts"]["new_papers"] = result.get("new_papers", 0)
            run["artifacts"]["raw_file"] = result.get("raw_file")
            run["artifacts"]["inbox_file"] = result.get("inbox_file")
            _upsert_step(
                run,
                "scout",
                status="completed" if result.get("status") == "ok" else "completed_with_warnings",
                output_hint=result.get("raw_file"),
                note="; ".join(failure["error"] for failure in result.get("failed_queries", [])) or None,
            )
            save_run_record(run)
            _append_event(run_id, "step.completed", {"step": "scout", "result": result})
            _record_legacy_mirror_warnings(run_id, "scout", result.get("legacy_mirror_errors"))

            if not _run_inbox_has_items(run_id):
                fallback_path = _copy_legacy_inbox_to_run(run_id)
                if fallback_path:
                    run = load_run_record(run_id)
                    scout_step = next((step for step in run.get("steps", []) if step.get("name") == "scout"), None)
                    if scout_step is not None:
                        note = scout_step.get("note") or ""
                        extra = "No new run-scoped inbox items. Fell back to legacy inbox."
                        scout_step["note"] = f"{note}; {extra}" if note else extra
                    save_run_record(run)
                    _append_event(run_id, "step.info", {"step": "scout", "fallback_inbox": fallback_path})

        triage_input = paths.raw_dir / "inbox.jsonl"
        if not triage_input.exists():
            fallback = _copy_legacy_inbox_to_run(run_id)
            if fallback:
                triage_input = Path(fallback)

        _mark_step_started(run_id, "triage")
        triage_result = ranking.rank_from_paths(
            input_path=triage_input,
            keywords_path=Path(run["artifacts"]["profile"]["keywords"]),
            output_jsonl=paths.triage_dir / "triage.jsonl",
            output_md=paths.triage_dir / "triage.md",
            mirror_legacy=run.get("write_legacy", True),
            topic_profile=profile,
        )
        run = load_run_record(run_id)
        run["counts"]["triage_items"] = triage_result["count"]
        run["artifacts"]["triage_jsonl"] = triage_result["output_jsonl"]
        run["artifacts"]["triage_md"] = triage_result["output_md"]
        _upsert_step(run, "triage", status="completed", output_hint=triage_result["output_md"])
        save_run_record(run)
        _append_event(run_id, "step.completed", {"step": "triage", "count": triage_result["count"]})
        _record_legacy_mirror_warnings(run_id, "triage", triage_result.get("legacy_mirror_errors"))

        if request.get("skip_summary"):
            run = load_run_record(run_id)
            _upsert_step(run, "summary", status="skipped", note="Skipped via request.")
            save_run_record(run)
            _append_event(run_id, "step.skipped", {"step": "summary"})
        else:
            _mark_step_started(run_id, "summary")
            summary_result = notes.summarize_from_paths(
                triage_path=paths.triage_dir / "triage.jsonl",
                inbox_path=triage_input,
                template_path=Path("templates/paper_summary_template.md"),
                out_dir=paths.notes_dir / "papers",
                top_n=request.get("top_papers", 3),
                mirror_legacy=run.get("write_legacy", True),
                domain_context=domain_context,
                source_label="run:triage/triage.jsonl",
            )
            run = load_run_record(run_id)
            run["artifacts"]["paper_notes"] = summary_result.get("created", [])
            _upsert_step(
                run,
                "summary",
                status="completed" if summary_result.get("status") == "ok" else "skipped",
                output_hint=(summary_result.get("created") or [str(paths.notes_dir / "papers")])[0],
                note=summary_result.get("message"),
            )
            save_run_record(run)
            _append_event(run_id, "step.completed", {"step": "summary", "created": len(summary_result.get("created", []))})
            _record_legacy_mirror_warnings(run_id, "summary", summary_result.get("legacy_mirror_errors"))

        if request.get("skip_synthesis"):
            run = load_run_record(run_id)
            _upsert_step(run, "synthesis", status="skipped", note="Skipped via request.")
            save_run_record(run)
            _append_event(run_id, "step.skipped", {"step": "synthesis"})
        else:
            _mark_step_started(run_id, "synthesis")
            week_tag = synthesis.iso_week_tag(datetime.now(timezone.utc))
            synthesis_result = synthesis.synthesize_weekly(
                triage_path=paths.triage_dir / "triage.jsonl",
                out_path=paths.reports_dir / f"{week_tag}.md",
                top_n=request.get("weekly_top", 15),
                mirror_legacy=run.get("write_legacy", True),
                domain_context=domain_context,
            )
            run = load_run_record(run_id)
            run["artifacts"]["report_markdown"] = synthesis_result.get("output")
            _upsert_step(
                run,
                "synthesis",
                status="completed" if synthesis_result.get("status") == "ok" else "skipped",
                output_hint=synthesis_result.get("output"),
                note=synthesis_result.get("message"),
            )
            save_run_record(run)
            _append_event(run_id, "step.completed", {"step": "synthesis", "output": synthesis_result.get("output")})
            _record_legacy_mirror_warnings(run_id, "synthesis", synthesis_result.get("legacy_mirror_errors"))
            try:
                plan = research_plan.generate_run_research_plan(
                    run_id,
                    domain_context=domain_context,
                )
                run = load_run_record(run_id)
                synthesis_step = next((step for step in run.get("steps", []) if step.get("name") == "synthesis"), None)
                generator_mode = (plan.generator.mode or "heuristic").strip()
                generator_label = generator_mode
                if plan.generator.provider_name:
                    generator_label = f"{generator_mode}:{plan.generator.provider_name}"
                if synthesis_step is not None:
                    synthesis_step["note"] = f"Research plan generated via {generator_label}."
                save_run_record(run)
                _append_event(
                    run_id,
                    "research_plan.generated",
                    {
                        "generator_mode": plan.generator.mode,
                        "provider_name": plan.generator.provider_name,
                        "claims": sum(len(section.claims) for section in plan.sections),
                        "evidence_count": plan.evidence_count,
                    },
                )
            except JsonlIntegrityError:
                raise
            except Exception:
                run = load_run_record(run_id)
                run.setdefault("warnings", []).append("[research_plan] research_plan_generation_failed")
                save_run_record(run)
                _append_event(run_id, "research_plan.failed", {"error": "research_plan_generation_failed"})

        if request.get("experiment"):
            _generate_experiment_scaffold_for_run(
                run_id=run_id,
                paths=paths,
                request=request,
                domain_context=domain_context,
            )
        else:
            run = load_run_record(run_id)
            _upsert_step(
                run,
                "experiment",
                status="skipped",
                note="Non-executable proposal scaffold generation was disabled for this run.",
                execution_status=experiments.SCAFFOLD_EXECUTION_STATUS,
                executable=False,
                runner_available=False,
            )
            save_run_record(run)
            _append_event(run_id, "step.skipped", {"step": "experiment"})

        run = load_run_record(run_id)
        prototype = run.get("prototype") if isinstance(run.get("prototype"), dict) else {}
        if prototype.get("schema_version") == PROTOTYPE_SCHEMA_VERSION:
            from app.services import prototype as prototype_service

            prototype_service.finalize_prototype_outputs(run_id)

        _finalize_run(run_id, status="completed")
    except experiments.ExperimentScaffoldError as exc:
        run = load_run_record(run_id)
        run.setdefault("warnings", []).append(f"[experiment_scaffold] {exc.code}")
        _upsert_step(
            run,
            "experiment",
            status="failed",
            note=exc.code,
            execution_status=experiments.SCAFFOLD_EXECUTION_STATUS,
            executable=False,
            runner_available=False,
        )
        save_run_record(run)
        _append_event(
            run_id,
            "experiment.scaffold_failed",
            {
                "step": "experiment",
                "error": exc.code,
                "execution_status": experiments.SCAFFOLD_EXECUTION_STATUS,
            },
        )
        _finalize_run(run_id, status="failed", error=exc.code)
    except JsonlIntegrityError as exc:
        run = load_run_record(run_id)
        run["status"] = "failed"
        run["finished_at"] = iso_now_utc()
        run["error"] = exc.code
        run.setdefault("warnings", []).append(f"[jsonl_integrity] {exc.code}")
        save_run_record(run)
        try:
            _append_event(
                run_id,
                "step.failed",
                {"error": exc.code, "complete": False},
            )
        except JsonlIntegrityError:
            # A corrupt event stream is itself the failure. Do not rewrite,
            # truncate, or append another record to it.
            pass
    except Exception:
        logger.warning("run_pipeline_failed")
        run = load_run_record(run_id)
        run.setdefault("warnings", []).append(f"[pipeline] {PIPELINE_EXECUTION_FAILED_ERROR}")
        save_run_record(run)
        _append_event(run_id, "step.failed", {"error": PIPELINE_EXECUTION_FAILED_ERROR})
        _finalize_run(run_id, status="failed", error=PIPELINE_EXECUTION_FAILED_ERROR)


def start_run(
    request: RunCreateRequest,
    *,
    source: str = "api",
    write_legacy: bool = True,
    owner_user_id: str | None = None,
    topic_profile_id: str | None = None,
    schedule_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run = create_run_from_request(
        request,
        source=source,
        write_legacy=write_legacy,
        owner_user_id=owner_user_id,
        topic_profile_id=topic_profile_id,
        schedule_context=schedule_context,
    )
    _enqueue_or_fail(run["run_id"])
    return load_run_record(run["run_id"])


def run_sync(
    request: RunCreateRequest,
    *,
    source: str = "legacy_cli",
    write_legacy: bool = True,
    owner_user_id: str | None = None,
    topic_profile_id: str | None = None,
    schedule_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run = create_run_from_request(
        request,
        source=source,
        write_legacy=write_legacy,
        owner_user_id=owner_user_id,
        topic_profile_id=topic_profile_id,
        schedule_context=schedule_context,
    )
    _execute_pipeline(run["run_id"])
    return load_run_record(run["run_id"])


def rerun_run(parent_run_id: str, request: RunRerunRequest, *, owner_user_id: str | None = None) -> dict[str, Any]:
    if request.mode not in {"full_rerun_from_profile", "branch_from_selected_papers", "rerun_with_profile_overrides"}:
        raise RuntimeError(f"Unsupported rerun mode: {request.mode}")

    parent_run = load_run_record(parent_run_id)
    base_request = dict(parent_run.get("request") or {})
    override_payload = request.profile_overrides.model_dump(exclude_none=True) if request.profile_overrides else {}
    merged_payload = _merge_request_payload(base_request, override_payload)
    parent_profile = _load_parent_profile_snapshot(parent_run)

    selected_paper_ids = [paper_id.strip() for paper_id in request.selected_paper_ids if paper_id.strip()]
    if request.mode == "branch_from_selected_papers":
        if not selected_paper_ids:
            raise RuntimeError("branch_from_selected_papers requires selected_paper_ids.")
        _validate_selected_papers(parent_run_id, selected_paper_ids)
        merged_payload["skip_scout"] = bool(request.skip_retrieval_when_branching)
        merged_payload["topic"] = merged_payload.get("topic") or f"{parent_run.get('topic', parent_run_id)} branch"

    request_model = RunCreateRequest(**merged_payload)
    lineage = {
        "parent_run_id": parent_run_id,
        "root_run_id": parent_run.get("lineage", {}).get("root_run_id") or parent_run_id,
        "lineage_mode": request.mode,
        "source_paper_ids": selected_paper_ids,
        "created_from_profile_snapshot": True,
        "compare_baseline_run_id": request.compare_baseline_run_id or parent_run_id,
        "branch_reason": request.note if request.mode == "branch_from_selected_papers" else None,
        "created_at": iso_now_utc(),
        "notes": request.note or "",
    }
    child_run = create_run_from_request(
        request_model,
        source="rerun_api",
        write_legacy=parent_run.get("write_legacy", True),
        base_profile=parent_profile,
        lineage=lineage,
        owner_user_id=parent_run.get("owner_user_id") or owner_user_id,
        topic_profile_id=parent_run.get("topic_profile_id"),
    )
    child_run = load_run_record(child_run["run_id"])
    parent_prototype = parent_run.get("prototype") if isinstance(parent_run.get("prototype"), dict) else {}
    if parent_prototype.get("schema_version") == PROTOTYPE_SCHEMA_VERSION:
        child_run["prototype"] = {
            **deepcopy(parent_prototype),
            "pipeline_status": "queued",
            "report_status": "pending",
            "reviewed_paper_count": 0,
            "evidence_span_count": 0,
            "parent_run_id": parent_run_id,
            "provider": {
                **deepcopy(parent_prototype.get("provider") or {}),
                "accepted_control": "pending",
                "rejection_controls": "pending",
            },
        }
    child_run["rerun_context"] = {
        "compare_baseline_run_id": request.compare_baseline_run_id or parent_run_id,
        "preset_id": request.preset_id,
        "selection_source": request.selection_source,
        "selection_bucket_id": request.selection_bucket_id,
        "selection_bucket_label": request.selection_bucket_label,
        "selected_paper_ids": selected_paper_ids,
    }
    save_run_record(child_run)

    if request.mode == "branch_from_selected_papers" and request.skip_retrieval_when_branching:
        seed_path = _seed_selected_papers(parent_run_id, child_run["run_id"], selected_paper_ids)
        child_run = load_run_record(child_run["run_id"])
        child_run.setdefault("artifacts", {})["branch_seed_source_run"] = parent_run_id
        child_run.setdefault("artifacts", {})["branch_seed_inbox"] = seed_path
        save_run_record(child_run)

    execution_plan = _execution_plan_for_rerun(
        request.mode,
        skip_retrieval_when_branching=request.skip_retrieval_when_branching,
    )

    _enqueue_or_fail(child_run["run_id"])

    return {
        "child_run_id": child_run["run_id"],
        "status": "queued",
        "lineage": load_run_record(child_run["run_id"]).get("lineage", {}),
        "execution_plan": execution_plan,
        "message": "Created a new child run from the requested baseline.",
        "rerun_context": load_run_record(child_run["run_id"]).get("rerun_context", {}),
    }


def get_run(run_id: str) -> dict[str, Any]:
    presented = experiments.present_run_experiment_status(load_run_record(run_id))
    return normalize_payload_references(presented, run_id=run_id)


def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    return [
        normalize_payload_references(
            experiments.present_run_experiment_status(run),
            run_id=str(run.get("run_id") or "") or None,
        )
        for run in list_run_records(limit=limit)
    ]


def get_run_papers(run_id: str) -> list[dict[str, Any]]:
    run = load_run_record(run_id)
    triage_path = Path(run.get("artifacts", {}).get("triage_jsonl") or build_run_paths(run_id).triage_dir / "triage.jsonl")
    return read_jsonl(triage_path)


def get_run_report(run_id: str) -> dict[str, Any]:
    report_path = get_run_report_path(run_id)
    if not report_path:
        return {"run_id": run_id, "path": None, "content": None}
    path = Path(report_path)
    return {"run_id": run_id, "path": str(path), "content": path.read_text(encoding="utf-8", errors="ignore")}


def get_run_report_path(run_id: str) -> str | None:
    run = load_run_record(run_id)
    artifacts = run.get("artifacts", {})
    prototype = run.get("prototype") if isinstance(run.get("prototype"), dict) else {}
    if prototype.get("schema_version") == PROTOTYPE_SCHEMA_VERSION:
        report_path = artifacts.get("report_markdown") or artifacts.get("research_plan_markdown")
    else:
        report_path = artifacts.get("research_plan_markdown") or artifacts.get("report_markdown")
    if report_path:
        path = Path(str(report_path))
        if path.is_file():
            return str(path)

    candidates = sorted(
        [
            path
            for path in build_run_paths(run_id).reports_dir.glob("*.md")
            if path.name != "preview.md" and not path.name.startswith("exported_")
        ]
    )
    return str(candidates[-1]) if candidates else None
