from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.domain.constants import RUN_SUBDIRS, STORAGE_ROOT
from app.domain.models import RunPaths
from app.domain.path_safety import (
    PathContainmentError,
    is_reparse_point,
    validate_run_id,
)
from app.repositories.artifact_refs import normalize_payload_references, resolve_payload_references
from app.repositories.files import ensure_dir, iso_now_utc, read_json, slugify, write_json


def build_run_paths(run_id: str) -> RunPaths:
    # Central trust boundary: an external run_id must be exactly one safe
    # directory segment before it is ever combined with the storage root.
    validate_run_id(run_id)
    run_dir = STORAGE_ROOT / run_id
    return RunPaths(
        run_dir=run_dir,
        inputs_dir=run_dir / "inputs",
        raw_dir=run_dir / "raw",
        triage_dir=run_dir / "triage",
        notes_dir=run_dir / "notes",
        reviews_dir=run_dir / "reviews",
        reports_dir=run_dir / "reports",
        run_json_path=run_dir / "run.json",
        events_path=run_dir / "events.jsonl",
    )


def ensure_run_layout(run_id: str) -> RunPaths:
    paths = build_run_paths(run_id)
    # Refuse to materialise a run whose directory is (or was pre-created as) a
    # symlink/junction/reparse point that could redirect writes outside storage.
    if is_reparse_point(paths.run_dir):
        raise PathContainmentError("reparse_point_component", field="run_id")
    ensure_dir(paths.run_dir)
    for name in RUN_SUBDIRS:
        ensure_dir(paths.run_dir / name)
    return paths


def make_run_id(topic: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    base = f"{stamp}-{slugify(topic)}"
    candidate = base
    suffix = 2
    while build_run_paths(candidate).run_dir.exists():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def normalize_lineage(run: dict[str, Any], lineage: dict[str, Any] | None = None) -> dict[str, Any]:
    source = dict(lineage or {})
    parent_run_id = source.get("parent_run_id")
    return {
        "parent_run_id": parent_run_id,
        "root_run_id": source.get("root_run_id") or parent_run_id or run["run_id"],
        "lineage_mode": source.get("lineage_mode") or "standalone",
        "source_paper_ids": [str(paper_id) for paper_id in source.get("source_paper_ids", []) if str(paper_id).strip()],
        "created_from_profile_snapshot": bool(source.get("created_from_profile_snapshot", False)),
        "compare_baseline_run_id": source.get("compare_baseline_run_id") or parent_run_id,
        "branch_reason": source.get("branch_reason"),
        "created_at": source.get("created_at") or run.get("created_at") or iso_now_utc(),
        "notes": source.get("notes") or "",
    }


def normalize_run_record(run: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(run)
    normalized.setdefault("steps", [])
    normalized.setdefault("artifacts", {})
    normalized.setdefault("counts", {})
    normalized.setdefault("warnings", [])
    normalized.setdefault("legacy_compatibility", {})
    normalized.setdefault("compare_ready", {})
    normalized.setdefault("rerun_context", {})
    normalized.setdefault("owner_user_id", None)
    normalized.setdefault("owner_assigned_at", None)
    normalized.setdefault("owner_assigned_by", None)
    normalized.setdefault("ownership_source", None)
    normalized.setdefault("topic_profile_id", None)
    normalized.setdefault("schedule_context", {})
    normalized["lineage"] = normalize_lineage(normalized, normalized.get("lineage"))
    return normalized


def create_run_record(
    *,
    topic: str,
    request_payload: dict[str, Any],
    source: str,
    write_legacy: bool,
    lineage: dict[str, Any] | None = None,
    owner_user_id: str | None = None,
) -> dict[str, Any]:
    run_id = make_run_id(topic)
    paths = ensure_run_layout(run_id)
    now = iso_now_utc()
    record = normalize_run_record(
        {
        "run_id": run_id,
        "topic": topic,
        "status": "queued",
        "source": source,
        "write_legacy": write_legacy,
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
        "request": request_payload,
        "steps": [],
        "artifacts": {},
        "counts": {},
        "warnings": [],
        "error": None,
        "legacy_compatibility": {},
        "compare_ready": {},
        "rerun_context": {},
        "owner_user_id": owner_user_id,
        "owner_assigned_at": now if owner_user_id else None,
        "owner_assigned_by": owner_user_id if owner_user_id else None,
        "ownership_source": "run_creator" if owner_user_id else None,
        "topic_profile_id": None,
        "schedule_context": {},
        "lineage": lineage or {},
    }
    )
    write_json(paths.run_json_path, record)
    return record


def load_run_record(run_id: str) -> dict[str, Any]:
    paths = build_run_paths(run_id)
    run = read_json(paths.run_json_path, default=None)
    if run is None:
        raise FileNotFoundError(f"Run not found: {run_id}")
    return normalize_run_record(resolve_payload_references(run, run_id=run_id))


def save_run_record(run: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_run_record(run)
    normalized["updated_at"] = iso_now_utc()
    paths = build_run_paths(normalized["run_id"])
    write_json(paths.run_json_path, normalize_payload_references(normalized, run_id=normalized["run_id"]))
    return normalized


def list_run_records(limit: int | None = 50) -> list[dict[str, Any]]:
    ensure_dir(STORAGE_ROOT)
    runs: list[dict[str, Any]] = []
    for run_json in sorted(STORAGE_ROOT.glob("*/run.json"), reverse=True):
        run = read_json(run_json, default=None)
        if isinstance(run, dict):
            runs.append(normalize_run_record(run))
    return runs[:limit] if limit is not None else runs
