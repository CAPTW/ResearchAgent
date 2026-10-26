"""Explicit Codex V2 composition and archived V1/V2 reads; synthetic input only."""
import importlib
import inspect
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import subprocess

import pytest

from app.db.bootstrap import connect_metadata_index
from app.domain import generated_journal, prepared_review, review_authority, review_transport_execution as transport
from app.domain import journal_dispatch as journal_receipts
from app.domain.journal_exchange import preserved_json
from app.domain.review_preparation import encoded, sha
from app.domain.review_range_input import build_packet
from app.domain.temporal_memory import digest
from app.repositories import ai_review_drafts as receipts, journal_candidates, journal_executions
from app.repositories import review_executions, review_preparations
from tests import test_codex_journal as journal, test_codex_prepared_review as paper
from tests import test_journal_dispatch as dispatch, test_journal_execution as prior
from tests import test_prepared_review as drafts, test_review_authority as proofs, test_review_dispatch as paper_dispatch

source = prior.source
completed = prior.completed
setup = paper_dispatch.setup
runtimes = paper_dispatch.runtimes


def paper_plan(version):
    suffix = "_v2" if version == "2" else ""
    return importlib.import_module("app.services.providers.codex_prepared_review_plan" + suffix)


def frozen_paper():
    return prepared_review.prepare(drafts.packet(), provider_name="codex_cli", model="synthetic-review-v1")


def manifest_for(frozen, version):
    return paper_plan(version).build_plan(frozen, runtime_identity=dict(launcher_sha256="a" * 64,
        environment_sha256="b" * 64, workspace_sha256="c" * 64), reasoning_effort="low", timeout_seconds=30)[-1]


def paper_response(frozen):
    return replace(drafts.response(frozen), provider_name="codex_cli", model="not_reported")


def forbid_process(*args, **kwargs):
    pytest.fail("Integration reads and synthetic responses must never launch a process")


@pytest.mark.parametrize("name", ["bind_journal", "bind_paper"])
def test_pipeline_profile_is_explicit_and_defaults_to_v1(name):
    pipeline = importlib.import_module("app.services.model_connection_pipeline")
    parameter = inspect.signature(getattr(pipeline, name)).parameters["codex_profile"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default == "CODEX_JOURNAL_CLI_V1"


@pytest.mark.parametrize("name", ["bind_journal", "bind_paper"])
@pytest.mark.parametrize("profile", ["", "CODEX_JOURNAL_CLI_V3", "codex_journal_cli_v2", None, []])
def test_pipeline_rejects_unknown_profile_before_binding(name, profile):
    pipeline = importlib.import_module("app.services.model_connection_pipeline")
    with pytest.raises(ValueError, match="^model_connection_codex_profile_invalid$"):
        getattr(pipeline, name)({}, connector_id="codex_subscription", codex_profile=profile)


@pytest.mark.parametrize("version", [None, "1", "2"])
def test_paper_pipeline_selects_only_explicit_profile(tmp_path, monkeypatch, version):
    pipeline = importlib.import_module("app.services.model_connection_pipeline")
    frozen = frozen_paper()
    runtime = paper.runtime_at(tmp_path / "cli", frozen)
    monkeypatch.setattr(subprocess, "Popen", forbid_process)
    kwargs = {} if version is None else dict(codex_profile="CODEX_JOURNAL_CLI_V" + version)
    provider = pipeline.bind_paper(frozen, connector_id="codex_subscription", runtime=runtime, **kwargs)
    assert provider.manifest()["schema_version"] == "CODEX_PREPARED_REVIEW_CLI_V" + (version or "1")
    assert provider.describe_status()["live_call_enabled"] is False


@pytest.mark.parametrize("version", ["1", "2"])
def test_paper_reconstruction_and_receipt_are_version_exact_without_runtime(version, monkeypatch):
    frozen = frozen_paper()
    manifest = manifest_for(frozen, version)
    record = paper_plan(version).transport_record(frozen, paper_response(frozen), manifest)
    monkeypatch.setattr(subprocess, "Popen", forbid_process)
    assert transport.build_manifest(frozen, manifest) == manifest
    transport.validate_record(dict(prepared=frozen, transport=manifest), record)
    content = receipts._receipt_content(receipts._normalize_record(record), frozen["packet"], "FIXTURE_SYNTHETIC")
    assert content["validation"]["execution"]["manifest"] == manifest
    changed = deepcopy(manifest)
    changed["adapter_version"] = "2" if version == "1" else "1"
    with pytest.raises(ValueError, match="transport_mismatch"):
        transport.build_manifest(frozen, changed)


@pytest.mark.parametrize("change", ["version", "schema", "adapter", "missing_schema", "missing_adapter", "missing_both", "manifest"])
def test_paper_reserved_v2_marker_never_falls_through_to_unverified_receipt(change):
    frozen = frozen_paper()
    record = paper_plan("2").transport_record(frozen, paper_response(frozen), manifest_for(frozen, "2"))
    marker = record["validation"]["execution"]
    if change == "version":
        marker["adapter_version"] = "1"
    elif change == "schema":
        marker["schema_version"] = "PREPARED_REVIEW_CLI_TRANSPORT_V1"
    elif change == "adapter":
        marker["adapter_id"] = "unknown"
    elif change == "manifest":
        marker["manifest"]["request_max_retries"] = 1
        marker["manifest_sha256"] = sha(preserved_json(marker["manifest"]))
    else:
        for field in {"missing_schema": ["schema_version"], "missing_adapter": ["adapter_id"],
                      "missing_both": ["schema_version", "adapter_id"]}[change]:
            del marker[field]
    with pytest.raises(ValueError, match="paper_codex_transport_invalid"):
        receipts._receipt_content(receipts._normalize_record(record), frozen["packet"], "FIXTURE_SYNTHETIC")


def test_paper_v1_authority_cannot_be_reused_for_v2_even_with_new_contract_hash():
    old = proofs.contract()
    grant = proofs.grant(old)
    current = deepcopy(old)
    current["transport"] = manifest_for(current["prepared"], "2")
    with pytest.raises(ValueError):
        review_authority.validate_grant(grant.model_copy(update={"contract_sha256": sha(encoded(current))}), current, proofs.NOW)
    review_authority.validate_grant(proofs.grant(current), current, proofs.NOW)


def test_db_journal_mixed_versions_preserve_candidates_and_replay_without_process(completed, tmp_path, monkeypatch):
    pipeline = importlib.import_module("app.services.model_connection_pipeline")
    v2 = importlib.import_module("app.services.providers.codex_journal_v2")
    saved_versions = []
    calls = []
    monkeypatch.setattr(subprocess, "Popen", forbid_process)
    for version in ("1", "2"):
        execution = prior.prepare(completed, request_id="profile-" + version,
            provider_name="codex_cli", model="offline-requested-model")[0]
        runtime = journal.runtime_at(tmp_path / ("cli-" + version))
        provider = pipeline.bind_journal(execution["contract"], connector_id="codex_subscription", runtime=runtime,
            codex_profile="CODEX_JOURNAL_CLI_V" + version, reasoning_effort="low", timeout_seconds=30)
        response = prior.response(execution, provider_name="codex_cli", model="not_reported")
        def respond(self, request, result=response, selected=version):
            calls.append(selected)
            return result
        monkeypatch.setattr(type(provider), "generate_json", respond)
        controller = (journal.journal_dispatcher if version == "1" else v2.journal_dispatcher)(provider,
            authority=lambda *_, item=execution, bound=provider: journal.approval(item, bound), clock=lambda: prior.NOW)
        try:
            controller.dispatch(*prior.scope(execution), dispatch.body(), "owner")
            saved = journal.wait_terminal(execution)
            assert saved["current"]["state"] == "SUCCEEDED"
            candidate, created = journal_candidates.save_execution(*prior.scope(saved), digest(saved["current"]["result"]), "owner")
            assert created
            assert candidate["raw_json"] == response.raw_text
            receipt = candidate["execution"]
            assert receipt["schema_version"] == "JOURNAL_EXECUTION_RECEIPT_V" + version
            if version == "1":
                assert "adapter_manifest" not in receipt
            else:
                assert receipt["adapter_manifest"] == provider.manifest()
                assert receipt["adapter_manifest"]["raw_output_encoding"] == "LAST_MESSAGE_JSON_UTF8_V1"
                assert receipt["adapter_manifest"]["raw_envelope_retained"] is False
                assert "Codex 최종 메시지" in candidate["markdown"]
            # Replayed commands and history never consult current runtime files.
            with monkeypatch.context() as patch:
                patch.setattr(type(provider), "validate_local_runtime", forbid_process)
                assert controller.dispatch(*prior.scope(saved), dispatch.body(), "owner") == saved
                assert journal_candidates.detail(*prior.scope(saved)[:2], candidate["candidate_id"]) == candidate
                assert journal_candidates.save_execution(*prior.scope(saved), digest(saved["current"]["result"]), "other") == (candidate, False)
            saved_versions.append((saved, candidate))
        finally:
            assert controller.stop()
    assert calls == ["1", "2"]
    assert journal_candidates.listing("topic", completed["job_id"])["total"] == 2
    for saved, candidate in saved_versions:
        assert journal_executions.detail(*prior.scope(saved)) == saved
        assert journal_candidates.detail("topic", completed["job_id"], candidate["candidate_id"]) == candidate


def test_db_journal_v2_requires_new_authority_and_default_denial_is_inert(completed, tmp_path, monkeypatch):
    v2 = importlib.import_module("app.services.providers.codex_journal_v2")
    execution = prior.prepare(completed, provider_name="codex_cli", model="offline-requested-model")[0]
    old = journal.adapter(execution, tmp_path / "cli")
    provider = v2.CodexJournalV2Provider(execution["contract"], runtime=old._runtime, reasoning_effort="low", timeout_seconds=30)
    monkeypatch.setattr(subprocess, "Popen", forbid_process)
    for authority, reason in ((None, "authority_unavailable"), (lambda *_: journal.approval(execution, old), "proof_rejected")):
        options = {} if authority is None else dict(authority=authority)
        controller = v2.journal_dispatcher(provider, clock=lambda: prior.NOW, **options)
        try:
            with pytest.raises(ValueError, match=reason):
                controller.dispatch(*prior.scope(execution), dispatch.body(), "owner")
            assert journal_executions.detail(*prior.scope(execution)) == execution
        finally:
            assert controller.stop()


@pytest.mark.parametrize("change", ["schema", "version", "retry"])
def test_db_journal_v2_receipt_and_candidate_reject_rehashed_tampering(completed, tmp_path, change):
    from app.repositories import journal_dispatch as admission
    v2 = importlib.import_module("app.services.providers.codex_journal_v2")
    execution = prior.prepare(completed, provider_name="codex_cli", model="offline-requested-model")[0]
    provider = v2.CodexJournalV2Provider(execution["contract"], runtime=journal.runtime_at(tmp_path / "cli"),
        reasoning_effort="low", timeout_seconds=30)
    saved, _ = admission.admit(*prior.scope(execution), dispatch.body(), "owner", journal.approval(execution, provider),
        provider.binding, now=prior.NOW, adapter_manifest=provider.manifest())
    event = deepcopy(saved["events"][0])
    if change == "schema":
        event["adapter_manifest"]["schema_version"] = "CODEX_JOURNAL_CLI_V1"
    elif change == "version":
        event["dispatch"]["binding"]["adapter_version"] = "1"
    else:
        event["adapter_manifest"]["request_max_retries"] = 1
    event["dispatch"]["binding"]["command_shape_sha256"] = digest(event["adapter_manifest"])
    with pytest.raises(ValueError):
        journal_receipts.validate_receipt(execution["contract"], journal_executions._initial(), event)
    from app.domain.journal_execution import result
    response = prior.response(execution, provider_name="codex_cli", model="not_reported")
    terminal = dict(state="SUCCEEDED", attempt=1, at=prior.NOW.isoformat(), result=result(execution["contract"], response))
    candidate = generated_journal.assemble(execution["contract"], terminal, "owner", prior.NOW.isoformat(), adapter_manifest=provider.manifest())
    manifest = candidate["execution"]["adapter_manifest"]
    manifest["raw_envelope_retained"] = True
    candidate["execution"]["adapter_manifest_digest"] = digest(manifest)
    candidate["markdown"] = candidate["markdown"].replace(digest(provider.manifest()), digest(manifest))
    candidate["content_sha256"] = sha(candidate["markdown"])
    with pytest.raises(ValueError, match="candidate_integrity_invalid"):
        generated_journal.validate(candidate, completed)


def test_db_paper_mixed_history_receipts_and_replay_keep_original_profiles(setup, tmp_path, monkeypatch):
    from app.services.review_dispatch import PaperDispatchRuntime
    from app.services import model_connection_pipeline as pipeline
    scope, _, clock = setup
    preparation = review_preparations.detail(scope.topic, scope.version, scope.preparation, list(scope.occurrences))
    frozen = prepared_review.prepare(build_packet(preparation), provider_name="codex_cli", model="offline-requested-model")
    calls, saved_versions = [], []
    monkeypatch.setattr(subprocess, "Popen", forbid_process)
    for version in ("1", "2"):
        provider = pipeline.bind_paper(frozen, connector_id="codex_subscription", runtime=paper.runtime_at(tmp_path / ("paper-" + version), frozen),
            codex_profile="CODEX_JOURNAL_CLI_V" + version, reasoning_effort="low", timeout_seconds=30)
        def respond(self, request, selected=version):
            calls.append(selected)
            return paper_response(frozen)
        monkeypatch.setattr(type(provider), "generate_json", respond)
        body = transport.Prepare(request_id="profile-" + version, expected_preparation_digest=sha(encoded(preparation)), model=frozen["model"])
        ready = review_executions.prepare_transport(scope, body, provider.manifest(), input_origin="FIXTURE_SYNTHETIC")
        def issuer(contract, command):
            return review_authority.Record(grant=proofs.grant(contract, authorization_id=contract["execution_id"]),
                issuer_id="synthetic-v2-test", decision_ref="local-only", registered_at=clock[0],
                expected_sequence=command.expected_sequence).model_dump(mode="json")
        runtime = PaperDispatchRuntime(resolver=issuer, provider=provider)
        try:
            paper_dispatch.approve(scope, ready, runtime)
            runtime.dispatch(scope, ready["contract"]["execution_id"], paper_dispatch.dispatch_body())
            runtime.wait_idle(30)
            saved = paper_dispatch.read(scope, ready)
            assert saved["current"]["state"] == "RESULT_SAVED"
            with closing(connect_metadata_index(read_only=True)) as connection:
                receipt = receipts.get_input_receipt_in(connection, scope.topic, saved["current"]["receipt_id"])
            assert receipt["validation"]["execution"]["manifest"] == provider.manifest()
            assert receipt["validation"]["execution"]["adapter_version"] == version
            with monkeypatch.context() as patch:
                patch.setattr(type(provider), "validate_local_runtime", forbid_process)
                assert runtime.dispatch(scope, ready["contract"]["execution_id"], paper_dispatch.dispatch_body()) == saved
                assert paper_dispatch.approve(scope, ready, runtime) == saved
            saved_versions.append((ready, saved, receipt))
        finally:
            runtime.stop()
            runtime.wait_idle(30)
    assert calls == ["1", "2"]
    for ready, saved, receipt in saved_versions:
        assert paper_dispatch.read(scope, ready) == saved
        with closing(connect_metadata_index(read_only=True)) as connection:
            assert receipts.get_input_receipt_in(connection, scope.topic, receipt["receipt_id"]) == receipt
