"""Explicit app composition uses disposable files and never a real account/CLI."""
from pathlib import Path
import subprocess

import pytest

from app.auth.config import get_auth_settings
from app.services import codex_app_composition as service
from app.services.providers.codex_journal_process import CliRuntime


@pytest.fixture
def configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("SESSION_SECRET", "synthetic-composition-secret-0123456789abcdef")
    monkeypatch.setenv("TOPIC_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("XRML_RUN_EXECUTOR_MODE", "disabled")
    get_auth_settings.cache_clear()
    launcher = tmp_path / "explicit-fixture.exe"
    launcher.write_bytes(b"non-executable fixture; must never be launched")
    home = tmp_path / "private-home"
    home.mkdir()
    (home / "auth.json").write_text("must not be read", encoding="utf-8")
    (home / "config.toml").write_text("must not be read", encoding="utf-8")
    value = dict(schema_version="CODEX_V2_APP_CONFIG_V1", runtime=dict(
        schema_version="CODEX_JOURNAL_PREVIEW_CONFIG_V2", launcher=[str(launcher)],
        environment={"CODEX_HOME": str(home), "TEMP": str(tmp_path)}, workspace=str(tmp_path / "attempts"),
        reasoning_effort="low", timeout_seconds=30), paper_model="explicit-fixture-model",
        paper_max_output_tokens=1024, approvers=["explicit-owner"],
        trusted_issuers=[dict(issuer_id="explicit-issuer", policy_version="explicit-policy")])
    yield value
    get_auth_settings.cache_clear()


def test_default_app_does_not_construct_a_runtime_or_call_a_source(monkeypatch):
    def prohibited(*args, **kwargs):
        pytest.fail("default app must not configure execution")
    monkeypatch.setattr(CliRuntime, "__init__", prohibited)
    app = service.create_codex_app()
    assert app.state.paper_codex_runtime is None
    assert app.state.journal_codex_runtime is None
    assert app.state.paper_approval_runtime is None
    assert app.state.journal_approval_runtime is None
    assert app.state.paper_trial_runtime is None
    with pytest.raises(ValueError, match="codex_app_configuration_required"):
        service.create_codex_app(paper_source=prohibited)
    with pytest.raises(ValueError, match="codex_app_configuration_required"):
        service.create_codex_app(paper_trial_source=prohibited)


def test_explicit_configuration_shares_one_runtime_without_issuing_or_starting(configuration, monkeypatch):
    calls = []
    def source(contract):
        calls.append(contract)
        pytest.fail("construction must not query authority or issue a proof")
    def prohibited(*args, **kwargs):
        pytest.fail("construction must not launch a process")
    monkeypatch.setattr(subprocess, "Popen", prohibited)
    monkeypatch.setattr(CliRuntime, "run", prohibited)
    monkeypatch.setenv("XRML_CODEX_JOURNAL_PREVIEW_CONFIG", "unrelated-environment-path")
    monkeypatch.setenv("XRML_CODEX_JOURNAL_COMPATIBILITY_REPORT", "unrelated-environment-report")
    home = Path(configuration["runtime"]["environment"]["CODEX_HOME"])
    original_open = Path.open
    def guarded(path, *args, **kwargs):
        if path.resolve().is_relative_to(home):
            pytest.fail("composition must not read profile contents")
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guarded)
    app = service.create_codex_app(service.CodexAppConfig.model_validate(configuration),
        paper_source=source, journal_source=source, paper_trial_source=source)
    runtime = app.state.paper_codex_runtime._runtime
    assert runtime is app.state.journal_codex_runtime._runtime
    assert runtime is app.state.journal_codex_preview._runtime
    assert app.state.journal_codex_preview._compatibility_report_path is None
    assert app.state.paper_codex_runtime._model == "explicit-fixture-model"
    assert app.state.paper_codex_runtime.input_origin == "PROVIDER_RESPONSE"
    assert app.state.paper_approval_runtime is not None
    assert app.state.journal_approval_runtime is not None
    assert app.state.paper_trial_runtime._paper is app.state.paper_codex_runtime
    assert calls == [] and not Path(configuration["runtime"]["workspace"]).exists()


def test_absent_sources_stay_disabled(configuration):
    app = service.create_codex_app(service.CodexAppConfig.model_validate(configuration))
    assert app.state.paper_approval_runtime is None
    assert app.state.journal_approval_runtime is None
    assert app.state.paper_trial_runtime is None
    assert app.state.paper_codex_runtime is not None
    assert app.state.journal_codex_runtime is not None


@pytest.mark.parametrize("mode,secret", [("disabled", "x" * 40), ("oidc", "short")])
def test_authentication_must_be_configured_before_runtime_files_are_read(configuration, monkeypatch, mode, secret):
    config = service.CodexAppConfig.model_validate(configuration)
    monkeypatch.setenv("AUTH_MODE", mode)
    monkeypatch.setenv("SESSION_SECRET", secret)
    get_auth_settings.cache_clear()
    def prohibited(*args, **kwargs):
        pytest.fail("invalid app identity must fail before runtime construction")
    monkeypatch.setattr(CliRuntime, "__init__", prohibited)
    with pytest.raises(ValueError, match="codex_app_authentication_required"):
        service.create_codex_app(config)


def test_launcher_in_profile_is_rejected_without_reading_it(configuration, monkeypatch):
    home = Path(configuration["runtime"]["environment"]["CODEX_HOME"])
    configuration["runtime"]["launcher"] = [str(home / "auth.json")]
    config = service.CodexAppConfig.model_validate(configuration)
    original_open = Path.open
    def guarded(path, *args, **kwargs):
        if path.resolve().is_relative_to(home):
            pytest.fail("never hash credentials as a launcher")
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guarded)
    with pytest.raises(ValueError, match="codex_app_runtime_invalid"):
        service.create_codex_app(config)


@pytest.mark.parametrize("field,value", [
    ("approvers", []), ("approvers", [" owner"]), ("approvers", ["owner", "owner"]),
    ("trusted_issuers", []), ("trusted_issuers", [dict(issuer_id="issuer", policy_version=" ")]),
    ("paper_model", ""), ("paper_max_output_tokens", True), ("paper_max_output_tokens", 100),
    ("extra", "not allowed"),
])
def test_configuration_rejects_ambiguous_or_invalid_inputs(configuration, field, value):
    configuration[field] = value
    with pytest.raises(ValueError):
        service.CodexAppConfig.model_validate(configuration)


def test_v1_configuration_cannot_silently_become_a_v2_app(configuration):
    configuration["runtime"]["schema_version"] = "CODEX_JOURNAL_PREVIEW_CONFIG_V1"
    with pytest.raises(ValueError):
        service.CodexAppConfig.model_validate(configuration)


def test_caller_collection_mutations_cannot_retarget_existing_app(configuration):
    config = service.CodexAppConfig.model_validate(configuration)
    app = service.create_codex_app(config, paper_source=lambda _: None, journal_source=lambda _: None)
    identity = app.state.paper_codex_runtime._runtime.identity()
    config.approvers.append("another-actor")
    config.trusted_issuers.clear()
    config.runtime.environment["CODEX_HOME"] = "changed"
    assert app.state.paper_approval_runtime._approvers == frozenset({"explicit-owner"})
    assert app.state.journal_approval_runtime._approvers == frozenset({"explicit-owner"})
    assert app.state.paper_codex_runtime._runtime.identity() == identity
    assert app.state.journal_codex_runtime._trusted == frozenset({("explicit-issuer", "explicit-policy")})
    with pytest.raises(ValueError):
        service.create_codex_app(config)


@pytest.mark.parametrize("domain", ["paper", "journal"])
def test_reserved_offline_issuer_cannot_authorize_composed_execution(configuration, domain):
    from app.services import review_transport_offline, journal_offline
    offline = review_transport_offline if domain == "paper" else journal_offline
    configuration["trusted_issuers"] = [dict(issuer_id=offline.ISSUER, policy_version=offline.POLICY)]
    with pytest.raises(ValueError):
        service.create_codex_app(service.CodexAppConfig.model_validate(configuration))


@pytest.mark.parametrize("domain", ["paper", "journal"])
def test_noncallable_source_is_rejected(configuration, domain):
    with pytest.raises(ValueError):
        service.create_codex_app(service.CodexAppConfig.model_validate(configuration), **{domain + "_source": object()})
