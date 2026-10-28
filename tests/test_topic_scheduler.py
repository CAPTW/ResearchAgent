from __future__ import annotations

import io
import json
import os
import socket
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Event

import pytest

from app.db import bootstrap as db_bootstrap
from app.auth.config import clear_auth_settings_cache
from app.auth.security import clear_auth_runtime_caches
from app.domain import constants
from app.jobs import topic_schedule_worker
from app.repositories import runs as run_repository
from app.services import topic_profiles, topic_schedules
from app.services.topic_scheduler import TopicScheduleWorker


def _configure_topic_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    monkeypatch.delenv("AUTH_MODE", raising=False)
    clear_auth_settings_cache()
    clear_auth_runtime_caches()
    storage_root = tmp_path / "storage" / "runs"
    profile_dir = tmp_path / "storage" / "topic_profiles"
    schedule_dir = tmp_path / "storage" / "topic_schedules"
    storage_root.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(constants, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(db_bootstrap, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(run_repository, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(topic_profiles, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_dir)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_dir / "profiles.json")
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_DIR", schedule_dir)
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_PATH", schedule_dir / "schedules.json")
    monkeypatch.setattr(topic_schedules, "SCHEDULER_STATUS_PATH", schedule_dir / "worker_status.json")
    return profile_dir, schedule_dir


def _schedule_path(schedule_dir: Path) -> Path:
    return schedule_dir / "schedules.json"


def _set_schedule_due(schedule_dir: Path, schedule_id: str, *, due_at: str, enabled: bool = True) -> dict:
    path = _schedule_path(schedule_dir)
    schedules = json.loads(path.read_text(encoding="utf-8"))
    schedule = next(item for item in schedules if item["schedule_id"] == schedule_id)
    schedule["enabled"] = enabled
    schedule["next_run_at"] = due_at
    schedule["next_run_hint"] = due_at
    path.write_text(json.dumps(schedules, ensure_ascii=False, indent=2), encoding="utf-8")
    return schedule


def _append_file_only_schedule(schedule_dir: Path, *, source_schedule_id: str, schedule_id: str) -> dict:
    path = _schedule_path(schedule_dir)
    schedules = json.loads(path.read_text(encoding="utf-8"))
    source = next(item for item in schedules if item["schedule_id"] == source_schedule_id)
    clone = json.loads(json.dumps(source, ensure_ascii=False))
    clone["schedule_id"] = schedule_id
    clone["title"] = "파일 모드 전용 예약"
    clone["enabled"] = False
    clone["current_run_id"] = None
    clone["current_run_status"] = None
    clone["scheduler_state"] = "paused"
    clone["lock_token"] = None
    clone["lock_owner"] = None
    clone["lock_acquired_at"] = None
    clone["lock_expires_at"] = None
    clone["updated_at"] = "2026-04-24T00:10:00+00:00"
    schedules.append(clone)
    path.write_text(json.dumps(schedules, ensure_ascii=False, indent=2), encoding="utf-8")
    return clone


@pytest.mark.parametrize("mode", ["single_node_file_backed", "db_backed"])
@pytest.mark.parametrize("reader", ["list", "detail"])
def test_unchanged_schedule_reads_preserve_files_and_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, reader: str,
) -> None:
    _profile_dir, schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setenv("TOPIC_SCHEDULER_RUNTIME_MODE", mode)
    topic_profiles.list_topic_profiles()
    first = topic_schedules.list_topic_schedules()["items"]
    assert first
    schedule_id = first[0]["schedule_id"]
    path = _schedule_path(schedule_dir)
    # A fixed timestamp makes accidental atomic replacement observable without sleeps.
    os.utime(path, ns=(1_700_000_000_000_000_000,) * 2)
    before_file = (path.read_bytes(), path.stat().st_mtime_ns)
    db_path = (
        db_bootstrap.metadata_index_path()
        if mode == "single_node_file_backed" else db_bootstrap.scheduler_db_path()
    )
    with closing(sqlite3.connect(db_path)) as observer:
        before_version = observer.execute("PRAGMA data_version").fetchone()[0]
        for _ in range(2):
            if reader == "list":
                # First DB import and subsequent SELECT use different row ordering.
                actual = topic_schedules.list_topic_schedules()["items"]
                assert {item["schedule_id"]: item for item in actual} == {
                    item["schedule_id"]: item for item in first
                }
            else:
                assert topic_schedules.get_topic_schedule(schedule_id) == first[0]
        assert observer.execute("PRAGMA data_version").fetchone()[0] == before_version
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before_file


def test_stable_schedule_api_read_does_not_wait_for_metadata_writer(client, isolated_run_repo) -> None:
    first = client.get("/topic-schedules")
    assert first.status_code == 200, first.text
    assert first.json()["items"]
    # Real competing transaction: the GET must succeed while the writer is held,
    # without changing SQLite timeouts or pretending a failed read is an empty list.
    with closing(sqlite3.connect(isolated_run_repo["metadata_index_path"])) as writer:
        writer.execute("BEGIN IMMEDIATE")
        try:
            response = client.get("/topic-schedules")
            assert response.status_code == 200, response.text
            assert response.json()["items"] == first.json()["items"]
            assert response.json()["degraded"] is False
            assert response.json()["warnings"] == []
        finally:
            writer.rollback()


@pytest.mark.parametrize("mode", ["single_node_file_backed", "db_backed"])
@pytest.mark.parametrize("reader", ["list", "detail"])
@pytest.mark.parametrize("change", ["missing_run", "completed_run", "expired_lease", "next_run"])
def test_schedule_reads_still_persist_runtime_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, reader: str, change: str,
) -> None:
    _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setenv("TOPIC_SCHEDULER_RUNTIME_MODE", mode)
    topic_profiles.list_topic_profiles()
    schedule_id = topic_schedules.list_topic_schedules()["items"][0]["schedule_id"]
    backend = topic_schedules.get_scheduler_backend()
    raw = backend.load_task_definitions()
    target = next(item for item in raw if item["schedule_id"] == schedule_id)
    if change in {"missing_run", "completed_run"}:
        target.update(current_run_id="synthetic-run", current_run_status="running")
        if change == "completed_run":
            monkeypatch.setattr(topic_schedules, "load_run_record", lambda _run_id: {
                "status": "completed", "finished_at": "2026-04-24T00:00:00+00:00",
            })
    elif change == "expired_lease":
        target.update(lock_token="old-lease", lock_owner="stopped-worker",
                      lock_expires_at="2020-01-01T00:00:00+00:00")
    else:
        target.update(enabled=True, next_run_at=None)
    backend.save_task_definitions(raw)
    if reader == "list":
        result = next(item for item in topic_schedules.list_topic_schedules()["items"]
                      if item["schedule_id"] == schedule_id)
    else:
        result = topic_schedules.get_topic_schedule(schedule_id)
    if change in {"missing_run", "completed_run"}:
        assert result["current_run_id"] is None
        assert result["current_run_status"] is None
        if change == "completed_run":
            assert result["last_status"] == "completed"
            assert result["last_success_at"] == "2026-04-24T00:00:00+00:00"
    elif change == "expired_lease":
        assert result["lock_token"] is None
        assert result["last_recovery_at"]
    else:
        assert result["next_run_at"]
        assert result["scheduler_state"] == "active"
    # Reopen through a new backend; a response-only mutation would fail this.
    persisted = next(item for item in topic_schedules.get_scheduler_backend().load_task_definitions()
                     if item["schedule_id"] == schedule_id)
    assert persisted == result


def test_scheduler_cycle_calculates_next_run_and_triggers_due_schedule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _profile_dir, schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    topic_profiles.list_topic_profiles()
    topic_schedules.list_topic_schedules()

    started_runs: list[dict[str, str | None]] = []

    def fake_start_run(request, source="api", owner_user_id=None, topic_profile_id=None, schedule_context=None):
        run_id = f"auto-{len(started_runs) + 1}"
        started_runs.append(
            {
                "run_id": run_id,
                "topic": request.topic,
                "source": source,
                "owner_user_id": owner_user_id,
                "topic_profile_id": topic_profile_id,
                "schedule_context": schedule_context,
            }
        )
        return {"run_id": run_id, "topic": request.topic, "status": "queued"}

    monkeypatch.setattr("app.services.runs.start_run", fake_start_run)

    now = datetime(2026, 4, 24, 0, 0, tzinfo=timezone.utc)
    schedule_id = "schedule-large-language-models-collect_analyze"
    _set_schedule_due(schedule_dir, schedule_id, due_at=now.isoformat())

    result = topic_schedules.run_scheduler_cycle(now=now, recover_orphans=True)

    assert result["executed_schedule_ids"] == [schedule_id]
    persisted = next(
        item
        for item in json.loads(_schedule_path(schedule_dir).read_text(encoding="utf-8"))
        if item["schedule_id"] == schedule_id
    )
    assert persisted["current_run_id"] == "auto-1"
    assert persisted["scheduler_state"] == "running"
    assert persisted["next_run_at"]
    assert persisted["next_run_at"] > now.isoformat()
    assert started_runs[0]["source"] == "topic_schedule_auto:collect_analyze"


def test_worker_restores_persisted_schedule_on_startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _profile_dir, schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    topic_profiles.list_topic_profiles()
    topic_schedules.list_topic_schedules()

    started_runs: list[str] = []

    def fake_start_run(request, source="api", owner_user_id=None, topic_profile_id=None, schedule_context=None):
        run_id = f"restored-{len(started_runs) + 1}"
        started_runs.append(run_id)
        return {"run_id": run_id, "topic": request.topic, "status": "queued"}

    monkeypatch.setattr("app.services.runs.start_run", fake_start_run)

    due_time = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    _set_schedule_due(schedule_dir, "schedule-large-language-models-weekly_report", due_at=due_time)

    worker = TopicScheduleWorker(enabled=True, tick_seconds=0.2)
    cycle_finished = Event()
    original_cycle = worker.run_cycle_now
    cycle_results = []

    def observe_cycle(*, recover_orphans=False):
        result = original_cycle(recover_orphans=recover_orphans)
        cycle_results.append(result)
        cycle_finished.set()
        return result

    monkeypatch.setattr(worker, "run_cycle_now", observe_cycle)
    worker.start()
    try:
        # A fixed sleep followed by bounded shutdown never proved startup finished.
        assert cycle_finished.wait(10), "initial scheduler cycle did not finish"
    finally:
        thread = worker._thread
        worker.stop()
        # The product's bounded stop may retain a draining thread. Test cleanup
        # must finish its own worker before monkeypatch restores fixture paths.
        if thread is not None:
            thread.join(timeout=10)
            assert not thread.is_alive(), "scheduler fixture worker did not stop"

    assert started_runs == ["restored-1"]
    assert cycle_results[0]["executed_schedule_ids"] == ["schedule-large-language-models-weekly_report"]
    persisted = next(
        item
        for item in topic_schedules.list_topic_schedules()["items"]
        if item["schedule_id"] == "schedule-large-language-models-weekly_report"
    )
    assert persisted["last_run_id"] == "restored-1"
    assert persisted["next_run_at"]


def test_compute_next_run_at_returns_real_schedule_clock() -> None:
    now = datetime(2026, 4, 24, 0, 5, tzinfo=timezone.utc)

    daily = topic_schedules.compute_next_run_at("daily_0900", now=now, timezone_name="Asia/Seoul", inclusive=True)
    weekly = topic_schedules.compute_next_run_at("weekly_monday_1000", now=now, timezone_name="Asia/Seoul", inclusive=True)
    monthly = topic_schedules.compute_next_run_at("monthly_day_1_1000", now=now, timezone_name="Asia/Seoul", inclusive=True)

    assert daily.startswith("2026-04-25T00:00:00+00:00")
    assert weekly.startswith("2026-04-27T01:00:00+00:00")
    assert monthly.startswith("2026-05-01T01:00:00+00:00")


def test_duplicate_enqueue_guard_blocks_second_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _profile_dir, _schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    topic_profiles.list_topic_profiles()
    topic_schedules.list_topic_schedules()

    monkeypatch.setattr(
        "app.services.runs.start_run",
        lambda request, source="api", owner_user_id=None, topic_profile_id=None, schedule_context=None: {
            "run_id": "queued-run-1",
            "topic": request.topic,
            "status": "queued",
        },
    )
    monkeypatch.setattr(
        topic_schedules,
        "load_run_record",
        lambda run_id: {"run_id": run_id, "status": "queued", "updated_at": "2026-04-24T00:00:00+00:00"},
    )

    topic_schedules.trigger_topic_schedule_run("schedule-large-language-models-collect_analyze", trigger="manual")

    with pytest.raises(RuntimeError, match="이미 실행 중인 예약 작업"):
        topic_schedules.trigger_topic_schedule_run("schedule-large-language-models-collect_analyze", trigger="manual")


def test_cross_process_dispatch_lease_blocks_duplicate_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _profile_dir, _schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    topic_profiles.list_topic_profiles()
    topic_schedules.list_topic_schedules()

    raw_schedules = topic_schedules._materialize_raw_schedules()
    schedule = next(item for item in raw_schedules if item["schedule_id"] == "schedule-large-language-models-weekly_report")
    topic_schedules._acquire_dispatch_lease(schedule, worker_id="worker-a")
    topic_schedules._save_schedules(raw_schedules)

    with pytest.raises(RuntimeError, match="dispatch lock"):
        topic_schedules.trigger_topic_schedule_run(
            "schedule-large-language-models-weekly_report",
            trigger="automatic",
            worker_id="worker-b",
        )


def test_worker_heartbeat_status_is_persisted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _profile_dir, _schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.delenv("TOPIC_SCHEDULER_MODE", raising=False)
    monkeypatch.setenv("TOPIC_SCHEDULER_ENABLED", "1")

    topic_schedules.record_scheduler_heartbeat(
        worker_id="external-worker-1",
        mode="external",
        enabled=True,
        running=True,
        tick_seconds=0.5,
        last_cycle_summary={"executed_schedule_ids": ["schedule-1"], "errors": []},
    )

    status = topic_schedules.get_scheduler_runtime_status(expected_mode="external")

    assert status["mode"] == "external"
    assert status["status"] == "healthy"
    assert status["running"] is True
    assert status["worker_id"] == "external-worker-1"
    assert status["last_cycle_summary"]["executed_schedule_ids"] == ["schedule-1"]
    assert status["configured_mode"] == "external"
    assert status["configured_mode_raw"] == "external"
    assert status["configured_mode_valid"] is True
    assert status["runtime_mode"] == "single_node_file_backed"
    assert status["runtime_mode_status"] == "healthy"
    assert status["storage_backend"] == "file_json"
    assert status["distributed_ready"] is False
    assert status["multi_node_dispatch_safe"] is False
    assert status["dispatch_safety"]["dispatch_leases"] is True
    assert status["dispatch_safety"]["duplicate_execution_protection"] is True
    assert status["dispatch_safety"]["orphan_recovery"] is True
    assert status["dispatch_safety"]["multi_node_dispatch_safe"] is False
    assert status["requires_external_worker"] is True
    assert status["operator_message"]
    assert status["schedule_summary"]["total"] == 0
    assert status["store_lock"]["locked"] is False


def test_scheduler_status_uses_current_config_over_stale_worker_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile_dir, _schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.delenv("TOPIC_SCHEDULER_MODE", raising=False)
    topic_schedules.record_scheduler_heartbeat(
        worker_id="old-external-worker",
        mode="external",
        enabled=True,
        running=True,
        tick_seconds=0.5,
        last_cycle_summary={"executed_schedule_ids": [], "errors": []},
    )

    status = topic_schedules.get_scheduler_runtime_status(expected_mode="embedded")

    assert status["mode"] == "embedded"
    assert status["configured_mode"] == "embedded"
    assert status["worker_mode"] == "external"
    assert status["status_snapshot"]["mode"] == "external"
    assert status["worker_id"] == "old-external-worker"


def test_scheduler_status_summarizes_worker_dispatch_and_lease_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile_dir, schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    topic_profiles.list_topic_profiles()
    topic_schedules.list_topic_schedules()
    schedules = json.loads(_schedule_path(schedule_dir).read_text(encoding="utf-8"))
    first = schedules[0]
    first["enabled"] = True
    first["last_run_at"] = "2026-04-24T00:01:00+00:00"
    first["last_success_at"] = "2026-04-24T00:02:00+00:00"
    first["last_worker_id"] = "worker-success"
    first["dispatch_attempts"] = 3
    first["last_recovery_at"] = "2026-04-24T00:00:30+00:00"
    first["last_recovery_message"] = "만료된 dispatch lease를 회수했습니다: stale-worker"
    second = schedules[1]
    second["enabled"] = True
    second["last_run_at"] = "2026-04-24T00:03:00+00:00"
    second["last_failure_at"] = "2026-04-24T00:04:00+00:00"
    second["last_failure_message"] = "provider failed"
    second["lock_token"] = "lease-active"
    second["lock_owner"] = "worker-leased"
    second["lock_acquired_at"] = datetime.now(timezone.utc).isoformat()
    second["lock_expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
    _schedule_path(schedule_dir).write_text(json.dumps(schedules, ensure_ascii=False, indent=2), encoding="utf-8")

    topic_schedules.record_scheduler_heartbeat(
        worker_id="worker-visible",
        mode="external",
        enabled=True,
        running=True,
        tick_seconds=5,
        last_cycle_summary={"executed_schedule_ids": [first["schedule_id"]], "errors": ["provider failed"]},
    )

    status = topic_schedules.get_scheduler_runtime_status(expected_mode="external")

    assert status["currently_leased_task_count"] == 1
    assert status["lease_health"]["active"] == 1
    assert status["lease_health"]["expired"] == 0
    assert status["due_task_count"] >= 0
    assert status["last_dispatch_at"] == "2026-04-24T00:03:00+00:00"
    assert status["last_success_at"] == "2026-04-24T00:02:00+00:00"
    assert status["last_failure_at"] == "2026-04-24T00:04:00+00:00"
    assert status["last_failure_message"] == "provider failed"
    assert status["last_dispatch_worker_id"] == "worker-leased"
    assert status["orphan_recovery_count"] == 1
    assert status["last_orphan_recovery_at"] == "2026-04-24T00:00:30+00:00"


def test_worker_cli_status_outputs_scheduler_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _profile_dir, _schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    topic_schedules.record_scheduler_heartbeat(
        worker_id="cli-status-worker",
        mode="external",
        enabled=True,
        running=True,
        tick_seconds=5,
        last_cycle_summary={"executed_schedule_ids": [], "errors": []},
    )

    result = topic_schedule_worker.main(["--status"])

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["worker_id"] == "cli-status-worker"
    assert payload["runtime_mode"] == "single_node_file_backed"
    assert payload["storage_backend"] == "file_json"
    assert payload["distributed_ready"] is False


def test_worker_cli_status_preserves_unicode_json_on_legacy_redirected_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setenv("TOPIC_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("TOPIC_SCHEDULER_MODE", "external")
    monkeypatch.setenv("SCHEDULER_MODE", "single_node_file_backed")
    expected = topic_schedules.get_scheduler_runtime_status(expected_mode="external")
    assert any(ord(character) > 127 for character in expected["operator_message"])
    # Freeze the observed status so timestamps cannot obscure a lossless JSON check.
    monkeypatch.setattr(topic_schedules, "get_scheduler_runtime_status", lambda **_kwargs: expected)
    buffer = io.BytesIO()
    output = io.TextIOWrapper(buffer, encoding="cp1252", errors="strict")
    with monkeypatch.context() as capture:
        capture.setattr(sys, "stdout", output)
        exit_code = topic_schedule_worker.main(["--status"])
        output.flush()
    assert exit_code == 0
    assert json.loads(buffer.getvalue()) == expected


def test_worker_cli_check_reports_queue_backed_preflight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _profile_dir, _schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setattr(db_bootstrap, "STORAGE_ROOT", constants.STORAGE_ROOT)

    result = topic_schedule_worker.main(["--check", "--runtime-mode", "queue_backed"])

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["runtime_mode"] == "queue_backed"
    assert payload["storage_backend"] == "sqlite_queue"
    assert payload["configured_runtime_mode_valid"] is True
    assert payload["queue_backend_status"] == "available"
    assert payload["distributed_support_status"] == "gated"
    assert payload["multi_node_dispatch_safe"] is False


def test_worker_cli_check_fails_for_missing_queue_backend_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _profile_dir, schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    topic_profiles.list_topic_profiles()
    topic_schedules.list_topic_schedules()
    _set_schedule_due(schedule_dir, "schedule-large-language-models-weekly_report", due_at="2026-04-24T00:00:00+00:00")

    def broken_connection():
        raise RuntimeError("scheduler queue db unavailable")

    monkeypatch.setattr(topic_schedules, "connect_scheduler_db", broken_connection)

    result = topic_schedule_worker.main(["--check", "--runtime-mode", "queue_backed"])

    assert result == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["runtime_mode"] == "legacy_file_read_only"
    assert payload["configured_runtime_mode"] == "queue_backed"
    assert payload["queue_backend_status"] == "unavailable"
    assert any("scheduler queue db unavailable" in reason for reason in payload["blocking_reasons"])


def test_worker_cli_check_fails_for_invalid_runtime_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _profile_dir, _schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)

    result = topic_schedule_worker.main(["--check", "--runtime-mode", "cluster"])

    assert result == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["runtime_mode"] == "legacy_file_read_only"
    assert payload["configured_runtime_mode_valid"] is False
    assert any("Invalid argument 'cluster'" in reason for reason in payload["blocking_reasons"])


def test_worker_cli_check_fails_when_distributed_scheduler_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _profile_dir, _schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setenv("SCHEDULER_DISTRIBUTED", "true")

    result = topic_schedule_worker.main(["--check", "--runtime-mode", "queue_backed"])

    assert result == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["runtime_mode"] == "queue_backed"
    assert payload["distributed_requested"] is True
    assert payload["distributed_support_status"] == "unsupported"
    assert any("distributed multi-node scheduler is not implemented" in reason for reason in payload["blocking_reasons"])


def test_worker_cli_once_runs_single_cycle_and_prints_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _profile_dir, _schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    calls: list[bool] = []

    class FakeWorker:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run_cycle_now(self, *, recover_orphans: bool = False):
            calls.append(recover_orphans)
            return {"executed_schedule_ids": ["schedule-1"], "errors": []}

    monkeypatch.setattr(topic_schedule_worker, "TopicScheduleWorker", FakeWorker)

    result = topic_schedule_worker.main(["--once", "--recover-orphans", "--mode", "external", "--tick-seconds", "2"])

    assert result == 0
    assert calls == [True]
    payload = json.loads(capsys.readouterr().out)
    assert payload["executed_schedule_ids"] == ["schedule-1"]


def test_scheduler_backend_registry_exposes_file_db_and_queue_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _profile_dir, _schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)

    status = topic_schedules.get_scheduler_runtime_status(expected_mode="embedded")

    assert status["backend"]["mode"] == "single_node_file_backed"
    assert status["backend"]["storage_backend"] == "file_json"
    assert status["backend"]["available"] is True
    assert status["backend"]["distributed_ready"] is False
    assert status["backend"]["multi_node_dispatch_safe"] is False
    assert status["backend"]["capabilities"]["task_definition_store"] is True
    assert status["backend"]["capabilities"]["due_task_query"] is True
    assert status["backend"]["capabilities"]["dispatch_lease"] is True
    assert status["backend"]["capabilities"]["heartbeat_status"] is True
    assert status["backend"]["capabilities"]["orphan_recovery"] is True
    assert status["backend"]["capabilities"]["next_run_calculation"] is True
    assert status["backend"]["capabilities"]["run_trigger_source_tracking"] is True
    assert status["available_runtime_modes"]["single_node_file_backed"]["status"] == "available"
    assert status["available_runtime_modes"]["db_backed"]["status"] == "available"
    assert status["available_runtime_modes"]["db_backed"]["reason"]
    assert status["available_runtime_modes"]["queue_backed"]["status"] == "available"
    assert status["available_runtime_modes"]["queue_backed"]["storage_backend"] == "sqlite_queue"
    assert status["available_runtime_modes"]["queue_backed"]["multi_process_dispatch_safe"] is True
    assert status["available_runtime_modes"]["queue_backed"]["multi_node_dispatch_safe"] is False
    assert status["available_runtime_modes"]["queue_backed"]["reason"]


def test_db_scheduler_backend_acquires_dispatch_lease_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _profile_dir, _schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setattr(db_bootstrap, "STORAGE_ROOT", constants.STORAGE_ROOT)
    monkeypatch.setenv("TOPIC_SCHEDULER_RUNTIME_MODE", "db_backed")
    topic_profiles.list_topic_profiles()

    backend = topic_schedules.get_scheduler_backend()
    schedules = backend.load_task_definitions()
    schedule = next(item for item in schedules if item["schedule_id"] == "schedule-large-language-models-weekly_report")

    token = backend.acquire_dispatch_lease(schedule, worker_id="worker-a")

    assert token.startswith("lease-schedule-large-language-models-weekly-report")
    assert schedule["lock_owner"] == "worker-a"
    with pytest.raises(RuntimeError, match="dispatch lock"):
        backend.acquire_dispatch_lease({"schedule_id": schedule["schedule_id"]}, worker_id="worker-b")


def test_db_scheduler_backend_recovers_expired_dispatch_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _profile_dir, _schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setattr(db_bootstrap, "STORAGE_ROOT", constants.STORAGE_ROOT)
    monkeypatch.setenv("TOPIC_SCHEDULER_RUNTIME_MODE", "db_backed")
    topic_profiles.list_topic_profiles()

    backend = topic_schedules.get_scheduler_backend()
    schedules = backend.load_task_definitions()
    schedule = next(item for item in schedules if item["schedule_id"] == "schedule-large-language-models-weekly_report")
    expired_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    schedule["lock_token"] = "lease-expired"
    schedule["lock_owner"] = "dead-worker"
    schedule["lock_acquired_at"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    schedule["lock_expires_at"] = expired_at
    backend.save_task_definitions(schedules)

    token = backend.acquire_dispatch_lease({"schedule_id": schedule["schedule_id"]}, worker_id="worker-b")

    assert token.startswith("lease-schedule-large-language-models-weekly-report")
    refreshed = next(item for item in backend.load_task_definitions() if item["schedule_id"] == schedule["schedule_id"])
    assert refreshed["lock_owner"] == "worker-b"
    assert refreshed["lock_token"] == token


def test_db_scheduler_status_is_real_but_not_multi_node_claimed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _profile_dir, _schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setattr(db_bootstrap, "STORAGE_ROOT", constants.STORAGE_ROOT)
    monkeypatch.setenv("TOPIC_SCHEDULER_RUNTIME_MODE", "db_backed")

    status = topic_schedules.get_scheduler_runtime_status(expected_mode="external")

    assert status["runtime_mode"] == "db_backed"
    assert status["storage_backend"] == "sqlite"
    assert status["backend"]["available"] is True
    assert status["backend"]["capabilities"]["dispatch_lease"] is True
    assert status["backend"]["capabilities"]["heartbeat_status"] is True
    assert status["backend"]["multi_process_dispatch_safe"] is True
    assert status["distributed_ready"] is False
    assert status["multi_node_dispatch_safe"] is False
    assert status["available_runtime_modes"]["db_backed"]["status"] == "available"
    assert status["available_runtime_modes"]["queue_backed"]["status"] == "available"
    assert status["available_runtime_modes"]["queue_backed"]["multi_node_dispatch_safe"] is False


def test_db_scheduler_cycle_creates_automatic_run_without_losing_schedule_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _profile_dir, _schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setattr(db_bootstrap, "STORAGE_ROOT", constants.STORAGE_ROOT)
    monkeypatch.setenv("TOPIC_SCHEDULER_RUNTIME_MODE", "db_backed")
    topic_profiles.list_topic_profiles()

    started_runs: list[dict[str, object]] = []

    def fake_start_run(request, source="api", owner_user_id=None, topic_profile_id=None, schedule_context=None):
        run_id = f"db-auto-{len(started_runs) + 1}"
        started_runs.append(
            {
                "run_id": run_id,
                "source": source,
                "topic_profile_id": topic_profile_id,
                "schedule_context": schedule_context,
            }
        )
        return {"run_id": run_id, "topic": request.topic, "status": "queued"}

    monkeypatch.setattr("app.services.runs.start_run", fake_start_run)
    backend = topic_schedules.get_scheduler_backend()
    schedules = backend.load_task_definitions()
    now = datetime(2026, 4, 24, 0, 0, tzinfo=timezone.utc)
    schedule = next(item for item in schedules if item["schedule_id"] == "schedule-large-language-models-weekly_report")
    schedule["enabled"] = True
    schedule["next_run_at"] = now.isoformat()
    schedule["next_run_hint"] = now.isoformat()
    backend.save_task_definitions(schedules)

    result = topic_schedules.run_scheduler_cycle(now=now, recover_orphans=True, worker_id="db-worker-1")

    assert result["executed_schedule_ids"] == ["schedule-large-language-models-weekly_report"]
    assert started_runs[0]["source"] == "topic_schedule_auto:weekly_report"
    persisted = next(
        item
        for item in backend.load_task_definitions()
        if item["schedule_id"] == "schedule-large-language-models-weekly_report"
    )
    assert persisted["current_run_id"] == "db-auto-1"
    assert persisted["current_run_status"] == "queued"
    assert persisted["last_run_id"] == "db-auto-1"
    assert persisted["last_trigger_source"] == "automatic"
    assert persisted["last_worker_id"] == "db-worker-1"
    assert persisted["next_run_at"] > now.isoformat()
    assert persisted["lock_token"] is None


def test_db_scheduler_heartbeat_is_written_to_scheduler_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _profile_dir, _schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setattr(db_bootstrap, "STORAGE_ROOT", constants.STORAGE_ROOT)
    monkeypatch.setenv("TOPIC_SCHEDULER_RUNTIME_MODE", "db_backed")

    topic_schedules.record_scheduler_heartbeat(
        worker_id="db-worker-heartbeat",
        mode="external",
        enabled=True,
        running=True,
        tick_seconds=1.0,
        last_cycle_summary={"executed_schedule_ids": ["schedule-1"], "errors": []},
    )

    with db_bootstrap.connect_scheduler_db() as connection:
        row = connection.execute(
            "SELECT worker_id, backend_mode, running, last_cycle_summary_json FROM scheduler_worker_heartbeats WHERE worker_id = ?",
            ("db-worker-heartbeat",),
        ).fetchone()

    assert row is not None
    assert row["backend_mode"] == "db_backed"
    assert row["running"] == 1
    assert json.loads(row["last_cycle_summary_json"])["executed_schedule_ids"] == ["schedule-1"]


def test_db_scheduler_imports_legacy_file_schedules_with_explicit_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile_dir, schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setattr(db_bootstrap, "STORAGE_ROOT", constants.STORAGE_ROOT)
    topic_profiles.list_topic_profiles()
    topic_schedules.list_topic_schedules()
    schedule_id = "schedule-large-language-models-weekly_report"
    legacy_schedule = _set_schedule_due(schedule_dir, schedule_id, due_at="2026-04-24T00:00:00+00:00")
    legacy_schedule["enabled"] = True
    _schedule_path(schedule_dir).write_text(json.dumps([legacy_schedule], ensure_ascii=False, indent=2), encoding="utf-8")

    monkeypatch.setenv("TOPIC_SCHEDULER_RUNTIME_MODE", "db_backed")
    backend = topic_schedules.get_scheduler_backend()
    schedules = backend.load_task_definitions()

    imported = next(item for item in schedules if item["schedule_id"] == schedule_id)
    assert imported["enabled"] is True
    assert imported["next_run_at"] == "2026-04-24T00:00:00+00:00"
    assert imported["scheduler_backend"] == "db_backed"
    assert imported["legacy_source_backend"] == "single_node_file_backed"
    assert imported["imported_from_legacy_file_at"]
    assert imported["migration_mode"] == "import_on_first_db_read"


def test_db_scheduler_imports_legacy_file_schedules_only_when_db_store_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile_dir, schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setattr(db_bootstrap, "STORAGE_ROOT", constants.STORAGE_ROOT)
    topic_profiles.list_topic_profiles()
    topic_schedules.list_topic_schedules()

    monkeypatch.setenv("SCHEDULER_MODE", "db_backed")
    backend = topic_schedules.get_scheduler_backend()
    imported = backend.load_task_definitions()
    imported_ids = {item["schedule_id"] for item in imported}

    late_file_schedule = _append_file_only_schedule(
        schedule_dir,
        source_schedule_id="schedule-large-language-models-weekly_report",
        schedule_id="schedule-file-only-after-db-init",
    )
    reloaded = backend.load_task_definitions()
    reloaded_ids = {item["schedule_id"] for item in reloaded}

    assert late_file_schedule["schedule_id"] not in reloaded_ids
    assert reloaded_ids == imported_ids
    with db_bootstrap.connect_scheduler_db() as connection:
        db_count = connection.execute("SELECT COUNT(*) FROM scheduler_schedules").fetchone()[0]
    assert db_count == len(imported_ids)

    monkeypatch.setenv("SCHEDULER_MODE", "single_node_file_backed")
    file_ids = {item["schedule_id"] for item in topic_schedules.list_topic_schedules()["items"]}
    assert late_file_schedule["schedule_id"] in file_ids


def test_queue_backed_mode_uses_db_task_definitions_and_queue_only_dispatch_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile_dir, schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setattr(db_bootstrap, "STORAGE_ROOT", constants.STORAGE_ROOT)
    topic_profiles.list_topic_profiles()
    topic_schedules.list_topic_schedules()

    monkeypatch.setenv("SCHEDULER_MODE", "queue_backed")
    backend = topic_schedules.get_scheduler_backend()
    imported = backend.load_task_definitions()
    imported_ids = {item["schedule_id"] for item in imported}
    late_file_schedule = _append_file_only_schedule(
        schedule_dir,
        source_schedule_id="schedule-large-language-models-weekly_report",
        schedule_id="schedule-file-only-after-queue-init",
    )

    schedules = backend.load_task_definitions()
    assert {item["schedule_id"] for item in schedules} == imported_ids
    assert late_file_schedule["schedule_id"] not in imported_ids

    now = datetime(2026, 4, 24, 0, 0, tzinfo=timezone.utc)
    target_schedule_id = "schedule-large-language-models-weekly_report"
    for schedule in schedules:
        schedule["enabled"] = schedule["schedule_id"] == target_schedule_id
        if schedule["schedule_id"] == target_schedule_id:
            schedule["next_run_at"] = now.isoformat()
            schedule["next_run_hint"] = now.isoformat()
            schedule["current_run_id"] = None
            schedule["current_run_status"] = None
            schedule["scheduler_state"] = "idle"
    backend.save_task_definitions(schedules)

    synced, due_ids = backend.query_due_task_ids(backend.load_task_definitions(), now=now, worker_id="queue-compat-worker")
    backend.save_task_definitions(synced)

    assert due_ids == [target_schedule_id]
    with db_bootstrap.connect_scheduler_db() as connection:
        schedule_count = connection.execute("SELECT COUNT(*) FROM scheduler_schedules").fetchone()[0]
        queue_rows = connection.execute(
            "SELECT schedule_id, due_at, status FROM scheduler_queue_items WHERE schedule_id = ?",
            (target_schedule_id,),
        ).fetchall()
    assert schedule_count == len(imported_ids)
    assert len(queue_rows) == 1
    assert queue_rows[0]["due_at"] == now.isoformat()
    assert queue_rows[0]["status"] == "leased"


def test_switching_from_file_to_queue_after_dispatch_does_not_double_run_same_due_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile_dir, schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setattr(db_bootstrap, "STORAGE_ROOT", constants.STORAGE_ROOT)
    topic_profiles.list_topic_profiles()
    topic_schedules.list_topic_schedules()
    now = datetime(2026, 4, 24, 0, 0, tzinfo=timezone.utc)
    schedule_id = "schedule-large-language-models-weekly_report"
    _set_schedule_due(schedule_dir, schedule_id, due_at=now.isoformat())
    started_runs: list[str] = []

    def fake_start_run(request, source="api", owner_user_id=None, topic_profile_id=None, schedule_context=None):
        run_id = f"mode-switch-run-{len(started_runs) + 1}"
        started_runs.append(run_id)
        return {"run_id": run_id, "topic": request.topic, "status": "queued"}

    monkeypatch.setattr("app.services.runs.start_run", fake_start_run)

    file_result = topic_schedules.run_scheduler_cycle(now=now, worker_id="file-mode-worker")
    assert file_result["executed_schedule_ids"] == [schedule_id]
    assert started_runs == ["mode-switch-run-1"]

    monkeypatch.setenv("SCHEDULER_MODE", "queue_backed")
    queue_result = topic_schedules.run_scheduler_cycle(now=now, worker_id="queue-mode-worker")

    assert queue_result["executed_schedule_ids"] == []
    assert started_runs == ["mode-switch-run-1"]
    with db_bootstrap.connect_scheduler_db() as connection:
        queue_count = connection.execute("SELECT COUNT(*) FROM scheduler_queue_items").fetchone()[0]
    assert queue_count == 0


def test_db_scheduler_does_not_write_db_only_schedules_back_to_file_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile_dir, _schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setattr(db_bootstrap, "STORAGE_ROOT", constants.STORAGE_ROOT)
    monkeypatch.setenv("TOPIC_SCHEDULER_RUNTIME_MODE", "db_backed")
    topic_profiles.list_topic_profiles()

    custom = topic_schedules.create_topic_schedule(
        {
            "profile_id": "large-language-models",
            "title": "DB 전용 예약",
            "task_type": "collect_papers",
            "cadence": "daily_0900",
            "enabled": True,
        }
    )

    monkeypatch.setenv("TOPIC_SCHEDULER_RUNTIME_MODE", "single_node_file_backed")
    file_mode_ids = {item["schedule_id"] for item in topic_schedules.list_topic_schedules()["items"]}

    assert custom["schedule_id"] not in file_mode_ids


def test_unavailable_db_backend_renders_legacy_schedules_read_only_without_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile_dir, schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    topic_profiles.list_topic_profiles()
    topic_schedules.list_topic_schedules()
    schedule_id = "schedule-large-language-models-weekly_report"
    _set_schedule_due(schedule_dir, schedule_id, due_at="2026-04-24T00:00:00+00:00")

    def broken_connection():
        raise RuntimeError("scheduler db unavailable")

    monkeypatch.setenv("TOPIC_SCHEDULER_RUNTIME_MODE", "db_backed")
    monkeypatch.setattr(topic_schedules, "connect_scheduler_db", broken_connection)

    listing = topic_schedules.list_topic_schedules()
    status = topic_schedules.get_scheduler_runtime_status(expected_mode="external")

    assert any(item["schedule_id"] == schedule_id for item in listing["items"])
    assert listing["backend"]["mode"] == "legacy_file_read_only"
    assert listing["backend"]["read_only"] is True
    assert status["runtime_mode"] == "legacy_file_read_only"
    assert status["runtime_mode_status"] == "degraded"
    assert status["backend"]["available"] is False
    assert "scheduler db unavailable" in status["backend"]["unavailable_reason"]
    assert "read-only legacy" in status["operator_message"]
    with pytest.raises(RuntimeError, match="read-only legacy"):
        topic_schedules.run_scheduler_cycle(now=datetime(2026, 4, 24, 0, 0, tzinfo=timezone.utc), worker_id="unsafe-worker")


def test_unavailable_queue_backend_renders_legacy_schedules_read_only_without_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile_dir, schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    topic_profiles.list_topic_profiles()
    topic_schedules.list_topic_schedules()
    schedule_id = "schedule-large-language-models-weekly_report"
    _set_schedule_due(schedule_dir, schedule_id, due_at="2026-04-24T00:00:00+00:00")
    before_fallback = json.loads(_schedule_path(schedule_dir).read_text(encoding="utf-8"))
    before_ids = {item["schedule_id"] for item in before_fallback}
    before_due = next(item["next_run_at"] for item in before_fallback if item["schedule_id"] == schedule_id)
    monkeypatch.setenv("TOPIC_SCHEDULER_RUNTIME_MODE", "queue_backed")

    def broken_connection():
        raise RuntimeError("scheduler queue db unavailable")

    monkeypatch.setattr(topic_schedules, "connect_scheduler_db", broken_connection)

    listing = topic_schedules.list_topic_schedules()
    status = topic_schedules.get_scheduler_runtime_status(expected_mode="external")

    assert any(item["schedule_id"] == schedule_id for item in listing["items"])
    assert listing["backend"]["mode"] == "legacy_file_read_only"
    assert listing["backend"]["configured_backend"] == "queue_backed"
    assert status["runtime_mode"] == "legacy_file_read_only"
    assert status["backend"]["available"] is False
    assert "scheduler queue db unavailable" in status["backend"]["unavailable_reason"]
    assert "read-only legacy" in status["operator_message"]
    after_fallback = json.loads(_schedule_path(schedule_dir).read_text(encoding="utf-8"))
    assert {item["schedule_id"] for item in after_fallback} == before_ids
    assert next(item["next_run_at"] for item in after_fallback if item["schedule_id"] == schedule_id) == before_due
    with pytest.raises(RuntimeError, match="read-only legacy"):
        topic_schedules.trigger_topic_schedule_run(schedule_id, trigger="automatic", worker_id="unsafe-worker")


def test_queue_scheduler_backend_is_available_without_claiming_multi_node_distribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile_dir, _schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setattr(db_bootstrap, "STORAGE_ROOT", constants.STORAGE_ROOT)
    monkeypatch.setenv("SCHEDULER_MODE", "queue_backed")

    status = topic_schedules.get_scheduler_runtime_status(expected_mode="external")

    assert status["runtime_mode"] == "queue_backed"
    assert status["storage_backend"] == "sqlite_queue"
    assert status["backend"]["available"] is True
    assert status["backend"]["configured_runtime_mode"] == "queue_backed"
    assert status["backend"]["capabilities"]["durable_queue"] is True
    assert status["dispatch_safety"]["durable_queue"] is True
    assert status["dispatch_safety"]["duplicate_execution_protection"] is True
    assert status["dispatch_safety"]["multi_process_dispatch_safe"] is True
    assert status["distributed_ready"] is False
    assert status["multi_node_dispatch_safe"] is False
    assert status["available_runtime_modes"]["queue_backed"]["status"] == "available"
    assert status["available_runtime_modes"]["queue_backed"]["multi_process_dispatch_safe"] is True
    assert status["available_runtime_modes"]["queue_backed"]["multi_node_dispatch_safe"] is False


def test_queue_scheduler_enqueues_due_window_once_and_recovers_expired_queue_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile_dir, _schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setattr(db_bootstrap, "STORAGE_ROOT", constants.STORAGE_ROOT)
    monkeypatch.setenv("SCHEDULER_MODE", "queue_backed")
    topic_profiles.list_topic_profiles()

    backend = topic_schedules.get_scheduler_backend()
    schedules = backend.load_task_definitions()
    now = datetime(2026, 4, 24, 0, 0, tzinfo=timezone.utc)
    schedule_id = "schedule-large-language-models-weekly_report"
    schedule = next(item for item in schedules if item["schedule_id"] == schedule_id)
    schedule["enabled"] = True
    schedule["next_run_at"] = now.isoformat()
    schedule["next_run_hint"] = now.isoformat()
    backend.save_task_definitions(schedules)

    synced, first_due = backend.query_due_task_ids(backend.load_task_definitions(), now=now)
    backend.save_task_definitions(synced)
    synced_again, second_due = backend.query_due_task_ids(backend.load_task_definitions(), now=now)

    assert first_due == [schedule_id]
    assert second_due == []
    queued_schedule = next(item for item in synced if item["schedule_id"] == schedule_id)
    assert queued_schedule["queue_status"] == "leased"
    assert queued_schedule["queue_item_id"]

    with db_bootstrap.connect_scheduler_db() as connection:
        row = connection.execute(
            """
            SELECT schedule_id, due_at, status, lease_owner, dispatch_attempts
            FROM scheduler_queue_items
            WHERE schedule_id = ?
            """,
            (schedule_id,),
        ).fetchone()
        assert row is not None
        assert row["status"] == "leased"
        assert row["due_at"] == now.isoformat()
        assert row["dispatch_attempts"] == 1
        connection.execute(
            """
            UPDATE scheduler_queue_items
            SET status = 'leased', lease_expires_at = ?
            WHERE schedule_id = ?
            """,
            ((now - timedelta(seconds=1)).isoformat(), schedule_id),
        )

    recovered, third_due = backend.query_due_task_ids(synced_again, now=now)
    recovered_schedule = next(item for item in recovered if item["schedule_id"] == schedule_id)

    assert third_due == [schedule_id]
    assert recovered_schedule["queue_status"] == "leased"
    assert recovered_schedule["last_queue_recovery_at"] == now.isoformat()


def test_queue_backend_contract_exposes_dispatch_message_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile_dir, _schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setattr(db_bootstrap, "STORAGE_ROOT", constants.STORAGE_ROOT)
    monkeypatch.setenv("SCHEDULER_MODE", "queue_backed")
    topic_profiles.list_topic_profiles()

    backend = topic_schedules.get_scheduler_backend()
    assert isinstance(backend.queue_backend, topic_schedules.SchedulerQueueBackend)
    schedules = backend.load_task_definitions()
    now = datetime(2026, 4, 24, 0, 0, tzinfo=timezone.utc)
    schedule_id = "schedule-large-language-models-weekly_report"
    schedule = next(item for item in schedules if item["schedule_id"] == schedule_id)
    schedule["enabled"] = True
    schedule["next_run_at"] = now.isoformat()
    schedule["next_run_hint"] = now.isoformat()
    backend.save_task_definitions(schedules)

    synced, due_ids = backend.query_due_task_ids(backend.load_task_definitions(), now=now)
    backend.save_task_definitions(synced)

    assert due_ids == [schedule_id]
    leased_schedule = next(item for item in synced if item["schedule_id"] == schedule_id)
    item = backend.queue_backend.get_item(str(leased_schedule["queue_item_id"]), now=now)

    assert item["queue_item_id"] == leased_schedule["queue_item_id"]
    assert item["task_id"] == schedule_id
    assert item["schedule_id"] == schedule_id
    assert item["topic_id"] == "large-language-models"
    assert item["due_at"] == now.isoformat()
    assert item["scheduled_for"] == now.isoformat()
    assert item["enqueued_at"] <= now.isoformat()
    assert item["attempt_count"] == 1
    assert item["status"] == "leased"
    assert item["worker_id"]
    assert item["lease_expires_at"] > now.isoformat()
    assert item["trigger_source"] == "automation_queue"
    assert item["run_id"] is None
    assert item["retry_eligible"] is False


def test_queue_backend_contract_acknowledges_failure_and_retry_after_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile_dir, _schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setattr(db_bootstrap, "STORAGE_ROOT", constants.STORAGE_ROOT)
    monkeypatch.setenv("SCHEDULER_MODE", "queue_backed")
    topic_profiles.list_topic_profiles()

    backend = topic_schedules.get_scheduler_backend()
    schedules = backend.load_task_definitions()
    now = datetime(2026, 4, 24, 0, 0, tzinfo=timezone.utc)
    schedule_id = "schedule-large-language-models-weekly_report"
    schedule = next(item for item in schedules if item["schedule_id"] == schedule_id)
    schedule["enabled"] = True
    schedule["next_run_at"] = now.isoformat()
    schedule["next_run_hint"] = now.isoformat()
    backend.save_task_definitions(schedules)

    synced, due_ids = backend.query_due_task_ids(backend.load_task_definitions(), now=now)
    assert due_ids == [schedule_id]
    leased_schedule = next(item for item in synced if item["schedule_id"] == schedule_id)
    queue_item_id = str(leased_schedule["queue_item_id"])
    queue_lease_token = str(leased_schedule["queue_lease_token"])

    backend.queue_backend.acknowledge(
        queue_item_id,
        run_id="queue-run-1",
        worker_id="queue-worker-ack",
        lease_token=queue_lease_token,
    )
    completed = backend.queue_backend.get_item(queue_item_id, now=now)
    assert completed["status"] == "completed"
    assert completed["run_id"] == "queue-run-1"
    assert completed["worker_id"] == "queue-worker-ack"
    assert completed["retry_eligible"] is False

    next_due = now + timedelta(days=7)
    schedules = backend.load_task_definitions()
    schedule = next(item for item in schedules if item["schedule_id"] == schedule_id)
    schedule["current_run_id"] = None
    schedule["next_run_at"] = next_due.isoformat()
    schedule["next_run_hint"] = next_due.isoformat()
    backend.save_task_definitions(schedules)
    synced, due_ids = backend.query_due_task_ids(backend.load_task_definitions(), now=next_due)
    assert due_ids == [schedule_id]
    failed_schedule = next(item for item in synced if item["schedule_id"] == schedule_id)
    failed_item_id = str(failed_schedule["queue_item_id"])
    failed_lease_token = str(failed_schedule["queue_lease_token"])

    backend.queue_backend.fail(
        failed_item_id,
        error_message="provider timeout",
        worker_id="queue-worker-fail",
        lease_token=failed_lease_token,
    )
    failed = backend.queue_backend.get_item(failed_item_id, now=next_due)
    assert failed["status"] == "failed"
    assert failed["error_message"] == "provider timeout"
    assert failed["retry_eligible"] is True

    with db_bootstrap.connect_scheduler_db() as connection:
        connection.execute(
            "UPDATE scheduler_queue_items SET status = 'leased', lease_expires_at = ? WHERE queue_item_id = ?",
            ((next_due - timedelta(seconds=1)).isoformat(), failed_item_id),
        )
    expired = backend.queue_backend.get_item(failed_item_id, now=next_due)
    assert expired["status"] == "expired"
    assert expired["retry_eligible"] is True


def test_queue_backend_rejects_stale_worker_ack_after_lease_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile_dir, _schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setattr(db_bootstrap, "STORAGE_ROOT", constants.STORAGE_ROOT)
    monkeypatch.setenv("SCHEDULER_MODE", "queue_backed")
    topic_profiles.list_topic_profiles()

    backend = topic_schedules.get_scheduler_backend()
    schedules = backend.load_task_definitions()
    now = datetime(2026, 4, 24, 0, 0, tzinfo=timezone.utc)
    schedule_id = "schedule-large-language-models-weekly_report"
    schedule = next(item for item in schedules if item["schedule_id"] == schedule_id)
    schedule["enabled"] = True
    schedule["next_run_at"] = now.isoformat()
    schedule["next_run_hint"] = now.isoformat()
    backend.save_task_definitions(schedules)

    synced, due_ids = backend.query_due_task_ids(backend.load_task_definitions(), now=now, worker_id="worker-a")
    assert due_ids == [schedule_id]
    worker_a_schedule = next(item for item in synced if item["schedule_id"] == schedule_id)
    queue_item_id = str(worker_a_schedule["queue_item_id"])
    worker_a_token = str(worker_a_schedule["queue_lease_token"])

    with db_bootstrap.connect_scheduler_db() as connection:
        connection.execute(
            "UPDATE scheduler_queue_items SET status = 'leased', lease_expires_at = ? WHERE queue_item_id = ?",
            ((now - timedelta(seconds=1)).isoformat(), queue_item_id),
        )

    recovered, recovered_due_ids = backend.query_due_task_ids(
        synced,
        now=now,
        worker_id="worker-b",
    )
    assert recovered_due_ids == [schedule_id]
    worker_b_schedule = next(item for item in recovered if item["schedule_id"] == schedule_id)
    worker_b_token = str(worker_b_schedule["queue_lease_token"])
    assert worker_b_token != worker_a_token

    with pytest.raises(RuntimeError, match="queue lease"):
        backend.queue_backend.acknowledge(
            queue_item_id,
            run_id="stale-run",
            worker_id="worker-a",
            lease_token=worker_a_token,
        )

    backend.queue_backend.acknowledge(
        queue_item_id,
        run_id="current-run",
        worker_id="worker-b",
        lease_token=worker_b_token,
    )
    completed = backend.queue_backend.get_item(queue_item_id, now=now)
    assert completed["status"] == "completed"
    assert completed["run_id"] == "current-run"
    assert completed["worker_id"] == "worker-b"


def test_queue_backend_concurrent_enqueue_attempts_create_one_due_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile_dir, _schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setattr(db_bootstrap, "STORAGE_ROOT", constants.STORAGE_ROOT)
    monkeypatch.setenv("SCHEDULER_MODE", "queue_backed")
    topic_profiles.list_topic_profiles()

    backend = topic_schedules.get_scheduler_backend()
    schedules = backend.load_task_definitions()
    now = datetime(2026, 4, 24, 0, 0, tzinfo=timezone.utc)
    schedule_id = "schedule-large-language-models-weekly_report"
    schedule = next(item for item in schedules if item["schedule_id"] == schedule_id)
    schedule["enabled"] = True
    schedule["next_run_at"] = now.isoformat()
    schedule["next_run_hint"] = now.isoformat()
    backend.save_task_definitions(schedules)

    barrier = Barrier(2)

    def discover_due(worker_id: str) -> list[str]:
        barrier.wait(timeout=5)
        _synced, due_ids = backend.query_due_task_ids(
            backend.load_task_definitions(),
            now=now,
            worker_id=worker_id,
        )
        return due_ids

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(discover_due, ["worker-a", "worker-b"]))

    assert sorted(len(result) for result in results) == [0, 1]
    assert [item for result in results for item in result] == [schedule_id]
    with db_bootstrap.connect_scheduler_db() as connection:
        rows = connection.execute(
            """
            SELECT schedule_id, due_at, status, dispatch_attempts, lease_owner
            FROM scheduler_queue_items
            WHERE schedule_id = ? AND due_at = ?
            """,
            (schedule_id, now.isoformat()),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "leased"
    assert rows[0]["dispatch_attempts"] == 1
    assert rows[0]["lease_owner"] in {"worker-a", "worker-b"}


def test_queue_backend_concurrent_lease_attempts_allow_one_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile_dir, _schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setattr(db_bootstrap, "STORAGE_ROOT", constants.STORAGE_ROOT)
    monkeypatch.setenv("SCHEDULER_MODE", "queue_backed")
    topic_profiles.list_topic_profiles()

    backend = topic_schedules.get_scheduler_backend()
    schedules = backend.load_task_definitions()
    now = datetime(2026, 4, 24, 0, 0, tzinfo=timezone.utc)
    schedule_id = "schedule-large-language-models-weekly_report"
    schedule = next(item for item in schedules if item["schedule_id"] == schedule_id)
    schedule["enabled"] = True
    schedule["next_run_at"] = now.isoformat()
    schedule["next_run_hint"] = now.isoformat()
    backend.save_task_definitions(schedules)
    with db_bootstrap.connect_scheduler_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            backend.queue_backend.enqueue(connection, schedule, enqueued_at=now)
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    barrier = Barrier(2)

    def lease_due(worker_id: str) -> tuple[bool, str | None]:
        local_schedule = json.loads(json.dumps(schedule))
        barrier.wait(timeout=5)
        with db_bootstrap.connect_scheduler_db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                leased = backend.queue_backend.lease(connection, local_schedule, now=now, worker_id=worker_id)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return leased, local_schedule.get("queue_lease_owner")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lease_due, ["worker-a", "worker-b"]))

    assert sorted(result[0] for result in results) == [False, True]
    winner = next(owner for leased, owner in results if leased)
    assert winner in {"worker-a", "worker-b"}
    with db_bootstrap.connect_scheduler_db() as connection:
        row = connection.execute(
            """
            SELECT status, dispatch_attempts, lease_owner
            FROM scheduler_queue_items
            WHERE schedule_id = ? AND due_at = ?
            """,
            (schedule_id, now.isoformat()),
        ).fetchone()
    assert row is not None
    assert row["status"] == "leased"
    assert row["dispatch_attempts"] == 1
    assert row["lease_owner"] == winner


def test_queue_scheduler_cycle_creates_automatic_run_and_completes_queue_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile_dir, _schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setattr(db_bootstrap, "STORAGE_ROOT", constants.STORAGE_ROOT)
    monkeypatch.setenv("SCHEDULER_MODE", "queue_backed")
    topic_profiles.list_topic_profiles()
    started_runs: list[dict[str, object]] = []

    def fake_start_run(request, source="api", owner_user_id=None, topic_profile_id=None, schedule_context=None):
        run_id = f"queue-auto-{len(started_runs) + 1}"
        started_runs.append(
            {
                "run_id": run_id,
                "source": source,
                "topic_profile_id": topic_profile_id,
                "schedule_context": schedule_context,
            }
        )
        return {"run_id": run_id, "topic": request.topic, "status": "queued"}

    monkeypatch.setattr("app.services.runs.start_run", fake_start_run)
    backend = topic_schedules.get_scheduler_backend()
    schedules = backend.load_task_definitions()
    now = datetime(2026, 4, 24, 0, 0, tzinfo=timezone.utc)
    schedule_id = "schedule-large-language-models-weekly_report"
    schedule = next(item for item in schedules if item["schedule_id"] == schedule_id)
    schedule["enabled"] = True
    schedule["next_run_at"] = now.isoformat()
    schedule["next_run_hint"] = now.isoformat()
    backend.save_task_definitions(schedules)

    result = topic_schedules.run_scheduler_cycle(now=now, recover_orphans=True, worker_id="queue-worker-1")

    assert result["executed_schedule_ids"] == [schedule_id]
    assert started_runs[0]["source"] == "automation_queue"
    assert started_runs[0]["schedule_context"]["trigger"] == "automatic"
    assert started_runs[0]["schedule_context"]["worker_id"] == "queue-worker-1"
    persisted = next(item for item in backend.load_task_definitions() if item["schedule_id"] == schedule_id)
    assert persisted["current_run_id"] == "queue-auto-1"
    assert persisted["last_run_id"] == "queue-auto-1"
    assert persisted["last_trigger_source"] == "automatic"
    assert persisted["last_queue_status"] == "completed"
    assert persisted["queue_item_id"] is None
    assert persisted["next_run_at"] > now.isoformat()
    with db_bootstrap.connect_scheduler_db() as connection:
        row = connection.execute(
            "SELECT status, run_id FROM scheduler_queue_items WHERE schedule_id = ?",
            (schedule_id,),
        ).fetchone()
    assert row is not None
    assert row["status"] == "completed"
    assert row["run_id"] == "queue-auto-1"


def test_queue_scheduler_cycle_records_failed_queue_item_without_losing_schedule_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile_dir, _schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setattr(db_bootstrap, "STORAGE_ROOT", constants.STORAGE_ROOT)
    monkeypatch.setenv("SCHEDULER_MODE", "queue_backed")
    topic_profiles.list_topic_profiles()

    def fail_start_run(*args, **kwargs):
        raise RuntimeError("provider timeout")

    monkeypatch.setattr("app.services.runs.start_run", fail_start_run)
    backend = topic_schedules.get_scheduler_backend()
    schedules = backend.load_task_definitions()
    now = datetime(2026, 4, 24, 0, 0, tzinfo=timezone.utc)
    schedule_id = "schedule-large-language-models-weekly_report"
    schedule = next(item for item in schedules if item["schedule_id"] == schedule_id)
    schedule["enabled"] = True
    schedule["next_run_at"] = now.isoformat()
    schedule["next_run_hint"] = now.isoformat()
    backend.save_task_definitions(schedules)

    result = topic_schedules.run_scheduler_cycle(now=now, recover_orphans=True, worker_id="queue-worker-fail")

    assert result["executed_schedule_ids"] == []
    assert result["errors"] == [f"{schedule_id}: provider timeout"]
    persisted = next(item for item in backend.load_task_definitions() if item["schedule_id"] == schedule_id)
    assert persisted["scheduler_state"] == "failed"
    assert persisted["last_error"] == "provider timeout"
    assert persisted["last_queue_status"] == "failed"
    assert persisted["queue_item_id"] is None
    assert persisted["current_run_id"] is None
    assert persisted["last_run_id"] is None
    assert persisted["next_run_at"] == now.isoformat()
    with db_bootstrap.connect_scheduler_db() as connection:
        row = connection.execute(
            "SELECT status, run_id, error_message, lease_owner FROM scheduler_queue_items WHERE schedule_id = ?",
            (schedule_id,),
        ).fetchone()
    assert row is not None
    assert row["status"] == "failed"
    assert row["run_id"] is None
    assert row["error_message"] == "provider timeout"
    assert row["lease_owner"] == "queue-worker-fail"


def test_queue_scheduler_cycle_does_not_complete_queue_item_without_real_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile_dir, _schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setattr(db_bootstrap, "STORAGE_ROOT", constants.STORAGE_ROOT)
    monkeypatch.setenv("SCHEDULER_MODE", "queue_backed")
    topic_profiles.list_topic_profiles()

    def fake_start_run_without_id(request, source="api", owner_user_id=None, topic_profile_id=None, schedule_context=None):
        return {"topic": request.topic, "status": "queued"}

    monkeypatch.setattr("app.services.runs.start_run", fake_start_run_without_id)
    backend = topic_schedules.get_scheduler_backend()
    schedules = backend.load_task_definitions()
    now = datetime(2026, 4, 24, 0, 0, tzinfo=timezone.utc)
    schedule_id = "schedule-large-language-models-weekly_report"
    schedule = next(item for item in schedules if item["schedule_id"] == schedule_id)
    schedule["enabled"] = True
    schedule["next_run_at"] = now.isoformat()
    schedule["next_run_hint"] = now.isoformat()
    backend.save_task_definitions(schedules)

    result = topic_schedules.run_scheduler_cycle(now=now, recover_orphans=True, worker_id="queue-worker-no-run")

    assert result["executed_schedule_ids"] == []
    assert result["errors"] == [f"{schedule_id}: 예약 작업 run dispatch가 run id를 반환하지 않았습니다."]
    persisted = next(item for item in backend.load_task_definitions() if item["schedule_id"] == schedule_id)
    assert persisted["scheduler_state"] == "failed"
    assert persisted["last_queue_status"] == "failed"
    assert persisted["current_run_id"] is None
    with db_bootstrap.connect_scheduler_db() as connection:
        row = connection.execute(
            "SELECT status, run_id, error_message FROM scheduler_queue_items WHERE schedule_id = ?",
            (schedule_id,),
        ).fetchone()
    assert row is not None
    assert row["status"] == "failed"
    assert row["run_id"] is None
    assert row["error_message"] == "예약 작업 run dispatch가 run id를 반환하지 않았습니다."


def test_queue_scheduler_mode_preserves_manual_run_now_without_queue_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile_dir, _schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setattr(db_bootstrap, "STORAGE_ROOT", constants.STORAGE_ROOT)
    monkeypatch.setenv("SCHEDULER_MODE", "queue_backed")
    topic_profiles.list_topic_profiles()
    started_runs: list[dict[str, object]] = []

    def fake_start_run(request, source="api", owner_user_id=None, topic_profile_id=None, schedule_context=None):
        started_runs.append({"source": source, "schedule_context": schedule_context})
        return {"run_id": "manual-run-1", "topic": request.topic, "status": "queued"}

    monkeypatch.setattr("app.services.runs.start_run", fake_start_run)

    schedule, run = topic_schedules.trigger_topic_schedule_run(
        "schedule-large-language-models-weekly_report",
        trigger="manual",
        worker_id="manual-worker",
    )

    assert run["run_id"] == "manual-run-1"
    assert len(started_runs) == 1
    assert started_runs[0]["source"] == "topic_schedule_run_now:weekly_report"
    context = started_runs[0]["schedule_context"]
    assert context["schedule_id"] == "schedule-large-language-models-weekly_report"
    assert context["profile_id"] == "large-language-models"
    assert context["task_type"] == "weekly_report"
    assert context["trigger"] == "manual"
    assert context["worker_id"] == "manual-worker"
    assert str(context["dispatch_token"]).startswith("lease-schedule-large-language-models-weekly-report-")
    assert schedule["last_trigger_source"] == "manual"
    assert schedule.get("last_queue_status") is None
    with db_bootstrap.connect_scheduler_db() as connection:
        queue_count = connection.execute("SELECT COUNT(*) FROM scheduler_queue_items").fetchone()[0]
    assert queue_count == 0


def test_invalid_scheduler_mode_status_is_explicit_and_not_distributed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _profile_dir, _schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setenv("TOPIC_SCHEDULER_MODE", "cluster")

    status = topic_schedules.get_scheduler_runtime_status(expected_mode="embedded")

    assert status["configured_mode_raw"] == "cluster"
    assert status["configured_mode"] == "embedded"
    assert status["configured_mode_valid"] is False
    assert status["runtime_mode"] == "single_node_file_backed"
    assert status["runtime_mode_status"] == "degraded"
    assert status["distributed_ready"] is False
    assert status["multi_node_dispatch_safe"] is False
    assert "지원되지 않습니다" in status["operator_message"]


def test_scheduler_mode_alias_selects_runtime_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _profile_dir, _schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setattr(db_bootstrap, "STORAGE_ROOT", constants.STORAGE_ROOT)
    monkeypatch.setenv("SCHEDULER_MODE", "db_backed")

    status = topic_schedules.get_scheduler_runtime_status(expected_mode="external")

    assert status["runtime_mode"] == "db_backed"
    assert status["storage_backend"] == "sqlite"
    assert status["backend"]["configured_runtime_mode_raw"] == "db_backed"
    assert status["backend"]["configured_runtime_mode_valid"] is True
    assert status["distributed_ready"] is False
    assert status["dispatch_safety"]["multi_process_dispatch_safe"] is True


def test_invalid_scheduler_runtime_mode_fails_safe_read_only_without_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile_dir, schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    topic_profiles.list_topic_profiles()
    topic_schedules.list_topic_schedules()
    schedule_id = "schedule-large-language-models-weekly_report"
    _set_schedule_due(schedule_dir, schedule_id, due_at="2026-04-24T00:00:00+00:00")
    monkeypatch.setenv("SCHEDULER_MODE", "cluster")

    listing = topic_schedules.list_topic_schedules()
    status = topic_schedules.get_scheduler_runtime_status(expected_mode="external")

    assert any(item["schedule_id"] == schedule_id for item in listing["items"])
    assert listing["backend"]["mode"] == "legacy_file_read_only"
    assert listing["backend"]["configured_backend"] == "cluster"
    assert listing["backend"]["configured_runtime_mode_valid"] is False
    assert status["runtime_mode"] == "legacy_file_read_only"
    assert status["runtime_mode_status"] == "degraded"
    assert status["distributed_ready"] is False
    assert status["multi_node_dispatch_safe"] is False
    assert "Invalid SCHEDULER_MODE" in status["backend"]["unavailable_reason"]
    with pytest.raises(RuntimeError, match="read-only legacy"):
        topic_schedules.run_scheduler_cycle(now=datetime(2026, 4, 24, 0, 0, tzinfo=timezone.utc), worker_id="unsafe-worker")


def test_external_worker_stop_records_stopped_heartbeat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _profile_dir, _schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.delenv("TOPIC_SCHEDULER_MODE", raising=False)
    monkeypatch.setenv("TOPIC_SCHEDULER_ENABLED", "1")
    worker = TopicScheduleWorker(enabled=True, tick_seconds=1.0, mode="external")

    worker.stop()

    status = topic_schedules.get_scheduler_runtime_status(expected_mode="external")
    assert status["mode"] == "external"
    assert status["running"] is False
    assert status["worker_id"] == worker.worker_id
    assert status["status"] == "healthy"


def test_external_mode_without_worker_is_explicitly_actionable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _profile_dir, _schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    monkeypatch.setenv("TOPIC_SCHEDULER_MODE", "external")
    monkeypatch.setenv("TOPIC_SCHEDULER_ENABLED", "1")

    status = topic_schedules.get_scheduler_runtime_status(expected_mode="embedded")

    assert status["configured_mode"] == "external"
    assert status["requires_external_worker"] is True
    assert status["running"] is False
    assert status["status"] == "stale"
    assert "python -m app.jobs.topic_schedule_worker" in status["operator_message"]


def test_recovery_marks_orphaned_active_run_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _profile_dir, schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    topic_profiles.list_topic_profiles()
    topic_schedules.list_topic_schedules()

    run_id = "orphaned-run"
    run_dir = constants.STORAGE_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "topic": "Large Language Models",
                "status": "running",
                "updated_at": "2026-04-24T00:00:00+00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    schedules = json.loads(_schedule_path(schedule_dir).read_text(encoding="utf-8"))
    schedule = next(item for item in schedules if item["schedule_id"] == "schedule-large-language-models-weekly_report")
    schedule["enabled"] = True
    schedule["current_run_id"] = run_id
    schedule["current_run_status"] = "running"
    schedule["scheduler_state"] = "running"
    _schedule_path(schedule_dir).write_text(json.dumps(schedules, ensure_ascii=False, indent=2), encoding="utf-8")

    result = topic_schedules.run_scheduler_cycle(recover_orphans=True, worker_id="recovery-worker")

    recovered = next(item for item in result["items"] if item["schedule_id"] == "schedule-large-language-models-weekly_report")
    failed_run = run_repository.load_run_record(run_id)
    assert failed_run["status"] == "failed"
    assert recovered["scheduler_state"] == "failed"
    assert recovered["last_failure_at"]


def test_expired_dispatch_lease_is_recovered_and_due_schedule_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _profile_dir, schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    topic_profiles.list_topic_profiles()
    topic_schedules.list_topic_schedules()

    started_runs: list[str] = []

    def fake_start_run(request, source="api", owner_user_id=None, topic_profile_id=None, schedule_context=None):
        run_id = f"retry-{len(started_runs) + 1}"
        started_runs.append(run_id)
        return {"run_id": run_id, "topic": request.topic, "status": "queued"}

    monkeypatch.setattr("app.services.runs.start_run", fake_start_run)

    now = datetime(2026, 4, 24, 0, 0, tzinfo=timezone.utc)
    schedule_id = "schedule-large-language-models-weekly_report"
    schedules = json.loads(_schedule_path(schedule_dir).read_text(encoding="utf-8"))
    schedule = next(item for item in schedules if item["schedule_id"] == schedule_id)
    schedule["enabled"] = True
    schedule["next_run_at"] = now.isoformat()
    schedule["lock_token"] = "lease-expired"
    schedule["lock_owner"] = "dead-worker"
    schedule["lock_acquired_at"] = (now - timedelta(minutes=5)).isoformat()
    schedule["lock_expires_at"] = (now - timedelta(minutes=1)).isoformat()
    _schedule_path(schedule_dir).write_text(json.dumps(schedules, ensure_ascii=False, indent=2), encoding="utf-8")

    result = topic_schedules.run_scheduler_cycle(now=now, recover_orphans=True, worker_id="recovery-worker")

    recovered = next(item for item in result["items"] if item["schedule_id"] == schedule_id)
    assert result["executed_schedule_ids"] == [schedule_id]
    assert started_runs == ["retry-1"]
    assert recovered["last_recovery_at"] == now.isoformat()
    assert "dead-worker" in recovered["last_recovery_message"]
    assert recovered["lock_token"] is None


def test_deleted_schedule_tombstone_is_not_duplicated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _profile_dir, schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    topic_profiles.list_topic_profiles()
    created = topic_schedules.create_topic_schedule(
        {
            "profile_id": "large-language-models",
            "title": "중복 방지 테스트 작업",
            "task_type": "weekly_report",
            "cadence": "weekly_monday_1000",
            "enabled": False,
        }
    )
    topic_schedules.delete_topic_schedule(created["schedule_id"])

    topic_schedules.list_topic_schedules()
    topic_schedules.list_topic_schedules()

    schedules = json.loads(_schedule_path(schedule_dir).read_text(encoding="utf-8"))
    matches = [item for item in schedules if item.get("schedule_id") == created["schedule_id"]]

    assert len(matches) == 1
    assert matches[0]["deleted"] is True


def test_schedule_store_lock_recovers_dead_owner_pid_before_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile_dir, schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    topic_profiles.list_topic_profiles()
    schedule_dir.mkdir(parents=True, exist_ok=True)
    lock_path = schedule_dir / "schedules.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": 999999999,
                "thread": "topic-schedule-worker",
                "acquired_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    listing = topic_schedules.list_topic_schedules()
    status = topic_schedules.get_scheduler_runtime_status()

    assert listing["items"]
    assert lock_path.exists() is False
    assert status["store_lock"]["locked"] is False


def test_schedule_store_lock_recovers_malformed_old_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _profile_dir, schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    topic_profiles.list_topic_profiles()
    schedule_dir.mkdir(parents=True, exist_ok=True)
    lock_path = schedule_dir / "schedules.lock"
    lock_path.write_text("{not-json", encoding="utf-8")
    old_mtime = time.time() - (topic_schedules.STORE_LOCK_STALE_SECONDS + 5)
    os.utime(lock_path, (old_mtime, old_mtime))

    listing = topic_schedules.list_topic_schedules()

    assert listing["items"]
    assert lock_path.exists() is False


def test_list_topic_schedules_safe_degrades_on_fresh_malformed_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile_dir, schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    topic_profiles.list_topic_profiles()
    schedule_dir.mkdir(parents=True, exist_ok=True)
    lock_path = schedule_dir / "schedules.lock"
    lock_path.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(topic_schedules, "STORE_LOCK_TIMEOUT_SECONDS", 0.05)

    listing = topic_schedules.list_topic_schedules_safe()

    assert listing["items"] == []
    assert listing["degraded"] is True
    assert listing["read_only"] is True
    assert any("예약 작업 저장소" in warning for warning in listing["warnings"])
    assert listing["store_lock"]["locked"] is True
    assert listing["store_lock"]["malformed"] is True
    assert lock_path.exists() is True


def test_schedule_store_lock_recovers_old_lock_without_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _profile_dir, schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    topic_profiles.list_topic_profiles()
    schedule_dir.mkdir(parents=True, exist_ok=True)
    lock_path = schedule_dir / "schedules.lock"
    lock_path.write_text(
        json.dumps(
            {
                "hostname": socket.gethostname(),
                "owner": "topic_schedule_store",
                "purpose": "schedule_storage_lock",
                "createdAt": (datetime.now(timezone.utc) - timedelta(seconds=topic_schedules.STORE_LOCK_STALE_SECONDS + 5)).isoformat(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    listing = topic_schedules.list_topic_schedules()

    assert listing["items"]
    assert lock_path.exists() is False


def test_schedule_store_lock_recovers_old_empty_metadata_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _profile_dir, schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    topic_profiles.list_topic_profiles()
    schedule_dir.mkdir(parents=True, exist_ok=True)
    lock_path = schedule_dir / "schedules.lock"
    lock_path.write_text("{}", encoding="utf-8")
    old_mtime = time.time() - (topic_schedules.STORE_LOCK_STALE_SECONDS + 5)
    os.utime(lock_path, (old_mtime, old_mtime))

    listing = topic_schedules.list_topic_schedules()

    assert listing["items"]
    assert lock_path.exists() is False


def test_schedule_store_lock_preserves_live_owner_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile_dir, schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    topic_profiles.list_topic_profiles()
    schedule_dir.mkdir(parents=True, exist_ok=True)
    lock_path = schedule_dir / "schedules.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "thread": "live-test",
                "acquired_at": (
                    datetime.now(timezone.utc) - timedelta(seconds=topic_schedules.STORE_LOCK_STALE_SECONDS + 5)
                ).isoformat(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(topic_schedules, "STORE_LOCK_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(RuntimeError, match="잠겨"):
        topic_schedules.list_topic_schedules()

    assert lock_path.exists() is True


def test_schedule_store_lock_preflight_recovers_stale_dead_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile_dir, schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    topic_profiles.list_topic_profiles()
    schedule_dir.mkdir(parents=True, exist_ok=True)
    lock_path = schedule_dir / "schedules.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": 999999999,
                "hostname": socket.gethostname(),
                "owner": "topic_schedule_store",
                "purpose": "schedule_storage_lock",
                "createdAt": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    status = topic_schedules.schedule_store_lock_preflight(recover_stale=True)

    assert status["ok"] is True
    assert status["store_status"] == "available"
    assert status["before"]["locked"] is True
    assert status["before"]["owner_running"] is False
    assert status["cleanup_attempted"] is True
    assert status["cleanup_result"] == "cleared"
    assert status["after"]["locked"] is False
    assert lock_path.exists() is False


def test_schedule_store_lock_preflight_preserves_live_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile_dir, schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    topic_profiles.list_topic_profiles()
    schedule_dir.mkdir(parents=True, exist_ok=True)
    lock_path = schedule_dir / "schedules.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "hostname": socket.gethostname(),
                "owner": "topic_schedule_store",
                "purpose": "schedule_storage_lock",
                "createdAt": (
                    datetime.now(timezone.utc) - timedelta(seconds=topic_schedules.STORE_LOCK_STALE_SECONDS + 5)
                ).isoformat(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    status = topic_schedules.schedule_store_lock_preflight(recover_stale=True)

    assert status["ok"] is False
    assert status["store_status"] == "locked_live_owner"
    assert status["before"]["owner_running"] is True
    assert status["cleanup_attempted"] is True
    assert status["cleanup_result"] == "not_safe_or_not_stale"
    assert status["after"]["locked"] is True
    assert lock_path.exists() is True


def test_worker_cli_lock_check_recovers_stale_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _profile_dir, schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    topic_profiles.list_topic_profiles()
    schedule_dir.mkdir(parents=True, exist_ok=True)
    lock_path = schedule_dir / "schedules.lock"
    lock_path.write_text(
        json.dumps({"pid": 999999999, "hostname": socket.gethostname()}, ensure_ascii=False),
        encoding="utf-8",
    )

    result = topic_schedule_worker.main(["--lock-check", "--recover-stale-lock"])

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["cleanup_result"] == "cleared"
    assert payload["after"]["locked"] is False
    assert lock_path.exists() is False


def test_worker_cli_lock_check_reports_live_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _profile_dir, schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    topic_profiles.list_topic_profiles()
    schedule_dir.mkdir(parents=True, exist_ok=True)
    lock_path = schedule_dir / "schedules.lock"
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "hostname": socket.gethostname()}, ensure_ascii=False),
        encoding="utf-8",
    )

    result = topic_schedule_worker.main(["--lock-check", "--recover-stale-lock"])

    assert result == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["store_status"] == "locked_live_owner"
    assert payload["after"]["locked"] is True
    assert lock_path.exists() is True


def test_worker_cli_lock_check_reports_fresh_malformed_lock_without_deleting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _profile_dir, schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    topic_profiles.list_topic_profiles()
    schedule_dir.mkdir(parents=True, exist_ok=True)
    lock_path = schedule_dir / "schedules.lock"
    lock_path.write_text("{not-json", encoding="utf-8")

    result = topic_schedule_worker.main(["--lock-check", "--recover-stale-lock"])

    assert result == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["store_status"] == "locked_unrecovered_stale"
    assert payload["after"]["locked"] is True
    assert payload["after"]["malformed"] is True
    assert lock_path.exists() is True


def test_schedule_store_lock_metadata_includes_owner_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _profile_dir, schedule_dir = _configure_topic_storage(tmp_path, monkeypatch)
    schedule_dir.mkdir(parents=True, exist_ok=True)
    lock_path = schedule_dir / "schedules.lock"

    with topic_schedules._schedule_store_lock():
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        status = topic_schedules.get_scheduler_runtime_status()["store_lock"]

    assert payload["pid"] == os.getpid()
    assert payload["process_id"] == os.getpid()
    assert payload["hostname"] == socket.gethostname()
    assert payload["worker_id"]
    assert payload["createdAt"]
    assert payload["updatedAt"]
    assert payload["heartbeatAt"]
    assert payload["owner"] == "topic_schedule_store"
    assert payload["purpose"] == "schedule_storage_lock"
    assert "schedules.json" in payload["context"]
    assert status["same_host"] is True
    assert status["owner_running"] is True
