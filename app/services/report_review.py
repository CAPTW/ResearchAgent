"""Explicit human review of saved report versions, separate from Provider acceptance.

Only this append operation writes the ledger. CAS and replay checks hold the
same local OS lock as version publication; JSONL replacement is crash-safe.
"""

from typing import Any

from app.domain.temporal_memory import digest
from app.repositories.files import iso_now_utc, read_jsonl, write_jsonl
from app.repositories.runs import build_run_paths
from app.services import report_versions


def _records(run_id: str, version_id: str) -> list[dict[str, Any]]:
    if report_versions._get_version(run_id, version_id) is None:
        raise FileNotFoundError("report_version_not_found")
    return [item for item in read_jsonl(build_run_paths(run_id).reports_dir / "version_decisions.jsonl")
            if item["version_id"] == version_id]


def history(run_id: str, version_id: str, limit: int = 10, page: int = 1) -> dict[str, Any]:
    if not 1 <= limit <= 100 or page < 1:
        raise ValueError("pagination_invalid")
    records = list(reversed(_records(run_id, version_id)))
    return dict(items=records[(page - 1) * limit:page * limit], total=len(records), limit=limit, page=page,
                latest_id=records[0]["decision_id"] if records else None)


def decide(run_id: str, version_id: str, *, request_id: str, state: str, note: str,
           expected_previous_id: str | None, expected_binding: str, actor_id: str) -> tuple[dict[str, Any], bool]:
    if state not in {"accepted", "held", "rejected"} or not 1 <= len(request_id) <= 100 or len(note) > 2000 or not actor_id:
        raise ValueError("invalid_decision")
    with report_versions.version_write_lock(run_id):
        records = _records(run_id, version_id)
        fields = dict(request_id=request_id, state=state, note=note, previous_id=expected_previous_id,
                      review_binding=expected_binding, actor_id=actor_id)
        existing = next((item for item in records if item["request_id"] == request_id), None)
        if existing:
            if any(existing.get(key) != value for key, value in fields.items()):
                raise ValueError("decision_request_conflict")
            return existing, False
        if (records[-1]["decision_id"] if records else None) != expected_previous_id:
            raise ValueError("decision_conflict")
        detail = report_versions.get_report_version_detail(run_id, version_id)
        if expected_binding != detail["review_binding"]:
            raise ValueError("report_binding_changed")
        if state == "accepted" and not detail["review_eligible"]:
            raise ValueError("report_not_eligible")
        version = detail["version"]
        record = dict(**fields, decision_id="repdec_" + digest([run_id, version_id, request_id]),
                      run_id=run_id, version_id=version_id, created_at=iso_now_utc(),
                      schema_version="REPORT_REVIEW_V1", content_sha256=version.get("content_sha256"),
                      content_hash=version.get("content_hash"), citations_snapshot=version.get("citations_snapshot"),
                      content_integrity=detail["content_integrity"], citation_status=detail["citations"]["status"])
        ledger = build_run_paths(run_id).reports_dir / "version_decisions.jsonl"
        # Atomic replacement preserves all prior records if a write fails. A
        # raw JSONL append can leave a partial trailing line after interruption.
        write_jsonl(ledger, [*read_jsonl(ledger), record])
        return record, True
