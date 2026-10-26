"""Startup can inspect an initialized WAL store without waiting for its users."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
from queue import Queue
import subprocess
import sys
from threading import Event
from threading import Thread
import sqlite3

import httpx
import pytest

from app.db import bootstrap
from app.db.sqlite_integrity import DEFAULT_SCHEDULER_WRITE_TIMEOUT_SECONDS
from tests.test_scheduler_bootstrap_atomicity import seed_existing_rows, snapshot


@pytest.mark.parametrize("holder_kind", ["writer", "reader"])
def test_runtime_bootstrap_finishes_while_wal_transaction_remains_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, holder_kind: str,
) -> None:
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "runs")
    bootstrap.bootstrap_runtime_stores()
    seed_existing_rows()
    store = bootstrap.scheduler_db_path(ensure_parent=False)
    baseline = snapshot(store)
    original_connect = bootstrap.connect_scheduler_db
    connected = Event()
    connections: list[sqlite3.Connection] = []
    statements: list[str] = []

    def observed_connect() -> sqlite3.Connection:
        connection = original_connect()
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == int(
            DEFAULT_SCHEDULER_WRITE_TIMEOUT_SECONDS * 1000
        )
        connections.append(connection)
        connection.set_trace_callback(statements.append)
        connected.set()
        return connection

    def start() -> dict[str, str]:
        result = bootstrap.bootstrap_runtime_stores()
        # Check handle lifetime on its owning thread, not the test thread.
        for connection in connections:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                connection.execute("SELECT 1")
        return result

    with closing(original_connect()) as holder:
        if holder_kind == "writer":
            holder.execute("BEGIN IMMEDIATE")
            holder.execute("UPDATE run_jobs SET state = 'running' WHERE job_id = 'job-1'")
        else:
            holder.execute("BEGIN")
            assert holder.execute("SELECT state FROM run_jobs WHERE job_id = 'job-1'").fetchone()[0] == "queued"
            with closing(original_connect()) as writer:
                writer.execute("UPDATE run_jobs SET state = 'running' WHERE job_id = 'job-1'")
            assert holder.execute("SELECT state FROM run_jobs WHERE job_id = 'job-1'").fetchone()[0] == "queued"
        monkeypatch.setattr(bootstrap, "connect_scheduler_db", observed_connect)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(start)
            try:
                assert connected.wait(10), "startup did not finish schema validation"
                # Synchronize after real schema checks. Do not shorten SQLite's
                # production busy timeout or release the holder to make startup pass.
                try:
                    result = future.result(timeout=2)
                except TimeoutError:
                    pytest.fail(f"startup waited for an unrelated {holder_kind}: {statements}")
                assert result == {
                    "metadata_index": "bootstrap_completed", "scheduler": "bootstrap_completed",
                    "run_executor": "bootstrap_completed",
                }
                assert holder.in_transaction
                with closing(bootstrap.connect_scheduler_db_read_only()) as reader:
                    expected = "queued" if holder_kind == "writer" else "running"
                    assert reader.execute("SELECT state FROM run_jobs WHERE job_id = 'job-1'").fetchone()[0] == expected
                    assert reader.execute("PRAGMA foreign_key_check").fetchall() == []
                assert holder.execute("SELECT state FROM run_jobs WHERE job_id = 'job-1'").fetchone()[0] == (
                    "running" if holder_kind == "writer" else "queued"
                )
            finally:
                holder.rollback()
                future.result(timeout=35)
    if holder_kind == "writer":
        assert snapshot(store) == baseline
    else:
        after = snapshot(store)
        assert after[0] == baseline[0] and after[2] == "wal"
        with closing(original_connect()) as reader:
            assert reader.execute("SELECT state FROM run_jobs WHERE job_id = 'job-1'").fetchone()[0] == "running"


@pytest.mark.parametrize("holder_kind", ["writer", "reader"])
def test_sidecar_readiness_and_restart_preserve_jobs_with_another_wal_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, holder_kind: str,
) -> None:
    data_root = tmp_path / "runtime-data"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", data_root / "runs")
    bootstrap.bootstrap_runtime_stores()
    seed_existing_rows()
    dist = tmp_path / "web-dist"
    dist.mkdir()
    (dist / "index.html").write_text("<main>Startup fixture</main>", encoding="utf-8")
    environment = {
        **os.environ, "XRML_DATA_DIR": str(data_root), "AUTH_MODE": "disabled",
        "TOPIC_SCHEDULER_ENABLED": "0", "TOPIC_SCHEDULER_MODE": "disabled",
        "XRML_RUN_EXECUTOR_MODE": "disabled", "XRML_PROVIDER": "disabled",
        "XRML_JOURNAL_OFFLINE_ENABLED": "0", "XRML_REVIEW_OFFLINE_ENABLED": "0",
        "XRML_REVIEW_EXECUTION_OFFLINE_ENABLED": "0", "XRML_PAPER_TRANSPORT_OFFLINE_ENABLED": "0",
    }

    def ready_and_stop(label: str) -> None:
        with (tmp_path / f"{label}.stderr.log").open("w", encoding="utf-8") as errors:
            process = subprocess.Popen(
                [sys.executable, "-m", "app.desktop.sidecar", "--web-dist", str(dist),
                 "--data-dir", str(data_root), "--startup-timeout-seconds", "20"],
                cwd=Path(__file__).resolve().parents[1], env=environment,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                text=True, encoding="utf-8",
            )
            assert process.stdout is not None and process.stdin is not None
            lines: Queue[str] = Queue()
            reader = Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True)
            reader.start()
            try:
                event = json.loads(lines.get(timeout=60))
                assert event["event"] == "ready" and event["host"] == "127.0.0.1"
                with httpx.Client(base_url=f"http://127.0.0.1:{event['port']}", trust_env=False) as client:
                    response = client.get("/health/ready")
                    assert response.status_code == 200
                    assert response.json()["runtime_bootstrap"] == {"status": "completed"}
                    assert response.json()["runtime_stores"]["ready"] is True
                    assert client.get("/app").text == "<main>Startup fixture</main>"
                process.stdin.write("shutdown\n")
                process.stdin.flush()
                assert process.wait(timeout=15) == 0
                remaining = [json.loads(line) for line in process.stdout.read().splitlines() if line.strip()]
                assert remaining[-1]["event"] == "shutdown" and remaining[-1]["reason"] == "host_requested"
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                reader.join(timeout=5)
                process.stdin.close()
                process.stdout.close()

    with closing(bootstrap.connect_scheduler_db()) as holder:
        if holder_kind == "writer":
            holder.execute("BEGIN IMMEDIATE")
            holder.execute("UPDATE run_jobs SET state = 'running' WHERE job_id = 'job-1'")
        else:
            holder.execute("BEGIN")
            assert holder.execute("SELECT state FROM run_jobs WHERE job_id = 'job-1'").fetchone()[0] == "queued"
            with closing(bootstrap.connect_scheduler_db()) as writer:
                writer.execute("UPDATE run_jobs SET state = 'running' WHERE job_id = 'job-1'")
        try:
            ready_and_stop("held")
            assert holder.in_transaction
            assert holder.execute("SELECT state FROM run_jobs WHERE job_id = 'job-1'").fetchone()[0] == (
                "running" if holder_kind == "writer" else "queued"
            )
            holder.commit()
        finally:
            holder.rollback()
    ready_and_stop("restart")
    with closing(bootstrap.connect_scheduler_db_read_only()) as connection:
        assert [tuple(row) for row in connection.execute("SELECT job_id, run_id, state FROM run_jobs")] == [
            ("job-1", "run-1", "running")
        ]
        assert connection.execute("SELECT profile_id FROM scheduler_schedules").fetchone()[0] == "profile-1"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
