"""Package layout checks without executing Python services or a model provider."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import build_desktop_sidecar as packaging


def test_sidecar_does_not_extract_a_runtime_on_each_launch(tmp_path: Path) -> None:
    command = packaging.pyinstaller_command(tmp_path / "repo", tmp_path / "output")
    assert "--onedir" in command
    assert "--onefile" not in command
    assert command[command.index("--contents-directory") + 1] == "sidecar-runtime"


def _repository(root: Path) -> Path:
    repo = root / "repo"
    for name in ("app/desktop/sidecar.py", "web/dist/index.html", "config/sources.json", "pyproject.toml", "uv.lock"):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")
    return repo


def _fake_pyinstaller(monkeypatch: pytest.MonkeyPatch, output: Path, *, runtime: bool = True) -> None:
    monkeypatch.setitem(sys.modules, "PyInstaller", SimpleNamespace(__version__=packaging.PINNED_PYINSTALLER))

    def run(*args, **kwargs):
        bundle = output / "dist/xrml-sidecar"
        bundle.mkdir(parents=True)
        (bundle / "xrml-sidecar.exe").write_bytes(b"new executable")
        if runtime:
            for name in ("python312.dll", "base_library.zip", "web/dist/index.html", "config/sources.json"):
                target = bundle / "sidecar-runtime" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(name.encode())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(packaging.subprocess, "run", run)


def test_build_stages_the_complete_runtime_and_replaces_stale_generated_files(tmp_path: Path, monkeypatch) -> None:
    repo = _repository(tmp_path)
    output = tmp_path / "build"
    binaries = repo / "desktop/src-tauri/binaries"
    stale = binaries / "sidecar-runtime/stale.dll"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old dependency")
    (binaries / "unrelated.txt").write_bytes(b"preserve")
    _fake_pyinstaller(monkeypatch, output)

    result = packaging.build(repo, output)

    assert Path(result["tauri_external_binary"]).read_bytes() == b"new executable"
    assert result["layout"] == "onedir"
    runtime = Path(result["tauri_runtime_directory"])
    assert runtime == binaries / "sidecar-runtime"
    assert (runtime / "python312.dll").read_bytes() == b"python312.dll"
    assert (runtime / "web/dist/index.html").is_file()
    assert (runtime / "config/sources.json").is_file()
    assert not stale.exists()
    assert (binaries / "unrelated.txt").read_bytes() == b"preserve"
    files = json.loads(Path(result["runtime_inventory"]).read_text(encoding="utf-8"))["files"]
    assert {item["path"] for item in files} == {
        "python312.dll", "base_library.zip", "web/dist/index.html", "config/sources.json",
    }
    assert all(item["sha256"] == packaging.sha256(runtime / item["path"]) for item in files)


def test_missing_runtime_does_not_replace_a_previous_executable(tmp_path: Path, monkeypatch) -> None:
    repo = _repository(tmp_path)
    output = tmp_path / "build"
    prior = repo / f"desktop/src-tauri/binaries/xrml-sidecar-{packaging.TARGET_TRIPLE}.exe"
    prior.parent.mkdir(parents=True)
    prior.write_bytes(b"previous executable")
    _fake_pyinstaller(monkeypatch, output, runtime=False)

    with pytest.raises(RuntimeError, match="runtime"):
        packaging.build(repo, output)
    assert prior.read_bytes() == b"previous executable"


def test_tauri_installs_runtime_next_to_the_external_executable() -> None:
    repo = Path(__file__).resolve().parents[1]
    config = json.loads((repo / "desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    assert config["bundle"]["resources"]["binaries/sidecar-runtime/"] == "sidecar-runtime/"
    assert config["bundle"]["externalBin"] == ["binaries/xrml-sidecar"]


def test_staging_copy_failure_preserves_the_previous_bundle(tmp_path: Path, monkeypatch) -> None:
    repo = _repository(tmp_path)
    output = tmp_path / "build"
    binaries = repo / "desktop/src-tauri/binaries"
    runtime = binaries / "sidecar-runtime"
    runtime.mkdir(parents=True)
    (runtime / "previous.dll").write_bytes(b"previous runtime")
    prior = binaries / f"xrml-sidecar-{packaging.TARGET_TRIPLE}.exe"
    prior.write_bytes(b"previous executable")
    _fake_pyinstaller(monkeypatch, output)

    def fail_copy(*args, **kwargs):
        raise OSError("simulated copy failure")

    monkeypatch.setattr(packaging.shutil, "copytree", fail_copy)
    with pytest.raises(OSError, match="simulated copy failure"):
        packaging.build(repo, output)
    assert prior.read_bytes() == b"previous executable"
    assert (runtime / "previous.dll").read_bytes() == b"previous runtime"


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing/rename contract")
def test_a_locked_previous_executable_leaves_its_runtime_intact(tmp_path: Path, monkeypatch) -> None:
    repo = _repository(tmp_path)
    output = tmp_path / "build"
    binaries = repo / "desktop/src-tauri/binaries"
    runtime = binaries / "sidecar-runtime"
    runtime.mkdir(parents=True)
    (runtime / "previous.dll").write_bytes(b"previous runtime")
    prior = binaries / f"xrml-sidecar-{packaging.TARGET_TRIPLE}.exe"
    prior.write_bytes(b"previous executable")
    _fake_pyinstaller(monkeypatch, output)
    with prior.open("rb"):
        with pytest.raises(PermissionError):
            packaging.build(repo, output)
    assert prior.read_bytes() == b"previous executable"
    assert (runtime / "previous.dll").read_bytes() == b"previous runtime"
    assert not list(binaries.glob(".sidecar-stage-*"))


@pytest.mark.parametrize("blocked_name", ["sidecar-runtime", "next.exe"])
def test_replacement_failure_restores_both_previous_assets(tmp_path: Path, monkeypatch, blocked_name) -> None:
    repo = _repository(tmp_path)
    output = tmp_path / "build"
    binaries = repo / "desktop/src-tauri/binaries"
    runtime = binaries / "sidecar-runtime"
    runtime.mkdir(parents=True)
    (runtime / "previous.dll").write_bytes(b"previous runtime")
    prior = binaries / f"xrml-sidecar-{packaging.TARGET_TRIPLE}.exe"
    prior.write_bytes(b"previous executable")
    _fake_pyinstaller(monkeypatch, output)
    replace = Path.replace

    def blocked_replace(path, target):
        if path.name == blocked_name and path.parent.name.startswith(".sidecar-stage-"):
            raise PermissionError("simulated replacement refusal")
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", blocked_replace)
    with pytest.raises(PermissionError, match="replacement refusal"):
        packaging.build(repo, output)
    assert prior.read_bytes() == b"previous executable"
    assert (runtime / "previous.dll").read_bytes() == b"previous runtime"
    assert not (runtime / "python312.dll").exists()
    assert not list(binaries.glob(".sidecar-stage-*"))


@pytest.mark.skipif(os.name != "nt", reason="Windows local launcher")
def test_local_launcher_refuses_an_incomplete_runtime_before_starting(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for name in ("xrml-desktop-host.exe", "xrml-sidecar.exe"):
        (bundle / name).write_bytes(b"not executable")
    workspace = tmp_path / "workspace"
    shell = shutil.which("pwsh") or shutil.which("powershell")
    assert shell is not None
    result = subprocess.run([
        shell, "-NoProfile", "-File", str(repo / "scripts/start_local_desktop.ps1"),
        "-BundleDirectory", str(bundle), "-WorkspaceDirectory", str(workspace),
    ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    assert result.returncode != 0
    assert "bundle is incomplete" in result.stderr
    assert not workspace.exists()
