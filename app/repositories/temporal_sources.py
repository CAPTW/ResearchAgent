"""Bounded source capture over existing repository authorities.

SQLite data is read on the capture transaction. Shared JSON/JSONL writers use
their existing locks. Run manifests are atomic replacements without a common
lock: an optimistic file-incarnation and inventory check rejects concurrent
publication. No external IO, retry, source mutation or second persistence store.
"""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import sqlite3
from typing import Any, Callable

from app.auth.resource_access import ResourceActor
from app.domain.path_safety import is_reparse_point
from app.domain.temporal_memory import Member, digest
from app.repositories import paper_collections, paper_reviews, runs
from app.repositories.file_locks import file_lock
from app.repositories.shared_state import lock_path_for
from app.services import paper_bank, paper_occurrences, topic_dashboard, topic_profiles, workbench


def _signature(path: Path) -> tuple[int, int, int, int] | None:
    if not path.exists():
        return None
    if is_reparse_point(path):
        raise ValueError("source_path_unavailable")
    stat = path.stat()
    return stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _manifests() -> dict[Path, tuple[int, int, int, int] | None]:
    return {path: _signature(path) for path in sorted(runs.STORAGE_ROOT.glob("*/run.json"))}


def project(connection: sqlite3.Connection, topic_id: str, actor: ResourceActor | None = None, *,
            consume: Callable[[dict[str, Any], list[dict[str, Any]], list[Member]], None] | None = None) -> list[Member]:
    with ExitStack() as stack:
        for path in sorted((topic_profiles.PROFILE_LIBRARY_PATH, paper_collections.COLLECTION_LIBRARY_PATH)):
            path.parent.mkdir(parents=True, exist_ok=True)
            stack.enter_context(file_lock(lock_path_for(path)))
        manifests = _manifests()
        profile = paper_bank._load_topic_profile(topic_id, actor=actor)
        records = topic_dashboard._topic_run_records(str(profile.get("topic") or ""), profile_id=topic_id, actor=actor)
        if len(records) > 1000:
            raise ValueError("source_capture_limit_exceeded")
        source_paths: dict[str, Path] = {}
        for run in records:
            run_id = str(run["run_id"])
            loaded = runs.load_run_record(run_id)
            path = Path(loaded.get("artifacts", {}).get("triage_jsonl") or runs.build_run_paths(run_id).triage_dir / "triage.jsonl")
            # Snapshotting never expands the current run's storage authority.
            if not path.resolve().is_relative_to(runs.build_run_paths(run_id).run_dir.resolve()):
                raise ValueError("source_path_unavailable")
            if path.exists():
                stack.enter_context(file_lock(lock_path_for(path)))
            source_paths[run_id] = path
        signatures = {path: _signature(path) for path in source_paths.values()}
        discoveries = [(run, item) for run in records for item in workbench.load_run_triage_items(str(run["run_id"]))]
        reviews = [paper_reviews._row_to_review(row) for row in connection.execute(
            "SELECT * FROM paper_reviews WHERE topic_id=? ORDER BY relevance_score DESC, reviewed_at DESC, run_id ASC, paper_id ASC", (topic_id,),
        )]
        membership = paper_collections.collection_membership(topic_id)
        catalog = paper_bank._project_catalog(discoveries, reviews, review_unavailable=False, collection_membership=membership)
        # Exact occurrence authority is deliberately not the UI compatibility ID.
        occurrence_map: dict[tuple[str, str], list[str]] = {}
        for run, raw in discoveries:
            identity = paper_bank._identity(run, raw)
            key = (paper_bank._stable_id("paper", str(identity["workKey"])), paper_bank._stable_id("version", str(identity["versionKey"])))
            occurrence_map.setdefault(key, []).append(paper_occurrences.require_authority(raw))
        result = []
        for item in catalog:
            review: dict[str, Any] | None = item["_selectedReview"]
            evidence_ids: list[str] = []
            evidence_digests: list[tuple[str, str]] = []
            evidence_state = "available" if review else "unavailable"
            for span in (review or {}).get("evidence_spans") or []:
                if not isinstance(span, dict):
                    raise ValueError("evidence_projection_invalid")
                evidence_id = str(span.get("evidence_span_id") or span.get("span_id") or span.get("evidence_id") or "")
                content = span.get("content_sha256") or span.get("text_sha256") or span.get("content_digest")
                if evidence_id:
                    evidence_ids.append(evidence_id)
                if not evidence_id or not content:
                    evidence_state = "unavailable"
                    continue
                evidence_digests.append((evidence_id, digest({"content_digest": content, "locator": span.get("locator")})))
            rights = paper_bank._rights(item["_raw"])
            result.append(Member(
                canonical_paper_id=item["canonicalPaperId"], paper_version_id=item["paperVersionId"],
                occurrence_ids=tuple(occurrence_map[(item["canonicalPaperId"], item["paperVersionId"])]),
                collection_ids=tuple(item["collectionIds"]), review_state=item["review"]["state"],
                full_text_state=str((review or {}).get("full_text_status") or "unavailable"),
                full_text_content_digest=((review or {}).get("source_text_sha256") or None)
                if (review or {}).get("full_text_status") in {"full_text_available", "reviewed"} else None,
                evidence_ids=tuple(evidence_ids), evidence_digests=tuple(evidence_digests), evidence_state=evidence_state,
                rights_access_state=rights["state"], rights_declaration_digest=digest(rights["label"]) if rights["label"] is not None else None,
                source_entity_ids=tuple(sorted({o["runId"] for o in item["compatibility"]["occurrences"]})),
            ))
        if consume is not None:
            consume(profile, catalog, result)
        if manifests != _manifests() or signatures != {path: _signature(path) for path in source_paths.values()}:
            raise ValueError("snapshot_capture_conflict")
        return result
