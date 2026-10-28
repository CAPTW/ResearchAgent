"""Synthetic local settings/planning only; no Ollama daemon or execution path."""
from copy import deepcopy
import json

import httpx
import pytest

from app.domain import prepared_review, review_document_plan as domain, review_preparation as prep, review_range_input
from app.domain.journal_exchange import preserved_json
from app.repositories.shared_state import SharedStateSchemaInvalid
from app.services import model_connections as connections, model_connection_pipeline as pipeline, model_credentials
from app.services import review_document_plan as plans
from app.services.providers import describe_provider_status, get_active_provider
from app.services.providers import ollama_prepared_review as adapter
from tests import test_review_preparation_api as preparation_tests
from tests.test_model_connections import connections as connections_fixture
from tests.test_review_preparation_api import client, observed, root

range_source = preparation_tests.range_source
seeded = preparation_tests.seeded
connection_state = connections_fixture

# Captured from unchanged candidate 31a970ad, using the synthetic inputs below.
# A new connector must not reinterpret any of these seven V1 identities/bytes.
V1_HASHES = {
    "codex_subscription": "5abcca72d1e521fdbe125f6d5c4cfc91a8cef1575acdf2f297d056eee5a54142",
    "claude_subscription": "b52f26a335e8584cefcc169b5a43162c4cf6a7c7898dbf98fdb504daab7c6087",
    "antigravity_subscription": "9d6c07839855ccafdab38a13ee1ae86f091ec5a871897395ae188102ee3b648d",
    "grok_subscription": "433415d97366745ddece458bc510716593f176012fb0388ee17798cd1f68b19a",
    "openai_api": "dad675d952c0beb050f5b62045ea456a939fd84b4b1c2210125bcb38c04d1d4a",
    "xai_api": "d51aba480ee875b26ab51a48921c7a32c6ade2b0696e6dd135c170b87b754f05",
    "anthropic_api": "c47176804f49c8c6c9ae81a87cb5e8133bd383aa53b83162707922f33612b678",
}
V1_ID = "rplan_682cf37109481a7d38fc1c8542295edc4e7359b60478d9e6d326550ef4989713"


def synthetic_plan(monkeypatch, connector):
    text = "1. Introduction\nSynthetic local planning fixture.\n2. Methods\nOnly generated text."
    source = dict(run_id="synthetic-run", paper_id="synthetic-paper", source_chars=len(text), source_kind="text",
        artifact_sha256=prep.sha(text), artifact_bytes=len(text.encode("utf-8")), artifact_ref="run:raw/synthetic-paper.txt",
        extraction_truncation="UNKNOWN")
    source["source_digest"] = prep.source_digest(source)
    profile = dict(title="Synthetic topic", topic="synthetic", updated_at="2026-01-01")
    paper = dict(title="Synthetic paper", authors=[], _raw={}, metadataDigest="3" * 64)
    monkeypatch.setattr(plans.review_preparation, "_observe_selected", lambda *args: (source, text))
    monkeypatch.setattr(plans, "iso_now_utc", lambda: "2026-01-01T00:00:00+00:00")
    body = domain.Plan(request_id="synthetic-plan", run_id=source["run_id"], paper_id=source["paper_id"],
        expected_source_digest=source["source_digest"], connector_id=connector, model="synthetic-model",
        context_tokens=65536, input_tokens=50000, max_output_tokens=2200, safety_tokens=1024)
    return plans._new("synthetic-topic", "synthetic-version", "synthetic-owner", body, profile, paper)


@pytest.mark.parametrize("connector", list(V1_HASHES))
def test_all_seven_v1_plan_hashes_and_readback_remain_exact(monkeypatch, connector):
    value = synthetic_plan(monkeypatch, connector)
    assert value["schema_version"] == "PAPER_DOCUMENT_PLAN_V1"
    assert value["plan_id"] == V1_ID and value["plan_sha256"] == V1_HASHES[connector]
    assert "local_capability" not in value
    monkeypatch.setattr(connections, "BY_ID", {})
    monkeypatch.setattr(connections, "CONNECTORS", ())
    assert plans.validate(value) == value


def test_v1_settings_read_is_byte_preserving_then_explicit_edit_is_v2_and_replay_safe(connection_state):
    before = dict(schema_version="MODEL_CONNECTIONS_V1", revision=7, default_connector_id="openai_api",
        models={"openai_api": "synthetic-model"})
    raw = json.dumps(before, indent=3) + "\n"
    connection_state.STATE_PATH.write_text(raw, encoding="utf-8")
    before_bytes = connection_state.STATE_PATH.read_bytes()
    assert connection_state.read() == before
    status = connection_state.status()
    assert status["schema_version"] == "MODEL_CONNECTIONS_V1" and status["catalog_schema_version"] == "MODEL_CONNECTORS_V2"
    assert connection_state.STATE_PATH.read_bytes() == before_bytes
    body = connections.Update(expected_revision=7, default_connector_id="ollama_local",
        models=before["models"] | {"ollama_local": "synthetic-model:tiny"})
    saved = connection_state.save(body)
    assert saved["schema_version"] == "MODEL_CONNECTIONS_V2" and saved["revision"] == 8
    assert connection_state.save(body) == saved
    assert connection_state.read()["models"] == body.models
    with pytest.raises(ValueError, match="^model_connections_changed$"):
        connection_state.save(body.model_copy(update={"default_connector_id": "openai_api"}))


def test_v1_cannot_smuggle_new_connector_or_silently_upgrade(connection_state):
    value = dict(schema_version="MODEL_CONNECTIONS_V1", revision=1, default_connector_id="ollama_local", models={})
    raw = json.dumps(value)
    connection_state.STATE_PATH.write_text(raw, encoding="utf-8")
    before = connection_state.STATE_PATH.read_bytes()
    with pytest.raises(SharedStateSchemaInvalid):
        connection_state.read()
    assert connection_state.STATE_PATH.read_bytes() == before


@pytest.mark.parametrize("model", ["model:cloud", "model:tiny-cloud", "https://synthetic.invalid/model", "sk-synthetic", "xai-synthetic", "ya29.synthetic"])
def test_local_settings_and_plan_reject_remote_or_credential_shaped_model_without_echo(model):
    with pytest.raises(ValueError):
        connections.Update(expected_revision=0, default_connector_id="ollama_local", models={"ollama_local": model})
    with pytest.raises(ValueError):
        domain.Plan(request_id="synthetic", run_id="run", paper_id="paper", expected_source_digest="0" * 64,
            connector_id="ollama_local", model=model, context_tokens=65536, input_tokens=50000,
            max_output_tokens=2200, safety_tokens=1024)
    # Remote/cloud model naming for existing external providers is unchanged.
    if "cloud" in model:
        connections.Update(expected_revision=0, default_connector_id="openai_api", models={"openai_api": model})


def test_local_selection_and_capability_never_resolve_keys_or_activate(connection_state, monkeypatch):
    looked_up = []
    monkeypatch.setattr(model_credentials, "status", lambda connector: looked_up.append(connector) or {"source": "none"})
    monkeypatch.setattr(model_credentials, "resolve", lambda *args: pytest.fail("no credential resolution"))
    saved = connection_state.save(connections.Update(expected_revision=0, default_connector_id="ollama_local",
        models={"ollama_local": "synthetic-model:tiny"}))
    row = next(item for item in saved["connectors"] if item["connector_id"] == "ollama_local")
    assert row["auth_mode"] == "local_runtime" and row["provider_name"] == "ollama"
    assert row["adapter_available"] is False
    assert row["credential_env"] is row["credential_present"] is row["cli_command"] is None
    assert row["local_capability"] == domain.local_capability_v1()
    assert row["local_capability"]["execution_available"] is False
    assert "ollama_local" not in looked_up and saved["execution_allowed"] is False
    assert get_active_provider() is None
    assert describe_provider_status()["provider_mode"] == "local_planning_only"
    assert describe_provider_status()["live_call_enabled"] is False
    from tests.test_prepared_review import packet
    with pytest.raises(ValueError, match="^model_connection_adapter_unavailable$"):
        pipeline.prepare_paper(packet())
    frozen = prepared_review.prepare(packet(), provider_name="ollama", model="synthetic-model:tiny")
    with pytest.raises(ValueError, match="^model_connection_adapter_unavailable$"):
        pipeline.bind_paper(frozen, connector_id="ollama_local")
    with pytest.raises(ValueError, match="^model_connection_adapter_unavailable$"):
        pipeline.bind_journal({}, connector_id="ollama_local")
    from app.services.journal_connection_preparation import _selection
    assert _selection()["ready"] is False
    from app.domain import review_transport_execution
    with pytest.raises(ValueError, match="^review_execution_transport_mismatch$"):
        review_transport_execution.build_manifest(frozen, adapter.build_plan(frozen, context_tokens=65536, timeout_seconds=30)[2])


def local_request(http, source, *, input_tokens=100000):
    found = observed(http, source)
    return dict(request_id="synthetic-local-plan", run_id=found["run_id"], paper_id=found["paper_id"],
        expected_source_digest=found["expected_source_digest"], connector_id="ollama_local", model="synthetic-model:tiny",
        context_tokens=131072, input_tokens=input_tokens, max_output_tokens=2200, safety_tokens=4096)


def test_local_v2_plan_prepare_restart_and_settings_changes_do_not_execute(range_source, connection_state, monkeypatch):
    monkeypatch.setattr(model_credentials, "resolve", lambda *args: pytest.fail("no credential resolution"))
    monkeypatch.setattr(adapter, "OllamaPreparedReviewProvider", lambda *args, **kwargs: pytest.fail("no adapter runtime"))
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda *args: pytest.fail("no network"))
    connection_state.save(connections.Update(expected_revision=0, default_connector_id="ollama_local", models={"ollama_local": "synthetic-model:tiny"}))
    with client() as http:
        configuration = http.get(root(range_source) + "/document-plans/configuration").json()
        choice = next(item for item in configuration["choices"] if item["connector_id"] == "ollama_local")
        assert configuration["default_connector_id"] == "ollama_local"
        assert choice["context_tokens"] is None and choice["budget_source"] == "USER_REQUIRED"
        assert choice["plan_schema_version"] == domain.LOCAL_SCHEMA and choice["max_context_tokens"] == 1000000
        assert choice["local_capability"] == domain.local_capability_v1()
        body = local_request(http, range_source)
        response = http.post(root(range_source) + "/document-plans", json=body)
        assert response.status_code == 200, response.text
        plan = response.json()
        assert plan["schema_version"] == domain.LOCAL_SCHEMA and plan["budget"]["provider_name"] == "ollama"
        assert plan["local_capability"] == choice["local_capability"]
        assert plan["model_capability"] == dict(documented_context_tokens=None, observed_at=None, docs_url=None, live_access="NOT_VERIFIED")
        endpoint = root(range_source) + "/document-plans/" + plan["plan_id"]
        saved = http.post(endpoint + "/units/0/prepare").json()
        assert saved["state"] == "PREPARED_NOT_EXECUTED" and saved["execution"]["started"] is False
        contract = prepared_review.prepare(review_range_input.build_packet(saved), provider_name="ollama", model=body["model"])
        assert contract["schema_version"] == prepared_review.DOCUMENT_SCHEMA
        assert contract["packet"]["source_preparation"]["analysis_unit"]["budget"] == plan["budget"]
        range_source[2].unlink()
        connection_state.save(connections.Update(expected_revision=1, default_connector_id="openai_api", models={"openai_api": "different-model"}))
        assert http.post(root(range_source) + "/document-plans", json=body).json()["plan_id"] == plan["plan_id"]
        assert http.post(endpoint + "/units/0/prepare").json() == saved
        conflict = http.post(root(range_source) + "/document-plans", json=body | {"model": "different-model"})
        assert conflict.status_code == 409
    with client() as http:
        reread = http.get(endpoint).json()
        assert reread["budget"] == plan["budget"] and reread["local_capability"] == plan["local_capability"]
        assert reread["units"][0]["preparation_id"] == saved["preparation_id"]
        assert reread["units"][0]["receipts"] == []
        assert reread["full_document_review"] == "NOT_ESTABLISHED"


def test_v2_split_preserves_coverage_under_pure_wire_bound(range_source, monkeypatch):
    monkeypatch.setattr(adapter, "MAX_REQUEST_BYTES", 24000)
    with client() as http:
        body = local_request(http, range_source)
        response = http.post(root(range_source) + "/document-plans", json=body)
        assert response.status_code == 200, response.text
        plan = response.json()
        assert len(plan["units"]) > 1
        cursor = 0
        for unit in plan["units"]:
            assert unit["start"] == cursor and unit["estimated_input_tokens"] <= body["input_tokens"]
            cursor = unit["end"]
        assert cursor == plan["source"]["source_chars"]


@pytest.mark.parametrize("change", ["execution", "locality", "context_capacity", "provider", "demote"])
def test_v2_rehashed_capability_provider_or_version_tampering_is_rejected(monkeypatch, change):
    value = synthetic_plan(monkeypatch, "ollama_local")
    if change in {"execution", "locality", "context_capacity"}:
        field = "execution_available" if change == "execution" else change
        value["local_capability"][field] = True if change == "execution" else "VERIFIED"
    elif change == "provider":
        value["budget"]["provider_name"] = "openai"
        value["request"]["connector_id"] = "openai_api"
    else:
        value["schema_version"] = domain.SCHEMA
        value["plan_id"] = V1_ID
        value.pop("local_capability")
    value["plan_sha256"] = prep.sha(prep.encoded({key: item for key, item in value.items() if key != "plan_sha256"}))
    with pytest.raises(ValueError, match="^document_plan_integrity_invalid$"):
        plans.validate(value)


def test_v2_snapshot_and_model_context_do_not_drift_with_current_catalog(monkeypatch):
    value = synthetic_plan(monkeypatch, "ollama_local")
    original = preserved_json(value)
    monkeypatch.setattr(connections, "CONNECTORS", ())
    monkeypatch.setattr(connections, "BY_ID", {})
    monkeypatch.setattr(plans, "MODEL_CONTEXTS", {("ollama", "synthetic-model"): 999999})
    assert preserved_json(plans.validate(value)) == original
    assert value["model_capability"]["documented_context_tokens"] is None


def test_new_v2_cannot_relabel_legacy_provider_and_context_bound_is_explicit(monkeypatch):
    value = synthetic_plan(monkeypatch, "openai_api")
    value.update(schema_version=domain.LOCAL_SCHEMA, local_capability=domain.local_capability_v1())
    value["plan_id"] = plans.identity(value["topic_id"], value["paper_version_id"], value["actor_id"], value["request"]["request_id"], schema_version=domain.LOCAL_SCHEMA)
    value["plan_sha256"] = prep.sha(prep.encoded({key: item for key, item in value.items() if key != "plan_sha256"}))
    with pytest.raises(ValueError, match="^document_plan_integrity_invalid$"):
        plans.validate(value)
    body = deepcopy(value["request"])
    body.update(connector_id="ollama_local", context_tokens=1000001)
    with pytest.raises(ValueError, match="document_plan_budget_invalid"):
        domain.Plan.model_validate(body)
