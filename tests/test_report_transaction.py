"""Report apply-all transaction: all-or-nothing, concurrency, and recovery.

Every fixture is synthetic and lives under pytest ``tmp_path`` (the autouse
conftest isolation redirects the storage roots). No canonical ``storage/**``
content, provider, OAuth/OIDC, MCP connector, or network is used.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from app.services import report_transaction, reporting, workspace_actions
from app.repositories import runs as run_repository


# ---------------------------------------------------------------------------
# synthetic fixtures
# ---------------------------------------------------------------------------


RUN_ID = "transaction-run"


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
    """A synthetic run with a two-section outline and no staged candidates."""
    run_repository.ensure_run_layout(RUN_ID)
    run_repository.save_run_record({"run_id": RUN_ID, "topic": "synthetic transaction", "status": "completed"})
    reporting.save_outline(RUN_ID, [_section("sec-1", "First"), _section("sec-2", "Second")], applied_actions=[])
    return RUN_ID


def _stage_candidate(run_id: str, candidate_id: str, title: str) -> None:
    """Stage one create_candidate_section candidate directly in the queue file."""
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


def _outline_titles(run_id: str) -> list[str]:
    return [str(s.get("title")) for s in reporting.load_outline(run_id).get("sections", [])]


def _outline_digest(run_id: str) -> str:
    return reporting.compute_outline_draft_hash(outline=reporting.load_outline(run_id))


# ---------------------------------------------------------------------------
# pure planner: no persistence
# ---------------------------------------------------------------------------


def test_planner_makes_no_persistent_write(report_run: str) -> None:
    outline = reporting.load_outline(report_run)
    before_digest = _outline_digest(report_run)
    plan = workspace_actions._plan_workspace_action(
        report_run,
        {
            "action_type": "create_candidate_section",
            "source_run_id": report_run,
            "payload": {"title": "Planned", "body_markdown": "planned body"},
        },
        outline=outline,
    )
    # Plan describes the target but changes nothing on disk.
    assert any(s.get("title") == "Planned" for s in plan["sections"])
    assert _outline_digest(report_run) == before_digest
    assert "Planned" not in _outline_titles(report_run)


def test_planner_is_deterministic(report_run: str) -> None:
    outline = reporting.load_outline(report_run)
    payload = {
        "action_type": "create_candidate_section",
        "source_run_id": report_run,
        "candidate_id": "cand-fixed",
        "payload": {"title": "Deterministic", "body_markdown": "same"},
    }
    first = workspace_actions._plan_workspace_action(report_run, dict(payload), outline=outline)
    second = workspace_actions._plan_workspace_action(report_run, dict(payload), outline=outline)

    def structure(plan: dict[str, Any]) -> list[tuple]:
        return [
            (
                s.get("section_id"),
                s.get("title"),
                s.get("kind"),
                s.get("body_markdown"),
                tuple(s.get("paper_ids") or []),
            )
            for s in plan["sections"]
        ]

    # The planned target structure is deterministic. The outline digest is not
    # asserted here because applied_actions legitimately record a fresh
    # provenance timestamp, so two plans made at different instants differ there.
    assert structure(first) == structure(second)
    assert first["candidate_id"] == second["candidate_id"]


def test_planner_folds_candidates_without_writing(report_run: str) -> None:
    outline = reporting.load_outline(report_run)
    before_digest = _outline_digest(report_run)
    working = outline
    for index in range(3):
        plan = workspace_actions._plan_workspace_action(
            report_run,
            {
                "action_type": "create_candidate_section",
                "source_run_id": report_run,
                "candidate_id": f"cand-{index}",
                "payload": {"title": f"Folded {index}", "body_markdown": "body"},
            },
            outline=working,
        )
        working = plan["projected_outline"]
    # All three folded in memory; disk untouched.
    titles = [str(s.get("title")) for s in working.get("sections", [])]
    assert {"Folded 0", "Folded 1", "Folded 2"} <= set(titles)
    assert _outline_digest(report_run) == before_digest


# ---------------------------------------------------------------------------
# atomic success
# ---------------------------------------------------------------------------


def test_apply_all_commits_every_candidate_in_one_version(report_run: str) -> None:
    for index in range(3):
        _stage_candidate(report_run, f"cand-{index}", f"Batch {index}")
    result = workspace_actions.apply_staged_workspace_actions(report_run, apply_all=True)
    assert result["status"] == "applied"
    assert result["applied_count"] == 3
    titles = _outline_titles(report_run)
    assert {"Batch 0", "Batch 1", "Batch 2"} <= set(titles)
    # Queue fully consumed; preview reads the committed state.
    assert workspace_actions._read_staged_actions(report_run) == []
    preview = reporting.build_preview(report_run)
    assert preview is not None


def test_apply_all_status_is_never_partial_on_success(report_run: str) -> None:
    _stage_candidate(report_run, "cand-a", "Alpha")
    _stage_candidate(report_run, "cand-b", "Beta")
    result = workspace_actions.apply_staged_workspace_actions(report_run, apply_all=True)
    assert result["status"] != "partial"
    assert result["applied_count"] == 2


# ---------------------------------------------------------------------------
# failure injection: pre-commit failures leave the old state
# ---------------------------------------------------------------------------


def test_failure_in_later_candidate_applies_none(report_run: str, monkeypatch: pytest.MonkeyPatch) -> None:
    for index in range(3):
        _stage_candidate(report_run, f"cand-{index}", f"Batch {index}")
    before_titles = _outline_titles(report_run)
    before_digest = _outline_digest(report_run)
    before_queue = list(workspace_actions._read_staged_actions(report_run))

    real_plan = workspace_actions._plan_workspace_action

    def failing_plan(run_id, payload, *, outline):
        if payload.get("candidate_id") == "cand-2":
            raise RuntimeError("synthetic planner failure")
        return real_plan(run_id, payload, outline=outline)

    monkeypatch.setattr(workspace_actions, "_plan_workspace_action", failing_plan)
    with pytest.raises(RuntimeError):
        workspace_actions.apply_staged_workspace_actions(report_run, apply_all=True)

    # Nothing applied: report, digest and queue are all untouched.
    assert _outline_titles(report_run) == before_titles
    assert _outline_digest(report_run) == before_digest
    assert workspace_actions._read_staged_actions(report_run) == before_queue


def test_commit_write_failure_leaves_old_report(report_run: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _stage_candidate(report_run, "cand-x", "Should Not Land")
    before_titles = _outline_titles(report_run)
    before_queue = list(workspace_actions._read_staged_actions(report_run))

    def failing_save(*_args, **_kwargs):
        raise OSError("synthetic commit failure")

    monkeypatch.setattr(reporting, "save_outline", failing_save)
    with pytest.raises(OSError):
        workspace_actions.apply_staged_workspace_actions(report_run, apply_all=True)

    assert _outline_titles(report_run) == before_titles
    assert "Should Not Land" not in _outline_titles(report_run)
    # Candidate is not consumed by a failed commit.
    assert workspace_actions._read_staged_actions(report_run) == before_queue


def test_no_write_when_batch_validation_fails(report_run: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _stage_candidate(report_run, "cand-bad", "Bad")
    before_digest = _outline_digest(report_run)
    writes: list[str] = []
    real_save = reporting.save_outline

    def counting_save(*args, **kwargs):
        writes.append("save_outline")
        return real_save(*args, **kwargs)

    monkeypatch.setattr(reporting, "save_outline", counting_save)

    def always_fail(run_id, payload, *, outline):
        raise RuntimeError("validation failed")

    monkeypatch.setattr(workspace_actions, "_plan_workspace_action", always_fail)
    with pytest.raises(RuntimeError):
        workspace_actions.apply_staged_workspace_actions(report_run, apply_all=True)
    assert writes == []  # zero persistent report writes
    assert _outline_digest(report_run) == before_digest


def test_unconfirmed_destructive_candidate_fails_whole_batch(
    report_run: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stage_candidate(report_run, "cand-ok", "Fine")
    _stage_candidate(report_run, "cand-danger", "Dangerous")
    items = workspace_actions._read_staged_actions(report_run)
    for item in items:
        if item["candidate_id"] == "cand-danger":
            item["requires_confirmation"] = True
    workspace_actions._write_staged_actions(report_run, items)
    before_titles = _outline_titles(report_run)

    # Bypass preflight blockers so the batch reaches the plan phase.
    monkeypatch.setattr(
        workspace_actions,
        "preflight_apply_staged_workspace_actions",
        lambda *a, **k: {"warnings": [], "blockers": []},
    )
    with pytest.raises(RuntimeError):
        workspace_actions.apply_staged_workspace_actions(report_run, apply_all=True)
    # "Fine" must not be applied just because it preceded the destructive one.
    assert _outline_titles(report_run) == before_titles


def test_post_commit_history_failure_keeps_committed_report(
    report_run: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stage_candidate(report_run, "cand-hist", "History Fail")
    from app.services import outline_history

    def failing_write(*_args, **_kwargs):
        raise OSError("synthetic history failure")

    monkeypatch.setattr(outline_history, "_write_history", failing_write)
    result = workspace_actions.apply_staged_workspace_actions(report_run, apply_all=True)
    # Commit stands; the history gap is reported as cleanup-pending, not rolled back.
    assert result["status"] == "applied"
    assert "History Fail" in _outline_titles(report_run)
    assert report_transaction.CODE_CLEANUP_PENDING in result["warnings"]


# ---------------------------------------------------------------------------
# optimistic concurrency (compare-and-swap)
# ---------------------------------------------------------------------------


def test_stale_base_is_rejected_before_commit(report_run: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _stage_candidate(report_run, "cand-stale", "Stale Base")
    real_plan = workspace_actions._plan_workspace_action

    def plan_then_mutate(run_id, payload, *, outline):
        plan = real_plan(run_id, payload, outline=outline)
        # Simulate a concurrent writer landing between plan and commit.
        reporting.save_outline(
            run_id,
            [_section("sec-1", "First"), _section("sec-2", "Second"), _section("sec-3", "Concurrent")],
            applied_actions=[],
        )
        return plan

    monkeypatch.setattr(workspace_actions, "_plan_workspace_action", plan_then_mutate)
    with pytest.raises(report_transaction.ReportApplyConflict):
        workspace_actions.apply_staged_workspace_actions(report_run, apply_all=True)
    titles = _outline_titles(report_run)
    assert "Concurrent" in titles          # concurrent write preserved
    assert "Stale Base" not in titles      # stale plan not applied


def test_ensure_base_unchanged_contract() -> None:
    report_transaction.ensure_base_unchanged(expected_digest="a", actual_digest="a")
    with pytest.raises(report_transaction.ReportApplyConflict):
        report_transaction.ensure_base_unchanged(expected_digest="a", actual_digest="b")


# ---------------------------------------------------------------------------
# cross-process lock
# ---------------------------------------------------------------------------


def test_lock_is_exclusive_and_times_out(report_run: str) -> None:
    with report_transaction.report_mutation_lock(report_run, timeout_seconds=0.2):
        with pytest.raises(report_transaction.ReportTransactionBusy):
            with report_transaction.report_mutation_lock(report_run, timeout_seconds=0.2):
                pass


def test_lock_is_released_after_exception(report_run: str) -> None:
    with pytest.raises(RuntimeError):
        with report_transaction.report_mutation_lock(report_run, timeout_seconds=0.2):
            raise RuntimeError("boom")
    # Re-acquire proves the lock file was cleaned up on the exception path.
    with report_transaction.report_mutation_lock(report_run, timeout_seconds=0.2):
        pass


def test_lock_file_presence_is_not_ownership(report_run: str) -> None:
    """Ownership is the OS handle lock, not the marker file.

    The lock file is deliberately left on disk after release: a file left behind
    by a crashed holder must not block the run, so presence means nothing and
    re-acquisition must still succeed.
    """
    lock_path = report_transaction._lock_path(report_run)
    with report_transaction.report_mutation_lock(report_run):
        assert lock_path.exists()
    with report_transaction.report_mutation_lock(report_run, timeout_seconds=1.0):
        pass


def test_lock_rejects_malformed_run_id() -> None:
    from app.domain.path_safety import InvalidRunId

    with pytest.raises(InvalidRunId):
        with report_transaction.report_mutation_lock("../evil"):
            pass


def test_different_runs_do_not_serialize(tmp_path: Path) -> None:
    for run_id in ("run-lock-a", "run-lock-b"):
        run_repository.ensure_run_layout(run_id)
    with report_transaction.report_mutation_lock("run-lock-a", timeout_seconds=0.2):
        # A different run must be independently lockable.
        with report_transaction.report_mutation_lock("run-lock-b", timeout_seconds=0.2):
            pass


def test_concurrent_apply_all_yields_exactly_one_winner(report_run: str) -> None:
    _stage_candidate(report_run, "cand-race", "Race")
    results: list[str] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        try:
            result = workspace_actions.apply_staged_workspace_actions(report_run, apply_all=True)
            results.append(result["status"])
        except Exception as exc:  # noqa: BLE001 - classified below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    # Exactly one commit; the loser must not double-apply the candidate.
    assert len(results) + len(errors) == 2
    assert _outline_titles(report_run).count("Race") == 1


# ---------------------------------------------------------------------------
# idempotency / duplicate consumption
# ---------------------------------------------------------------------------


def test_retry_after_success_does_not_duplicate_version(report_run: str) -> None:
    _stage_candidate(report_run, "cand-once", "Once")
    first = workspace_actions.apply_staged_workspace_actions(report_run, apply_all=True)
    assert first["applied_count"] == 1
    # The queue is empty, so a retry is a truthful no-op rather than a re-apply.
    second = workspace_actions.apply_staged_workspace_actions(report_run, apply_all=True)
    assert second["applied_count"] == 0
    assert second["status"] == "noop"
    assert _outline_titles(report_run).count("Once") == 1


def test_duplicate_candidate_ids_in_batch_are_rejected(report_run: str) -> None:
    _stage_candidate(report_run, "cand-dup", "Dup")
    items = workspace_actions._read_staged_actions(report_run)
    items.append(dict(items[0]))  # same candidate_id twice
    workspace_actions._write_staged_actions(report_run, items)
    before_titles = _outline_titles(report_run)
    with pytest.raises(RuntimeError):
        workspace_actions.apply_staged_workspace_actions(report_run, apply_all=True)
    assert _outline_titles(report_run) == before_titles


# ---------------------------------------------------------------------------
# single-candidate apply shares the transaction contract
# ---------------------------------------------------------------------------


def test_single_apply_commits_and_uses_lock(report_run: str) -> None:
    result = workspace_actions.apply_workspace_action(
        report_run,
        {
            "action_type": "create_candidate_section",
            "source_run_id": report_run,
            "candidate_id": "single-1",
            "payload": {"title": "Single", "body_markdown": "single body"},
        },
    )
    assert result["applied"] is True
    assert "Single" in _outline_titles(report_run)
    # Lock released after the mutation: it can be taken again immediately.
    with report_transaction.report_mutation_lock(report_run, timeout_seconds=1.0):
        pass


def test_single_apply_failure_leaves_old_report(report_run: str, monkeypatch: pytest.MonkeyPatch) -> None:
    before_titles = _outline_titles(report_run)

    def failing_save(*_args, **_kwargs):
        raise OSError("synthetic commit failure")

    monkeypatch.setattr(reporting, "save_outline", failing_save)
    with pytest.raises(OSError):
        workspace_actions.apply_workspace_action(
            report_run,
            {
                "action_type": "create_candidate_section",
                "source_run_id": report_run,
                "candidate_id": "single-fail",
                "payload": {"title": "Nope", "body_markdown": "nope"},
            },
        )
    assert _outline_titles(report_run) == before_titles


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------


def test_transaction_errors_carry_no_path_or_traceback(report_run: str) -> None:
    error = report_transaction.ReportTransactionBusy("report_mutation_lock_timeout")
    text = str(error)
    assert "Traceback" not in text
    assert str(report_transaction._lock_path(report_run)) not in text
    assert error.code == "report_transaction_busy"
