"""Existing provider -> immutable input -> authenticated Paper Bank readback."""
from copy import deepcopy

from fastapi.testclient import TestClient
import pytest

from app.auth.resource_access import ResourceActor, resource_actor_for_user
from app.auth.security import require_product_user
from app.db import bootstrap
from app.repositories import ai_review_drafts, paper_reviews
from app.services import ai_provider, ai_review, paper_bank
from tests.test_ai_review_provider import _review
from tests import test_temporal_memory

seeded = test_temporal_memory.seeded


def _packet(source, max_chars):
    return ai_review._packet_from_review(source, topic_id=source["topic_id"], topic_name="Fixture topic", max_chars=max_chars)


def test_provider_preserves_packet_before_source_changes_and_does_not_skip_different_input(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    source = _review(1)
    paper_reviews.upsert_review(source)
    original_build = ai_review.build_ai_review_packets
    packets = original_build(source["topic_id"], limit=1, max_chars=100)
    monkeypatch.setattr(ai_review, "build_ai_review_packets", lambda *a, **kw: deepcopy(packets))
    adapter = ai_provider._provider_for_name("stub")
    original_generate = adapter.generate

    def generate(request):
        # Current review changes while the response is in flight. Binding must
        # come from the packet, never the latest mutable review lookup.
        paper_reviews.upsert_review({**source, "run_id": "later-run", "reviewed_at": "2099-01-01"})
        response = original_generate(request)
        request.packet["abstract"] = "adapter mutation must not rewrite archived input"
        return response

    monkeypatch.setattr(adapter, "generate", generate)
    monkeypatch.setattr(ai_provider, "_provider_for_name", lambda _: adapter)
    options = dict(provider="stub", mode="stub", limit=1, import_provider_drafts=True,
                   output_root=tmp_path / "output")
    result = ai_provider.run_ai_review_provider(source["topic_id"], **options)
    assert result["stored_draft_count"] == 1
    history = ai_review_drafts.list_input_history(topic_id=source["topic_id"], paper_ids=[source["paper_id"]])
    assert len(history) == 1
    assert history[0]["packet"] == packets["packets"][0]
    assert history[0]["run_id"] == source["run_id"]
    assert history[0]["input_origin"] == "FIXTURE_SYNTHETIC"
    assert ai_provider.run_ai_review_provider(source["topic_id"], **options)["invoked_count"] == 0
    packets["packets"] = [_packet(source, 200)]
    assert ai_provider.run_ai_review_provider(source["topic_id"], **options)["stored_draft_count"] == 1
    history = ai_review_drafts.list_input_history(topic_id=source["topic_id"], paper_ids=[source["paper_id"]])
    assert len(history) == 2
    assert {h["packet"]["selected_text_excerpts"][0]["char_count"] for h in history} == {100, 200}


def _seed_receipt(topic, run, paper, *, abstract="Synthetic preserved input"):
    source = _review(1, topic_id=topic, run_id=run, paper_id=paper, abstract_text=abstract)
    packet = _packet(source, 120)
    draft = ai_review.generate_fixture_ai_review_draft(packet)
    response = ai_provider.ProviderResponse(status="generated", provider_name="stub", paper_id=paper,
                                            run_id="input-history-fixture", parsed_draft=draft)
    response = ai_provider._validate_response(packet, response)
    stored = ai_provider._store_accepted_draft(packet, response, provider_mode="stub", provider_origin="stub")
    return stored["validation"]["input_receipt_id"], packet


def test_paper_bank_input_history_readback_paging_restart_and_workspace_authorization(seeded, monkeypatch):
    from app.main import create_app
    from app.api import paper_bank as api

    topic = seeded.TOPIC_A_ID
    bank = paper_bank.list_topic_papers(topic, page_size=100)
    item = bank["items"][0]
    occurrence = item["compatibility"]["occurrences"][0]
    run, paper = occurrence["runId"], occurrence["paperId"]
    first_id, first_packet = _seed_receipt(topic, run, paper)
    second_id, _ = _seed_receipt(topic, run, paper, abstract="Second distinct preserved input")
    hidden_id, _ = _seed_receipt(topic, "different-unrelated-run", paper)
    root = f"/api/topics/{topic}/paper-bank/{item['paperVersionId']}/review-inputs"

    def client():
        app = create_app()
        app.dependency_overrides[require_product_user] = lambda: None
        return TestClient(app)

    with client() as http:
        response = http.get(root, params={"pageSize": 1})
        assert response.status_code == 200, response.text
        first_page = response.json()
        assert first_page["hasMore"] is True
        second_page = http.get(root, params={"pageSize": 1, "page": 2}).json()
        assert second_page["hasMore"] is False
        assert {first_page["items"][0]["receipt_id"], second_page["items"][0]["receipt_id"]} == {first_id, second_id}
        assert "packet" not in first_page["items"][0]
        assert http.get(root + "/" + hidden_id).status_code == 404
        assert http.get(root + "/missing").status_code == 404
        assert http.get(root, params={"page": 0}).status_code == 422
        assert http.get(root.replace(topic, seeded.TOPIC_B_ID)).status_code == 404
        assert http.get(root + "/" + first_id).json()["packet"] == first_packet
    with client() as http:
        assert http.get(root + "/" + first_id).json()["packet"] == first_packet
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("local-internal-user"))
        assert http.get(root + "/" + first_id).json()["packet"] == first_packet
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("outsider"))
        assert http.get(root).status_code == 404
        assert http.get(root + "/" + first_id).status_code == 404
        # Public template READ never grants access to stored private inputs.
        profiles = api.review_input_history.topic_profiles
        monkeypatch.setattr(profiles, "SYSTEM_TEMPLATE_PROFILE_IDS", profiles.SYSTEM_TEMPLATE_PROFILE_IDS | {topic})
        assert http.get(root).status_code == 404
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("local-internal-user", is_admin=True))
        assert http.get(root + "/" + first_id).json()["packet"] == first_packet


def test_input_store_error_remains_visible(seeded, monkeypatch):
    from app.main import create_app
    import sqlite3

    item = paper_bank.list_topic_papers(seeded.TOPIC_A_ID, page_size=100)["items"][0]
    app = create_app()
    app.dependency_overrides[require_product_user] = lambda: None
    def broken(**kwargs):
        raise sqlite3.OperationalError("fixture read failure")
    monkeypatch.setattr(ai_review_drafts, "list_input_history", broken)
    with TestClient(app) as http:
        result = http.get(f"/api/topics/{seeded.TOPIC_A_ID}/paper-bank/{item['paperVersionId']}/review-inputs")
        assert result.status_code == 503
        assert result.json()["detail"] == "review_input_store_unavailable"


@pytest.mark.parametrize("ambiguous", [False, True])
def test_history_reuses_unique_legacy_alias_binding_but_never_ambiguous_alias(tmp_path, monkeypatch, ambiguous):
    from app.services import review_input_history, topic_profiles
    from tests.test_m2_occurrence_authority import _fallback_record

    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    alias = "legacy-review-note.json"
    run = {"run_id": "alias-run", "request": {"source_provider": "unknown"}}
    discoveries = [(run, _fallback_record(occurrence_id="occ-" + "d" * 32, raw_metadata_ref=alias))]
    if ambiguous:
        discoveries.append((run, _fallback_record(occurrence_id="occ-" + "e" * 32, raw_metadata_ref=alias)))
    monkeypatch.setattr(topic_profiles, "get_topic_profile", lambda *a, **kw: {"topic": "topic"})
    monkeypatch.setattr(paper_bank, "_load_topic_discoveries", lambda *a, **kw: discoveries)
    receipt_id, _ = _seed_receipt("topic", run["run_id"], alias)
    actor = resource_actor_for_user(None)
    items = paper_bank.list_topic_papers("topic", actor=actor)["items"]
    for item in items:
        result = review_input_history.listing("topic", item["paperVersionId"], actor)
        assert [row["receipt_id"] for row in result["items"]] == ([] if ambiguous else [receipt_id])


def test_history_displays_stored_result_level_not_broader_input_level(tmp_path, monkeypatch):
    from app.services.review_input_history import _summary

    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "storage" / "runs")
    source = _review(1)
    packet = _packet(source, 120)
    draft = ai_review.generate_fixture_ai_review_draft(packet)
    draft["review_level"] = "abstract_only"
    response = ai_provider.ProviderResponse(status="generated", provider_name="stub", run_id="fixture",
                                            paper_id=source["paper_id"], parsed_draft=draft)
    response = ai_provider._validate_response(packet, response)
    assert response.validation_result["status"] == "accepted"
    saved = ai_provider._store_accepted_draft(packet, response, provider_mode="stub", provider_origin="stub")
    receipt = ai_review_drafts.get_input_receipt(source["topic_id"], saved["validation"]["input_receipt_id"])
    assert receipt["packet"]["review_level"] == "full_text_grounded"
    assert _summary(receipt)["review_level"] == "abstract_only"
