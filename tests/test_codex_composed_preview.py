"""An explicitly composed preview shares V2 runtime identity without execution."""
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app.auth.security import require_product_user
from app.domain.temporal_memory import digest
from app.main import create_app
from app.services.codex_cli_compatibility import requirements_digest
from app.services.journal_codex_preview import JournalCodexPreview, CONFIG_ENV, COMPATIBILITY_ENV
from app.services.providers.codex_journal_process import CliRuntime
from app.services.providers.codex_journal_v2 import CodexJournalV2Provider
from tests import test_journal_codex_preview as prior

seeded = prior.seeded
workspace = prior.workspace
configured = prior.configured


def compose(configured, *, report=None):
    app, _, _, _, _, config, *_ = configured
    runtime = CliRuntime(tuple(config["launcher"]), config["environment"], Path(config["workspace"]))
    preview = JournalCodexPreview.from_runtime(runtime, reasoning_effort="low", timeout_seconds=30,
                                               compatibility_report_path=report)
    app.state.journal_codex_preview = preview
    return runtime, preview


def test_composed_preview_matches_v2_adapter_without_environment_or_execution(configured, monkeypatch):
    app, client, endpoint, saved, path, config, url, *_ = configured
    monkeypatch.setenv(CONFIG_ENV, str(path.parent / "unrelated-config.json"))
    monkeypatch.setenv(COMPATIBILITY_ENV, str(Path(config["environment"]["CODEX_HOME"]) / "auth.json"))
    runtime, preview = compose(configured)
    before = prior.stored_state()
    response = client.get(endpoint)
    assert response.status_code == 200, response.text
    observed = response.json()
    adapter = CodexJournalV2Provider(saved["contract"], runtime=runtime, reasoning_effort="low", timeout_seconds=30)
    assert observed["available"] and observed["preview_only"] and not observed["execution_allowed"]
    assert observed["manifest"] == adapter.manifest()
    assert observed["binding"] == adapter.binding.model_dump()
    assert observed["binding"]["command_shape_sha256"] == digest(observed["manifest"])
    compatibility = client.get(endpoint.replace("codex-preview", "codex-compatibility")).json()
    assert compatibility["status"] == "NOT_CHECKED" and compatibility["reason"] == "report_not_configured"
    assert compatibility["report"] is None and not compatibility["model_invoked"]
    for private in [str(path), *config["launcher"], config["workspace"], *config["environment"].values(), "must never be read"]:
        assert all(private not in value for value in prior.public_strings(observed))
    assert app.state.journal_codex_preview is preview
    assert client.get(url).json()["items"][0]["attempt"] == 0
    assert prior.stored_state() == before
    assert not Path(config["workspace"]).exists()


def test_explicit_preview_injection_overrides_environment_but_default_remains_lazy(configured, monkeypatch):
    _, _, endpoint, _, path, _, *_ = configured
    _, preview = compose(configured)
    monkeypatch.setenv(CONFIG_ENV, str(path))  # Existing V1 configuration.
    def no_read(*_args):
        pytest.fail("Application construction must not read preview configuration")
    with monkeypatch.context() as patch:
        patch.setattr(JournalCodexPreview, "_read_configuration", no_read)
        explicit = create_app(journal_codex_preview=preview)
        default = create_app()
    assert explicit.state.journal_codex_preview is preview
    assert default.state.journal_codex_preview is not preview
    for app, schema in ((explicit, "CODEX_JOURNAL_CLI_V2"), (default, "CODEX_JOURNAL_CLI_V1")):
        app.dependency_overrides[require_product_user] = lambda: None
        with TestClient(app) as client:
            value = client.get(endpoint).json()
            assert value["available"] and value["manifest"]["schema_version"] == schema
            assert not value["execution_allowed"]


@pytest.mark.parametrize("change", ["launcher", "home"])
def test_composed_runtime_drift_latches_after_restoration(configured, change):
    app, client, endpoint, _, _, config, *_ = configured
    runtime, _ = compose(configured)
    first = client.get(endpoint).json()
    assert first["available"]
    before = prior.stored_state()
    launcher = Path(config["launcher"][0])
    original = launcher.read_bytes()
    home = Path(config["environment"]["CODEX_HOME"])
    moved = home.with_name("moved-private-profile")
    if change == "launcher":
        launcher.write_bytes(b"changed synthetic launcher")
    else:
        home.rename(moved)
    changed = client.get(endpoint).json()
    assert not changed["available"] and changed["reason"] == "codex_preview_runtime_changed"
    assert changed["manifest"] is None and changed["binding"] is None
    if change == "launcher":
        launcher.write_bytes(original)
    else:
        moved.rename(home)
    assert client.get(endpoint).json() == changed
    app.state.journal_codex_preview = JournalCodexPreview.from_runtime(runtime, reasoning_effort="low", timeout_seconds=30)
    assert client.get(endpoint).json() == first
    assert prior.stored_state() == before


def test_composed_preview_checks_explicit_report_profile_and_runtime(configured):
    app, client, endpoint, _, path, _, *_ = configured
    report_path = path.parent / "explicit-report.json"
    _, preview = compose(configured, report=str(report_path))
    manifest = client.get(endpoint).json()["manifest"]
    report = dict(schema_version="CODEX_CLI_COMPATIBILITY_V2", profile="CODEX_JOURNAL_CLI_V2",
        runtime_identity=manifest["runtime_identity"], requirements_sha256=requirements_digest("CODEX_JOURNAL_CLI_V2"),
        checked_at="2026-10-08T00:00:00Z", cli_version="codex-cli 0.162.0-alpha.2",
        status="CONFIGURATION_OBSERVED", reason=None, execution_allowed=False, model_invoked=False,
        checks=[dict(name=name, exit_code=0, stdout_sha256="a" * 64) for name in ("version", "exec_help", "configuration")])
    report_path.write_text(json.dumps(report), encoding="utf-8")
    target = endpoint.replace("codex-preview", "codex-compatibility")
    observed = client.get(target).json()
    assert observed["status"] == "CONFIGURATION_OBSERVED" and observed["report"] == report
    assert not observed["execution_allowed"] and not observed["model_invoked"]
    report.update(schema_version="CODEX_CLI_COMPATIBILITY_V1", profile="CODEX_JOURNAL_CLI_V1",
                  requirements_sha256=requirements_digest())
    report_path.write_text(json.dumps(report), encoding="utf-8")
    assert client.get(target).json()["status"] == "STALE"
    report.update(schema_version="CODEX_CLI_COMPATIBILITY_V2", profile="CODEX_JOURNAL_CLI_V2",
                  requirements_sha256=requirements_digest("CODEX_JOURNAL_CLI_V2"),
                  runtime_identity=manifest["runtime_identity"] | dict(launcher_sha256="b" * 64))
    report_path.write_text(json.dumps(report), encoding="utf-8")
    assert client.get(target).json()["status"] == "STALE"
    assert app.state.journal_codex_preview is preview


@pytest.mark.parametrize("field,value", [("reasoning_effort", "invalid"), ("timeout_seconds", True),
    ("timeout_seconds", 0), ("timeout_seconds", 121), ("timeout_seconds", float("nan"))])
def test_composed_options_reuse_strict_v2_validation(configured, field, value):
    _, _, _, _, _, config, *_ = configured
    runtime = CliRuntime(tuple(config["launcher"]), config["environment"], Path(config["workspace"]))
    with pytest.raises(ValueError):
        JournalCodexPreview.from_runtime(runtime, **(dict(reasoning_effort="low", timeout_seconds=30) | {field: value}))
