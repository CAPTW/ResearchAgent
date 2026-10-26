"""Stable topic membership must survive edits without widening run visibility."""
import pytest

from app.auth.resource_access import ResourceActor
from app.domain.topic_membership import run_matches_topic
from app.services import paper_bank, paper_review, review_input_history, review_preparation, temporal_memory, topic_analytics, topic_dashboard, topic_profiles
from tests import test_review_preparation as preparation_tests

seeded = preparation_tests.seeded
linked = preparation_tests.linked


def test_topic_edit_retains_papers_and_exact_preparation_history(linked):
    topic, version, actor = linked["topic"], linked["version"], linked["actor"]
    before = paper_bank.list_topic_papers(topic, page_size=100, actor=actor)
    request = preparation_tests.selected(linked)
    saved = review_preparation.prepare(topic, version, request, actor)
    from tests.test_review_source_coverage import append
    receipt = append(saved)
    snapshot = temporal_memory.capture(topic, actor)
    profile = topic_profiles.update_topic_profile(topic, {"topic": "새 연구 범위 e\u0301 한글🙂"}, actor=actor)
    assert profile["profile_id"] == topic
    after = paper_bank.list_topic_papers(topic, page_size=100, actor=actor)
    assert [item["paperVersionId"] for item in after["items"]] == [item["paperVersionId"] for item in before["items"]]
    assert review_preparation.detail(topic, version, saved["preparation_id"], actor) == saved
    assert review_preparation.prepare(topic, version, request, actor) == saved
    assert review_input_history.detail(topic, version, receipt["receipt_id"], actor)["packet"] == receipt["packet"]
    assert temporal_memory.capture(topic, actor) == {"snapshot": snapshot["snapshot"], "created": False}
    new = review_preparation.prepare(topic, version, request.model_copy(update={"request_id": "after-rename"}), actor)
    assert new["context"]["topic_name"] == profile["topic"]
    assert saved["context"]["topic_name"] != new["context"]["topic_name"]
    assert new["source"] == saved["source"] and new["selection"] == saved["selection"]


@pytest.mark.parametrize("reader", ["bank", "review", "analytics", "dashboard", "legacy_dashboard"])
def test_explicit_topic_binding_wins_over_another_profiles_matching_name(seeded, reader):
    from app.repositories import metadata_index
    topic = seeded.TOPIC_A_ID
    profile = topic_profiles.update_topic_profile(topic, {"topic": seeded.TOPIC_B_NAME})
    if reader == "bank":
        ids = {occurrence["runId"] for item in paper_bank.list_topic_papers(topic, page_size=100)["items"]
               for occurrence in item["compatibility"]["occurrences"]}
    elif reader == "review":
        ids = {run["run_id"] for run in paper_review._topic_run_records(profile)}
    elif reader == "analytics":
        ids = {run["runId"] for run in topic_analytics.build_topic_analytics_snapshot(topic)["sourceRuns"]}
    elif reader == "dashboard":
        ids = {item["runId"] for item in topic_dashboard.build_api_topic_papers(topic)["items"]}
    else:
        for run_id in (seeded.RUN_A_1, seeded.RUN_A_2, seeded.RUN_B_1):
            metadata_index.index_run(run_id)
        ids = {run["run_id"] for run in topic_dashboard.build_topic_dashboard(topic)["recent_runs"]}
    assert ids == {seeded.RUN_A_1, seeded.RUN_A_2}


@pytest.mark.parametrize("binding,name,expected", [
    ("topic-a", "old name", True), ("topic-b", "current name", False),
    ("unknown", "current name", False), (None, "current name", True),
    ("", "current name", True), (" ", "current name", True),
    (None, "old name", False), (None, "Current Name", False),
    (None, " current name ", False), (0, "current name", False),
    (False, "current name", False), (["topic-a"], "current name", False),
])
def test_membership_uses_only_explicit_binding_or_exact_unbound_legacy_name(binding, name, expected):
    run = {"topic_profile_id": binding, "topic": name}
    assert run_matches_topic(run, "current name", "topic-a") is expected
    assert run == {"topic_profile_id": binding, "topic": name}


def test_name_only_legacy_query_and_missing_legacy_binding_keep_their_contract():
    assert run_matches_topic({"topic": "old", "topic_profile_id": "another"}, "old")
    assert run_matches_topic({"topic": "old"}, "old", "profile")
    assert not run_matches_topic({"topic": "old"}, "old", "")


@pytest.mark.parametrize("owner", ["another-user", None])
def test_explicit_topic_binding_does_not_bypass_existing_owner_filter(seeded, monkeypatch, owner):
    from app.repositories import runs
    topic_profiles.update_topic_profile(seeded.TOPIC_A_ID, {"topic": "edited research topic"})
    rows = runs.list_run_records(limit=None)
    for row in rows:
        if row["run_id"] == seeded.RUN_A_2:
            row["owner_user_id"] = owner
    monkeypatch.setattr(topic_dashboard, "list_run_records", lambda **kwargs: rows)
    actor = ResourceActor.oidc_user("local-internal-user")
    result = paper_bank.list_topic_papers(seeded.TOPIC_A_ID, page_size=100, actor=actor)
    assert {occ["runId"] for item in result["items"] for occ in item["compatibility"]["occurrences"]} == {seeded.RUN_A_1}


def test_actual_api_edit_preserves_history_across_new_app_instances(linked, monkeypatch):
    from app.api import paper_bank as api
    from tests.test_review_preparation_api import client
    from tests.test_review_source_coverage import append
    topic, version, actor = linked["topic"], linked["version"], linked["actor"]
    body = preparation_tests.selected(linked)
    saved = review_preparation.prepare(topic, version, body, actor)
    receipt = append(saved)
    base = f"/api/topics/{topic}/paper-bank/{version}"
    with client() as http:
        response = http.patch(f"/topic-profiles/{topic}", json={"topic": "edited via API"})
        assert response.status_code == 200, response.text
    monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("local-internal-user"))
    with client() as http:
        assert http.get(base + "/review-preparations/" + saved["preparation_id"]).json() == saved
        assert http.get(base + "/review-inputs/" + receipt["receipt_id"]).json()["packet"] == receipt["packet"]
        assert http.post(base + "/review-preparations", json=body.model_dump()).json() == saved
        assert http.get(base.replace(topic, linked["other_topic"]) + "/review-preparations").status_code == 404
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("outsider"))
        assert http.get(base + "/review-preparations/" + saved["preparation_id"]).status_code == 404
        assert http.get(base + "/review-inputs/" + receipt["receipt_id"]).status_code == 404


def test_stored_v3_analytics_is_not_recomputed_and_new_snapshots_identify_the_new_rule(seeded, tmp_path, monkeypatch):
    import json
    directory = tmp_path / "analytics"
    directory.mkdir()
    monkeypatch.setattr(topic_analytics, "ANALYTICS_LIBRARY_DIR", directory)
    topic = seeded.TOPIC_A_ID
    old = topic_analytics.build_topic_analytics_snapshot(topic)
    old["artifactVersion"] = "topic-analytics-v3"  # A stored pre-upgrade fixture.
    path = directory / f"{topic}.json"
    original = json.dumps(old, ensure_ascii=False, indent=2).encode("utf-8")
    path.write_bytes(original)
    topic_profiles.update_topic_profile(topic, {"topic": seeded.TOPIC_B_NAME})
    assert topic_analytics.load_topic_analytics(topic) == old
    current = topic_analytics.build_topic_analytics_snapshot(topic)
    assert current["artifactVersion"] == "topic-analytics-v4"
    assert {run["runId"] for run in current["sourceRuns"]} == {seeded.RUN_A_1, seeded.RUN_A_2}
    assert topic_analytics.load_topic_analytics(topic) == old
    assert path.read_bytes() == original
