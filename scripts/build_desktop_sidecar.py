from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PINNED_PYINSTALLER = "6.22.2"
TARGET_TRIPLE = "x86_64-pc-windows-msvc"
SIDECAR_BASENAME = "xrml-sidecar"
RUNTIME_DIRECTORY = "sidecar-runtime"
PACKAGED_RESOURCE_DIRS = (("web/dist", "web/dist"), ("config", "config"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def deterministic_inputs(repository_root: Path) -> list[dict[str, object]]:
    roots = [repository_root / "app"] + [
        repository_root / source for source, _target in PACKAGED_RESOURCE_DIRS
    ]
    files = [repository_root / "pyproject.toml", repository_root / "uv.lock"]
    for root in roots:
        files.extend(path for path in root.rglob("*") if path.is_file() and "__pycache__" not in path.parts)
    result = []
    for path in sorted(set(files), key=lambda item: item.relative_to(repository_root).as_posix()):
        relative = path.relative_to(repository_root).as_posix()
        result.append({"path": relative, "bytes": path.stat().st_size, "sha256": sha256(path)})
    return result


def pyinstaller_command(repository_root: Path, output_root: Path) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        # Tauri already installs a directory. A one-file child needlessly
        # extracts every DLL/resource before Python can report readiness.
        "--onedir",
        "--contents-directory",
        RUNTIME_DIRECTORY,
        "--console",
        "--name",
        SIDECAR_BASENAME,
        "--distpath",
        str(output_root / "dist"),
        "--workpath",
        str(output_root / "work"),
        "--specpath",
        str(output_root / "spec"),
        "--paths",
        str(repository_root),
        "--collect-submodules",
        "app",
    ]
    for source, target in PACKAGED_RESOURCE_DIRS:
        command.extend(["--add-data", f"{repository_root / source};{target}"])
    command.append(str(repository_root / "app" / "desktop" / "sidecar.py"))
    return command


def stage_bundle(built: Path, runtime: Path, tauri_binary: Path, tauri_runtime: Path) -> None:
    """Prepare all files before replacing generated assets; retain the old pair on failure."""
    staging = Path(tempfile.mkdtemp(prefix=".sidecar-stage-", dir=tauri_binary.parent))
    previous_binary = staging / "previous.exe"
    previous_runtime = staging / "previous-runtime"
    installed_runtime = False
    installed_binary = False
    try:
        shutil.copytree(runtime, staging / RUNTIME_DIRECTORY)
        shutil.copy2(built, staging / "next.exe")
        if tauri_binary.exists():
            tauri_binary.replace(previous_binary)
        if tauri_runtime.exists():
            tauri_runtime.replace(previous_runtime)
        (staging / RUNTIME_DIRECTORY).replace(tauri_runtime)
        installed_runtime = True
        (staging / "next.exe").replace(tauri_binary)
        installed_binary = True
    except BaseException:
        try:
            # Targets were checked by build(); only our newly installed assets
            # are removed here. A failed rollback preserves the backup directory.
            if installed_binary:
                tauri_binary.unlink()
            if installed_runtime:
                shutil.rmtree(tauri_runtime)
            if previous_runtime.exists():
                previous_runtime.replace(tauri_runtime)
            if previous_binary.exists():
                previous_binary.replace(tauri_binary)
        except OSError as exc:
            raise RuntimeError(f"Sidecar staging rollback failed; backups retained at {staging}") from exc
        shutil.rmtree(staging)
        raise
    shutil.rmtree(staging)


def build(repository_root: Path, output_root: Path) -> dict[str, object]:
    try:
        import PyInstaller
    except ImportError as exc:
        raise RuntimeError("The pinned desktop dependency group is not installed.") from exc
    if PyInstaller.__version__ != PINNED_PYINSTALLER:
        raise RuntimeError(
            f"PyInstaller version mismatch: expected {PINNED_PYINSTALLER}, got {PyInstaller.__version__}."
        )

    repository_root = repository_root.resolve()
    output_root = output_root.resolve()
    frontend_dist = repository_root / "web" / "dist"
    if not (frontend_dist / "index.html").is_file():
        raise RuntimeError("Build web/dist with WEB_APP_BASE_PATH=/app before packaging the sidecar.")
    try:
        output_root.relative_to(repository_root)
    except ValueError:
        pass
    else:
        raise RuntimeError("Sidecar build output must stay outside the source worktree.")

    output_root.mkdir(parents=True, exist_ok=True)
    inventory = deterministic_inputs(repository_root)
    inventory_path = output_root / "input-inventory.json"
    inventory_path.write_text(
        json.dumps({"schema_version": "xrml-desktop-sidecar-input-v1", "files": inventory}, indent=2),
        encoding="utf-8",
    )

    dist_path = output_root / "dist"
    command = pyinstaller_command(repository_root, output_root)
    build_environment = os.environ.copy()
    build_environment["PYINSTALLER_CONFIG_DIR"] = str(output_root / "config")
    completed = subprocess.run(command, cwd=repository_root, env=build_environment, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"PyInstaller failed with exit code {completed.returncode}.")

    bundle = dist_path / SIDECAR_BASENAME
    built = bundle / f"{SIDECAR_BASENAME}.exe"
    if not built.is_file():
        raise RuntimeError("PyInstaller reported success without the sidecar executable.")
    runtime = bundle / RUNTIME_DIRECTORY
    if not (runtime / "base_library.zip").is_file() or not any(runtime.glob("python3*.dll")):
        raise RuntimeError("PyInstaller reported success without the sidecar runtime.")
    runtime_inventory = output_root / "runtime-inventory.json"
    runtime_inventory.write_text(json.dumps({"files": [
        {"path": path.relative_to(runtime).as_posix(), "bytes": path.stat().st_size, "sha256": sha256(path)}
        for path in sorted(runtime.rglob("*")) if path.is_file()
    ]}, indent=2), encoding="utf-8")

    tauri_binary_dir = repository_root / "desktop" / "src-tauri" / "binaries"
    # Never follow a redirected generated directory during replacement.
    if tauri_binary_dir.resolve() != tauri_binary_dir:
        raise RuntimeError("The generated sidecar directory must not be redirected.")
    tauri_binary_dir.mkdir(parents=True, exist_ok=True)
    tauri_binary = tauri_binary_dir / f"{SIDECAR_BASENAME}-{TARGET_TRIPLE}.exe"
    tauri_runtime = tauri_binary_dir / RUNTIME_DIRECTORY
    if tauri_runtime.resolve() != tauri_runtime or tauri_binary.resolve() != tauri_binary:
        raise RuntimeError("The generated sidecar runtime must not be redirected.")
    # Replace the complete generated directory, so upgrades cannot retain stale DLLs.
    stage_bundle(built, runtime, tauri_binary, tauri_runtime)

    result = {
        "schema_version": "xrml-desktop-sidecar-build-v2",
        "layout": "onedir",
        "python": sys.version.split()[0],
        "pyinstaller": PyInstaller.__version__,
        "target_triple": TARGET_TRIPLE,
        "input_count": len(inventory),
        "input_inventory": str(inventory_path),
        "built_executable": str(built),
        "tauri_external_binary": str(tauri_binary),
        "tauri_runtime_directory": str(tauri_runtime),
        "runtime_inventory": str(runtime_inventory),
        "bytes": built.stat().st_size,
        "sha256": sha256(built),
    }
    (output_root / "sidecar-build-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, separators=(",", ":")))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the pinned XRML FastAPI desktop sidecar.")
    parser.add_argument("--repository-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        build(args.repository_root, args.output_root)
    except Exception as exc:
        print(json.dumps({"event": "sidecar_build_failed", "reason": type(exc).__name__}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
