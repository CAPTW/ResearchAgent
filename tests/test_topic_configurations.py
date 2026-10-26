from __future__ import annotations

from copy import deepcopy

from app.services import topic_profiles, topic_schedules


def _isolate_topic_profiles(isolated_run_repo, monkeypatch):
    profile_dir = isolated_run_repo["storage_root"].parent / "topic_profiles"
    schedule_dir = isolated_run_repo["storage_root"].parent / "topic_schedules"
    monkeypatch.setattr(topic_profiles, "STORAGE_ROOT", isolated_run_repo["storage_root"])
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_dir)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_dir / "profiles.json")
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_DIR", schedule_dir)
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_PATH", schedule_dir / "schedules.json")
    monkeypatch.setattr(topic_schedules, "SCHEDULER_STATUS_PATH", schedule_dir / "worker_status.json")
    return profile_dir / "profiles.json"


def test_topic_configuration_validation_rejects_unsafe_fields_and_writes_nothing(client, isolated_run_repo, monkeypatch) -> None:
    profile_path = _isolate_topic_profiles(isolated_run_repo, monkeypatch)
    client.get("/topic-profiles")
    original_profiles = deepcopy(topic_profiles.list_topic_profiles()["items"])

    response = client.post(
        "/api/topics/large-language-models/configuration/validate",
        json={
            "topicId": "bad topic!",
            "displayName": "Large Language Models",
            "queryTerms": ["large language model"],
            "defaultAllowDomains": ["https://arxiv.org/pdf"],
            "defaultDownloadLimit": 3,
            "defaultMaxPdfBytes": 5_000_000,
            "providerModeDefault": "gemini",
            "apiKey": "should-not-be-accepted",
        },
    )
    body = response.json()

    assert response.status_code == 200, response.text
    assert body["validationStatus"] == "invalid"
    assert any("topic_id" in error for error in body["errors"])
    assert any("default_allow_domains" in error for error in body["errors"])
    assert any("provider_mode_default" in error for error in body["errors"])
    assert any("secret" in error.lower() or "credential" in error.lower() for error in body["errors"])
    assert body["savedPath"] is None
    assert topic_profiles.list_topic_profiles()["items"] == original_profiles
    assert profile_path.exists()


def test_topic_configuration_list_and_current_return_safe_defaults(client, isolated_run_repo, monkeypatch) -> None:
    _isolate_topic_profiles(isolated_run_repo, monkeypatch)
    client.get("/topic-profiles")

    list_response = client.get("/api/topic-configurations")
    current_response = client.get("/api/topics/large-language-models/configuration")

    assert list_response.status_code == 200, list_response.text
    assert current_response.status_code == 200, current_response.text
    list_body = list_response.json()
    current_body = current_response.json()
    topic_ids = {item["topicId"] for item in list_body["items"]}
    assert "large-language-models" in topic_ids
    assert current_body["validationStatus"] == "valid"
    config = current_body["topicConfiguration"]
    assert config["topicId"] == "large-language-models"
    assert config["displayName"] == "Large Language Models"
    assert config["defaultAllowDomains"] == ["arxiv.org"]
    assert config["defaultDownloadLimit"] == 5
    assert config["providerModeDefault"] == "none"
    assert config["liveActionsEnabled"] is False
    acquisition = next(plan for plan in current_body["operationsPlanner"]["runPlans"] if plan["planId"] == "full_text_acquisition_dry_run")
    assert acquisition["serverExecutesCommand"] is False
    assert "--dry-run" in acquisition["copyableCliCommand"]


def test_topic_configuration_preview_uses_draft_config_without_writing(client, isolated_run_repo, monkeypatch) -> None:
    _isolate_topic_profiles(isolated_run_repo, monkeypatch)
    client.get("/topic-profiles")
    original_profiles = deepcopy(topic_profiles.list_topic_profiles()["items"])

    response = client.post(
        "/api/topics/large-language-models/configuration/preview",
        json={
            "displayName": "LLM Reliability Monitor",
            "queryTerms": ["llm reliability", "rag evaluation"],
            "defaultUniqueLimit": 17,
            "defaultAllowDomains": ["arxiv.org"],
            "defaultDownloadLimit": 2,
            "defaultMaxPdfBytes": 6_000_000,
            "recommendationLimit": 4,
            "followupLimit": 6,
            "providerModeDefault": "stub",
        },
    )
    body = response.json()

    assert response.status_code == 200, response.text
    assert body["validationStatus"] == "valid"
    assert body["savedPath"] is None
    assert set(body["changedFields"]) >= {"displayName", "queryTerms", "defaultUniqueLimit", "defaultDownloadLimit"}
    planner = body["operationsPlanner"]
    config = planner["topicConfiguration"]
    assert config["displayName"] == "LLM Reliability Monitor"
    assert config["queryTerms"] == ["llm reliability", "rag evaluation"]
    assert config["defaultUniqueLimit"] == 17
    acquisition = next(plan for plan in planner["runPlans"] if plan["planId"] == "full_text_acquisition_dry_run")
    assert "--unique-limit 17" in acquisition["copyableCliCommand"]
    assert "--download-limit 2" in acquisition["copyableCliCommand"]
    assert "--allow-domain arxiv.org" in acquisition["copyableCliCommand"]
    assert "--max-pdf-bytes 6000000" in acquisition["copyableCliCommand"]
    assert acquisition["serverExecutesCommand"] is False
    assert topic_profiles.list_topic_profiles()["items"] == original_profiles


def test_topic_configuration_save_validates_and_persists_only_config(client, isolated_run_repo, monkeypatch) -> None:
    profile_path = _isolate_topic_profiles(isolated_run_repo, monkeypatch)
    client.get("/topic-profiles")
    existing_run_paths = {path.name for path in isolated_run_repo["storage_root"].glob("*")}
    metadata_touches: list[str] = []
    monkeypatch.setattr(topic_profiles.metadata_index, "touch_index_state", lambda key, value=None: metadata_touches.append(key))

    response = client.put(
        "/api/topics/large-language-models/configuration",
        json={
            "displayName": "LLM Safety Monitor",
            "queryTerms": ["llm safety", "alignment evaluation"],
            "includeKeywords": ["llm safety", "alignment evaluation", "cs.CL"],
            "excludeKeywords": ["virtual reality"],
            "minRelevance": 70,
            "defaultUniqueLimit": 25,
            "defaultAllowDomains": ["arxiv.org"],
            "defaultDownloadLimit": 3,
            "defaultMaxPdfBytes": 8_000_000,
            "recommendationLimit": 4,
            "followupLimit": 8,
            "providerModeDefault": "stub",
            "liveActionsEnabled": False,
            "notes": ["M7.2 save smoke"],
        },
    )
    body = response.json()

    assert response.status_code == 200, response.text
    assert body["validationStatus"] == "saved"
    assert body["savedPath"].endswith("topic_profiles/profiles.json") or body["savedPath"].endswith("topic_profiles\\profiles.json")
    assert "displayName" in body["changedFields"]
    assert "queryTerms" in body["changedFields"]
    assert body["configHash"]

    saved = topic_profiles.get_topic_profile("large-language-models")
    assert saved["title"] == "LLM Safety Monitor"
    assert saved["queries"] == ["llm safety", "alignment evaluation"]
    assert saved["operations_config"]["default_download_limit"] == 3
    assert saved["operations_config"]["default_max_pdf_bytes"] == 8_000_000
    assert saved["operations_config"]["provider_mode_default"] == "stub"
    assert saved["operations_config"]["live_actions_enabled"] is False
    assert {path.name for path in isolated_run_repo["storage_root"].glob("*")} == existing_run_paths
    assert profile_path.exists()
    assert metadata_touches == []
