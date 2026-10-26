from __future__ import annotations

import json

from app.db import bootstrap
from app.schemas.runs import RunCreateRequest
from app.services import profile as run_profile_service, retrieval, source_config, topic_profiles


def _patch_profile_storage(isolated_run_repo, monkeypatch) -> None:
    profile_dir = isolated_run_repo["storage_root"].parent / "topic_profiles"
    profile_path = profile_dir / "profiles.json"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_dir)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_path)


def test_topic_profile_lifecycle_persists_favorite_and_archive_state(client, isolated_run_repo, monkeypatch) -> None:
    _patch_profile_storage(isolated_run_repo, monkeypatch)

    create_response = client.post(
        "/topic-profiles",
        json={
            "title": "Graph Neural Networks",
            "description": "그래프 학습 동향",
            "topic": "Graph Neural Networks",
            "queries": ["graph neural networks", "gnn"],
            "categories": ["cs.LG"],
            "max_results": 40,
            "top_papers": 4,
            "weekly_top": 10,
            "default_collection_cadence": "daily_1800",
            "default_report_cadence": "monthly_day_1_1000",
        },
    )

    assert create_response.status_code == 200, create_response.text
    created_profile = next(
        item for item in create_response.json()["items"] if item["profile_id"] == "graph-neural-networks"
    )
    assert created_profile["is_favorite"] is False
    assert created_profile["status"] == "active"
    assert created_profile["default_collection_cadence"] == "daily_1800"
    assert created_profile["default_report_cadence"] == "monthly_day_1_1000"
    assert created_profile["source_provider"] == "arxiv"
    assert created_profile["arxiv_sort_by"] == "submittedDate"
    assert created_profile["arxiv_sort_order"] == "descending"
    assert created_profile["source_settings"]["defaultProvider"] == "arxiv"
    assert created_profile["source_settings"]["providers"]["arxiv"]["queries"] == ["graph neural networks", "gnn"]
    assert created_profile["source_settings"]["providers"]["arxiv"]["categories"] == ["cs.LG"]
    assert created_profile["source_statuses"][0]["provider"] == "arxiv"
    assert created_profile["source_statuses"][0]["available"] is True

    favorite_response = client.patch(
        "/topic-profiles/graph-neural-networks",
        json={"is_favorite": True},
    )
    assert favorite_response.status_code == 200, favorite_response.text
    favorited = next(item for item in favorite_response.json()["items"] if item["profile_id"] == "graph-neural-networks")
    assert favorited["is_favorite"] is True

    archive_response = client.patch(
        "/topic-profiles/graph-neural-networks",
        json={"status": "archived"},
    )
    assert archive_response.status_code == 200, archive_response.text
    archived = next(item for item in archive_response.json()["items"] if item["profile_id"] == "graph-neural-networks")
    assert archived["status"] == "archived"
    assert archived["archived_at"]

    api_topics_response = client.get("/api/topics")
    assert api_topics_response.status_code == 200, api_topics_response.text
    assert "graph-neural-networks" not in {item["id"] for item in api_topics_response.json()["items"]}

    restore_response = client.patch(
        "/topic-profiles/graph-neural-networks",
        json={"status": "active"},
    )
    assert restore_response.status_code == 200, restore_response.text
    restored = next(item for item in restore_response.json()["items"] if item["profile_id"] == "graph-neural-networks")
    assert restored["status"] == "active"
    assert restored["archived_at"] is None

    restored_topics_response = client.get("/api/topics")
    assert restored_topics_response.status_code == 200, restored_topics_response.text
    restored_topic = next(item for item in restored_topics_response.json()["items"] if item["id"] == "graph-neural-networks")
    assert restored_topic["isFavorite"] is True
    assert restored_topic["status"] == "active"

    persisted_profiles = json.loads((isolated_run_repo["storage_root"].parent / "topic_profiles" / "profiles.json").read_text(encoding="utf-8"))
    persisted = next(item for item in persisted_profiles if item["profile_id"] == "graph-neural-networks")
    assert persisted["is_favorite"] is True
    assert persisted["status"] == "active"
    assert persisted["default_collection_cadence"] == "daily_1800"
    assert persisted["default_report_cadence"] == "monthly_day_1_1000"
    assert persisted["source_provider"] == "arxiv"
    assert persisted["arxiv_sort_by"] == "submittedDate"
    assert persisted["arxiv_sort_order"] == "descending"
    assert persisted["source_settings"]["providers"]["arxiv"]["maxResults"] == 40


def test_topic_source_settings_persist_and_drive_run_profile(client, isolated_run_repo, monkeypatch) -> None:
    _patch_profile_storage(isolated_run_repo, monkeypatch)

    create_response = client.post(
        "/topic-profiles",
        json={
            "title": "Source Config Topic",
            "topic": "Source Config Topic",
            "queries": ["fallback query"],
            "categories": ["cs.AI"],
            "max_results": 20,
            "source_settings": {
                "defaultProvider": "arxiv",
                "enabledProviders": ["arxiv"],
                "providers": {
                    "arxiv": {
                        "provider": "arxiv",
                        "enabled": True,
                        "priority": 3,
                        "queries": ["source configured query"],
                        "categories": ["cs.IR"],
                        "maxResults": 25,
                        "sortBy": "relevance",
                        "sortOrder": "ascending",
                    }
                },
            },
            "top_papers": 3,
            "weekly_top": 10,
        },
    )

    assert create_response.status_code == 200, create_response.text
    created = next(item for item in create_response.json()["items"] if item["profile_id"] == "source-config-topic")
    assert created["queries"] == ["source configured query"]
    assert created["categories"] == ["cs.IR"]
    assert created["max_results"] == 25
    assert created["arxiv_sort_by"] == "relevance"
    assert created["arxiv_sort_order"] == "ascending"
    assert created["source_settings"]["providers"]["arxiv"]["priority"] == 3

    run_payload = topic_profiles.to_run_request_payload(created)
    request = RunCreateRequest.model_validate(run_payload)
    run_profile = run_profile_service.build_run_profile(request.model_dump())
    arxiv_config = run_profile["sources"]["arxiv"]

    assert request.source_settings["providers"]["arxiv"]["queries"] == ["source configured query"]
    assert arxiv_config["queries"] == ["source configured query"]
    assert arxiv_config["categories"] == ["cs.IR"]
    assert arxiv_config["max_results"] == 25
    assert arxiv_config["sort_by"] == "relevance"
    assert arxiv_config["sort_order"] == "ascending"
    assert arxiv_config["priority"] == 3

    patch_response = client.patch(
        "/topic-profiles/source-config-topic",
        json={
            "source_settings": {
                "defaultProvider": "arxiv",
                "enabledProviders": ["arxiv"],
                "providers": {
                    "arxiv": {
                        "provider": "arxiv",
                        "enabled": True,
                        "priority": 1,
                        "queries": ["updated query"],
                        "categories": ["cs.CL"],
                        "maxResults": 15,
                        "sortBy": "submittedDate",
                        "sortOrder": "descending",
                    }
                },
            }
        },
    )
    assert patch_response.status_code == 200, patch_response.text
    updated = next(item for item in patch_response.json()["items"] if item["profile_id"] == "source-config-topic")
    assert updated["queries"] == ["updated query"]
    assert updated["source_settings"]["providers"]["arxiv"]["categories"] == ["cs.CL"]

    invalid_response = client.patch(
        "/topic-profiles/source-config-topic",
        json={
            "source_settings": {
                "defaultProvider": "arxiv",
                "enabledProviders": ["arxiv"],
                "providers": {
                    "arxiv": {
                        "provider": "arxiv",
                        "enabled": False,
                        "priority": 1,
                        "queries": ["disabled"],
                        "categories": ["cs.AI"],
                        "maxResults": 10,
                        "sortBy": "submittedDate",
                        "sortOrder": "descending",
                    }
                },
            }
        },
    )
    assert invalid_response.status_code == 400, invalid_response.text


def test_topic_source_settings_support_openalex_provider(client, isolated_run_repo, monkeypatch) -> None:
    _patch_profile_storage(isolated_run_repo, monkeypatch)

    create_response = client.post(
        "/topic-profiles",
        json={
            "title": "OpenAlex Source Topic",
            "topic": "OpenAlex Source Topic",
            "queries": ["fallback"],
            "categories": ["cs.AI"],
            "max_results": 20,
            "source_settings": {
                "defaultProvider": "openalex",
                "enabledProviders": ["openalex"],
                "providers": {
                    "arxiv": {
                        "provider": "arxiv",
                        "enabled": False,
                        "priority": 2,
                        "queries": ["arxiv disabled"],
                        "categories": ["cs.AI"],
                        "maxResults": 10,
                        "sortBy": "submittedDate",
                        "sortOrder": "descending",
                    },
                    "openalex": {
                        "provider": "openalex",
                        "enabled": True,
                        "priority": 1,
                        "queries": ["openalex graph neural networks"],
                        "maxResults": 7,
                        "sortBy": "publication_date",
                        "sortOrder": "descending",
                    },
                },
            },
            "top_papers": 3,
            "weekly_top": 10,
        },
    )

    assert create_response.status_code == 200, create_response.text
    created = next(item for item in create_response.json()["items"] if item["profile_id"] == "openalex-source-topic")
    assert created["source_provider"] == "openalex"
    assert created["queries"] == ["openalex graph neural networks"]
    assert created["categories"] == []
    assert created["max_results"] == 7
    assert created["source_settings"]["enabledProviders"] == ["openalex"]
    assert created["source_settings"]["providers"]["openalex"]["sortBy"] == "publication_date"
    assert [status["provider"] for status in created["source_statuses"]] == ["arxiv", "openalex", "crossref"]
    assert next(status for status in created["source_statuses"] if status["provider"] == "openalex")["available"] is True
    assert next(status for status in created["source_statuses"] if status["provider"] == "crossref")["state"] == "available"

    run_payload = topic_profiles.to_run_request_payload(created)
    request = RunCreateRequest.model_validate(run_payload)
    run_profile = run_profile_service.build_run_profile(request.model_dump())

    assert request.source_provider == "openalex"
    assert request.source_settings["defaultProvider"] == "openalex"
    assert "arxiv" not in run_profile["sources"]
    assert run_profile["sources"]["openalex"]["queries"] == ["openalex graph neural networks"]
    assert run_profile["sources"]["openalex"]["max_results"] == 7
    assert run_profile["sources"]["openalex"]["sort_by"] == "publication_date"


def test_source_provider_registry_exposes_real_provider_states(client) -> None:
    response = client.get("/api/source-providers")

    assert response.status_code == 200, response.text
    payload = response.json()
    provider_ids = {item["provider"] for item in payload["items"]}
    assert {"arxiv", "openalex", "crossref"}.issubset(provider_ids)
    assert payload["checkedAt"]

    arxiv = next(item for item in payload["items"] if item["provider"] == "arxiv")
    assert arxiv["available"] is True
    assert arxiv["configured"] is True
    assert arxiv["state"] == "available"
    assert arxiv["capabilities"]["papers"] is True
    assert "queries" in arxiv["supportedQueryFields"]

    openalex = next(item for item in payload["items"] if item["provider"] == "openalex")
    assert openalex["available"] is True
    assert openalex["configured"] is True
    assert openalex["capabilities"]["citations"] is True
    assert openalex["capabilities"]["institutions"] is True

    crossref = next(item for item in payload["items"] if item["provider"] == "crossref")
    assert crossref["available"] is True
    assert crossref["configured"] is True
    assert crossref["state"] == "available"
    assert crossref["requiresCredentials"] is False
    assert crossref["capabilities"]["papers"] is True
    assert crossref["capabilities"]["citations"] is True
    assert crossref["unavailableReason"] is None


def test_crossref_source_provider_can_be_enabled() -> None:
    settings = source_config.normalize_source_settings(
        {
            "defaultProvider": "crossref",
            "enabledProviders": ["crossref"],
            "providers": {
                "crossref": {
                    "provider": "crossref",
                    "enabled": True,
                    "priority": 1,
                    "queries": ["research agents"],
                    "maxResults": 10,
                    "sortBy": "relevance",
                    "sortOrder": "descending",
                }
            },
        }
    )

    assert settings["providers"]["crossref"]["enabled"] is True
    assert settings["enabledProviders"] == ["crossref"]
    assert settings["defaultProvider"] == "crossref"
    source_config.validate_source_settings(settings)


def test_unavailable_source_provider_payload_is_rejected(client, isolated_run_repo, monkeypatch) -> None:
    _patch_profile_storage(isolated_run_repo, monkeypatch)

    create_response = client.post(
        "/topic-profiles",
        json={
            "title": "Unsupported Provider Topic",
            "topic": "Unsupported Provider Topic",
            "queries": ["unsupported provider"],
            "source_settings": {
                "defaultProvider": "semantic_scholar",
                "enabledProviders": ["semantic_scholar"],
                "providers": {
                    "semantic_scholar": {
                        "provider": "semantic_scholar",
                        "enabled": True,
                        "priority": 1,
                        "queries": ["unsupported provider"],
                        "maxResults": 10,
                        "sortBy": "relevance",
                        "sortOrder": "descending",
                    }
                },
            },
            "top_papers": 3,
            "weekly_top": 10,
        },
    )

    assert create_response.status_code == 400, create_response.text
    assert "Unsupported source provider" in create_response.json()["detail"]


def test_flat_openalex_source_provider_normalizes_to_enabled_openalex(client, isolated_run_repo, monkeypatch) -> None:
    _patch_profile_storage(isolated_run_repo, monkeypatch)

    create_response = client.post(
        "/topic-profiles",
        json={
            "title": "Flat OpenAlex Topic",
            "topic": "Flat OpenAlex Topic",
            "queries": ["flat openalex query"],
            "source_provider": "openalex",
            "max_results": 5,
            "top_papers": 3,
            "weekly_top": 10,
        },
    )

    assert create_response.status_code == 200, create_response.text
    created = next(item for item in create_response.json()["items"] if item["profile_id"] == "flat-openalex-topic")
    assert created["source_provider"] == "openalex"
    assert created["source_settings"]["defaultProvider"] == "openalex"
    assert created["source_settings"]["enabledProviders"] == ["openalex"]
    assert created["source_settings"]["providers"]["arxiv"]["enabled"] is False
    assert created["source_settings"]["providers"]["openalex"]["queries"] == ["flat openalex query"]
    assert created["source_settings"]["providers"]["openalex"]["maxResults"] == 5


def test_openalex_retrieval_uses_real_provider_shape(tmp_path, monkeypatch) -> None:
    requested_urls: list[str] = []

    def fake_fetch_json(url: str, timeout: int = 30) -> dict:
        requested_urls.append(url)
        return {
            "results": [
                {
                    "id": "https://openalex.org/W123",
                    "doi": "https://doi.org/10.1234/example",
                    "display_name": "OpenAlex Provider Integration for Research Agents",
                    "abstract_inverted_index": {"research": [0], "agents": [1]},
                    "publication_date": "2026-04-01",
                    "updated_date": "2026-04-02T00:00:00",
                    "cited_by_count": 42,
                    "primary_location": {
                        "landing_page_url": "https://example.org/openalex-paper",
                        "pdf_url": "https://example.org/openalex-paper.pdf",
                    },
                    "authorships": [
                        {
                            "author": {"display_name": "Kim Researcher"},
                            "institutions": [{"display_name": "KAIST"}],
                        }
                    ],
                    "concepts": [{"display_name": "Artificial intelligence"}],
                    "type": "article",
                    "publication_year": 2026,
                }
            ]
        }

    monkeypatch.setattr(retrieval, "fetch_json", fake_fetch_json)
    profile = {
        "sources": {
            "openalex": {
                "queries": ["research agents"],
                "max_results": 3,
                "sort_by": "publication_date",
                "sort_order": "descending",
                "priority": 1,
            }
        },
        "keywords": {"primary": ["research"], "secondary": ["agents"]},
    }

    result = retrieval.retrieve_from_profile(
        profile=profile,
        raw_path=tmp_path / "sources.jsonl",
        inbox_path=tmp_path / "inbox.jsonl",
        sleep_seconds=0,
        mirror_legacy=False,
    )

    assert result["status"] == "ok"
    assert result["new_papers"] == 1
    assert result["providers_run"] == {"openalex": 1}
    assert "search=research+agents" in requested_urls[0]
    entries = [json.loads(line) for line in (tmp_path / "inbox.jsonl").read_text(encoding="utf-8").splitlines()]
    assert entries[0]["source_provider"] == "openalex"
    assert entries[0]["paper_id"].startswith("openalex:")
    assert entries[0]["citations"] == 42
    assert entries[0]["institutions"] == ["KAIST"]


def test_seeded_default_profiles_expose_source_statuses_before_any_write(client, isolated_run_repo, monkeypatch) -> None:
    """A fresh install must answer like a written one.

    Seeded defaults used to be returned un-normalized, so `source_statuses` was
    absent until some unrelated mutation happened to persist a normalized
    library. The topic settings screen renders provider availability from that
    field, so on first run it listed providers with no availability state and
    then silently gained it after the first edit.
    """
    _patch_profile_storage(isolated_run_repo, monkeypatch)
    # Force the seed branch: the fixture may already have a stored library, and
    # stored profiles were always normalized, so only an empty one exercises this.
    topic_profiles.PROFILE_LIBRARY_PATH.unlink(missing_ok=True)
    assert not topic_profiles.PROFILE_LIBRARY_PATH.exists()

    first = client.get("/topic-profiles")
    assert first.status_code == 200
    seeded = first.json()["items"]
    assert seeded, "a fresh library must still seed default profiles"

    expected_providers = set(source_config.REGISTRY_PROVIDER_IDS)
    for profile in seeded:
        statuses = profile["source_statuses"]
        assert {status["provider"] for status in statuses} == expected_providers, profile["profile_id"]
        assert all(status["statusLabel"] for status in statuses), profile["profile_id"]

    # A later write must not change the derived field either: the pre-write and
    # post-write answers agree, which is the invariant that was broken.
    created = client.post(
        "/topic-profiles",
        json={"title": "Post Write Probe", "topic": "probe", "queries": ["probe"], "categories": ["cs.CV"]},
    )
    assert created.status_code == 200
    after_write = {item["profile_id"]: item for item in created.json()["items"]}
    for profile in seeded:
        assert after_write[profile["profile_id"]]["source_statuses"] == profile["source_statuses"]
