from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.services import prototype_real_corpus as real
from app.services import prototype_research_gaps as research
from app.services import prototype_workspace as workspace
from tests.test_prototype_workspace import (
    _acquire_real,
    _established_real_candidates,
    _hold_preview,
)
from tests.test_prototype_real_corpus import _fixture_network


@pytest.fixture
def research_workspace_client(tmp_path: Path, app_factory, monkeypatch):
    data_root = (tmp_path / "prototype-v0.6-data").resolve()
    fixture_clients = [_fixture_network(candidates=_established_real_candidates()) for _ in range(8)]
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


@pytest.fixture
def analyzed_real_topic(research_workspace_client):
    client, data_root = research_workspace_client
    acquired = _acquire_real(client)
    topic = client.get(f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}").json()
    snapshot = topic["active_snapshot"]
    response = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/gap-analyses",
        json={"snapshot_id": snapshot["snapshot_id"]},
    )
    assert response.status_code == 201, response.text
    return client, data_root, acquired, snapshot, response.json()


def test_analysis_builds_exact_profiles_and_reproducible_evidence_matrix(analyzed_real_topic) -> None:
    client, data_root, acquired, snapshot, analysis = analyzed_real_topic
    assert analysis["status"] == "COMPLETED"
    assert analysis["topic_id"] == workspace.REAL_TOPIC_ID
    assert analysis["snapshot_id"] == snapshot["snapshot_id"]
    assert analysis["run_id"] == acquired["run"]["run_id"]
    assert analysis["input_paper_count"] == 4
    assert analysis["input_review_count"] == 4
    assert analysis["input_evidence_count"] == 12
    assert analysis["input_claim_count"] == 12
    assert analysis["provider_calls"] == 0
    assert analysis["external_network_requests"] == 0

    profiles = analysis["study_profiles"]
    assert len(profiles) == 4
    assert {row["paper_id"] for row in profiles} == {
        "PMC7465609", "PMC6779025", "PMC11910998", "PMC11226757"
    }
    for profile in profiles:
        assert profile["extraction_method"] == research.PROFILE_METHOD
        assert profile["run_id"] == analysis["run_id"]
        assert profile["snapshot_id"] == snapshot["snapshot_id"]
        assert profile["design_type"] == "unknown"
        assert profile["sample_size_known"] is False
        assert profile["sample_size_value"] is None
        assert profile["evidence_direction"] == "positive"
        assert profile["evidence_strength"] == "moderate"
        assert profile["follow_up_bucket"] == "not_reported"
        assert {"small_sample", "short_follow_up", "limited_external_validity"} <= set(profile["limitation_codes"])
        assert profile["evidence_ids"]
        assert profile["claim_ids"]
        assert profile["stale"] is False
        assert str(data_root).lower() not in json.dumps(profile).lower()

    matrix = analysis["evidence_matrix"]
    assert matrix["source_paper_count"] == 4
    assert len(matrix["rows"]) == 4
    assert set(research.MATRIX_COLUMNS) == set(matrix["columns"])
    first_digest = matrix["matrix_digest"]
    repeated = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/gap-analyses",
        json={"snapshot_id": snapshot["snapshot_id"]},
    )
    assert repeated.status_code == 201, repeated.text
    assert repeated.json()["analysis_id"] == analysis["analysis_id"]
    assert repeated.json()["matrix_digest"] == first_digest
    assert repeated.json()["study_profiles"] == profiles


def test_gap_rules_are_corpus_bounded_and_structurally_supported(analyzed_real_topic) -> None:
    _client, _data_root, _acquired, _snapshot, analysis = analyzed_real_topic
    gaps = analysis["gaps"]
    keys = {row["gap_key"] for row in gaps}
    assert {"G01:small_sample", "G01:limited_external_validity", "G01:short_follow_up"} <= keys
    assert {"G02:clinical_learners", "G02:healthcare_trainees", "G03:telerehabilitation"} <= keys
    assert "G06:long_term_follow_up" in keys
    assert "G04:controlled_evaluation" in keys
    assert not any(row["gap_type"] in {"conflicting_findings", "data_availability_gap", "new_unreviewed_evidence"} for row in gaps)
    for gap in gaps:
        assert gap["scope_qualifier"] == research.SCOPE_QUALIFIER
        assert gap["bounded_statement"].startswith("Within the current accepted corpus")
        lowered = gap["bounded_statement"].lower()
        assert "no research exists" not in lowered
        assert "never been studied" not in lowered
        assert "globally novel" not in lowered
        assert gap["generation_eligible"] is True
        assert gap["supporting_profile_ids"] or gap["change_event_ids"]
        assert len(gap["independent_paper_ids"]) == len(set(gap["independent_paper_ids"]))


def test_recommendations_have_exact_gap_evidence_collision_and_scores(analyzed_real_topic) -> None:
    client, _data_root, _acquired, _snapshot, analysis = analyzed_real_topic
    recommendations = analysis["recommendations"]
    assert len(recommendations) >= 3
    gap_ids = {row["gap_id"] for row in analysis["gaps"]}
    for row in recommendations:
        assert set(row["supporting_gap_ids"]) <= gap_ids
        assert row["supporting_gap_ids"]
        assert row["supporting_evidence_ids"]
        assert row["support_acceptance"] == "deterministic_rule_validated_current_gap"
        assert row["global_novelty_status"] == research.GLOBAL_NOVELTY_STATUS
        assert row["scoring_policy_version"] == research.SCORING_POLICY
        expected = round(
            0.15 * row["corpus_relative_novelty_score"]
            + 0.20 * row["feasibility_score"]
            + 0.25 * row["significance_score"]
            + 0.20 * row["falsifiability_score"]
            + 0.10 * row["evidence_readiness_score"]
            + 0.10 * row["timeliness_score"]
        )
        assert row["overall_score"] == expected
        assert row["hypothesis_status"] == "falsifiable_candidate"
        assert row["collision"]["policy_version"] == research.COLLISION_POLICY
        assert "External novelty has not been assessed" in row["collision"]["disclosure"]
        assert "not scientific validation" in row["score_explanation"]
        assert row["status"] == "generated"

    chosen = [row["recommendation_id"] for row in recommendations[:3]]
    compared = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/recommendations/compare",
        json={"recommendation_ids": chosen},
    )
    assert compared.status_code == 200, compared.text
    assert compared.json()["recommendation_ids"] == chosen
    assert len(compared.json()["columns"]) == 3
    assert compared.json()["provider_calls"] == 0


def test_analysis_rejects_wrong_topic_missing_run_and_cross_snapshot_evidence(research_workspace_client) -> None:
    client, _data_root = research_workspace_client
    synthetic = client.get(f"/prototype/workspace/topics/{workspace.SYNTHETIC_TOPIC_ID}").json()
    denied = client.post(
        f"/prototype/workspace/topics/{workspace.SYNTHETIC_TOPIC_ID}/gap-analyses",
        json={"snapshot_id": synthetic["active_snapshot_id"]},
    )
    assert denied.status_code == 409
    assert denied.json()["detail"]["code"] == "research_analysis_real_corpus_required"

    _acquire_real(client)
    topic = client.get(f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}").json()
    cross = client.post(
        f"/prototype/workspace/topics/{workspace.SYNTHETIC_TOPIC_ID}/gap-analyses",
        json={"snapshot_id": topic["active_snapshot_id"]},
    )
    assert cross.status_code == 409
    assert cross.json()["detail"]["code"] == "cross_topic_snapshot_reference"


def test_analysis_does_not_create_network_or_provider_activity(analyzed_real_topic, monkeypatch) -> None:
    client, _data_root, _acquired, snapshot, analysis = analyzed_real_topic

    class ForbiddenNetwork:
        def __init__(self, *args, **kwargs):
            raise AssertionError("analysis must not instantiate a network client")

    monkeypatch.setattr(real, "PmcNetworkClient", ForbiddenNetwork)
    repeated = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/gap-analyses",
        json={"snapshot_id": snapshot["snapshot_id"]},
    )
    assert repeated.status_code == 201
    assert repeated.json()["analysis_id"] == analysis["analysis_id"]
    assert repeated.json()["provider_calls"] == 0
    assert repeated.json()["external_network_requests"] == 0


def test_decisions_are_append_only_and_accept_hold_reject_are_truthful(analyzed_real_topic) -> None:
    client, _data_root, _acquired, _snapshot, analysis = analyzed_real_topic
    recommendations = analysis["recommendations"][:3]
    before_records = deepcopy(workspace._load_state()["research_recommendations"])
    choices = [
        (recommendations[0], "accepted", "evidence_basis_sufficient_for_follow_up"),
        (recommendations[1], "held", "needs_more_evidence"),
        (recommendations[2], "rejected", "feasibility_uncertain"),
    ]
    event_ids = []
    for row, status, reason in choices:
        response = client.post(
            f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/recommendations/{row['recommendation_id']}/decisions",
            json={"requested_status": status, "reason_code": reason, "bounded_note": "Bounded local decision."},
        )
        assert response.status_code == 201, response.text
        payload = response.json()
        assert payload["recommendation"]["status"] == status
        assert payload["decision_event"]["previous_status"] == "generated"
        assert payload["decision_event"]["actor_mode"] == research.LOCAL_ACTOR
        event_ids.append(payload["decision_event"]["decision_event_id"])
    state = workspace._load_state()
    assert state["research_recommendations"] == before_records
    assert len(state["recommendation_decisions"]) == 3
    assert len(set(event_ids)) == 3
    held_promotion = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/recommendations/{recommendations[1]['recommendation_id']}/promote",
        json={"confirmed": True},
    )
    assert held_promotion.status_code == 409
    rejected_promotion = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/recommendations/{recommendations[2]['recommendation_id']}/promote",
        json={"confirmed": True},
    )
    assert rejected_promotion.status_code == 409


def test_accepted_recommendation_promotion_creates_empty_local_topic_once(analyzed_real_topic) -> None:
    client, _data_root, _acquired, _snapshot, analysis = analyzed_real_topic
    recommendation = analysis["recommendations"][0]
    denied = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/recommendations/{recommendation['recommendation_id']}/promote",
        json={"confirmed": False},
    )
    assert denied.status_code == 400
    decision = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/recommendations/{recommendation['recommendation_id']}/decisions",
        json={
            "requested_status": "accepted",
            "reason_code": "clear_falsifiable_question",
            "bounded_note": "Promote as a local planning topic only.",
        },
    )
    assert decision.status_code == 201
    promoted = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/recommendations/{recommendation['recommendation_id']}/promote",
        json={"confirmed": True},
    )
    assert promoted.status_code == 201, promoted.text
    payload = promoted.json()
    topic = payload["topic"]
    assert topic["topic_kind"] == "local_empty"
    assert topic["source_policy"] == "local_only_no_external_source"
    assert topic["provider_posture"] == "deterministic_only"
    assert topic["active_snapshot"] is None
    assert topic["runs"] == []
    assert topic["reports"] == []
    assert topic["living_schedule"]["enabled"] is False
    assert topic["living_schedule"]["network_policy"] == "disabled"
    assert topic["research_gaps_workspace"]["promoted_origin"]["recommendation_id"] == recommendation["recommendation_id"]
    assert payload["automatic_snapshot_count"] == 0
    assert payload["automatic_run_count"] == 0
    assert payload["automatic_report_count"] == 0
    assert payload["automatic_schedule_count"] == 0
    assert payload["provider_calls"] == 0
    assert payload["network_requests"] == 0
    repeated = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/recommendations/{recommendation['recommendation_id']}/promote",
        json={"confirmed": True},
    )
    assert repeated.status_code == 201
    assert repeated.json()["duplicate"] is True
    assert repeated.json()["topic"]["topic_id"] == topic["topic_id"]


def test_curated_three_paper_analysis_adds_coverage_gap_and_preserves_history(analyzed_real_topic) -> None:
    client, _data_root, _acquired, original_snapshot, before = analyzed_real_topic
    source, changeset = _hold_preview(client)
    assert source["snapshot_id"] == original_snapshot["snapshot_id"]
    applied = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/changesets/{changeset['changeset_id']}/apply",
        json={"confirmed": True},
    ).json()
    run = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/runs",
        json={"snapshot_id": applied["snapshot_id"], "confirmed": True},
    )
    assert run.status_code == 200, run.text
    after_response = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/gap-analyses",
        json={"snapshot_id": applied["snapshot_id"]},
    )
    assert after_response.status_code == 201, after_response.text
    after = after_response.json()
    assert after["input_paper_count"] == 3
    assert after["input_evidence_count"] == 9
    coverage = next(row for row in after["gaps"] if row["gap_type"] == "coverage_loss")
    assert coverage["change_event_ids"]
    assert "historical reports and evidence remain unchanged" in coverage["bounded_statement"]
    comparison_response = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/gap-analyses/compare",
        json={"before_analysis_id": before["analysis_id"], "after_analysis_id": after["analysis_id"]},
    )
    assert comparison_response.status_code == 200, comparison_response.text
    comparison = comparison_response.json()
    assert comparison["before_snapshot_id"] == original_snapshot["snapshot_id"]
    assert comparison["after_snapshot_id"] == applied["snapshot_id"]
    assert comparison["change_attribution"] == "corpus_curation"
    assert comparison["gaps_added"]
    assert comparison["recommendation_score_deltas"]
    reopened_before = client.get(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/gap-analyses/{before['analysis_id']}"
    ).json()
    assert reopened_before["analysis_digest"] == before["analysis_digest"]
    assert reopened_before["study_profiles"] == before["study_profiles"]
    assert all(row["stale"] is False for row in reopened_before["recommendations"])


def test_export_is_portable_bounded_and_reopens_without_paths(analyzed_real_topic) -> None:
    client, data_root, _acquired, _snapshot, analysis = analyzed_real_topic
    response = client.get(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/gap-analyses/{analysis['analysis_id']}/export"
    )
    assert response.status_code == 200, response.text
    exported = response.json()
    assert exported["media_type"].startswith("text/markdown")
    assert "All findings are bounded to the current accepted corpus" in exported["content"]
    assert "External novelty has not been assessed" in exported["content"]
    assert "Recommendations are decision support, not scientific validation" in exported["content"]
    assert analysis["run_id"] in exported["content"]
    assert analysis["matrix_digest"] in exported["content"]
    assert str(data_root).lower() not in exported["content"].lower()
    assert "\\raw\\" not in exported["content"].lower()
    reopened = client.get(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/gap-analyses/{analysis['analysis_id']}"
    )
    assert reopened.status_code == 200
    assert reopened.json()["analysis_digest"] == analysis["analysis_digest"]


def test_workspace_totals_and_v05_state_upgrade_keep_prior_records(research_workspace_client) -> None:
    client, _data_root = research_workspace_client
    before = client.get("/prototype/workspace").json()
    assert before["totals"]["research_gap_analyses"] == 0
    assert before["totals"]["research_recommendations"] == 0
    _acquire_real(client)
    topic = client.get(f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}").json()
    analyzed = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/gap-analyses",
        json={"snapshot_id": topic["active_snapshot_id"]},
    ).json()
    after = client.get("/prototype/workspace").json()
    assert after["schema_version"] == workspace.WORKSPACE_SCHEMA
    assert after["totals"]["research_gap_analyses"] == 1
    assert after["totals"]["research_gaps"] == analyzed["gap_count"]
    assert after["totals"]["research_recommendations"] == analyzed["recommendation_count"]
    assert len(after["topics"]) == 3
    assert after["totals"]["living_topics"] == before["totals"]["living_topics"]


def test_workspace_validation_rejects_global_gap_claim_without_persisting(analyzed_real_topic) -> None:
    _client, _data_root, _acquired, _snapshot, analysis = analyzed_real_topic
    original = deepcopy(workspace._load_state()["research_gaps"])

    def invalid_mutation(state):
        candidate = deepcopy(state["research_gaps"][0])
        candidate["gap_id"] = "gap-invalid-global-claim"
        candidate["bounded_statement"] = "No research exists for this question."
        candidate["gap_digest"] = research._record_digest(candidate, "gap_digest")
        state["research_gaps"].append(candidate)
        return state, None

    with pytest.raises(workspace.PrototypeWorkspaceError, match="research_gap_global_claim_rejected"):
        workspace._mutate_state(invalid_mutation)
    assert workspace._load_state()["research_gaps"] == original
    assert analysis["status"] == "COMPLETED"


def test_remaining_gap_rule_families_require_explicit_compatible_evidence(analyzed_real_topic) -> None:
    _client, _data_root, _acquired, snapshot, analysis = analyzed_real_topic
    state = workspace._load_state()
    context = research._analysis_context(state, workspace.REAL_TOPIC_ID, snapshot["snapshot_id"])
    left = deepcopy(analysis["study_profiles"][0])
    right = deepcopy(analysis["study_profiles"][1])
    left.update({
        "paper_id": "PAPER-A", "study_profile_id": "profile-paper-a",
        "evidence_direction": "positive", "limitation_codes": ["measurement_limitations"],
        "data_availability_codes": ["explicit_unavailable"], "claim_ids": ["claim-low"],
        "evidence_ids": ["evidence-low-a"],
    })
    right.update({
        "paper_id": "PAPER-B", "study_profile_id": "profile-paper-b",
        "evidence_direction": "negative", "limitation_codes": ["measurement_limitations"],
        "data_availability_codes": ["restricted"], "claim_ids": [],
        "evidence_ids": ["evidence-low-b"],
    })
    context["evidence"] = [
        {"evidence_id": "evidence-low-a", "paper_id": "PAPER-A", "confidence_strength": "low"},
        {"evidence_id": "evidence-low-b", "paper_id": "PAPER-B", "confidence_strength": "low"},
    ]
    context["claims"] = [{
        "claim_id": "claim-low", "claim_category": "single_material_result",
        "paper_ids": ["PAPER-A"], "evidence_ids": ["evidence-low-a"],
    }]
    scope = research._scope_record(workspace.REAL_TOPIC_ID, "2026-08-02T00:00:00Z")
    gaps = research._detect_gaps(
        state, context, scope, "analysis-explicit-rules", [left, right], "2026-08-02T00:00:00Z"
    )
    types = {row["gap_type"] for row in gaps}
    assert {
        "measurement_gap", "replication_gap", "evidence_strength_gap",
        "conflicting_findings", "data_availability_gap",
    } <= types
    conflict = next(row for row in gaps if row["gap_type"] == "conflicting_findings")
    assert conflict["counterevidence_present"] is True
    assert set(conflict["independent_paper_ids"]) == {"PAPER-A", "PAPER-B"}
    right["evidence_direction"] = "unknown"
    unknown_direction = research._detect_gaps(
        state, context, scope, "analysis-unknown-direction", [left, right], "2026-08-02T00:00:00Z"
    )
    assert not any(row["gap_type"] == "conflicting_findings" for row in unknown_direction)


def test_analysis_context_rejects_stale_sources_and_duplicate_work_versions(analyzed_real_topic) -> None:
    _client, _data_root, _acquired, snapshot, _analysis = analyzed_real_topic
    stale = workspace._load_state()
    stale_snapshot = workspace._snapshot(stale, snapshot["snapshot_id"], topic_id=workspace.REAL_TOPIC_ID)
    stale_snapshot["items"][0]["stale_status"] = "stale"
    with pytest.raises(workspace.PrototypeWorkspaceError, match="research_analysis_untrusted_source"):
        research._analysis_context(stale, workspace.REAL_TOPIC_ID, snapshot["snapshot_id"])

    duplicate = workspace._load_state()
    duplicate_snapshot = workspace._snapshot(duplicate, snapshot["snapshot_id"], topic_id=workspace.REAL_TOPIC_ID)
    duplicate_snapshot["items"][1]["work_identity_digest"] = duplicate_snapshot["items"][0]["work_identity_digest"]
    with pytest.raises(workspace.PrototypeWorkspaceError, match="research_analysis_duplicate_work_identity"):
        research._analysis_context(duplicate, workspace.REAL_TOPIC_ID, snapshot["snapshot_id"])


def test_current_corpus_collision_is_deterministic_and_does_not_claim_global_novelty(analyzed_real_topic) -> None:
    _client, _data_root, _acquired, snapshot, analysis = analyzed_real_topic
    state = workspace._load_state()
    context = research._analysis_context(state, workspace.REAL_TOPIC_ID, snapshot["snapshot_id"])
    recommendation = analysis["recommendations"][0]
    collision = research._collision(
        state,
        context,
        recommendation["research_question"],
        recommendation["collision_facets"],
    )
    repeated = research._collision(
        state,
        context,
        recommendation["research_question"],
        recommendation["collision_facets"],
    )
    assert collision == repeated
    assert collision["result"] == "duplicate_candidate"
    assert "external novelty has not been assessed" in collision["disclosure"].lower()


def test_historical_snapshot_recommendation_cannot_be_promoted(analyzed_real_topic) -> None:
    client, _data_root, _acquired, _snapshot, analysis = analyzed_real_topic
    recommendation = analysis["recommendations"][0]
    accepted = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/recommendations/{recommendation['recommendation_id']}/decisions",
        json={
            "requested_status": "accepted",
            "reason_code": "clear_falsifiable_question",
            "bounded_note": "Accepted while this snapshot is current.",
        },
    )
    assert accepted.status_code == 201
    _source, changeset = _hold_preview(client)
    applied = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/changesets/{changeset['changeset_id']}/apply",
        json={"confirmed": True},
    )
    assert applied.status_code == 200
    blocked = client.post(
        f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/recommendations/{recommendation['recommendation_id']}/promote",
        json={"confirmed": True},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "historical_recommendation_promotion_blocked"
