"""Read-only workspace preflight with disposable files, never a real CLI."""
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

from app.services.providers import codex_journal_process as process
from app.services.providers.codex_journal_plan import sha


def runtime_at(root: Path, workspace: Path) -> process.CliRuntime:
    home = root / "empty-home"
    home.mkdir(exist_ok=True)
    return process.CliRuntime((str(Path(sys.executable).resolve()),), {"CODEX_HOME": str(home)}, workspace)


@pytest.mark.parametrize("nested", [False, True])
def test_workspace_file_is_rejected_before_construction(tmp_path, nested):
    blocker = tmp_path / "blocked"
    blocker.write_bytes(b"preserve fixture bytes")
    workspace = blocker / "attempts" if nested else blocker
    with pytest.raises(ValueError, match="^journal_codex_runtime_invalid$"):
        runtime_at(tmp_path, workspace)
    assert blocker.read_bytes() == b"preserve fixture bytes"


@pytest.mark.parametrize("nested", [False, True])
def test_workspace_file_is_rechecked_before_attempt_or_capture(tmp_path, monkeypatch, nested):
    blocker = tmp_path / "blocked"
    workspace = blocker / "attempts" if nested else blocker
    runtime = runtime_at(tmp_path, workspace)
    identity = runtime.identity()
    blocker.write_bytes(b"preserve fixture bytes")
    capture = Mock()
    monkeypatch.setattr(process, "capture", capture)
    with pytest.raises(ValueError, match="^journal_codex_runtime_changed$"):
        runtime.verify()
    with pytest.raises(ValueError, match="^journal_codex_runtime_changed$"):
        runtime.run("synthetic", "{}", model="offline", effort="low", timeout_seconds=1)
    capture.assert_not_called()
    assert runtime.identity() == identity
    assert blocker.read_bytes() == b"preserve fixture bytes"


@pytest.mark.parametrize("existing", [False, True])
def test_directory_preflight_is_read_only_and_keeps_v1_identity(tmp_path, monkeypatch, existing):
    workspace = tmp_path / "parent" / "attempts"
    if existing:
        workspace.mkdir(parents=True)
    runtime = runtime_at(tmp_path, workspace)
    runtime.verify()
    assert workspace.exists() is existing
    assert runtime.identity()["workspace_sha256"] == sha(str(workspace.resolve()))
    capture = Mock(return_value="synthetic captured result")
    monkeypatch.setattr(process, "capture", capture)
    assert runtime.run("synthetic", "{}", model="offline", effort="low", timeout_seconds=1) == "synthetic captured result"
    assert capture.call_count == 1
    assert capture.call_args.args[2].parent == workspace.resolve()
    assert list(workspace.iterdir()) == []


@pytest.mark.parametrize("error", [PermissionError("private path"), RuntimeError("private link loop")])
def test_workspace_metadata_failure_is_bounded(tmp_path, monkeypatch, error):
    workspace = tmp_path / "attempts"
    runtime = runtime_at(tmp_path, workspace)
    original = Path.resolve

    def resolve(path, *args, **kwargs):
        if path == workspace:
            raise error
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(ValueError, match="^journal_codex_runtime_invalid$"):
        runtime_at(tmp_path, workspace)
    with pytest.raises(ValueError, match="^journal_codex_runtime_changed$"):
        runtime.verify()
