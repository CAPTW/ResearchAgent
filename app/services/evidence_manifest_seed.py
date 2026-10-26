from __future__ import annotations

from pathlib import Path
from typing import Any

from app.domain import constants
from app.domain.constants import RUN_SUBDIRS
from app.repositories.files import ensure_dir, iso_now_utc, read_json, read_jsonl, safe_filename, slugify, write_json
from app.repositories.runs import normalize_run_record
from app.services import evidence_manifest_verifier, paper_occurrences

SEED_STATUSES = [
    "seed_plan_created",
    "seed_blocked_no_data_dir",
    "seed_blocked_real_data_dir",
    "manifest_invalid",
    "seed_written",
    "seed_already_present",
    "seed_candidate_mismatch",
    "seed_verification_failed",
    "replay_plan_ready",
    "failed",
]


def _base_result(
    status: str,
    *,
    manifest_path: str | Path,
    manifest: dict[str, Any] | None = None,
    data_dir: str | Path | None = None,
    dry_run: bool = False,
    reasons: list[str] | None = None,
) -> dict[str, Any]:
    manifest = manifest or {}
    return {
        "status": status,
        "manifest_path": str(manifest_path),
        "topic_id": manifest.get("topic_id"),
        "paper_id": manifest.get("paper_id"),
        "run_id": None,
        "data_dir": str(data_dir) if data_dir is not None else None,
        "dry_run": bool(dry_run),
        "wrote_files": False,
        "wrote_db": False,
        "seeded_run_count": 0,
        "seeded_candidate_count": 0,
        "seeded_triage_count": 0,
        "seeded_exclusion_count": 0,
        "network_attempted": False,
        "review_written": False,
        "replay_executed": False,
        "safe_to_claim_reviewed": False,
        "safe_to_claim_replay_planned": False,
        "reasons": reasons or [],
        "files_planned": [],
        "files_written": [],
        "supported_statuses": SEED_STATUSES,
    }


def _manifest_or_error(path: str | Path) -> tuple[dict[str, Any] | None, list[str]]:
    manifest_path = Path(path)
    if not manifest_path.exists():
        return None, ["manifest_not_found"]
    try:
        manifest = evidence_manifest_verifier.load_manifest(manifest_path)
    except Exception as exc:
        return None, [f"manifest_parse_error:{type(exc).__name__}"]
    return manifest, evidence_manifest_verifier.validate_manifest_payload(manifest)


def _is_real_data_dir(path: Path) -> bool:
    project_root = constants.PROJECT_ROOT.resolve()
    real_storage = (project_root / "storage").resolve()
    real_runs = (real_storage / "runs").resolve()
    resolved = path.resolve()
    if resolved in {project_root, real_storage, real_runs}:
        return True
    try:
        return resolved.is_relative_to(real_storage)
    except AttributeError:  # pragma: no cover - Python < 3.9 fallback
        return str(resolved).startswith(str(real_storage))


def _run_id_for_manifest(manifest: dict[str, Any]) -> str:
    topic = slugify(str(manifest.get("topic_id") or "topic"))
    paper = safe_filename(str(manifest.get("paper_id") or "paper"))
    return f"manifest-seed-{topic}-{paper}"


def _paths(data_dir: Path, manifest: dict[str, Any]) -> dict[str, Path]:
    run_id = _run_id_for_manifest(manifest)
    run_dir = data_dir / "runs" / run_id
    return {
        "run_dir": run_dir,
        "run_json": run_dir / "run.json",
        "triage_jsonl": run_dir / "triage" / "triage.jsonl",
    }


def _topic_title(topic_id: str) -> str:
    return " ".join(part.capitalize() for part in str(topic_id or "").replace("_", "-").split("-") if part) or topic_id


def _target_abstract(manifest: dict[str, Any]) -> str:
    title = str(manifest.get("title") or "").strip()
    return (
        f"Manifest-seeded abstract-only candidate metadata for replay planning. "
        f"The paper is about large language models and benchmark assessment. Title: {title}"
    ).strip()


def _known_exclusion_rows(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for exclusion in manifest.get("known_exclusions") or []:
        paper_id = str(exclusion.get("paper_id") or "").strip()
        if not paper_id:
            continue
        rows.append(
            {
                "paper_id": paper_id,
                "title": "Known oversized replay exclusion",
                "abstract": "Known oversized candidate excluded from this bounded replay plan.",
                "score": 100,
                "source_provider": "arxiv",
                "published": "2026-05-08T00:00:00Z",
                "pdf_url": f"https://arxiv.org/pdf/{paper_id}",
                "full_text_status": "abstract_only",
                "acquisition_status": "excluded",
                "exclusion_reason": str(exclusion.get("reason") or "excluded"),
                "max_pdf_bytes": int(exclusion.get("max_pdf_bytes") or (manifest.get("acquisition") or {}).get("max_pdf_bytes") or 0),
            }
        )
    return rows


def _triage_rows(manifest: dict[str, Any], manifest_path: str | Path) -> list[dict[str, Any]]:
    target = {
        "paper_id": str(manifest["paper_id"]),
        "title": str(manifest.get("title") or "Manifest replay target"),
        "abstract": _target_abstract(manifest),
        "score": 68,
        "source_provider": "arxiv",
        "published": "2026-04-30T00:00:00Z",
        "pdf_url": str(manifest["pdf_url"]),
        "full_text_status": "abstract_only",
        "acquisition_status": "candidate",
        "source_manifest_path": str(manifest_path),
    }
    return [*_known_exclusion_rows(manifest), target]


def _run_record(manifest: dict[str, Any], *, run_id: str, triage_count: int) -> dict[str, Any]:
    now = iso_now_utc()
    topic_id = str(manifest["topic_id"])
    topic = _topic_title(topic_id)
    return normalize_run_record(
        {
            "run_id": run_id,
            "topic": topic,
            "status": "completed",
            "source": "manifest_seed:temp_replay_plan",
            "write_legacy": False,
            "created_at": now,
            "updated_at": now,
            "started_at": now,
            "finished_at": now,
            "request": {
                "topic": topic,
                "topic_id": topic_id,
                "source": "manifest_seed",
                "manifest_paper_id": manifest.get("paper_id"),
            },
            "steps": [{"name": "triage", "status": "completed"}],
            "artifacts": {},
            "counts": {"triage_items": triage_count},
            "warnings": [],
            "error": None,
            "legacy_compatibility": {},
            "compare_ready": {},
            "rerun_context": {},
            "owner_user_id": None,
            "owner_assigned_at": None,
            "owner_assigned_by": None,
            "ownership_source": None,
            "topic_profile_id": topic_id,
            "schedule_context": {},
            "lineage": {},
        }
    )


def _seed_matches(run_json: Path, triage_jsonl: Path, manifest: dict[str, Any]) -> bool:
    run = read_json(run_json, default={})
    rows = read_jsonl(triage_jsonl)
    paper_ids = {str(row.get("paper_id") or "") for row in rows}
    known_ids = {str(row.get("paper_id") or "") for row in _known_exclusion_rows(manifest)}
    return (
        isinstance(run, dict)
        and str(run.get("source") or "") == "manifest_seed:temp_replay_plan"
        and str(run.get("status") or "") == "completed"
        and str(run.get("topic_profile_id") or "") == str(manifest.get("topic_id") or "")
        and str(manifest.get("paper_id") or "") in paper_ids
        and known_ids.issubset(paper_ids)
    )


def seed_manifest_metadata(
    manifest_path: str | Path,
    *,
    data_dir: str | Path | None,
    dry_run: bool = False,
    allow_real_data_dir: bool = False,
) -> dict[str, Any]:
    if data_dir is None or not str(data_dir).strip():
        return _base_result("seed_blocked_no_data_dir", manifest_path=manifest_path, dry_run=dry_run, reasons=["data_dir_required"])

    manifest, reasons = _manifest_or_error(manifest_path)
    if manifest is None or reasons:
        return _base_result(
            "manifest_invalid",
            manifest_path=manifest_path,
            manifest=manifest,
            data_dir=data_dir,
            dry_run=dry_run,
            reasons=reasons,
        )

    resolved_data_dir = Path(data_dir).expanduser().resolve()
    if not allow_real_data_dir and _is_real_data_dir(resolved_data_dir):
        return _base_result(
            "seed_blocked_real_data_dir",
            manifest_path=manifest_path,
            manifest=manifest,
            data_dir=resolved_data_dir,
            dry_run=dry_run,
            reasons=["real_data_dir_blocked"],
        )

    run_id = _run_id_for_manifest(manifest)
    paths = _paths(resolved_data_dir, manifest)
    triage_rows = _triage_rows(manifest, manifest_path)
    planned_files = [str(paths["run_json"]), str(paths["triage_jsonl"])]
    result = _base_result(
        "seed_plan_created" if dry_run else "seed_written",
        manifest_path=manifest_path,
        manifest=manifest,
        data_dir=resolved_data_dir,
        dry_run=dry_run,
    )
    result.update(
        {
            "run_id": run_id,
            "seeded_run_count": 1,
            "seeded_candidate_count": 1,
            "seeded_triage_count": len(triage_rows),
            "seeded_exclusion_count": len(_known_exclusion_rows(manifest)),
            "files_planned": planned_files,
        }
    )

    if dry_run:
        return result

    if paths["run_json"].exists() or paths["triage_jsonl"].exists():
        if _seed_matches(paths["run_json"], paths["triage_jsonl"], manifest):
            result["status"] = "seed_already_present"
            result["wrote_files"] = False
            return result
        result["status"] = "seed_candidate_mismatch"
        result["reasons"] = ["existing_seed_metadata_mismatch"]
        return result

    for subdir in RUN_SUBDIRS:
        ensure_dir(paths["run_dir"] / subdir)
    run = _run_record(manifest, run_id=run_id, triage_count=len(triage_rows))
    write_json(paths["run_json"], run)
    paper_occurrences.write_triage_records(paths["triage_jsonl"], triage_rows)
    result["wrote_files"] = True
    result["files_written"] = planned_files
    return result
