"""Local Codex plan inspection never invokes a CLI or grants execution authority."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import subprocess
from threading import Barrier
import urllib.request

from fastapi import HTTPException
from fastapi.testclient import TestClient
import httpx
import pytest

from app.auth.security import require_product_user
from app.auth.security_types import ProductUser
from app.db import bootstrap
from app.domain.journal_execution import Prepare
from app.domain.temporal_memory import digest
from app.main import create_app
from app.repositories import journal_authority, journal_executions as repo
from app.services import journal_codex_preview as service, model_connections
from app.services.journal_dispatch import JournalDispatcher
from app.services.providers.codex_journal import CodexJournalProvider
from app.services.providers.codex_journal_process import CliRuntime
from tests import test_journal_preparation as prior
from tests import test_journal_connection_preparation as selected_prior

seeded = prior.seeded
workspace = prior.workspace
source = selected_prior.source
oidc_api = selected_prior.oidc_api


def stored_state():
    with closing(bootstrap.connect_metadata_index(read_only=True)) as connection:
        return tuple(connection.iterdump())


def public_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from public_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from public_strings(item)


def local_configuration(root):
    root.mkdir()
    launcher = root / "synthetic-launcher.exe"
    launcher.write_bytes(b"Preview-only fixture: never executable or launched")
    home = root / "private-profile"
    home.mkdir()
    (home / "auth.json").write_text("must never be read", encoding="utf-8")
    (home / "config.toml").write_text("must never be read", encoding="utf-8")
    value = dict(schema_version="CODEX_JOURNAL_PREVIEW_CONFIG_V1", launcher=[str(launcher)],
        environment={"CODEX_HOME": str(home), "TEMP": str(root), "LANG": "private-language-value"},
        workspace=str(root / "attempts"), reasoning_effort="low", timeout_seconds=30)
    path = root / "preview.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path, value


@pytest.fixture
def configured(workspace, tmp_path, monkeypatch):
    app, client, url, body, topic, job = workspace
    config_path, config = local_configuration(tmp_path / "preview-runtime")
    app.state.journal_codex_preview = service.JournalCodexPreview(str(config_path))
    saved = client.post(url, json=body | dict(provider_name="codex_cli")).json()["execution"]
    endpoint = url + "/" + saved["contract"]["execution_id"] + "/codex-preview"
    def prohibited(*args, **kwargs):
        pytest.fail("Preview must never execute, discover credentials, issue authority or use network")
    monkeypatch.setattr(subprocess, "Popen", prohibited)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", prohibited)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", prohibited)
    monkeypatch.setattr(urllib.request, "urlopen", prohibited)
    monkeypatch.setattr(CliRuntime, "run", prohibited)
    monkeypatch.setattr(JournalDispatcher, "dispatch", prohibited)
    monkeypatch.setattr(journal_authority, "register", prohibited)
    # Config and launcher reads are explicit, while CLI profile contents are not.
    original_open = Path.open
    home = Path(config["environment"]["CODEX_HOME"])
    def guarded_open(path, *args, **kwargs):
        if path.resolve().is_relative_to(home):
            raise AssertionError("Preview must never read a CLI profile's contents")
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guarded_open)
    return app, client, endpoint, saved, config_path, config, url, body, topic, job


def test_default_preview_is_unavailable_without_configuration_or_execution(workspace):
    _, client, url, body, _, _ = workspace
    saved = client.post(url, json=body | dict(provider_name="codex_cli")).json()["execution"]
    endpoint = url + "/" + saved["contract"]["execution_id"] + "/codex-preview"
    response = client.get(endpoint)
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["reason"] == "codex_preview_not_configured"
    assert value["available"] is False and value["execution_allowed"] is False and value["preview_only"] is True
    assert value["manifest"] is None and value["binding"] is None and value["preview_digest"] is None


def test_creation_only_captures_path_and_defers_preview_filesystem_work(workspace, tmp_path, monkeypatch):
    _, client, url, body, _, _ = workspace
    path, _ = local_configuration(tmp_path / "startup-runtime")
    monkeypatch.setenv(service.CONFIG_ENV, str(path))
    def no_read(*args):
        pytest.fail("App creation must not inspect preview configuration or runtime")
    with monkeypatch.context() as patch:
        patch.setattr(service.JournalCodexPreview, "_read_configuration", no_read)
        app = create_app()
    assert isinstance(app.state.journal_codex_preview, service.JournalCodexPreview)
    saved = client.post(url, json=body | dict(provider_name="codex_cli")).json()["execution"]
    endpoint = url + "/" + saved["contract"]["execution_id"] + "/codex-preview"
    # Changing the process setting cannot retarget an already-created application.
    monkeypatch.setenv(service.CONFIG_ENV, str(tmp_path / "different-missing.json"))
    app.dependency_overrides[require_product_user] = lambda: None
    with TestClient(app) as restarted:
        assert restarted.get(endpoint).json()["available"] is True


def test_exact_existing_plan_binding_private_path_redaction_and_no_state_changes(configured):
    _, client, endpoint, saved, config_path, config, url, _, _, _ = configured
    before = stored_state()
    raw_config = config_path.read_bytes()
    response = client.get(endpoint)
    assert response.status_code == 200, response.text
    result = response.json()
    runtime = CliRuntime(tuple(config["launcher"]), config["environment"], Path(config["workspace"]))
    adapter = CodexJournalProvider(saved["contract"], runtime=runtime, reasoning_effort="low", timeout_seconds=30)
    assert result == dict(schema_version="CODEX_JOURNAL_PREVIEW_V1", preview_only=True, execution_allowed=False,
        execution_id=saved["contract"]["execution_id"], contract_digest=digest(saved["contract"]),
        actor_id="local-internal-user", observed_sequence=0, available=True, reason=None,
        manifest=adapter.manifest(), binding=adapter.binding.model_dump(), preview_digest=result["preview_digest"])
    assert len(result["preview_digest"]) == 64
    for private in [str(config_path), *config["launcher"], config["workspace"], *config["environment"].values(), "must never be read"]:
        assert all(private not in value for value in public_strings(result))
    assert client.get(endpoint).json() == result
    assert client.get(url).json()["items"][0]["attempt"] == 0
    assert stored_state() == before and config_path.read_bytes() == raw_config
    assert not Path(config["workspace"]).exists()


def test_requery_restart_and_cancellation_keep_plan_identity_but_report_current_sequence(configured, monkeypatch):
    _, client, endpoint, saved, path, config, _, _, _, _ = configured
    first = client.get(endpoint).json()
    assert first["available"]
    detail = endpoint.removesuffix("/codex-preview")
    cancelled = client.post(detail + "/control", json=dict(request_id="cancel", action="cancel", expected_sequence=0)).json()["execution"]
    before = stored_state()
    second = client.get(endpoint).json()
    assert second == first | dict(observed_sequence=1)
    assert cancelled["current"]["attempt"] == 0
    monkeypatch.setenv(service.CONFIG_ENV, str(path))
    reopened = create_app()
    reopened.dependency_overrides[require_product_user] = lambda: None
    with TestClient(reopened) as other:
        assert other.get(endpoint).json() == second
        assert other.get(detail).json() == cancelled
    assert stored_state() == before and not Path(config["workspace"]).exists()
    assert saved["contract"] == cancelled["contract"]


def test_current_connection_settings_are_not_consulted(configured, monkeypatch):
    _, client, endpoint, saved, _, _, _, _, _, _ = configured
    def no_settings():
        pytest.fail("Preview must use only the already-frozen request")
    monkeypatch.setattr(model_connections, "read", no_settings)
    monkeypatch.setenv("XRML_PROVIDER", "disabled")
    value = client.get(endpoint).json()
    assert value["available"] and value["manifest"]["model"] == saved["contract"]["specification"]["model"]


def test_launcher_inside_profile_is_rejected_before_reading_profile_contents(configured):
    _, client, endpoint, _, path, config, _, _, _, _ = configured
    changed = config | dict(launcher=[str(Path(config["environment"]["CODEX_HOME"]) / "auth.json")])
    path.write_text(json.dumps(changed), encoding="utf-8")
    value = client.get(endpoint).json()
    assert value["reason"] == "codex_preview_configuration_invalid" and not value["available"]


def test_launcher_alias_target_drift_into_profile_is_rejected_before_reading_profile_contents(configured, monkeypatch):
    _, client, endpoint, _, _, config, _, _, _, _ = configured
    assert client.get(endpoint).json()["available"]
    launcher = Path(config["launcher"][0])
    target = Path(config["environment"]["CODEX_HOME"]) / "auth.json"
    original = Path.resolve
    # Model a changed parent alias without requiring OS symlink privileges.
    # The profile-content guard fails if the old verifier tries to hash it.
    def redirected(path, *args, **kwargs):
        return target if path == launcher else original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", redirected)
        value = client.get(endpoint).json()
    assert value["reason"] == "codex_preview_runtime_changed" and not value["available"]
    assert client.get(endpoint).json() == value


def test_unsupported_provider_default_missing_and_unauthorized_routes_do_not_inspect_config(configured, monkeypatch, seeded):
    app, client, endpoint, saved, _, _, url, body, topic, job = configured
    def no_io(*args):
        pytest.fail("This request must not inspect the preview runtime")
    monkeypatch.setattr(service.JournalCodexPreview, "_read_configuration", no_io)
    other = client.post(url, json=body | dict(request_id="other-provider", provider_name="openai")).json()["execution"]
    other_url = url + "/" + other["contract"]["execution_id"] + "/codex-preview"
    assert client.get(other_url).json()["reason"] == "codex_preview_provider_unsupported"
    app.state.journal_codex_preview = service.JournalCodexPreview(None)
    assert client.get(endpoint).json()["reason"] == "codex_preview_not_configured"
    for invalid in (endpoint.replace(topic, seeded.TOPIC_B_ID), endpoint.replace(job["job_id"], "missing-job"),
                    endpoint.replace(saved["contract"]["execution_id"], "absent")):
        assert client.get(invalid).status_code == 404
    def unauthenticated():
        raise HTTPException(401)
    app.dependency_overrides[require_product_user] = unauthenticated
    assert client.get(endpoint).status_code == 401


@pytest.mark.parametrize("change", ("bytes", "removed", "invalid"))
def test_configuration_drift_latches_until_restart_even_after_original_is_restored(configured, change):
    app, client, endpoint, _, path, _, _, _, _, _ = configured
    first = client.get(endpoint).json()
    assert first["available"]
    original = path.read_bytes()
    if change == "bytes":
        path.write_bytes(original + b" ")
    elif change == "removed":
        path.unlink()
    else:
        path.write_bytes(b"{invalid")
    before = stored_state()
    changed = client.get(endpoint).json()
    assert changed["reason"] == "codex_preview_configuration_changed"
    assert changed["manifest"] is None and changed["preview_digest"] is None
    path.write_bytes(original)
    assert client.get(endpoint).json() == changed
    app.state.journal_codex_preview = service.JournalCodexPreview(str(path))
    assert client.get(endpoint).json() == first
    assert stored_state() == before


@pytest.mark.parametrize("change", ("launcher", "missing_launcher", "workspace", "home"))
def test_runtime_drift_latches_until_restart(configured, change):
    app, client, endpoint, _, path, config, _, _, _, _ = configured
    first = client.get(endpoint).json()
    assert first["available"]
    launcher = Path(config["launcher"][0])
    original = launcher.read_bytes()
    workspace = Path(config["workspace"])
    home = Path(config["environment"]["CODEX_HOME"])
    moved_home = home.with_name("moved-profile")
    if change == "launcher":
        launcher.write_bytes(b"changed launcher")
    elif change == "missing_launcher":
        launcher.unlink()
    elif change == "workspace":
        workspace.write_bytes(b"blocking file")
    else:
        home.rename(moved_home)
    changed = client.get(endpoint).json()
    assert changed["reason"] == "codex_preview_runtime_changed"
    assert changed["binding"] is None and changed["manifest"] is None
    if change in {"launcher", "missing_launcher"}:
        launcher.write_bytes(original)
    elif change == "workspace":
        workspace.unlink()
    else:
        moved_home.rename(home)
    assert client.get(endpoint).json() == changed
    app.state.journal_codex_preview = service.JournalCodexPreview(str(path))
    assert client.get(endpoint).json() == first


@pytest.mark.parametrize("change", ("config", "runtime"))
def test_changes_during_plan_compilation_are_caught_before_returning_manifest(configured, monkeypatch, change):
    _, client, endpoint, _, path, config, _, _, _, _ = configured
    build = service.build_plan
    def changed(*args, **kwargs):
        result = build(*args, **kwargs)
        if change == "config":
            path.write_bytes(path.read_bytes() + b" ")
        else:
            Path(config["launcher"][0]).write_bytes(b"changed while compiling")
        return result
    monkeypatch.setattr(service, "build_plan", changed)
    value = client.get(endpoint).json()
    assert value["reason"] == ("codex_preview_configuration_changed" if change == "config" else "codex_preview_runtime_changed")
    assert not value["available"] and value["manifest"] is None and value["preview_digest"] is None


INVALID_CONFIG = (
    "malformed", "oversized", "non_utf8", "duplicate", "nested_duplicate", "missing_field", "unknown_field",
    "launcher_relative", "launcher_missing", "launcher_empty", "launcher_three", "launcher_cmd", "bad_environment",
    "nonstring_environment", "home_relative", "home_missing", "workspace_relative", "workspace_checkout",
    "timeout_zero", "timeout_large", "timeout_bool", "timeout_nan", "timeout_infinity", "effort", "schema",
)


@pytest.mark.parametrize("case", INVALID_CONFIG)
def test_invalid_initial_configuration_is_redacted_and_can_be_repaired(configured, case):
    _, client, endpoint, _, path, config, _, _, _, _ = configured
    original = path.read_bytes()
    changed = deepcopy(config)
    overrides = {
        "missing_field": lambda: changed.pop("timeout_seconds"),
        "unknown_field": lambda: changed.update(api_key="sk-private-value"),
        "launcher_relative": lambda: changed.update(launcher=["relative.exe"]),
        "launcher_missing": lambda: changed.update(launcher=[str(path.parent / "missing.exe")]),
        "launcher_empty": lambda: changed.update(launcher=[]),
        "launcher_three": lambda: changed.update(launcher=config["launcher"] * 3),
        "launcher_cmd": lambda: changed.update(launcher=[str(path.parent / "forbidden.cmd")]),
        "bad_environment": lambda: changed["environment"].update(OPENAI_API_KEY="sk-private-value"),
        "nonstring_environment": lambda: changed["environment"].update(LANG=42),
        "home_relative": lambda: changed["environment"].update(CODEX_HOME="relative-home"),
        "home_missing": lambda: changed["environment"].update(CODEX_HOME=str(path.parent / "missing-profile")),
        "workspace_relative": lambda: changed.update(workspace="relative-workspace"),
        "workspace_checkout": lambda: changed.update(workspace=str(Path(__file__).resolve().parents[1] / "forbidden-preview-attempts")),
        "timeout_zero": lambda: changed.update(timeout_seconds=0),
        "timeout_large": lambda: changed.update(timeout_seconds=121),
        "timeout_bool": lambda: changed.update(timeout_seconds=True),
        "timeout_nan": lambda: changed.update(timeout_seconds=float("nan")),
        "timeout_infinity": lambda: changed.update(timeout_seconds=float("inf")),
        "effort": lambda: changed.update(reasoning_effort="unbounded"),
        "schema": lambda: changed.update(schema_version="unknown"),
    }
    if case in overrides:
        overrides[case]()
    raw = json.dumps(changed).encode()
    if case == "malformed":
        raw = b"{broken"
    elif case == "oversized":
        raw = b" " * 65_537
    elif case == "non_utf8":
        raw = b"\xff"
    elif case == "duplicate":
        raw = raw[:-1] + b',"timeout_seconds":30}'
    elif case == "nested_duplicate":
        raw = raw.replace(b'"environment": {', b'"environment": {"LANG":"duplicated",')
    path.write_bytes(raw)
    before = stored_state()
    response = client.get(endpoint)
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["reason"] == "codex_preview_configuration_invalid" and not value["available"]
    for private in ["sk-private-value", str(path)]:
        assert all(private not in value for value in public_strings(response.json()))
    assert value["manifest"] is None and value["binding"] is None and value["preview_digest"] is None
    assert path.read_bytes() == raw and stored_state() == before
    path.write_bytes(original)
    assert client.get(endpoint).json()["available"] is True


@pytest.mark.parametrize("kind", ("relative", "missing", "directory"))
def test_config_path_must_be_absolute_existing_regular_file(configured, kind):
    app, client, endpoint, _, path, _, _, _, _, _ = configured
    option = {"relative": "preview.json", "missing": str(path.parent / "missing.json"), "directory": str(path.parent)}[kind]
    app.state.journal_codex_preview = service.JournalCodexPreview(option)
    value = client.get(endpoint).json()
    assert value["reason"] == "codex_preview_configuration_invalid" and not value["available"]


def test_unsupported_frozen_model_is_reported_without_rewriting_contract(configured):
    _, client, _, _, _, _, url, body, _, _ = configured
    saved = client.post(url, json=body | dict(request_id="unsupported-model", provider_name="codex_cli", model="invalid model")).json()["execution"]
    endpoint = url + "/" + saved["contract"]["execution_id"] + "/codex-preview"
    before = stored_state()
    value = client.get(endpoint).json()
    assert value["reason"] == "codex_preview_contract_unsupported" and not value["available"]
    assert stored_state() == before
    assert client.get(endpoint.removesuffix("/codex-preview")).json() == saved


def test_concurrent_reads_initialize_once_and_do_not_write_events(configured, monkeypatch):
    _, client, endpoint, _, _, _, _, _, _, _ = configured
    before = stored_state()
    original = service.JournalCodexPreview._initialize
    initialized = []
    def count(*args):
        original(*args)
        initialized.append(True)
    monkeypatch.setattr(service.JournalCodexPreview, "_initialize", count)
    barrier = Barrier(6)
    def inspect(_):
        barrier.wait()
        return client.get(endpoint)
    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(inspect, range(6)))
    assert all(result.status_code == 200 and result.json()["available"] for result in results)
    assert all(result.json() == results[0].json() for result in results)
    assert initialized == [True] and stored_state() == before


@pytest.mark.parametrize("topic,subject,admin,allowed", (
    ("default-xrml", "reader", False, False), ("default-xrml", "admin", True, True),
    ("private-topic", "owner", False, True), ("private-topic", "outsider", False, False),
    ("private-topic", "admin", True, False),
))
def test_oidc_update_acl_protects_preview_before_any_config_access(oidc_api, tmp_path, monkeypatch, topic, subject, admin, allowed):
    app, client, user, jobs = oidc_api
    path, _ = local_configuration(tmp_path / "authorized-preview")
    app.state.journal_codex_preview = service.JournalCodexPreview(str(path))
    job = jobs[topic]
    saved, _ = repo.prepare(topic, job["job_id"], Prepare(request_id="preview", expected_input_digest=job["input_digest"],
        provider_name="codex_cli", model="fixture-model"), "original-owner")
    user["value"] = ProductUser(subject, subject, None, "oidc", is_admin=admin)
    endpoint = f"/api/topics/{topic}/local-synthesis/{job['job_id']}/journal-executions/{saved['contract']['execution_id']}/codex-preview"
    if not allowed:
        monkeypatch.setattr(service.JournalCodexPreview, "_read_configuration", lambda *_: pytest.fail("Unauthorized config read"))
    before = stored_state()
    response = client.get(endpoint)
    assert response.status_code == (200 if allowed else 404), response.text
    if allowed:
        assert response.json()["available"] and response.json()["actor_id"] == "original-owner"
    assert stored_state() == before


def test_preview_digest_binds_viewing_actor_without_changing_original_actor_or_events(oidc_api, tmp_path):
    app, client, user, jobs = oidc_api
    path, _ = local_configuration(tmp_path / "viewer-preview")
    app.state.journal_codex_preview = service.JournalCodexPreview(str(path))
    job = jobs["default-xrml"]
    saved, _ = repo.prepare("default-xrml", job["job_id"], Prepare(request_id="preview", expected_input_digest=job["input_digest"],
        provider_name="codex_cli", model="fixture-model"), "original-owner")
    endpoint = f"/api/topics/default-xrml/local-synthesis/{job['job_id']}/journal-executions/{saved['contract']['execution_id']}/codex-preview"
    before = stored_state()
    user["value"] = ProductUser("admin-a", "Admin A", None, "oidc", is_admin=True)
    first = client.get(endpoint).json()
    user["value"] = ProductUser("admin-b", "Admin B", None, "oidc", is_admin=True)
    second = client.get(endpoint).json()
    assert first["available"] and second["available"]
    assert first["preview_digest"] != second["preview_digest"]
    assert first | dict(preview_digest=second["preview_digest"]) == second
    assert first["actor_id"] == "original-owner" and stored_state() == before
