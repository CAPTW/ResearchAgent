from __future__ import annotations

import json

from tests.conftest import COMPARE_RUN_ID, PRIMARY_RUN_ID, REAL_RUN_FIXTURE_OPT_IN_ENV, _copy_or_create_run_fixture


def test_isolated_run_repo_synthesizes_missing_ignored_run_fixture(tmp_path) -> None:
    source_runs_root = tmp_path / "missing-source-runs"
    temp_runs_root = tmp_path / "storage" / "runs"
    temp_runs_root.mkdir(parents=True)

    run_dir = _copy_or_create_run_fixture(
        source_runs_root,
        temp_runs_root,
        PRIMARY_RUN_ID,
        compare_run_id=COMPARE_RUN_ID,
    )

    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert run["run_id"] == PRIMARY_RUN_ID
    assert run["source"] == "pytest_synthetic_fixture"
    assert run["artifacts"]["triage_jsonl"] == "run:triage/triage.jsonl"
    assert (run_dir / "triage" / "triage.jsonl").is_file()
    assert (run_dir / "reports" / "outline.json").is_file()
    assert (run_dir / "inputs" / "editing_sessions.json").is_file()
    assert not source_runs_root.exists()


def test_isolated_run_repo_prefers_synthetic_fixture_when_ignored_run_exists(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(REAL_RUN_FIXTURE_OPT_IN_ENV, raising=False)
    source_runs_root = tmp_path / "source-runs"
    source_run_dir = source_runs_root / PRIMARY_RUN_ID
    source_run_dir.mkdir(parents=True)
    (source_run_dir / "run.json").write_text(
        json.dumps({"run_id": PRIMARY_RUN_ID, "source": "ignored_repo_local_fixture"}),
        encoding="utf-8",
    )
    temp_runs_root = tmp_path / "storage" / "runs"
    temp_runs_root.mkdir(parents=True)

    run_dir = _copy_or_create_run_fixture(
        source_runs_root,
        temp_runs_root,
        PRIMARY_RUN_ID,
        compare_run_id=COMPARE_RUN_ID,
    )

    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert run["run_id"] == PRIMARY_RUN_ID
    assert run["source"] == "pytest_synthetic_fixture"
    assert (run_dir / "triage" / "triage.jsonl").is_file()


def test_isolated_run_repo_can_copy_real_fixture_with_explicit_opt_in(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(REAL_RUN_FIXTURE_OPT_IN_ENV, "1")
    source_runs_root = tmp_path / "source-runs"
    source_run_dir = source_runs_root / PRIMARY_RUN_ID
    source_run_dir.mkdir(parents=True)
    (source_run_dir / "run.json").write_text(
        json.dumps({"run_id": PRIMARY_RUN_ID, "source": "explicit_opt_in_fixture"}),
        encoding="utf-8",
    )
    temp_runs_root = tmp_path / "storage" / "runs"
    temp_runs_root.mkdir(parents=True)

    run_dir = _copy_or_create_run_fixture(
        source_runs_root,
        temp_runs_root,
        PRIMARY_RUN_ID,
        compare_run_id=COMPARE_RUN_ID,
    )

    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert run == {"run_id": PRIMARY_RUN_ID, "source": "explicit_opt_in_fixture"}
