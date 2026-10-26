"""Reading CLI observations cannot run a CLI, grant authority or change history."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.services import codex_cli_compatibility as checker
from app.services.journal_codex_preview import JournalCodexPreview
from tests import test_journal_codex_preview as prior

seeded = prior.seeded
workspace = prior.workspace
configured = prior.configured
source = prior.source
oidc_api = prior.oidc_api


@pytest.fixture
def observation(configured):
    app, client, endpoint, saved, config_path, _, *_ = configured
    preview = client.get(endpoint).json()
    value = dict(schema_version="CODEX_CLI_COMPATIBILITY_V1", profile="CODEX_JOURNAL_CLI_V1",
        runtime_identity=preview["manifest"]["runtime_identity"], requirements_sha256=checker.requirements_digest(),
        checked_at="2026-10-07T00:00:00Z", cli_version="codex-cli 0.162.0-alpha.2",
        status="CONFIGURATION_REJECTED", reason="config_rejected", execution_allowed=False, model_invoked=False,
        checks=[dict(name=name, exit_code=1 if name == "configuration" else 0, stdout_sha256="a" * 64)
                for name in ("version", "exec_help", "configuration")])
    path = config_path.parent / "compatibility.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    app.state.journal_codex_preview = JournalCodexPreview(str(config_path), str(path))
    return configured, endpoint.replace("codex-preview", "codex-compatibility"), path, value, saved


def test_compatibility_read_is_separate_and_unconfigured_by_default(configured):
    _, client, endpoint, *_ = configured
    response = client.get(endpoint.replace("codex-preview", "codex-compatibility"))
    assert response.status_code == 200
    value = response.json()
    assert value["status"] == "NOT_CHECKED"
    assert value["execution_allowed"] is False and value["model_invoked"] is False
    assert value["report"] is None


@pytest.mark.parametrize("status", ("CONFIGURATION_REJECTED", "CONFIGURATION_OBSERVED", "INCOMPLETE"))
def test_valid_observations_never_enable_execution_or_change_stored_state(observation, status):
    configured, endpoint, path, report, saved = observation
    _, client, *_ = configured
    report = deepcopy(report)
    report["status"] = status
    if status == "CONFIGURATION_OBSERVED":
        report["reason"] = None
        report["checks"][-1]["exit_code"] = 0
    elif status == "INCOMPLETE":
        report.update(cli_version=None, reason="invalid_version", checks=report["checks"][:1])
    path.write_text(json.dumps(report), encoding="utf-8")
    before = prior.stored_state()
    response = client.get(endpoint)
    assert response.status_code == 200
    value = response.json()
    assert value["status"] == status and value["execution_allowed"] is False and value["model_invoked"] is False
    assert value["report"] == report
    assert value["contract_digest"] == prior.digest(saved["contract"]) and value["observed_sequence"] == 0
    assert all(str(path) not in item for item in prior.public_strings(value))
    assert prior.stored_state() == before


@pytest.mark.parametrize("field", ("launcher_sha256", "environment_sha256", "workspace_sha256", "requirements_sha256"))
def test_observation_for_another_runtime_or_requirement_is_stale(observation, field):
    configured, endpoint, path, report, _ = observation
    _, client, *_ = configured
    target = report if field == "requirements_sha256" else report["runtime_identity"]
    target[field] = "f" * 64
    path.write_text(json.dumps(report), encoding="utf-8")
    value = client.get(endpoint).json()
    assert value["status"] == "STALE" and value["report"] is None
    assert value["execution_allowed"] is False


@pytest.mark.parametrize("case", ("malformed", "oversized", "duplicate", "permission", "positive_failed", "schema", "missing", "profile"))
def test_bad_observations_fail_closed_without_path_or_error_disclosure(observation, case):
    configured, endpoint, path, report, _ = observation
    app, client, _, _, config_path, config, *_ = configured
    if case == "malformed":
        path.write_bytes(b"\xff")
    elif case == "oversized":
        path.write_bytes(b" " * 65_537)
    elif case == "duplicate":
        path.write_text(json.dumps(report)[:-1] + ',"status":"CONFIGURATION_OBSERVED"}', encoding="utf-8")
    elif case == "missing":
        path.unlink()
    elif case == "profile":
        path = Path(config["environment"]["CODEX_HOME"]) / "auth.json"
        app.state.journal_codex_preview = JournalCodexPreview(str(config_path), str(path))
    else:
        if case == "permission":
            report["execution_allowed"] = True
        elif case == "positive_failed":
            report.update(status="CONFIGURATION_OBSERVED", reason=None)
        elif case == "schema":
            report["schema_version"] = "future"
        path.write_text(json.dumps(report), encoding="utf-8")
    value = client.get(endpoint).json()
    assert value["status"] == "INVALID" and value["report"] is None
    assert value["reason"] == "report_unreadable"
    assert all(str(path) not in item for item in prior.public_strings(value))


def test_drift_disables_observation_and_restoring_old_bytes_does_not_reset_runtime(observation):
    configured, endpoint, _, _, _ = observation
    _, client, _, _, config_path, _, *_ = configured
    assert client.get(endpoint).json()["status"] == "CONFIGURATION_REJECTED"
    original = config_path.read_bytes()
    config_path.write_bytes(original + b" ")
    value = client.get(endpoint).json()
    assert value["status"] == "UNAVAILABLE" and value["report"] is None
    config_path.write_bytes(original)
    assert client.get(endpoint).json()["status"] == "UNAVAILABLE"


def test_replacing_report_and_restarting_keeps_the_original_execution(observation):
    configured, endpoint, path, _, saved = observation
    app, client, _, _, config_path, _, *_ = configured
    first = client.get(endpoint).json()
    path.write_text("{broken", encoding="utf-8")
    assert client.get(endpoint).json()["status"] == "INVALID"
    path.write_text(json.dumps(first["report"]), encoding="utf-8")
    app.state.journal_codex_preview = JournalCodexPreview(str(config_path), str(path))
    assert client.get(endpoint).json() == first
    assert client.get(endpoint.removesuffix("/codex-compatibility")).json() == saved


def test_configuration_path_is_only_captured_at_creation(configured, monkeypatch):
    _, _, _, _, config_path, _, *_ = configured
    monkeypatch.setenv(prior.service.CONFIG_ENV, str(config_path))
    monkeypatch.setenv(prior.service.COMPATIBILITY_ENV, str(config_path.parent / "absent.json"))
    def fail(*args, **kwargs):
        pytest.fail("Creation must not read preview or report files")
    with monkeypatch.context() as constructor_patch:
        constructor_patch.setattr(Path, "open", fail)
        assert isinstance(JournalCodexPreview.from_environment(), JournalCodexPreview)


@pytest.mark.parametrize("topic,subject,admin,allowed", (
    ("default-xrml", "reader", False, False), ("default-xrml", "admin", True, True),
    ("private-topic", "owner", False, True), ("private-topic", "outsider", False, False),
    ("private-topic", "admin", True, False),
))
def test_oidc_update_acl_precedes_any_report_read(oidc_api, tmp_path, monkeypatch, topic, subject, admin, allowed):
    app, client, user, jobs = oidc_api
    path, _ = prior.local_configuration(tmp_path / "authorized")
    app.state.journal_codex_preview = JournalCodexPreview(str(path), str(path.parent / "missing-report.json"))
    job = jobs[topic]
    saved, _ = prior.repo.prepare(topic, job["job_id"], prior.Prepare(request_id="compatibility",
        expected_input_digest=job["input_digest"], provider_name="codex_cli", model="fixture-model"), "original-owner")
    user["value"] = prior.ProductUser(subject, subject, None, "oidc", is_admin=admin)
    endpoint = f"/api/topics/{topic}/local-synthesis/{job['job_id']}/journal-executions/{saved['contract']['execution_id']}/codex-compatibility"
    if not allowed:
        monkeypatch.setattr(JournalCodexPreview, "compatibility", lambda *_: pytest.fail("Unauthorized observation read"))
    before = prior.stored_state()
    response = client.get(endpoint)
    assert response.status_code == (200 if allowed else 404)
    if allowed:
        assert response.json()["status"] == "INVALID" and response.json()["actor_id"] == "original-owner"
    assert prior.stored_state() == before
