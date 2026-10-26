"""No real provider: selection -> frozen request -> adapter -> validated receipt."""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import importlib
import json
import sqlite3

import httpx
import pytest

from app.domain.journal_exchange import preserved_json
from app.repositories import ai_review_drafts as receipts
from app.services import model_connections
from app.services.providers.base import ProviderError, ProviderOutputError, ProviderReportedFailure, ProviderRequest
from tests import test_openai_prepared_review as prior
from tests.test_codex_prepared_review import runtime_at
from tests import test_openai_journal_binding as journal

source = journal.source
completed = journal.completed
execution = journal.execution


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr(model_connections, "STATE_PATH", tmp_path / "connections.json")
    monkeypatch.delenv("XRML_PROVIDER", raising=False)
    return importlib.import_module("app.services.model_connection_pipeline")


def select(connector, model="synthetic-model"):
    value = model_connections.status()
    model_connections.save(model_connections.Update(expected_revision=value["revision"],
        default_connector_id=connector, models={connector: model}))


@pytest.mark.parametrize("connector,provider_name,host", [("openai_api", "openai", "api.openai.com"), ("xai_api", "xai", "api.x.ai")])
def test_selected_api_request_and_receipt_persist_without_secret(pipeline, monkeypatch, tmp_path, connector, provider_name, host):
    select(connector)
    frozen = pipeline.prepare_paper(prior.prior.packet())
    assert frozen["provider_name"] == provider_name
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=prior.envelope(frozen))
    key_name = "OPENAI_API_KEY" if provider_name == "openai" else "XAI_API_KEY"
    monkeypatch.setenv(key_name, "synthetic-private-key")
    provider = pipeline.bind_paper(frozen, connector_id=connector, transport=httpx.MockTransport(handler))
    assert calls == []
    select("codex_subscription", "other-model")
    record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    assert len(calls) == 1
    assert calls[0].url.host == host
    assert calls[0].headers["authorization"] == "Bearer synthetic-private-key"
    assert record["validation"]["prepared_execution"]["contract"] == frozen
    assert record["validation"]["prepared_execution"]["response"]["provider_name"] == provider_name
    assert "synthetic-private-key" not in repr(provider) + preserved_json(record)
    database = tmp_path / "receipt.sqlite3"
    with closing(sqlite3.connect(database)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        saved = receipts.append_input_receipt_in(db, record, input_packet=frozen["packet"], input_origin="FIXTURE_SYNTHETIC")
    with closing(sqlite3.connect(database)) as db:
        db.row_factory = sqlite3.Row
        assert receipts.get_input_receipt_in(db, "topic", saved["receipt_id"]) == saved


def test_codex_uses_only_explicit_official_cli_boundary(pipeline, tmp_path):
    select("codex_subscription", "synthetic-model")
    frozen = pipeline.prepare_paper(prior.prior.packet())
    with pytest.raises(ValueError, match="model_connection_runtime_required"):
        pipeline.bind_paper(frozen, connector_id="codex_subscription")
    runtime = runtime_at(tmp_path / "fake", frozen)
    provider = pipeline.bind_paper(frozen, connector_id="codex_subscription", runtime=runtime, reasoning_effort="low")
    record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    assert record["validation"]["prepared_execution"]["response"]["provider_name"] == "codex_cli"


def test_claude_selection_binds_own_runtime_and_frozen_request(pipeline, tmp_path):
    select("claude_subscription", "synthetic-claude-model")
    frozen = pipeline.prepare_paper(prior.prior.packet())
    assert frozen["provider_name"] == "claude_code"
    with pytest.raises(ValueError, match="model_connection_runtime_required"):
        pipeline.bind_paper(frozen, connector_id="claude_subscription")
    with pytest.raises(ValueError, match="model_connection_runtime_mismatch"):
        pipeline.bind_paper(frozen, connector_id="claude_subscription", runtime=runtime_at(tmp_path / "wrong", frozen))
    fake = importlib.import_module("tests.test_claude_prepared_review").runtime_at(tmp_path / "claude", frozen)
    provider = pipeline.bind_paper(frozen, connector_id="claude_subscription", runtime=fake)
    select("openai_api", "different-model")
    record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    assert record["validation"]["prepared_execution"]["contract"] == frozen
    assert record["validation"]["prepared_execution"]["response"]["provider_name"] == "claude_code"


def test_antigravity_selection_binds_own_runtime_and_frozen_request(pipeline, tmp_path):
    select("antigravity_subscription", "synthetic-antigravity-model")
    frozen = pipeline.prepare_paper(prior.prior.packet())
    assert frozen["provider_name"] == "antigravity_cli"
    with pytest.raises(ValueError, match="model_connection_runtime_required"):
        pipeline.bind_paper(frozen, connector_id="antigravity_subscription")
    with pytest.raises(ValueError, match="model_connection_runtime_mismatch"):
        pipeline.bind_paper(frozen, connector_id="antigravity_subscription", runtime=runtime_at(tmp_path / "wrong-agy", frozen))
    fake = importlib.import_module("tests.test_antigravity_prepared_review").runtime_at(tmp_path / "antigravity", frozen)
    provider = pipeline.bind_paper(frozen, connector_id="antigravity_subscription", runtime=fake)
    select("codex_subscription", "different-model")
    record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    assert record["validation"]["prepared_execution"]["contract"] == frozen
    assert record["validation"]["prepared_execution"]["response"]["provider_name"] == "antigravity_cli"


def test_grok_selection_binds_own_runtime_and_frozen_request(pipeline, tmp_path):
    select("grok_subscription", "synthetic-grok-model")
    frozen = pipeline.prepare_paper(prior.prior.packet())
    assert frozen["provider_name"] == "grok_cli"
    with pytest.raises(ValueError, match="model_connection_runtime_required"):
        pipeline.bind_paper(frozen, connector_id="grok_subscription")
    with pytest.raises(ValueError, match="model_connection_runtime_mismatch"):
        pipeline.bind_paper(frozen, connector_id="grok_subscription", runtime=runtime_at(tmp_path / "wrong-grok", frozen))
    fake = importlib.import_module("tests.test_grok_prepared_review").runtime_at(tmp_path / "grok", frozen)
    provider = pipeline.bind_paper(frozen, connector_id="grok_subscription", runtime=fake)
    select("codex_subscription", "different-model")
    record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    assert record["validation"]["prepared_execution"]["contract"] == frozen
    assert record["validation"]["prepared_execution"]["response"]["provider_name"] == "grok_cli"


def test_unavailable_adapter_still_cannot_fall_back_or_read_keys(pipeline, monkeypatch):
    select("grok_subscription")
    monkeypatch.setitem(model_connections.BY_ID, "grok_subscription",
        replace(model_connections.BY_ID["grok_subscription"], adapter_available=False))
    with pytest.raises(ValueError, match="model_connection_adapter_unavailable"):
        pipeline.prepare_paper(prior.prior.packet())


def test_missing_model_key_changed_provider_and_override_fail_closed(pipeline, monkeypatch):
    with pytest.raises(ValueError, match="model_connection_model_required"):
        pipeline.prepare_paper(prior.prior.packet())
    select("openai_api")
    frozen = pipeline.prepare_paper(prior.prior.packet())
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="model_connection_key_required"):
        pipeline.bind_paper(frozen, connector_id="openai_api")
    with pytest.raises(ValueError, match="model_connection_mismatch"):
        pipeline.bind_paper(frozen, connector_id="xai_api")
    monkeypatch.setenv("XRML_PROVIDER", "disabled")
    with pytest.raises(ValueError, match="model_connection_unavailable"):
        pipeline.prepare_paper(prior.prior.packet())


@pytest.mark.parametrize("status", ["failed", "incomplete", "cancelled", "in_progress"])
def test_xai_terminal_and_unknown_outcomes_not_retried(pipeline, monkeypatch, status):
    select("xai_api")
    frozen = pipeline.prepare_paper(prior.prior.packet())
    monkeypatch.setenv("XAI_API_KEY", "synthetic")
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=prior.envelope(frozen) | {"status": status})
    provider = pipeline.bind_paper(frozen, connector_id="xai_api", transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as caught:
        provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    assert isinstance(caught.value, ProviderReportedFailure) == (status != "in_progress")
    assert len(calls) == 1


def test_xai_manifest_request_mutation_and_invalid_citation(pipeline, monkeypatch):
    select("xai_api")
    frozen = pipeline.prepare_paper(prior.prior.packet())
    monkeypatch.setenv("XAI_API_KEY", "synthetic")
    calls = []
    def handler(request):
        calls.append(request)
        output = prior.envelope(frozen)
        value = json.loads(output["output"][0]["content"][0]["text"])
        value["cited_evidence_span_ids"] = ["invented"]
        output["output"][0]["content"][0]["text"] = json.dumps(value)
        return httpx.Response(200, json=output)
    provider = pipeline.bind_paper(frozen, connector_id="xai_api", transport=httpx.MockTransport(handler))
    request = ProviderRequest(**deepcopy(frozen["provider_request"]))
    request.prompt += "changed"
    with pytest.raises(ValueError):
        provider.generate_record(request)
    assert calls == []
    with pytest.raises(ProviderOutputError):
        provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    body = json.loads(calls[0].content)
    assert body["store"] is False and body["stream"] is False
    assert body["text"]["format"]["schema"]["properties"]["contribution_summary"]["pattern"] == r"[\s\S]*\S[\s\S]*"
    from app.domain.review_transport_execution import build_manifest
    manifest = provider.manifest()
    assert build_manifest(frozen, manifest) == manifest
    manifest["endpoint"] = "https://other.invalid/v1/responses"
    with pytest.raises(ValueError):
        build_manifest(frozen, manifest)


def test_journal_binding_uses_existing_contract_and_keeps_default_deny(pipeline, monkeypatch, execution):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    calls = []
    def handler(request):
        calls.append(request)
        output = journal.prior.imports.output(execution["contract"]["packet"])
        return httpx.Response(200, json=journal.envelope(json.dumps(output)))
    provider = pipeline.bind_journal(execution["contract"], connector_id="openai_api", transport=httpx.MockTransport(handler))
    assert calls == []
    assert provider.describe_status()["live_call_enabled"] is False
    select("codex_subscription", "changed-default")
    result = provider.generate_json(ProviderRequest(**execution["contract"]["provider_request"]))
    assert result.provider_name == "openai" and len(calls) == 1
    assert calls[0].url.host == "api.openai.com"
    assert provider.manifest()["model"] == execution["contract"]["specification"]["model"]
    with pytest.raises(ValueError, match="model_connection_mismatch"):
        pipeline.bind_journal(execution["contract"], connector_id="codex_subscription")


def test_xai_journal_uses_separate_key_and_preserves_frozen_selection(pipeline, monkeypatch):
    from app.domain import journal_execution
    from tests.test_journal_review_packet import frozen_job, proposal, NOW
    job = frozen_job()
    contract = journal_execution.prepare(job, journal_execution.Prepare(request_id="xai-journal",
        expected_input_digest=job["input_digest"], provider_name="xai", model="synthetic-xai"), "owner", NOW)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "wrong-provider-key")
    with pytest.raises(ValueError, match="model_connection_key_required"):
        pipeline.bind_journal(contract, connector_id="xai_api")
    monkeypatch.setenv("XAI_API_KEY", "synthetic-xai-key")
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=journal.envelope(json.dumps(proposal(contract["packet"]))))
    provider = pipeline.bind_journal(contract, connector_id="xai_api", transport=httpx.MockTransport(handler))
    assert calls == [] and provider.describe_status()["live_call_enabled"] is False
    select("codex_subscription", "changed-model")
    result = provider.generate_json(ProviderRequest(**contract["provider_request"]))
    assert len(calls) == 1 and calls[0].url.host == "api.x.ai"
    assert calls[0].headers["authorization"] == "Bearer synthetic-xai-key"
    assert result.provider_name == "xai" and provider.model == "synthetic-xai"
    assert "synthetic-xai-key" not in repr(provider) + preserved_json(provider.manifest())
    with pytest.raises(ValueError, match="model_connection_mismatch"):
        pipeline.bind_journal(contract, connector_id="grok_subscription")


def test_antigravity_journal_requires_own_runtime_and_keeps_frozen_selection(pipeline, tmp_path):
    from app.domain import journal_execution
    from tests.test_journal_review_packet import frozen_job, NOW
    job = frozen_job()
    contract = journal_execution.prepare(job, journal_execution.Prepare(request_id="antigravity-journal",
        expected_input_digest=job["input_digest"], provider_name="antigravity_cli", model="synthetic-agy"), "owner", NOW)
    with pytest.raises(ValueError, match="model_connection_runtime_required"):
        pipeline.bind_journal(contract, connector_id="antigravity_subscription")
    with pytest.raises(ValueError, match="model_connection_runtime_mismatch"):
        pipeline.bind_journal(contract, connector_id="antigravity_subscription", runtime=object())
    fixture = importlib.import_module("tests.test_antigravity_journal")
    runtime = fixture.runtime_at(tmp_path / "agy-journal", contract)
    provider = pipeline.bind_journal(contract, connector_id="antigravity_subscription", runtime=runtime)
    assert provider.describe_status()["live_call_enabled"] is False
    select("codex_subscription", "changed-default")
    result = provider.generate_json(ProviderRequest(**contract["provider_request"]))
    assert result.provider_name == "antigravity_cli" and result.model == "not_reported"
    assert provider.model == "synthetic-agy"
    assert (tmp_path / "agy-journal" / "launches.log").read_text().splitlines() == ["launch"]
    with pytest.raises(ValueError, match="model_connection_mismatch"):
        pipeline.bind_journal(contract, connector_id="claude_subscription", runtime=runtime)


def test_claude_journal_requires_own_runtime_and_keeps_frozen_selection(pipeline, tmp_path):
    from app.domain import journal_execution
    from tests.test_journal_review_packet import frozen_job, NOW
    job = frozen_job(selected=True)
    contract = journal_execution.prepare(job, journal_execution.Prepare(request_id="claude-journal",
        expected_input_digest=job["input_digest"], provider_name="claude_code", model="synthetic-claude"), "owner", NOW)
    with pytest.raises(ValueError, match="model_connection_runtime_required"):
        pipeline.bind_journal(contract, connector_id="claude_subscription")
    wrong = runtime_at(tmp_path / "wrong-journal", prior.contract())
    with pytest.raises(ValueError, match="model_connection_runtime_mismatch"):
        pipeline.bind_journal(contract, connector_id="claude_subscription", runtime=wrong)
    fake = importlib.import_module("tests.test_claude_journal").runtime_at(tmp_path / "claude-journal", contract)
    provider = pipeline.bind_journal(contract, connector_id="claude_subscription", runtime=fake)
    assert not (tmp_path / "claude-journal" / "launches.log").exists()
    assert provider.describe_status()["live_call_enabled"] is False
    select("codex_subscription", "changed-model")
    result = provider.generate_json(ProviderRequest(**contract["provider_request"]))
    assert result.provider_name == "claude_code" and result.model == "not_reported"
    assert provider.model == "synthetic-claude"
    assert (tmp_path / "claude-journal" / "stdin.json").read_text(encoding="utf-8") == preserved_json(contract["provider_request"])
    assert (tmp_path / "claude-journal" / "launches.log").read_text().splitlines() == ["launch"]
    with pytest.raises(ValueError, match="model_connection_mismatch"):
        pipeline.bind_journal(contract, connector_id="codex_subscription", runtime=fake)
    codex = journal_execution.prepare(job, journal_execution.Prepare(request_id="codex-journal",
        expected_input_digest=job["input_digest"], provider_name="codex_cli", model="synthetic-codex"), "owner", NOW)
    with pytest.raises(ValueError, match="model_connection_runtime_mismatch"):
        pipeline.bind_journal(codex, connector_id="codex_subscription", runtime=fake)


def test_grok_journal_requires_own_runtime_and_keeps_frozen_selection(pipeline, tmp_path):
    from app.domain import journal_execution
    from tests.test_journal_review_packet import frozen_job, NOW
    job = frozen_job()
    contract = journal_execution.prepare(job, journal_execution.Prepare(request_id="grok-journal",
        expected_input_digest=job["input_digest"], provider_name="grok_cli", model="synthetic-grok"), "owner", NOW)
    with pytest.raises(ValueError, match="model_connection_runtime_required"):
        pipeline.bind_journal(contract, connector_id="grok_subscription")
    with pytest.raises(ValueError, match="model_connection_runtime_mismatch"):
        pipeline.bind_journal(contract, connector_id="grok_subscription", runtime=object())
    fixture = importlib.import_module("tests.test_grok_journal")
    runtime = fixture.runtime_at(tmp_path / "grok-journal", contract)
    provider = pipeline.bind_journal(contract, connector_id="grok_subscription", runtime=runtime)
    assert provider.describe_status()["live_call_enabled"] is False
    select("codex_subscription", "changed-default")
    result = provider.generate_json(ProviderRequest(**contract["provider_request"]))
    assert result.provider_name == "grok_cli" and result.model == "not_reported"
    assert provider.model == "synthetic-grok"
    assert (tmp_path / "grok-journal" / "launches.log").read_text().splitlines() == ["launch"]
    with pytest.raises(ValueError, match="model_connection_mismatch"):
        pipeline.bind_journal(contract, connector_id="claude_subscription", runtime=runtime)
