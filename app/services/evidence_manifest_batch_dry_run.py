from __future__ import annotations

import shutil
import tempfile
import time
import uuid
import gc
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from app.db import bootstrap
from app.domain import constants
from app.repositories import artifact_refs, runs as run_repository
from app.services import evidence_manifest_admission, evidence_manifest_replay, evidence_manifest_seed, evidence_manifest_verifier, topic_analytics, topic_profiles

SUCCESS_STATUS = "batch_dry_run_planned"
ADMISSION_REJECTED_STATUS = "batch_admission_rejected"
FAILED_STATUS = "batch_dry_run_failed"
PRE_REPLAY_ALLOWED_STATUSES = {"missing_review_row", "replay_required"}


def _safe_segment(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in {"-", "_", "."} else "-" for char in value.strip())
    return cleaned.strip(".-") or "paper"


def _base_result(
    status: str,
    *,
    topic_id: str,
    batch_size: int,
    admission_report: dict[str, Any],
    temp_parent_dir: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "topic_id": topic_id,
        "batch_size": batch_size,
        "attempted_count": 0,
        "dry_run_planned_count": 0,
        "failed_count": 0,
        "skipped_count": batch_size if status == ADMISSION_REJECTED_STATUS else 0,
        "stopped_early": False,
        "first_failure_paper_id": None,
        "first_failure_status": None,
        "temp_parent_dir": temp_parent_dir,
        "temp_cleanup_performed": False,
        "temp_cleanup_exists": False,
        "network_attempted_count": 0,
        "write_attempted_count": 0,
        "replay_verified_count": 0,
        "reviewed_claim_count": 0,
        "safe_to_claim_batch_dry_run_readiness": False,
        "safe_to_claim_batch_live_replay_success": False,
        "safe_to_claim_arbitrary_manifest_automation": False,
        "admission_report": admission_report,
        "per_manifest_results": [],
        "reasons": [],
    }


def _temp_parent(path: str | Path | None) -> Path:
    if path is not None:
        parent = Path(path).expanduser().resolve()
        parent.mkdir(parents=True, exist_ok=False)
        return parent
    return Path(tempfile.mkdtemp(prefix=f"xrml-m24-batch-dryrun-{uuid.uuid4().hex[:8]}-")).resolve()


@contextmanager
def _patched_runtime(data_dir: Path) -> Iterator[None]:
    storage_root = data_dir / "runs"
    profile_dir = data_dir / "topic_profiles"
    analytics_dir = data_dir / "topic_analytics"
    old_values = {
        "constants_storage_root": constants.STORAGE_ROOT,
        "bootstrap_storage_root": bootstrap.STORAGE_ROOT,
        "run_storage_root": run_repository.STORAGE_ROOT,
        "topic_profiles_storage_root": topic_profiles.STORAGE_ROOT,
        "topic_profiles_library_dir": topic_profiles.PROFILE_LIBRARY_DIR,
        "topic_profiles_library_path": topic_profiles.PROFILE_LIBRARY_PATH,
        "topic_analytics_library_dir": topic_analytics.ANALYTICS_LIBRARY_DIR,
        "artifact_storage_root": artifact_refs.constants.STORAGE_ROOT,
    }
    constants.STORAGE_ROOT = storage_root
    bootstrap.STORAGE_ROOT = storage_root
    run_repository.STORAGE_ROOT = storage_root
    topic_profiles.STORAGE_ROOT = storage_root
    topic_profiles.PROFILE_LIBRARY_DIR = profile_dir
    topic_profiles.PROFILE_LIBRARY_PATH = profile_dir / "profiles.json"
    topic_analytics.ANALYTICS_LIBRARY_DIR = analytics_dir
    artifact_refs.constants.STORAGE_ROOT = storage_root
    try:
        yield
    finally:
        constants.STORAGE_ROOT = old_values["constants_storage_root"]
        bootstrap.STORAGE_ROOT = old_values["bootstrap_storage_root"]
        run_repository.STORAGE_ROOT = old_values["run_storage_root"]
        topic_profiles.STORAGE_ROOT = old_values["topic_profiles_storage_root"]
        topic_profiles.PROFILE_LIBRARY_DIR = old_values["topic_profiles_library_dir"]
        topic_profiles.PROFILE_LIBRARY_PATH = old_values["topic_profiles_library_path"]
        topic_analytics.ANALYTICS_LIBRARY_DIR = old_values["topic_analytics_library_dir"]
        artifact_refs.constants.STORAGE_ROOT = old_values["artifact_storage_root"]


def _paper_items(admission_report: dict[str, Any], paths: Sequence[str | Path]) -> list[dict[str, Any]]:
    items = list(admission_report.get("manifests") or [])
    if len(items) == len(paths):
        return items
    return [
        {
            "index": index,
            "manifest_path": str(path),
            "paper_id": f"paper-{index + 1}",
        }
        for index, path in enumerate(paths)
    ]


def _failure(item: dict[str, Any], status: str, result: dict[str, Any], *, batch_size: int) -> None:
    result["status"] = FAILED_STATUS
    result["failed_count"] = 1
    result["stopped_early"] = True
    result["first_failure_paper_id"] = item.get("paper_id")
    result["first_failure_status"] = status
    result["skipped_count"] = max(batch_size - result["attempted_count"], 0)
    result["reasons"] = [status]


def _record_counts(result: dict[str, Any], replay: dict[str, Any] | None) -> None:
    if replay is None:
        return
    if replay.get("network_attempted") is True:
        result["network_attempted_count"] += 1
    if replay.get("write_attempted") is True:
        result["write_attempted_count"] += 1
    if replay.get("status") == "replay_verified":
        result["replay_verified_count"] += 1
    if replay.get("safe_to_claim_reviewed") is True:
        result["reviewed_claim_count"] += 1


def run_batch_dry_run(
    manifest_paths: Sequence[str | Path],
    *,
    topic_id: str,
    temp_parent_dir: str | Path | None = None,
    keep_temp: bool = False,
    require_local_verified: bool = True,
    allow_untracked_manifest: bool = False,
    strict: bool = False,
) -> dict[str, Any]:
    paths = list(manifest_paths)
    admission = evidence_manifest_admission.check_batch_admission(
        paths,
        topic_id=topic_id,
        require_tracked=not allow_untracked_manifest,
        require_local_verified=require_local_verified,
        strict=strict,
    )
    if admission.get("status") != "admitted":
        return _base_result(ADMISSION_REJECTED_STATUS, topic_id=topic_id, batch_size=len(paths), admission_report=admission)

    parent: Path | None = None
    result = _base_result(SUCCESS_STATUS, topic_id=topic_id, batch_size=len(paths), admission_report=admission)
    try:
        parent = _temp_parent(temp_parent_dir)
        result["temp_parent_dir"] = str(parent)
        paper_items = _paper_items(admission, paths)
        for index, (manifest_path, item) in enumerate(zip(paths, paper_items)):
            paper_id = str(item.get("paper_id") or f"paper-{index + 1}")
            data_dir = parent / f"{index + 1:02d}-{_safe_segment(paper_id)}"
            data_dir.mkdir(parents=True, exist_ok=False)
            per_item = {
                "index": index,
                "manifest_path": str(manifest_path),
                "paper_id": paper_id,
                "temp_data_dir": str(data_dir),
                "status": "started",
                "seed_status": None,
                "pre_verify_status": None,
                "dry_run_status": None,
                "post_verify_status": None,
                "network_attempted": False,
                "write_attempted": False,
                "safe_to_claim_reviewed": False,
                "failure_status": None,
            }
            result["per_manifest_results"].append(per_item)
            result["attempted_count"] += 1
            with _patched_runtime(data_dir):
                seed = evidence_manifest_seed.seed_manifest_metadata(manifest_path, data_dir=data_dir)
                per_item["seed_status"] = seed.get("status")
                if seed.get("status") not in {"seed_written", "seed_already_present"}:
                    per_item["status"] = "failed"
                    per_item["failure_status"] = "seed_failed"
                    _failure(per_item, "seed_failed", result, batch_size=len(paths))
                    break

                pre_verify = evidence_manifest_verifier.verify_manifest(manifest_path, topic_id=topic_id, strict=strict)
                per_item["pre_verify_status"] = pre_verify.get("status")
                if pre_verify.get("status") == "verified" or pre_verify.get("status") not in PRE_REPLAY_ALLOWED_STATUSES:
                    per_item["status"] = "failed"
                    per_item["failure_status"] = "pre_verify_unexpectedly_verified"
                    _failure(per_item, "pre_verify_unexpectedly_verified", result, batch_size=len(paths))
                    break

                replay = evidence_manifest_replay.replay_manifest(manifest_path, dry_run=True, env={})
                per_item["dry_run_status"] = replay.get("status")
                per_item["network_attempted"] = bool(replay.get("network_attempted"))
                per_item["write_attempted"] = bool(replay.get("write_attempted"))
                per_item["safe_to_claim_reviewed"] = bool(replay.get("safe_to_claim_reviewed"))
                _record_counts(result, replay)
                if replay.get("network_attempted") is True:
                    per_item["status"] = "failed"
                    per_item["failure_status"] = "replay_dry_run_unexpected_network"
                    _failure(per_item, "replay_dry_run_unexpected_network", result, batch_size=len(paths))
                    break
                if replay.get("write_attempted") is True:
                    per_item["status"] = "failed"
                    per_item["failure_status"] = "replay_dry_run_unexpected_write"
                    _failure(per_item, "replay_dry_run_unexpected_write", result, batch_size=len(paths))
                    break
                if replay.get("status") != "replay_dry_run_planned":
                    per_item["status"] = "failed"
                    per_item["failure_status"] = "replay_dry_run_failed"
                    _failure(per_item, "replay_dry_run_failed", result, batch_size=len(paths))
                    break

                post_verify = evidence_manifest_verifier.verify_manifest(manifest_path, topic_id=topic_id, strict=strict)
                per_item["post_verify_status"] = post_verify.get("status")
                if post_verify.get("status") == "verified" or post_verify.get("status") not in PRE_REPLAY_ALLOWED_STATUSES:
                    per_item["status"] = "failed"
                    per_item["failure_status"] = "post_verify_unexpectedly_verified"
                    _failure(per_item, "post_verify_unexpectedly_verified", result, batch_size=len(paths))
                    break

                per_item["status"] = "dry_run_planned"
                result["dry_run_planned_count"] += 1
        else:
            result["safe_to_claim_batch_dry_run_readiness"] = True
        if result["status"] == SUCCESS_STATUS:
            result["skipped_count"] = 0
    except Exception as exc:
        result["status"] = "failed"
        result["failed_count"] = 1
        result["stopped_early"] = True
        result["first_failure_status"] = "unknown_error"
        result["reasons"] = [f"unknown_error:{type(exc).__name__}"]
    finally:
        if parent is not None and not keep_temp:
            cleanup_error = False
            for attempt in range(5):
                gc.collect()
                try:
                    shutil.rmtree(parent)
                    cleanup_error = False
                    break
                except OSError:
                    cleanup_error = True
                    if attempt < 4:
                        time.sleep(0.1)
            result["temp_cleanup_performed"] = not cleanup_error
            result["temp_cleanup_exists"] = parent.exists()
            if cleanup_error:
                result["status"] = FAILED_STATUS
                result["first_failure_status"] = "temp_cleanup_failed"
                result["reasons"] = ["temp_cleanup_failed"]
        elif parent is not None:
            result["temp_cleanup_exists"] = parent.exists()

    if result["status"] != SUCCESS_STATUS:
        result["safe_to_claim_batch_dry_run_readiness"] = False
    return result
