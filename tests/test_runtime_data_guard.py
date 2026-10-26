from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path


def _load_guard_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "runtime_data_guard.py"
    spec = importlib.util.spec_from_file_location("runtime_data_guard", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _relative_file_listing(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))


def test_runtime_data_guard_snapshots_protected_artifacts_without_writes(tmp_path: Path) -> None:
    guard = _load_guard_module()
    storage = tmp_path / "storage"
    runs = storage / "runs"
    analytics = storage / "topic_analytics"
    schedules = storage / "topic_schedules"
    runs.mkdir(parents=True)
    analytics.mkdir()
    schedules.mkdir()
    metadata_bytes = b"metadata-db"
    scheduler_bytes = b"scheduler-db"
    (storage / "metadata_index.sqlite3").write_bytes(metadata_bytes)
    (storage / "metadata_index.sqlite3-wal").write_bytes(b"metadata-wal")
    (storage / "scheduler.sqlite3").write_bytes(scheduler_bytes)
    (analytics / "large-language-models.json").write_text('{"ok": true}', encoding="utf-8")
    (schedules / "schedules.json").write_text("[]", encoding="utf-8")
    (runs / "run-a").mkdir()
    (runs / "run-a" / "run.json").write_text("{}", encoding="utf-8")
    (runs / "run-b" / "reports").mkdir(parents=True)
    (runs / "run-b" / "reports" / "preview.md").write_text("preview", encoding="utf-8")
    before_files = _relative_file_listing(tmp_path)

    snapshot = guard.build_snapshot(tmp_path)

    assert _relative_file_listing(tmp_path) == before_files
    assert snapshot["read_only"] is True
    assert snapshot["artifacts"]["metadata_db"]["size"] == len(metadata_bytes)
    assert snapshot["artifacts"]["metadata_db"]["sha256"] == hashlib.sha256(metadata_bytes).hexdigest()
    assert snapshot["artifacts"]["metadata_db"]["sidecars"]["wal"]["exists"] is True
    assert snapshot["artifacts"]["scheduler_db"]["size"] == len(scheduler_bytes)
    assert snapshot["artifacts"]["topic_analytics"]["file_count"] == 1
    assert snapshot["artifacts"]["topic_schedules"]["file_count"] == 1
    assert snapshot["artifacts"]["storage_runs"]["immediate_dir_count"] == 2
    assert snapshot["artifacts"]["storage_runs"]["recursive_file_count"] == 2


def test_runtime_data_guard_cli_writes_only_when_output_path_is_given(tmp_path: Path, capsys) -> None:
    guard = _load_guard_module()
    output_path = tmp_path / "guard.json"

    assert guard.main(["--repo-root", str(tmp_path)]) == 0
    assert not output_path.exists()
    stdout_payload = json.loads(capsys.readouterr().out)
    assert stdout_payload["schema_version"] == guard.SCHEMA_VERSION

    assert guard.main(["--repo-root", str(tmp_path), "--output", str(output_path)]) == 0
    assert output_path.exists()
    file_payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert file_payload["artifacts"]["storage_runs"]["exists"] is False


def test_runtime_data_guard_compare_reports_changed_artifacts(tmp_path: Path) -> None:
    guard = _load_guard_module()
    before = {
        "artifacts": {
            "metadata_db": {"sha256": "a"},
            "storage_runs": {"recursive_file_count": 1},
        }
    }
    after = {
        "artifacts": {
            "metadata_db": {"sha256": "b"},
            "storage_runs": {"recursive_file_count": 1},
        }
    }

    result = guard.compare_snapshots(before, after)

    assert result["equal"] is False
    assert result["changed_artifacts"] == ["metadata_db"]
