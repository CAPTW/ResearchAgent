from __future__ import annotations

import pytest

from app.db import bootstrap


def test_metadata_index_write_guard_blocks_repo_storage_under_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", bootstrap.PROJECT_ROOT / "storage" / "runs")

    with pytest.raises(RuntimeError, match="Refusing to write repo-local storage/metadata_index.sqlite3"):
        bootstrap.connect_metadata_index()


def test_metadata_index_write_guard_allows_temp_storage_root(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temp_runs_root = tmp_path / "storage" / "runs"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", temp_runs_root)

    with bootstrap.connect_metadata_index() as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1

    assert (tmp_path / "storage" / "metadata_index.sqlite3").exists()
