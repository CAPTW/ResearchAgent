"""Deterministic, offline-safe Prototype research project bundles.

The bundle is a portable projection, not a backup.  It carries inspectable
research metadata, evidence excerpts, reports, lineage, and owner decisions.
It deliberately excludes full-text bodies, raw provider material, runtime
databases, credentials, and filesystem paths.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import unicodedata
import uuid
import zipfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from app.repositories import paper_reviews
from app.repositories.runs import load_run_record
from app.repositories.shared_state import load_json_state, mutate_json_state
from app.services import (
    prototype,
    prototype_living_topics,
    prototype_real_corpus,
    prototype_research_gaps,
    prototype_workspace,
    report_versions,
)

BUNDLE_SCHEMA = "xrml_research_project_bundle_v1"
BUNDLE_EXTENSION = ".xrmlbundle"
SNAPSHOT_STRATEGY = "normalized_portable_projection"
IMPORTED_POSTURE = "DISABLED_PENDING_LOCAL_RECONFIRMATION"
SOURCE_PROTOTYPE_VERSION = "v0.7"

MAX_COMPRESSED_BYTES = 25 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_ENTRY_BYTES = 10 * 1024 * 1024
MAX_ENTRY_COUNT = 2_000
MAX_COMPRESSION_RATIO = 20
MAX_JSONL_LINE_BYTES = 1024 * 1024
MAX_NESTING_DEPTH = 32
MAX_TOTAL_RECORDS = 100_000

RECORD_PATHS = {
    "topics": "records/topics.jsonl",
    "topic_settings": "records/topic_settings.jsonl",
    "corpus_snapshots": "records/corpus_snapshots.jsonl",
    "corpus_items": "records/corpus_items.jsonl",
    "rights": "records/rights.jsonl",
    "full_text_artifact_descriptors": "records/full_text_artifact_descriptors.jsonl",
    "reviews": "records/reviews.jsonl",
    "evidence": "records/evidence.jsonl",
    "claims": "records/claims.jsonl",
    "runs": "records/runs.jsonl",
    "reports": "records/reports.jsonl",
    "report_versions": "records/report_versions.jsonl",
    "provider_execution_summaries": "records/provider_execution_summaries.jsonl",
    "schedules": "records/schedules.jsonl",
    "schedule_versions": "records/schedule_versions.jsonl",
    "refreshes": "records/refreshes.jsonl",
    "change_events": "records/change_events.jsonl",
    "impact_records": "records/impact_records.jsonl",
    "briefings": "records/briefings.jsonl",
    "attention_events": "records/attention_events.jsonl",
    "study_profiles": "records/study_profiles.jsonl",
    "evidence_matrices": "records/evidence_matrices.jsonl",
    "gap_analyses": "records/gap_analyses.jsonl",
    "gaps": "records/gaps.jsonl",
    "recommendations": "records/recommendations.jsonl",
    "recommendation_decisions": "records/recommendation_decisions.jsonl",
    "topic_origins": "records/topic_origins.jsonl",
}
PATH_TO_RECORD_TYPE = {path: record_type for record_type, path in RECORD_PATHS.items()}
REQUIRED_PATHS = {"manifest.json", "project.json", *RECORD_PATHS.values()}
RECORD_COUNT_LIMITS = {record_type: 20_000 for record_type in RECORD_PATHS}
RECORD_COUNT_LIMITS.update(
    {
        "topics": 256,
        "topic_settings": 4_096,
        "corpus_snapshots": 4_096,
        "runs": 10_000,
        "reports": 10_000,
        "report_versions": 10_000,
        "schedules": 4_096,
        "schedule_versions": 10_000,
        "provider_execution_summaries": 10_000,
    }
)

_ID_KEYS = (
    "snapshot_id",
    "corpus_item_id",
    "rights_record_id",
    "descriptor_id",
    "review_id",
    "evidence_id",
    "claim_id",
    "run_id",
    "report_id",
    "report_version_id",
    "version_id",
    "execution_id",
    "schedule_version_id",
    "schedule_id",
    "refresh_id",
    "event_id",
    "changeset_id",
    "impact_id",
    "briefing_id",
    "attention_event_id",
    "study_profile_id",
    "profile_id",
    "matrix_id",
    "analysis_id",
    "gap_id",
    "recommendation_id",
    "decision_event_id",
    "decision_id",
    "origin_id",
    "comparison_id",
    "scope_id",
    "settings_digest",
    "topic_id",
)
_DROP_KEYS = {
    "activation_token",
    "api_key",
    "artifacts",
    "artifact_availability",
    "artifact_references",
    "artifact_path",
    "command",
    "content_reference",
    "cookie",
    "credential",
    "download_available",
    "full_text",
    "full_text_artifact_path",
    "jats_xml",
    "parsed_body",
    "password",
    "raw_output",
    "raw_request",
    "raw_response",
    "request_records",
    "secret",
    "session",
    "snapshot_path",
    "source_artifact_path",
    "storage_path",
    "token",
    "traceback",
    "workspace_path",
    "imported_project_id",
    "automatic_execution_posture",
}
_DROP_SUFFIXES = ("_path", "_directory", "_dir")
_FORBIDDEN_ARCHIVE_SUFFIXES = {
    ".7z",
    ".bat",
    ".cmd",
    ".com",
    ".dll",
    ".exe",
    ".jar",
    ".js",
    ".msi",
    ".ps1",
    ".py",
    ".rar",
    ".sh",
    ".tar",
    ".vbs",
    ".zip",
}
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")

_MEMORY_LOCK = threading.RLock()
_EXPORT_PREVIEWS: dict[str, dict[str, Any]] = {}
_EXPORTS: dict[str, bytes] = {}
_IMPORTS: dict[str, dict[str, Any]] = {}


class PrototypeProjectBundleError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 409) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def _canonical_bytes(value: Any) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise PrototypeProjectBundleError("bundle_value_not_canonical") from exc
    return text.encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest(value: Any) -> str:
    return _sha256_bytes(_canonical_bytes(value))


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PrototypeProjectBundleError("bundle_json_duplicate_key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise PrototypeProjectBundleError("bundle_json_non_finite_number")


def _parse_json(data: bytes) -> Any:
    if data.startswith(b"\xef\xbb\xbf"):
        raise PrototypeProjectBundleError("bundle_json_bom_forbidden")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PrototypeProjectBundleError("bundle_json_utf8_required") from exc
    if "\r" in text:
        raise PrototypeProjectBundleError("bundle_json_lf_required")
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except PrototypeProjectBundleError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise PrototypeProjectBundleError("bundle_json_invalid") from exc


def _is_secret_key(key: str) -> bool:
    lowered = key.casefold()
    return any(
        marker in lowered
        for marker in ("password", "secret", "credential", "api_key", "access_token", "refresh_token")
    )


def _is_path_string(value: str) -> bool:
    lowered = value.casefold()
    return bool(
        _WINDOWS_ABSOLUTE_RE.match(value)
        or value.startswith(("/", "\\\\"))
        or lowered.startswith("file:")
    )


def _portable_value(value: Any, *, depth: int = 0) -> Any:
    if depth > MAX_NESTING_DEPTH:
        raise PrototypeProjectBundleError("bundle_record_nesting_exceeded")
    if value is None or isinstance(value, (bool, int, float)):
        if isinstance(value, float) and not (float("-inf") < value < float("inf")):
            raise PrototypeProjectBundleError("bundle_json_non_finite_number")
        return value
    if isinstance(value, str):
        if _CONTROL_RE.search(value):
            raise PrototypeProjectBundleError("bundle_record_control_character")
        if _is_path_string(value):
            raise PrototypeProjectBundleError("bundle_record_filesystem_path_forbidden")
        return value
    if isinstance(value, list):
        return [_portable_value(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            lowered = key.casefold()
            if (
                lowered in _DROP_KEYS
                or lowered.endswith(_DROP_SUFFIXES)
                or _is_secret_key(lowered)
                or lowered.startswith("raw_")
            ):
                continue
            output[key] = _portable_value(item, depth=depth + 1)
        return output
    raise PrototypeProjectBundleError("bundle_record_type_forbidden")


def _record_id(record_type: str, payload: dict[str, Any], *, hint: str | None = None) -> str:
    if hint:
        return hint
    if record_type == "topic_settings":
        return f"{payload.get('topic_id')}:{payload.get('settings_version')}"
    if record_type == "corpus_items":
        return f"{payload.get('snapshot_id')}:{payload.get('corpus_item_id')}"
    if record_type == "schedule_versions":
        return f"{payload.get('schedule_id')}:{payload.get('schedule_version')}"
    if record_type in {"rights", "full_text_artifact_descriptors"}:
        specific = (
            payload.get("rights_record_id")
            if record_type == "rights"
            else payload.get("descriptor_id")
        )
        return f"{payload.get('run_id')}:{specific}"
    if record_type in {"reviews", "evidence", "claims", "report_versions"}:
        specific = {
            "reviews": payload.get("review_id") or payload.get("paper_id"),
            "evidence": payload.get("evidence_id"),
            "claims": payload.get("claim_id"),
            "report_versions": payload.get("report_version_id") or payload.get("version_id"),
        }[record_type]
        return f"{payload.get('run_id')}:{specific}"
    if record_type == "reports":
        version = payload.get("report_version_id") or payload.get("latest_report_version_id") or "summary"
        return f"{payload.get('run_id')}:{payload.get('report_id')}:{version}"
    if record_type == "change_events" and isinstance(payload.get("record"), dict):
        nested = payload["record"]
        nested_id = nested.get("event_id") or nested.get("changeset_id")
        return f"{payload.get('record_kind')}:{nested_id}"
    if record_type == "gap_analyses" and isinstance(payload.get("record"), dict):
        nested = payload["record"]
        nested_id = next((nested.get(key) for key in _ID_KEYS if nested.get(key)), None)
        return f"{payload.get('record_kind')}:{nested_id}"
    primary_fields = {
        "topics": "topic_id",
        "corpus_snapshots": "snapshot_id",
        "runs": "run_id",
        "provider_execution_summaries": "execution_id",
        "schedules": "schedule_id",
        "refreshes": "refresh_id",
        "impact_records": "impact_id",
        "briefings": "briefing_id",
        "attention_events": "attention_event_id",
        "study_profiles": "study_profile_id",
        "evidence_matrices": "matrix_id",
        "gaps": "gap_id",
        "recommendations": "recommendation_id",
        "recommendation_decisions": "decision_event_id",
        "topic_origins": "origin_id",
    }
    primary_field = primary_fields.get(record_type)
    if primary_field and payload.get(primary_field):
        return str(payload[primary_field])
    for key in _ID_KEYS:
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return f"{record_type}-{_digest(payload)[:24]}"


def _envelope(record_type: str, payload: dict[str, Any], *, hint: str | None = None) -> dict[str, Any]:
    portable = _portable_value(deepcopy(payload))
    record_id = _record_id(record_type, portable, hint=hint)
    basis = {"record_type": record_type, "record_id": record_id, "payload": portable}
    return {**basis, "bundle_record_digest": _digest(basis)}


def _belongs_to_topics(record: dict[str, Any], selected: set[str]) -> bool:
    direct = str(record.get("topic_id") or "")
    if direct:
        return direct in selected
    candidates = {
        str(record.get("source_topic_id") or ""),
        str(record.get("promoted_topic_id") or ""),
        str(record.get("target_topic_id") or ""),
    }
    candidates.discard("")
    return bool(candidates & selected)


def _selected_topics(state: dict[str, Any], selected_topic_ids: list[str] | None) -> list[str]:
    available = {str(item.get("topic_id") or "") for item in state["topics"]}
    if selected_topic_ids is None:
        selected = sorted(
            str(item["topic_id"])
            for item in state["topics"]
            if not item.get("archived")
        )
    else:
        selected = sorted({str(item).strip() for item in selected_topic_ids if str(item).strip()})
    if not selected:
        raise PrototypeProjectBundleError("bundle_export_topic_selection_required", status_code=400)
    if any(item not in available for item in selected):
        raise PrototypeProjectBundleError("bundle_export_topic_not_found", status_code=404)
    return selected


def _latest_schedule_versions(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for item in records:
        key = str(item.get("schedule_id") or item.get("topic_id") or "")
        previous = latest.get(key)
        if previous is None or int(item.get("version") or 0) >= int(previous.get("version") or 0):
            latest[key] = item
    return [latest[key] for key in sorted(latest)]


def _state_record_payloads(
    state: dict[str, Any],
    selected_topic_ids: list[str],
) -> dict[str, list[tuple[dict[str, Any], str | None]]]:
    selected = set(selected_topic_ids)
    snapshots = [item for item in state["snapshots"] if _belongs_to_topics(item, selected)]
    payloads: dict[str, list[tuple[dict[str, Any], str | None]]] = {
        record_type: [] for record_type in RECORD_PATHS
    }
    for topic in state["topics"]:
        if str(topic.get("topic_id") or "") in selected:
            payloads["topics"].append((topic, str(topic["topic_id"])))
    for item in state["settings_versions"]:
        if _belongs_to_topics(item, selected):
            hint = f"{item.get('topic_id')}:{item.get('settings_version')}"
            payloads["topic_settings"].append((item, hint))
    for snapshot in snapshots:
        projected = {key: value for key, value in snapshot.items() if key != "items"}
        item_ids: list[str] = []
        for item in snapshot.get("items") or []:
            hint = f"{snapshot['snapshot_id']}:{item.get('corpus_item_id') or _digest(item)[:16]}"
            item_ids.append(hint)
            payloads["corpus_items"].append((item, hint))
        projected["corpus_item_ids"] = item_ids
        payloads["corpus_snapshots"].append((projected, str(snapshot["snapshot_id"])))

    for item in state["curation_events"]:
        if _belongs_to_topics(item, selected):
            payloads["change_events"].append(
                ({"record_kind": "curation_event", "record": item}, str(item.get("event_id") or ""))
            )
    for item in state["changesets"]:
        if _belongs_to_topics(item, selected):
            payloads["change_events"].append(
                ({"record_kind": "changeset", "record": item}, str(item.get("changeset_id") or ""))
            )
    for item in state.get("change_events") or []:
        if _belongs_to_topics(item, selected):
            payloads["change_events"].append(
                (
                    {"record_kind": "living_change_event", "record": item},
                    f"living:{item.get('event_id')}",
                )
            )
    direct_mappings = {
        "refreshes": "refresh_records",
        "schedule_versions": "schedules",
        "impact_records": "impact_records",
        "briefings": "briefings",
        "attention_events": "attention_events",
        "study_profiles": "study_profiles",
        "evidence_matrices": "evidence_matrices",
        "gaps": "research_gaps",
        "recommendations": "research_recommendations",
        "recommendation_decisions": "recommendation_decisions",
        "topic_origins": "recommendation_origins",
    }
    for record_type, state_key in direct_mappings.items():
        for item in state.get(state_key) or []:
            if _belongs_to_topics(item, selected):
                payloads[record_type].append((item, None))
    schedule_versions = [item for item, _hint in payloads["schedule_versions"]]
    payloads["schedules"] = [(item, str(item.get("schedule_id") or item.get("topic_id") or "")) for item in _latest_schedule_versions(schedule_versions)]

    gap_sources = {
        "analysis_scope": state.get("analysis_scopes") or [],
        "gap_analysis": state.get("gap_analyses") or [],
        "gap_comparison": state.get("gap_comparisons") or [],
    }
    for record_kind, records in gap_sources.items():
        for item in records:
            if _belongs_to_topics(item, selected):
                hint = _record_id("gap_analyses", item)
                payloads["gap_analyses"].append(
                    ({"record_kind": record_kind, "record": item}, hint)
                )
    return payloads


def _run_projection(run: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    workspace = run.get("workspace") if isinstance(run.get("workspace"), dict) else {}
    prototype_record = run.get("prototype") if isinstance(run.get("prototype"), dict) else {}
    return {
        **deepcopy(summary),
        "started_at": run.get("started_at"),
        "failure_category": run.get("error") if str(run.get("status") or "") == "failed" else None,
        "lineage": deepcopy(run.get("lineage") or {}),
        "workspace": {
            key: workspace.get(key)
            for key in (
                "topic_id",
                "snapshot_id",
                "settings_version",
                "source_manifest_digest",
                "evidence_set_digest",
                "generation_mode",
                "provider_calls",
            )
        },
        "prototype_summary": {
            "schema_version": prototype_record.get("schema_version"),
            "pipeline_status": prototype_record.get("pipeline_status"),
            "report_status": prototype_record.get("report_status"),
            "corpus_count": prototype_record.get("corpus_count"),
            "reviewed_paper_count": prototype_record.get("reviewed_paper_count"),
            "evidence_span_count": prototype_record.get("evidence_span_count"),
        },
    }


def _report_version_projection(
    *,
    topic_id: str,
    run_id: str,
    detail: dict[str, Any],
) -> dict[str, Any]:
    version = detail.get("version") if isinstance(detail.get("version"), dict) else {}
    content = detail.get("content")
    content_text = str(content) if isinstance(content, str) else None
    return {
        "report_version_id": str(version.get("version_id") or ""),
        "report_id": str(version.get("report_id") or run_id),
        "run_id": run_id,
        "topic_id": topic_id,
        "version_number": version.get("version_number"),
        "created_at": version.get("created_at"),
        "source": version.get("source"),
        "status": "ready" if content_text is not None else "content_unavailable",
        "content": content_text,
        "content_digest": _sha256_bytes(content_text.encode("utf-8")) if content_text is not None else None,
        "source_export_id": version.get("source_export_id"),
        "parent_version_id": version.get("parent_version_id"),
    }


def _review_and_evidence_from_repository(
    *,
    topic_id: str,
    run_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    reviews: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    for review in paper_reviews.list_reviews(run_id=run_id, limit=10_000, read_only=True):
        review_copy = {
            key: value
            for key, value in review.items()
            if key not in {"evidence_spans", "full_text_artifact_path"}
        }
        review_copy["topic_id"] = topic_id
        review_copy["run_id"] = run_id
        reviews.append(review_copy)
        review_id = str(review.get("review_id") or "")
        paper_id = str(review.get("paper_id") or "")
        for index, span in enumerate(review.get("evidence_spans") or []):
            evidence_id = str(span.get("evidence_id") or span.get("span_id") or "")
            if not evidence_id:
                evidence_id = f"evidence-{_digest({'run_id': run_id, 'paper_id': paper_id, 'index': index, 'span': span})[:20]}"
            evidence.append(
                {
                    **deepcopy(span),
                    "evidence_id": evidence_id,
                    "review_id": review_id,
                    "paper_id": paper_id,
                    "run_id": run_id,
                    "topic_id": topic_id,
                }
            )
    return reviews, evidence


def _public_real_records(
    *,
    topic_id: str,
    run_id: str,
    public_record: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    output: dict[str, list[dict[str, Any]]] = {
        "rights": [],
        "full_text_artifact_descriptors": [],
        "reviews": [],
        "evidence": [],
        "claims": [],
        "reports": [],
    }
    for article in public_record.get("articles") or []:
        paper_id = str(article.get("pmcid") or "")
        content_digest = str(article.get("content_digest") or "")
        rights_id = f"rights-{_digest({'paper_id': paper_id, 'content_digest': content_digest, 'license': article.get('license_type')})[:20]}"
        descriptor_id = f"descriptor-{_digest({'paper_id': paper_id, 'content_digest': content_digest})[:20]}"
        output["rights"].append(
            {
                "rights_record_id": rights_id,
                "topic_id": topic_id,
                "run_id": run_id,
                "paper_id": paper_id,
                "license_type": article.get("license_type"),
                "license_url": article.get("license_url"),
                "rights_accepted": bool(article.get("rights_accepted")),
                "content_digest": content_digest,
            }
        )
        output["full_text_artifact_descriptors"].append(
            {
                "descriptor_id": descriptor_id,
                "topic_id": topic_id,
                "run_id": run_id,
                "paper_id": paper_id,
                "content_digest": content_digest,
                "artifact_embedded": False,
                "artifact_kind": "normalized_jats_full_text",
                "availability_at_export": bool(article.get("full_text_retrieved")),
                "portable_posture": "descriptor_only_reacquisition_required",
            }
        )
        output["reviews"].append(
            {
                "review_id": article.get("review_id"),
                "topic_id": topic_id,
                "run_id": run_id,
                "paper_id": paper_id,
                "review_method": article.get("review_method"),
                "evidence_ids": deepcopy(article.get("evidence_ids") or []),
                "content_digest": content_digest,
                "full_text_status": "reviewed_not_embedded",
            }
        )
    for item in public_record.get("evidence") or []:
        output["evidence"].append({**deepcopy(item), "topic_id": topic_id})
    for item in public_record.get("claims") or []:
        output["claims"].append(
            {**deepcopy(item), "topic_id": topic_id, "run_id": run_id}
        )
    report = public_record.get("report")
    if isinstance(report, dict):
        output["reports"].append(
            {**deepcopy(report), "topic_id": topic_id, "run_id": run_id}
        )
    return output


def _provider_summary(topic_id: str, run: dict[str, Any]) -> dict[str, Any]:
    prototype_record = run.get("prototype") if isinstance(run.get("prototype"), dict) else {}
    provider = prototype_record.get("provider") if isinstance(prototype_record.get("provider"), dict) else {}
    workspace = run.get("workspace") if isinstance(run.get("workspace"), dict) else {}
    run_id = str(run.get("run_id") or "")
    return {
        "execution_id": f"provider-summary-{run_id}",
        "run_id": run_id,
        "topic_id": topic_id,
        "provider_id": provider.get("provider_id"),
        "surface_id": provider.get("surface_id"),
        "stage": provider.get("stage"),
        "output_class": provider.get("output_class"),
        "route_status": provider.get("route_status") or "not_invoked",
        "provider_calls": int(workspace.get("provider_calls") or 0),
        "raw_output_embedded": False,
        "credentials_embedded": False,
    }


def _append_external_records(
    state: dict[str, Any],
    selected_topic_ids: list[str],
    payloads: dict[str, list[tuple[dict[str, Any], str | None]]],
) -> None:
    selected = set(selected_topic_ids)
    portable_mapping = {
        "runs": "portable_runs",
        "rights": "portable_rights",
        "full_text_artifact_descriptors": "portable_full_text_descriptors",
        "reviews": "portable_reviews",
        "evidence": "portable_evidence",
        "claims": "portable_claims",
        "reports": "portable_reports",
        "report_versions": "portable_report_versions",
        "provider_execution_summaries": "portable_provider_execution_summaries",
    }
    if state.get("bundle_projects"):
        for record_type, state_key in portable_mapping.items():
            for item in state.get(state_key) or []:
                if _belongs_to_topics(item, selected):
                    payloads[record_type].append((item, None))
        return

    for topic_id in selected_topic_ids:
        for summary in prototype_workspace._workspace_runs(topic_id, state):
            run_id = str(summary.get("run_id") or "")
            run = load_run_record(run_id)
            payloads["runs"].append((_run_projection(run, summary), run_id))
            payloads["provider_execution_summaries"].append(
                (_provider_summary(topic_id, run), f"provider-summary-{run_id}")
            )
            try:
                public_record = prototype_real_corpus.load_public_record(run_id)
            except prototype_real_corpus.RealCorpusError:
                public_record = None
            if public_record is not None:
                for record_type, records in _public_real_records(
                    topic_id=topic_id,
                    run_id=run_id,
                    public_record=public_record,
                ).items():
                    payloads[record_type].extend((item, None) for item in records)
            else:
                reviews, evidence = _review_and_evidence_from_repository(
                    topic_id=topic_id,
                    run_id=run_id,
                )
                payloads["reviews"].extend((item, None) for item in reviews)
                payloads["evidence"].extend((item, None) for item in evidence)
            versions = report_versions.list_report_versions(run_id).get("items") or []
            for version in versions:
                version_id = str(version.get("version_id") or "")
                detail = report_versions.get_report_version_detail(run_id, version_id)
                projected = _report_version_projection(
                    topic_id=topic_id,
                    run_id=run_id,
                    detail=detail,
                )
                payloads["report_versions"].append((projected, None))
                if public_record is None:
                    payloads["reports"].append(
                        (
                            {
                                "report_id": projected["report_id"],
                                "run_id": run_id,
                                "topic_id": topic_id,
                                "latest_report_version_id": version_id,
                                "status": projected["status"],
                                "content_digest": projected["content_digest"],
                            },
                            None,
                        )
                    )


def _enveloped_records(
    payloads: dict[str, list[tuple[dict[str, Any], str | None]]],
) -> dict[str, list[dict[str, Any]]]:
    output: dict[str, list[dict[str, Any]]] = {}
    for record_type in sorted(RECORD_PATHS):
        by_id: dict[str, dict[str, Any]] = {}
        for payload, hint in payloads[record_type]:
            envelope = _envelope(record_type, payload, hint=hint)
            record_id = str(envelope["record_id"])
            previous = by_id.get(record_id)
            if previous is not None and previous != envelope:
                error = PrototypeProjectBundleError("bundle_record_id_content_conflict")
                error.record_type = record_type
                error.record_id = record_id
                raise error
            by_id[record_id] = envelope
        output[record_type] = [by_id[key] for key in sorted(by_id)]
    return output


def _jsonl_bytes(records: list[dict[str, Any]]) -> bytes:
    if not records:
        return b""
    return b"".join(_canonical_bytes(record) + b"\n" for record in records)


def _semantic_digest(
    selected_topic_ids: list[str],
    record_files: dict[str, bytes],
) -> str:
    return _digest(
        {
            "bundle_schema_version": BUNDLE_SCHEMA,
            "selected_topic_ids": selected_topic_ids,
            "record_entry_digests": {
                path: _sha256_bytes(record_files[path]) for path in sorted(record_files)
            },
        }
    )


def _stable_project_timestamp(state: dict[str, Any], selected: set[str]) -> str:
    candidates = sorted(
        str(item.get("created_at") or "")
        for item in state["topics"]
        if str(item.get("topic_id") or "") in selected and item.get("created_at")
    )
    return candidates[0] if candidates else "1970-01-01T00:00:00+00:00"


def _rights_summary(records: list[dict[str, Any]]) -> dict[str, int]:
    accepted = sum(
        1 for item in records if bool(item.get("payload", {}).get("rights_accepted"))
    )
    return {
        "records": len(records),
        "accepted": accepted,
        "requires_review": len(records) - accepted,
    }


def _build_bundle(
    *,
    state: dict[str, Any],
    selected_topic_ids: list[str] | None,
) -> tuple[bytes, dict[str, Any]]:
    prototype.require_prototype_mode()
    selected = _selected_topics(state, selected_topic_ids)
    payloads = _state_record_payloads(state, selected)
    _append_external_records(state, selected, payloads)
    records = _enveloped_records(payloads)
    record_files = {
        RECORD_PATHS[record_type]: _jsonl_bytes(records[record_type])
        for record_type in RECORD_PATHS
    }
    semantic_digest = _semantic_digest(selected, record_files)
    existing = next(
        (
            item
            for item in state.get("bundle_projects") or []
            if sorted(item.get("selected_topic_ids") or []) == selected
            and item.get("project_semantic_digest") == semantic_digest
        ),
        None,
    )
    project_id = str((existing or {}).get("project_id") or f"project-{semantic_digest[:20]}")
    project_created_at = str(
        (existing or {}).get("project_created_at")
        or _stable_project_timestamp(state, set(selected))
    )
    record_counts = {record_type: len(records[record_type]) for record_type in RECORD_PATHS}
    rights_summary = _rights_summary(records["rights"])
    project = {
        "schema_version": BUNDLE_SCHEMA,
        "source_prototype_version": SOURCE_PROTOTYPE_VERSION,
        "project_id": project_id,
        "project_created_at": project_created_at,
        "project_semantic_digest": semantic_digest,
        "selected_topic_ids": selected,
        "record_counts": record_counts,
        "rights_summary": rights_summary,
        "source_workspace_schema": prototype_workspace.WORKSPACE_SCHEMA,
        "portable_projection": SNAPSHOT_STRATEGY,
        "import_activation_posture": IMPORTED_POSTURE,
        "automatic_execution_enabled": False,
        "full_text_embedded": False,
        "raw_provider_output_embedded": False,
        "runtime_database_embedded": False,
        "credentials_embedded": False,
        "filesystem_paths_embedded": False,
        "exclusions": [
            "credentials_sessions_tokens_cookies",
            "full_text_bodies_and_raw_jats_xml",
            "raw_provider_requests_responses_and_traces",
            "runtime_sqlite_and_jsonl_authority_files",
            "scheduler_runtime_jobs_and_locks",
            "filesystem_paths_and_temporary_identifiers",
        ],
    }
    content_files = {"project.json": _canonical_bytes(project), **record_files}
    entries = [
        {
            "path": path,
            "size_bytes": len(content_files[path]),
            "sha256": _sha256_bytes(content_files[path]),
        }
        for path in sorted(content_files)
    ]
    content_digest = _digest(entries)
    bundle_id = f"bundle-{content_digest[:20]}"
    manifest_base = {
        "schema_version": BUNDLE_SCHEMA,
        "bundle_id": bundle_id,
        "project_id": project_id,
        "project_semantic_digest": semantic_digest,
        "bundle_content_digest": content_digest,
        "exported_at": project_created_at,
        "entry_order": "lexicographic_utf8",
        "archive_format": "zip_compatible_xrmlbundle",
        "compression": "stored",
        "signature": {"required": False, "status": "not_present"},
        "limits": {
            "max_compressed_bytes": MAX_COMPRESSED_BYTES,
            "max_uncompressed_bytes": MAX_UNCOMPRESSED_BYTES,
            "max_entry_bytes": MAX_ENTRY_BYTES,
            "max_entry_count": MAX_ENTRY_COUNT,
            "max_compression_ratio": MAX_COMPRESSION_RATIO,
        },
        "entries": entries,
    }
    manifest = {
        **manifest_base,
        "manifest_payload_digest": _digest(manifest_base),
    }
    all_files = {"manifest.json": _canonical_bytes(manifest), **content_files}
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as bundle:
        for path in sorted(all_files):
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            bundle.writestr(info, all_files[path])
    bundle_bytes = archive.getvalue()
    if len(bundle_bytes) > MAX_COMPRESSED_BYTES:
        raise PrototypeProjectBundleError("bundle_export_compressed_limit_exceeded")
    verified = _validate_bundle_bytes(bundle_bytes)
    if (
        verified["manifest"].get("bundle_content_digest") != content_digest
        or verified["project"].get("project_semantic_digest") != semantic_digest
    ):
        raise PrototypeProjectBundleError("bundle_export_verification_failed")
    preview = {
        "status": "READY_FOR_CONFIRMATION",
        "bundle_schema_version": BUNDLE_SCHEMA,
        "source_prototype_version": SOURCE_PROTOTYPE_VERSION,
        "project_id": project_id,
        "project_semantic_digest": semantic_digest,
        "bundle_id": bundle_id,
        "bundle_content_digest": content_digest,
        "selected_topic_ids": selected,
        "record_counts": record_counts,
        "rights_summary": rights_summary,
        "entry_count": len(all_files),
        "archive_size_bytes": len(bundle_bytes),
        "automatic_execution_enabled": False,
        "provider_calls": 0,
        "network_requests": 0,
        "writes": 0,
        "excluded_content": deepcopy(project["exclusions"]),
        "validation_status": {
            "archive_safety": "VERIFIED",
            "manifest": "VERIFIED",
            "schema": "VERIFIED",
            "integrity": "VERIFIED",
            "lineage": "VERIFIED",
            "rights": "PRESERVED",
        },
    }
    preview["preview_digest"] = _digest(preview)
    return bundle_bytes, preview


def _state_for_preview() -> dict[str, Any]:
    path = prototype_workspace._workspace_path()
    if not path.is_file():
        return prototype_workspace._validate_state(prototype_workspace._default_state())
    state = load_json_state(
        path,
        state_name=prototype_workspace.STATE_NAME,
        expected_type=dict,
        default=dict,
    )
    return prototype_workspace._validate_state(prototype_workspace._upgrade_state(deepcopy(state)))


def preview_export(selected_topic_ids: list[str] | None = None) -> dict[str, Any]:
    bundle_bytes, preview = _build_bundle(
        state=_state_for_preview(),
        selected_topic_ids=selected_topic_ids,
    )
    with _MEMORY_LOCK:
        _EXPORT_PREVIEWS[str(preview["preview_digest"])] = {
            "preview": deepcopy(preview),
            "bundle_bytes": bundle_bytes,
            "workspace_missing": not prototype_workspace._workspace_path().is_file(),
        }
    return preview


def confirm_export(
    *,
    selected_topic_ids: list[str] | None,
    preview_digest: str,
    confirmed: bool,
) -> dict[str, Any]:
    if not confirmed:
        raise PrototypeProjectBundleError("bundle_export_confirmation_required", status_code=400)
    with _MEMORY_LOCK:
        prior = deepcopy(_EXPORT_PREVIEWS.get(preview_digest))
    if prior is None:
        raise PrototypeProjectBundleError("bundle_export_preview_not_found")
    preview = prior["preview"]
    requested = (
        sorted({str(item).strip() for item in selected_topic_ids if str(item).strip()})
        if selected_topic_ids is not None
        else list(preview["selected_topic_ids"])
    )
    if requested != preview["selected_topic_ids"]:
        raise PrototypeProjectBundleError("bundle_export_preview_stale")
    if prior["workspace_missing"]:
        if prototype_workspace._workspace_path().is_file():
            raise PrototypeProjectBundleError("bundle_export_preview_stale")
        bundle_bytes = prior["bundle_bytes"]
    else:
        bundle_bytes, current_preview = _build_bundle(
            state=_state_for_preview(),
            selected_topic_ids=selected_topic_ids,
        )
        if current_preview != preview:
            raise PrototypeProjectBundleError("bundle_export_preview_stale")
    with _MEMORY_LOCK:
        if preview.get("preview_digest") != preview_digest:
            raise PrototypeProjectBundleError("bundle_export_preview_not_found")
        bundle_ref = str(preview["bundle_id"])
        _EXPORTS[bundle_ref] = bundle_bytes
    return {
        **preview,
        "status": "EXPORTED",
        "bundle_ref": bundle_ref,
        "filename": f"{preview['project_id']}{BUNDLE_EXTENSION}",
        "download_url": f"/prototype/workspace/bundles/exports/{bundle_ref}/download",
        "writes": 0,
    }


def get_export_bytes(bundle_ref: str) -> bytes:
    with _MEMORY_LOCK:
        data = _EXPORTS.get(str(bundle_ref))
    if data is None:
        raise PrototypeProjectBundleError("bundle_export_not_found", status_code=404)
    return data


def _validate_archive_path(name: str, seen: set[str]) -> None:
    if not name or _CONTROL_RE.search(name) or "\\" in name:
        raise PrototypeProjectBundleError("bundle_archive_path_invalid")
    path = PurePosixPath(name)
    if path.is_absolute() or name.endswith("/") or any(part in {"", ".", ".."} for part in path.parts):
        raise PrototypeProjectBundleError("bundle_archive_path_invalid")
    if any(":" in part for part in path.parts):
        raise PrototypeProjectBundleError("bundle_archive_alternate_stream_forbidden")
    normalized = unicodedata.normalize("NFKC", name).casefold()
    if normalized in seen:
        raise PrototypeProjectBundleError("bundle_archive_normalized_path_collision")
    seen.add(normalized)
    suffix = path.suffix.casefold()
    if suffix in _FORBIDDEN_ARCHIVE_SUFFIXES:
        raise PrototypeProjectBundleError("bundle_archive_executable_or_nested_archive_forbidden")
    if name not in REQUIRED_PATHS and not (
        name.startswith("exports/") and suffix == ".md" and len(path.parts) >= 2
    ):
        raise PrototypeProjectBundleError("bundle_archive_entry_not_allowed")


def _validate_zip_info(info: zipfile.ZipInfo) -> None:
    if info.flag_bits & 0x1:
        raise PrototypeProjectBundleError("bundle_archive_encryption_forbidden")
    if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
        raise PrototypeProjectBundleError("bundle_archive_compression_unsupported")
    mode = info.external_attr >> 16
    if mode and stat.S_IFMT(mode) not in {0, stat.S_IFREG}:
        raise PrototypeProjectBundleError("bundle_archive_non_regular_entry_forbidden")
    if info.external_attr & 0x400:
        raise PrototypeProjectBundleError("bundle_archive_reparse_entry_forbidden")
    if info.file_size < 0 or info.file_size > MAX_ENTRY_BYTES:
        raise PrototypeProjectBundleError("bundle_archive_entry_size_exceeded")
    if info.file_size and not info.compress_size:
        raise PrototypeProjectBundleError("bundle_archive_compression_ratio_exceeded")
    if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
        raise PrototypeProjectBundleError("bundle_archive_compression_ratio_exceeded")


def _extract_validated_entries(bundle_bytes: bytes, destination: Path) -> dict[str, bytes]:
    if len(bundle_bytes) > MAX_COMPRESSED_BYTES:
        raise PrototypeProjectBundleError("bundle_archive_compressed_limit_exceeded")
    if len(bundle_bytes) < 22:
        raise PrototypeProjectBundleError("bundle_archive_invalid")
    try:
        archive = zipfile.ZipFile(io.BytesIO(bundle_bytes), "r")
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise PrototypeProjectBundleError("bundle_archive_invalid") from exc
    with archive:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_ENTRY_COUNT:
            raise PrototypeProjectBundleError("bundle_archive_entry_count_exceeded")
        seen: set[str] = set()
        total_uncompressed = 0
        for info in infos:
            original_name = str(info.orig_filename)
            _validate_archive_path(original_name, seen)
            if original_name != info.filename:
                raise PrototypeProjectBundleError("bundle_archive_path_invalid")
            _validate_zip_info(info)
            total_uncompressed += info.file_size
            if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
                raise PrototypeProjectBundleError("bundle_archive_uncompressed_limit_exceeded")
        names = {info.filename for info in infos}
        if not REQUIRED_PATHS.issubset(names):
            raise PrototypeProjectBundleError("bundle_archive_required_entry_missing")
        extracted: dict[str, bytes] = {}
        for info in infos:
            target = destination.joinpath(*PurePosixPath(info.filename).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            total = 0
            chunks: list[bytes] = []
            try:
                with archive.open(info, "r") as source, target.open("xb") as output:
                    while True:
                        chunk = source.read(64 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > info.file_size or total > MAX_ENTRY_BYTES:
                            raise PrototypeProjectBundleError("bundle_archive_entry_size_mismatch")
                        output.write(chunk)
                        chunks.append(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            except PrototypeProjectBundleError:
                raise
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                raise PrototypeProjectBundleError("bundle_archive_extraction_failed") from exc
            if total != info.file_size:
                raise PrototypeProjectBundleError("bundle_archive_entry_size_mismatch")
            extracted[info.filename] = b"".join(chunks)
        return extracted


def _parse_jsonl(path: str, data: bytes) -> list[dict[str, Any]]:
    if not data:
        return []
    if not data.endswith(b"\n"):
        raise PrototypeProjectBundleError("bundle_jsonl_lf_termination_required")
    records: list[dict[str, Any]] = []
    for line in data[:-1].split(b"\n"):
        if not line or len(line) > MAX_JSONL_LINE_BYTES:
            raise PrototypeProjectBundleError("bundle_jsonl_line_invalid")
        record = _parse_json(line)
        if not isinstance(record, dict) or _canonical_bytes(record) != line:
            raise PrototypeProjectBundleError("bundle_jsonl_not_canonical")
        records.append(record)
    record_type = PATH_TO_RECORD_TYPE[path]
    if len(records) > RECORD_COUNT_LIMITS[record_type]:
        raise PrototypeProjectBundleError("bundle_record_count_limit_exceeded")
    return records


def _validate_record_envelope(record_type: str, envelope: dict[str, Any]) -> None:
    expected_keys = {"record_type", "record_id", "payload", "bundle_record_digest"}
    if set(envelope) != expected_keys or envelope.get("record_type") != record_type:
        raise PrototypeProjectBundleError("bundle_record_envelope_invalid")
    if not isinstance(envelope.get("record_id"), str) or not envelope["record_id"]:
        raise PrototypeProjectBundleError("bundle_record_identity_invalid")
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise PrototypeProjectBundleError("bundle_record_payload_invalid")
    if _portable_value(deepcopy(payload)) != payload:
        raise PrototypeProjectBundleError("bundle_record_forbidden_field")
    basis = {
        "record_type": record_type,
        "record_id": envelope["record_id"],
        "payload": payload,
    }
    if envelope.get("bundle_record_digest") != _digest(basis):
        raise PrototypeProjectBundleError("bundle_record_digest_mismatch")


def _validate_references(records: dict[str, list[dict[str, Any]]], project: dict[str, Any]) -> None:
    payloads = {
        record_type: [item["payload"] for item in envelopes]
        for record_type, envelopes in records.items()
    }
    topic_ids = {str(item.get("topic_id") or "") for item in payloads["topics"]}
    if topic_ids != set(project.get("selected_topic_ids") or []):
        raise PrototypeProjectBundleError("bundle_project_topic_inventory_mismatch")
    snapshot_ids = {str(item.get("snapshot_id") or "") for item in payloads["corpus_snapshots"]}
    corpus_item_ids = {str(item["record_id"]) for item in records["corpus_items"]}
    run_ids = {str(item.get("run_id") or "") for item in payloads["runs"]}
    evidence_ids = {str(item.get("evidence_id") or "") for item in payloads["evidence"]}
    for record_type, rows in payloads.items():
        for row in rows:
            topic_id = str(row.get("topic_id") or "")
            if topic_id and topic_id not in topic_ids:
                raise PrototypeProjectBundleError("bundle_record_topic_reference_missing")
            snapshot_id = str(row.get("snapshot_id") or "")
            if snapshot_id and record_type != "corpus_snapshots" and snapshot_id not in snapshot_ids:
                raise PrototypeProjectBundleError("bundle_record_snapshot_reference_missing")
            run_id = str(row.get("run_id") or "")
            if run_id and record_type != "runs" and run_id not in run_ids:
                raise PrototypeProjectBundleError("bundle_record_run_reference_missing")
    for snapshot in payloads["corpus_snapshots"]:
        if snapshot.get("topic_id") not in topic_ids:
            raise PrototypeProjectBundleError("bundle_snapshot_topic_reference_missing")
        if any(str(item) not in corpus_item_ids for item in snapshot.get("corpus_item_ids") or []):
            raise PrototypeProjectBundleError("bundle_snapshot_item_reference_missing")
    for claim in payloads["claims"]:
        if any(str(item) not in evidence_ids for item in claim.get("evidence_ids") or []):
            raise PrototypeProjectBundleError("bundle_claim_evidence_reference_missing")


def _validate_bundle_bytes(bundle_bytes: bytes) -> dict[str, Any]:
    prototype.require_prototype_mode()
    temp_path: str | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="xrml-bundle-quarantine-") as temporary:
            temp_path = temporary
            root = Path(temporary).resolve()
            entries = _extract_validated_entries(bundle_bytes, root)
            manifest = _parse_json(entries["manifest.json"])
            project = _parse_json(entries["project.json"])
            if not isinstance(manifest, dict) or _canonical_bytes(manifest) != entries["manifest.json"]:
                raise PrototypeProjectBundleError("bundle_manifest_not_canonical")
            if not isinstance(project, dict) or _canonical_bytes(project) != entries["project.json"]:
                raise PrototypeProjectBundleError("bundle_project_not_canonical")
            if manifest.get("schema_version") != BUNDLE_SCHEMA or project.get("schema_version") != BUNDLE_SCHEMA:
                raise PrototypeProjectBundleError("bundle_version_unsupported")
            if (
                project.get("source_prototype_version") != SOURCE_PROTOTYPE_VERSION
                or project.get("source_workspace_schema") != prototype_workspace.WORKSPACE_SCHEMA
            ):
                raise PrototypeProjectBundleError("bundle_source_schema_unsupported")
            posture_keys = {
                "automatic_execution_enabled",
                "full_text_embedded",
                "raw_provider_output_embedded",
                "runtime_database_embedded",
                "credentials_embedded",
                "filesystem_paths_embedded",
            }
            portable_project_metadata = {
                key: value for key, value in project.items() if key not in posture_keys
            }
            if (
                _portable_value(deepcopy(manifest)) != manifest
                or _portable_value(deepcopy(portable_project_metadata))
                != portable_project_metadata
            ):
                raise PrototypeProjectBundleError("bundle_metadata_forbidden_field")
            manifest_base = {key: value for key, value in manifest.items() if key != "manifest_payload_digest"}
            if manifest.get("manifest_payload_digest") != _digest(manifest_base):
                raise PrototypeProjectBundleError("bundle_manifest_digest_mismatch")
            declared = manifest.get("entries")
            if not isinstance(declared, list):
                raise PrototypeProjectBundleError("bundle_manifest_entries_invalid")
            expected_declared = [
                {
                    "path": path,
                    "size_bytes": len(entries[path]),
                    "sha256": _sha256_bytes(entries[path]),
                }
                for path in sorted(path for path in entries if path != "manifest.json")
            ]
            if declared != expected_declared:
                raise PrototypeProjectBundleError("bundle_manifest_entry_digest_mismatch")
            if manifest.get("bundle_content_digest") != _digest(expected_declared):
                raise PrototypeProjectBundleError("bundle_content_digest_mismatch")
            if manifest.get("project_id") != project.get("project_id"):
                raise PrototypeProjectBundleError("bundle_project_identity_mismatch")
            records = {
                record_type: _parse_jsonl(path, entries[path])
                for record_type, path in RECORD_PATHS.items()
            }
            if sum(len(items) for items in records.values()) > MAX_TOTAL_RECORDS:
                raise PrototypeProjectBundleError("bundle_total_record_count_exceeded")
            for record_type, envelopes in records.items():
                seen_ids: set[str] = set()
                for envelope in envelopes:
                    _validate_record_envelope(record_type, envelope)
                    record_id = str(envelope["record_id"])
                    if record_id in seen_ids:
                        raise PrototypeProjectBundleError("bundle_record_identity_collision")
                    seen_ids.add(record_id)
            record_files = {path: entries[path] for path in RECORD_PATHS.values()}
            semantic_digest = _semantic_digest(
                list(project.get("selected_topic_ids") or []),
                record_files,
            )
            if (
                project.get("project_semantic_digest") != semantic_digest
                or manifest.get("project_semantic_digest") != semantic_digest
            ):
                raise PrototypeProjectBundleError("bundle_project_semantic_digest_mismatch")
            expected_counts = {record_type: len(records[record_type]) for record_type in RECORD_PATHS}
            if project.get("record_counts") != expected_counts:
                raise PrototypeProjectBundleError("bundle_project_record_count_mismatch")
            if project.get("rights_summary") != _rights_summary(records["rights"]):
                raise PrototypeProjectBundleError("bundle_project_rights_summary_mismatch")
            if any(
                project.get(key) is not False
                for key in (
                    "automatic_execution_enabled",
                    "full_text_embedded",
                    "raw_provider_output_embedded",
                    "runtime_database_embedded",
                    "credentials_embedded",
                    "filesystem_paths_embedded",
                )
            ):
                raise PrototypeProjectBundleError("bundle_project_unsafe_posture")
            if project.get("import_activation_posture") != IMPORTED_POSTURE:
                raise PrototypeProjectBundleError("bundle_project_activation_posture_invalid")
            _validate_references(records, project)
            return {
                "manifest": manifest,
                "project": project,
                "records": records,
                "quarantine_residue": 0,
            }
    finally:
        if temp_path is not None and Path(temp_path).exists():
            shutil.rmtree(temp_path, ignore_errors=True)


def _payloads(validated: dict[str, Any], record_type: str) -> list[dict[str, Any]]:
    return [deepcopy(item["payload"]) for item in validated["records"][record_type]]


def _pristine_workspace(state: dict[str, Any]) -> bool:
    if state.get("bundle_projects"):
        return False
    if {str(item.get("topic_id") or "") for item in state.get("topics") or []} != {
        prototype_workspace.SYNTHETIC_TOPIC_ID,
        prototype_workspace.REAL_TOPIC_ID,
        prototype_workspace.LOCAL_TOPIC_ID,
    }:
        return False
    if len(state.get("settings_versions") or []) != 3 or any(
        int(item.get("settings_version") or 0) != 1
        for item in state.get("settings_versions") or []
    ):
        return False
    snapshots = state.get("snapshots") or []
    if len(snapshots) != 1 or snapshots[0].get("topic_id") != prototype_workspace.SYNTHETIC_TOPIC_ID:
        return False
    non_pristine_keys = (
        "curation_events",
        "changesets",
        "refresh_records",
        *prototype_living_topics.LIVING_LIST_KEYS,
        *prototype_research_gaps.RESEARCH_LIST_KEYS,
        *prototype_workspace.BUNDLE_STATE_LIST_KEYS[1:],
    )
    return all(not state.get(key) for key in non_pristine_keys)


def _existing_record_conflict(
    state: dict[str, Any],
    validated: dict[str, Any],
) -> bool:
    mappings = {
        "runs": "portable_runs",
        "rights": "portable_rights",
        "full_text_artifact_descriptors": "portable_full_text_descriptors",
        "reviews": "portable_reviews",
        "evidence": "portable_evidence",
        "claims": "portable_claims",
        "reports": "portable_reports",
        "report_versions": "portable_report_versions",
        "provider_execution_summaries": "portable_provider_execution_summaries",
    }
    for record_type, state_key in mappings.items():
        existing = {
            item["record_id"]: item["bundle_record_digest"]
            for item in (_envelope(record_type, payload) for payload in state.get(state_key) or [])
        }
        for incoming in validated["records"][record_type]:
            prior = existing.get(str(incoming["record_id"]))
            if prior is not None and prior != incoming.get("bundle_record_digest"):
                return True
    return False


def _collision_status(state: dict[str, Any], validated: dict[str, Any]) -> str:
    project = validated["project"]
    project_id = str(project["project_id"])
    semantic_digest = str(project["project_semantic_digest"])
    for existing in state.get("bundle_projects") or []:
        if existing.get("project_id") == project_id:
            if existing.get("project_semantic_digest") == semantic_digest:
                return "EXACT_BUNDLE_ALREADY_IMPORTED"
            return "PROJECT_ID_SEMANTIC_CONFLICT"
    if _existing_record_conflict(state, validated):
        return "RECORD_ID_CONTENT_CONFLICT"
    if not _pristine_workspace(state):
        return "PROJECT_ID_SEMANTIC_CONFLICT"
    return "NO_CONFLICT"


def _current_state_no_write() -> dict[str, Any]:
    return _state_for_preview()


def _import_preview(
    validated: dict[str, Any],
    collision: str,
    *,
    archive_size_bytes: int,
) -> dict[str, Any]:
    project = validated["project"]
    manifest = validated["manifest"]
    preview = {
        "status": "VALIDATED_READY_FOR_CONFIRMATION",
        "bundle_schema_version": BUNDLE_SCHEMA,
        "source_prototype_version": project.get("source_prototype_version"),
        "bundle_id": manifest["bundle_id"],
        "bundle_content_digest": manifest["bundle_content_digest"],
        "project_id": project["project_id"],
        "project_semantic_digest": project["project_semantic_digest"],
        "selected_topic_ids": deepcopy(project["selected_topic_ids"]),
        "record_counts": deepcopy(project["record_counts"]),
        "rights_summary": deepcopy(project.get("rights_summary") or {}),
        "archive_size_bytes": archive_size_bytes,
        "excluded_content": deepcopy(project.get("exclusions") or []),
        "collision_status": collision,
        "migration_outcome": "NOT_REQUIRED",
        "activation_posture": IMPORTED_POSTURE,
        "automatic_execution_enabled": False,
        "full_text_embedded": False,
        "provider_calls": 0,
        "network_requests": 0,
        "target_writes": 0,
        "quarantine_residue": 0,
        "rollback_status": "NOT_REQUIRED",
        "validation_status": {
            "archive_safety": "VERIFIED",
            "manifest": "VERIFIED",
            "schema": "VERIFIED",
            "integrity": "VERIFIED",
            "lineage": "VERIFIED",
            "rights": "PRESERVED",
        },
    }
    preview["preview_digest"] = _digest(preview)
    return preview


def receive_import(bundle_bytes: bytes, *, filename: str) -> dict[str, Any]:
    safe_name = str(filename or "").strip()
    if (
        not safe_name
        or Path(safe_name).name != safe_name
        or _CONTROL_RE.search(safe_name)
        or not safe_name.casefold().endswith(BUNDLE_EXTENSION)
    ):
        raise PrototypeProjectBundleError("bundle_import_filename_invalid", status_code=400)
    if len(bundle_bytes) > MAX_COMPRESSED_BYTES:
        raise PrototypeProjectBundleError("bundle_archive_compressed_limit_exceeded", status_code=413)
    validated = _validate_bundle_bytes(bundle_bytes)
    collision = _collision_status(_current_state_no_write(), validated)
    preview = _import_preview(
        validated,
        collision,
        archive_size_bytes=len(bundle_bytes),
    )
    import_id = f"bundle-import-{uuid.uuid4().hex}"
    session = {
        "import_id": import_id,
        "status": "VALIDATED_READY_FOR_CONFIRMATION",
        "state_history": [
            "RECEIVED",
            "QUARANTINED",
            "VALIDATING",
            "VALIDATED_READY_FOR_CONFIRMATION",
        ],
        "bundle_bytes": bytes(bundle_bytes),
        "preview": deepcopy(preview),
    }
    with _MEMORY_LOCK:
        _IMPORTS[import_id] = session
    return {
        "import_id": import_id,
        "state_history": deepcopy(session["state_history"]),
        **preview,
    }


def _reconstruct_workspace(validated: dict[str, Any]) -> dict[str, Any]:
    project = validated["project"]
    topics = _payloads(validated, "topics")
    for topic in topics:
        topic["imported_project_id"] = project["project_id"]
        topic["automatic_execution_posture"] = IMPORTED_POSTURE
    corpus_items = {
        str(envelope["record_id"]): deepcopy(envelope["payload"])
        for envelope in validated["records"]["corpus_items"]
    }
    snapshots: list[dict[str, Any]] = []
    for item in _payloads(validated, "corpus_snapshots"):
        item_ids = list(item.pop("corpus_item_ids", []))
        item["items"] = [deepcopy(corpus_items[str(record_id)]) for record_id in item_ids]
        snapshots.append(item)
    state: dict[str, Any] = {
        "schema_version": prototype_workspace.WORKSPACE_SCHEMA,
        "topics": topics,
        "settings_versions": _payloads(validated, "topic_settings"),
        "snapshots": snapshots,
        "curation_events": [],
        "changesets": [],
        "refresh_records": _payloads(validated, "refreshes"),
        **{key: [] for key in prototype_living_topics.LIVING_LIST_KEYS},
        **{key: [] for key in prototype_research_gaps.RESEARCH_LIST_KEYS},
        **{key: [] for key in prototype_workspace.BUNDLE_STATE_LIST_KEYS},
    }
    for wrapper in _payloads(validated, "change_events"):
        record = deepcopy(wrapper.get("record") or {})
        if wrapper.get("record_kind") == "curation_event":
            state["curation_events"].append(record)
        elif wrapper.get("record_kind") == "changeset":
            state["changesets"].append(record)
        elif wrapper.get("record_kind") == "living_change_event":
            state["change_events"].append(record)
        else:
            raise PrototypeProjectBundleError("bundle_change_event_kind_invalid")
    state["schedules"] = _payloads(validated, "schedule_versions")
    state["impact_records"] = _payloads(validated, "impact_records")
    state["briefings"] = _payloads(validated, "briefings")
    state["attention_events"] = _payloads(validated, "attention_events")
    state["study_profiles"] = _payloads(validated, "study_profiles")
    state["evidence_matrices"] = _payloads(validated, "evidence_matrices")
    state["research_gaps"] = _payloads(validated, "gaps")
    state["research_recommendations"] = _payloads(validated, "recommendations")
    state["recommendation_decisions"] = _payloads(validated, "recommendation_decisions")
    state["recommendation_origins"] = _payloads(validated, "topic_origins")
    for wrapper in _payloads(validated, "gap_analyses"):
        record_kind = wrapper.get("record_kind")
        target = {
            "analysis_scope": "analysis_scopes",
            "gap_analysis": "gap_analyses",
            "gap_comparison": "gap_comparisons",
        }.get(str(record_kind))
        if target is None:
            raise PrototypeProjectBundleError("bundle_gap_analysis_kind_invalid")
        state[target].append(deepcopy(wrapper.get("record") or {}))
    portable_mappings = {
        "portable_runs": "runs",
        "portable_rights": "rights",
        "portable_full_text_descriptors": "full_text_artifact_descriptors",
        "portable_reviews": "reviews",
        "portable_evidence": "evidence",
        "portable_claims": "claims",
        "portable_reports": "reports",
        "portable_report_versions": "report_versions",
        "portable_provider_execution_summaries": "provider_execution_summaries",
    }
    for state_key, record_type in portable_mappings.items():
        state[state_key] = _payloads(validated, record_type)
    state["bundle_projects"] = [
        {
            "project_id": project["project_id"],
            "project_created_at": project["project_created_at"],
            "project_semantic_digest": project["project_semantic_digest"],
            "bundle_id": validated["manifest"]["bundle_id"],
            "bundle_content_digest": validated["manifest"]["bundle_content_digest"],
            "bundle_schema_version": BUNDLE_SCHEMA,
            "source_prototype_version": project.get("source_prototype_version"),
            "source_exported_at": validated["manifest"].get("exported_at"),
            "imported_at": datetime.now(timezone.utc).isoformat(),
            "selected_topic_ids": deepcopy(project["selected_topic_ids"]),
            "source_topic_count": len(project["selected_topic_ids"]),
            "imported_topic_count": len(topics),
            "record_counts": deepcopy(project["record_counts"]),
            "rights_summary": deepcopy(project.get("rights_summary") or {}),
            "exclusion_policy": deepcopy(project.get("exclusions") or []),
            "activation_posture": IMPORTED_POSTURE,
            "automatic_execution_enabled": False,
            "full_text_embedded": False,
            "status": "IMPORTED",
        }
    ]
    return prototype_workspace._validate_state(state)


def confirm_import(
    import_id: str,
    *,
    preview_digest: str,
    confirmed: bool,
) -> dict[str, Any]:
    if not confirmed:
        raise PrototypeProjectBundleError("bundle_import_confirmation_required", status_code=400)
    with _MEMORY_LOCK:
        session = deepcopy(_IMPORTS.get(str(import_id)))
    if not session:
        raise PrototypeProjectBundleError("bundle_import_session_not_found", status_code=404)
    if session.get("status") != "VALIDATED_READY_FOR_CONFIRMATION":
        raise PrototypeProjectBundleError("bundle_import_session_not_confirmable")
    if session["preview"].get("preview_digest") != preview_digest:
        raise PrototypeProjectBundleError("bundle_import_preview_stale")
    validated = _validate_bundle_bytes(session["bundle_bytes"])
    current = _current_state_no_write()
    collision = _collision_status(current, validated)
    fresh_preview = _import_preview(
        validated,
        collision,
        archive_size_bytes=len(session["bundle_bytes"]),
    )
    if fresh_preview.get("preview_digest") != preview_digest:
        raise PrototypeProjectBundleError("bundle_import_preview_stale")
    if collision == "EXACT_BUNDLE_ALREADY_IMPORTED":
        result = {
            "import_id": import_id,
            **fresh_preview,
            "status": "IMPORTED",
            "idempotent": True,
            "target_writes": 0,
            "state_history": [*session["state_history"], "IMPORTING", "IMPORTED"],
        }
        with _MEMORY_LOCK:
            _IMPORTS[import_id] = {**session, "status": "IMPORTED", "result": deepcopy(result)}
        return result
    if collision != "NO_CONFLICT":
        raise PrototypeProjectBundleError(f"bundle_import_{collision.casefold()}")
    try:
        imported_state = _reconstruct_workspace(validated)

        def apply(current_value: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            if current_value:
                guarded = prototype_workspace._validate_state(
                    prototype_workspace._upgrade_state(deepcopy(current_value))
                )
            else:
                guarded = prototype_workspace._validate_state(prototype_workspace._default_state())
            current_collision = _collision_status(guarded, validated)
            if current_collision != "NO_CONFLICT":
                raise PrototypeProjectBundleError(f"bundle_import_{current_collision.casefold()}")
            return imported_state, {"atomic_replace": True}

        mutation_result = mutate_json_state(
            prototype_workspace._workspace_path(),
            state_name=prototype_workspace.STATE_NAME,
            expected_type=dict,
            default=dict,
            mutate=apply,
        )
    except PrototypeProjectBundleError:
        with _MEMORY_LOCK:
            _IMPORTS[import_id] = {
                **session,
                "status": "ROLLED_BACK",
                "state_history": [*session["state_history"], "IMPORTING", "ROLLED_BACK"],
            }
        raise
    except Exception as exc:
        with _MEMORY_LOCK:
            _IMPORTS[import_id] = {
                **session,
                "status": "ROLLED_BACK",
                "state_history": [*session["state_history"], "IMPORTING", "ROLLED_BACK"],
            }
        raise PrototypeProjectBundleError("bundle_import_atomic_write_failed") from exc
    result = {
        "import_id": import_id,
        **fresh_preview,
        "status": "IMPORTED",
        "idempotent": False,
        "target_writes": 1,
        "atomic_replace": bool(mutation_result.get("atomic_replace")),
        "state_history": [*session["state_history"], "IMPORTING", "IMPORTED"],
    }
    with _MEMORY_LOCK:
        _IMPORTS[import_id] = {**session, "status": "IMPORTED", "result": deepcopy(result)}
    return result


def get_import_status(import_id: str) -> dict[str, Any]:
    with _MEMORY_LOCK:
        session = deepcopy(_IMPORTS.get(str(import_id)))
    if not session:
        raise PrototypeProjectBundleError("bundle_import_session_not_found", status_code=404)
    if isinstance(session.get("result"), dict):
        return session["result"]
    return {
        "import_id": import_id,
        **deepcopy(session.get("preview") or {}),
        "status": session["status"],
        "state_history": deepcopy(session.get("state_history") or []),
    }


def list_projects() -> dict[str, Any]:
    state = _current_state_no_write()
    return {
        "bundle_schema_version": BUNDLE_SCHEMA,
        "projects": deepcopy(state.get("bundle_projects") or []),
        "current_workspace": {
            "topic_count": len(state.get("topics") or []),
            "imported": bool(state.get("bundle_projects")),
            "export_preview_required": True,
        },
        "limits": {
            "compressed_bytes": MAX_COMPRESSED_BYTES,
            "uncompressed_bytes": MAX_UNCOMPRESSED_BYTES,
            "entries": MAX_ENTRY_COUNT,
            "individual_entry_bytes": MAX_ENTRY_BYTES,
            "compression_ratio": MAX_COMPRESSION_RATIO,
        },
        "activation_posture": IMPORTED_POSTURE,
        "provider_calls": 0,
        "network_requests": 0,
    }


def get_project_detail(project_id: str) -> dict[str, Any]:
    state = _current_state_no_write()
    project = next(
        (item for item in state.get("bundle_projects") or [] if item.get("project_id") == project_id),
        None,
    )
    if project is None:
        raise PrototypeProjectBundleError("bundle_project_not_found", status_code=404)
    selected = set(project.get("selected_topic_ids") or [])
    return {
        **deepcopy(project),
        "topics": [
            prototype_workspace._topic_detail(state, item)
            for item in state["topics"]
            if item.get("topic_id") in selected
        ],
        "portable_records": {
            "runs": deepcopy(state.get("portable_runs") or []),
            "rights": deepcopy(state.get("portable_rights") or []),
            "full_text_artifact_descriptors": deepcopy(
                state.get("portable_full_text_descriptors") or []
            ),
            "reviews": deepcopy(state.get("portable_reviews") or []),
            "evidence": deepcopy(state.get("portable_evidence") or []),
            "claims": deepcopy(state.get("portable_claims") or []),
            "reports": deepcopy(state.get("portable_reports") or []),
            "report_versions": [
                {key: value for key, value in item.items() if key != "content"}
                for item in state.get("portable_report_versions") or []
            ],
            "provider_execution_summaries": deepcopy(
                state.get("portable_provider_execution_summaries") or []
            ),
        },
        "automatic_execution_enabled": False,
        "provider_calls": 0,
        "network_requests": 0,
    }


def get_portable_run(run_id: str) -> dict[str, Any]:
    state = _current_state_no_write()
    record = next(
        (item for item in state.get("portable_runs") or [] if item.get("run_id") == run_id),
        None,
    )
    if record is None:
        raise PrototypeProjectBundleError("bundle_portable_run_not_found", status_code=404)
    return {
        **deepcopy(record),
        "portable": True,
        "activation_posture": IMPORTED_POSTURE,
        "full_text_embedded": False,
    }


def get_portable_report_version(run_id: str, version_id: str) -> dict[str, Any]:
    state = _current_state_no_write()
    record = next(
        (
            item
            for item in state.get("portable_report_versions") or []
            if item.get("run_id") == run_id
            and (item.get("report_version_id") == version_id or item.get("version_id") == version_id)
        ),
        None,
    )
    if record is None:
        raise PrototypeProjectBundleError("bundle_portable_report_version_not_found", status_code=404)
    return {
        "run_id": run_id,
        "report_id": record.get("report_id") or run_id,
        "version": {key: value for key, value in record.items() if key != "content"},
        "content": record.get("content"),
        "portable": True,
        "activation_posture": IMPORTED_POSTURE,
        "warnings": ["Full-text source artifacts are not embedded in project bundles."],
    }


def get_portable_evidence(run_id: str, evidence_id: str) -> dict[str, Any]:
    state = _current_state_no_write()
    evidence = next(
        (
            item
            for item in state.get("portable_evidence") or []
            if item.get("run_id") == run_id and item.get("evidence_id") == evidence_id
        ),
        None,
    )
    if evidence is None:
        raise PrototypeProjectBundleError("bundle_portable_evidence_not_found", status_code=404)
    rights = [
        item
        for item in state.get("portable_rights") or []
        if item.get("run_id") == run_id and item.get("paper_id") == evidence.get("paper_id")
    ]
    return {
        "run_id": run_id,
        "evidence": deepcopy(evidence),
        "rights": deepcopy(rights),
        "portable": True,
        "full_text_embedded": False,
        "activation_posture": IMPORTED_POSTURE,
    }
