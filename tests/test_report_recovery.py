"""Report transaction journal, crash recovery, and crash-safe locking.

All fixtures are synthetic and live under pytest ``tmp_path`` (the autouse
conftest isolation redirects the storage roots). No canonical ``storage/**``
content, provider, OAuth/OIDC, MCP connector, or network is used.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from app.repositories import runs as run_repository
from app.services import outline_history, report_transaction, reporting, workspace_actions

RUN_ID = "recovery-run"


def _section(section_id: str, title: str, body: str = "base body") -> dict[str, Any]:
    return {
        "section_id": section_id,
        "title": title,
        "kind": "draft",
        "body_markdown": body,
        "paper_ids": [],
        "compare_notes": [],
        "provenance": [],
    }


@pytest.fixture()
def report_run(tmp_path: Path) -> str:
    run_repository.ensure_run_layout(RUN_ID)
    run_repository.save_run_record({"run_id": RUN_ID, "topic": "synthetic recovery", "status": "completed"})
    reporting.save_outline(RUN_ID, [_section("sec-1", "First"), _section("sec-2", "Second")], applied_actions=[])
    return RUN_ID


def _stage_candidate(run_id: str, candidate_id: str, title: str) -> None:
    items = workspace_actions._read_staged_actions(run_id)
    items.append(
        {
            "candidate_id": candidate_id,
            "action_id": candidate_id,
            "action_type": "create_candidate_section",
            "merge_intent": "create_candidate_section",
            "editing_session_id": None,
            "order": len(items),
            "request": {
                "action_type": "create_candidate_section",
                "source_run_id": run_id,
                "source_section_title": title,
                "payload": {"title": title, "body_markdown": f"{title} body"},
            },
        }
    )
    workspace_actions._write_staged_actions(run_id, items)


def _titles(run_id: str) -> list[str]:
    return [str(s.get("title")) for s in reporting.load_outline(run_id).get("sections", [])]


def _digest(run_id: str) -> str:
    return reporting.compute_outline_draft_hash(outline=reporting.load_outline(run_id))


def _queue_ids(run_id: str) -> list[str]:
    return [str(item.get("candidate_id")) for item in workspace_actions._read_staged_actions(run_id)]


def _history_count(run_id: str) -> int:
    return len(outline_history._read_history(run_id))


# ---------------------------------------------------------------------------
# journal lifecycle
# ---------------------------------------------------------------------------


def test_successful_apply_leaves_no_journal(report_run: str) -> None:
    _stage_candidate(report_run, "cand-1", "Alpha")
    result = workspace_actions.apply_staged_workspace_actions(report_run, apply_all=True)
    assert result["status"] == "applied"
    assert not report_transaction.journal_path(report_run).exists()
    assert _queue_ids(report_run) == []
    assert _history_count(report_run) == 1


def test_validation_failure_writes_no_journal(report_run: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _stage_candidate(report_run, "cand-bad", "Bad")
    monkeypatch.setattr(
        workspace_actions,
        "_plan_workspace_action",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("invalid")),
    )
    with pytest.raises(RuntimeError):
        workspace_actions.apply_staged_workspace_actions(report_run, apply_all=True)
    assert not report_transaction.journal_path(report_run).exists()


def test_journal_write_failure_prevents_commit(report_run: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _stage_candidate(report_run, "cand-j", "Journal Fail")
    before = _titles(report_run)

    def failing_write(*_args, **_kwargs):
        raise report_transaction.ReportTransactionPrepareFailed("journal_write_failed")

    monkeypatch.setattr(report_transaction, "write_journal", failing_write)
    with pytest.raises(report_transaction.ReportTransactionPrepareFailed):
        workspace_actions.apply_staged_workspace_actions(report_run, apply_all=True)
    # Prepare failed -> zero commit, queue intact.
    assert _titles(report_run) == before
    assert _queue_ids(report_run) == ["cand-j"]


def test_journal_contains_no_content_path_or_secret(report_run: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _stage_candidate(report_run, "cand-priv", "Private Body")
    captured: dict[str, Any] = {}
    real_write = report_transaction.write_journal

    def capture(run_id, journal):
        captured.update(journal)
        return real_write(run_id, journal)

    monkeypatch.setattr(report_transaction, "write_journal", capture)
    workspace_actions.apply_staged_workspace_actions(report_run, apply_all=True)
    blob = json.dumps(captured)
    assert "Private Body body" not in blob        # no raw candidate content
    assert str(reporting._report_paths(report_run)[0]) not in blob  # no absolute path
    assert "Traceback" not in blob
    assert captured["schema_version"] == report_transaction.JOURNAL_SCHEMA_VERSION
    assert captured["base_digest"] and captured["target_digest"]


# ---------------------------------------------------------------------------
# recovery matrix
# ---------------------------------------------------------------------------


def _write_journal(run_id: str, **overrides: Any) -> dict[str, Any]:
    journal = report_transaction.build_journal(
        transaction_id="tx-test",
        kind=report_transaction.KIND_APPLY_ALL,
        run_id=run_id,
        base_digest=_digest(run_id),
        target_digest="target-digest-value",
        candidate_ids=["cand-x"],
        editing_session_ids=[],
        now="2026-01-01T00:00:00+00:00",
    )
    journal.update(overrides)
    report_transaction.write_journal(run_id, journal)
    return journal


def test_prepared_with_base_digest_aborts_precommit(report_run: str) -> None:
    _write_journal(report_run)  # base == current, prepared
    outcome = workspace_actions._recover_incomplete_transaction(report_run)
    assert outcome == report_transaction.CODE_ABORTED_PRECOMMIT
    assert not report_transaction.journal_path(report_run).exists()


def test_prepared_with_target_digest_finalizes(report_run: str) -> None:
    _stage_candidate(report_run, "cand-x", "Committed Already")
    # Journal whose target equals the CURRENT outline: the commit landed.
    _write_journal(report_run, base_digest="old-base", target_digest=_digest(report_run))
    outcome = workspace_actions._recover_incomplete_transaction(report_run)
    assert outcome == report_transaction.CODE_RECOVERED
    # Secondary state finalized: the frozen candidate is consumed exactly once.
    assert _queue_ids(report_run) == []
    assert not report_transaction.journal_path(report_run).exists()


def test_committed_journal_with_base_digest_is_recovery_required(report_run: str) -> None:
    _write_journal(report_run, state=report_transaction.STATE_COMMITTED)
    with pytest.raises(report_transaction.ReportTransactionRecoveryRequired):
        workspace_actions._recover_incomplete_transaction(report_run)
    # Journal is preserved and now pins the run closed.
    assert report_transaction.journal_path(report_run).exists()


def test_unknown_digest_is_recovery_required(report_run: str) -> None:
    _write_journal(report_run, base_digest="neither-a", target_digest="neither-b")
    with pytest.raises(report_transaction.ReportTransactionRecoveryRequired):
        workspace_actions._recover_incomplete_transaction(report_run)


def test_recovery_required_blocks_further_mutation(report_run: str) -> None:
    _write_journal(report_run, base_digest="neither-a", target_digest="neither-b")
    before = _titles(report_run)
    with pytest.raises(report_transaction.ReportTransactionRecoveryRequired):
        workspace_actions._recover_incomplete_transaction(report_run)
    _stage_candidate(report_run, "cand-blocked", "Blocked")
    with pytest.raises(report_transaction.ReportTransactionRecoveryRequired):
        workspace_actions.apply_staged_workspace_actions(report_run, apply_all=True)
    # No automatic repair: report untouched and candidate still pending.
    assert _titles(report_run) == before
    assert "cand-blocked" in _queue_ids(report_run)


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        json.dumps([1, 2, 3]),
        json.dumps({"schema_version": 999, "transaction_id": "t", "state": "prepared", "base_digest": "b", "target_digest": "t"}),
        json.dumps({"schema_version": 1, "state": "prepared"}),
    ],
)
def test_malformed_journal_is_recovery_required_and_preserved(report_run: str, payload: str) -> None:
    path = report_transaction.journal_path(report_run)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(report_transaction.ReportTransactionRecoveryRequired):
        workspace_actions._recover_incomplete_transaction(report_run)
    assert path.exists()  # never auto-deleted


def test_recovery_is_idempotent(report_run: str) -> None:
    _stage_candidate(report_run, "cand-x", "Twice")
    _write_journal(report_run, base_digest="old-base", target_digest=_digest(report_run))
    first = workspace_actions._recover_incomplete_transaction(report_run)
    second = workspace_actions._recover_incomplete_transaction(report_run)
    assert first == report_transaction.CODE_RECOVERED
    assert second is None  # nothing left to recover


def test_recovery_runs_in_a_fresh_service_instance(report_run: str) -> None:
    """A new module-level view of the same temp root recovers the transaction."""
    _stage_candidate(report_run, "cand-x", "Fresh")
    _write_journal(report_run, base_digest="old-base", target_digest=_digest(report_run))
    import importlib

    fresh = importlib.reload(workspace_actions)
    try:
        assert fresh._recover_incomplete_transaction(report_run) == report_transaction.CODE_RECOVERED
        assert _queue_ids(report_run) == []
    finally:
        importlib.reload(workspace_actions)


# ---------------------------------------------------------------------------
# post-commit failure injection
# ---------------------------------------------------------------------------


def test_history_failure_leaves_committed_report_and_recoverable_journal(
    report_run: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stage_candidate(report_run, "cand-h", "History Gap")
    original_write = outline_history._write_history

    def failing_write(*_args, **_kwargs):
        raise OSError("synthetic history failure")

    monkeypatch.setattr(outline_history, "_write_history", failing_write)
    result = workspace_actions.apply_staged_workspace_actions(report_run, apply_all=True)
    assert result["status"] == "applied"
    assert "History Gap" in _titles(report_run)
    assert report_transaction.CODE_CLEANUP_PENDING in result["warnings"]
    # Journal survives so the next mutation can finish the finalize.
    assert report_transaction.journal_path(report_run).exists()

    # Restore only this patch (monkeypatch.undo() would also revert the autouse
    # storage-root isolation, which shares this monkeypatch instance).
    monkeypatch.setattr(outline_history, "_write_history", original_write)
    outcome = workspace_actions._recover_incomplete_transaction(report_run)
    assert outcome == report_transaction.CODE_RECOVERED
    assert _queue_ids(report_run) == []
    assert not report_transaction.journal_path(report_run).exists()


def test_queue_removal_failure_is_recovered_without_reapplying(
    report_run: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stage_candidate(report_run, "cand-q", "Queue Gap")
    original_delete = workspace_actions._delete_staged_action_ids

    def failing_delete(*_args, **_kwargs):
        raise OSError("synthetic queue failure")

    monkeypatch.setattr(workspace_actions, "_delete_staged_action_ids", failing_delete)
    result = workspace_actions.apply_staged_workspace_actions(report_run, apply_all=True)
    assert result["status"] == "applied"
    assert report_transaction.CODE_CLEANUP_PENDING in result["warnings"]
    titles_after_commit = _titles(report_run)
    assert titles_after_commit.count("Queue Gap") == 1
    assert "cand-q" in _queue_ids(report_run)  # stale queue entry remains

    monkeypatch.setattr(workspace_actions, "_delete_staged_action_ids", original_delete)
    workspace_actions._recover_incomplete_transaction(report_run)
    # Recovery consumes the stale candidate WITHOUT applying it again.
    assert _queue_ids(report_run) == []
    assert _titles(report_run).count("Queue Gap") == 1


def test_journal_cleanup_failure_is_reported_not_rolled_back(
    report_run: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stage_candidate(report_run, "cand-c", "Cleanup Gap")
    monkeypatch.setattr(report_transaction, "clear_journal", lambda run_id: False)
    result = workspace_actions.apply_staged_workspace_actions(report_run, apply_all=True)
    assert result["status"] == "applied"
    assert "Cleanup Gap" in _titles(report_run)
    assert report_transaction.CODE_CLEANUP_PENDING in result["warnings"]


# ---------------------------------------------------------------------------
# history / queue idempotency
# ---------------------------------------------------------------------------


def test_history_entries_carry_transaction_identity(report_run: str) -> None:
    _stage_candidate(report_run, "cand-a", "Alpha")
    _stage_candidate(report_run, "cand-b", "Beta")
    workspace_actions.apply_staged_workspace_actions(report_run, apply_all=True)
    entries = outline_history._read_history(report_run)
    assert len(entries) == 2
    tx_ids = {str(entry.get("transaction_id")) for entry in entries}
    assert len(tx_ids) == 1 and "" not in tx_ids  # one transaction, identified
    assert {str(e.get("candidate_id")) for e in entries} == {"cand-a", "cand-b"}


def test_finalize_does_not_duplicate_history(report_run: str) -> None:
    _stage_candidate(report_run, "cand-a", "Alpha")
    workspace_actions.apply_staged_workspace_actions(report_run, apply_all=True)
    before = _history_count(report_run)
    journal = report_transaction.build_journal(
        transaction_id="tx-dup",
        kind=report_transaction.KIND_APPLY_ALL,
        run_id=report_run,
        base_digest="b",
        target_digest=_digest(report_run),
        candidate_ids=["cand-a"],
        editing_session_ids=[],
        now="2026-01-01T00:00:00+00:00",
    )
    entries = outline_history._read_history(report_run)
    # Re-running finalize with the SAME entries must not duplicate them.
    workspace_actions._finalize_committed_transaction(
        report_run, journal=journal, history_entries=entries, candidate_ids=["cand-a"], editing_session_ids=[]
    )
    assert _history_count(report_run) == before


def test_unrelated_candidates_are_preserved_by_recovery(report_run: str) -> None:
    _stage_candidate(report_run, "cand-x", "In Batch")
    _write_journal(report_run, base_digest="old-base", target_digest=_digest(report_run))
    _stage_candidate(report_run, "cand-new", "Added Later")
    workspace_actions._recover_incomplete_transaction(report_run)
    # Only the frozen candidate is consumed; the newer one still pends.
    assert _queue_ids(report_run) == ["cand-new"]


def test_idempotent_removal_of_already_deleted_candidate(report_run: str) -> None:
    journal = report_transaction.build_journal(
        transaction_id="tx-gone",
        kind=report_transaction.KIND_APPLY_ALL,
        run_id=report_run,
        base_digest="b",
        target_digest=_digest(report_run),
        candidate_ids=["never-existed"],
        editing_session_ids=[],
        now="2026-01-01T00:00:00+00:00",
    )
    warnings = workspace_actions._finalize_committed_transaction(
        report_run, journal=journal, history_entries=[], candidate_ids=["never-existed"], editing_session_ids=[]
    )
    assert report_transaction.CODE_CLEANUP_PENDING not in warnings


# ---------------------------------------------------------------------------
# crash-safe lock
# ---------------------------------------------------------------------------


def test_stale_lock_file_from_dead_process_does_not_block(report_run: str) -> None:
    """The core crash-safety property: a marker left by a dead holder is not a lock."""
    path = report_transaction._lock_path(report_run)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.close(os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600))  # crashed holder residue
    with report_transaction.report_mutation_lock(report_run, timeout_seconds=1.0):
        pass


def test_live_handle_owner_still_blocks(report_run: str) -> None:
    path = report_transaction._lock_path(report_run)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    assert report_transaction._try_acquire_os_lock(handle)
    try:
        with pytest.raises(report_transaction.ReportTransactionBusy):
            with report_transaction.report_mutation_lock(report_run, timeout_seconds=0.3):
                pass
    finally:
        report_transaction._release_os_lock(handle)
        os.close(handle)
    # Once the owner releases, the lock is immediately available again.
    with report_transaction.report_mutation_lock(report_run, timeout_seconds=1.0):
        pass


def test_lock_released_after_exception(report_run: str) -> None:
    with pytest.raises(RuntimeError):
        with report_transaction.report_mutation_lock(report_run, timeout_seconds=1.0):
            raise RuntimeError("boom")
    with report_transaction.report_mutation_lock(report_run, timeout_seconds=1.0):
        pass


def test_different_runs_lock_independently(report_run: str) -> None:
    for other in ("recovery-other",):
        run_repository.ensure_run_layout(other)
    with report_transaction.report_mutation_lock(report_run, timeout_seconds=1.0):
        with report_transaction.report_mutation_lock("recovery-other", timeout_seconds=1.0):
            pass


def test_lock_rejects_malformed_run_id() -> None:
    from app.domain.path_safety import InvalidRunId

    with pytest.raises(InvalidRunId):
        with report_transaction.report_mutation_lock("../evil"):
            pass


# ---------------------------------------------------------------------------
# read-only paths never recover or write
# ---------------------------------------------------------------------------


def _tree_fingerprint(root: Path) -> dict[str, tuple[int, float]]:
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_read_only_paths_do_not_recover_or_write(report_run: str, tmp_path: Path) -> None:
    _stage_candidate(report_run, "cand-x", "Pending")
    _write_journal(report_run)  # incomplete transaction present
    run_root = run_repository.build_run_paths(report_run).run_dir
    before = _tree_fingerprint(run_root)

    from app.api.health import get_health, get_readiness

    get_health()
    get_readiness()
    reporting.build_preview(report_run)
    reporting.load_outline(report_run)
    outline_history.list_outline_action_history(report_run)
    workspace_actions.list_staged_workspace_actions(report_run)

    assert _tree_fingerprint(run_root) == before
    # The journal is still there: reads never recover.
    assert report_transaction.journal_path(report_run).exists()
