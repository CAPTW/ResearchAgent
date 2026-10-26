from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any

from fastapi.testclient import TestClient

from app.services.codex_session_probe import CommandResult, assert_no_secret_leakage, probe_codex_session
from app.services.report_draft_assist_registry import (
    ReportDraftAssistError,
    describe_report_draft_assist,
    refresh_report_draft_assist,
    reset_report_draft_assist_state_for_tests,
    select_report_draft_provider,
)
import pytest

from app.services import prototype_live_provider
from app.services.providers import codex_sdk_provider


@pytest.fixture(autouse=True)
def _reset_report_draft_assist_state() -> Any:
    reset_report_draft_assist_state_for_tests()
    yield
    reset_report_draft_assist_state_for_tests()


LOGIN_HELP = """Manage login

Usage: codex login [OPTIONS] [COMMAND]

Commands:
  status  Show login status
  help    Print this message or the help of the given subcommand(s)
"""


def _runner(status_stdout: str = "Logged in using ChatGPT", status_code: int = 0, version: str = "codex-cli 0.144.6"):
    def run(argv_tail: tuple[str, ...]) -> CommandResult:
        if argv_tail == ("--version",):
            return CommandResult(argv_tail, 0, version, "")
        if argv_tail == ("--help",):
            return CommandResult(argv_tail, 0, "Commands:\n  login  Authenticate\n  exec   Run non-interactively\n", "")
        if argv_tail == ("login", "--help"):
            return CommandResult(argv_tail, 0, LOGIN_HELP, "")
        if argv_tail == ("login", "status"):
            return CommandResult(argv_tail, status_code, status_stdout, "")
        raise AssertionError(argv_tail)

    return run


def test_probe_serializes_authenticated_cli_session_without_secrets() -> None:
    snapshot = probe_codex_session(runner=_runner(), executable="codex")
    assert snapshot["installed"] is True
    assert snapshot["authenticated"] is True
    assert snapshot["surface"] == "cli"
    assert snapshot["version"] == "0.144.6"
    assert snapshot["official_login_command"] == "codex login"
    assert snapshot["reason_code"] == "codex_session_authenticated"
    assert assert_no_secret_leakage(snapshot) == []
    assert "C:\\" not in json.dumps(snapshot)
    assert "auth.json" not in json.dumps(snapshot)


def test_probe_marks_unauthenticated_and_missing_cli() -> None:
    missing = probe_codex_session(executable="")
    assert missing["installed"] is False
    assert missing["authenticated"] is False
    assert missing["reason_code"] == "codex_cli_not_installed"

    signed_out = probe_codex_session(runner=_runner("Not logged in", status_code=1), executable="codex")
    assert signed_out["authenticated"] is False
    assert signed_out["reason_code"] == "codex_session_unauthenticated"


def test_probe_ignores_secret_shaped_help_examples_and_reads_status() -> None:
    login_help = LOGIN_HELP + "\n  Example token shape mentioned in docs: sk- not a live credential\n"

    def run(argv_tail: tuple[str, ...]) -> CommandResult:
        if argv_tail == ("--version",):
            return CommandResult(argv_tail, 0, "codex-cli 0.144.6", "")
        if argv_tail == ("--help",):
            return CommandResult(argv_tail, 0, "Commands:\n  login  Authenticate\n", "")
        if argv_tail == ("login", "--help"):
            return CommandResult(argv_tail, 0, login_help, "")
        if argv_tail == ("login", "status"):
            return CommandResult(argv_tail, 0, "Logged in using ChatGPT", "")
        raise AssertionError(argv_tail)

    snapshot = probe_codex_session(runner=run, executable="codex")
    assert snapshot["authenticated"] is True
    assert snapshot["reason_code"] == "codex_session_authenticated"
    assert "sk-" not in json.dumps(snapshot)
    assert assert_no_secret_leakage(snapshot) == []


def test_probe_rejects_secret_shaped_status_output() -> None:
    snapshot = probe_codex_session(
        runner=_runner("Logged in using ChatGPT access_token=super-secret-value"),
        executable="codex",
    )
    assert snapshot["authenticated"] is False
    assert snapshot["reason_code"] == "codex_session_status_redacted"
    assert "super-secret-value" not in json.dumps(snapshot)
    assert assert_no_secret_leakage(snapshot) == []


def test_probe_blocks_when_official_login_help_is_missing(tmp_path: Path) -> None:
    def run(argv_tail: tuple[str, ...]) -> CommandResult:
        if argv_tail == ("--version",):
            return CommandResult(argv_tail, 0, "codex-cli 0.1.0", "")
        if argv_tail == ("--help",):
            return CommandResult(argv_tail, 0, "no login surface", "")
        return CommandResult(argv_tail, 0, "no login surface", "")

    snapshot = probe_codex_session(runner=run, executable="codex")
    assert snapshot["official_auth_surface_available"] is False
    assert snapshot["reason_code"] == "codex_official_auth_surface_unavailable"


def test_registry_defaults_to_deterministic_and_requires_auth_for_codex(tmp_path: Path, monkeypatch: Any) -> None:
    reset_report_draft_assist_state_for_tests()
    monkeypatch.setenv("XRML_DATA_DIR", str(tmp_path / "data"))
    snapshot = describe_report_draft_assist(refresh=False)
    assert snapshot["selected_provider_id"] == "deterministic_local"
    assert snapshot["activation_state"] == "inactive"
    assert snapshot["deterministic_fallback_available"] is True
    assert [item["provider_id"] for item in snapshot["providers"]] == ["deterministic_local", "codex"]

    refresh_report_draft_assist(runner=_runner("Not logged in", status_code=1))
    try:
        select_report_draft_provider("codex", runner=_runner("Not logged in", status_code=1))
        raise AssertionError("unauthenticated Codex selection must fail")
    except ReportDraftAssistError as exc:
        assert exc.code == "codex_session_unauthenticated"
    assert describe_report_draft_assist()["selected_provider_id"] == "deterministic_local"

    selected = select_report_draft_provider("codex", runner=_runner())
    assert selected["selected_provider_id"] == "codex"
    assert selected["activation_state"] in {"eligible", "activated"}
    switched = select_report_draft_provider("deterministic_local")
    assert switched["selected_provider_id"] == "deterministic_local"
    assert switched["activation_state"] == "inactive"
    assert switched["deterministic_fallback_available"] is True


def test_settings_and_generate_honor_selection_without_secret_leakage(tmp_path: Path, app_factory, monkeypatch: Any) -> None:
    reset_report_draft_assist_state_for_tests()
    prototype_live_provider.reset_ephemeral_activation_state_for_tests()
    data_root = (tmp_path / "report-draft-assist").resolve()
    monkeypatch.setattr(prototype_live_provider, "_dependency_available", lambda: True)

    def _blocked_runner(**_kwargs: Any) -> Any:
        raise AssertionError("live runner must not run")

    monkeypatch.setattr("app.services.codex_cli_report_draft.run_codex_cli_report_draft_once", _blocked_runner)
    monkeypatch.setattr(codex_sdk_provider, "run_codex_sdk_prototype_report_draft_once", _blocked_runner)
    monkeypatch.setattr(
        "app.services.report_draft_assist_registry.probe_codex_session",
        lambda **_kwargs: probe_codex_session(runner=_runner("Not logged in", status_code=1), executable="codex"),
    )
    with TestClient(
        app_factory(
            AUTH_MODE="disabled",
            XRML_PROTOTYPE_MODE="1",
            XRML_PROTOTYPE_LIVE_PROVIDER_PROOF="1",
            XRML_PROTOTYPE_SESSION_ID="registry-session",
            XRML_DATA_DIR=str(data_root),
            XRML_RUN_EXECUTOR_POLL_SECONDS="0.05",
        ),
        client=("127.0.0.1", 50000),
    ) as client:
        status = client.get("/settings/status")
        assert status.status_code == 200, status.text
        body = status.json()["reportDraftAssist"]
        assert body["stage"] == "report_draft_assist"
        assert body["selected_provider_id"] == "deterministic_local"
        assert body["deterministic_fallback_available"] is True
        assert assert_no_secret_leakage(status.json()["reportDraftAssist"]) == []

        refresh = client.post("/settings/report-draft-assist/refresh")
        assert refresh.status_code == 200, refresh.text
        assert refresh.json()["providers"][1]["authenticated"] is False
        assert "C:\\" not in refresh.text
        assert "auth.json" not in refresh.text

        denied = client.post("/settings/report-draft-assist/select", json={"provider_id": "codex"})
        assert denied.status_code == 409, denied.text
        assert denied.json()["detail"]["code"] == "codex_session_unauthenticated"

        started = client.post("/prototype/runs")
        assert started.status_code == 200, started.text
        run_id = started.json()["run_id"]
        payload: dict[str, Any] | None = None
        for _ in range(80):
            run = client.get(f"/runs/{run_id}")
            assert run.status_code == 200
            payload = run.json()
            if payload["status"] in {"completed", "failed"}:
                break
            time.sleep(0.05)
        assert payload is not None
        assert payload["status"] == "completed"
        select_report_draft_provider("deterministic_local")
        activation = client.post(
            f"/prototype/runs/{run_id}/provider-proof/activation",
            json={
                "provider_id": "codex",
                "execution_surface": "codex_sdk_python",
                "stage": "report_draft_assist",
                "internal_output_class": "report_draft",
                "product_output_class": "weekly_report",
            },
        )
        assert activation.status_code == 409, activation.text
        assert activation.json()["detail"]["code"] == "report_draft_assist_codex_not_selected"
        versions = client.get(f"/runs/{run_id}").json()
        assert versions["status"] == "completed"
