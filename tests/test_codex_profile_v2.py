"""Explicit V2 routing intent, synthetic processes only; no CLI/account discovery."""
import importlib
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from app.domain import journal_execution, journal_exchange as exchange
from app.domain.journal_dispatch import Dispatch
from app.services import journal_dispatch as dispatch_service
from app.services.providers.base import ProviderError, ProviderOutputError, ProviderReportedFailure, ProviderRequest
from app.services.providers import codex_journal_process as process
from app.services.providers import codex_journal_plan as v1
from app.services.providers import codex_prepared_review_plan as paper_v1
from tests import test_codex_prepared_review as paper_prior, test_codex_journal as journal_prior
from tests import test_journal_review_packet as review_packet

IDENTITY = dict(launcher_sha256="a" * 64, environment_sha256="b" * 64, workspace_sha256="c" * 64)
OPTIONS = dict(runtime_identity=IDENTITY, reasoning_effort="low", timeout_seconds=30)
# Captured from baseline 0e3258f after checking the unchanged V1 builders,
# compiler and argv helper against Git. These are values, not same-builder
# expectations: historical stdin/schema/manifest drift must fail visibly.
V1_GOLDENS = {
    "journal": (
        "4e7b07dbaad6870dc57bef03a3359acdb28e98f22af68325fd3c8901a9aa7d88",
        "0684990c0c2dcf516364ba4d0cc0382110fa02bb68c5ea750efca4809ddda19d",
        "8b77ab99c5ccfdbfbf890a96e14c8d1e83667a3e5636d4f7d4c02fd9a77a7ba2",
    ),
    "paper": (
        "dd5fe7bd7328ae763c55d44f2c6c019ef4aca83daae076b29cf6907402e88ed0",
        "89c235e8bffcaf49024f63bf5d5230d67667a361948e10a4808e89a7e00aa172",
        "ce22769a70df1c1a9a2eeaa49669c20822fb42e9f0cd08d58600efd3930bc689",
    ),
}


def contract(*, selected=True):
    job = review_packet.frozen_job(selected=selected)
    spec = journal_execution.Prepare(request_id="profile-v2", expected_input_digest=job["input_digest"],
                                    provider_name="codex_cli", model="offline-requested-model")
    return journal_execution.prepare(job, spec, "owner", review_packet.NOW)


def modules(domain):
    prefix = "codex_journal" if domain == "journal" else "codex_prepared_review"
    return (importlib.import_module("app.services.providers." + prefix + "_plan_v2"),
            importlib.import_module("app.services.providers." + prefix + "_v2"))


def adapter_at(root, domain, mode="success", **options):
    _, adapter_module = modules(domain)
    frozen = contract() if domain == "journal" else paper_prior.contract()
    # Existing synthetic process fixture records request/schema and emits JSONL.
    runtime = paper_prior.runtime_at(root, paper_prior.contract(), mode)
    if domain == "journal":
        (root / "output.json").write_text(json.dumps(review_packet.proposal(frozen["packet"]),
            ensure_ascii=False, indent=2), encoding="utf-8", newline="")
    adapter_type = (adapter_module.CodexJournalV2Provider if domain == "journal"
                    else adapter_module.CodexPreparedReviewV2Provider)
    provider = adapter_type(frozen, runtime=runtime, **(dict(reasoning_effort="low", timeout_seconds=30) | options))
    return provider, frozen, runtime


def test_v2_uses_a_custom_chatgpt_provider_without_rewriting_v1():
    profile = importlib.import_module("app.services.providers.codex_cli_profile_v2")
    expected = tuple(setting for setting in v1.CONFIG
                     if not setting.startswith(("model_provider=", "model_providers.openai."))) + (
        'model_provider="xrml_openai"',
        'model_providers.xrml_openai.name="OpenAI"',
        'model_providers.xrml_openai.requires_openai_auth=true',
        'model_providers.xrml_openai.wire_api="responses"',
        'model_providers.xrml_openai.request_max_retries=0',
        'model_providers.xrml_openai.stream_max_retries=0',
        'model_providers.xrml_openai.supports_websockets=false',
        'forced_login_method="chatgpt"',
    )
    assert profile.CONFIG == expected
    assert v1.CONFIG[0] == 'model_provider="openai"'
    assert 'model_providers.openai.request_max_retries=0' in v1.CONFIG
    assert not any(any(key in setting for key in ("base_url", "env_key", "token="))
                   for setting in profile.CONFIG)


@pytest.mark.parametrize("domain", ["journal", "paper"])
def test_baseline_v1_stdin_schema_and_manifest_golden_hashes_are_unchanged(domain):
    frozen = contract() if domain == "journal" else paper_prior.contract()
    plan = v1 if domain == "journal" else paper_v1
    stdin, schema, manifest = plan.build_plan(frozen, **OPTIONS)
    assert (v1.sha(stdin), v1.sha(schema), v1.sha(exchange.preserved_json(manifest))) == V1_GOLDENS[domain]


@pytest.mark.parametrize("domain", ["journal", "paper"])
def test_v2_plan_retains_frozen_bytes_and_declares_only_requested_controls(domain):
    plan, _ = modules(domain)
    frozen = contract() if domain == "journal" else paper_prior.contract()
    prior = v1 if domain == "journal" else paper_v1
    old_stdin, old_schema, old_manifest = prior.build_plan(frozen, **OPTIONS)
    stdin, schema, manifest = plan.build_plan(frozen, **OPTIONS)
    assert stdin == old_stdin == exchange.preserved_json(frozen["provider_request"])
    assert schema == old_schema
    assert manifest["stdin_sha256"] == old_manifest["stdin_sha256"]
    assert manifest["schema_sha256"] == old_manifest["schema_sha256"]
    assert manifest["runtime_identity"] == IDENTITY
    profile = importlib.import_module("app.services.providers.codex_cli_profile_v2")
    assert all(manifest.get(key) == value for key, value in profile.profile_metadata().items())
    assert manifest["schema_version"].endswith("_V2")
    assert manifest["adapter_version"] == "2"
    assert manifest["runtime_profile"] == "CODEX_JOURNAL_CLI_V2"
    for key in ("automatic_retry", "temperature_enforced", "output_token_limit_enforced", "cost_limit_enforced",
                "remote_call_count_enforced", "raw_envelope_retained"):
        assert manifest[key] is False
    assert manifest["request_max_retries"] == manifest["stream_max_retries"] == 0
    assert manifest["retry_policy"] == "REQUESTED_ZERO_NOT_LIVE_VERIFIED"
    assert manifest["live_execution"] == "NOT_VERIFIED"
    metadata = profile.profile_metadata()
    metadata["model_provider"] = "changed"
    assert profile.profile_metadata()["model_provider"] == "xrml_openai"
    assert old_manifest["schema_version"].endswith("_V1")
    assert "forced_login_method" not in old_manifest


@pytest.mark.parametrize("domain", ["journal", "paper"])
@pytest.mark.parametrize("separator", ["", "\u0085", "\u2028", "\u2029"])
def test_fake_process_exact_v2_argv_bytes_usage_and_cleanup(tmp_path, monkeypatch, domain, separator):
    root = tmp_path / "cli"
    provider, frozen, runtime = adapter_at(root, domain)
    output_path = root / "output.json"
    output = json.loads(output_path.read_text(encoding="utf-8"))
    output["title" if domain == "journal" else "contribution_summary"] = " e\u0301 한글🙂 " + separator + " output "
    raw = json.dumps(output, ensure_ascii=False, indent=2) + "\n"
    output_path.write_text(raw, encoding="utf-8", newline="")
    original_capture = process.capture
    observed = []
    def recording_capture(command, stdin, cwd, environment, timeout):
        observed.append((command, str(cwd), stdin))
        return original_capture(command, stdin, cwd, environment, timeout)
    monkeypatch.setattr(process, "capture", recording_capture)
    assert not (root / "launches.log").exists()
    assert not (root / "attempts").exists()
    identity = runtime.identity()
    response = provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert response.raw_text == raw
    assert response.model == "not_reported"
    assert response.usage == dict(input_tokens=4, output_tokens=7, total_tokens=11)
    assert response.response_id == "fake-paper-thread"
    assert runtime.identity() == identity
    assert (root / "stdin.json").read_text(encoding="utf-8") == exchange.preserved_json(frozen["provider_request"])
    assert v1.sha((root / "schema.json").read_bytes()) == provider.manifest()["schema_sha256"]
    command, cwd, stdin = observed[0]
    manifest_command = [part.replace("{attempt_dir}", cwd) for part in provider.manifest()["argv_shape"]]
    assert command == [*runtime.launcher, *manifest_command[1:]]
    assert command[-1] == "-"
    assert stdin == exchange.preserved_json(frozen["provider_request"])
    assert (root / "launches.log").read_text().splitlines() == ["launch"]
    assert list((root / "attempts").iterdir()) == []
    assert provider.describe_status()["live_call_enabled"] is False
    if domain == "journal":
        saved = journal_execution.result(frozen, response)
        assert saved["raw_json"] == raw
        assert saved["validation"]["scientific_assessment"] == "NOT_ASSESSED"
    else:
        plan, _ = modules(domain)
        record = plan.transport_record(frozen, response, provider.manifest())
        plan.validate_transport(record, frozen["packet"])
        assert record["validation"]["execution"]["schema_version"] == "PREPARED_REVIEW_CLI_TRANSPORT_V2"
        assert record["validation"]["prepared_execution"]["full_document_review"] == "NOT_ESTABLISHED"


@pytest.mark.parametrize("profile", [None, "", "CODEX_JOURNAL_CLI_V3", "--model bad", ["exec"]])
def test_runtime_rejects_unknown_profile_before_workspace_or_launch(tmp_path, profile):
    runtime = paper_prior.runtime_at(tmp_path / "cli", paper_prior.contract())
    with pytest.raises(ValueError, match="profile_invalid"):
        runtime.run("{}", "{}", model="offline-requested-model", effort="low", timeout_seconds=30, profile=profile)
    assert not runtime.workspace.exists()
    assert not (tmp_path / "cli" / "launches.log").exists()


def test_runtime_default_is_exact_v1_and_identity_is_independent_of_profile(tmp_path, monkeypatch):
    runtime = paper_prior.runtime_at(tmp_path / "cli", paper_prior.contract())
    observed = []
    original_capture = process.capture
    def recording_capture(command, stdin, cwd, env, timeout):
        observed.append((command, str(cwd)))
        return original_capture(command, stdin, cwd, env, timeout)
    monkeypatch.setattr(process, "capture", recording_capture)
    identity = runtime.identity()
    runtime.run("{}", "{}", model="offline-requested-model", effort="low", timeout_seconds=30)
    command, cwd = observed[0]
    assert command == v1.argv(runtime.launcher, cwd, "offline-requested-model", "low")
    assert 'model_provider="openai"' in command
    assert 'forced_login_method="chatgpt"' not in command
    assert runtime.identity() == identity


@pytest.mark.parametrize("domain", ["journal", "paper"])
@pytest.mark.parametrize("mode,category", [("tool", "unknown"), ("missing", "unknown"), ("mismatch", "unknown"),
    ("malformed_failure", "unknown"), ("nonzero", "unknown"), ("failed", "failed"), ("duplicate", "output")])
def test_uncertain_or_invalid_completion_never_retries(tmp_path, domain, mode, category):
    provider, frozen, _ = adapter_at(tmp_path / "cli", domain, mode)
    with pytest.raises(ProviderError) as failure:
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    if category == "output":
        assert isinstance(failure.value, ProviderOutputError)
    elif category == "failed":
        assert isinstance(failure.value, ProviderReportedFailure)
    else:
        assert type(failure.value) is ProviderError
        assert "unknown" in str(failure.value) or "unconfirmed" in str(failure.value)
    assert "private diagnostic" not in str(failure.value)
    assert (tmp_path / "cli" / "launches.log").read_text().splitlines() == ["launch"]
    assert list((tmp_path / "cli" / "attempts").iterdir()) == []


@pytest.mark.parametrize("mode", ["timeout", "flood", "stderr_flood", "final_flood"])
def test_v2_runtime_retains_existing_timeout_and_capture_bounds_without_retry(tmp_path, mode):
    frozen = contract()
    runtime = journal_prior.runtime_at(tmp_path / "cli", mode)
    _, module = modules("journal")
    provider = module.CodexJournalV2Provider(frozen, runtime=runtime, reasoning_effort="low", timeout_seconds=.8)
    with pytest.raises(ProviderError):
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert (tmp_path / "cli" / "launches.log").read_text().splitlines() == ["launch"]
    assert list((tmp_path / "cli" / "attempts").iterdir()) == []


@pytest.mark.parametrize("domain", ["journal", "paper"])
def test_request_or_launcher_change_fails_before_process(tmp_path, domain):
    provider, frozen, _ = adapter_at(tmp_path / "cli", domain)
    request = ProviderRequest(**frozen["provider_request"])
    with pytest.raises(ValueError, match="adapter_mismatch"):
        provider.generate_json(replace(request, prompt=request.prompt + " "))
    script = tmp_path / "cli" / "fake_cli.py"
    script.write_text(script.read_text(encoding="utf-8") + "\n# changed", encoding="utf-8")
    with pytest.raises(ValueError, match="runtime_changed"):
        provider.generate_json(request)
    assert not (tmp_path / "cli" / "launches.log").exists()


@pytest.mark.parametrize("domain", ["journal", "paper"])
def test_runtime_uses_only_explicit_environment_without_reading_home_contents(tmp_path, monkeypatch, domain):
    provider, frozen, runtime = adapter_at(tmp_path / "cli", domain)
    original_open = Path.open
    explicit_home = Path(dict(runtime.environment)["CODEX_HOME"])
    def guarded_open(path, *args, **kwargs):
        if path.is_relative_to(explicit_home):
            pytest.fail("Runtime must not read account/config contents")
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-inherited-key-must-not-forward")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://invalid.example/never-call")
    original_capture = process.capture
    environments = []
    def recording_capture(command, stdin, cwd, env, timeout):
        environments.append(frozenset(env))
        return original_capture(command, stdin, cwd, env, timeout)
    monkeypatch.setattr(process, "capture", recording_capture)
    provider.validate_local_runtime()
    provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert environments == [frozenset(dict(runtime.environment))]
    assert not ({"OPENAI_API_KEY", "OPENAI_BASE_URL", "HTTP_PROXY", "HTTPS_PROXY", "NODE_OPTIONS"} & environments[0])


@pytest.mark.parametrize("change", ["packet_id", "citation", "schema", "extra"])
def test_journal_v2_validates_result_against_frozen_packet(tmp_path, change):
    provider, frozen, _ = adapter_at(tmp_path / "cli", "journal")
    output_path = tmp_path / "cli" / "output.json"
    output = json.loads(output_path.read_text(encoding="utf-8"))
    if change == "citation":
        output["sections"][0]["entries"][0]["citation_ids"] = ["invented"]
    elif change == "schema":
        output["schema_version"] = "JOURNAL_PROPOSAL_V2"
    else:
        output[change] = "changed"
    output_path.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ProviderOutputError):
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert (tmp_path / "cli" / "launches.log").read_text().splitlines() == ["launch"]


@pytest.mark.parametrize("change", ["prompt", "instructions", "temperature", "metadata", "packet", "schema", "execution_id"])
def test_journal_plan_rejects_detached_frozen_inputs(change):
    frozen = contract()
    if change in {"prompt", "instructions"}:
        frozen["provider_request"][change] += " "
    elif change == "temperature":
        frozen["provider_request"][change] = .7
    elif change == "metadata":
        frozen["provider_request"][change]["packet_id"] = "other"
    elif change == "packet":
        frozen["packet"]["topic_id"] = "other"
    elif change == "schema":
        frozen["provider_request"][change]["properties"]["title"]["maxLength"] = 999
        frozen["packet"]["output_schema"] = deepcopy(frozen["provider_request"][change])
    else:
        frozen[change] = "other"
    with pytest.raises(ValueError, match="configuration_invalid"):
        modules("journal")[0].build_plan(frozen, **OPTIONS)


@pytest.mark.parametrize("domain", ["journal", "paper"])
@pytest.mark.parametrize("name,value", [("timeout_seconds", True), ("timeout_seconds", 0), ("timeout_seconds", 121),
    ("timeout_seconds", float("nan")), ("reasoning_effort", 'low"'), ("runtime_identity", dict(IDENTITY, extra="a" * 64)),
    ("runtime_identity", dict(IDENTITY, launcher_sha256="wrong"))])
def test_configuration_limits_still_fail_closed(domain, name, value):
    frozen = contract() if domain == "journal" else paper_prior.contract()
    with pytest.raises(ValueError, match="configuration_invalid"):
        modules(domain)[0].build_plan(frozen, **(OPTIONS | {name: value}))


def test_journal_v1_binding_cannot_authorize_v2_and_default_dispatch_denies_without_database(tmp_path, monkeypatch):
    provider, frozen, runtime = adapter_at(tmp_path / "cli", "journal")
    from app.services.providers.codex_journal import CodexJournalProvider
    old = CodexJournalProvider(frozen, runtime=runtime, reasoning_effort="low", timeout_seconds=30)
    with pytest.raises(ValueError, match="adapter_mismatch"):
        provider.validate_journal_binding(frozen, provider.model, old.binding)
    provider.validate_journal_binding(frozen, provider.model, provider.binding)
    manifest = provider.manifest()
    manifest["forced_login_method"] = "api"
    assert provider.manifest()["forced_login_method"] == "chatgpt"
    frozen["actor_id"] = "different-owner"
    with pytest.raises(ValueError, match="adapter_mismatch"):
        provider.validate_journal_binding(frozen, provider.model)
    frozen["actor_id"] = "owner"
    monkeypatch.setattr(dispatch_service.admission, "replay", lambda *_: None)
    monkeypatch.setattr(dispatch_service.repository, "detail", lambda *_: dict(contract=frozen))
    monkeypatch.setattr(dispatch_service.admission, "admit", lambda *_args, **_kwargs: pytest.fail("No claim without authority"))
    controller = modules("journal")[1].journal_dispatcher(provider)
    try:
        with pytest.raises(ValueError, match="authority_unavailable"):
            controller.dispatch(frozen["topic_id"], frozen["job_id"], frozen["execution_id"],
                                Dispatch(request_id="deny", expected_sequence=0), "owner")
        assert controller.snapshot()["workers"] == []
    finally:
        assert controller.stop()
    assert not (tmp_path / "cli" / "launches.log").exists()


@pytest.mark.parametrize("change", ["v1", "retry_policy", "forced_login_method", "schema", "runtime", "hash"])
def test_paper_archive_rejects_cross_profile_or_manifest_mutation(tmp_path, change):
    provider, frozen, _ = adapter_at(tmp_path / "cli", "paper")
    record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    plan = modules("paper")[0]
    plan.validate_transport(record, frozen["packet"])
    with pytest.raises(ValueError, match="transport_invalid"):
        paper_v1.validate_transport(record, frozen["packet"])
    marker = record["validation"]["execution"]
    if change == "v1":
        marker["schema_version"] = paper_v1.TRANSPORT_SCHEMA
        marker["adapter_version"] = "1"
    elif change == "schema":
        marker["manifest"]["schema_version"] = "CODEX_PREPARED_REVIEW_CLI_V1"
    elif change == "runtime":
        marker["manifest"]["runtime_profile"] = "CODEX_JOURNAL_CLI_V1"
    elif change == "hash":
        marker["response_sha256"] = "0" * 64
    else:
        marker["manifest"][change] = "changed"
    marker["manifest_sha256"] = v1.sha(exchange.preserved_json(marker["manifest"]))
    with pytest.raises(ValueError, match="transport_invalid"):
        plan.validate_transport(record, frozen["packet"])


def test_v1_paper_archive_is_still_readable_and_rejected_by_v2_reader():
    frozen = paper_prior.contract()
    response = replace(paper_prior.prior.response(frozen), provider_name="codex_cli")
    _, _, manifest = paper_v1.build_plan(frozen, **OPTIONS)
    record = paper_v1.transport_record(frozen, response, manifest)
    paper_v1.validate_transport(record, frozen["packet"])
    with pytest.raises(ValueError, match="transport_invalid"):
        modules("paper")[0].validate_transport(record, frozen["packet"])
