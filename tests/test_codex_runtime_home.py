"""Explicit CLI home boundaries using disposable paths, without account access."""
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

from app.domain.journal_exchange import preserved_json
from app.services.providers import codex_journal_process as process
from app.services.providers.codex_journal_plan import sha


def runtime_at(root: Path) -> process.CliRuntime:
    home = root / "empty-home"
    home.mkdir()
    return process.CliRuntime((sys.executable,), {"CODEX_HOME": str(home)}, root / "attempts")


def run(runtime: process.CliRuntime):
    return runtime.run("synthetic input", "{}", model="offline-requested-model", effort="low", timeout_seconds=1)


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_home_must_be_an_existing_directory(tmp_path, kind):
    home = tmp_path / "invalid-home"
    if kind == "file":
        home.write_text("synthetic fixture", encoding="utf-8")
    with pytest.raises(ValueError, match="^journal_codex_runtime_invalid$"):
        process.CliRuntime((sys.executable,), {"CODEX_HOME": str(home)}, tmp_path / "attempts")
    assert not (tmp_path / "attempts").exists()


@pytest.mark.parametrize("kind", ["missing", "file", "retargeted"])
def test_changed_home_refuses_before_attempt_or_capture(tmp_path, monkeypatch, kind):
    runtime = runtime_at(tmp_path)
    home = Path(dict(runtime.environment)["CODEX_HOME"])
    if kind == "retargeted":
        other = tmp_path / "other-home"
        other.mkdir()
        original = Path.resolve
        # Simulate a junction changing target without requiring symlink privileges.
        monkeypatch.setattr(Path, "resolve", lambda path, *args, **kwargs:
                            other if path == home else original(path, *args, **kwargs))
    else:
        home.rename(tmp_path / "preserved-home")
        if kind == "file":
            home.write_text("synthetic replacement", encoding="utf-8")
    capture = Mock(return_value=None)
    monkeypatch.setattr(process, "capture", capture)
    with pytest.raises(ValueError, match="^journal_codex_runtime_changed$"):
        run(runtime)
    capture.assert_not_called()
    assert not runtime.workspace.exists()


def test_resolved_home_is_the_actual_child_environment(tmp_path, monkeypatch):
    home = tmp_path / "empty-home"
    home.mkdir()
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    environment = {"CODEX_HOME": str(prefix / ".." / home.name)}
    runtime = process.CliRuntime((sys.executable,), environment, tmp_path / "attempts")
    environment["CODEX_HOME"] = str(tmp_path / "caller-mutation")
    capture = Mock(return_value=None)
    monkeypatch.setattr(process, "capture", capture)
    run(runtime)
    assert capture.call_args.args[3]["CODEX_HOME"] == str(home.resolve())
    assert runtime.identity()["environment_sha256"] == sha(preserved_json((("CODEX_HOME", str(home.resolve())),)))
    assert list(runtime.workspace.iterdir()) == []


def test_canonical_home_keeps_v1_identity(tmp_path):
    runtime = runtime_at(tmp_path)
    environment = (("CODEX_HOME", str((tmp_path / "empty-home").resolve())),)
    assert runtime.identity()["environment_sha256"] == sha(preserved_json(environment))
    assert set(runtime.identity()) == {"launcher_sha256", "environment_sha256", "workspace_sha256"}
    runtime.verify()


@pytest.mark.parametrize("error", [PermissionError("private path"), RuntimeError("private link loop")])
def test_home_resolution_failure_is_a_bounded_error(tmp_path, monkeypatch, error):
    runtime = runtime_at(tmp_path)
    home = Path(dict(runtime.environment)["CODEX_HOME"])
    original = Path.resolve

    def resolve(path, *args, **kwargs):
        if path == home:
            raise error
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(ValueError, match="^journal_codex_runtime_invalid$"):
        process.CliRuntime(runtime.launcher, dict(runtime.environment), runtime.workspace)
    with pytest.raises(ValueError, match="^journal_codex_runtime_changed$"):
        runtime.verify()
