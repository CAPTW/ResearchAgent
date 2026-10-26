from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
import sqlite3

import pytest

from app.domain import temporal_analysis as d
from app.domain.temporal_memory import Member, snapshot
from app.repositories import temporal_analysis as repo, temporal_memory as memory


@pytest.fixture(autouse=True)
def execution_boundary(monkeypatch):
    import httpx
    import subprocess
    import urllib.request

    def prohibited(*args, **kwargs):
        raise AssertionError("external/provider/retrieval/subprocess execution prohibited")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", prohibited)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", prohibited)
    monkeypatch.setattr(urllib.request, "urlopen", prohibited)
    monkeypatch.setattr(subprocess, "Popen", prohibited)


def member(**kwargs):
    return replace(
        Member(
            "paper-a",
            "version-a",
            review_state="not_reviewed",
            full_text_state="abstract_only",
            evidence_state="available",
            rights_access_state="restricted",
        ),
        **kwargs,
    )


def policy(**kwargs):
    return d.policy("topic", kwargs, "2026-01-01T00:00:00Z")


def calculation(members=None, before=None, **config):
    current = snapshot(
        "topic",
        members if members is not None else [member()],
        "2026-02-01T00:00:00Z",
        before.snapshot_id if before else None,
    )
    return d.evaluate(policy(**config), current, before, "clock")


def test_policy_canonical_clock_sets_and_immutability():
    x = policy()
    y = d.policy("topic", {"enabled_metric_ids": list(reversed(d.METRIC_IDS)) + [d.METRIC_IDS[0]]}, "later")
    assert x.policy_id == y.policy_id
    assert len(x.policy_id) == 69
    assert policy(minimum_absolute_count_delta=2).policy_id != x.policy_id
    with pytest.raises(FrozenInstanceError):
        x.topic_id = "other"


@pytest.mark.parametrize(
    "config",
    [
        {"unknown": 1},
        {"enabled_metric_ids": ["custom"]},
        {"enabled_insight_kinds": ["custom"]},
        {"minimum_paper_count": -1},
        {"minimum_denominator": True},
        {"minimum_percentage_point_delta_basis_points": 1.1},
        {"minimum_absolute_count_delta": 1000001},
        {"schema_version": "bad"},
        {"formula": '__import__("os")'},
        {"code": "print(1)"},
    ],
)
def test_policy_strict(config):
    with pytest.raises(ValueError):
        policy(**config)


def test_fourteen_formulas_and_first_snapshot_six_insights():
    evaluation, points, insights = calculation()
    assert len(points) == 14 and {p.metric_id for p in points} == set(d.METRIC_IDS)
    assert len(insights) == 6 and {i.insight_kind for i in insights} == set(d.INSIGHT_KINDS)
    assert all(
        p.state == "INSUFFICIENT_BASELINE" and p.count_value is None for p in points if p.metric_id.startswith("delta.")
    )
    assert all(i.state == "INSUFFICIENT_DATA" for i in insights)
    assert evaluation.metric_point_count == 14


def test_exact_counts_and_any_version_coverage():
    reviewed = member(
        paper_version_id="version-b",
        review_state="full_text_reviewed",
        full_text_state="reviewed",
        full_text_content_digest="a" * 64,
        evidence_ids=("ev",),
        evidence_digests=(("ev", "b" * 64),),
    )
    _, points, _ = calculation([member(), reviewed, member(canonical_paper_id="b", paper_version_id="v-b")])
    p = {p.metric_id: p for p in points}
    assert p["corpus.paper_count"].count_value == 2
    assert p["corpus.paper_version_count"].count_value == 3
    for key in ["reviewed_paper", "full_text_available", "evidence_backed_paper"]:
        assert (p["coverage." + key + "_ratio"].numerator, p["coverage." + key + "_ratio"].denominator) == (1, 2)
    assert p["coverage.rights_known_ratio"].numerator == 2


def test_empty_denominator_never_zero_ratio():
    _, points, _ = calculation([])
    assert all(
        p.state == "INSUFFICIENT_DENOMINATOR" and p.numerator is None and p.denominator == 0
        for p in points
        if p.unit == "RATIO"
    )


@pytest.mark.parametrize(
    "field,value,key",
    [
        ("review_state", "unavailable", "reviewed_paper"),
        ("full_text_state", "unavailable", "full_text_available"),
        ("evidence_state", "unavailable", "evidence_backed_paper"),
        ("rights_access_state", "unknown", "rights_known"),
        ("full_text_state", "reviewed", "full_text_available"),
    ],
)
def test_unknown_not_zero(field, value, key):
    _, points, _ = calculation([member(**{field: value})])
    p = next(p for p in points if p.metric_id == "coverage." + key + "_ratio")
    assert p.state == "UNAVAILABLE" and p.numerator is None and p.count_value is None


def test_eight_delta_counts_and_deterministic_order_clock():
    before = snapshot("topic", [member(), member(canonical_paper_id="gone", paper_version_id="gone")], "first")
    changed = member(
        paper_version_id="new-v",
        review_state="full_text_reviewed",
        full_text_state="reviewed",
        full_text_content_digest="a" * 64,
        evidence_ids=("ev",),
        evidence_digests=(("ev", "b" * 64),),
        collection_ids=("saved",),
        rights_access_state="open_access",
    )
    members = [changed, member(canonical_paper_id="new", paper_version_id="new")]
    a = calculation(members, before)
    b = calculation(list(reversed(members)), before)
    assert a == b
    assert [p.count_value for p in a[1] if p.metric_id.startswith("delta.")] == [1] * 8
    later = d.evaluate(policy(), snapshot("topic", members, "later", before.snapshot_id), before, "later")
    assert [p.metric_point_id for p in a[1]] == [p.metric_point_id for p in later[1]]
    assert [i.insight_record_id for i in a[2]] == [i.insight_record_id for i in later[2]]


def test_insight_accepted_threshold_exact_and_provenance():
    before = snapshot("topic", [member()], "first")
    _, points, records = calculation([member(review_state="abstract_reviewed")], before)
    review = next(i for i in records if i.insight_kind == "REVIEW_COVERAGE_CHANGE")
    assert review.state == "ACCEPTED" and review.percentage_point_delta_basis_points == 10000
    assert (review.before_numerator, review.before_denominator, review.after_numerator, review.after_denominator) == (
        0,
        1,
        1,
        1,
    )
    assert review.source_entity_ids and review.metric_point_ids
    assert set(review.metric_point_ids) <= {p.metric_point_id for p in points}
    assert review.confidence_class == "DETERMINISTIC_EXACT"
    assert next(i for i in records if i.insight_kind == "CORPUS_SIZE_CHANGE").state == "BELOW_POLICY_THRESHOLD"
    assert all(
        word not in review.rendered_factual_text.lower()
        for word in ["quality", "maturity", "impact", "consensus", "influential"]
    )


def test_unavailable_disabled_insufficient_and_fraction_threshold():
    before = snapshot("topic", [member()], "first")
    _, _, insights = calculation([member(review_state="unavailable")], before)
    assert next(i for i in insights if i.insight_kind == "REVIEW_COVERAGE_CHANGE").state == "UNAVAILABLE"
    _, _, insights = calculation([], before)
    assert all(i.state == "INSUFFICIENT_DATA" for i in insights)
    _, _, insights = calculation([member()], before, enabled_metric_ids=["corpus.paper_count"])
    assert next(i for i in insights if i.insight_kind == "REVIEW_COVERAGE_CHANGE").reason_code == "METRIC_DISABLED"
    before = snapshot(
        "topic",
        [
            member(),
            member(canonical_paper_id="b", paper_version_id="b"),
            member(canonical_paper_id="c", paper_version_id="c"),
        ],
        "first",
    )
    members = [
        replace(m, review_state="abstract_reviewed") if m.canonical_paper_id == "paper-a" else m for m in before.members
    ]
    _, _, insights = calculation(members, before, minimum_percentage_point_delta_basis_points=3334)
    review = next(i for i in insights if i.insight_kind == "REVIEW_COVERAGE_CHANGE")
    assert review.state == "BELOW_POLICY_THRESHOLD" and review.percentage_point_delta_basis_points == 3333


@pytest.fixture
def store(tmp_path, monkeypatch):
    from app.db import bootstrap

    monkeypatch.setenv("XRML_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "runs")
    return repo


def test_policy_persistence_binding_immutable_concurrency(store):
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: store.create_policy("topic", {}), range(6)))
    assert sum(created for _, created in rows) == 1
    p = rows[0][0]
    assert store.policies("topic")["active_policy_id"] is None
    store.activate("topic", p.policy_id)
    assert store.policies("topic")["active_policy_id"] == p.policy_id
    newer, _ = store.create_policy("topic", {"minimum_paper_count": 2})
    store.activate("topic", newer.policy_id)
    assert len(store.policies("topic")["items"]) == 2
    with pytest.raises(ValueError, match="cross_topic"):
        store.activate("other", p.policy_id)
    from app.db.bootstrap import connect_metadata_index

    with connect_metadata_index() as conn:
        for sql in ["UPDATE analysis_policies SET minimum_paper_count=3", "DELETE FROM analysis_policies"]:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)


def test_evaluation_manual_active_override_dedup_concurrent_rollback(store, monkeypatch):
    s, _ = memory.capture("topic", lambda _: [member()])
    p, _ = store.create_policy("topic", {})
    with pytest.raises(ValueError, match="no_active_policy"):
        store.evaluate("topic", s.snapshot_id)
    assert store.series("topic", d.METRIC_IDS[0])["items"] == []

    def fail(phase):
        if phase == "after_metrics":
            raise RuntimeError("injected")

    monkeypatch.setattr(store, "checkpoint", fail)
    with pytest.raises(RuntimeError):
        store.evaluate("topic", s.snapshot_id, p.policy_id)
    assert store.series("topic", d.METRIC_IDS[0])["items"] == []
    monkeypatch.setattr(store, "checkpoint", lambda _: None)
    store.activate("topic", p.policy_id)
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: store.evaluate("topic", s.snapshot_id), range(6)))
    assert sum(created for _, created in rows) == 1
    assert len({e.evaluation_id for e, _ in rows}) == 1
    assert len(store.insights("topic")["items"]) == 6
    with pytest.raises(ValueError, match="cross_topic"):
        store.evaluate("other", s.snapshot_id, p.policy_id)
    assert store.series("other", d.METRIC_IDS[0])["items"] == []
    new, _ = store.create_policy("topic", {"minimum_paper_count": 2})
    store.activate("topic", new.policy_id)
    assert store.evaluate("topic", s.snapshot_id, p.policy_id)[0] == rows[0][0]


def test_get_does_not_initialize_or_evaluate_and_pagination(store):
    from app.db.bootstrap import connect_metadata_index

    with connect_metadata_index() as c:
        before = c.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
    assert store.policies("topic")["items"] == []
    assert store.series("topic", d.METRIC_IDS[0])["items"] == []
    assert store.insights("topic")["items"] == []
    with connect_metadata_index(read_only=True) as c:
        assert c.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall() == before
    with pytest.raises(ValueError):
        store.series("topic", "invalid")
    with pytest.raises(ValueError):
        store.series("topic", d.METRIC_IDS[0], limit=101)


def test_api_strict_status_routes_and_no_execution(store, monkeypatch):
    from app.db.bootstrap import connect_metadata_index

    with connect_metadata_index():
        pass
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.temporal_analysis import router
    from app.auth.security import require_product_user
    from app.services import topic_profiles

    monkeypatch.setattr(topic_profiles, "get_topic_profile", lambda *args, **kw: {"profile_id": args[0]})
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_product_user] = lambda: None
    c = TestClient(app)
    root = "/api/topics/topic/analysis"
    assert c.get(root + "/policies").json()["items"] == []
    assert c.post(root + "/policies", json={"formula": "print(1)"}).status_code == 422
    new = c.post(root + "/policies", json={})
    assert new.status_code == 201
    pid = new.json()["policy"]["policy_id"]
    assert c.post(root + "/policies", json={}).status_code == 200
    # Creating a policy does not create M3-A snapshot tables. The first-use
    # dashboard must still expose truthful empty analysis resources, without DDL.
    with connect_metadata_index(read_only=True) as connection:
        schema_before = connection.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
    for suffix in ("/series?metric_id=corpus.paper_count", "/insights"):
        empty = c.get(root + suffix)
        assert empty.status_code == 200
        assert empty.json()["items"] == [] and empty.json()["total"] == 0
    with connect_metadata_index(read_only=True) as connection:
        assert connection.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall() == schema_before
    s, _ = memory.capture("topic", lambda _: [member()])
    assert c.post(root + "/evaluations", json={"snapshot_id": s.snapshot_id}).status_code == 409
    assert c.put(root + "/policies/active", json={"policy_id": pid}).status_code == 200
    assert (
        c.post(
            "/api/topics/other/analysis/evaluations", json={"snapshot_id": s.snapshot_id, "policy_id": pid}
        ).status_code
        == 422
    )
    result = c.post(root + "/evaluations", json={"snapshot_id": s.snapshot_id})
    assert result.status_code == 201
    assert c.post(root + "/evaluations", json={"snapshot_id": s.snapshot_id}).status_code == 200
    assert (
        c.get(root + "/series", params={"metric_id": "corpus.paper_count", "limit": 1}).json()["items"][0][
            "count_value"
        ]
        == 1
    )
    assert len(c.get(root + "/insights").json()["items"]) == 6
    assert c.get(root + "/series", params={"metric_id": "custom"}).status_code == 422
    assert c.get(root + "/series", params={"metric_id": "corpus.paper_count", "limit": 101}).status_code == 422
    assert c.delete(root + "/policies/" + pid).status_code in (404, 405)
    assert c.put(root + "/policies/" + pid, json={}).status_code in (404, 405)
    second, _ = memory.capture("topic", lambda _: [member(collection_ids=("saved",))])
    assert c.post(root + "/evaluations", json={"snapshot_id": second.snapshot_id}).status_code == 201
    # Filters apply before pagination and retain topic isolation, without recomputation.
    for suffix, extra, total in [("/series", {"metric_id": "corpus.paper_count"}, 1), ("/insights", {}, 6)]:
        response = c.get(root + suffix, params={**extra, "snapshot_id": s.snapshot_id, "limit": 1})
        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == total and len(payload["items"]) == 1
        assert payload["items"][0]["source_snapshot_id"] == s.snapshot_id
        assert c.get("/api/topics/other/analysis" + suffix, params={**extra, "snapshot_id": s.snapshot_id}).json()["total"] == 0
        assert c.get(root + suffix, params={**extra, "snapshot_id": "missing"}).json()["total"] == 0

    # Activation elsewhere cannot change an explicitly requested immutable policy.
    alternative, _ = store.create_policy("topic", {"minimum_paper_count": 99})
    store.activate("topic", alternative.policy_id)
    repeated = c.post(root + "/evaluations", json={"snapshot_id": s.snapshot_id, "policy_id": pid})
    assert repeated.status_code == 200
    assert repeated.json()["evaluation"]["policy_id"] == pid


def test_evaluation_immutable_schema_guards_and_all_rollback_phases(store, monkeypatch):
    from app.db.bootstrap import connect_metadata_index

    s, _ = memory.capture("topic", lambda _: [member()])
    p, _ = store.create_policy("topic", {})
    store.activate("topic", p.policy_id)
    for phase in ["after_evaluation", "after_metrics", "after_insights"]:

        def fail(current):
            if current == phase:
                raise RuntimeError(phase)

        monkeypatch.setattr(store, "checkpoint", fail)
        with pytest.raises(RuntimeError):
            store.evaluate("topic", s.snapshot_id)
        assert store.insights("topic")["items"] == []
    monkeypatch.setattr(store, "checkpoint", lambda _: None)
    store.evaluate("topic", s.snapshot_id)
    with connect_metadata_index() as c:
        for table in ["analysis_evaluations", "temporal_metric_points", "temporal_insight_records"]:
            with pytest.raises(sqlite3.IntegrityError):
                c.execute("DELETE FROM " + table)
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE topic_analysis_policy_bindings SET topic_id='other'")


def test_series_stable_pagination_and_policy_history(store):
    p, _ = store.create_policy("topic", {})
    store.activate("topic", p.policy_id)
    for n in range(3):
        s, _ = memory.capture("topic", lambda _, n=n: [member(collection_ids=(str(n),))])
        store.evaluate("topic", s.snapshot_id)
    all_rows = store.series("topic", "corpus.paper_count")["items"]
    pages = [store.series("topic", "corpus.paper_count", limit=1, page=i)["items"][0] for i in range(1, 4)]
    assert pages == all_rows
    assert [x["snapshot_created_at"] for x in all_rows] == sorted(x["snapshot_created_at"] for x in all_rows)


def test_all_six_accepted_templates_and_incomplete_provenance():
    a = member(rights_access_state="unknown")
    before = snapshot("topic", [a], "first")
    new = member(
        paper_version_id="version-next",
        review_state="full_text_reviewed",
        full_text_state="reviewed",
        full_text_content_digest="a" * 64,
        evidence_ids=("ev",),
        evidence_digests=(("ev", "b" * 64),),
        rights_access_state="open_access",
    )
    _, _, records = calculation([new, member(canonical_paper_id="added", paper_version_id="added")], before)
    assert next(i for i in records if i.insight_kind == "CORPUS_SIZE_CHANGE").absolute_delta == 1
    assert next(i for i in records if i.insight_kind == "PAPER_VERSION_ACTIVITY").state == "ACCEPTED"
    assert next(i for i in records if i.insight_kind == "RIGHTS_KNOWN_COVERAGE_CHANGE").state == "UNAVAILABLE"
    # Rights-known coverage is 100% when exact, unavailable when any paper is unknown.
    # A zero configured threshold accepts exact no-change without inventing a trend.
    before = snapshot("topic", [member()], "first")
    _, _, records = calculation(
        [new], before, minimum_percentage_point_delta_basis_points=0, minimum_absolute_count_delta=0
    )
    assert all(i.state == "ACCEPTED" for i in records)
    current = snapshot("topic", [new], "second", before.snapshot_id)
    incomplete = replace(current, members=(replace(current.members[0], membership_digest=""),))
    _, _, records = d.evaluate(policy(minimum_percentage_point_delta_basis_points=0), incomplete, before, "clock")
    assert not any(i.state == "ACCEPTED" for i in records)


def test_policy_nfc_and_invalid_canonical_bytes():
    assert d.policy("e\u0301", {}, "a").policy_id == d.policy("é", {}, "b").policy_id
    with pytest.raises(UnicodeError):
        d.policy("\ud800", {}, "clock")


def test_snapshot_policy_cross_topic_and_schema(store):
    p, _ = store.create_policy("other", {})
    s, _ = memory.capture("topic", lambda _: [member()])
    with pytest.raises(ValueError, match="cross_topic_policy"):
        store.evaluate("topic", s.snapshot_id, p.policy_id)
    with pytest.raises(ValueError, match="unsupported_snapshot_schema"):
        d.evaluate(policy(), replace(s, schema_version=2), None, "clock")


def test_policy_activation_preserves_all_historical_bytes(store):
    from app.db.bootstrap import connect_metadata_index

    p, _ = store.create_policy("topic", {})
    s, _ = memory.capture("topic", lambda _: [member()])
    store.evaluate("topic", s.snapshot_id, p.policy_id)
    tables = ("analysis_policies", "analysis_evaluations", "temporal_metric_points", "temporal_insight_records")
    with connect_metadata_index(read_only=True) as c:
        before = {t: [tuple(r) for r in c.execute("SELECT * FROM " + t)] for t in tables}
    store.activate("topic", p.policy_id)
    with connect_metadata_index(read_only=True) as c:
        assert before == {t: [tuple(r) for r in c.execute("SELECT * FROM " + t)] for t in tables}


def test_historical_values_survive_missing_or_changed_current_destinations(store, monkeypatch):
    from app.domain.temporal_memory import digest
    from app.services import temporal_analysis as service, paper_bank

    span = {"evidence_span_id": "ev", "content_sha256": "a" * 64, "locator": {"page": 1}}
    captured = member(
        evidence_ids=("ev",),
        evidence_digests=(("ev", digest({"content_digest": span["content_sha256"], "locator": span["locator"]})),),
    )
    s, _ = memory.capture("topic", lambda _: [captured])
    p, _ = store.create_policy("topic", {})
    store.evaluate("topic", s.snapshot_id, p.policy_id)
    review = {"run_id": "run", "evidence_spans": [span]}
    monkeypatch.setattr(
        paper_bank,
        "_load_catalog",
        lambda *args, **kw: ([{"paperVersionId": "version-a", "_selectedReview": review}], False, {}, None),
    )
    first = service.insights("topic", 25, 1)["items"]
    assert all(item["evidence_destinations"][0]["id"] == "ev" for item in first)
    review["evidence_spans"][0] = {**span, "content_sha256": "b" * 64}
    changed = service.insights("topic", 25, 1)["items"]
    assert all(not item["evidence_destinations"] for item in changed)
    assert all(item["unavailable_evidence_ids"] == ["ev"] for item in changed)
    assert all(item["unavailable_evidence_ids"] == [] for item in first)
    assert [i["insight_record_id"] for i in first] == [i["insight_record_id"] for i in changed]
    monkeypatch.setattr(paper_bank, "_load_catalog", lambda *args, **kw: ([], False, {}, None))
    missing = service.insights("topic", 25, 1)["items"]
    assert all(i["destination_reason"] == "NO_CURRENT_DESTINATION" for i in missing)
    assert [i["rendered_factual_text"] for i in first] == [i["rendered_factual_text"] for i in missing]


@pytest.mark.parametrize(
    "error,status,code",
    [
        (KeyError("policy_not_found"), 404, "policy_not_found"),
        (KeyError("snapshot_not_found"), 404, "snapshot_not_found"),
        (ValueError("cross_topic_policy"), 422, "cross_topic_policy"),
        (ValueError("unsupported_policy_schema"), 422, "unsupported_policy_schema"),
        (ValueError("untrusted local path"), 422, "analysis_canonicalization_failed"),
        (OSError("private path"), 503, "analysis_repository_unavailable"),
        (sqlite3.OperationalError("private SQL"), 503, "analysis_transaction_failed"),
        (sqlite3.IntegrityError("policy_binding_conflict"), 409, "policy_binding_conflict"),
    ],
)
def test_typed_failures_hide_internal_details(monkeypatch, error, status, code):
    from fastapi import HTTPException
    from app.api.temporal_analysis import invoke
    from app.auth.resource_access import ResourceAction
    from app.services import temporal_analysis as service

    monkeypatch.setattr(service, "authorize", lambda *args, action: None)

    def fail():
        raise error

    with pytest.raises(HTTPException) as caught:
        invoke("topic", None, fail, action=ResourceAction.READ)
    assert caught.value.status_code == status and caught.value.detail == code
