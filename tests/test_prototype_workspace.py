from __future__ import annotations

import json
import time
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.repositories.runs import load_run_record
from app.repositories.files import read_jsonl
from app.repositories.runs import build_run_paths
from app.services import prototype_real_corpus as real, report_versions
from app.services import prototype_workspace as workspace
from tests.test_prototype_real_corpus import _candidate, _fixture_network


def _established_real_candidates() -> list[dict]:
    pmcids = ("PMC7465609", "PMC6779025", "PMC11910998", "PMC11226757")
    candidates: list[dict] = []
    for index, pmcid in enumerate(pmcids, start=1):
        candidate = _candidate(index)
        candidate["uid"] = pmcid.removeprefix("PMC")
        candidate["pmcid"] = pmcid
        candidate["publication_date"] = "2024-01-02"
        candidates.append(candidate)
    return candidates


@pytest.fixture
def workspace_client(tmp_path: Path, app_factory, monkeypatch):
    data_root = (tmp_path / "prototype-v0.4-data").resolve()
    fixture_clients = [
        _fixture_network(candidates=_established_real_candidates())
        for _ in range(8)
    ]
    monkeypatch.setattr(real, "PmcNetworkClient", lambda: fixture_clients.pop(0))
    with TestClient(
        app_factory(
            AUTH_MODE="disabled",
            XRML_PROTOTYPE_MODE="1",
            XRML_PROTOTYPE_REAL_CORPUS_NETWORK="1",
            XRML_DATA_DIR=str(data_root),
            XRML_RUN_EXECUTOR_POLL_SECONDS="0.05",
        )
    ) as client:
        yield client, data_root


def _acquire_real(client: TestClient) -> dict:
    response = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/initial-acquisition",
        json={"confirmed": True, "network_enabled": True},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _hold_preview(client: TestClient) -> tuple[dict, dict]:
    topic = client.get(f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}").json()
    snapshot = topic["active_snapshot"]
    item = snapshot["items"][-1]
    response = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/curation/preview",
        json={
            "source_snapshot_id": snapshot["snapshot_id"],
            "corpus_item_id": item["corpus_item_id"],
            "requested_state": "hold",
            "reason_code": "quality_or_relevance_hold",
            "note": "Bounded local relevance review.",
        },
    )
    assert response.status_code == 200, response.text
    return snapshot, response.json()


def _wait_for_run(run_id: str) -> dict:
    for _ in range(200):
        run = load_run_record(run_id)
        if run.get("status") in {"failed", "cancelled"}:
            return run
        if run.get("status") == "completed" and report_versions.list_report_versions(run_id)["items"]:
            return load_run_record(run_id)
        time.sleep(0.05)
    raise AssertionError(f"Run {run_id} did not reach a terminal status")


def test_workspace_starts_with_three_typed_topics_and_truthful_totals(workspace_client) -> None:
    client, _data_root = workspace_client
    response = client.get("/prototype/workspace")
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["schema_version"] == workspace.WORKSPACE_SCHEMA
    assert payload["provider_calls"] == 0
    assert len(payload["topics"]) == 3
    assert {topic["topic_kind"] for topic in payload["topics"]} == {
        "synthetic_demo", "real_open_access", "local_empty"
    }
    assert {topic["source_policy"] for topic in payload["topics"]} == {
        "synthetic_fixture", "pmc_open_access", "local_only_no_external_source"
    }
    assert payload["totals"]["active_topics"] == 3
    assert payload["totals"]["archived_topics"] == 0
    assert payload["totals"]["synthetic_papers"] == 4
    assert payload["totals"]["real_full_text_papers"] == 0
    assert payload["totals"]["topics_needing_attention"] == 1
    real_topic = next(topic for topic in payload["topics"] if topic["topic_id"] == workspace.REAL_TOPIC_ID)
    assert real_topic["active_snapshot"] is None
    assert real_topic["needs_attention"] is True


def test_local_topic_creation_settings_versions_archive_and_reopen_are_non_destructive(workspace_client) -> None:
    client, _data_root = workspace_client
    created_response = client.post(
        "/prototype/workspace/topics",
        json={
            "display_name": "Immersive learning transfer replication",
            "description": "Local-only topic for a future owner-curated corpus.",
            "research_question": "How does immersive learning transfer into later training performance?",
            "topic_kind": "local_empty",
            "source_policy": "local_only_no_external_source",
            "language": "en",
            "publication_date_cutoff": None,
            "license_policy": "local_only",
            "refresh_mode": "disabled",
        },
    )
    assert created_response.status_code == 201, created_response.text
    created = created_response.json()
    topic_id = created["topic_id"]
    assert created["active_snapshot"] is None
    assert created["runs"] == []
    assert created["reports"] == []
    assert created["settings_version"] == 1

    updated_response = client.patch(
        f"/prototype/workspace/topics/{topic_id}/settings",
        json={"description": "Edited local-only research scope."},
    )
    assert updated_response.status_code == 200, updated_response.text
    updated = updated_response.json()
    assert updated["settings_version"] == 2
    assert len(updated["settings_history"]) == 2
    assert updated["settings_history"][0]["settings"]["description"] == "Edited local-only research scope."

    archived = client.patch(
        f"/prototype/workspace/topics/{topic_id}/settings",
        json={"archived": True},
    ).json()
    assert archived["archived"] is True
    assert archived["settings_version"] == 3
    reopened = client.get(f"/prototype/workspace/topics/{topic_id}")
    assert reopened.status_code == 200
    assert reopened.json()["topic_id"] == topic_id
    assert reopened.json()["active_snapshot"] is None
    unarchived = client.patch(
        f"/prototype/workspace/topics/{topic_id}/settings",
        json={"archived": False},
    ).json()
    assert unarchived["archived"] is False
    assert unarchived["settings_version"] == 4


def test_topic_identity_validation_rejects_collisions_incompatible_policy_and_markup(workspace_client) -> None:
    client, _data_root = workspace_client
    base = {
        "display_name": "Local transfer validation topic",
        "description": "",
        "research_question": "",
        "topic_kind": "local_empty",
        "source_policy": "local_only_no_external_source",
        "language": "en",
        "publication_date_cutoff": None,
        "license_policy": "local_only",
        "refresh_mode": "disabled",
    }
    assert client.post("/prototype/workspace/topics", json=base).status_code == 201
    collision = client.post("/prototype/workspace/topics", json=base)
    assert collision.status_code == 409
    assert collision.json()["detail"]["code"] == "topic_slug_conflict"

    incompatible = client.post(
        "/prototype/workspace/topics",
        json={**base, "display_name": "Different topic", "source_policy": "pmc_open_access"},
    )
    assert incompatible.status_code == 400
    assert incompatible.json()["detail"]["code"] == "topic_source_policy_incompatible"
    markup = client.post(
        "/prototype/workspace/topics",
        json={**base, "display_name": "<script>alert(1)</script>"},
    )
    assert markup.status_code == 400
    assert markup.json()["detail"]["code"] == "topic_text_rejected"


def test_settings_change_does_not_reinterpret_existing_snapshot(workspace_client) -> None:
    client, _data_root = workspace_client
    before = client.get(f"/prototype/workspace/topics/{workspace.SYNTHETIC_TOPIC_ID}").json()
    original = deepcopy(before["active_snapshot"])
    response = client.patch(
        f"/prototype/workspace/topics/{workspace.SYNTHETIC_TOPIC_ID}/settings",
        json={"research_question": "Which provenance constraints matter most for local XR research?"},
    )
    assert response.status_code == 200, response.text
    after = response.json()
    preserved = next(item for item in after["snapshots"] if item["snapshot_id"] == original["snapshot_id"])
    assert after["settings_version"] == 2
    assert preserved["settings_version"] == 1
    assert preserved["settings_digest"] == original["settings_digest"]
    assert preserved["manifest_digest"] == original["manifest_digest"]
    assert preserved["items"] == original["items"]


def test_real_acquisition_registers_exact_four_paper_snapshot_and_report_lineage(workspace_client) -> None:
    client, data_root = workspace_client
    acquired = _acquire_real(client)
    assert acquired["provider_calls"] == 0
    assert acquired["run"]["status"] == "completed"
    assert acquired["run"]["real_corpus"]["network_request_count"] <= real.MAX_HTTP_REQUESTS

    topic = client.get(f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}").json()
    snapshot = topic["active_snapshot"]
    assert snapshot["active"] is True
    assert snapshot["snapshot_version"] == 1
    assert snapshot["included_count"] == 4
    assert snapshot["reviewed_count"] == 4
    assert snapshot["evidence_count"] == 12
    assert {item["pmcid"] for item in snapshot["items"]} == {
        "PMC7465609",
        "PMC6779025",
        "PMC11910998",
        "PMC11226757",
    }
    assert all(item["license_status"] == "accepted" for item in snapshot["items"])
    assert all(item["full_text_status"] == "available" for item in snapshot["items"])
    assert len(topic["runs"]) == 1
    assert len(topic["reports"]) == 1
    run = load_run_record(acquired["run"]["run_id"])
    assert run["workspace"]["topic_id"] == workspace.REAL_TOPIC_ID
    assert run["workspace"]["snapshot_id"] == snapshot["snapshot_id"]
    assert run["workspace"]["source_manifest_digest"] == snapshot["manifest_digest"]
    assert str(data_root).lower() not in json.dumps(topic).lower()


def test_curation_preview_is_non_mutating_and_apply_creates_immutable_child_snapshot(workspace_client) -> None:
    client, _data_root = workspace_client
    _acquire_real(client)
    source, changeset = _hold_preview(client)
    during_preview = client.get(f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}").json()
    assert during_preview["active_snapshot_id"] == source["snapshot_id"]
    assert during_preview["active_snapshot"]["hold_count"] == 0
    assert changeset["applied"] is False
    assert changeset["held_item_ids"]
    assert changeset["projected_review_count"] == 3
    assert changeset["projected_evidence_count"] == 9
    assert changeset["projected_report_impact"] == "none_historical_reports_immutable"
    assert len(during_preview["curation_events"]) == 1
    event_before = deepcopy(during_preview["curation_events"][0])

    applied_response = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/changesets/{changeset['changeset_id']}/apply",
        json={"confirmed": True},
    )
    assert applied_response.status_code == 200, applied_response.text
    applied = applied_response.json()
    assert applied["snapshot_version"] == 2
    assert applied["parent_snapshot_id"] == source["snapshot_id"]
    assert applied["included_count"] == 3
    assert applied["hold_count"] == 1
    assert applied["reviewed_count"] == 3
    assert applied["evidence_count"] == 9

    after = client.get(f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}").json()
    old = next(item for item in after["snapshots"] if item["snapshot_id"] == source["snapshot_id"])
    assert old["active"] is False
    assert old["superseded_by_snapshot_id"] == applied["snapshot_id"]
    assert old["included_count"] == 4
    assert old["hold_count"] == 0
    assert old["manifest_digest"] == source["manifest_digest"]
    assert after["curation_events"][0] == event_before
    assert after["reports"][0]["snapshot_id"] == source["snapshot_id"]
    duplicate = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/changesets/{changeset['changeset_id']}/apply",
        json={"confirmed": True},
    )
    assert duplicate.status_code == 409

    compared = client.get(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/snapshots/compare",
        params={"from_snapshot_id": source["snapshot_id"], "to_snapshot_id": applied["snapshot_id"]},
    ).json()
    assert compared["held_papers"] == changeset["held_item_ids"]
    assert compared["review_count_delta"] == -1
    assert compared["evidence_count_delta"] == -3


def test_curated_real_run_uses_only_included_items_and_new_snapshot_evidence(workspace_client) -> None:
    client, data_root = workspace_client
    acquired = _acquire_real(client)
    source, changeset = _hold_preview(client)
    applied = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/changesets/{changeset['changeset_id']}/apply",
        json={"confirmed": True},
    ).json()
    old_record = client.get(f"/prototype/runs/{acquired['run']['run_id']}/real-corpus").json()
    old_digest = old_record["record_digest"]

    run_response = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/runs",
        json={"snapshot_id": applied["snapshot_id"], "confirmed": True},
    )
    assert run_response.status_code == 200, run_response.text
    run = run_response.json()
    assert run["status"] == "completed"
    assert run["snapshot_id"] == applied["snapshot_id"]
    assert run["counts"]["papers"] == 3
    assert run["counts"]["reviewed"] == 3
    assert run["counts"]["evidence_spans"] == 9
    assert run["report_version_id"]

    record_response = client.get(f"/prototype/runs/{run['run_id']}/real-corpus")
    assert record_response.status_code == 200, record_response.text
    record = record_response.json()
    assert record["selected_article_count"] == 3
    assert len(record["claims"]) == 9
    assert len(record["evidence"]) == 9
    assert all(item["run_id"] == run["run_id"] for item in record["evidence"])
    assert all(item["paper_id"] in {article["pmcid"] for article in record["articles"]} for item in record["evidence"])
    assert record["report"]["cross_run_evidence_ids"] == 0
    assert record["provider_calls"] == 0
    assert client.get(f"/prototype/runs/{acquired['run']['run_id']}/real-corpus").json()["record_digest"] == old_digest

    preview = client.get(f"/prototype/runs/{run['run_id']}/report-versions/{run['report_version_id']}/preview")
    assert preview.status_code == 200, preview.text
    export = client.post(f"/prototype/runs/{run['run_id']}/report-versions/{run['report_version_id']}/export")
    assert export.status_code == 200, export.text
    artifact = export.json()["artifact"]
    assert artifact["topic_id"] == workspace.REAL_TOPIC_ID
    assert artifact["corpus_snapshot_id"] == applied["snapshot_id"]
    assert artifact["evidence_set_digest"] == record["report"]["evidence_set_digest"]
    assert str(data_root).lower() not in json.dumps(export.json()).lower()
    assert source["snapshot_id"] != applied["snapshot_id"]


def test_curated_synthetic_run_uses_only_included_snapshot_items_and_records_evidence_set(workspace_client) -> None:
    client, _data_root = workspace_client
    topic = client.get(f"/prototype/workspace/topics/{workspace.SYNTHETIC_TOPIC_ID}").json()
    source = topic["active_snapshot"]
    held_item = source["items"][-1]
    preview_response = client.post(
        f"/prototype/workspace/topics/{workspace.SYNTHETIC_TOPIC_ID}/curation/preview",
        json={
            "source_snapshot_id": source["snapshot_id"],
            "corpus_item_id": held_item["corpus_item_id"],
            "requested_state": "hold",
            "reason_code": "quality_or_relevance_hold",
            "note": "Regression proof for snapshot-scoped execution.",
        },
    )
    assert preview_response.status_code == 200, preview_response.text
    changeset = preview_response.json()
    applied_response = client.post(
        f"/prototype/workspace/topics/{workspace.SYNTHETIC_TOPIC_ID}/changesets/{changeset['changeset_id']}/apply",
        json={"confirmed": True},
    )
    assert applied_response.status_code == 200, applied_response.text
    applied = applied_response.json()

    run_response = client.post(
        f"/prototype/workspace/topics/{workspace.SYNTHETIC_TOPIC_ID}/runs",
        json={"snapshot_id": applied["snapshot_id"], "confirmed": True},
    )
    assert run_response.status_code == 200, run_response.text
    run_id = run_response.json()["run_id"]
    run = _wait_for_run(run_id)

    assert run["status"] == "completed", run.get("error")
    assert run["workspace"]["snapshot_id"] == applied["snapshot_id"]
    assert len(run["workspace"]["evidence_set_digest"]) == 64
    assert run["counts"]["triage_items"] == 3
    assert run["counts"]["reviewed_items"] == 3
    assert run["counts"]["evidence_spans"] == 6
    inbox = read_jsonl(build_run_paths(run_id).raw_dir / "inbox.jsonl")
    assert len(inbox) == 3
    assert held_item["source_identity"].removeprefix("synthetic:") not in {
        item["paper_id"] for item in inbox
    }
    report = Path(run["artifacts"]["report_markdown"]).read_text(encoding="utf-8")
    assert held_item["title"] not in report

    refreshed_topic = client.get(f"/prototype/workspace/topics/{workspace.SYNTHETIC_TOPIC_ID}").json()
    completed_run = next(item for item in refreshed_topic["runs"] if item["run_id"] == run_id)
    report_version = next(item for item in refreshed_topic["reports"] if item["run_id"] == run_id)
    assert completed_run["counts"]["triage_items"] == 3
    assert completed_run["counts"]["reviewed_items"] == 3
    assert completed_run["counts"]["evidence_spans"] == 6
    assert report_version["snapshot_id"] == applied["snapshot_id"]
    assert report_version["evidence_set_digest"] == run["workspace"]["evidence_set_digest"]


def test_cross_topic_and_stale_changesets_fail_without_changing_active_snapshot(workspace_client) -> None:
    client, _data_root = workspace_client
    _acquire_real(client)
    source, changeset = _hold_preview(client)
    cross_topic = client.post(
        f"/prototype/workspace/topics/{workspace.SYNTHETIC_TOPIC_ID}/curation/preview",
        json={
            "source_snapshot_id": source["snapshot_id"],
            "corpus_item_id": source["items"][0]["corpus_item_id"],
            "requested_state": "hold",
            "reason_code": "quality_or_relevance_hold",
            "note": None,
        },
    )
    assert cross_topic.status_code == 409
    assert cross_topic.json()["detail"]["code"] == "cross_topic_snapshot_reference"

    settings = client.patch(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/settings",
        json={"description": "Versioned after preview."},
    )
    assert settings.status_code == 200
    failed_apply = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/changesets/{changeset['changeset_id']}/apply",
        json={"confirmed": True},
    )
    assert failed_apply.status_code == 409
    assert failed_apply.json()["detail"]["code"] == "changeset_settings_stale"
    after = client.get(f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}").json()
    assert after["active_snapshot_id"] == source["snapshot_id"]
    assert len(after["snapshots"]) == 1


@pytest.mark.parametrize(
    ("license_status", "full_text_status", "expected_code"),
    [
        ("rejected", "available", "license_not_accepted_for_inclusion"),
        ("accepted", "missing", "full_text_required_for_inclusion"),
    ],
)
def test_rejected_license_or_missing_full_text_cannot_be_restored_to_trusted_snapshot(
    workspace_client,
    license_status: str,
    full_text_status: str,
    expected_code: str,
) -> None:
    client, _data_root = workspace_client
    _acquire_real(client)

    def install_blocked_snapshot(state):
        topic = workspace._topic(state, workspace.REAL_TOPIC_ID)
        source = workspace._snapshot(state, topic["active_snapshot_id"], topic_id=topic["topic_id"])
        items = deepcopy(source["items"])
        items[0]["curation_state"] = "excluded"
        items[0]["license_status"] = license_status
        items[0]["full_text_status"] = full_text_status
        blocked = workspace._new_snapshot(
            topic=topic,
            version=2,
            parent_snapshot_id=source["snapshot_id"],
            query_id=source["query_id"],
            query_digest=source["query_digest"],
            items=items,
            created_by_mode=workspace.LOCAL_ACTOR,
            refresh_id=None,
            source_run_id=source["source_run_id"],
            change_summary={"added": 0, "removed": 1, "held": 0, "restored": 0},
        )
        state["snapshots"].append(blocked)
        topic["active_snapshot_id"] = blocked["snapshot_id"]
        return state, blocked

    blocked = workspace._mutate_state(install_blocked_snapshot)
    response = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/curation/preview",
        json={
            "source_snapshot_id": blocked["snapshot_id"],
            "corpus_item_id": blocked["items"][0]["corpus_item_id"],
            "requested_state": "included",
            "reason_code": "manual_owner_decision",
            "note": None,
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == expected_code
    active = client.get(f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}").json()["active_snapshot"]
    assert active["snapshot_id"] == blocked["snapshot_id"]
    assert active["excluded_count"] == 1


def test_refresh_is_explicit_preview_only_and_no_change_creates_no_snapshot(workspace_client) -> None:
    client, _data_root = workspace_client
    _acquire_real(client)
    before = client.get(f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}").json()
    active_id = before["active_snapshot_id"]
    denied = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/refreshes",
        json={"confirmed": False},
    )
    assert denied.status_code == 400
    response = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/refreshes",
        json={"confirmed": True},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["refresh"]["status"] == "NO_MATERIAL_CHANGE"
    assert 0 < result["refresh"]["request_count"] <= real.MAX_HTTP_REQUESTS
    assert result["refresh"]["retries"] == 0
    assert result["refresh"]["non_allowlisted_requests"] == 0
    assert result["changeset"]["applied"] is False
    assert result["changeset"]["no_accepted_change"] is True
    after = client.get(f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}").json()
    assert after["active_snapshot_id"] == active_id
    assert len(after["snapshots"]) == len(before["snapshots"])
    apply = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/changesets/{result['changeset']['changeset_id']}/apply",
        json={"confirmed": True},
    )
    assert apply.status_code == 409
    assert apply.json()["detail"]["code"] == "changeset_no_accepted_change"


def test_official_source_failure_records_attempt_and_preserves_active_snapshot(workspace_client, monkeypatch) -> None:
    client, _data_root = workspace_client
    _acquire_real(client)
    before = client.get(f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}").json()

    class AttemptThenFail:
        request_count = 0
        total_downloaded_bytes = 0

        def fetch(self, *_args, **_kwargs):
            self.request_count += 1
            raise real.RealCorpusError("retrieval_transport_error")

    failing_client = AttemptThenFail()
    monkeypatch.setattr(real, "PmcNetworkClient", lambda: failing_client)

    response = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/refreshes",
        json={"confirmed": True},
    )
    assert response.status_code == 200, response.text
    refresh = response.json()
    assert refresh["status"] == "FAILED_SOURCE_UNAVAILABLE"
    assert refresh["failure_category"] == "retrieval_transport_error"
    assert refresh["request_count"] == 1
    assert refresh["retries"] == 0
    assert refresh["non_allowlisted_requests"] == 0
    after = client.get(f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}").json()
    assert after["active_snapshot_id"] == before["active_snapshot_id"]
    assert len(after["snapshots"]) == len(before["snapshots"])


def test_zero_network_reopen_preserves_topics_snapshots_events_runs_and_reports(
    tmp_path: Path,
    app_factory,
    monkeypatch,
) -> None:
    data_root = (tmp_path / "prototype-reopen-data").resolve()
    fixture_clients = [
        _fixture_network(candidates=_established_real_candidates())
        for _ in range(8)
    ]
    monkeypatch.setattr(real, "PmcNetworkClient", lambda: fixture_clients.pop(0))
    env = {
        "AUTH_MODE": "disabled",
        "XRML_PROTOTYPE_MODE": "1",
        "XRML_PROTOTYPE_REAL_CORPUS_NETWORK": "1",
        "XRML_DATA_DIR": str(data_root),
    }
    with TestClient(app_factory(**env)) as client:
        _acquire_real(client)
        source, changeset = _hold_preview(client)
        applied = client.post(
            f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/changesets/{changeset['changeset_id']}/apply",
            json={"confirmed": True},
        ).json()
        run = client.post(
            f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/runs",
            json={"snapshot_id": applied["snapshot_id"], "confirmed": True},
        ).json()
        before = client.get("/prototype/workspace").json()

    def forbidden_network():
        raise AssertionError("cached reopen attempted a network client")

    monkeypatch.setattr(real, "PmcNetworkClient", forbidden_network)
    with TestClient(
        app_factory(
            AUTH_MODE="disabled",
            XRML_PROTOTYPE_MODE="1",
            XRML_PROTOTYPE_REAL_CORPUS_NETWORK="0",
            XRML_DATA_DIR=str(data_root),
        )
    ) as reopened:
        after = reopened.get("/prototype/workspace")
        assert after.status_code == 200, after.text
        payload = after.json()
        assert {topic["topic_id"] for topic in payload["topics"]} == {
            topic["topic_id"] for topic in before["topics"]
        }
        real_topic = next(topic for topic in payload["topics"] if topic["topic_id"] == workspace.REAL_TOPIC_ID)
        assert real_topic["active_snapshot_id"] == applied["snapshot_id"]
        assert any(item["snapshot_id"] == source["snapshot_id"] for item in real_topic["snapshots"])
        assert real_topic["curation_events"]
        assert any(item["run_id"] == run["run_id"] for item in real_topic["runs"])
        assert real_topic["reports"]


def test_snapshot_manifest_is_deterministic_for_same_corpus_and_settings(workspace_client) -> None:
    client, _data_root = workspace_client
    topic = client.get(f"/prototype/workspace/topics/{workspace.SYNTHETIC_TOPIC_ID}").json()
    snapshot = topic["active_snapshot"]
    first = workspace._snapshot_manifest_digest(
        source_policy=snapshot["source_policy"],
        query_id=snapshot["query_id"],
        query_digest=snapshot["query_digest"],
        settings_version=snapshot["settings_version"],
        settings_digest=snapshot["settings_digest"],
        items=list(reversed(snapshot["items"])),
    )
    second = workspace._snapshot_manifest_digest(
        source_policy=snapshot["source_policy"],
        query_id=snapshot["query_id"],
        query_digest=snapshot["query_digest"],
        settings_version=snapshot["settings_version"],
        settings_digest=snapshot["settings_digest"],
        items=snapshot["items"],
    )
    assert first == second == snapshot["manifest_digest"]
