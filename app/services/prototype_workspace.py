"""Versioned multi-topic workspace for the isolated Prototype v0.7 lane.

The workspace is a small, local metadata index.  It deliberately references the
existing run repository and v0.3 real-corpus artifacts instead of introducing a
second run, report, evidence, or corpus store.  One lock-held JSON replacement is
the authority boundary for topic settings, immutable snapshots, curation events,
changesets, and refresh records.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from app.auth.resource_access import ResourceActor
from app.repositories.files import iso_now_utc, slugify
from app.repositories.runs import list_run_records, load_run_record, save_run_record
from app.repositories.shared_state import load_json_state, mutate_json_state
from app.services import (
    prototype,
    prototype_living_topics,
    prototype_real_corpus,
    prototype_research_gaps,
    report_versions,
)

WORKSPACE_SCHEMA = "prototype_multi_topic_workspace_v0_7"
LEGACY_WORKSPACE_SCHEMAS = {
    "prototype_multi_topic_workspace_v0_4",
    "prototype_multi_topic_workspace_v0_5",
    "prototype_multi_topic_workspace_v0_6",
}
STATE_NAME = "prototype_workspace"
STATE_FILENAME = "workspace.json"

SYNTHETIC_TOPIC_ID = "topic-synthetic-spatial-evidence"
REAL_TOPIC_ID = "topic-real-pmc-xr-rehabilitation"
LOCAL_TOPIC_ID = "topic-local-immersive-training-transfer"
LOCAL_ACTOR = "LOCAL_PROTOTYPE_USER"

TOPIC_KINDS = {"synthetic_demo", "real_open_access", "local_empty"}
SOURCE_POLICIES = {"synthetic_fixture", "pmc_open_access", "local_only_no_external_source"}
PROVIDER_POSTURES = {
    "deterministic_only",
    "prototype_provider_capability_available_but_disabled",
}
CURATION_STATES = {"included", "excluded", "hold"}
CURATION_REASONS = {
    "relevant_core",
    "relevant_context",
    "duplicate_work",
    "out_of_scope",
    "insufficient_full_text",
    "license_not_accepted",
    "article_type_not_eligible",
    "quality_or_relevance_hold",
    "manual_owner_decision",
}

BUNDLE_STATE_LIST_KEYS = (
    "bundle_projects",
    "portable_runs",
    "portable_rights",
    "portable_full_text_descriptors",
    "portable_reviews",
    "portable_evidence",
    "portable_claims",
    "portable_reports",
    "portable_report_versions",
    "portable_provider_execution_summaries",
)

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,179}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class PrototypeWorkspaceError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 409) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _workspace_path() -> Path:
    prototype.require_prototype_mode()
    configured = str(os.getenv("XRML_DATA_DIR", "") or "").strip()
    if not configured:  # require_prototype_mode normally catches this first.
        raise PrototypeWorkspaceError("prototype_data_root_required")
    return Path(configured).expanduser().resolve() / "prototype-workspace" / STATE_FILENAME


def _safe_text(value: Any, *, maximum: int, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise PrototypeWorkspaceError("topic_text_required", status_code=400)
    if len(text) > maximum or _CONTROL_RE.search(text) or "<" in text or ">" in text:
        raise PrototypeWorkspaceError("topic_text_rejected", status_code=400)
    return text


def _safe_identifier(value: Any, *, code: str) -> str:
    text = str(value or "").strip()
    if not _SAFE_ID_RE.fullmatch(text):
        raise PrototypeWorkspaceError(code, status_code=400)
    return text


def _settings_payload(topic: dict[str, Any]) -> dict[str, Any]:
    return {
        key: topic.get(key)
        for key in (
            "display_name",
            "description",
            "research_question",
            "topic_kind",
            "source_policy",
            "language",
            "publication_date_cutoff",
            "license_policy",
            "refresh_mode",
            "provider_posture",
            "archived",
        )
    }


def _settings_digest(topic: dict[str, Any]) -> str:
    return _digest(_settings_payload(topic))


def _validate_policy(topic: dict[str, Any]) -> None:
    kind = str(topic.get("topic_kind") or "")
    policy = str(topic.get("source_policy") or "")
    license_policy = str(topic.get("license_policy") or "")
    refresh_mode = str(topic.get("refresh_mode") or "")
    compatible = {
        "synthetic_demo": "synthetic_fixture",
        "real_open_access": "pmc_open_access",
        "local_empty": "local_only_no_external_source",
    }
    if kind not in TOPIC_KINDS or policy != compatible.get(kind):
        raise PrototypeWorkspaceError("topic_source_policy_incompatible", status_code=400)
    if kind == "real_open_access" and license_policy != "cc0_cc_by_only":
        raise PrototypeWorkspaceError("topic_license_policy_incompatible", status_code=400)
    if kind == "synthetic_demo" and license_policy != "synthetic_fixture":
        raise PrototypeWorkspaceError("topic_license_policy_incompatible", status_code=400)
    if kind == "local_empty" and license_policy != "local_only":
        raise PrototypeWorkspaceError("topic_license_policy_incompatible", status_code=400)
    if kind == "real_open_access" and refresh_mode != "manual_only":
        raise PrototypeWorkspaceError("topic_refresh_mode_incompatible", status_code=400)
    if kind != "real_open_access" and refresh_mode != "disabled":
        raise PrototypeWorkspaceError("topic_refresh_mode_incompatible", status_code=400)
    if str(topic.get("provider_posture") or "") not in PROVIDER_POSTURES:
        raise PrototypeWorkspaceError("topic_provider_posture_invalid", status_code=400)


def _snapshot_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    included = [item for item in items if item.get("curation_state") == "included"]
    return {
        "item_count": len(items),
        "included_count": len(included),
        "excluded_count": sum(1 for item in items if item.get("curation_state") == "excluded"),
        "hold_count": sum(1 for item in items if item.get("curation_state") == "hold"),
        "reviewed_count": sum(1 for item in included if item.get("review_status") == "reviewed"),
        "evidence_count": sum(int(item.get("evidence_count") or 0) for item in included),
    }


def _snapshot_manifest_digest(
    *,
    source_policy: str,
    query_id: str,
    query_digest: str,
    settings_version: int,
    settings_digest: str,
    items: list[dict[str, Any]],
) -> str:
    manifest_items = [
        {
            "corpus_item_id": item.get("corpus_item_id"),
            "work_identity_digest": item.get("work_identity_digest"),
            "source_type": item.get("source_type"),
            "source_identity": item.get("source_identity"),
            "license_status": item.get("license_status"),
            "full_text_status": item.get("full_text_status"),
            "curation_state": item.get("curation_state"),
            "source_content_digest": item.get("source_content_digest"),
        }
        for item in sorted(items, key=lambda row: str(row.get("corpus_item_id") or ""))
    ]
    return _digest(
        {
            "source_policy": source_policy,
            "query_id": query_id,
            "query_digest": query_digest,
            "settings_version": settings_version,
            "settings_digest": settings_digest,
            "items": manifest_items,
        }
    )


def _new_snapshot(
    *,
    topic: dict[str, Any],
    version: int,
    parent_snapshot_id: str | None,
    query_id: str,
    query_digest: str,
    items: list[dict[str, Any]],
    created_by_mode: str,
    refresh_id: str | None,
    source_run_id: str | None,
    change_summary: dict[str, Any],
    created_at: str | None = None,
) -> dict[str, Any]:
    copied_items = deepcopy(items)
    manifest_digest = _snapshot_manifest_digest(
        source_policy=str(topic["source_policy"]),
        query_id=query_id,
        query_digest=query_digest,
        settings_version=int(topic["settings_version"]),
        settings_digest=str(topic["settings_digest"]),
        items=copied_items,
    )
    snapshot_id = f"snapshot-{topic['topic_id']}-v{version}-{manifest_digest[:12]}"
    for item in copied_items:
        item["topic_id"] = topic["topic_id"]
        item["snapshot_id"] = snapshot_id
    return {
        "snapshot_id": snapshot_id,
        "topic_id": topic["topic_id"],
        "parent_snapshot_id": parent_snapshot_id,
        "snapshot_version": version,
        "source_policy": topic["source_policy"],
        "query_id": query_id,
        "query_digest": query_digest,
        "settings_version": topic["settings_version"],
        "settings_digest": topic["settings_digest"],
        "created_at": created_at or iso_now_utc(),
        "created_by_mode": created_by_mode,
        "refresh_id": refresh_id,
        **_snapshot_counts(copied_items),
        "manifest_digest": manifest_digest,
        "change_summary": deepcopy(change_summary),
        "source_run_id": source_run_id,
        "items": copied_items,
    }


def _synthetic_items() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for paper in prototype._demo_corpus():
        paper_id = str(paper["paper_id"])
        identity = f"synthetic:{paper_id}"
        items.append(
            {
                "corpus_item_id": identity,
                "source_type": "synthetic_fixture",
                "source_identity": identity,
                "pmcid": None,
                "pmid": None,
                "doi": None,
                "work_identity_digest": _digest({"identity": identity}),
                "title": paper["title"],
                "publication_date": paper["published"],
                "license_status": "synthetic_fixture",
                "license_type": "Synthetic fixture",
                "full_text_status": "available",
                "review_status": "reviewed",
                "evidence_count": 2,
                "curation_state": "included",
                "curation_reason": "relevant_core",
                "source_content_digest": _digest({"paper_id": paper_id, "fixture": "prototype-v0.1"}),
                "stale_status": "current",
            }
        )
    return items


def _topic_record(
    *,
    topic_id: str,
    display_name: str,
    description: str,
    research_question: str,
    topic_kind: str,
    source_policy: str,
    language: str,
    publication_date_cutoff: str | None,
    license_policy: str,
    refresh_mode: str,
    provider_posture: str,
    created_at: str,
) -> dict[str, Any]:
    topic = {
        "topic_id": topic_id,
        "slug": slugify(display_name),
        "display_name": display_name,
        "description": description,
        "research_question": research_question,
        "topic_kind": topic_kind,
        "source_policy": source_policy,
        "language": language,
        "publication_date_cutoff": publication_date_cutoff,
        "license_policy": license_policy,
        "refresh_mode": refresh_mode,
        "provider_posture": provider_posture,
        "active_snapshot_id": None,
        "latest_run_id": None,
        "latest_report_version_id": None,
        "archived": False,
        "created_at": created_at,
        "updated_at": created_at,
        "settings_version": 1,
        "settings_digest": "",
    }
    _validate_policy(topic)
    topic["settings_digest"] = _settings_digest(topic)
    return topic


def _settings_version(topic: dict[str, Any], *, changed_fields: list[str], created_at: str) -> dict[str, Any]:
    return {
        "topic_id": topic["topic_id"],
        "settings_version": topic["settings_version"],
        "settings_digest": topic["settings_digest"],
        "settings": _settings_payload(topic),
        "changed_fields": sorted(changed_fields),
        "created_at": created_at,
        "actor_mode": LOCAL_ACTOR if changed_fields else "SYSTEM_PROTOTYPE_SEED",
    }


def _default_state() -> dict[str, Any]:
    now = iso_now_utc()
    synthetic = _topic_record(
        topic_id=SYNTHETIC_TOPIC_ID,
        display_name=prototype.DEMO_TOPIC,
        description="Synthetic local corpus for the isolated research journey.",
        research_question="How can spatial-computing claims remain bounded to inspectable evidence?",
        topic_kind="synthetic_demo",
        source_policy="synthetic_fixture",
        language="en",
        publication_date_cutoff=None,
        license_policy="synthetic_fixture",
        refresh_mode="disabled",
        provider_posture="deterministic_only",
        created_at=now,
    )
    real_topic = _topic_record(
        topic_id=REAL_TOPIC_ID,
        display_name=prototype_real_corpus.TOPIC,
        description="License-aware full-text research from the official PMC Open Access corpus.",
        research_question="What does reusable full-text evidence show about VR/AR rehabilitation and clinical training?",
        topic_kind="real_open_access",
        source_policy="pmc_open_access",
        language="en",
        publication_date_cutoff=prototype_real_corpus.QUERY_CUTOFF,
        license_policy="cc0_cc_by_only",
        refresh_mode="manual_only",
        provider_posture="prototype_provider_capability_available_but_disabled",
        created_at=now,
    )
    local_topic = _topic_record(
        topic_id=LOCAL_TOPIC_ID,
        display_name="Immersive learning systems and training transfer",
        description="Local-only topic for owner-curated evidence and future research runs.",
        research_question="How does immersive learning transfer into later training performance?",
        topic_kind="local_empty",
        source_policy="local_only_no_external_source",
        language="en",
        publication_date_cutoff=None,
        license_policy="local_only",
        refresh_mode="disabled",
        provider_posture="deterministic_only",
        created_at=now,
    )
    snapshot = _new_snapshot(
        topic=synthetic,
        version=1,
        parent_snapshot_id=None,
        query_id="synthetic-fixture-v1",
        query_digest=_digest({"fixture": "prototype-v0.1"}),
        items=_synthetic_items(),
        created_by_mode="SYSTEM_PROTOTYPE_SEED",
        refresh_id=None,
        source_run_id=None,
        change_summary={"added": 4, "removed": 0, "held": 0, "restored": 0},
        created_at=now,
    )
    synthetic["active_snapshot_id"] = snapshot["snapshot_id"]
    return {
        "schema_version": WORKSPACE_SCHEMA,
        "topics": [synthetic, real_topic, local_topic],
        "settings_versions": [
            _settings_version(synthetic, changed_fields=[], created_at=now),
            _settings_version(real_topic, changed_fields=[], created_at=now),
            _settings_version(local_topic, changed_fields=[], created_at=now),
        ],
        "snapshots": [snapshot],
        "curation_events": [],
        "changesets": [],
        "refresh_records": [],
        **{key: [] for key in prototype_living_topics.LIVING_LIST_KEYS},
        **{key: [] for key in prototype_research_gaps.RESEARCH_LIST_KEYS},
        **{key: [] for key in BUNDLE_STATE_LIST_KEYS},
    }


def _event_digest(event: dict[str, Any]) -> str:
    return _digest({key: value for key, value in event.items() if key != "event_digest"})


def _changeset_digest(changeset: dict[str, Any]) -> str:
    ignored = {
        "changeset_digest",
        "applied",
        "failed",
        "discarded",
        "resulting_snapshot_id",
        "applied_at",
        "failure_category",
    }
    return _digest({key: value for key, value in changeset.items() if key not in ignored})


def _validate_state(state: dict[str, Any]) -> dict[str, Any]:
    if state.get("schema_version") != WORKSPACE_SCHEMA:
        raise PrototypeWorkspaceError("workspace_schema_invalid")
    for key in (
        "topics",
        "settings_versions",
        "snapshots",
        "curation_events",
        "changesets",
        "refresh_records",
        *BUNDLE_STATE_LIST_KEYS,
    ):
        if not isinstance(state.get(key), list):
            raise PrototypeWorkspaceError("workspace_schema_invalid")

    topics = state["topics"]
    topic_ids = [str(topic.get("topic_id") or "") for topic in topics]
    slugs = [str(topic.get("slug") or "") for topic in topics]
    if len(topic_ids) != len(set(topic_ids)) or len(slugs) != len(set(slugs)):
        raise PrototypeWorkspaceError("workspace_topic_identity_collision")
    for topic in topics:
        _validate_policy(topic)
        if topic.get("settings_digest") != _settings_digest(topic):
            raise PrototypeWorkspaceError("workspace_settings_digest_mismatch")

    snapshots = state["snapshots"]
    snapshot_ids = [str(snapshot.get("snapshot_id") or "") for snapshot in snapshots]
    if len(snapshot_ids) != len(set(snapshot_ids)):
        raise PrototypeWorkspaceError("workspace_snapshot_identity_collision")
    topic_by_id = {topic["topic_id"]: topic for topic in topics}
    for snapshot in snapshots:
        if snapshot.get("topic_id") not in topic_ids:
            raise PrototypeWorkspaceError("workspace_snapshot_topic_missing")
        expected = _snapshot_manifest_digest(
            source_policy=str(snapshot.get("source_policy") or ""),
            query_id=str(snapshot.get("query_id") or ""),
            query_digest=str(snapshot.get("query_digest") or ""),
            settings_version=int(snapshot.get("settings_version") or 0),
            settings_digest=str(snapshot.get("settings_digest") or ""),
            items=list(snapshot.get("items") or []),
        )
        if snapshot.get("manifest_digest") != expected:
            raise PrototypeWorkspaceError("workspace_snapshot_manifest_mismatch")
        if _snapshot_counts(list(snapshot.get("items") or [])) != {
            key: int(snapshot.get(key) or 0)
            for key in (
                "item_count",
                "included_count",
                "excluded_count",
                "hold_count",
                "reviewed_count",
                "evidence_count",
            )
        }:
            raise PrototypeWorkspaceError("workspace_snapshot_count_mismatch")
        for item in snapshot.get("items") or []:
            if item.get("topic_id") != snapshot.get("topic_id") or item.get("snapshot_id") != snapshot.get("snapshot_id"):
                raise PrototypeWorkspaceError("workspace_snapshot_item_boundary_mismatch")
            if item.get("curation_state") not in CURATION_STATES:
                raise PrototypeWorkspaceError("workspace_curation_state_invalid")
            if (
                item.get("curation_state") == "included"
                and topic_by_id[str(snapshot["topic_id"])].get("source_policy") == "pmc_open_access"
                and (
                    item.get("license_status") != "accepted"
                    or item.get("full_text_status") != "available"
                )
            ):
                raise PrototypeWorkspaceError("workspace_trusted_snapshot_item_rejected")

    for topic in topics:
        topic_snapshots = [snapshot for snapshot in snapshots if snapshot.get("topic_id") == topic.get("topic_id")]
        active_id = topic.get("active_snapshot_id")
        if topic_snapshots and active_id not in {snapshot["snapshot_id"] for snapshot in topic_snapshots}:
            raise PrototypeWorkspaceError("workspace_active_snapshot_missing")
        if not topic_snapshots and active_id is not None:
            raise PrototypeWorkspaceError("workspace_empty_topic_has_snapshot_pointer")

    event_ids: set[str] = set()
    for event in state["curation_events"]:
        event_id = str(event.get("event_id") or "")
        if event_id in event_ids or event.get("event_digest") != _event_digest(event):
            raise PrototypeWorkspaceError("workspace_curation_event_invalid")
        event_ids.add(event_id)
    for changeset in state["changesets"]:
        if changeset.get("changeset_digest") != _changeset_digest(changeset):
            raise PrototypeWorkspaceError("workspace_changeset_digest_mismatch")
    prototype_living_topics.validate_state(state)
    prototype_research_gaps.validate_state(state)
    return state


def _upgrade_state(state: dict[str, Any]) -> dict[str, Any]:
    schema = state.get("schema_version")
    if schema == WORKSPACE_SCHEMA:
        return prototype_research_gaps.ensure_state_shape(prototype_living_topics.ensure_state_shape(state))
    if schema not in LEGACY_WORKSPACE_SCHEMAS:
        raise PrototypeWorkspaceError("workspace_schema_invalid")
    upgraded = prototype_research_gaps.ensure_state_shape(prototype_living_topics.ensure_state_shape(state))
    for key in BUNDLE_STATE_LIST_KEYS:
        upgraded.setdefault(key, [])
    upgraded["schema_version"] = WORKSPACE_SCHEMA
    return upgraded


def _ensure_state() -> dict[str, Any]:
    path = _workspace_path()

    def initialize(current: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        if current:
            validated = _validate_state(_upgrade_state(deepcopy(current)))
            return validated, validated
        seeded = _default_state()
        _validate_state(seeded)
        return seeded, seeded

    return mutate_json_state(
        path,
        state_name=STATE_NAME,
        expected_type=dict,
        default=dict,
        mutate=initialize,
    )


def _load_state() -> dict[str, Any]:
    _ensure_state()
    state = load_json_state(
        _workspace_path(),
        state_name=STATE_NAME,
        expected_type=dict,
        default=dict,
    )
    return _validate_state(deepcopy(state))


def _mutate_state(
    mutation: Callable[[dict[str, Any]], tuple[dict[str, Any], Any]],
) -> Any:
    _ensure_state()

    def guarded(current: dict[str, Any]) -> tuple[dict[str, Any], Any]:
        working = _validate_state(deepcopy(current))
        next_state, result = mutation(working)
        _validate_state(next_state)
        return next_state, result

    return mutate_json_state(
        _workspace_path(),
        state_name=STATE_NAME,
        expected_type=dict,
        default=dict,
        mutate=guarded,
    )


def _topic(state: dict[str, Any], topic_id: str) -> dict[str, Any]:
    safe_id = _safe_identifier(topic_id, code="topic_id_invalid")
    match = next((item for item in state["topics"] if item.get("topic_id") == safe_id), None)
    if not isinstance(match, dict):
        raise PrototypeWorkspaceError("topic_not_found", status_code=404)
    return match


def _require_bundle_execution_allowed(topic: dict[str, Any]) -> None:
    if topic.get("imported_project_id"):
        raise PrototypeWorkspaceError("bundle_import_execution_disabled")


def _snapshot(state: dict[str, Any], snapshot_id: str, *, topic_id: str | None = None) -> dict[str, Any]:
    safe_id = _safe_identifier(snapshot_id, code="snapshot_id_invalid")
    match = next((item for item in state["snapshots"] if item.get("snapshot_id") == safe_id), None)
    if not isinstance(match, dict):
        raise PrototypeWorkspaceError("snapshot_not_found", status_code=404)
    if topic_id is not None and match.get("topic_id") != topic_id:
        raise PrototypeWorkspaceError("cross_topic_snapshot_reference", status_code=409)
    return match


def _decorate_snapshot(state: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    topic = _topic(state, str(snapshot["topic_id"]))
    children = sorted(
        (
            item
            for item in state["snapshots"]
            if item.get("topic_id") == snapshot.get("topic_id")
            and item.get("parent_snapshot_id") == snapshot.get("snapshot_id")
        ),
        key=lambda item: int(item.get("snapshot_version") or 0),
    )
    return {
        **deepcopy(snapshot),
        "active": topic.get("active_snapshot_id") == snapshot.get("snapshot_id"),
        "superseded_by_snapshot_id": children[0]["snapshot_id"] if children else None,
    }


def _workspace_runs(topic_id: str, state: dict[str, Any]) -> list[dict[str, Any]]:
    snapshot_by_id = {
        snapshot["snapshot_id"]: snapshot
        for snapshot in state["snapshots"]
        if snapshot.get("topic_id") == topic_id
    }
    rows: list[dict[str, Any]] = []
    live_run_ids: set[str] = set()
    for run in list_run_records(limit=None):
        workspace = run.get("workspace") if isinstance(run.get("workspace"), dict) else {}
        if workspace.get("topic_id") != topic_id:
            continue
        snapshot = snapshot_by_id.get(str(workspace.get("snapshot_id") or ""))
        if snapshot is None:
            continue
        if workspace.get("source_manifest_digest") != snapshot.get("manifest_digest"):
            continue
        versions = report_versions.list_report_versions(str(run["run_id"])).get("items") or []
        latest_version = versions[-1] if versions else None
        rows.append(
            {
                "run_id": run["run_id"],
                "topic_id": topic_id,
                "snapshot_id": snapshot["snapshot_id"],
                "snapshot_version": snapshot["snapshot_version"],
                "settings_version": workspace.get("settings_version"),
                "source_manifest_digest": workspace.get("source_manifest_digest"),
                "evidence_set_digest": workspace.get("evidence_set_digest"),
                "parent_run_id": (run.get("lineage") or {}).get("parent_run_id"),
                "status": run.get("status"),
                "created_at": run.get("created_at"),
                "finished_at": run.get("finished_at"),
                "generation_mode": workspace.get("generation_mode") or "deterministic_only",
                "provider_calls": int(workspace.get("provider_calls") or 0),
                "counts": deepcopy(run.get("counts") or {}),
                "report_quality": deepcopy(run.get("report_quality") or {}),
                "report_version_id": (latest_version or {}).get("version_id"),
                "report_status": "ready" if latest_version else (run.get("prototype") or {}).get("report_status"),
            }
        )
        live_run_ids.add(str(run["run_id"]))
    for run in state.get("portable_runs") or []:
        if run.get("topic_id") != topic_id or str(run.get("run_id") or "") in live_run_ids:
            continue
        if run.get("snapshot_id") not in snapshot_by_id:
            continue
        rows.append(deepcopy(run))
    return sorted(rows, key=lambda row: str(row.get("created_at") or ""), reverse=True)


def _topic_detail(state: dict[str, Any], topic: dict[str, Any]) -> dict[str, Any]:
    topic_id = str(topic["topic_id"])
    snapshots = sorted(
        (_decorate_snapshot(state, item) for item in state["snapshots"] if item.get("topic_id") == topic_id),
        key=lambda item: int(item.get("snapshot_version") or 0),
        reverse=True,
    )
    active = next((item for item in snapshots if item.get("active")), None)
    runs = _workspace_runs(topic_id, state)
    latest_run = runs[0] if runs else None
    reports = [
        {
            "report_version_id": run["report_version_id"],
            "run_id": run["run_id"],
            "topic_id": topic_id,
            "snapshot_id": run["snapshot_id"],
            "snapshot_version": run["snapshot_version"],
            "evidence_set_digest": run.get("evidence_set_digest"),
            "generation_mode": run.get("generation_mode"),
            "quality": run.get("report_quality") or {},
            "status": "ready",
        }
        for run in runs
        if run.get("report_version_id")
    ]
    settings_history = sorted(
        (item for item in state["settings_versions"] if item.get("topic_id") == topic_id),
        key=lambda item: int(item.get("settings_version") or 0),
        reverse=True,
    )
    events = sorted(
        (item for item in state["curation_events"] if item.get("topic_id") == topic_id),
        key=lambda item: str(item.get("event_timestamp") or ""),
        reverse=True,
    )
    changesets = sorted(
        (item for item in state["changesets"] if item.get("topic_id") == topic_id),
        key=lambda item: str(item.get("created_timestamp") or ""),
        reverse=True,
    )
    refreshes = sorted(
        (item for item in state["refresh_records"] if item.get("topic_id") == topic_id),
        key=lambda item: str(item.get("started_at") or ""),
        reverse=True,
    )
    detail = {
        **deepcopy(topic),
        "latest_run_id": (latest_run or {}).get("run_id") or topic.get("latest_run_id"),
        "latest_report_version_id": (reports[0] if reports else {}).get("report_version_id")
        or topic.get("latest_report_version_id"),
        "label": {
            "synthetic_demo": "Synthetic demonstration corpus",
            "real_open_access": "Real PMC Open Access corpus",
            "local_empty": "Local empty topic",
        }[str(topic["topic_kind"])],
        "active_snapshot": active,
        "snapshots": snapshots,
        "settings_history": deepcopy(settings_history),
        "curation_events": deepcopy(events),
        "changesets": deepcopy(changesets),
        "refreshes": deepcopy(refreshes),
        "runs": runs,
        "reports": reports,
        "coverage": {
            "included_papers": int((active or {}).get("included_count") or 0),
            "reviewed_papers": int((active or {}).get("reviewed_count") or 0),
            "evidence_spans": int((active or {}).get("evidence_count") or 0),
            "grounded_claims": int((latest_run or {}).get("counts", {}).get("grounded_claims") or 0),
            "report_count": len(reports),
        },
        "last_refresh": deepcopy(refreshes[0]) if refreshes else None,
        "needs_attention": (active is None and topic.get("topic_kind") == "real_open_access")
        or bool(refreshes and str(refreshes[0].get("status") or "").startswith("FAILED_")),
    }
    living = prototype_living_topics.decorate_topic(state, detail)
    imported_project_id = str(topic.get("imported_project_id") or "")
    if imported_project_id:
        project = next(
            (
                item
                for item in state.get("bundle_projects") or []
                if item.get("project_id") == imported_project_id
            ),
            None,
        )
        living["bundle_import"] = {
            "project_id": imported_project_id,
            "project_semantic_digest": (project or {}).get("project_semantic_digest"),
            "activation_posture": "DISABLED_PENDING_LOCAL_RECONFIRMATION",
            "automatic_execution_enabled": False,
            "full_text_embedded": False,
        }
        living["living_schedule"] = {
            **dict(living.get("living_schedule") or {}),
            "enabled": False,
            "dispatch_enabled": False,
            "activation_posture": "DISABLED_PENDING_LOCAL_RECONFIRMATION",
        }
    living["needs_attention"] = bool(
        detail["needs_attention"]
        or living["attention_summary"]["unread"]
        or living["attention_summary"]["critical"]
    )
    return prototype_research_gaps.decorate_topic(state, living)


def get_workspace() -> dict[str, Any]:
    state = _load_state()
    details = [_topic_detail(state, topic) for topic in state["topics"]]
    details.sort(key=lambda item: (bool(item.get("archived")), str(item.get("display_name") or "").lower()))
    active_topics = [item for item in details if not item.get("archived")]
    active_snapshots = [item.get("active_snapshot") for item in active_topics if item.get("active_snapshot")]
    all_runs = [run for topic in details for run in topic["runs"]]
    recent_lineage = sorted(all_runs, key=lambda row: str(row.get("created_at") or ""), reverse=True)[:8]
    living_totals = prototype_living_topics.workspace_totals(state)
    research_totals = prototype_research_gaps.workspace_totals(state)
    return {
        "schema_version": WORKSPACE_SCHEMA,
        "enabled": True,
        "external_network_default": "disabled",
        "provider_calls": 0,
        "topics": details,
        "totals": {
            "active_topics": len(active_topics),
            "archived_topics": len(details) - len(active_topics),
            "total_included_papers": sum(int(snapshot.get("included_count") or 0) for snapshot in active_snapshots),
            "real_full_text_papers": sum(
                int(snapshot.get("included_count") or 0)
                for topic, snapshot in ((item, item.get("active_snapshot")) for item in active_topics)
                if snapshot and topic.get("topic_kind") == "real_open_access"
            ),
            "synthetic_papers": sum(
                int(snapshot.get("included_count") or 0)
                for topic, snapshot in ((item, item.get("active_snapshot")) for item in active_topics)
                if snapshot and topic.get("topic_kind") == "synthetic_demo"
            ),
            "total_reviews": sum(int(snapshot.get("reviewed_count") or 0) for snapshot in active_snapshots),
            "total_evidence_spans": sum(int(snapshot.get("evidence_count") or 0) for snapshot in active_snapshots),
            "total_reports": sum(len(topic["reports"]) for topic in details),
            "topics_needing_attention": sum(1 for topic in details if topic.get("needs_attention")),
            **living_totals,
            **research_totals,
        },
        "recent_lineage": recent_lineage,
        "bundle_projects": deepcopy(state.get("bundle_projects") or []),
        "bundle_posture": {
            "schema_version": "xrml_research_project_bundle_v1",
            "automatic_execution_default": "disabled",
            "provider_default": "disabled",
            "network_default": "disabled",
            "full_text_embedded": False,
        },
    }


def get_topic(topic_id: str) -> dict[str, Any]:
    state = _load_state()
    return _topic_detail(state, _topic(state, topic_id))


def create_topic(payload: dict[str, Any]) -> dict[str, Any]:
    display_name = _safe_text(payload.get("display_name"), maximum=160, required=True)
    description = _safe_text(payload.get("description"), maximum=500)
    research_question = _safe_text(payload.get("research_question"), maximum=500)
    kind = str(payload.get("topic_kind") or "")
    source_policy = str(payload.get("source_policy") or "")
    provider_posture = "deterministic_only"
    now = iso_now_utc()
    candidate = _topic_record(
        topic_id=f"topic-{uuid.uuid4().hex}",
        display_name=display_name,
        description=description,
        research_question=research_question,
        topic_kind=kind,
        source_policy=source_policy,
        language=str(payload.get("language") or "en"),
        publication_date_cutoff=payload.get("publication_date_cutoff"),
        license_policy=str(payload.get("license_policy") or ""),
        refresh_mode=str(payload.get("refresh_mode") or ""),
        provider_posture=provider_posture,
        created_at=now,
    )

    def mutation(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        if any(topic.get("slug") == candidate["slug"] for topic in state["topics"]):
            raise PrototypeWorkspaceError("topic_slug_conflict", status_code=409)
        state["topics"].append(candidate)
        state["settings_versions"].append(_settings_version(candidate, changed_fields=[], created_at=now))
        return state, deepcopy(candidate)

    created = _mutate_state(mutation)
    return get_topic(str(created["topic_id"]))


def update_topic_settings(topic_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "display_name",
        "description",
        "research_question",
        "source_policy",
        "language",
        "publication_date_cutoff",
        "license_policy",
        "refresh_mode",
        "provider_posture",
        "archived",
    }
    updates = {key: value for key, value in payload.items() if key in allowed and value is not None}
    if not updates:
        raise PrototypeWorkspaceError("topic_settings_no_change", status_code=400)
    for key, maximum, required in (
        ("display_name", 160, True),
        ("description", 500, False),
        ("research_question", 500, False),
    ):
        if key in updates:
            updates[key] = _safe_text(updates[key], maximum=maximum, required=required)
    now = iso_now_utc()

    def mutation(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        topic = _topic(state, topic_id)
        before = _settings_payload(topic)
        candidate = {**topic, **updates}
        candidate["slug"] = slugify(candidate["display_name"])
        if any(
            item.get("topic_id") != topic["topic_id"] and item.get("slug") == candidate["slug"]
            for item in state["topics"]
        ):
            raise PrototypeWorkspaceError("topic_slug_conflict", status_code=409)
        _validate_policy(candidate)
        changed_fields = [key for key in sorted(updates) if before.get(key) != candidate.get(key)]
        if not changed_fields:
            raise PrototypeWorkspaceError("topic_settings_no_change", status_code=400)
        candidate["settings_version"] = int(topic["settings_version"]) + 1
        candidate["updated_at"] = now
        candidate["settings_digest"] = _settings_digest(candidate)
        topic.clear()
        topic.update(candidate)
        version = _settings_version(topic, changed_fields=changed_fields, created_at=now)
        version["new_snapshot_or_run_recommended"] = bool(
            {"source_policy", "publication_date_cutoff", "license_policy", "refresh_mode"} & set(changed_fields)
        )
        state["settings_versions"].append(version)
        return state, deepcopy(topic)

    updated = _mutate_state(mutation)
    return get_topic(str(updated["topic_id"]))


def preview_curation(topic_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    source_snapshot_id = _safe_identifier(payload.get("source_snapshot_id"), code="snapshot_id_invalid")
    item_id = _safe_identifier(payload.get("corpus_item_id"), code="corpus_item_id_invalid")
    requested_state = str(payload.get("requested_state") or "")
    reason_code = str(payload.get("reason_code") or "")
    note = _safe_text(payload.get("note"), maximum=200) or None
    if requested_state not in CURATION_STATES or reason_code not in CURATION_REASONS:
        raise PrototypeWorkspaceError("curation_value_invalid", status_code=400)
    now = iso_now_utc()

    def mutation(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        topic = _topic(state, topic_id)
        source = _snapshot(state, source_snapshot_id, topic_id=topic["topic_id"])
        if topic.get("active_snapshot_id") != source["snapshot_id"]:
            raise PrototypeWorkspaceError("curation_source_snapshot_not_active")
        source_items = deepcopy(source["items"])
        target = next((item for item in source_items if item.get("corpus_item_id") == item_id), None)
        if not isinstance(target, dict):
            raise PrototypeWorkspaceError("corpus_item_not_found", status_code=404)
        previous_state = str(target.get("curation_state") or "")
        if previous_state == requested_state:
            raise PrototypeWorkspaceError("curation_no_change", status_code=400)
        if requested_state == "included":
            if target.get("license_status") not in {"accepted", "synthetic_fixture"}:
                raise PrototypeWorkspaceError("license_not_accepted_for_inclusion")
            if topic.get("source_policy") == "pmc_open_access" and target.get("full_text_status") != "available":
                raise PrototypeWorkspaceError("full_text_required_for_inclusion")
        target["curation_state"] = requested_state
        target["curation_reason"] = reason_code
        target["curation_note"] = note
        projected = _snapshot_counts(source_items)
        changeset_id = f"changeset-{uuid.uuid4().hex}"
        changed_id = str(target["corpus_item_id"])
        changeset = {
            "changeset_id": changeset_id,
            "kind": "curation",
            "topic_id": topic["topic_id"],
            "source_snapshot_id": source["snapshot_id"],
            "proposed_parent_snapshot_id": source["snapshot_id"],
            "proposed_snapshot_version": int(source["snapshot_version"]) + 1,
            "settings_version": topic["settings_version"],
            "settings_digest": topic["settings_digest"],
            "added_item_ids": [],
            "removed_item_ids": [changed_id] if requested_state == "excluded" else [],
            "included_item_ids": [
                str(item["corpus_item_id"]) for item in source_items if item.get("curation_state") == "included"
            ],
            "excluded_item_ids": [
                str(item["corpus_item_id"]) for item in source_items if item.get("curation_state") == "excluded"
            ],
            "held_item_ids": [
                str(item["corpus_item_id"]) for item in source_items if item.get("curation_state") == "hold"
            ],
            "restored_item_ids": [changed_id] if requested_state == "included" else [],
            "license_blocked_item_ids": [],
            "stale_item_ids": [
                str(item["corpus_item_id"]) for item in source_items if item.get("stale_status") != "current"
            ],
            "unchanged_item_count": max(0, len(source_items) - 1),
            "projected_review_count": projected["reviewed_count"],
            "projected_evidence_count": projected["evidence_count"],
            "projected_report_impact": "none_historical_reports_immutable",
            "new_report_run_recommended": True,
            "created_timestamp": now,
            "applied": False,
            "failed": False,
            "discarded": False,
            "resulting_snapshot_id": None,
            "applied_at": None,
            "failure_category": None,
            "proposed_items": source_items,
            "operations": [
                {
                    "corpus_item_id": changed_id,
                    "previous_state": previous_state,
                    "requested_state": requested_state,
                    "reason_code": reason_code,
                }
            ],
            "no_accepted_change": False,
        }
        changeset["changeset_digest"] = _changeset_digest(changeset)
        event = {
            "event_id": f"curation-{uuid.uuid4().hex}",
            "topic_id": topic["topic_id"],
            "source_snapshot_id": source["snapshot_id"],
            "target_item_identity": changed_id,
            "previous_state": previous_state,
            "requested_state": requested_state,
            "reason_code": reason_code,
            "note": note,
            "event_timestamp": now,
            "actor_mode": LOCAL_ACTOR,
            "resulting_change_set_id": changeset_id,
        }
        event["event_digest"] = _event_digest(event)
        state["changesets"].append(changeset)
        state["curation_events"].append(event)
        return state, deepcopy(changeset)

    return _mutate_state(mutation)


def apply_changeset(topic_id: str, changeset_id: str, *, confirmed: bool) -> dict[str, Any]:
    if not confirmed:
        raise PrototypeWorkspaceError("changeset_confirmation_required", status_code=400)
    safe_changeset_id = _safe_identifier(changeset_id, code="changeset_id_invalid")
    now = iso_now_utc()

    def mutation(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        topic = _topic(state, topic_id)
        changeset = next(
            (item for item in state["changesets"] if item.get("changeset_id") == safe_changeset_id),
            None,
        )
        if not isinstance(changeset, dict):
            raise PrototypeWorkspaceError("changeset_not_found", status_code=404)
        if changeset.get("topic_id") != topic["topic_id"]:
            raise PrototypeWorkspaceError("cross_topic_changeset_reference")
        if changeset.get("applied") or changeset.get("discarded"):
            raise PrototypeWorkspaceError("changeset_not_applicable")
        if changeset.get("no_accepted_change"):
            raise PrototypeWorkspaceError("changeset_no_accepted_change")
        if topic.get("active_snapshot_id") != changeset.get("source_snapshot_id"):
            raise PrototypeWorkspaceError("changeset_source_snapshot_stale")
        if topic.get("settings_version") != changeset.get("settings_version") or topic.get(
            "settings_digest"
        ) != changeset.get("settings_digest"):
            raise PrototypeWorkspaceError("changeset_settings_stale")
        source = _snapshot(state, str(changeset["source_snapshot_id"]), topic_id=topic["topic_id"])
        new_snapshot = _new_snapshot(
            topic=topic,
            version=int(changeset["proposed_snapshot_version"]),
            parent_snapshot_id=source["snapshot_id"],
            query_id=str(source["query_id"]),
            query_digest=str(source["query_digest"]),
            items=list(changeset.get("proposed_items") or []),
            created_by_mode=LOCAL_ACTOR,
            refresh_id=changeset.get("refresh_id"),
            source_run_id=source.get("source_run_id"),
            change_summary={
                "added": len(changeset.get("added_item_ids") or []),
                "removed": len(changeset.get("removed_item_ids") or []),
                "held": len(changeset.get("held_item_ids") or []),
                "restored": len(changeset.get("restored_item_ids") or []),
            },
            created_at=now,
        )
        state["snapshots"].append(new_snapshot)
        topic["active_snapshot_id"] = new_snapshot["snapshot_id"]
        topic["updated_at"] = now
        changeset["applied"] = True
        changeset["resulting_snapshot_id"] = new_snapshot["snapshot_id"]
        changeset["applied_at"] = now
        if changeset.get("refresh_id"):
            refresh = next(
                (item for item in state["refresh_records"] if item.get("refresh_id") == changeset["refresh_id"]),
                None,
            )
            if isinstance(refresh, dict):
                refresh["applied_snapshot_id"] = new_snapshot["snapshot_id"]
        return state, _decorate_snapshot(state, new_snapshot)

    return _mutate_state(mutation)


def discard_changeset(topic_id: str, changeset_id: str) -> dict[str, Any]:
    safe_changeset_id = _safe_identifier(changeset_id, code="changeset_id_invalid")

    def mutation(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        topic = _topic(state, topic_id)
        changeset = next(
            (item for item in state["changesets"] if item.get("changeset_id") == safe_changeset_id),
            None,
        )
        if not isinstance(changeset, dict):
            raise PrototypeWorkspaceError("changeset_not_found", status_code=404)
        if changeset.get("topic_id") != topic["topic_id"]:
            raise PrototypeWorkspaceError("cross_topic_changeset_reference")
        if changeset.get("applied"):
            raise PrototypeWorkspaceError("changeset_already_applied")
        changeset["discarded"] = True
        return state, deepcopy(changeset)

    return _mutate_state(mutation)


def compare_snapshots(topic_id: str, from_snapshot_id: str, to_snapshot_id: str) -> dict[str, Any]:
    state = _load_state()
    topic = _topic(state, topic_id)
    before = _snapshot(state, from_snapshot_id, topic_id=topic["topic_id"])
    after = _snapshot(state, to_snapshot_id, topic_id=topic["topic_id"])
    before_items = {str(item["corpus_item_id"]): item for item in before["items"]}
    after_items = {str(item["corpus_item_id"]): item for item in after["items"]}
    added = sorted(set(after_items) - set(before_items))
    missing = sorted(set(before_items) - set(after_items))
    held = sorted(
        item_id
        for item_id, item in after_items.items()
        if item.get("curation_state") == "hold"
        and (before_items.get(item_id) or {}).get("curation_state") != "hold"
    )
    removed = sorted(
        set(missing)
        | {
            item_id
            for item_id, item in after_items.items()
            if item.get("curation_state") == "excluded"
            and (before_items.get(item_id) or {}).get("curation_state") != "excluded"
        }
    )
    changed_digests = sorted(
        item_id
        for item_id in set(before_items) & set(after_items)
        if before_items[item_id].get("source_content_digest") != after_items[item_id].get("source_content_digest")
    )
    unchanged = sorted(
        item_id
        for item_id in set(before_items) & set(after_items)
        if before_items[item_id].get("curation_state") == after_items[item_id].get("curation_state")
        and before_items[item_id].get("source_content_digest") == after_items[item_id].get("source_content_digest")
    )
    return {
        "topic_id": topic["topic_id"],
        "from_snapshot": _decorate_snapshot(state, before),
        "to_snapshot": _decorate_snapshot(state, after),
        "added_papers": added,
        "removed_papers": removed,
        "held_papers": held,
        "unchanged_papers": unchanged,
        "changed_source_content_digests": changed_digests,
        "review_count_delta": int(after["reviewed_count"]) - int(before["reviewed_count"]),
        "evidence_count_delta": int(after["evidence_count"]) - int(before["evidence_count"]),
        "report_availability": {
            "from": bool(before.get("source_run_id")),
            "to": bool(after.get("source_run_id")),
        },
    }


def _attach_workspace_to_run(
    run_id: str,
    *,
    topic: dict[str, Any],
    snapshot: dict[str, Any],
    evidence_set_digest: str | None,
    generation_mode: str,
    provider_calls: int = 0,
) -> dict[str, Any]:
    run = load_run_record(run_id)
    existing = run.get("workspace") if isinstance(run.get("workspace"), dict) else {}
    if existing and (
        existing.get("topic_id") != topic["topic_id"] or existing.get("snapshot_id") != snapshot["snapshot_id"]
    ):
        raise PrototypeWorkspaceError("run_workspace_linkage_conflict")
    run["workspace"] = {
        "schema_version": WORKSPACE_SCHEMA,
        "topic_id": topic["topic_id"],
        "snapshot_id": snapshot["snapshot_id"],
        "settings_version": snapshot["settings_version"],
        "settings_digest": snapshot["settings_digest"],
        "source_manifest_digest": snapshot["manifest_digest"],
        "evidence_set_digest": evidence_set_digest,
        "generation_mode": generation_mode,
        "provider_calls": provider_calls,
    }
    save_run_record(run)
    return run


def register_real_corpus_run(run_id: str, *, activate_if_empty: bool = True) -> dict[str, Any]:
    record = prototype_real_corpus.load_public_record(run_id)
    if record.get("status") != "complete" or int(record.get("selected_article_count") or 0) < 3:
        raise PrototypeWorkspaceError("real_corpus_run_incomplete")
    claims = list(record.get("claims") or [])
    evidence_rows = list(record.get("evidence") or [])
    items = []
    for article in record.get("articles") or []:
        pmcid = str(article.get("pmcid") or "")
        if not pmcid:
            raise PrototypeWorkspaceError("real_corpus_article_identity_missing")
        items.append(
            {
                "corpus_item_id": pmcid,
                "source_type": "pmc_open_access",
                "source_identity": pmcid,
                "pmcid": pmcid,
                "pmid": article.get("pmid"),
                "doi": article.get("doi"),
                "work_identity_digest": article.get("article_identity_digest") or _digest({"pmcid": pmcid}),
                "title": _safe_text(article.get("title"), maximum=300, required=True),
                "publication_date": article.get("publication_date"),
                "license_status": "accepted" if article.get("rights_accepted") else "rejected",
                "license_type": article.get("license_type"),
                "full_text_status": "available" if article.get("full_text_retrieved") else "missing",
                "review_status": "reviewed" if article.get("review_id") else "not_reviewed",
                "review_id": article.get("review_id"),
                "evidence_ids": sorted(str(value) for value in article.get("evidence_ids") or []),
                "claim_ids": sorted(
                    str(claim.get("claim_id"))
                    for claim in claims
                    if pmcid in {str(value) for value in claim.get("paper_ids") or []}
                    and claim.get("claim_id")
                ),
                "source_paragraph_ids": sorted(
                    {
                        str(evidence.get("paragraph_id"))
                        for evidence in evidence_rows
                        if evidence.get("evidence_id") in set(article.get("evidence_ids") or [])
                        and evidence.get("paragraph_id")
                    }
                ),
                "report_version_ids": [],
                "evidence_count": len(article.get("evidence_ids") or []),
                "curation_state": "included",
                "curation_reason": "relevant_core",
                "source_content_digest": article.get("content_digest"),
                "stale_status": "current",
            }
        )
    state = _load_state()
    topic = _topic(state, REAL_TOPIC_ID)
    preview_snapshot = _new_snapshot(
        topic=topic,
        version=1,
        parent_snapshot_id=None,
        query_id=prototype_real_corpus.QUERY_ID,
        query_digest=prototype_real_corpus.QUERY_SHA256,
        items=items,
        created_by_mode="OFFICIAL_SOURCE_ACQUISITION",
        refresh_id=None,
        source_run_id=run_id,
        change_summary={"added": len(items), "removed": 0, "held": 0, "restored": 0},
        created_at=record.get("completed_at") or iso_now_utc(),
    )
    evidence_set_digest = _digest(
        sorted(
            (
                {
                    "evidence_id": evidence.get("evidence_id"),
                    "paper_id": evidence.get("paper_id"),
                    "source_content_digest": evidence.get("source_content_digest"),
                }
                for evidence in record.get("evidence") or []
            ),
            key=lambda item: str(item.get("evidence_id") or ""),
        )
    )
    _attach_workspace_to_run(
        run_id,
        topic=topic,
        snapshot=preview_snapshot,
        evidence_set_digest=evidence_set_digest,
        generation_mode=prototype_real_corpus.REVIEW_METHOD,
    )
    versions = report_versions.list_report_versions(run_id).get("items") or []
    if not versions:
        created_version = report_versions.snapshot_completed_report_if_available(run_id)
        versions = [created_version] if created_version else []
    report_version_id = (versions[-1] if versions else {}).get("version_id") or (
        record.get("report") or {}
    ).get("report_version_id")
    if report_version_id:
        for item in preview_snapshot["items"]:
            item["report_version_ids"] = [report_version_id]

    def mutation(current: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        current_topic = _topic(current, REAL_TOPIC_ID)
        existing_for_run = next(
            (item for item in current["snapshots"] if item.get("source_run_id") == run_id),
            None,
        )
        if isinstance(existing_for_run, dict):
            return current, _decorate_snapshot(current, existing_for_run)
        active_id = current_topic.get("active_snapshot_id")
        if active_id:
            active = _snapshot(current, str(active_id), topic_id=REAL_TOPIC_ID)
            if active.get("manifest_digest") == preview_snapshot.get("manifest_digest"):
                current_topic["latest_run_id"] = run_id
                current_topic["latest_report_version_id"] = report_version_id
                return current, _decorate_snapshot(current, active)
            raise PrototypeWorkspaceError("real_corpus_active_snapshot_requires_preview")
        if not activate_if_empty:
            raise PrototypeWorkspaceError("real_corpus_activation_not_authorized")
        current["snapshots"].append(preview_snapshot)
        current_topic["active_snapshot_id"] = preview_snapshot["snapshot_id"]
        current_topic["latest_run_id"] = run_id
        current_topic["latest_report_version_id"] = report_version_id
        current_topic["updated_at"] = iso_now_utc()
        return current, _decorate_snapshot(current, preview_snapshot)

    return _mutate_state(mutation)


def acquire_initial_real_corpus(*, confirmed: bool, network_enabled: bool, owner_user_id: str | None) -> dict[str, Any]:
    if not confirmed or not network_enabled:
        raise PrototypeWorkspaceError("real_corpus_confirmation_required", status_code=400)
    current = get_topic(REAL_TOPIC_ID)
    if current.get("active_snapshot_id"):
        raise PrototypeWorkspaceError("real_corpus_already_initialized")
    summary = prototype_real_corpus.start_real_corpus_run(owner_user_id=owner_user_id)
    snapshot = register_real_corpus_run(str(summary["run_id"]), activate_if_empty=True)
    return {"run": summary, "snapshot": snapshot, "provider_calls": 0}


def start_topic_run(
    topic_id: str,
    *,
    snapshot_id: str | None,
    actor: ResourceActor,
    owner_user_id: str | None,
) -> dict[str, Any]:
    state = _load_state()
    topic = _topic(state, topic_id)
    _require_bundle_execution_allowed(topic)
    selected_id = snapshot_id or topic.get("active_snapshot_id")
    if not selected_id:
        raise PrototypeWorkspaceError("topic_active_snapshot_required")
    snapshot = _snapshot(state, str(selected_id), topic_id=topic["topic_id"])
    included = [item for item in snapshot["items"] if item.get("curation_state") == "included"]
    if not included:
        raise PrototypeWorkspaceError("snapshot_has_no_included_items")
    if any(item.get("license_status") not in {"accepted", "synthetic_fixture"} for item in included):
        raise PrototypeWorkspaceError("snapshot_license_boundary_rejected")
    if topic.get("topic_kind") == "real_open_access":
        if len(included) < 3 or any(item.get("full_text_status") != "available" for item in included):
            raise PrototypeWorkspaceError("trusted_real_snapshot_requires_three_full_text_items")
        source_run_id = str(snapshot.get("source_run_id") or "")
        if not source_run_id:
            raise PrototypeWorkspaceError("snapshot_source_run_missing")
        topic_runs = _workspace_runs(topic["topic_id"], state)
        parent_run_id = topic_runs[0]["run_id"] if topic_runs else source_run_id
        run = prototype_real_corpus.create_curated_real_corpus_run(
            source_run_id=source_run_id,
            selected_pmcids=[str(item["pmcid"]) for item in included],
            owner_user_id=owner_user_id,
            workspace_context={
                "schema_version": WORKSPACE_SCHEMA,
                "topic_id": topic["topic_id"],
                "snapshot_id": snapshot["snapshot_id"],
                "settings_version": snapshot["settings_version"],
                "settings_digest": snapshot["settings_digest"],
                "source_manifest_digest": snapshot["manifest_digest"],
                "parent_run_id": parent_run_id,
            },
        )
    elif topic.get("topic_kind") == "synthetic_demo":
        run = prototype.start_prototype_run(
            actor=actor,
            owner_user_id=owner_user_id,
            selected_paper_ids=[
                str(item["source_identity"]).removeprefix("synthetic:")
                for item in included
            ],
            workspace_context={
                "schema_version": WORKSPACE_SCHEMA,
                "topic_id": topic["topic_id"],
                "snapshot_id": snapshot["snapshot_id"],
                "settings_version": snapshot["settings_version"],
                "settings_digest": snapshot["settings_digest"],
                "source_manifest_digest": snapshot["manifest_digest"],
                "evidence_set_digest": None,
                "generation_mode": "deterministic_only",
                "provider_calls": 0,
            },
        )
    else:
        raise PrototypeWorkspaceError("local_empty_topic_has_no_runnable_corpus")
    run_id = str(run["run_id"])

    def mutation(current: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        current_topic = _topic(current, topic["topic_id"])
        current_topic["latest_run_id"] = run_id
        current_topic["latest_report_version_id"] = run.get("report_version_id")
        current_topic["updated_at"] = iso_now_utc()
        return current, deepcopy(run)

    return _mutate_state(mutation)


def _refresh_record(
    topic: dict[str, Any],
    source_snapshot: dict[str, Any],
    *,
    schedule_id: str | None = None,
    schedule_version: int | None = None,
    due_at: str | None = None,
    idempotency_key: str | None = None,
    execution_mode: str = "manual",
    network_policy: str = "manual_official_source_only",
) -> dict[str, Any]:
    now = iso_now_utc()
    refresh = {
        "refresh_id": f"refresh-{uuid.uuid4().hex}",
        "topic_id": topic["topic_id"],
        "schedule_id": schedule_id,
        "schedule_version": schedule_version,
        "due_at": due_at,
        "idempotency_key": idempotency_key,
        "execution_mode": execution_mode,
        "source_snapshot_id": source_snapshot["snapshot_id"],
        "active_snapshot_id_at_start": source_snapshot["snapshot_id"],
        "query_id": source_snapshot["query_id"],
        "query_digest": source_snapshot["query_digest"],
        "source_policy": topic["source_policy"],
        "network_policy": network_policy,
        "started_at": now,
        "completed_at": None,
        "status": "RUNNING",
        "request_count": 0,
        "downloaded_bytes": 0,
        "candidate_count": 0,
        "license_eligible_count": 0,
        "added_work_count": 0,
        "removed_unavailable_count": 0,
        "unavailable_work_count": 0,
        "metadata_changed_count": 0,
        "content_changed_count": 0,
        "full_text_changed_count": 0,
        "license_changed_count": 0,
        "article_status_changed_count": 0,
        "unchanged_count": 0,
        "no_material_change": False,
        "failure_category": None,
        "changeset_id": None,
        "briefing_id": None,
        "applied_snapshot_id": None,
        "retries": 0,
        "non_allowlisted_requests": 0,
        "refresh_digest": None,
    }
    refresh["refresh_digest"] = _digest({key: value for key, value in refresh.items() if key != "refresh_digest"})
    return refresh


def check_for_corpus_updates(
    topic_id: str,
    *,
    confirmed: bool,
    probe: Callable[[], dict[str, Any]] | None = None,
    schedule_id: str | None = None,
    schedule_version: int | None = None,
    due_at: str | None = None,
    idempotency_key: str | None = None,
    execution_mode: str = "manual",
    network_policy: str = "manual_official_source_only",
) -> dict[str, Any]:
    if not confirmed:
        raise PrototypeWorkspaceError("refresh_confirmation_required", status_code=400)
    state = _load_state()
    topic = _topic(state, topic_id)
    _require_bundle_execution_allowed(topic)
    if topic.get("source_policy") != "pmc_open_access":
        raise PrototypeWorkspaceError("topic_refresh_not_eligible")
    if schedule_id is None and topic.get("refresh_mode") != "manual_only":
        raise PrototypeWorkspaceError("topic_refresh_not_eligible")
    if schedule_id is not None:
        schedule = prototype_living_topics._schedule_version(
            state,
            schedule_id,
            int(schedule_version or 0),
        )
        if (
            not schedule
            or schedule.get("schedule_id") != schedule_id
            or int(schedule.get("schedule_version") or 0) != int(schedule_version or 0)
            or not schedule.get("enabled")
            or schedule.get("network_policy") != "scheduled_official_source_only"
            or network_policy != "scheduled_official_source_only"
            or topic.get("archived")
        ):
            raise PrototypeWorkspaceError("living_schedule_refresh_boundary_invalid")
    if not topic.get("active_snapshot_id"):
        raise PrototypeWorkspaceError("refresh_source_snapshot_required")
    source = _snapshot(state, str(topic["active_snapshot_id"]), topic_id=topic["topic_id"])
    refresh = _refresh_record(
        topic,
        source,
        schedule_id=schedule_id,
        schedule_version=schedule_version,
        due_at=due_at,
        idempotency_key=idempotency_key,
        execution_mode=execution_mode,
        network_policy=network_policy,
    )

    def begin(current: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        current_topic = _topic(current, topic["topic_id"])
        if current_topic.get("active_snapshot_id") != source["snapshot_id"]:
            raise PrototypeWorkspaceError("refresh_source_snapshot_stale")
        if idempotency_key:
            existing = next(
                (row for row in current["refresh_records"] if row.get("idempotency_key") == idempotency_key),
                None,
            )
            if existing:
                return current, {"existing": True, "refresh": deepcopy(existing)}
        if any(
            row.get("topic_id") == topic["topic_id"] and row.get("status") in {"PENDING", "RUNNING"}
            for row in current["refresh_records"]
        ):
            raise PrototypeWorkspaceError("living_topic_refresh_overlap")
        current["refresh_records"].append(refresh)
        return current, {"existing": False, "refresh": deepcopy(refresh)}

    begun = _mutate_state(begin)
    if begun["existing"]:
        existing_refresh = begun["refresh"]
        existing_changeset = next(
            (
                row for row in _load_state()["changesets"]
                if row.get("changeset_id") == existing_refresh.get("changeset_id")
            ),
            None,
        )
        return {
            "refresh": existing_refresh,
            "changeset": deepcopy(existing_changeset) if existing_changeset else None,
            "duplicate": True,
        }
    network_client: prototype_real_corpus.PmcNetworkClient | None = None
    try:
        if probe is None:
            network_client = prototype_real_corpus.PmcNetworkClient()
            observation = prototype_real_corpus.check_for_corpus_updates(client=network_client)
        else:
            observation = probe()
    except Exception as exc:  # Stable category only; no source exception text is persisted or returned.
        code = getattr(exc, "code", "official_source_unavailable")
        request_count = network_client.request_count if network_client is not None else 0
        downloaded_bytes = network_client.total_downloaded_bytes if network_client is not None else 0

        def fail(current: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            record = next(item for item in current["refresh_records"] if item["refresh_id"] == refresh["refresh_id"])
            record.update(
                {
                    "completed_at": iso_now_utc(),
                    "status": "FAILED_SOURCE_UNAVAILABLE",
                    "failure_category": str(code)[:80],
                    "request_count": request_count,
                    "downloaded_bytes": downloaded_bytes,
                }
            )
            record["refresh_digest"] = _digest(
                {key: value for key, value in record.items() if key != "refresh_digest"}
            )
            return current, deepcopy(record)

        return _mutate_state(fail)

    observed = {str(item.get("pmcid") or ""): item for item in observation.get("articles") or [] if item.get("pmcid")}
    current_items = {str(item.get("pmcid") or ""): item for item in source["items"] if item.get("pmcid")}
    added = sorted(set(observed) - set(current_items))
    removed = sorted(set(current_items) - set(observed))
    unchanged = sorted(set(current_items) & set(observed))
    metadata_changed = sorted(
        pmcid
        for pmcid in unchanged
        if any(
            str(current_items[pmcid].get(field) or "") != str(observed[pmcid].get(field) or "")
            for field in ("title", "publication_date", "license_type")
        )
    )
    full_text_changed = sorted(
        pmcid
        for pmcid in unchanged
        if observed[pmcid].get("content_digest")
        and str(current_items[pmcid].get("source_content_digest") or "")
        != str(observed[pmcid].get("content_digest") or "")
    )
    license_changed = sorted(
        pmcid
        for pmcid in unchanged
        if observed[pmcid].get("rights_accepted") is not None
        and str(current_items[pmcid].get("license_status") or "")
        != ("accepted" if observed[pmcid].get("rights_accepted") else "rejected")
    )
    article_status_changed = sorted(
        pmcid
        for pmcid in unchanged
        if observed[pmcid].get("article_status")
        and str(current_items[pmcid].get("article_status") or "current")
        != str(observed[pmcid].get("article_status") or "current")
    )
    # Newly discovered metadata is never trusted as full text during refresh preview.
    # It remains blocked until an explicit later acquisition validates its body artifact.
    no_accepted_change = not metadata_changed
    no_material_change = not any(
        (added, removed, metadata_changed, full_text_changed, license_changed, article_status_changed)
    )
    changeset_id = f"changeset-{uuid.uuid4().hex}"
    proposed_items = deepcopy(source["items"])
    for item in proposed_items:
        pmcid = str(item.get("pmcid") or "")
        if pmcid in metadata_changed:
            item["title"] = _safe_text(observed[pmcid].get("title"), maximum=300, required=True)
            item["publication_date"] = observed[pmcid].get("publication_date")
            item["license_type"] = observed[pmcid].get("license_type")
        if pmcid in full_text_changed:
            item["source_content_digest"] = observed[pmcid].get("content_digest")
        if pmcid in license_changed:
            item["license_status"] = "accepted" if observed[pmcid].get("rights_accepted") else "rejected"
        if pmcid in article_status_changed:
            item["article_status"] = observed[pmcid].get("article_status")
    observation_items = [
        deepcopy(item)
        for item in proposed_items
        if str(item.get("pmcid") or "") in observed
    ]
    for pmcid in added:
        article = observed[pmcid]
        observation_items.append(
            {
                "corpus_item_id": pmcid,
                "source_type": "pmc_open_access",
                "source_identity": pmcid,
                "pmcid": pmcid,
                "pmid": article.get("pmid"),
                "doi": article.get("doi"),
                "work_identity_digest": article.get("article_identity_digest") or _digest({"pmcid": pmcid}),
                "title": _safe_text(article.get("title"), maximum=300, required=True),
                "publication_date": article.get("publication_date"),
                "license_status": "accepted" if article.get("rights_accepted") else "rejected",
                "license_type": article.get("license_type"),
                "full_text_status": "missing",
                "review_status": "not_reviewed",
                "evidence_count": 0,
                "curation_state": "hold",
                "curation_reason": "insufficient_full_text",
                "source_content_digest": article.get("content_digest"),
                "article_status": article.get("article_status") or "current",
                "stale_status": "new_source_unreviewed",
                "review_id": None,
                "evidence_ids": [],
                "claim_ids": [],
                "report_version_ids": [],
                "source_paragraph_ids": [],
            }
        )
    changeset = {
        "changeset_id": changeset_id,
        "kind": "refresh",
        "refresh_id": refresh["refresh_id"],
        "topic_id": topic["topic_id"],
        "source_snapshot_id": source["snapshot_id"],
        "proposed_parent_snapshot_id": source["snapshot_id"],
        "proposed_snapshot_version": int(source["snapshot_version"]) + 1,
        "settings_version": topic["settings_version"],
        "settings_digest": topic["settings_digest"],
        "added_item_ids": added,
        "removed_item_ids": [],
        "included_item_ids": [
            str(item["corpus_item_id"]) for item in proposed_items if item.get("curation_state") == "included"
        ],
        "excluded_item_ids": [
            str(item["corpus_item_id"]) for item in proposed_items if item.get("curation_state") == "excluded"
        ],
        "held_item_ids": [
            str(item["corpus_item_id"]) for item in proposed_items if item.get("curation_state") == "hold"
        ],
        "restored_item_ids": [],
        "license_blocked_item_ids": added,
        "stale_item_ids": removed,
        "unchanged_item_count": len(unchanged) - len(metadata_changed),
        "projected_review_count": int(source["reviewed_count"]),
        "projected_evidence_count": int(source["evidence_count"]),
        "projected_report_impact": "none_historical_reports_immutable",
        "new_report_run_recommended": bool(metadata_changed),
        "created_timestamp": iso_now_utc(),
        "applied": False,
        "failed": False,
        "discarded": False,
        "resulting_snapshot_id": None,
        "applied_at": None,
        "failure_category": None,
        "proposed_items": proposed_items,
        "observation_items": observation_items,
        "operations": (
            [{"paper_added": pmcid} for pmcid in added]
            + [{"paper_unavailable": pmcid} for pmcid in removed]
            + [{"metadata_changed": pmcid} for pmcid in metadata_changed]
            + [{"full_text_changed": pmcid} for pmcid in full_text_changed]
            + [{"license_changed": pmcid} for pmcid in license_changed]
            + [{"article_status_changed": pmcid} for pmcid in article_status_changed]
        ),
        "no_accepted_change": no_accepted_change,
    }
    changeset["changeset_digest"] = _changeset_digest(changeset)

    def finish(current: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        current_topic = _topic(current, topic["topic_id"])
        if current_topic.get("active_snapshot_id") != source["snapshot_id"]:
            raise PrototypeWorkspaceError("refresh_source_snapshot_stale")
        record = next(item for item in current["refresh_records"] if item["refresh_id"] == refresh["refresh_id"])
        network = observation.get("network") or {}
        record.update(
            {
                "completed_at": iso_now_utc(),
                "status": "NO_MATERIAL_CHANGE" if no_material_change else "PREVIEW_READY",
                "request_count": int(network.get("request_count") or 0),
                "downloaded_bytes": int(network.get("total_downloaded_bytes") or 0),
                "candidate_count": int(observation.get("candidate_count") or len(observed)),
                "license_eligible_count": int(observation.get("license_eligible_count") or len(observed)),
                "added_work_count": len(added),
                "removed_unavailable_count": len(removed),
                "unavailable_work_count": len(removed),
                "metadata_changed_count": len(metadata_changed),
                "content_changed_count": len(full_text_changed),
                "full_text_changed_count": len(full_text_changed),
                "license_changed_count": len(license_changed),
                "article_status_changed_count": len(article_status_changed),
                "unchanged_count": len(unchanged) - len(metadata_changed),
                "no_material_change": no_material_change,
                "failure_category": None,
                "changeset_id": changeset_id,
                "retries": int(network.get("retries") or 0),
                "non_allowlisted_requests": int(network.get("non_allowlisted_requests") or 0),
            }
        )
        record["refresh_digest"] = _digest(
            {key: value for key, value in record.items() if key != "refresh_digest"}
        )
        current["changesets"].append(changeset)
        return current, {"refresh": deepcopy(record), "changeset": deepcopy(changeset)}

    return _mutate_state(finish)
