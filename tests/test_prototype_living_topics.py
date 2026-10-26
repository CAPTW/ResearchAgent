from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.services import prototype_living_topics as living
from app.services import prototype_real_corpus as real
from app.services import prototype_workspace as workspace
from app.services import topic_schedules
from app.services.topic_scheduler import TopicScheduleWorker
from tests.test_prototype_real_corpus import _fixture_network
from tests.test_prototype_workspace import (
    _acquire_real,
    _established_real_candidates,
    _hold_preview,
)


@pytest.fixture
def living_client(tmp_path: Path, app_factory, monkeypatch):
    data_root = (tmp_path / "prototype-v0.5-data").resolve()
    fixture_clients = [_fixture_network(candidates=_established_real_candidates()) for _ in range(8)]
    monkeypatch.setattr(real, "PmcNetworkClient", lambda: fixture_clients.pop(0))
    env = {
        "AUTH_MODE": "disabled",
        "XRML_PROTOTYPE_MODE": "1",
        "XRML_PROTOTYPE_REAL_CORPUS_NETWORK": "1",
        "XRML_PROTOTYPE_LIVING_SCHEDULER_ENABLED": "0",
        "XRML_DATA_DIR": str(data_root),
        "XRML_RUN_EXECUTOR_POLL_SECONDS": "0.05",
    }
    with TestClient(app_factory(**env)) as client:
        yield client, data_root, env


def _current_observation(client: TestClient, *, requests: int = 1) -> dict:
    topic = client.get(f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}").json()
    articles = []
    for item in topic["active_snapshot"]["items"]:
        articles.append(
            {
                "pmcid": item["pmcid"],
                "pmid": item.get("pmid"),
                "doi": item.get("doi"),
                "title": item["title"],
                "publication_date": item.get("publication_date"),
                "license_type": item.get("license_type"),
                "rights_accepted": True,
                "article_identity_digest": item["work_identity_digest"],
                "article_status": "current",
            }
        )
    return {
        "articles": articles,
        "candidate_count": len(articles),
        "license_eligible_count": len(articles),
        "network": {
            "request_count": requests,
            "total_downloaded_bytes": 1024,
            "retries": 0,
            "non_allowlisted_requests": 0,
        },
    }


def _enable_weekly(now: str = "2026-01-05T00:00:00Z") -> dict:
    return living.save_schedule(
        workspace.REAL_TOPIC_ID,
        {
            "enabled": True,
            "cadence": "weekly",
            "time_zone": "Asia/Seoul",
            "local_time": "09:00",
            "network_policy": "scheduled_official_source_only",
            "briefing_policy": "no_change_and_material_changes",
        },
        now=now,
    )


def test_living_schedule_defaults_validation_and_versions(living_client) -> None:
    client, _data_root, _env = living_client
    payload = client.get("/prototype/workspace").json()
    assert len(payload["topics"]) == 3
    assert all(topic["living_schedule"]["enabled"] is False for topic in payload["topics"])
    assert payload["totals"]["schedules_enabled"] == 0
    assert payload["totals"]["living_topics"] == 0

    created = client.post(
        "/prototype/workspace/topics",
        json={
            "display_name": "Local transfer replication",
            "description": "Local fixture only.",
            "research_question": "Can a local replication preserve provenance?",
            "topic_kind": "local_empty",
            "source_policy": "local_only_no_external_source",
            "language": "en",
            "publication_date_cutoff": None,
            "license_policy": "local_only",
            "refresh_mode": "disabled",
        },
    ).json()
    assert created["living_schedule"]["schedule_id"] is None

    invalid_zone = client.put(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/schedule",
        json={"enabled": True, "cadence": "weekly", "time_zone": "Not/AZone", "local_time": "09:00", "network_policy": "scheduled_official_source_only"},
    )
    assert invalid_zone.status_code == 400
    invalid_cadence = client.put(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/schedule",
        json={"enabled": True, "cadence": "cron", "time_zone": "UTC", "local_time": "09:00", "network_policy": "scheduled_official_source_only"},
    )
    assert invalid_cadence.status_code == 422
    for topic_id in (workspace.SYNTHETIC_TOPIC_ID, workspace.LOCAL_TOPIC_ID):
        denied = client.put(
            f"/prototype/workspace/topics/{topic_id}/schedule",
            json={"enabled": True, "cadence": "weekly", "time_zone": "UTC", "local_time": "09:00", "network_policy": "scheduled_official_source_only"},
        )
        assert denied.status_code == 409

    first = client.put(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/schedule",
        json={"enabled": True, "cadence": "weekly", "time_zone": "Asia/Seoul", "local_time": "09:00", "network_policy": "scheduled_official_source_only"},
    ).json()
    second = client.put(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/schedule",
        json={"enabled": False, "cadence": "weekly", "time_zone": "Asia/Seoul", "local_time": "09:00", "network_policy": "disabled"},
    ).json()
    assert first["schedule_id"] == second["schedule_id"]
    assert (first["schedule_version"], second["schedule_version"]) == (1, 2)
    topic = client.get(f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}").json()
    assert [row["schedule_version"] for row in topic["schedule_history"]] == [2, 1]
    assert topic["living_schedule"]["enabled"] is False


def test_monthly_schedule_keeps_its_anchor_day_after_a_short_month() -> None:
    february = living.compute_next_due_at(
        "monthly",
        "UTC",
        "09:00",
        after="2026-01-31T09:00:00Z",
        anchor_local_date="2026-01-31",
    )
    march = living.compute_next_due_at(
        "monthly",
        "UTC",
        "09:00",
        after=february,
        anchor_local_date="2026-01-31",
    )
    assert february == "2026-02-28T09:00:00Z"
    assert march == "2026-03-31T09:00:00Z"


def test_due_dispatch_is_idempotent_non_overlapping_and_no_change_is_truthful(living_client) -> None:
    client, _data_root, _env = living_client
    _acquire_real(client)
    active_before = client.get(f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}").json()["active_snapshot_id"]
    schedule = _enable_weekly()
    due = schedule["next_due_at"]
    calls = 0

    def probe() -> dict:
        nonlocal calls
        calls += 1
        return _current_observation(client)

    first = living.execute_due_schedule(workspace.REAL_TOPIC_ID, now=due, source_probe=probe)
    second = living.execute_due_schedule(workspace.REAL_TOPIC_ID, now=due, source_probe=probe)
    assert first["job"]["status"] == "COMPLETED"
    assert first["refresh"]["status"] == "NO_MATERIAL_CHANGE"
    assert first["briefing"]["status"] == "NO_MATERIAL_CHANGE"
    assert second["duplicate"] is True
    assert second["job"]["job_id"] == first["job"]["job_id"]
    assert second["refresh"]["refresh_id"] == first["refresh"]["refresh_id"]
    assert second["briefing"]["briefing_id"] == first["briefing"]["briefing_id"]
    assert calls == 1
    after = client.get(f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}").json()
    assert after["active_snapshot_id"] == active_before
    assert first["refresh"]["changeset_id"]
    assert first["refresh"]["briefing_id"] == first["briefing"]["briefing_id"]
    assert first["refresh"]["retries"] == 0
    assert first["refresh"]["non_allowlisted_requests"] == 0
    assert first["refresh"]["no_material_change"] is True
    assert len(after["runs"]) == 1
    assert len(after["reports"]) == 1


def test_refresh_and_briefing_keep_the_claimed_schedule_version(living_client) -> None:
    client, _data_root, _env = living_client
    _acquire_real(client)
    schedule = _enable_weekly()

    def probe() -> dict:
        living.save_schedule(
            workspace.REAL_TOPIC_ID,
            {
                "enabled": True,
                "cadence": "daily",
                "time_zone": "UTC",
                "local_time": "10:00",
                "network_policy": "scheduled_official_source_only",
                "briefing_policy": "no_change_and_material_changes",
            },
            now="2026-01-05T00:01:00Z",
        )
        return _current_observation(client)

    result = living.execute_due_schedule(
        workspace.REAL_TOPIC_ID,
        now=schedule["next_due_at"],
        source_probe=probe,
    )
    assert result["refresh"]["schedule_version"] == 1
    assert result["briefing"]["schedule_version"] == 1
    assert client.get(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}"
    ).json()["living_schedule"]["schedule_version"] == 2


def test_official_refresh_probe_preserves_prevalidated_rights(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XRML_PROTOTYPE_MODE", "1")
    monkeypatch.setenv("XRML_PROTOTYPE_REAL_CORPUS_NETWORK", "1")
    monkeypatch.setenv("XRML_DATA_DIR", str((tmp_path / "rights-refresh").resolve()))
    observation = real.check_for_corpus_updates(
        client=_fixture_network(candidates=_established_real_candidates())
    )
    assert observation["articles"]
    assert all(article["rights_accepted"] is True for article in observation["articles"])


def test_overlap_prevention_catch_up_coalescing_and_expired_lease_recovery(living_client) -> None:
    client, _data_root, _env = living_client
    _acquire_real(client)
    schedule = _enable_weekly()
    due = datetime.fromisoformat(schedule["next_due_at"].replace("Z", "+00:00"))
    claimed = living.execute_due_schedule(
        workspace.REAL_TOPIC_ID,
        now=due + timedelta(days=28),
        claim_only=True,
    )
    assert claimed["job"]["execution_mode"] == "catch_up"
    assert claimed["job"]["missed_intervals"] >= 3
    assert len(workspace._load_state()["schedule_jobs"]) == 1

    state = workspace._load_state()
    runtime = next(row for row in state["schedule_runtime"] if row["schedule_id"] == schedule["schedule_id"])

    def keep_first_lease_active(current: dict) -> tuple[dict, None]:
        job = current["schedule_jobs"][0]
        job["lease_expires_at"] = (
            datetime.fromisoformat(runtime["next_due_at"].replace("Z", "+00:00")) + timedelta(minutes=1)
        ).isoformat().replace("+00:00", "Z")
        job["job_digest"] = living._record_digest(job, "job_digest")
        return current, None

    workspace._mutate_state(keep_first_lease_active)
    with pytest.raises(workspace.PrototypeWorkspaceError, match="living_topic_refresh_overlap"):
        living.execute_due_schedule(
            workspace.REAL_TOPIC_ID,
            now=runtime["next_due_at"],
            source_probe=lambda: _current_observation(client),
        )

    recovery_now = due + timedelta(days=28, minutes=6)

    def expire(current: dict) -> tuple[dict, None]:
        job = current["schedule_jobs"][0]
        job["lease_expires_at"] = (recovery_now - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        job["job_digest"] = living._record_digest(job, "job_digest")
        return current, None

    workspace._mutate_state(expire)
    calls = 0

    def probe() -> dict:
        nonlocal calls
        calls += 1
        return _current_observation(client)

    recovered = living.execute_due_schedule(
        workspace.REAL_TOPIC_ID,
        now=recovery_now,
        source_probe=probe,
    )
    assert recovered["job"]["recovered"] is True
    assert recovered["job"]["attempt_number"] == 1
    assert recovered["job"]["status"] == "COMPLETED"
    assert calls == 1
    final = workspace._load_state()
    assert len(final["schedule_jobs"]) == 1
    assert len(final["refresh_records"]) == 1
    assert len(final["briefings"]) == 1


def _snapshot(topic_id: str, snapshot_id: str, item: dict) -> dict:
    return {
        "topic_id": topic_id,
        "snapshot_id": snapshot_id,
        "source_policy": "pmc_open_access",
        "manifest_digest": living._digest({"snapshot": snapshot_id}),
        "included_count": 1,
        "items": [item],
    }


def _paper(**overrides) -> dict:
    base = {
        "corpus_item_id": "PMC1",
        "pmcid": "PMC1",
        "work_identity_digest": "work-1",
        "title": "Stable title",
        "publication_date": "2024-01-01",
        "license_type": "CC BY",
        "license_status": "accepted",
        "source_content_digest": "body-v1",
        "article_status": "current",
        "curation_state": "included",
        "review_id": "review-1",
        "evidence_ids": ["evidence-1"],
        "claim_ids": ["claim-1"],
        "report_version_ids": ["report-1"],
        "source_paragraph_ids": ["paragraph-1"],
    }
    return {**base, **overrides}


def test_delta_rules_are_deterministic_and_exactly_provenance_scoped() -> None:
    before = _snapshot("topic-a", "snapshot-a", _paper())
    metadata_after = _snapshot("topic-a", "snapshot-b", _paper(title="Stable title."))
    first = living.compute_delta_events(
        topic_id="topic-a", source_ref="refresh-stable", before_snapshot=before,
        after_snapshot=metadata_after, detected_at="2026-01-01T00:00:00Z",
    )
    second = living.compute_delta_events(
        topic_id="topic-a", source_ref="refresh-stable", before_snapshot=before,
        after_snapshot=metadata_after, detected_at="2026-02-01T00:00:00Z",
    )
    assert [(row["event_id"], row["event_digest"]) for row in first] == [
        (row["event_id"], row["event_digest"]) for row in second
    ]
    assert first[0]["event_type"] == "metadata_changed"
    state = {"snapshots": [before, metadata_after]}
    impacts = living.build_impacts(state, first)
    assert [row["impact_state"] for row in impacts] == ["unaffected"]
    assert impacts[0]["review_ids"] == ["review-1"]

    changed = _snapshot(
        "topic-a",
        "snapshot-c",
        _paper(source_content_digest="body-v2", license_status="rejected", article_status="retracted"),
    )
    critical = living.compute_delta_events(
        topic_id="topic-a", source_ref="refresh-critical", before_snapshot=before, after_snapshot=changed
    )
    assert {row["event_type"] for row in critical} == {
        "full_text_changed", "license_changed", "article_status_changed",
        "review_became_stale", "evidence_became_stale",
    }
    assert all(row["materiality"] == "trust_boundary" and row["severity"] == "critical" for row in critical)
    critical_impacts = living.build_impacts({"snapshots": [before, changed]}, critical)
    assert {value for row in critical_impacts for value in row["claim_ids"]} == {"claim-1"}
    assert {value for row in critical_impacts for value in row["evidence_ids"]} == {"evidence-1"}
    assert any(row["impact_state"] == "claim_invalidated_source_untrusted" for row in critical_impacts)

    with pytest.raises(workspace.PrototypeWorkspaceError, match="living_cross_topic_delta"):
        living.compute_delta_events(
            topic_id="topic-a",
            source_ref="bad",
            before_snapshot=before,
            after_snapshot={**changed, "topic_id": "topic-b"},
        )
    with pytest.raises(workspace.PrototypeWorkspaceError, match="living_cross_policy_delta"):
        living.compute_delta_events(
            topic_id="topic-a",
            source_ref="bad-policy",
            before_snapshot=before,
            after_snapshot={**changed, "source_policy": "local_only_no_external_source"},
        )

    corrected = _snapshot("topic-a", "snapshot-d", _paper(article_status="corrected"))
    correction_events = living.compute_delta_events(
        topic_id="topic-a", source_ref="refresh-correction", before_snapshot=before, after_snapshot=corrected
    )
    correction = next(row for row in correction_events if row["event_type"] == "article_status_changed")
    assert (correction["event_subtype"], correction["materiality"], correction["severity"]) == (
        "correction_detected", "material", "attention"
    )

    reduced = {**metadata_after, "included_count": 0}
    coverage_events = living.compute_delta_events(
        topic_id="topic-a", source_ref="curation-coverage", before_snapshot=before, after_snapshot=reduced
    )
    assert any(row["event_type"] == "coverage_decreased" for row in coverage_events)


def test_impact_boundaries_reject_cross_topic_and_cross_snapshot_links() -> None:
    before = _snapshot("topic-a", "snapshot-a", _paper())
    after = _snapshot("topic-a", "snapshot-b", _paper(title="Changed"))
    event = living.compute_delta_events(
        topic_id="topic-a", source_ref="refresh-impact", before_snapshot=before, after_snapshot=after
    )[0]
    with pytest.raises(workspace.PrototypeWorkspaceError, match="living_cross_topic_impact"):
        living.build_impacts({"snapshots": [{**before, "topic_id": "topic-b"}, after]}, [event])
    unrelated = _snapshot("topic-a", "snapshot-a", _paper(corpus_item_id="PMC-other", pmcid="PMC-other"))
    with pytest.raises(workspace.PrototypeWorkspaceError, match="living_cross_snapshot_evidence_mismatch"):
        living.build_impacts({"snapshots": [unrelated, after]}, [event])


def test_informational_repetition_is_suppressed_but_critical_repetition_is_not() -> None:
    info_one = living._event_core(
        topic_id="topic-a", source_ref="refresh-1", before_snapshot_id="snapshot-a",
        after_snapshot_id=None, work_identity=None, event_type="no_material_change",
        event_subtype=None, materiality="non_material", severity="info",
        before_digest="same", after_digest="same", reason_code="no_material_source_delta",
    )
    info_two = living._event_core(
        topic_id="topic-a", source_ref="refresh-2", before_snapshot_id="snapshot-a",
        after_snapshot_id=None, work_identity=None, event_type="no_material_change",
        event_subtype=None, materiality="non_material", severity="info",
        before_digest="same", after_digest="same", reason_code="no_material_source_delta",
    )
    critical_one = living._event_core(
        topic_id="topic-a", source_ref="refresh-3", before_snapshot_id="snapshot-a",
        after_snapshot_id=None, work_identity="PMC1", event_type="license_changed",
        event_subtype="license_downgraded", materiality="trust_boundary", severity="critical",
        before_digest="accepted", after_digest="rejected", reason_code="source_rights_changed",
    )
    critical_two = living._event_core(
        topic_id="topic-a", source_ref="refresh-4", before_snapshot_id="snapshot-a",
        after_snapshot_id=None, work_identity="PMC1", event_type="license_changed",
        event_subtype="license_downgraded", materiality="trust_boundary", severity="critical",
        before_digest="accepted", after_digest="rejected", reason_code="source_rights_changed",
    )
    state = {
        "schedules": [], "schedule_runtime": [], "schedule_jobs": [],
        "change_events": [info_one, info_two, critical_one, critical_two],
        "impact_records": [], "attention_events": [],
        "briefings": [
            {"briefing_id": "briefing-1", "topic_id": "topic-a", "generated_at": "2026-01-01T00:00:00Z", "change_event_ids": [info_one["event_id"], critical_one["event_id"]]},
            {"briefing_id": "briefing-2", "topic_id": "topic-a", "generated_at": "2026-01-02T00:00:00Z", "change_event_ids": [info_two["event_id"], critical_two["event_id"]]},
        ],
    }
    decorated = living.decorate_topic(state, {"topic_id": "topic-a"})
    latest = decorated["briefings"][0]["attention_items"]
    assert next(row for row in latest if row["severity"] == "info")["state"] == "suppressed_for_repetition"
    assert next(row for row in latest if row["severity"] == "critical")["state"] == "unread"
    assert next(row for row in latest if row["severity"] == "critical")["alert_class"] == "critical"


def test_before_due_and_archived_topic_execute_no_probe(living_client) -> None:
    client, _data_root, _env = living_client
    _acquire_real(client)
    schedule = _enable_weekly()
    due = datetime.fromisoformat(schedule["next_due_at"].replace("Z", "+00:00"))
    calls = 0

    def probe() -> dict:
        nonlocal calls
        calls += 1
        return _current_observation(client)

    with pytest.raises(workspace.PrototypeWorkspaceError, match="living_schedule_not_due"):
        living.execute_due_schedule(workspace.REAL_TOPIC_ID, now=due - timedelta(seconds=1), source_probe=probe)
    assert calls == 0
    assert workspace._load_state()["schedule_jobs"] == []
    archived = client.patch(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/settings", json={"archived": True}
    )
    assert archived.status_code == 200
    cycle = living.run_scheduler_cycle(
        now=due, probe_by_topic={workspace.REAL_TOPIC_ID: probe}
    )
    assert cycle["due_topic_count"] == 0
    assert calls == 0
    assert workspace._load_state()["schedule_jobs"] == []


def test_added_work_is_preview_only_and_does_not_invalidate_historical_report(living_client) -> None:
    client, _data_root, _env = living_client
    _acquire_real(client)
    topic_before = client.get(f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}").json()
    schedule = _enable_weekly()

    def probe() -> dict:
        observation = _current_observation(client)
        observation["articles"].append(
            {
                "pmcid": "PMC99999999", "pmid": "99999999", "doi": "10.0000/new",
                "title": "New eligible open article", "publication_date": "2026-01-01",
                "license_type": "CC BY", "rights_accepted": True,
                "article_identity_digest": "new-work-digest", "article_status": "current",
            }
        )
        observation["candidate_count"] += 1
        observation["license_eligible_count"] += 1
        return observation

    result = living.execute_due_schedule(
        workspace.REAL_TOPIC_ID, now=schedule["next_due_at"], source_probe=probe
    )
    assert result["refresh"]["status"] == "PREVIEW_READY"
    event = next(row for row in workspace._load_state()["change_events"] if row["event_type"] == "paper_added")
    assert (event["materiality"], event["severity"]) == ("material", "attention")
    impact = next(row for row in workspace._load_state()["impact_records"] if row["change_event_id"] == event["event_id"])
    assert impact["report_version_ids"] == []
    topic_after = client.get(f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}").json()
    assert topic_after["active_snapshot_id"] == topic_before["active_snapshot_id"]
    assert [row["report_version_id"] for row in topic_after["reports"]] == [row["report_version_id"] for row in topic_before["reports"]]
    assert len(topic_after["snapshots"]) == len(topic_before["snapshots"])
    assert len(topic_after["runs"]) == len(topic_before["runs"])


def test_one_active_refresh_per_topic_blocks_nested_probe(living_client) -> None:
    client, _data_root, _env = living_client
    _acquire_real(client)
    nested_probe_calls = 0

    def outer_probe() -> dict:
        nonlocal nested_probe_calls
        with pytest.raises(workspace.PrototypeWorkspaceError, match="living_topic_refresh_overlap"):
            workspace.check_for_corpus_updates(
                workspace.REAL_TOPIC_ID,
                confirmed=True,
                probe=lambda: (_ for _ in ()).throw(AssertionError("nested transport called")),
            )
        nested_probe_calls += 1
        return _current_observation(client)

    result = workspace.check_for_corpus_updates(
        workspace.REAL_TOPIC_ID, confirmed=True, probe=outer_probe
    )
    assert result["refresh"]["status"] == "NO_MATERIAL_CHANGE"
    assert nested_probe_calls == 1
    assert len(workspace._load_state()["refresh_records"]) == 1


def test_embedded_worker_delegates_to_living_scheduler_only_when_explicitly_enabled(
    living_client, monkeypatch
) -> None:
    _client, _data_root, _env = living_client
    calls: list[str] = []
    monkeypatch.setenv("XRML_PROTOTYPE_MODE", "1")
    monkeypatch.setenv("XRML_PROTOTYPE_LIVING_SCHEDULER_ENABLED", "1")
    monkeypatch.setattr(
        topic_schedules,
        "run_scheduler_cycle",
        lambda **_kwargs: {"executed_schedule_ids": [], "errors": []},
    )
    monkeypatch.setattr(
        living,
        "run_scheduler_cycle",
        lambda **_kwargs: calls.append("living") or {
            "executed": [], "errors": [], "due_topic_count": 0, "provider_calls": 0
        },
    )
    monkeypatch.setattr(topic_schedules, "record_scheduler_heartbeat", lambda **_kwargs: None)
    worker = TopicScheduleWorker(enabled=True, tick_seconds=1)
    result = worker.run_cycle_now()
    assert calls == ["living"]
    assert result["living_topics"]["provider_calls"] == 0


def test_curation_briefing_links_provenance_compares_exports_and_preserves_events(living_client) -> None:
    client, _data_root, _env = living_client
    _acquire_real(client)
    before, changeset = _hold_preview(client)
    after = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/changesets/{changeset['changeset_id']}/apply",
        json={"confirmed": True},
    ).json()
    first = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/briefings/curation-delta",
        json={"before_snapshot_id": before["snapshot_id"], "after_snapshot_id": after["snapshot_id"]},
    ).json()
    repeated = living.create_curation_delta_briefing(
        workspace.REAL_TOPIC_ID, before["snapshot_id"], after["snapshot_id"]
    )
    assert repeated["briefing_id"] == first["briefing_id"]
    assert first["briefing_type"] == "curation_delta"
    assert first["coverage_after"] == first["coverage_before"] - 1
    assert first["change_event_ids"]
    assert first["report_version_ids"]
    assert first["evidence_ids"]
    event_id = first["change_event_ids"][0]
    before_state = workspace._load_state()
    acknowledged = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/briefings/{first['briefing_id']}/attention",
        json={"change_event_id": event_id, "action": "acknowledge"},
    )
    assert acknowledged.status_code == 200
    after_state = workspace._load_state()
    assert len(after_state["change_events"]) == len(before_state["change_events"])
    exported = client.get(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/briefings/{first['briefing_id']}/export"
    ).json()
    assert "<article" not in exported["content"]
    assert str(_data_root) not in exported["content"]


def test_source_failure_is_not_no_change_and_restart_is_zero_network(living_client, app_factory, monkeypatch) -> None:
    client, data_root, env = living_client
    _acquire_real(client)
    active_before = client.get(f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}").json()["active_snapshot_id"]
    schedule = _enable_weekly()

    def unavailable():
        raise real.RealCorpusError("retrieval_transport_error")

    result = living.execute_due_schedule(
        workspace.REAL_TOPIC_ID,
        now=schedule["next_due_at"],
        source_probe=unavailable,
    )
    assert result["refresh"]["status"] == "FAILED_SOURCE_UNAVAILABLE"
    assert result["refresh"]["no_material_change"] is False
    assert result["briefing"]["status"] == "SOURCE_UNAVAILABLE"
    assert result["briefing"]["briefing_type"] == "source_failure"
    assert result["refresh"]["retries"] == 0
    assert client.get(f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}").json()["active_snapshot_id"] == active_before

    client.close()

    def forbidden_network():
        raise AssertionError("zero-network reopen attempted a network client")

    monkeypatch.setattr(real, "PmcNetworkClient", forbidden_network)
    reopened_env = {**env, "XRML_PROTOTYPE_REAL_CORPUS_NETWORK": "0"}
    with TestClient(app_factory(**reopened_env)) as reopened:
        topic = reopened.get(f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}").json()
        assert topic["living_schedule"]["last_status"] == "source_unavailable"
        assert topic["refreshes"][0]["refresh_id"] == result["refresh"]["refresh_id"]
        assert topic["briefings"][0]["briefing_id"] == result["briefing"]["briefing_id"]
        assert topic["active_snapshot_id"] == active_before
    assert (data_root / "prototype-workspace" / "workspace.json").exists()


def test_critical_alert_cannot_be_dismissed_but_can_be_acknowledged(living_client) -> None:
    client, _data_root, _env = living_client
    _acquire_real(client)
    schedule = _enable_weekly()

    def downgraded_license() -> dict:
        observation = _current_observation(client)
        observation["articles"][0]["rights_accepted"] = False
        observation["articles"][0]["license_type"] = "unknown"
        return observation

    result = living.execute_due_schedule(
        workspace.REAL_TOPIC_ID,
        now=schedule["next_due_at"],
        source_probe=downgraded_license,
    )
    event_id = next(
        row["event_id"]
        for row in workspace._load_state()["change_events"]
        if row["event_type"] == "license_changed"
    )
    before_events = deepcopy(workspace._load_state()["change_events"])
    dismissed = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/briefings/{result['briefing']['briefing_id']}/attention",
        json={"change_event_id": event_id, "action": "dismiss"},
    )
    assert dismissed.status_code == 409
    acknowledged = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/briefings/{result['briefing']['briefing_id']}/attention",
        json={"change_event_id": event_id, "action": "acknowledge"},
    )
    assert acknowledged.status_code == 200
    assert workspace._load_state()["change_events"] == before_events


def test_schedule_disable_prevents_new_dispatch(living_client) -> None:
    client, _data_root, _env = living_client
    _acquire_real(client)
    _enable_weekly()
    living.save_schedule(
        workspace.REAL_TOPIC_ID,
        {
            "enabled": False,
            "cadence": "weekly",
            "time_zone": "Asia/Seoul",
            "local_time": "09:00",
            "network_policy": "disabled",
            "briefing_policy": "no_change_and_material_changes",
        },
    )
    with pytest.raises(workspace.PrototypeWorkspaceError, match="living_schedule_not_dispatchable"):
        living.execute_due_schedule(
            workspace.REAL_TOPIC_ID,
            now="2030-01-01T00:00:00Z",
            source_probe=lambda: _current_observation(client),
        )
    assert workspace._load_state()["schedule_jobs"] == []
