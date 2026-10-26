"""Saved reports retain explicit topic identity across profile name edits."""
from pathlib import Path

import pytest

from app.auth.resource_access import ResourceActor, resource_actor_context
from app.repositories import runs
from app.repositories.files import write_json
from app.services import report_hub, topic_profiles
from tests.test_report_artifact_truthfulness import _run_record


@pytest.fixture
def reports(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "STORAGE_ROOT", tmp_path / "runs")
    profiles_path = tmp_path / "profiles" / "profiles.json"
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profiles_path)
    profiles = [
        {"profile_id": "profile-a", "title": "Profile A", "topic": "Original topic",
         "resource_kind": "user_owned", "owner_user_id": "owner-a"},
        {"profile_id": "profile-b", "title": "Profile B", "topic": "Renamed topic",
         "resource_kind": "user_owned", "owner_user_id": "owner-b"},
    ]
    write_json(profiles_path, profiles)

    def save(run_id, topic, binding):
        report_path = tmp_path / "reports" / f"{run_id}.md"
        report_path.parent.mkdir(exist_ok=True)
        report_path.write_text(f"# Preserved report {run_id}\n", encoding="utf-8")
        run = _run_record(run_id, artifacts={"report_markdown": str(report_path)})
        run.update(topic=topic, topic_profile_id=binding, write_legacy=False)
        runs.save_run_record(run)
        return runs.load_run_record(run_id)

    return save


def _rename_a():
    return topic_profiles.update_topic_profile(
        "profile-a", {"topic": "Renamed topic"}, touch_metadata=False,
    )


def test_saved_report_stays_visible_after_topic_rename_without_changing_artifact(reports):
    report = reports("report-a", "Original topic", "profile-a")
    path = Path(report["artifacts"]["report_markdown"])
    original = path.read_bytes()
    before = report_hub.build_api_reports([report], topic_id="profile-a")
    assert before["items"][0]["topicId"] == "profile-a"

    assert _rename_a()["profile_id"] == "profile-a"
    reloaded = runs.load_run_record("report-a")
    after = report_hub.build_api_reports([reloaded], topic_id="profile-a")

    assert after["items"] == before["items"]
    assert after["items"][0]["downloadUrl"] == "/runs/report-a/report/download"
    assert path.read_bytes() == original
    assert reloaded["topic"] == "Original topic"


def test_explicit_profile_binding_controls_filter_and_unfiltered_projection(reports):
    report_a = reports("report-a", "Original topic", "profile-a")
    report_b = reports("report-b", "Renamed topic", "profile-b")
    _rename_a()

    selected = report_hub.build_api_reports([report_a, report_b], topic_id="profile-a")
    assert [item["runId"] for item in selected["items"]] == ["report-a"]
    assert selected["provenance"]["sourceRunIds"] == ["report-a"]
    all_reports = report_hub.build_api_reports([report_a, report_b])
    assert {item["runId"]: item["topicId"] for item in all_reports["items"]} == {
        "report-a": "profile-a", "report-b": "profile-b",
    }


@pytest.mark.parametrize("binding", ["invisible-profile", 0, ["profile-a"]])
def test_missing_or_malformed_binding_does_not_fall_back_to_visible_name(reports, binding):
    report = reports("unresolved-report", "Original topic", binding)
    assert report_hub.build_api_reports([report], topic_id="profile-a")["items"] == []
    assert report_hub.build_api_reports([report])["items"][0]["topicId"] is None


def test_explicit_topic_projection_uses_current_actor_visible_profiles(reports):
    report = reports("owned-report", "Original topic", "profile-b")
    with resource_actor_context(ResourceActor.oidc_user("owner-a")):
        item = report_hub.build_api_reports([report])["items"][0]
        assert item["topicId"] is None
        with pytest.raises(FileNotFoundError):
            report_hub.build_api_reports([report], topic_id="profile-b")


def test_unbound_legacy_reports_keep_exact_name_membership_without_rename_alias(reports):
    legacy = reports("legacy-report", "Original topic", None)
    wrong_case = reports("legacy-case-report", "original topic", None)
    selected = report_hub.build_api_reports([legacy, wrong_case], topic_id="profile-a")
    assert [item["runId"] for item in selected["items"]] == ["legacy-report"]
    assert selected["items"][0]["topicId"] == "profile-a"
    _rename_a()
    assert report_hub.build_api_reports([legacy], topic_id="profile-a")["items"] == []
