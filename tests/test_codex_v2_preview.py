"""A V2 syntax observation cannot authorize execution or qualify a V1 plan."""
import json

import pytest
from pydantic import ValidationError

from app.services import codex_cli_compatibility as checker
from app.services.journal_codex_preview import JournalCodexPreview
from tests import test_codex_cli_compatibility as cli
from tests import test_journal_codex_preview as prior

seeded = prior.seeded
workspace = prior.workspace
configured = prior.configured


def test_v2_configuration_probes_exact_frozen_profile(tmp_path):
    path, config = cli.fixture_config(tmp_path / "fake")
    config["schema_version"] = "CODEX_JOURNAL_PREVIEW_CONFIG_V2"
    path.write_text(json.dumps(config), encoding="utf-8")
    result = checker.inspect(path, tmp_path / "scratch")
    from app.services.providers.codex_cli_profile_v2 import CONFIG
    assert result.schema_version == "CODEX_CLI_COMPATIBILITY_V2"
    assert result.profile == "CODEX_JOURNAL_CLI_V2"
    assert result.status == "CONFIGURATION_OBSERVED"
    assert not result.execution_allowed and not result.model_invoked
    assert result.requirements_sha256 == checker.requirements_digest("CODEX_JOURNAL_CLI_V2")
    assert result.requirements_sha256 != checker.requirements_digest()
    assert cli.calls(path)[-1]["args"] == ["features", "list", *[part for setting in CONFIG for part in ("-c", setting)]]
    assert len(cli.calls(path)) == 3
    assert list((tmp_path / "scratch").iterdir()) == []
    assert checker.CompatibilityReport.model_validate_json(result.model_dump_json()) == result
    for field, value in (("profile", "CODEX_JOURNAL_CLI_V1"), ("schema_version", "CODEX_CLI_COMPATIBILITY_V1")):
        payload = result.model_dump(mode="json") | {field: value}
        with pytest.raises(ValidationError):
            checker.CompatibilityReport.model_validate_json(json.dumps(payload))
    checker.write_report(tmp_path / "report.json", result, config_path=path, scratch_root=tmp_path / "scratch")
    config["schema_version"] = "CODEX_JOURNAL_PREVIEW_CONFIG_V1"
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        checker.write_report(tmp_path / "wrong-profile.json", result, config_path=path, scratch_root=tmp_path / "scratch")
    assert not (tmp_path / "wrong-profile.json").exists()


def test_v2_preview_preserves_history_and_rejects_v1_observation(configured):
    app, client, endpoint, saved, path, config, *_ = configured
    original = client.get(endpoint).json()
    assert original["manifest"]["schema_version"] == "CODEX_JOURNAL_CLI_V1"
    before = prior.stored_state()
    config["schema_version"] = "CODEX_JOURNAL_PREVIEW_CONFIG_V2"
    path.write_text(json.dumps(config), encoding="utf-8")
    assert client.get(endpoint).json()["reason"] == "codex_preview_configuration_changed"
    report_path = path.parent / "report.json"
    app.state.journal_codex_preview = JournalCodexPreview(str(path), str(report_path))
    preview = client.get(endpoint).json()
    assert preview["available"] is True
    assert preview["manifest"]["schema_version"] == "CODEX_JOURNAL_CLI_V2"
    assert preview["binding"]["adapter_version"] == "2"
    assert preview["preview_digest"] != original["preview_digest"]
    assert preview["manifest"]["runtime_identity"] == original["manifest"]["runtime_identity"]
    report = dict(schema_version="CODEX_CLI_COMPATIBILITY_V1", profile="CODEX_JOURNAL_CLI_V1",
        runtime_identity=preview["manifest"]["runtime_identity"], requirements_sha256=checker.requirements_digest(),
        checked_at="2026-10-07T00:00:00Z", cli_version="codex-cli 0.162.0-alpha.2",
        status="CONFIGURATION_OBSERVED", reason=None, execution_allowed=False, model_invoked=False,
        checks=[dict(name=name, exit_code=0, stdout_sha256="a" * 64) for name in ("version", "exec_help", "configuration")])
    report_path.write_text(json.dumps(report), encoding="utf-8")
    target = endpoint.replace("codex-preview", "codex-compatibility")
    assert client.get(target).json()["status"] == "STALE"
    report.update(schema_version="CODEX_CLI_COMPATIBILITY_V2", profile="CODEX_JOURNAL_CLI_V2",
                  requirements_sha256=checker.requirements_digest("CODEX_JOURNAL_CLI_V2"))
    report_path.write_text(json.dumps(report), encoding="utf-8")
    result = client.get(target).json()
    assert result["status"] == "CONFIGURATION_OBSERVED" and not result["execution_allowed"]
    assert result["report"] == report
    assert client.get(endpoint.removesuffix("/codex-preview")).json() == saved
    assert prior.stored_state() == before
    app.state.journal_codex_preview = JournalCodexPreview(str(path), str(report_path))
    assert client.get(endpoint).json() == preview
    assert client.get(target).json() == result
