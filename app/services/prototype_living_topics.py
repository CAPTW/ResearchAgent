"""Deterministic Living Topic scheduling, delta, impact, and briefing services.

The v0.5 prototype deliberately keeps these records in the existing
``prototype_workspace`` authority document.  Its cross-process lock and atomic
replace boundary provide the single-node claim primitive; external work is
always performed after the lock is released.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import re
from copy import deepcopy
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.repositories.files import iso_now_utc

CADENCES = {"disabled", "manual", "daily", "weekly", "monthly"}
NETWORK_POLICIES = {
    "disabled",
    "manual_official_source_only",
    "scheduled_official_source_only",
}
BRIEFING_POLICIES = {"no_change_and_material_changes", "material_changes_only"}
CATCH_UP_POLICY = "coalesce_one"
TERMINAL_REFRESH_STATES = {
    "PREVIEW_READY",
    "NO_MATERIAL_CHANGE",
    "FAILED_SOURCE_UNAVAILABLE",
    "FAILED_POLICY",
    "FAILED_VALIDATION",
    "COMPLETED",
}
ACTIVE_JOB_STATES = {"PENDING", "RUNNING"}
CRITICAL_EVENT_TYPES = {"full_text_changed", "license_changed", "article_status_changed"}
LIVING_LIST_KEYS = (
    "schedules",
    "schedule_runtime",
    "schedule_jobs",
    "change_events",
    "impact_records",
    "briefings",
    "attention_events",
)

_LOCAL_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _record_digest(record: dict[str, Any], field: str) -> str:
    return _digest({key: value for key, value in record.items() if key != field})


def _event_digest(record: dict[str, Any]) -> str:
    return _digest(
        {
            key: value
            for key, value in record.items()
            if key not in {"event_digest", "detected_at"}
        }
    )


def _parse_utc(value: str | datetime | None) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    else:
        parsed = datetime.now(UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


_FIXED_ZONE_FALLBACKS: dict[str, tzinfo] = {
    "UTC": UTC,
    "Etc/UTC": UTC,
    # Korea has observed UTC+09:00 year-round since 1988.  This fallback keeps
    # the local prototype usable on Windows hosts whose managed interpreter
    # does not bundle the optional IANA tzdata wheel.
    "Asia/Seoul": timezone(timedelta(hours=9), name="Asia/Seoul"),
}


def _zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        fallback = _FIXED_ZONE_FALLBACKS.get(name)
        if fallback is not None:
            return fallback
        from app.services.prototype_workspace import PrototypeWorkspaceError

        raise PrototypeWorkspaceError("living_schedule_time_zone_invalid", status_code=400) from exc


def _local_clock(value: str) -> time:
    if not _LOCAL_TIME_RE.fullmatch(value):
        from app.services.prototype_workspace import PrototypeWorkspaceError

        raise PrototypeWorkspaceError("living_schedule_local_time_invalid", status_code=400)
    hour, minute = (int(part) for part in value.split(":"))
    return time(hour=hour, minute=minute)


def ensure_state_shape(state: dict[str, Any]) -> dict[str, Any]:
    for key in LIVING_LIST_KEYS:
        state.setdefault(key, [])
    return state


def _unique(records: list[dict[str, Any]], field: str, code: str) -> None:
    values = [str(record.get(field) or "") for record in records]
    if any(not value for value in values) or len(values) != len(set(values)):
        from app.services.prototype_workspace import PrototypeWorkspaceError

        raise PrototypeWorkspaceError(code)


def validate_state(state: dict[str, Any]) -> None:
    from app.services.prototype_workspace import PrototypeWorkspaceError

    for key in LIVING_LIST_KEYS:
        if not isinstance(state.get(key), list):
            raise PrototypeWorkspaceError("living_workspace_schema_invalid")
    topic_ids = {str(topic.get("topic_id") or "") for topic in state.get("topics") or []}
    snapshot_ids = {str(row.get("snapshot_id") or "") for row in state.get("snapshots") or []}

    schedules = state["schedules"]
    pairs: set[tuple[str, int]] = set()
    for row in schedules:
        pair = (str(row.get("schedule_id") or ""), int(row.get("schedule_version") or 0))
        if pair in pairs or not pair[0] or pair[1] < 1:
            raise PrototypeWorkspaceError("living_schedule_identity_invalid")
        pairs.add(pair)
        if row.get("topic_id") not in topic_ids or row.get("cadence") not in CADENCES:
            raise PrototypeWorkspaceError("living_schedule_boundary_invalid")
        if row.get("network_policy") not in NETWORK_POLICIES or row.get("briefing_policy") not in BRIEFING_POLICIES:
            raise PrototypeWorkspaceError("living_schedule_policy_invalid")
        if row.get("catch_up_policy") != CATCH_UP_POLICY:
            raise PrototypeWorkspaceError("living_schedule_catch_up_invalid")
        if row.get("schedule_digest") != _record_digest(row, "schedule_digest"):
            raise PrototypeWorkspaceError("living_schedule_digest_mismatch")

    runtime = state["schedule_runtime"]
    _unique(runtime, "schedule_id", "living_schedule_runtime_identity_invalid") if runtime else None
    schedule_ids = {pair[0] for pair in pairs}
    for row in runtime:
        if row.get("schedule_id") not in schedule_ids:
            raise PrototypeWorkspaceError("living_schedule_runtime_orphan")
        if row.get("runtime_digest") != _record_digest(row, "runtime_digest"):
            raise PrototypeWorkspaceError("living_schedule_runtime_digest_mismatch")

    jobs = state["schedule_jobs"]
    _unique(jobs, "job_id", "living_job_identity_invalid") if jobs else None
    idempotency = [str(row.get("idempotency_key") or "") for row in jobs]
    if len(idempotency) != len(set(idempotency)):
        raise PrototypeWorkspaceError("living_job_idempotency_collision")
    for row in jobs:
        if row.get("topic_id") not in topic_ids or row.get("schedule_id") not in schedule_ids:
            raise PrototypeWorkspaceError("living_job_boundary_invalid")
        if row.get("job_digest") != _record_digest(row, "job_digest"):
            raise PrototypeWorkspaceError("living_job_digest_mismatch")

    events = state["change_events"]
    _unique(events, "event_id", "living_change_event_identity_invalid") if events else None
    event_ids = {str(row["event_id"]) for row in events}
    for row in events:
        if row.get("topic_id") not in topic_ids:
            raise PrototypeWorkspaceError("living_change_event_topic_invalid")
        for field in ("before_snapshot_id", "after_snapshot_id"):
            if row.get(field) and row.get(field) not in snapshot_ids:
                raise PrototypeWorkspaceError("living_change_event_snapshot_invalid")
        if row.get("event_digest") != _event_digest(row):
            raise PrototypeWorkspaceError("living_change_event_digest_mismatch")

    impacts = state["impact_records"]
    _unique(impacts, "impact_id", "living_impact_identity_invalid") if impacts else None
    impact_ids = {str(row["impact_id"]) for row in impacts}
    for row in impacts:
        if row.get("change_event_id") not in event_ids or row.get("topic_id") not in topic_ids:
            raise PrototypeWorkspaceError("living_impact_boundary_invalid")
        event = next(item for item in events if item["event_id"] == row["change_event_id"])
        if event.get("topic_id") != row.get("topic_id"):
            raise PrototypeWorkspaceError("living_cross_topic_impact")
        if row.get("impact_digest") != _record_digest(row, "impact_digest"):
            raise PrototypeWorkspaceError("living_impact_digest_mismatch")

    briefings = state["briefings"]
    _unique(briefings, "briefing_id", "living_briefing_identity_invalid") if briefings else None
    for row in briefings:
        if row.get("topic_id") not in topic_ids:
            raise PrototypeWorkspaceError("living_briefing_topic_invalid")
        if not set(row.get("change_event_ids") or []).issubset(event_ids):
            raise PrototypeWorkspaceError("living_briefing_event_invalid")
        if not set(row.get("impact_record_ids") or []).issubset(impact_ids):
            raise PrototypeWorkspaceError("living_briefing_impact_invalid")
        if row.get("briefing_digest") != _record_digest(row, "briefing_digest"):
            raise PrototypeWorkspaceError("living_briefing_digest_mismatch")

    attention = state["attention_events"]
    _unique(attention, "attention_event_id", "living_attention_identity_invalid") if attention else None
    briefing_ids = {str(row["briefing_id"]) for row in briefings}
    for row in attention:
        if row.get("briefing_id") not in briefing_ids or row.get("change_event_id") not in event_ids:
            raise PrototypeWorkspaceError("living_attention_boundary_invalid")
        if row.get("attention_digest") != _record_digest(row, "attention_digest"):
            raise PrototypeWorkspaceError("living_attention_digest_mismatch")


def compute_next_due_at(
    cadence: str,
    time_zone: str,
    local_time: str,
    *,
    after: str | datetime | None = None,
    anchor_local_date: str | None = None,
) -> str | None:
    if cadence in {"disabled", "manual"}:
        return None
    if cadence not in CADENCES:
        from app.services.prototype_workspace import PrototypeWorkspaceError

        raise PrototypeWorkspaceError("living_schedule_cadence_invalid", status_code=400)
    zone = _zone(time_zone)
    clock = _local_clock(local_time)
    after_utc = _parse_utc(after)
    local_now = after_utc.astimezone(zone)
    anchor = date.fromisoformat(anchor_local_date) if anchor_local_date else local_now.date()

    def candidate(day: date) -> datetime:
        return datetime.combine(day, clock, tzinfo=zone)

    current = candidate(anchor)
    if cadence == "daily":
        while current.astimezone(UTC) <= after_utc:
            current += timedelta(days=1)
    elif cadence == "weekly":
        while current.astimezone(UTC) <= after_utc:
            current += timedelta(days=7)
    else:
        year, month = current.year, current.month
        anchor_day = current.day
        while current.astimezone(UTC) <= after_utc:
            month += 1
            if month == 13:
                month = 1
                year += 1
            day = min(anchor_day, calendar.monthrange(year, month)[1])
            current = candidate(date(year, month, day))
    return _iso(current.astimezone(UTC))


def _latest_schedule(state: dict[str, Any], topic_id: str) -> dict[str, Any] | None:
    rows = [row for row in state["schedules"] if row.get("topic_id") == topic_id]
    return max(rows, key=lambda row: int(row["schedule_version"])) if rows else None


def _schedule_version(
    state: dict[str, Any], schedule_id: str, schedule_version: int
) -> dict[str, Any] | None:
    return next(
        (
            row
            for row in state["schedules"]
            if row.get("schedule_id") == schedule_id
            and int(row.get("schedule_version") or 0) == schedule_version
        ),
        None,
    )


def _runtime(state: dict[str, Any], schedule_id: str) -> dict[str, Any] | None:
    return next((row for row in state["schedule_runtime"] if row.get("schedule_id") == schedule_id), None)


def _schedule_view(state: dict[str, Any], topic: dict[str, Any]) -> dict[str, Any]:
    row = _latest_schedule(state, str(topic["topic_id"]))
    if row is None:
        return {
            "schedule_id": None,
            "topic_id": topic["topic_id"],
            "schedule_version": 0,
            "enabled": False,
            "cadence": "disabled",
            "time_zone": "UTC",
            "local_time": "09:00",
            "next_due_at": None,
            "last_due_at": None,
            "last_attempt_at": None,
            "last_success_at": None,
            "last_status": "disabled",
            "last_refresh_id": None,
            "last_briefing_id": None,
            "consecutive_failures": 0,
            "catch_up_policy": CATCH_UP_POLICY,
            "network_policy": "disabled",
            "briefing_policy": "no_change_and_material_changes",
            "created_at": None,
            "updated_at": None,
            "schedule_digest": None,
            "scheduler_health": "disabled",
        }
    view = deepcopy(row)
    runtime = _runtime(state, str(row["schedule_id"]))
    if runtime:
        for key in (
            "next_due_at",
            "last_due_at",
            "last_attempt_at",
            "last_success_at",
            "last_status",
            "last_refresh_id",
            "last_briefing_id",
            "consecutive_failures",
            "scheduler_health",
        ):
            view[key] = runtime.get(key)
    if (
        view.get("enabled")
        and view.get("next_due_at")
        and _parse_utc(str(view["next_due_at"])) <= datetime.now(UTC)
        and view.get("scheduler_health") not in {"running", "degraded", "source_unavailable", "blocked_by_policy"}
    ):
        view["scheduler_health"] = "due"
    return view


def decorate_topic(state: dict[str, Any], topic: dict[str, Any]) -> dict[str, Any]:
    topic_id = str(topic["topic_id"])
    schedule = _schedule_view(state, topic)
    history = sorted(
        (deepcopy(row) for row in state["schedules"] if row.get("topic_id") == topic_id),
        key=lambda row: int(row["schedule_version"]),
        reverse=True,
    )
    events = [deepcopy(row) for row in state["change_events"] if row.get("topic_id") == topic_id]
    impacts = [deepcopy(row) for row in state["impact_records"] if row.get("topic_id") == topic_id]
    briefings = sorted(
        (deepcopy(row) for row in state["briefings"] if row.get("topic_id") == topic_id),
        key=lambda row: str(row.get("generated_at") or ""),
        reverse=True,
    )
    attention = [row for row in state["attention_events"] if row.get("topic_id") == topic_id]
    latest_attention: dict[tuple[str, str], dict[str, Any]] = {}
    for row in attention:
        latest_attention[(str(row["briefing_id"]), str(row["change_event_id"]))] = row
    suppressed: set[tuple[str, str]] = set()
    seen_informational_signatures: set[str] = set()
    for briefing in sorted(briefings, key=lambda row: str(row.get("generated_at") or "")):
        for event_id in briefing.get("change_event_ids") or []:
            event = next((row for row in events if row.get("event_id") == event_id), None)
            if not event or event.get("severity") != "info":
                continue
            signature = str(event.get("event_signature") or event.get("event_digest") or "")
            if signature in seen_informational_signatures:
                suppressed.add((str(briefing["briefing_id"]), str(event_id)))
            elif signature:
                seen_informational_signatures.add(signature)
    for briefing in briefings:
        items = []
        for event_id in briefing.get("change_event_ids") or []:
            event = next((row for row in events if row.get("event_id") == event_id), None)
            action = latest_attention.get((str(briefing["briefing_id"]), str(event_id)))
            severity = (event or {}).get("severity")
            state_name = (action or {}).get("state") or (
                "suppressed_for_repetition"
                if (str(briefing["briefing_id"]), str(event_id)) in suppressed
                else "unread"
            )
            items.append(
                {
                    "change_event_id": event_id,
                    "event_type": (event or {}).get("event_type"),
                    "severity": severity,
                    "alert_class": (
                        "critical" if severity == "critical"
                        else "review_required" if severity == "attention"
                        else "informational"
                    ),
                    "state": state_name,
                }
            )
        briefing["attention_items"] = items
        briefing["acknowledged"] = bool(items) and all(item["state"] == "acknowledged" for item in items)
        briefing["dismissed"] = bool(items) and all(item["state"] == "dismissed" for item in items)
    unread = sum(
        1
        for briefing in briefings
        for item in briefing.get("attention_items") or []
        if item["state"] == "unread"
    )
    critical = sum(
        1
        for briefing in briefings
        for item in briefing.get("attention_items") or []
        if item["state"] == "unread" and item["severity"] == "critical"
    )
    review_required = sum(
        1
        for briefing in briefings
        for item in briefing.get("attention_items") or []
        if item["state"] == "unread" and item["alert_class"] == "review_required"
    )
    return {
        **deepcopy(topic),
        "living_schedule": schedule,
        "schedule_history": history,
        "schedule_jobs": [
            deepcopy(row) for row in state["schedule_jobs"] if row.get("topic_id") == topic_id
        ],
        "change_events": events,
        "impact_records": impacts,
        "briefings": briefings,
        "latest_briefing": briefings[0] if briefings else None,
        "attention_summary": {
            "unread": unread,
            "review_required": review_required,
            "critical": critical,
        },
    }


def workspace_totals(state: dict[str, Any]) -> dict[str, int | str | None]:
    topics = state.get("topics") or []
    views = [_schedule_view(state, topic) for topic in topics]
    decorated = [decorate_topic(state, topic) for topic in topics]
    briefings = state.get("briefings") or []
    return {
        "living_topics": sum(1 for view in views if view.get("schedule_id")),
        "schedules_enabled": sum(1 for view in views if view.get("enabled")),
        "topics_due": sum(1 for view in views if view.get("scheduler_health") == "due"),
        "unread_briefings": sum(
            1
            for topic in decorated
            for briefing in topic.get("briefings") or []
            if any(item.get("state") == "unread" for item in briefing.get("attention_items") or [])
        ),
        "review_required": sum(int(topic["attention_summary"]["review_required"]) for topic in decorated),
        "critical_alerts": sum(int(topic["attention_summary"]["critical"]) for topic in decorated),
        "last_successful_refresh": max(
            (str(row.get("last_success_at")) for row in state["schedule_runtime"] if row.get("last_success_at")),
            default=None,
        ),
        "last_briefing": max(
            (str(row.get("generated_at")) for row in briefings if row.get("generated_at")),
            default=None,
        ),
    }


def save_schedule(topic_id: str, payload: dict[str, Any], *, now: str | datetime | None = None) -> dict[str, Any]:
    from app.services import prototype_workspace as workspace

    cadence = str(payload.get("cadence") or "disabled")
    enabled = bool(payload.get("enabled", False))
    time_zone = str(payload.get("time_zone") or "UTC")
    local_time = str(payload.get("local_time") or "09:00")
    network_policy = str(payload.get("network_policy") or "disabled")
    briefing_policy = str(payload.get("briefing_policy") or "no_change_and_material_changes")
    _zone(time_zone)
    _local_clock(local_time)
    if cadence not in CADENCES:
        raise workspace.PrototypeWorkspaceError("living_schedule_cadence_invalid", status_code=400)
    if network_policy not in NETWORK_POLICIES or briefing_policy not in BRIEFING_POLICIES:
        raise workspace.PrototypeWorkspaceError("living_schedule_policy_invalid", status_code=400)
    if cadence == "disabled" and enabled:
        raise workspace.PrototypeWorkspaceError("living_schedule_disabled_cannot_enable", status_code=400)
    if not enabled:
        cadence = "disabled" if cadence == "disabled" else cadence
    now_utc = _parse_utc(now)
    now_text = _iso(now_utc)

    def mutation(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        topic = workspace._topic(state, topic_id)
        if topic.get("imported_project_id") and enabled:
            raise workspace.PrototypeWorkspaceError("bundle_import_execution_disabled", status_code=409)
        if topic.get("archived") and enabled:
            raise workspace.PrototypeWorkspaceError("living_schedule_archived_topic", status_code=409)
        if network_policy == "scheduled_official_source_only":
            if (
                topic.get("topic_kind") != "real_open_access"
                or topic.get("source_policy") != "pmc_open_access"
                or cadence not in {"daily", "weekly", "monthly"}
                or not enabled
            ):
                raise workspace.PrototypeWorkspaceError("living_schedule_external_policy_incompatible", status_code=409)
        elif topic.get("topic_kind") in {"synthetic_demo", "local_empty"} and network_policy != "disabled":
            raise workspace.PrototypeWorkspaceError("living_schedule_external_policy_incompatible", status_code=409)
        current = _latest_schedule(state, str(topic["topic_id"]))
        schedule_id = str(current["schedule_id"]) if current else f"schedule-{_digest({'topic_id': topic['topic_id']})[:24]}"
        version = int(current["schedule_version"]) + 1 if current else 1
        anchor = now_utc.astimezone(_zone(time_zone)).date().isoformat()
        next_due = compute_next_due_at(
            cadence,
            time_zone,
            local_time,
            after=now_utc,
            anchor_local_date=anchor,
        ) if enabled else None
        record = {
            "schedule_id": schedule_id,
            "topic_id": topic["topic_id"],
            "schedule_version": version,
            "enabled": enabled,
            "cadence": cadence,
            "time_zone": time_zone,
            "local_time": local_time,
            "anchor_local_date": anchor,
            "next_due_at": next_due,
            "last_due_at": None,
            "last_attempt_at": None,
            "last_success_at": None,
            "last_status": "disabled" if not enabled else "healthy",
            "last_refresh_id": None,
            "last_briefing_id": None,
            "consecutive_failures": 0,
            "catch_up_policy": CATCH_UP_POLICY,
            "network_policy": network_policy,
            "briefing_policy": briefing_policy,
            "created_at": str((current or {}).get("created_at") or now_text),
            "updated_at": now_text,
            "schedule_digest": "",
        }
        record["schedule_digest"] = _record_digest(record, "schedule_digest")
        state["schedules"].append(record)
        runtime = _runtime(state, schedule_id)
        if runtime is None:
            runtime = {
                "schedule_id": schedule_id,
                "next_due_at": next_due,
                "last_due_at": None,
                "last_attempt_at": None,
                "last_success_at": None,
                "last_status": record["last_status"],
                "last_refresh_id": None,
                "last_briefing_id": None,
                "consecutive_failures": 0,
                "scheduler_health": record["last_status"],
                "runtime_digest": "",
            }
            state["schedule_runtime"].append(runtime)
        else:
            runtime.update(
                {
                    "next_due_at": next_due,
                    "last_status": "disabled" if not enabled else "healthy",
                    "scheduler_health": "disabled" if not enabled else "healthy",
                }
            )
        runtime["runtime_digest"] = _record_digest(runtime, "runtime_digest")
        return state, _schedule_view(state, topic)

    return workspace._mutate_state(mutation)


def _advance_due(schedule: dict[str, Any], *, now: datetime) -> tuple[str | None, int]:
    due = _parse_utc(str(schedule["next_due_at"]))
    count = 0
    cursor = due
    while cursor <= now:
        count += 1
        next_text = compute_next_due_at(
            str(schedule["cadence"]),
            str(schedule["time_zone"]),
            str(schedule["local_time"]),
            after=cursor,
            anchor_local_date=cursor.astimezone(_zone(str(schedule["time_zone"]))).date().isoformat(),
        )
        if next_text is None:
            return None, count
        cursor = _parse_utc(next_text)
    return _iso(cursor), count


def _job_key(topic: dict[str, Any], schedule: dict[str, Any], due_at: str, source_snapshot: dict[str, Any]) -> str:
    return _digest(
        {
            "topic_id": topic["topic_id"],
            "schedule_id": schedule["schedule_id"],
            "schedule_version": schedule["schedule_version"],
            "normalized_due_time": due_at,
            "source_policy_digest": _digest({"source_policy": topic["source_policy"]}),
            "query_digest": source_snapshot["query_digest"],
        }
    )


def _event_core(
    *,
    topic_id: str,
    source_ref: str,
    before_snapshot_id: str | None,
    after_snapshot_id: str | None,
    work_identity: str | None,
    event_type: str,
    event_subtype: str | None,
    materiality: str,
    severity: str,
    before_digest: str | None,
    after_digest: str | None,
    reason_code: str,
) -> dict[str, Any]:
    identity = {
        "topic_id": topic_id,
        "source_ref": source_ref,
        "before_snapshot_id": before_snapshot_id,
        "after_snapshot_id": after_snapshot_id,
        "work_identity": work_identity,
        "event_type": event_type,
        "event_subtype": event_subtype,
        "before_digest": before_digest,
        "after_digest": after_digest,
        "reason_code": reason_code,
    }
    event_signature = _digest({
        "topic_id": topic_id,
        "work_identity": work_identity,
        "event_type": event_type,
        "event_subtype": event_subtype,
        "materiality": materiality,
        "severity": severity,
        "before_digest": before_digest,
        "after_digest": after_digest,
        "reason_code": reason_code,
    })
    event = {
        "event_id": f"change-{_digest(identity)[:28]}",
        **identity,
        "event_signature": event_signature,
        "materiality": materiality,
        "severity": severity,
        "detected_at": None,
        "first_observed_refresh_id": source_ref if source_ref.startswith("refresh-") else None,
        "affected_review_ids": [],
        "affected_evidence_ids": [],
        "affected_claim_ids": [],
        "affected_report_version_ids": [],
        "event_digest": "",
    }
    return event


def compute_delta_events(
    *,
    topic_id: str,
    source_ref: str,
    before_snapshot: dict[str, Any],
    after_snapshot: dict[str, Any] | None = None,
    observed_items: list[dict[str, Any]] | None = None,
    detected_at: str | None = None,
) -> list[dict[str, Any]]:
    from app.services.prototype_workspace import PrototypeWorkspaceError

    if before_snapshot.get("topic_id") != topic_id:
        raise PrototypeWorkspaceError("living_cross_topic_delta")
    if after_snapshot is not None and after_snapshot.get("topic_id") != topic_id:
        raise PrototypeWorkspaceError("living_cross_topic_delta")
    if after_snapshot is not None and after_snapshot.get("source_policy") != before_snapshot.get("source_policy"):
        raise PrototypeWorkspaceError("living_cross_policy_delta")
    after_items = list(after_snapshot.get("items") or []) if after_snapshot is not None else list(observed_items or [])
    before_items = {str(row.get("corpus_item_id") or row.get("pmcid") or ""): row for row in before_snapshot.get("items") or []}
    after_by_id = {str(row.get("corpus_item_id") or row.get("pmcid") or ""): row for row in after_items}
    if "" in before_items or "" in after_by_id:
        raise PrototypeWorkspaceError("living_delta_work_identity_missing")
    events: list[dict[str, Any]] = []
    after_snapshot_id = str(after_snapshot.get("snapshot_id")) if after_snapshot else None

    for work_id in sorted(set(after_by_id) - set(before_items)):
        row = after_by_id[work_id]
        events.append(_event_core(
            topic_id=topic_id, source_ref=source_ref,
            before_snapshot_id=str(before_snapshot["snapshot_id"]), after_snapshot_id=after_snapshot_id,
            work_identity=work_id, event_type="paper_added", event_subtype=None,
            materiality="material", severity="attention", before_digest=None,
            after_digest=str(row.get("work_identity_digest") or row.get("source_content_digest") or ""),
            reason_code="new_eligible_work",
        ))
    for work_id in sorted(set(before_items) - set(after_by_id)):
        row = before_items[work_id]
        events.append(_event_core(
            topic_id=topic_id, source_ref=source_ref,
            before_snapshot_id=str(before_snapshot["snapshot_id"]), after_snapshot_id=after_snapshot_id,
            work_identity=work_id, event_type="paper_unavailable", event_subtype=None,
            materiality="material", severity="attention",
            before_digest=str(row.get("work_identity_digest") or row.get("source_content_digest") or ""),
            after_digest=None, reason_code="source_work_unavailable",
        ))
    for work_id in sorted(set(before_items) & set(after_by_id)):
        before, after = before_items[work_id], after_by_id[work_id]
        stale_reason: str | None = None
        before_content = str(before.get("source_content_digest") or "")
        after_content = str(after.get("source_content_digest") or "")
        if before_content and after_content and before_content != after_content:
            stale_reason = "source_content_identity_changed"
            events.append(_event_core(
                topic_id=topic_id, source_ref=source_ref,
                before_snapshot_id=str(before_snapshot["snapshot_id"]), after_snapshot_id=after_snapshot_id,
                work_identity=work_id, event_type="full_text_changed", event_subtype="content_digest_changed",
                materiality="trust_boundary", severity="critical", before_digest=before_content,
                after_digest=after_content, reason_code="source_content_identity_changed",
            ))
        before_license = str(before.get("license_status") or "")
        after_license = str(after.get("license_status") or "")
        if before_license != after_license:
            downgraded = before_license in {"accepted", "synthetic_fixture"} and after_license not in {"accepted", "synthetic_fixture"}
            if downgraded:
                stale_reason = "source_rights_changed"
            events.append(_event_core(
                topic_id=topic_id, source_ref=source_ref,
                before_snapshot_id=str(before_snapshot["snapshot_id"]), after_snapshot_id=after_snapshot_id,
                work_identity=work_id, event_type="license_changed",
                event_subtype="license_downgraded" if downgraded else "license_restored",
                materiality="trust_boundary" if downgraded else "material",
                severity="critical" if downgraded else "attention",
                before_digest=_digest({"license": before_license}), after_digest=_digest({"license": after_license}),
                reason_code="source_rights_changed",
            ))
        before_status = str(before.get("article_status") or "current")
        after_status = str(after.get("article_status") or "current")
        if before_status != after_status:
            critical = after_status in {"retracted", "expression_of_concern"}
            status_subtype = {
                "retracted": "retraction_detected",
                "expression_of_concern": "expression_of_concern_detected",
                "corrected": "correction_detected",
                "correction": "correction_detected",
            }.get(after_status, after_status)
            if critical:
                stale_reason = "article_status_changed"
            events.append(_event_core(
                topic_id=topic_id, source_ref=source_ref,
                before_snapshot_id=str(before_snapshot["snapshot_id"]), after_snapshot_id=after_snapshot_id,
                work_identity=work_id, event_type="article_status_changed", event_subtype=status_subtype,
                materiality="trust_boundary" if critical else "material",
                severity="critical" if critical else "attention",
                before_digest=_digest({"status": before_status}), after_digest=_digest({"status": after_status}),
                reason_code="article_status_changed",
            ))
        before_curation = str(before.get("curation_state") or "included")
        after_curation = str(after.get("curation_state") or "included")
        if before_curation != after_curation:
            events.append(_event_core(
                topic_id=topic_id, source_ref=source_ref,
                before_snapshot_id=str(before_snapshot["snapshot_id"]), after_snapshot_id=after_snapshot_id,
                work_identity=work_id, event_type="curation_state_changed", event_subtype=after_curation,
                materiality="material", severity="attention",
                before_digest=_digest({"curation": before_curation}), after_digest=_digest({"curation": after_curation}),
                reason_code="explicit_curation_transition",
            ))
        metadata_fields = ("title", "publication_date", "license_type")
        if not any(
            event["work_identity"] == work_id and event["event_type"] in CRITICAL_EVENT_TYPES
            for event in events
        ) and any(str(before.get(field) or "") != str(after.get(field) or "") for field in metadata_fields):
            events.append(_event_core(
                topic_id=topic_id, source_ref=source_ref,
                before_snapshot_id=str(before_snapshot["snapshot_id"]), after_snapshot_id=after_snapshot_id,
                work_identity=work_id, event_type="metadata_changed", event_subtype=None,
                materiality="non_material", severity="info",
                before_digest=_digest({field: before.get(field) for field in metadata_fields}),
                after_digest=_digest({field: after.get(field) for field in metadata_fields}),
                reason_code="bibliographic_metadata_changed",
            ))
        if stale_reason and before.get("review_id"):
            events.append(_event_core(
                topic_id=topic_id, source_ref=source_ref,
                before_snapshot_id=str(before_snapshot["snapshot_id"]), after_snapshot_id=after_snapshot_id,
                work_identity=work_id, event_type="review_became_stale", event_subtype=None,
                materiality="trust_boundary", severity="critical",
                before_digest=_digest({"review_id": str(before.get("review_id") or "")}),
                after_digest=_digest({"reason": stale_reason, "source": after_content or after_license or after_status}),
                reason_code="direct_review_source_dependency_changed",
            ))
        if stale_reason and before.get("evidence_ids"):
            events.append(_event_core(
                topic_id=topic_id, source_ref=source_ref,
                before_snapshot_id=str(before_snapshot["snapshot_id"]), after_snapshot_id=after_snapshot_id,
                work_identity=work_id, event_type="evidence_became_stale", event_subtype=None,
                materiality="trust_boundary", severity="critical",
                before_digest=_digest({"evidence_ids": sorted(before.get("evidence_ids") or [])}),
                after_digest=_digest({"reason": stale_reason, "source": after_content or after_license or after_status}),
                reason_code="direct_evidence_source_dependency_changed",
            ))
    if after_snapshot is not None:
        before_coverage = int(before_snapshot.get("included_count") or 0)
        after_coverage = int(after_snapshot.get("included_count") or 0)
        if before_coverage != after_coverage:
            increased = after_coverage > before_coverage
            events.append(_event_core(
                topic_id=topic_id, source_ref=source_ref,
                before_snapshot_id=str(before_snapshot["snapshot_id"]), after_snapshot_id=after_snapshot_id,
                work_identity=None,
                event_type="coverage_increased" if increased else "coverage_decreased",
                event_subtype=None, materiality="material", severity="attention",
                before_digest=_digest({"included_count": before_coverage}),
                after_digest=_digest({"included_count": after_coverage}),
                reason_code="accepted_corpus_coverage_changed",
            ))
    if not events:
        events.append(_event_core(
            topic_id=topic_id, source_ref=source_ref,
            before_snapshot_id=str(before_snapshot["snapshot_id"]), after_snapshot_id=after_snapshot_id,
            work_identity=None, event_type="no_material_change", event_subtype=None,
            materiality="non_material", severity="info",
            before_digest=str(before_snapshot.get("manifest_digest") or ""),
            after_digest=str((after_snapshot or {}).get("manifest_digest") or before_snapshot.get("manifest_digest") or ""),
            reason_code="no_material_source_delta",
        ))
    timestamp = detected_at or iso_now_utc()
    for event in events:
        event["detected_at"] = timestamp
        event["event_digest"] = _event_digest(event)
    return events


def _provenance_for_work(
    state: dict[str, Any],
    topic_id: str,
    work_id: str | None,
    *,
    snapshot_ids: set[str] | None = None,
) -> dict[str, list[str]]:
    if not work_id:
        return {key: [] for key in ("reviews", "evidence", "claims", "reports", "paragraphs")}
    snapshots = [
        row for row in state["snapshots"]
        if row.get("topic_id") == topic_id
        and (snapshot_ids is None or str(row.get("snapshot_id")) in snapshot_ids)
    ]
    items = [
        item for snapshot in snapshots for item in snapshot.get("items") or []
        if str(item.get("corpus_item_id") or item.get("pmcid") or "") == work_id
    ]
    return {
        "reviews": sorted({str(item.get("review_id")) for item in items if item.get("review_id")}),
        "evidence": sorted({str(value) for item in items for value in item.get("evidence_ids") or []}),
        "claims": sorted({str(value) for item in items for value in item.get("claim_ids") or []}),
        "reports": sorted({str(value) for item in items for value in item.get("report_version_ids") or []}),
        "paragraphs": sorted({str(value) for item in items for value in item.get("source_paragraph_ids") or []}),
    }


def build_impacts(state: dict[str, Any], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from app.services.prototype_workspace import PrototypeWorkspaceError

    impacts: list[dict[str, Any]] = []
    for event in events:
        event_topic = str(event["topic_id"])
        provenance_snapshot = event.get("after_snapshot_id") if event.get("event_type") == "paper_added" else event.get("before_snapshot_id")
        snapshot_ids = {str(provenance_snapshot)} if provenance_snapshot else set()
        referenced = next(
            (
                row for row in state.get("snapshots") or []
                if str(row.get("snapshot_id") or "") == str(provenance_snapshot or "")
            ),
            None,
        )
        if provenance_snapshot and referenced is None:
            raise PrototypeWorkspaceError("living_impact_snapshot_missing", status_code=409)
        if referenced is not None and str(referenced.get("topic_id") or "") != event_topic:
            raise PrototypeWorkspaceError("living_cross_topic_impact", status_code=409)
        if event.get("work_identity") and referenced is not None and not any(
            str(item.get("corpus_item_id") or item.get("pmcid") or "") == str(event["work_identity"])
            for item in referenced.get("items") or []
        ):
            raise PrototypeWorkspaceError("living_cross_snapshot_evidence_mismatch", status_code=409)
        links = _provenance_for_work(
            state,
            event_topic,
            event.get("work_identity"),
            snapshot_ids=snapshot_ids,
        )
        event["affected_review_ids"] = links["reviews"]
        event["affected_evidence_ids"] = links["evidence"]
        event["affected_claim_ids"] = links["claims"]
        event["affected_report_version_ids"] = links["reports"]
        event["source_paragraph_ids"] = links["paragraphs"]
        event["event_digest"] = _event_digest(event)
        event_type = str(event["event_type"])
        if event_type == "paper_added":
            states = [("new_source_unreviewed", "review_new_source")]
        elif event_type == "paper_unavailable":
            states = [("coverage_loss", "review_source_availability")]
        elif event_type == "full_text_changed":
            states = [
                ("review_stale", "re_review_changed_source"),
                ("evidence_stale", "revalidate_source_evidence"),
                ("claim_review_required", "review_directly_dependent_claims"),
            ]
        elif event_type == "review_became_stale":
            states = [("review_stale", "re_review_changed_source")]
        elif event_type == "evidence_became_stale":
            states = [("evidence_stale", "revalidate_source_evidence")]
        elif event_type in {"license_changed", "article_status_changed"} and event["severity"] == "critical":
            states = [("claim_invalidated_source_untrusted", "review_source_trust_boundary")]
        elif event_type == "curation_state_changed":
            states = [("coverage_loss", "review_curated_coverage")]
        elif event_type == "coverage_increased":
            states = [("coverage_increased", "review_new_coverage")]
        elif event_type == "coverage_decreased":
            states = [("coverage_loss", "review_reduced_coverage")]
        elif event_type == "metadata_changed":
            states = [("unaffected", "no_review_staleness_for_metadata_only_change")]
        elif event_type == "no_material_change":
            states = [("unaffected", "no_report_regeneration")]
        else:
            states = [("review_required", "review_changed_source")]
        for state_name, action in states:
            identity = {"event_id": event["event_id"], "impact_state": state_name}
            impact = {
                "impact_id": f"impact-{_digest(identity)[:28]}",
                "topic_id": event["topic_id"],
                "change_event_id": event["event_id"],
                "source_snapshot_id": event.get("before_snapshot_id"),
                "target_snapshot_id": event.get("after_snapshot_id"),
                "work_identity": event.get("work_identity"),
                "review_ids": links["reviews"],
                "evidence_ids": links["evidence"],
                "claim_ids": links["claims"],
                "report_version_ids": links["reports"],
                "source_paragraph_ids": links["paragraphs"],
                "impact_state": state_name,
                "direct_dependency_reason": event["reason_code"],
                "severity": event["severity"],
                "recommended_action": action,
                "impact_digest": "",
            }
            impact["impact_digest"] = _record_digest(impact, "impact_digest")
            impacts.append(impact)
    return impacts


def _briefing_status(events: list[dict[str, Any]]) -> str:
    if any(event["severity"] == "critical" for event in events):
        return "CRITICAL_ATTENTION"
    if any(event["severity"] == "attention" for event in events):
        return "REVIEW_REQUIRED"
    if all(event["event_type"] == "no_material_change" for event in events):
        return "NO_MATERIAL_CHANGE"
    return "READY"


def _build_briefing(
    state: dict[str, Any],
    *,
    topic_id: str,
    briefing_type: str,
    source_ref: str,
    schedule: dict[str, Any] | None,
    refresh_id: str | None,
    before_snapshot: dict[str, Any],
    after_snapshot: dict[str, Any] | None,
    events: list[dict[str, Any]],
    impacts: list[dict[str, Any]],
    generated_at: str,
    source_failure: bool = False,
) -> dict[str, Any]:
    event_ids = sorted(event["event_id"] for event in events)
    briefing_identity = {
        "topic_id": topic_id,
        "briefing_type": briefing_type,
        "source_ref": source_ref,
        "event_ids": event_ids,
    }
    prior = sorted(
        (row for row in state["briefings"] if row.get("topic_id") == topic_id),
        key=lambda row: str(row.get("generated_at") or ""),
    )
    before_coverage = int(before_snapshot.get("included_count") or 0)
    after_coverage = int((after_snapshot or {}).get("included_count") or before_coverage)
    material = [event for event in events if event["materiality"] != "non_material"]
    critical = [event for event in events if event["severity"] == "critical"]
    status = "SOURCE_UNAVAILABLE" if source_failure else _briefing_status(events)
    headline = (
        "Official source was unavailable; the active snapshot was preserved."
        if source_failure
        else "No material source change was observed."
        if status == "NO_MATERIAL_CHANGE"
        else f"{len(material)} material Living Topic change{'s' if len(material) != 1 else ''} require review."
    )
    briefing = {
        "briefing_id": f"briefing-{_digest(briefing_identity)[:28]}",
        "topic_id": topic_id,
        "briefing_version": len(prior) + 1,
        "parent_briefing_id": prior[-1]["briefing_id"] if prior else None,
        "schedule_id": (schedule or {}).get("schedule_id"),
        "schedule_version": (schedule or {}).get("schedule_version"),
        "refresh_id": refresh_id,
        "source_snapshot_id": before_snapshot["snapshot_id"],
        "observed_snapshot_id": (after_snapshot or {}).get("snapshot_id"),
        "briefing_type": briefing_type,
        "status": status,
        "period_start": (schedule or {}).get("last_due_at") or generated_at,
        "period_end": generated_at,
        "generated_at": generated_at,
        "headline": headline,
        "executive_summary": headline,
        "what_changed": sorted({event["event_type"] for event in events}),
        "why_it_matters": sorted({event["reason_code"] for event in events}),
        "change_event_ids": event_ids,
        "impact_record_ids": sorted(impact["impact_id"] for impact in impacts),
        "paper_ids": sorted({str(event["work_identity"]) for event in events if event.get("work_identity")}),
        "review_ids": sorted({value for impact in impacts for value in impact["review_ids"]}),
        "evidence_ids": sorted({value for impact in impacts for value in impact["evidence_ids"]}),
        "claim_ids": sorted({value for impact in impacts for value in impact["claim_ids"]}),
        "report_version_ids": sorted({value for impact in impacts for value in impact["report_version_ids"]}),
        "source_paragraph_ids": sorted({value for impact in impacts for value in impact["source_paragraph_ids"]}),
        "coverage_before": before_coverage,
        "coverage_after": after_coverage,
        "recommended_actions": sorted({impact["recommended_action"] for impact in impacts}),
        "attention_count": sum(1 for event in events if event["severity"] == "attention"),
        "critical_count": len(critical),
        "acknowledged": False,
        "dismissed": False,
        "briefing_digest": "",
    }
    briefing["briefing_digest"] = _record_digest(briefing, "briefing_digest")
    return briefing


def _store_artifacts(
    state: dict[str, Any],
    events: list[dict[str, Any]],
    impacts: list[dict[str, Any]],
    briefing: dict[str, Any],
) -> dict[str, Any]:
    existing_events = {row["event_id"] for row in state["change_events"]}
    existing_impacts = {row["impact_id"] for row in state["impact_records"]}
    existing_briefings = {row["briefing_id"] for row in state["briefings"]}
    state["change_events"].extend(row for row in events if row["event_id"] not in existing_events)
    state["impact_records"].extend(row for row in impacts if row["impact_id"] not in existing_impacts)
    if briefing["briefing_id"] not in existing_briefings:
        state["briefings"].append(briefing)
    return next(row for row in state["briefings"] if row["briefing_id"] == briefing["briefing_id"])


def create_curation_delta_briefing(
    topic_id: str,
    before_snapshot_id: str,
    after_snapshot_id: str,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    from app.services import prototype_workspace as workspace

    timestamp = generated_at or iso_now_utc()

    def mutation(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        workspace._topic(state, topic_id)
        before = workspace._snapshot(state, before_snapshot_id, topic_id=topic_id)
        after = workspace._snapshot(state, after_snapshot_id, topic_id=topic_id)
        source_ref = f"curation-delta:{before_snapshot_id}:{after_snapshot_id}"
        events = compute_delta_events(
            topic_id=topic_id,
            source_ref=source_ref,
            before_snapshot=before,
            after_snapshot=after,
            detected_at=timestamp,
        )
        impacts = build_impacts(state, events)
        briefing = _build_briefing(
            state,
            topic_id=topic_id,
            briefing_type="curation_delta",
            source_ref=source_ref,
            schedule=None,
            refresh_id=None,
            before_snapshot=before,
            after_snapshot=after,
            events=events,
            impacts=impacts,
            generated_at=timestamp,
        )
        stored = _store_artifacts(state, events, impacts, briefing)
        return state, deepcopy(stored)

    return workspace._mutate_state(mutation)


def _source_failure_artifacts(
    state: dict[str, Any],
    *,
    topic: dict[str, Any],
    schedule: dict[str, Any],
    refresh: dict[str, Any],
    source_snapshot: dict[str, Any],
) -> dict[str, Any]:
    identity = {
        "topic_id": topic["topic_id"],
        "refresh_id": refresh["refresh_id"],
        "failure_category": refresh.get("failure_category"),
    }
    event = _event_core(
        topic_id=str(topic["topic_id"]), source_ref=str(refresh["refresh_id"]),
        before_snapshot_id=str(source_snapshot["snapshot_id"]), after_snapshot_id=None,
        work_identity=None, event_type="paper_unavailable", event_subtype="source_failure",
        materiality="material", severity="attention",
        before_digest=str(source_snapshot.get("manifest_digest") or ""), after_digest=None,
        reason_code="official_source_unavailable",
    )
    event["event_id"] = f"change-{_digest(identity)[:28]}"
    event["detected_at"] = str(refresh.get("completed_at") or iso_now_utc())
    event["event_digest"] = _event_digest(event)
    events = [event]
    impacts = build_impacts(state, events)
    briefing = _build_briefing(
        state,
        topic_id=str(topic["topic_id"]), briefing_type="source_failure",
        source_ref=str(refresh["refresh_id"]), schedule=schedule,
        refresh_id=str(refresh["refresh_id"]), before_snapshot=source_snapshot,
        after_snapshot=None, events=events, impacts=impacts,
        generated_at=str(refresh.get("completed_at") or iso_now_utc()), source_failure=True,
    )
    return _store_artifacts(state, events, impacts, briefing)


def finalize_refresh_artifacts(refresh_id: str) -> dict[str, Any]:
    from app.services import prototype_workspace as workspace

    def mutation(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        refresh = next((row for row in state["refresh_records"] if row.get("refresh_id") == refresh_id), None)
        if not refresh:
            raise workspace.PrototypeWorkspaceError("living_refresh_not_found", status_code=404)
        if refresh.get("briefing_id"):
            briefing = next(row for row in state["briefings"] if row["briefing_id"] == refresh["briefing_id"])
            return state, deepcopy(briefing)
        topic = workspace._topic(state, str(refresh["topic_id"]))
        source = workspace._snapshot(state, str(refresh["source_snapshot_id"]), topic_id=str(topic["topic_id"]))
        schedule_id = str(refresh.get("schedule_id") or "")
        schedule_version = int(refresh.get("schedule_version") or 0)
        schedule = (
            _schedule_version(state, schedule_id, schedule_version)
            if schedule_id and schedule_version
            else None
        ) or {}
        if refresh.get("status") == "FAILED_SOURCE_UNAVAILABLE":
            briefing = _source_failure_artifacts(
                state, topic=topic, schedule=schedule, refresh=refresh, source_snapshot=source
            )
        else:
            changeset = next(
                (row for row in state["changesets"] if row.get("changeset_id") == refresh.get("changeset_id")),
                None,
            )
            observed_items = list(
                (changeset or {}).get("observation_items")
                or (changeset or {}).get("proposed_items")
                or source.get("items")
                or []
            )
            events = compute_delta_events(
                topic_id=str(topic["topic_id"]), source_ref=str(refresh["refresh_id"]),
                before_snapshot=source, observed_items=observed_items,
                detected_at=str(refresh.get("completed_at") or iso_now_utc()),
            )
            impacts = build_impacts(state, events)
            briefing_type = (
                "no_material_change" if refresh.get("status") == "NO_MATERIAL_CHANGE"
                else "catch_up_update" if refresh.get("execution_mode") == "catch_up"
                else "periodic_update"
            )
            briefing = _build_briefing(
                state, topic_id=str(topic["topic_id"]), briefing_type=briefing_type,
                source_ref=str(refresh["refresh_id"]), schedule=schedule,
                refresh_id=str(refresh["refresh_id"]), before_snapshot=source,
                after_snapshot=None, events=events, impacts=impacts,
                generated_at=str(refresh.get("completed_at") or iso_now_utc()),
            )
            briefing = _store_artifacts(state, events, impacts, briefing)
        refresh["briefing_id"] = briefing["briefing_id"]
        refresh["refresh_digest"] = workspace._digest(
            {key: value for key, value in refresh.items() if key != "refresh_digest"}
        )
        return state, deepcopy(briefing)

    return workspace._mutate_state(mutation)


def finalize_refresh_result(result: dict[str, Any]) -> dict[str, Any]:
    """Attach the one authoritative briefing while preserving the v0.4 API shape."""
    from app.services import prototype_workspace as workspace

    enveloped = isinstance(result.get("refresh"), dict)
    refresh = result["refresh"] if enveloped else result
    briefing = finalize_refresh_artifacts(str(refresh["refresh_id"]))
    persisted = workspace._load_state()
    updated = deepcopy(next(
        row for row in persisted["refresh_records"]
        if row.get("refresh_id") == refresh["refresh_id"]
    ))
    if enveloped:
        return {**result, "refresh": updated, "briefing": briefing}
    return {**updated, "briefing": briefing}


def _claim_due_job(
    topic_id: str,
    *,
    now: datetime,
    worker_id: str,
    claim_only: bool,
) -> dict[str, Any]:
    from app.services import prototype_workspace as workspace

    def mutation(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        topic = workspace._topic(state, topic_id)
        schedule = _latest_schedule(state, topic_id)
        if not schedule or not schedule.get("enabled") or topic.get("archived"):
            raise workspace.PrototypeWorkspaceError("living_schedule_not_dispatchable", status_code=409)
        runtime = _runtime(state, str(schedule["schedule_id"]))
        due_at = str((runtime or {}).get("next_due_at") or schedule.get("next_due_at") or "")
        active_jobs = [
            row for row in state["schedule_jobs"]
            if row.get("topic_id") == topic_id and row.get("status") in ACTIVE_JOB_STATES
        ]
        expired = next(
            (row for row in active_jobs if _parse_utc(str(row.get("lease_expires_at"))) <= now),
            None,
        )
        if expired:
            if expired.get("network_started"):
                expired.update(
                    {
                        "status": "FAILED",
                        "completed_at": _iso(now),
                        "failure_category": "expired_after_network_start_no_retry",
                    }
                )
                expired["job_digest"] = _record_digest(expired, "job_digest")
                if runtime:
                    runtime.update(
                        {
                            "last_status": "degraded",
                            "scheduler_health": "degraded",
                            "consecutive_failures": int(runtime.get("consecutive_failures") or 0) + 1,
                        }
                    )
                    runtime["runtime_digest"] = _record_digest(runtime, "runtime_digest")
                return state, {"job": deepcopy(expired), "duplicate": False, "execute": False}
            if not claim_only:
                expired.update(
                    {
                        "status": "RUNNING",
                        "claimed_at": _iso(now),
                        "lease_expires_at": _iso(now + timedelta(minutes=5)),
                        "owner_instance_digest": _digest({"worker": worker_id}),
                        "recovered": True,
                    }
                )
                expired["job_digest"] = _record_digest(expired, "job_digest")
                return state, {"job": deepcopy(expired), "duplicate": False, "execute": True}
            return state, {"job": deepcopy(expired), "duplicate": True, "execute": False}
        if not due_at or _parse_utc(due_at) > now:
            previous = sorted(
                (
                    row for row in state["schedule_jobs"]
                    if row.get("schedule_id") == schedule["schedule_id"]
                    and int(row.get("schedule_version") or 0) == int(schedule["schedule_version"])
                    and _parse_utc(str(row.get("due_at"))) <= now
                ),
                key=lambda row: str(row.get("due_at") or ""),
                reverse=True,
            )
            if previous:
                return state, {"job": deepcopy(previous[0]), "duplicate": True, "execute": False}
            raise workspace.PrototypeWorkspaceError("living_schedule_not_due", status_code=409)
        source_id = topic.get("active_snapshot_id")
        if not source_id:
            raise workspace.PrototypeWorkspaceError("refresh_source_snapshot_required", status_code=409)
        source = workspace._snapshot(state, str(source_id), topic_id=topic_id)
        key = _job_key(topic, schedule, due_at, source)
        existing = next((row for row in state["schedule_jobs"] if row.get("idempotency_key") == key), None)
        if existing:
            return state, {"job": deepcopy(existing), "duplicate": True, "execute": False}
        active = next(
            (row for row in active_jobs if _parse_utc(str(row.get("lease_expires_at"))) > now),
            None,
        )
        if active:
            raise workspace.PrototypeWorkspaceError("living_topic_refresh_overlap", status_code=409)
        next_due, due_windows = _advance_due({**schedule, "next_due_at": due_at}, now=now)
        mode = "catch_up" if due_windows > 1 else "scheduled"
        job = {
            "job_id": f"living-job-{key[:24]}",
            "topic_id": topic_id,
            "schedule_id": schedule["schedule_id"],
            "schedule_version": schedule["schedule_version"],
            "due_at": due_at,
            "idempotency_key": key,
            "source_snapshot_id": source["snapshot_id"],
            "query_digest": source["query_digest"],
            "execution_mode": mode,
            "missed_intervals": max(0, due_windows - 1),
            "claimed_at": _iso(now),
            "lease_expires_at": _iso(now + timedelta(minutes=5)),
            "attempt_number": 1,
            "status": "PENDING" if claim_only else "RUNNING",
            "owner_instance_digest": _digest({"worker": worker_id}) if worker_id else _digest({"worker": "local"}),
            "network_started": False,
            "recovered": False,
            "refresh_id": None,
            "briefing_id": None,
            "completed_at": None,
            "failure_category": None,
            "job_digest": "",
        }
        job["job_digest"] = _record_digest(job, "job_digest")
        state["schedule_jobs"].append(job)
        if runtime:
            runtime.update(
                {
                    "next_due_at": next_due,
                    "last_due_at": due_at,
                    "last_attempt_at": _iso(now),
                    "last_status": "running",
                    "scheduler_health": "running",
                }
            )
            runtime["runtime_digest"] = _record_digest(runtime, "runtime_digest")
        return state, {"job": deepcopy(job), "duplicate": False, "execute": not claim_only}

    return workspace._mutate_state(mutation)


def _mark_network_started(job_id: str) -> None:
    from app.services import prototype_workspace as workspace

    def mutation(state: dict[str, Any]) -> tuple[dict[str, Any], None]:
        job = next(row for row in state["schedule_jobs"] if row["job_id"] == job_id)
        job["network_started"] = True
        job["status"] = "RUNNING"
        job["job_digest"] = _record_digest(job, "job_digest")
        return state, None

    workspace._mutate_state(mutation)


def _complete_job(job_id: str, refresh: dict[str, Any], briefing: dict[str, Any]) -> dict[str, Any]:
    from app.services import prototype_workspace as workspace

    def mutation(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        job = next(row for row in state["schedule_jobs"] if row["job_id"] == job_id)
        schedule = next(
            row for row in state["schedules"]
            if row["schedule_id"] == job["schedule_id"] and row["schedule_version"] == job["schedule_version"]
        )
        runtime = _runtime(state, str(schedule["schedule_id"]))
        failed = str(refresh.get("status") or "").startswith("FAILED_")
        job.update(
            {
                "status": "FAILED" if failed else "COMPLETED",
                "refresh_id": refresh["refresh_id"],
                "briefing_id": briefing["briefing_id"],
                "completed_at": str(refresh.get("completed_at") or iso_now_utc()),
                "failure_category": refresh.get("failure_category"),
            }
        )
        job["job_digest"] = _record_digest(job, "job_digest")
        if runtime:
            runtime.update(
                {
                    "last_status": "source_unavailable" if failed else "healthy",
                    "scheduler_health": "source_unavailable" if failed else "healthy",
                    "last_refresh_id": refresh["refresh_id"],
                    "last_briefing_id": briefing["briefing_id"],
                    "last_success_at": runtime.get("last_success_at") if failed else refresh.get("completed_at"),
                    "consecutive_failures": int(runtime.get("consecutive_failures") or 0) + 1 if failed else 0,
                }
            )
            runtime["runtime_digest"] = _record_digest(runtime, "runtime_digest")
        return state, deepcopy(job)

    return workspace._mutate_state(mutation)


def execute_due_schedule(
    topic_id: str,
    *,
    now: str | datetime | None = None,
    worker_id: str = "prototype-living-worker",
    source_probe: Callable[[], dict[str, Any]] | None = None,
    claim_only: bool = False,
) -> dict[str, Any]:
    from app.services import prototype_workspace as workspace

    now_utc = _parse_utc(now)
    boundary_state = workspace._load_state()
    workspace._require_bundle_execution_allowed(workspace._topic(boundary_state, topic_id))
    claim = _claim_due_job(topic_id, now=now_utc, worker_id=worker_id, claim_only=claim_only)
    if not claim["execute"]:
        job = claim["job"]
        if job.get("refresh_id"):
            state = workspace._load_state()
            refresh = next(
                row for row in state["refresh_records"] if row.get("refresh_id") == job["refresh_id"]
            )
            briefing = next(
                row for row in state["briefings"] if row.get("briefing_id") == job.get("briefing_id")
            )
            return {
                **claim,
                "refresh": deepcopy(refresh),
                "briefing": deepcopy(briefing),
            }
        return claim
    job = claim["job"]
    state = workspace._load_state()
    schedule = _schedule_version(
        state,
        str(job["schedule_id"]),
        int(job["schedule_version"]),
    )
    if not schedule or schedule.get("network_policy") != "scheduled_official_source_only":
        raise workspace.PrototypeWorkspaceError("living_schedule_network_policy_blocked", status_code=409)
    _mark_network_started(str(job["job_id"]))
    result = workspace.check_for_corpus_updates(
        topic_id,
        confirmed=True,
        probe=source_probe,
        schedule_id=str(schedule["schedule_id"]),
        schedule_version=int(schedule["schedule_version"]),
        due_at=str(job["due_at"]),
        idempotency_key=str(job["idempotency_key"]),
        execution_mode=str(job["execution_mode"]),
        network_policy=str(schedule["network_policy"]),
    )
    finalized = finalize_refresh_result(result)
    refresh = finalized.get("refresh") if isinstance(finalized.get("refresh"), dict) else finalized
    briefing = finalized["briefing"]
    completed = _complete_job(str(job["job_id"]), refresh, briefing)
    return {"job": completed, "refresh": refresh, "briefing": briefing, "duplicate": False}


def run_scheduler_cycle(
    *,
    now: str | datetime | None = None,
    worker_id: str = "prototype-living-worker",
    probe_by_topic: dict[str, Callable[[], dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    from app.services import prototype_workspace as workspace

    now_utc = _parse_utc(now)
    state = workspace._load_state()
    due_topics = []
    for topic in state["topics"]:
        schedule = _schedule_view(state, topic)
        if (
            schedule.get("enabled")
            and not topic.get("archived")
            and not topic.get("imported_project_id")
            and schedule.get("next_due_at")
            and _parse_utc(str(schedule["next_due_at"])) <= now_utc
        ):
            due_topics.append(str(topic["topic_id"]))
    executed, errors = [], []
    for topic_id in due_topics:
        try:
            result = execute_due_schedule(
                topic_id,
                now=now_utc,
                worker_id=worker_id,
                source_probe=(probe_by_topic or {}).get(topic_id),
            )
            executed.append(result)
        except workspace.PrototypeWorkspaceError as exc:
            errors.append({"topic_id": topic_id, "code": exc.code})
    return {
        "executed": executed,
        "errors": errors,
        "due_topic_count": len(due_topics),
        "provider_calls": 0,
    }


def acknowledge_attention(
    topic_id: str,
    briefing_id: str,
    change_event_id: str,
    *,
    action: str,
    occurred_at: str | None = None,
) -> dict[str, Any]:
    from app.services import prototype_workspace as workspace

    if action not in {"acknowledge", "dismiss"}:
        raise workspace.PrototypeWorkspaceError("living_attention_action_invalid", status_code=400)
    timestamp = occurred_at or iso_now_utc()

    def mutation(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        workspace._topic(state, topic_id)
        briefing = next(
            (row for row in state["briefings"] if row.get("briefing_id") == briefing_id), None
        )
        event = next(
            (row for row in state["change_events"] if row.get("event_id") == change_event_id), None
        )
        if not briefing or not event or briefing.get("topic_id") != topic_id or event.get("topic_id") != topic_id:
            raise workspace.PrototypeWorkspaceError("living_attention_boundary_invalid", status_code=404)
        if change_event_id not in briefing.get("change_event_ids", []):
            raise workspace.PrototypeWorkspaceError("living_attention_boundary_invalid", status_code=409)
        if action == "dismiss" and event.get("severity") == "critical":
            raise workspace.PrototypeWorkspaceError("living_critical_alert_not_dismissible", status_code=409)
        identity = {"briefing_id": briefing_id, "change_event_id": change_event_id, "action": action}
        record = {
            "attention_event_id": f"attention-{_digest(identity)[:28]}",
            "topic_id": topic_id,
            "briefing_id": briefing_id,
            "change_event_id": change_event_id,
            "state": "acknowledged" if action == "acknowledge" else "dismissed",
            "occurred_at": timestamp,
            "actor_mode": "LOCAL_PROTOTYPE_USER",
            "attention_digest": "",
        }
        record["attention_digest"] = _record_digest(record, "attention_digest")
        existing = next(
            (row for row in state["attention_events"] if row["attention_event_id"] == record["attention_event_id"]),
            None,
        )
        if existing:
            return state, deepcopy(existing)
        state["attention_events"].append(record)
        return state, deepcopy(record)

    return workspace._mutate_state(mutation)


def compare_briefings(topic_id: str, before_id: str, after_id: str) -> dict[str, Any]:
    from app.services import prototype_workspace as workspace

    state = workspace._load_state()
    workspace._topic(state, topic_id)
    before = next((row for row in state["briefings"] if row.get("briefing_id") == before_id), None)
    after = next((row for row in state["briefings"] if row.get("briefing_id") == after_id), None)
    if not before or not after or before.get("topic_id") != topic_id or after.get("topic_id") != topic_id:
        raise workspace.PrototypeWorkspaceError("living_briefing_compare_boundary_invalid", status_code=409)
    before_events = set(before.get("change_event_ids") or [])
    after_events = set(after.get("change_event_ids") or [])
    return {
        "topic_id": topic_id,
        "before_briefing_id": before_id,
        "after_briefing_id": after_id,
        "added_change_event_ids": sorted(after_events - before_events),
        "resolved_change_event_ids": sorted(before_events - after_events),
        "retained_change_event_ids": sorted(before_events & after_events),
        "coverage_delta": int(after.get("coverage_after") or 0) - int(before.get("coverage_after") or 0),
        "critical_delta": int(after.get("critical_count") or 0) - int(before.get("critical_count") or 0),
        "comparison_digest": _digest({"before": before["briefing_digest"], "after": after["briefing_digest"]}),
    }


def export_briefing(topic_id: str, briefing_id: str) -> dict[str, Any]:
    from app.services import prototype_workspace as workspace

    state = workspace._load_state()
    topic = workspace._topic(state, topic_id)
    briefing = next((row for row in state["briefings"] if row.get("briefing_id") == briefing_id), None)
    if not briefing or briefing.get("topic_id") != topic_id:
        raise workspace.PrototypeWorkspaceError("living_briefing_not_found", status_code=404)
    lines = [
        f"# {briefing['headline']}",
        "",
        f"Topic: {topic['display_name']}",
        f"Status: {briefing['status']}",
        f"Briefing: {briefing['briefing_id']}",
        "",
        "## What changed",
        *[f"- {value}" for value in briefing.get("what_changed") or []],
        "",
        "## Why it matters",
        *[f"- {value}" for value in briefing.get("why_it_matters") or []],
        "",
        "## Recommended next actions",
        *[f"- {value}" for value in briefing.get("recommended_actions") or []],
        "",
        "## Provenance",
        *[f"- Change event: {value}" for value in briefing.get("change_event_ids") or []],
        *[f"- Report: {value}" for value in briefing.get("report_version_ids") or []],
        *[f"- Evidence: {value}" for value in briefing.get("evidence_ids") or []],
    ]
    content = "\n".join(lines).strip() + "\n"
    return {
        "briefing_id": briefing_id,
        "topic_id": topic_id,
        "format": "markdown",
        "content": content,
        "content_digest": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "provider_calls": 0,
    }
