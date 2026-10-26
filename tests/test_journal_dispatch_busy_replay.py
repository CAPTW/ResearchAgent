"""A busy duplicate may acknowledge only a fully validated durable receipt."""
from contextlib import closing
import sqlite3

import pytest

from app.db.bootstrap import connect_metadata_index
from app.db.sqlite_integrity import ManagedSqliteConnection
from app.repositories import journal_dispatch as admission, journal_executions as repo
from tests import test_anthropic_journal_archive as native

source = native.source
completed = native.completed
execution = native.execution


def admit(execution, *, actor="owner", body=None):
    provider = native.adapter(execution)
    return admission.admit(*native.shared.prior.scope(execution), body or native.shared.dispatch.body(), actor,
        native.shared.approval(execution, provider), provider.binding, now=native.shared.prior.NOW,
        adapter_manifest=provider.manifest())


@pytest.mark.parametrize("committed", [True, False])
def test_held_writer_can_only_recover_a_previously_committed_matching_dispatch(execution, committed):
    if committed:
        saved, claim = admit(execution)
        assert claim is not None
    else:
        saved = execution
    # A real independent connection occupies the writer slot. Existing readers
    # can still see the committed receipt; BEGIN keeps its original 5s budget.
    with closing(connect_metadata_index()) as blocker:
        assert blocker.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        blocker.execute("BEGIN IMMEDIATE")
        if committed:
            replayed, claim = admit(execution)
            assert replayed == saved and claim is None
        else:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                admit(execution)
        assert repo.detail(*native.shared.prior.scope(execution)) == saved
        blocker.rollback()


@pytest.mark.parametrize("mismatch", ["actor", "body", "different_request", "corrupt", "nonbusy", "locked"])
def test_busy_reconciliation_never_hides_missing_conflicting_or_invalid_receipts(execution, monkeypatch, mismatch):
    saved, _ = admit(execution)
    if mismatch == "corrupt":
        with closing(connect_metadata_index()) as connection, connection:
            connection.execute("DROP TRIGGER journal_execution_events_no_update")
            connection.execute("UPDATE journal_execution_events SET payload_digest='corrupt'")
    original = ManagedSqliteConnection.execute
    failure = sqlite3.OperationalError("synthetic locked" if mismatch != "nonbusy" else "synthetic failure")
    failure.sqlite_errorcode = {"nonbusy": sqlite3.SQLITE_IOERR, "locked": sqlite3.SQLITE_LOCKED}.get(mismatch, sqlite3.SQLITE_BUSY)
    reads = []
    replay = admission.replay

    def counted(*args):
        reads.append(args)
        return replay(*args)

    monkeypatch.setattr(admission, "replay", counted)

    def blocked(self, sql, *args, **kwargs):
        if sql == "BEGIN IMMEDIATE":
            raise failure
        return original(self, sql, *args, **kwargs)

    monkeypatch.setattr(ManagedSqliteConnection, "execute", blocked)
    options = {"actor": "other"} if mismatch == "actor" else {}
    if mismatch == "body":
        options["body"] = native.shared.dispatch.body(acknowledge_unknown=True)
    elif mismatch == "different_request":
        options["body"] = native.shared.dispatch.body(request_id="different")
    expected = ValueError if mismatch in {"actor", "body", "corrupt"} else sqlite3.OperationalError
    with pytest.raises(expected) as caught:
        admit(execution, **options)
    if expected is sqlite3.OperationalError:
        assert caught.value is failure
    assert len(reads) == (0 if mismatch in {"nonbusy", "locked"} else 1)
    if mismatch != "corrupt":
        assert repo.detail(*native.shared.prior.scope(execution)) == saved


def test_busy_reconciliation_read_failure_propagates_without_repeated_read_or_write(execution, monkeypatch):
    saved, _ = admit(execution)
    calls = []
    original = ManagedSqliteConnection.execute
    failure = sqlite3.OperationalError("writer busy")
    failure.sqlite_errorcode = sqlite3.SQLITE_BUSY
    read_failure = sqlite3.OperationalError("reader busy")
    read_failure.sqlite_errorcode = sqlite3.SQLITE_BUSY

    def blocked(self, sql, *args, **kwargs):
        if sql == "BEGIN IMMEDIATE":
            calls.append("write")
            raise failure
        return original(self, sql, *args, **kwargs)

    def unavailable(*args):
        calls.append("read")
        raise read_failure

    monkeypatch.setattr(ManagedSqliteConnection, "execute", blocked)
    monkeypatch.setattr(admission, "replay", unavailable)
    with pytest.raises(sqlite3.OperationalError) as caught:
        admit(execution)
    assert caught.value is read_failure
    assert calls == ["write", "read"]
    assert repo.detail(*native.shared.prior.scope(execution)) == saved


def test_busy_commit_rolls_back_new_claim_before_reconciling(execution):
    # In rollback-journal mode an independent reader allows BEGIN/INSERT but
    # prevents COMMIT. The inserted receipt must not survive that failed commit.
    with closing(connect_metadata_index()) as reader:
        assert reader.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        reader.execute("BEGIN")
        assert reader.execute("SELECT count(*) FROM journal_execution_events").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError) as caught:
            admit(execution)
        assert caught.value.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY
        assert repo.detail(*native.shared.prior.scope(execution)) == execution
        reader.rollback()
    # Releasing the reader permits a fresh explicit admission; no stale claim,
    # reservation or dispatch event was left behind by the failed transaction.
    saved, claim = admit(execution)
    assert claim is not None
    assert saved["current"]["attempt"] == 1 and len(saved["events"]) == 1
