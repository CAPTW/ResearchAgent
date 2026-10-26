from __future__ import annotations

import asyncio
import json
from typing import Any

from app.domain.constants import TERMINAL_RUN_STATUSES
from app.repositories.jsonl import JsonlPolicy, inspect_jsonl
from app.repositories.runs import build_run_paths, load_run_record


def _validate_run_event(record: dict[str, Any]) -> None:
    required = {"run_id", "type", "timestamp", "payload"}
    if required - set(record):
        raise ValueError("run_event_schema_invalid")
    if not all(isinstance(record[field], str) and record[field] for field in ("run_id", "type", "timestamp")):
        raise ValueError("run_event_schema_invalid")
    if not isinstance(record["payload"], dict):
        raise ValueError("run_event_schema_invalid")


_RUN_EVENT_POLICY = JsonlPolicy(
    artifact_role="run_events",
    record_validator=_validate_run_event,
)


async def iter_sse_events(run_id: str):
    paths = build_run_paths(run_id)
    sent = 0
    idle_cycles = 0
    last_integrity_state: tuple[int, str | None, int | None] | None = None

    while True:
        inspection = inspect_jsonl(paths.events_path, policy=_RUN_EVENT_POLICY)
        while sent < len(inspection.records):
            payload = json.dumps(
                inspection.records[sent],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            yield f"data: {payload}\n\n"
            sent += 1
            idle_cycles = 0

        if not inspection.complete:
            integrity_state = (
                inspection.issue_count,
                inspection.first_issue_code,
                inspection.first_issue_line_number,
            )
            if integrity_state != last_integrity_state:
                diagnostic = json.dumps(
                    {
                        "complete": False,
                        "degraded": True,
                        "issueCount": inspection.issue_count,
                        "firstIssueCode": inspection.first_issue_code,
                        "firstIssueLine": inspection.first_issue_line_number,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
                yield f"event: integrity\ndata: {diagnostic}\n\n"
                last_integrity_state = integrity_state

        run = load_run_record(run_id)
        if run.get("status") in TERMINAL_RUN_STATUSES:
            idle_cycles += 1
            if idle_cycles >= 2:
                yield "event: end\ndata: {}\n\n"
                break

        yield ": keepalive\n\n"
        await asyncio.sleep(1.0)

