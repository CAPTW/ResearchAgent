"""Local runtime failure must not spend a durable dispatch reservation."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from app.domain import review_execution as paper_domain
from app.repositories import journal_executions
from app.services.review_dispatch import PaperDispatchRuntime
from tests import test_codex_journal as journal, test_review_dispatch as paper

source = journal.source
completed = journal.completed
execution = journal.execution
runtimes = paper.runtimes
setup = paper.setup


def damage(root: Path, kind: str):
    if kind == "launcher":
        path = root / "fake_cli.py"
        original = path.read_bytes()
        path.write_bytes(original + b"\n# changed launcher\n")
        return lambda: path.write_bytes(original)
    path = root / "empty-home"
    preserved = root / "preserved-home"
    path.rename(preserved)
    if kind == "home_file":
        path.write_text("synthetic replacement", encoding="utf-8")

    def restore():
        if path.is_file():
            path.unlink()
        preserved.rename(path)

    return restore


@pytest.mark.parametrize("kind", ["home_missing", "home_file", "launcher"])
def test_journal_preflight_preserves_reservation_and_retry_identity(execution, tmp_path, kind):
    root = tmp_path / "cli"
    provider = journal.adapter(execution, root)
    calls = []

    def authority(*_):
        calls.append("resolve")
        return journal.approval(execution, provider)

    controller = journal.journal_dispatcher(provider, authority=authority, clock=lambda: journal.prior.NOW)
    restore = damage(root, kind)
    try:
        with pytest.raises(ValueError, match="journal_codex_runtime_(changed|invalid)"):
            controller.dispatch(*journal.prior.scope(execution), journal.dispatch.body(), "owner")
        assert journal_executions.detail(*journal.prior.scope(execution)) == execution
        assert controller.snapshot()["workers"] == []
        assert not (root / "launches.log").exists()
        assert not (root / "attempts").exists()
    finally:
        assert controller.stop()
        restore()

    # No new request, unknown-outcome acknowledgement or replacement approval.
    controller = journal.journal_dispatcher(provider, authority=authority, clock=lambda: journal.prior.NOW)
    try:
        controller.dispatch(*journal.prior.scope(execution), journal.dispatch.body(), "owner")
        saved = journal.wait_terminal(execution)
        assert saved["current"]["state"] == "SUCCEEDED"
        assert saved["current"]["attempt"] == 1
        restore = damage(root, kind)
        before = len(calls)
        try:
            assert controller.dispatch(*journal.prior.scope(execution), journal.dispatch.body(), "owner") == saved
            assert len(calls) == before
            assert (root / "launches.log").read_text().splitlines() == ["launch"]
        finally:
            restore()
    finally:
        assert controller.stop()


@pytest.mark.parametrize("kind", ["home_missing", "home_file", "launcher"])
def test_paper_preflight_retains_approval_and_replays_without_runtime(setup, kind, runtimes):
    scope, make, _ = setup
    ready, provider, runtime, calls, root, _, issuer = make()
    identity = ready["contract"]["execution_id"]
    approved = paper.approve(scope, ready, runtime)
    restore = damage(root, kind)
    try:
        with pytest.raises(ValueError, match="journal_codex_runtime_(changed|invalid)"):
            runtime.dispatch(scope, identity, paper.dispatch_body())
        assert paper.read(scope, ready) == approved
        assert len(calls) == 1
        assert not (root / "launches.log").exists()
        assert not (root / "attempts").exists()
    finally:
        runtime.stop()
        runtime.wait_idle(30)
        restore()

    resumed = PaperDispatchRuntime(resolver=issuer, provider=provider)
    runtimes.append(resumed)
    resumed.dispatch(scope, identity, paper.dispatch_body())
    resumed.wait_idle(30)
    saved = paper.read(scope, ready)
    assert [event["state"] for event in saved["events"]] == [
        "READY", "APPROVED", "LEASED", "RUNNING", "RESULT_RECEIVED", "RESULT_SAVED"]
    restore = damage(root, kind)
    try:
        assert resumed.dispatch(scope, identity, paper.dispatch_body()) == saved
        assert paper.approve(scope, ready, resumed) == saved
        assert len(calls) == 1
        assert (root / "launches.log").read_text().splitlines() == ["launch"]
    finally:
        restore()


def test_preflight_does_not_replace_paper_authority(setup):
    scope, make, _ = setup
    ready, _, runtime, _, root, _, _ = make()
    with pytest.raises(ValueError, match="not_approved"):
        runtime.dispatch(scope, ready["contract"]["execution_id"], paper_domain.Dispatch(request_id="unapproved", expected_sequence=0))
    assert paper.read(scope, ready) == ready
    assert not (root / "launches.log").exists()


def hold_preflight(monkeypatch, provider):
    entered, release = Event(), Event()
    original = type(provider).validate_local_runtime

    def check(self):
        original(self)
        entered.set()
        assert release.wait(10), "preflight release timed out"

    monkeypatch.setattr(type(provider), "validate_local_runtime", check)
    return entered, release


def test_paper_shutdown_during_preflight_cannot_claim(setup, monkeypatch):
    scope, make, _ = setup
    ready, provider, runtime, _, root, _, _ = make()
    approved = paper.approve(scope, ready, runtime)
    entered, release = hold_preflight(monkeypatch, provider)
    with ThreadPoolExecutor(2) as pool:
        dispatched = pool.submit(runtime.dispatch, scope, ready["contract"]["execution_id"], paper.dispatch_body())
        try:
            assert entered.wait(10)
            # stop must not wait for the local preflight/file reads.
            pool.submit(runtime.stop).result(timeout=3)
        finally:
            release.set()
        with pytest.raises(ValueError, match="review_execution_disabled"):
            dispatched.result(timeout=10)
    assert paper.read(scope, ready) == approved
    assert not (root / "attempts").exists()


def test_journal_shutdown_during_preflight_cannot_reserve(execution, tmp_path, monkeypatch):
    root = tmp_path / "cli"
    provider = journal.adapter(execution, root)
    controller = journal.journal_dispatcher(provider, authority=lambda *_: journal.approval(execution, provider),
        clock=lambda: journal.prior.NOW)
    entered, release = hold_preflight(monkeypatch, provider)
    try:
        with ThreadPoolExecutor(2) as pool:
            dispatched = pool.submit(controller.dispatch, *journal.prior.scope(execution), journal.dispatch.body(), "owner")
            try:
                assert entered.wait(10)
                assert pool.submit(controller.stop).result(timeout=3)
            finally:
                release.set()
            with pytest.raises(ValueError, match="journal_dispatch_closed"):
                dispatched.result(timeout=10)
        assert journal_executions.detail(*journal.prior.scope(execution)) == execution
        assert not (root / "attempts").exists()
    finally:
        assert controller.stop()


def change_after_preflight(monkeypatch, provider, root):
    original = type(provider).validate_local_runtime
    restores = []

    def check(self):
        original(self)
        restores.append(damage(root, "home_missing"))

    monkeypatch.setattr(type(provider), "validate_local_runtime", check)
    return restores


def test_paper_late_drift_keeps_unknown_outcome_and_never_retries(setup, monkeypatch):
    scope, make, _ = setup
    ready, provider, runtime, _, root, _, _ = make()
    identity = ready["contract"]["execution_id"]
    paper.approve(scope, ready, runtime)
    restores = change_after_preflight(monkeypatch, provider, root)
    try:
        runtime.dispatch(scope, identity, paper.dispatch_body())
        runtime.wait_idle(30)
        saved = paper.read(scope, ready)
        assert saved["current"]["state"] == "INTERRUPTED"
        assert saved["current"]["outcome_unknown"] is True
        assert runtime.dispatch(scope, identity, paper.dispatch_body()) == saved
        assert len(restores) == 1
        assert not (root / "attempts").exists()
    finally:
        runtime.stop()
        runtime.wait_idle(30)
        for restore in restores:
            restore()


def test_journal_late_drift_keeps_unknown_outcome_and_never_retries(execution, tmp_path, monkeypatch):
    root = tmp_path / "cli"
    provider = journal.adapter(execution, root)
    controller = journal.journal_dispatcher(provider, authority=lambda *_: journal.approval(execution, provider),
        clock=lambda: journal.prior.NOW)
    restores = change_after_preflight(monkeypatch, provider, root)
    try:
        controller.dispatch(*journal.prior.scope(execution), journal.dispatch.body(), "owner")
        saved = journal.wait_terminal(execution)
        assert saved["current"]["state"] == "INTERRUPTED"
        assert saved["current"]["outcome_unknown"] is True
        assert controller.dispatch(*journal.prior.scope(execution), journal.dispatch.body(), "owner") == saved
        assert len(restores) == 1
        assert not (root / "attempts").exists()
    finally:
        assert controller.stop()
        for restore in restores:
            restore()
