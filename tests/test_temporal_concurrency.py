"""Competing local writers and readers use disposable databases, never user data."""

from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import os
import sqlite3
import threading

import pytest


def _capture_and_evaluate(root, barrier, results):
    os.environ["XRML_DATA_DIR"] = str(root)
    from app.db import bootstrap
    from app.domain.temporal_memory import Member
    from app.repositories import temporal_analysis as analysis, temporal_memory as memory

    bootstrap.STORAGE_ROOT = root / "runs"
    try:
        barrier.wait(timeout=20)
        policy, policy_created = analysis.create_policy("topic", {})
        snapshot, snapshot_created = memory.capture("topic", lambda _: [Member("paper", "version")])
        evaluation, evaluation_created = analysis.evaluate("topic", snapshot.snapshot_id, policy.policy_id)
        results.put(("ok", policy_created, snapshot_created, evaluation_created, evaluation.evaluation_id))
    except BaseException as exc:
        # Keep traceback information for failed races; never turn an error into success.
        import traceback

        results.put(("error", type(exc).__name__, traceback.format_exc()))


@pytest.mark.parametrize("iteration", range(3))
def test_spawn_writers_deduplicate_complete_history(tmp_path, monkeypatch, iteration):
    from app.db import bootstrap

    root = tmp_path / str(iteration)
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", root / "runs")
    ctx = multiprocessing.get_context("spawn")
    barrier, results = ctx.Barrier(4), ctx.Queue()
    workers = [ctx.Process(target=_capture_and_evaluate, args=(root, barrier, results)) for _ in range(4)]
    try:
        for worker in workers:
            worker.start()
        messages = [results.get(timeout=40) for _ in workers]
        for worker in workers:
            worker.join(10)
        assert [worker.exitcode for worker in workers] == [0] * 4
        assert all(message[0] == "ok" for message in messages), messages
        assert [sum(message[index] for message in messages) for index in (1, 2, 3)] == [1, 1, 1]
        assert len({message[4] for message in messages}) == 1
        with bootstrap.connect_metadata_index(read_only=True) as connection:
            for table, count in [("analysis_policies", 1), ("topic_snapshots", 1),
                                 ("analysis_evaluations", 1), ("temporal_metric_points", 14),
                                 ("temporal_insight_records", 6)]:
                assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == count
            assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
            if worker.pid:
                worker.join(5)
        results.close()
        results.join_thread()


def test_reader_sees_no_partial_evaluation_while_competing_writer_recovers(tmp_path, monkeypatch):
    from app.db import bootstrap
    from app.domain.temporal_memory import Member
    from app.repositories import temporal_analysis as analysis, temporal_memory as memory

    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "runs")
    snapshot, _ = memory.capture("topic", lambda _: [Member("paper", "version")])
    policy, _ = analysis.create_policy("topic", {})
    pending, release, contender = threading.Event(), threading.Event(), threading.Event()

    def checkpoint(phase):
        if phase == "after_metrics" and not pending.is_set():
            pending.set()
            assert release.wait(10)
            raise RuntimeError("rollback first writer")

    monkeypatch.setattr(analysis, "checkpoint", checkpoint)

    def evaluate():
        return analysis.evaluate("topic", snapshot.snapshot_id, policy.policy_id)

    def compete():
        contender.set()
        return evaluate()

    with ThreadPoolExecutor(max_workers=2) as pool:
        failing = pool.submit(evaluate)
        try:
            assert pending.wait(10)
            retry = pool.submit(compete)
            assert contender.wait(10)
            # A new independent connection must not see the in-flight header or children.
            with bootstrap.connect_metadata_index(read_only=True) as connection:
                for table in ("analysis_evaluations", "temporal_metric_points", "temporal_insight_records"):
                    assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
            assert not retry.done()
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="rollback first writer"):
            failing.result(timeout=10)
        evaluation, created = retry.result(timeout=10)
    assert created
    assert analysis.evaluate("topic", snapshot.snapshot_id, policy.policy_id) == (evaluation, False)
    with bootstrap.connect_metadata_index(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM analysis_evaluations").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM temporal_metric_points").fetchone()[0] == 14
        assert connection.execute("SELECT count(*) FROM temporal_insight_records").fetchone()[0] == 6


def test_busy_evaluation_has_no_partial_write_and_can_be_retried(tmp_path, monkeypatch):
    from app.db import bootstrap
    from app.domain.temporal_memory import Member
    from app.repositories import temporal_analysis as analysis, temporal_memory as memory

    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "runs")
    snapshot, _ = memory.capture("topic", lambda _: [Member("paper", "version")])
    policy, _ = analysis.create_policy("topic", {})
    # Force an actual SQLite lock failure quickly; production's 5 s timeout stays unchanged.
    real_connect = analysis.connect_metadata_index

    def short_wait():
        connection = real_connect()
        connection.execute("PRAGMA busy_timeout = 50")
        return connection

    with bootstrap.connect_metadata_index() as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        monkeypatch.setattr(analysis, "connect_metadata_index", short_wait)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            analysis.evaluate("topic", snapshot.snapshot_id, policy.policy_id)
        blocker.rollback()
    monkeypatch.setattr(analysis, "connect_metadata_index", real_connect)
    assert analysis.series("topic", "corpus.paper_count")["total"] == 0
    assert analysis.insights("topic")["total"] == 0
    evaluation, created = analysis.evaluate("topic", snapshot.snapshot_id, policy.policy_id)
    assert created
    assert analysis.evaluate("topic", snapshot.snapshot_id, policy.policy_id) == (evaluation, False)
