from contextlib import closing

import pytest

from app.db.bootstrap import connect_metadata_index
from app.repositories import local_synthesis, synthesis_reports as reports
from tests import test_local_synthesis as synthesis
from tests import test_synthesis_reports as review

source = synthesis.source
seeded = synthesis.seeded


def save(source, request="first"):
    job, _ = synthesis.capture(source, request)
    for step in range(job["total_steps"]):
        job, _ = local_synthesis.advance("topic", job["job_id"], step)
    review.decide(job)
    return job


def test_comparison_preserves_versions_and_exact_evidence_in_both_directions(source):
    first = save(source)
    original = reports.detail("topic", first["job_id"])
    exported = reports.export("topic", first["job_id"], original["version"]["version_id"], original["latest_decision"]["decision_id"], "json")
    source[0]["_raw"]["abstract"] = "New abstract"
    source[0]["_selectedReview"]["evidence_spans"][0]["locator"] = {"page": 3}
    source[0]["_selectedReview"]["evidence_spans"][0]["text"] = "New exact excerpt"
    source[2]["authors"] = ["Another author"]
    second = save(source[:-1], "second")
    result = reports.compare("topic", second["job_id"], first["job_id"])
    assert result["counts"] == dict(added=0, removed=1, changed=2, unchanged=3)
    assert result["matching"] == "paper_version_id" and result["scientific_assessment"] == "NOT_ASSESSED"
    changed = {p["paper_version_id"]: p for p in result["papers"]}
    assert changed["v1"]["status"] == "unchanged"  # same canonical work as v0, never collapsed
    fields = {f["field"]: f for f in changed["v0"]["fields"]}
    assert fields["abstract"]["before"] == "Original abstract 0" and fields["abstract"]["after"] == "New abstract"
    assert fields["evidence"]["before"] == first["inputs"]["papers"][0]["evidence"]
    assert fields["evidence"]["after"] == second["inputs"]["papers"][0]["evidence"]
    assert result["coverage"][0] == dict(metric="selected_papers", before=6, after=5, delta=-1)
    assert all(row["metric"] != "contradiction_count" for row in result["coverage"])
    reverse = reports.compare("topic", first["job_id"], second["job_id"])
    assert reverse["counts"] == dict(added=1, removed=0, changed=2, unchanged=3)
    assert reverse["coverage"][0]["delta"] == 1
    assert reverse["papers"][0]["fields"][0]["before"] == fields["abstract"]["after"]
    assert reports.detail("topic", first["job_id"]) == original
    assert reports.export("topic", first["job_id"], original["version"]["version_id"], original["latest_decision"]["decision_id"], "json") == exported
    same = reports.compare("topic", first["job_id"], first["job_id"])
    assert same["counts"] == dict(added=0, removed=0, changed=0, unchanged=6)
    assert same["input_context"] == []
    assert all(row["delta"] == 0 for row in same["coverage"])


def test_report_pages_include_held_versions_but_exclude_unaccepted_drafts(source):
    # Match application startup: read endpoints do not create a missing store.
    with closing(connect_metadata_index()):
        pass
    assert reports.listing("topic")["total"] == 0
    first = save(source)
    second = save(source[:2], "second")
    third = save(source[:1], "third")
    synthesis.capture(source[2:], "unaccepted")
    previous = reports.detail("topic", second["job_id"])["latest_decision"]
    review.decide(second, "held", "held", previous["decision_id"])
    assert reports.listing("topic", 1, 2)["total"] == 3
    assert [r["job_id"] for r in reports.listing("topic", 1, 2)["items"]] == [third["job_id"], second["job_id"]]
    assert reports.listing("topic", 1, 2)["items"][1]["latest_decision"]["state"] == "held"
    assert reports.listing("topic", 2, 2)["items"][0]["job_id"] == first["job_id"]
    assert reports.listing("topic", 3, 2)["items"] == []
    assert reports.listing("other-topic")["total"] == 0
    assert reports.compare("topic", second["job_id"], first["job_id"])["after"]["latest_decision"]["state"] == "held"
    with pytest.raises(ValueError, match="pagination_invalid"):
        reports.listing("topic", 0)


def test_missing_cross_topic_and_corrupt_reports_never_look_unchanged(source):
    first = save(source)
    second = save(source[:2], "second")
    for topic, before, after in [("other-topic", first["job_id"], second["job_id"]), ("topic", "missing", first["job_id"])]:
        with pytest.raises(KeyError):
            reports.compare(topic, after, before)
    with closing(connect_metadata_index()) as c:
        c.execute("DROP TRIGGER synthesis_report_versions_no_update")
        c.execute("UPDATE synthesis_report_versions SET payload_json='{}' WHERE job_id=?", (first["job_id"],))
        c.commit()
    for before, after in [(first["job_id"], second["job_id"]), (second["job_id"], first["job_id"])]:
        with pytest.raises(ValueError, match="integrity_invalid"):
            reports.compare("topic", after, before)
    with pytest.raises(ValueError, match="integrity_invalid"):
        reports.listing("topic")


def test_no_paper_change_does_not_hide_input_context_change(source, monkeypatch):
    first = save(source)
    project = local_synthesis.temporal_sources.project
    def renamed(*args, consume, **kwargs):
        def wrapped(profile, catalog, members):
            consume({**profile, "title": "Renamed topic"}, catalog, members)
        return project(*args, consume=wrapped, **kwargs)
    monkeypatch.setattr(local_synthesis.temporal_sources, "project", renamed)
    second = save(source, "renamed")
    diff = reports.compare("topic", second["job_id"], first["job_id"])
    assert diff["counts"] == dict(added=0, removed=0, changed=0, unchanged=6)
    assert diff["input_context"] == [dict(field="topic_label", before="Synthetic topic", after="Renamed topic")]


def test_report_routes_and_private_topic_authorization(seeded, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.auth.security import require_product_user
    from app.auth.resource_access import ResourceActor
    from app.services import paper_bank
    from app.api import local_synthesis as api
    app = create_app()
    app.dependency_overrides[require_product_user] = lambda: None
    topic = seeded.TOPIC_A_ID
    root = f"/api/topics/{topic}/local-synthesis"
    selection = synthesis.select(paper_bank.list_topic_papers(topic, page_size=100)["items"][:2])
    with TestClient(app) as client:
        assert client.get(root + "/reports").json()["total"] == 0
        job = client.post(root, json=dict(request_id="capture", selection=selection)).json()["job"]
        path = root + "/" + job["job_id"]
        for step in range(job["total_steps"]):
            assert client.post(path + "/step", json=dict(expected_step=step)).status_code == 200
        assert client.get(path + "/report/comparison", params={"from_job_id": job["job_id"]}).status_code == 404
        detail = client.get(path + "/report").json()
        assert client.post(path + "/decisions", json=dict(request_id="accept", state="accepted", note="", expected_previous_id=None, expected_binding=detail["candidate_digest"])).status_code == 201
        assert client.get(root + "/reports?page=0").status_code == 422
        listing = client.get(root + "/reports?page=1&limit=1").json()
        assert listing["total"] == 1 and listing["items"][0]["job_id"] == job["job_id"]
        assert client.get(path + "/report/comparison", params={"from_job_id": job["job_id"]}).json()["counts"]["unchanged"] == 2
        assert client.get(path + "/report/comparison", params={"from_job_id": "missing"}).status_code == 404
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("outsider"))
        assert client.get(root + "/reports").status_code == 404
        assert client.get(path + "/report/comparison", params={"from_job_id": job["job_id"]}).status_code == 404
