"""Shared file-state concurrency, crash safety and corruption behavior.

Everything here is synthetic and lives under pytest ``tmp_path`` or an OS
temporary directory. No canonical ``storage/**`` content, provider, OAuth/OIDC
endpoint, MCP connector, SMTP server or network is used. Subprocesses are
bounded and only exercise local locking.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

from app.auth import sessions
from app.auth.config import AuthSettings
from app.auth.security_types import ProductUser
from app.repositories import file_locks, shared_state
from app.services import topic_notifications, topic_profiles

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolate_notification_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect the notification library into this test's temp root.

    conftest already redirects the run/profile/analytics/schedule roots; the
    notification library is redirected per test, so nothing here can touch a
    repository-local or canonical location.
    """
    library = tmp_path / "topic_notifications"
    monkeypatch.setattr(topic_notifications, "NOTIFICATION_LIBRARY_DIR", library)
    monkeypatch.setattr(topic_notifications, "NOTIFICATION_SETTINGS_PATH", library / "settings.json")
    monkeypatch.setattr(topic_notifications, "NOTIFICATION_EVENTS_PATH", library / "notifications.json")
    monkeypatch.setattr(topic_notifications, "NOTIFICATION_DELIVERIES_PATH", library / "deliveries.json")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _state(tmp_path: Path, name: str = "state.json") -> Path:
    return tmp_path / "nested" / name


def _mutate(path: Path, fn, **kwargs: Any) -> Any:
    return shared_state.mutate_json_state(
        path,
        state_name="synthetic",
        expected_type=list,
        default=lambda: [],
        mutate=fn,
        **kwargs,
    )


def _load(path: Path) -> Any:
    return shared_state.load_json_state(
        path, state_name="synthetic", expected_type=list, default=lambda: []
    )


def _tree(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _run_worker(body: str, *, env: dict[str, str], timeout: float = 60.0) -> subprocess.CompletedProcess:
    """Run a bounded helper process against the same synthetic temp root."""
    script = textwrap.dedent(
        f"""
        import os, sys, time, json
        sys.path.insert(0, {str(REPO_ROOT)!r})
        {textwrap.indent(textwrap.dedent(body), "        ").lstrip()}
        """
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# 19.1 generic shared-state helper
# ---------------------------------------------------------------------------


def test_missing_read_creates_nothing(tmp_path: Path) -> None:
    path = _state(tmp_path)
    assert _load(path) == []
    assert not path.exists()
    assert not path.parent.exists()
    assert not shared_state.lock_path_for(path).exists()
    assert list(tmp_path.rglob("*")) == []


def test_first_mutation_creates_parent_state_and_lock(tmp_path: Path) -> None:
    path = _state(tmp_path)
    _mutate(path, lambda s: (s + [{"id": "a"}], None))
    assert path.exists() and path.parent.exists()
    assert shared_state.lock_path_for(path).exists()
    assert _load(path) == [{"id": "a"}]


def test_no_op_mutation_performs_no_write(tmp_path: Path) -> None:
    path = _state(tmp_path)
    _mutate(path, lambda s: (s + [{"id": "a"}], None))
    before = (path.stat().st_mtime_ns, path.read_bytes())
    for _ in range(3):
        _mutate(path, lambda s: (s, None))
    assert (path.stat().st_mtime_ns, path.read_bytes()) == before


def test_mutation_callback_failure_leaves_state_untouched(tmp_path: Path) -> None:
    path = _state(tmp_path)
    _mutate(path, lambda s: (s + [{"id": "a"}], None))
    before = path.read_bytes()

    def _boom(_state_arg):
        raise RuntimeError("callback failed")

    with pytest.raises(RuntimeError):
        _mutate(path, _boom)
    assert path.read_bytes() == before


def test_malformed_json_is_corruption_and_never_overwritten(tmp_path: Path) -> None:
    path = _state(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{ not json at all", encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(shared_state.SharedStateCorrupt):
        _load(path)
    with pytest.raises(shared_state.SharedStateCorrupt):
        _mutate(path, lambda s: (s + [{"id": "a"}], None))
    assert path.read_bytes() == before


def test_wrong_top_level_type_is_schema_invalid_and_never_overwritten(tmp_path: Path) -> None:
    path = _state(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"unexpected": "object"}), encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(shared_state.SharedStateSchemaInvalid):
        _load(path)
    with pytest.raises(shared_state.SharedStateSchemaInvalid):
        _mutate(path, lambda s: (s + [{"id": "a"}], None))
    assert path.read_bytes() == before


def test_mutation_producing_wrong_type_is_rejected(tmp_path: Path) -> None:
    path = _state(tmp_path)
    _mutate(path, lambda s: (s + [{"id": "a"}], None))
    before = path.read_bytes()
    with pytest.raises(shared_state.SharedStateSchemaInvalid):
        _mutate(path, lambda _s: ({"now": "a dict"}, None))
    assert path.read_bytes() == before


@pytest.mark.parametrize("failing", ["mkstemp", "fsync", "replace"])
def test_write_failures_keep_old_state_authoritative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    path = _state(tmp_path)
    _mutate(path, lambda s: (s + [{"id": "original"}], None))
    before = path.read_bytes()

    def _fail(*_a, **_k):
        raise OSError(f"synthetic {failing} failure")

    monkeypatch.setattr(
        {"mkstemp": shared_state.tempfile, "fsync": shared_state.os, "replace": shared_state.os}[failing],
        failing,
        _fail,
    )
    with pytest.raises(shared_state.SharedStateWriteFailed):
        _mutate(path, lambda s: (s + [{"id": "new"}], None))
    assert path.read_bytes() == before
    assert list(path.parent.glob("*.tmp")) == []


def test_errors_carry_no_path_content_or_parser_message(tmp_path: Path) -> None:
    path = _state(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"secret-destination": "https://hook.example/xyz" ', encoding="utf-8")
    with pytest.raises(shared_state.SharedStateCorrupt) as excinfo:
        _load(path)
    text = str(excinfo.value)
    assert str(path) not in text and str(tmp_path) not in text
    assert "hook.example" not in text and "Expecting" not in text
    assert excinfo.value.code == shared_state.CODE_CORRUPT


def test_lock_timeout_is_bounded_and_reported_as_busy(tmp_path: Path) -> None:
    path = _state(tmp_path)
    path.parent.mkdir(parents=True)
    handle = os.open(str(shared_state.lock_path_for(path)), os.O_CREAT | os.O_RDWR, 0o600)
    assert file_locks.try_acquire_os_lock(handle)
    try:
        started = time.monotonic()
        with pytest.raises(shared_state.SharedStateBusy):
            _mutate(path, lambda s: (s + [{"id": "a"}], None), timeout_seconds=0.3)
        assert time.monotonic() - started < 5.0
    finally:
        file_locks.release_os_lock(handle)
        os.close(handle)
    _mutate(path, lambda s: (s + [{"id": "a"}], None), timeout_seconds=1.0)


def test_lock_released_after_exception(tmp_path: Path) -> None:
    path = _state(tmp_path)

    def _boom(_s):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        _mutate(path, _boom)
    _mutate(path, lambda s: (s + [{"id": "a"}], None), timeout_seconds=1.0)


def test_stale_lock_file_without_os_lock_does_not_block(tmp_path: Path) -> None:
    path = _state(tmp_path)
    path.parent.mkdir(parents=True)
    # Residue from a holder that died: the file exists, nobody owns it.
    os.close(os.open(str(shared_state.lock_path_for(path)), os.O_CREAT | os.O_RDWR, 0o600))
    _mutate(path, lambda s: (s + [{"id": "a"}], None), timeout_seconds=1.0)
    assert _load(path) == [{"id": "a"}]


def test_different_states_lock_independently(tmp_path: Path) -> None:
    first = _state(tmp_path, "first.json")
    second = _state(tmp_path, "second.json")
    first.parent.mkdir(parents=True)
    handle = os.open(str(shared_state.lock_path_for(first)), os.O_CREAT | os.O_RDWR, 0o600)
    assert file_locks.try_acquire_os_lock(handle)
    try:
        _mutate(second, lambda s: (s + [{"id": "b"}], None), timeout_seconds=1.0)
    finally:
        file_locks.release_os_lock(handle)
        os.close(handle)
    assert _load(second) == [{"id": "b"}]


def test_reparse_point_state_path_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _state(tmp_path)
    monkeypatch.setattr(shared_state, "is_reparse_point", lambda _p: True)
    with pytest.raises(shared_state.SharedStateSchemaInvalid):
        _mutate(path, lambda s: (s + [{"id": "a"}], None))
    assert not path.exists()


def test_reparse_point_lock_path_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _state(tmp_path)
    monkeypatch.setattr(file_locks, "is_reparse_point", lambda _p: True)
    with pytest.raises(shared_state.SharedStateWriteFailed):
        _mutate(path, lambda s: (s + [{"id": "a"}], None))


# ---------------------------------------------------------------------------
# 19.2 lost-update concurrency
# ---------------------------------------------------------------------------


def test_two_independent_processes_both_survive(tmp_path: Path) -> None:
    """The core lost-update property, proven across real OS processes."""
    path = _state(tmp_path)
    path.parent.mkdir(parents=True)
    barrier = time.time() + 3.0
    body = """
        from pathlib import Path
        from app.repositories import shared_state
        p = Path(os.environ["TARGET"]); tag = os.environ["TAG"]
        while time.time() < float(os.environ["BARRIER"]): time.sleep(0.002)
        shared_state.mutate_json_state(
            p, state_name="synthetic", expected_type=list, default=lambda: [],
            mutate=lambda s: (s + [{"id": tag}], None), timeout_seconds=30.0)
        """
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(f"import os,sys,time\nsys.path.insert(0,{str(REPO_ROOT)!r})\n") + textwrap.dedent(body)],
            env={**os.environ, "TARGET": str(path), "TAG": tag, "BARRIER": str(barrier)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for tag in ("A", "B")
    ]
    for proc in procs:
        _, err = proc.communicate(timeout=90)
        assert proc.returncode == 0, err
    assert sorted(item["id"] for item in _load(path)) == ["A", "B"]


def test_many_concurrent_mutations_lose_nothing(tmp_path: Path) -> None:
    import threading

    path = _state(tmp_path)
    errors: list[BaseException] = []

    def _worker(index: int) -> None:
        try:
            _mutate(path, lambda s: (s + [{"id": f"item-{index}"}], None), timeout_seconds=30.0)
        except BaseException as exc:  # noqa: BLE001 - recorded and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(24)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    stored = _load(path)
    assert sorted(item["id"] for item in stored) == sorted(f"item-{i}" for i in range(24))
    json.loads(path.read_text(encoding="utf-8"))  # still valid JSON


def test_delete_and_create_race_preserves_both_outcomes(tmp_path: Path) -> None:
    path = _state(tmp_path)
    _mutate(path, lambda s: (s + [{"id": "A"}], None))
    _mutate(path, lambda s: ([i for i in s if i["id"] != "A"], None))
    _mutate(path, lambda s: (s + [{"id": "B"}], None))
    ids = [item["id"] for item in _load(path)]
    assert "A" not in ids and "B" in ids


def test_stale_snapshot_cannot_be_written_back(tmp_path: Path) -> None:
    """A snapshot read before a delete cannot resurrect the deleted item."""
    path = _state(tmp_path)
    _mutate(path, lambda s: (s + [{"id": "A"}, {"id": "B"}], None))
    stale = _load(path)  # taken before the delete
    _mutate(path, lambda s: ([i for i in s if i["id"] != "A"], None))
    # The only way back in is a callback over the *current* state.
    _mutate(path, lambda s: (s + [{"id": "C"}], None))
    ids = [item["id"] for item in _load(path)]
    assert ids == ["B", "C"]
    assert any(item["id"] == "A" for item in stale)  # the stale copy still has it


# ---------------------------------------------------------------------------
# 19.3 sessions
# ---------------------------------------------------------------------------


@pytest.fixture()
def auth_settings() -> AuthSettings:
    return AuthSettings()


def _user(user_id: str = "user-1") -> ProductUser:
    return ProductUser(
        user_id=user_id, display_name=user_id, email=None, auth_source="oidc", is_admin=False
    )


def test_concurrent_session_creates_are_both_kept(auth_settings: AuthSettings) -> None:
    import threading

    created: list[str] = []
    lock = threading.Lock()

    def _create(index: int) -> None:
        sid = sessions.create_product_session(_user(f"user-{index}"), settings=auth_settings)
        with lock:
            created.append(sid)

    threads = [threading.Thread(target=_create, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    store = sessions._read_store()
    assert len(created) == 8
    assert all(sid in store["sessions"] for sid in created)


def test_revoked_session_is_not_resurrected_by_a_later_touch(auth_settings: AuthSettings) -> None:
    sid = sessions.create_product_session(_user(), settings=auth_settings)
    assert sessions.read_product_session(sid, settings=auth_settings) is not None
    assert sessions.revoke_product_session(sid) is True
    # Repeated post-logout reuse is always rejected.
    for _ in range(5):
        assert sessions.read_product_session(sid, settings=auth_settings) is None
    assert sessions._read_store()["sessions"][sid]["revoked_at"]


def test_logout_wins_against_concurrent_touch(auth_settings: AuthSettings) -> None:
    import threading

    sid = sessions.create_product_session(_user(), settings=auth_settings)
    results: list[Any] = []
    start = threading.Barrier(2)

    def _touch() -> None:
        start.wait(timeout=10)
        for _ in range(30):
            results.append(sessions.read_product_session(sid, settings=auth_settings))

    def _logout() -> None:
        start.wait(timeout=10)
        sessions.revoke_product_session(sid)

    threads = [threading.Thread(target=_touch), threading.Thread(target=_logout)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    # Once revoked, it stays revoked: no touch may write the session back to life.
    assert sessions.read_product_session(sid, settings=auth_settings) is None
    assert sessions._read_store()["sessions"][sid]["revoked_at"]


def test_prune_preserves_a_concurrently_created_valid_session(auth_settings: AuthSettings) -> None:
    fresh = sessions.create_product_session(_user("keep-me"), settings=auth_settings)

    def _expire(store: dict[str, Any]) -> tuple[dict[str, Any], None]:
        store["sessions"]["expired-session"] = {
            "session_id": "expired-session",
            "user_id": "old",
            "expires_at": "2020-01-01T00:00:00+00:00",
            "revoked_at": None,
        }
        return store, None

    sessions._mutate_store(_expire)
    removed = sessions.prune_expired_product_sessions(settings=auth_settings)
    assert removed == 1
    assert fresh in sessions._read_store()["sessions"]
    assert sessions.read_product_session(fresh, settings=auth_settings) is not None


def test_corrupt_session_store_authenticates_nobody_and_is_preserved(
    auth_settings: AuthSettings,
) -> None:
    sid = sessions.create_product_session(_user(), settings=auth_settings)
    path = sessions._session_store_path()
    path.write_text("{ corrupted store", encoding="utf-8")
    before = path.read_bytes()

    assert sessions.read_product_session(sid, settings=auth_settings) is None
    with pytest.raises(shared_state.SharedStateError):
        sessions.create_product_session(_user("other"), settings=auth_settings)
    assert path.read_bytes() == before  # never repaired, never emptied


def test_session_errors_do_not_leak_identifiers(auth_settings: AuthSettings) -> None:
    sid = sessions.create_product_session(_user("secret-user"), settings=auth_settings)
    sessions._session_store_path().write_text("{ corrupted", encoding="utf-8")
    with pytest.raises(shared_state.SharedStateError) as excinfo:
        sessions.create_product_session(_user("secret-user"), settings=auth_settings)
    text = str(excinfo.value)
    assert sid not in text and "secret-user" not in text
    assert str(sessions._session_store_path()) not in text


def test_session_lookup_does_not_create_the_store(auth_settings: AuthSettings) -> None:
    path = sessions._session_store_path()
    assert not path.exists()
    parent_existed = path.parent.exists()
    # A pure lookup is read-only: no store, no directory, no lock.
    assert sessions.read_product_session("A" * 43, settings=auth_settings) is None
    assert not path.exists()
    assert path.parent.exists() is parent_existed
    assert not shared_state.lock_path_for(path).exists()


def test_revoke_is_a_mutation_boundary_and_writes_no_default_store(
    auth_settings: AuthSettings,
) -> None:
    path = sessions._session_store_path()
    # Revoking is a mutation, so it may take a lock; it must still not
    # materialize a default store for a session that never existed.
    assert sessions.revoke_product_session("A" * 43) is False
    assert not path.exists()


# ---------------------------------------------------------------------------
# 19.4 topic profiles
# ---------------------------------------------------------------------------


def test_concurrent_profile_creates_all_survive() -> None:
    import threading

    errors: list[BaseException] = []

    def _create(index: int) -> None:
        try:
            topic_profiles.create_topic_profile({"title": f"Concurrent {index}", "topic": "t"})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_create, args=(i,)) for i in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    titles = {p["title"] for p in topic_profiles.list_topic_profiles()["items"]}
    assert {f"Concurrent {i}" for i in range(10)} <= titles


def test_concurrent_updates_to_different_profiles_both_persist() -> None:
    import threading

    first = topic_profiles.create_topic_profile({"title": "First", "topic": "t"})["profile_id"]
    second = topic_profiles.create_topic_profile({"title": "Second", "topic": "t"})["profile_id"]

    def _update(profile_id: str, title: str) -> None:
        topic_profiles.update_topic_profile(profile_id, {"title": title})

    threads = [
        threading.Thread(target=_update, args=(first, "First Updated")),
        threading.Thread(target=_update, args=(second, "Second Updated")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert topic_profiles.get_topic_profile(first)["title"] == "First Updated"
    assert topic_profiles.get_topic_profile(second)["title"] == "Second Updated"


def test_delete_preserves_other_profiles_and_blocks_stale_update() -> None:
    keep = topic_profiles.create_topic_profile({"title": "Keep", "topic": "t"})["profile_id"]
    drop = topic_profiles.create_topic_profile({"title": "Drop", "topic": "t"})["profile_id"]

    topic_profiles.delete_topic_profile(drop)
    assert topic_profiles.get_topic_profile(keep)["title"] == "Keep"
    with pytest.raises(FileNotFoundError):
        topic_profiles.update_topic_profile(drop, {"title": "Resurrected"})
    assert all(p["profile_id"] != drop for p in topic_profiles.list_topic_profiles()["items"])


def test_malformed_profile_library_blocks_mutation_without_overwrite() -> None:
    topic_profiles.create_topic_profile({"title": "Seeded", "topic": "t"})
    path = topic_profiles.PROFILE_LIBRARY_PATH
    path.write_text("[ broken", encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(shared_state.SharedStateCorrupt):
        topic_profiles.list_topic_profiles()
    with pytest.raises(shared_state.SharedStateCorrupt):
        topic_profiles.create_topic_profile({"title": "New", "topic": "t"})
    assert path.read_bytes() == before


def test_wrong_shaped_profile_library_is_not_replaced_by_defaults() -> None:
    path = topic_profiles.PROFILE_LIBRARY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"profiles": []}), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(shared_state.SharedStateSchemaInvalid):
        topic_profiles.create_topic_profile({"title": "New", "topic": "t"})
    assert path.read_bytes() == before


def test_profile_reads_create_no_files_or_locks() -> None:
    assert not topic_profiles.PROFILE_LIBRARY_DIR.exists()
    items = topic_profiles.list_topic_profiles()["items"]
    assert items  # defaults are materialized in memory
    topic_profiles.get_topic_profile(items[0]["profile_id"])
    assert not topic_profiles.PROFILE_LIBRARY_DIR.exists()


def test_first_profile_mutation_persists_the_seeded_defaults() -> None:
    created = topic_profiles.create_topic_profile({"title": "Added", "topic": "t"})
    stored = json.loads(topic_profiles.PROFILE_LIBRARY_PATH.read_text(encoding="utf-8"))
    ids = {p["profile_id"] for p in stored}
    assert created["profile_id"] in ids
    assert "large-language-models" in ids  # seeds preserved, not lost


# ---------------------------------------------------------------------------
# 19.5 notifications
# ---------------------------------------------------------------------------


@pytest.fixture()
def profile_id() -> str:
    return topic_profiles.create_topic_profile({"title": "Notify", "topic": "t"})["profile_id"]


def test_concurrent_delivery_appends_are_both_kept(profile_id: str) -> None:
    import threading

    def _append(index: int) -> None:
        topic_notifications._append_delivery_record(
            {
                "delivery_id": f"delivery-{index}",
                "profile_id": profile_id,
                "created_at": f"2026-01-01T00:00:{index:02d}+00:00",
            }
        )

    threads = [threading.Thread(target=_append, args=(i,)) for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    stored = {
        item["delivery_id"]
        for item in topic_notifications._load_json_list(topic_notifications.NOTIFICATION_DELIVERIES_PATH)
    }
    assert {f"delivery-{i}" for i in range(12)} <= stored


def test_retention_is_applied_to_the_latest_history(profile_id: str) -> None:
    limit = topic_notifications.MAX_DELIVERY_RECORDS
    for index in range(limit + 5):
        topic_notifications._append_delivery_record(
            {"delivery_id": f"d-{index:05d}", "profile_id": profile_id, "created_at": f"2026-01-01T00:00:00.{index:06d}+00:00"}
        )
    stored = topic_notifications._load_json_list(topic_notifications.NOTIFICATION_DELIVERIES_PATH)
    assert len(stored) == limit
    # The newest records are the ones retained.
    assert stored[0]["delivery_id"] == f"d-{limit + 4:05d}"


def test_channel_updates_preserve_the_other_channel(profile_id: str) -> None:
    topic_notifications.update_topic_notification_settings(
        profile_id,
        {"channels": [{"channel_id": "email", "enabled": True, "destination": "ops@example.test"}]},
    )
    topic_notifications.update_topic_notification_settings(
        profile_id, {"rules": [{"rule_id": "report_completed", "enabled": True}]}
    )
    # The stored entry is what must survive an unrelated update. (The normalized
    # view can still report the channel as unavailable when SMTP env is absent,
    # which is existing behavior and independent of persistence.)
    stored = topic_notifications._load_json_list(topic_notifications.NOTIFICATION_SETTINGS_PATH)
    entry = next(item for item in stored if item.get("profile_id") == profile_id)
    assert entry["channels"]["email"] == {"enabled": True, "destination": "ops@example.test"}
    assert entry["rules"]["report_completed"] is True
    assert topic_notifications.get_topic_notification_settings(profile_id)


def test_settings_update_preserves_unrelated_profiles() -> None:
    first = topic_profiles.create_topic_profile({"title": "One", "topic": "t"})["profile_id"]
    second = topic_profiles.create_topic_profile({"title": "Two", "topic": "t"})["profile_id"]
    topic_notifications.update_topic_notification_settings(
        first, {"channels": [{"channel_id": "email", "enabled": True, "destination": "one@example.test"}]}
    )
    topic_notifications.update_topic_notification_settings(
        second, {"channels": [{"channel_id": "email", "enabled": True, "destination": "two@example.test"}]}
    )
    stored = topic_notifications._load_json_list(topic_notifications.NOTIFICATION_SETTINGS_PATH)
    ids = {str(item.get("profile_id")) for item in stored}
    assert {first, second} <= ids
    for entry in stored:
        if entry.get("profile_id") in {first, second}:
            assert entry["channels"]["email"]["enabled"] is True


def test_malformed_notification_state_blocks_writes_without_overwrite(profile_id: str) -> None:
    topic_notifications._append_delivery_record(
        {"delivery_id": "d-1", "profile_id": profile_id, "created_at": "2026-01-01T00:00:00+00:00"}
    )
    path = topic_notifications.NOTIFICATION_DELIVERIES_PATH
    path.write_text("[ broken", encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(shared_state.SharedStateCorrupt):
        topic_notifications._append_delivery_record(
            {"delivery_id": "d-2", "profile_id": profile_id, "created_at": "2026-01-02T00:00:00+00:00"}
        )
    assert path.read_bytes() == before


def test_notification_reads_create_no_files_or_locks(profile_id: str) -> None:
    library = topic_notifications.NOTIFICATION_LIBRARY_DIR
    if library.exists():
        for child in library.iterdir():
            child.unlink()
        library.rmdir()

    topic_notifications.get_topic_notification_settings(profile_id)
    topic_notifications.list_topic_notifications(profile_id)
    topic_notifications.list_topic_notification_deliveries(profile_id)
    topic_notifications.notification_delivery_health_summary()
    assert not library.exists()


def test_no_network_call_happens_while_the_state_lock_is_held(
    profile_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delivery I/O must never run inside a shared-state mutation."""
    depth = {"value": 0}
    real_mutate = shared_state.mutate_json_state

    def _tracking_mutate(*args: Any, **kwargs: Any) -> Any:
        depth["value"] += 1
        try:
            return real_mutate(*args, **kwargs)
        finally:
            depth["value"] -= 1

    def _forbidden(*_a: Any, **_k: Any):
        assert depth["value"] == 0, "network delivery attempted while holding the state lock"
        raise AssertionError("network must not be reached in this test")

    monkeypatch.setattr(topic_notifications.shared_state, "mutate_json_state", _tracking_mutate)
    monkeypatch.setattr(topic_notifications.webhook_egress, "deliver_webhook", _forbidden)
    monkeypatch.setattr(topic_notifications, "_send_email", _forbidden)

    # Settings/history mutations happen with no channel enabled: no send occurs,
    # and the assertion above would fire first if one were attempted under lock.
    topic_notifications.update_topic_notification_settings(
        profile_id, {"rules": [{"rule_id": "report_completed", "enabled": True}]}
    )
    topic_notifications._append_delivery_record(
        {"delivery_id": "d-x", "profile_id": profile_id, "created_at": "2026-01-01T00:00:00+00:00"}
    )
    assert depth["value"] == 0


# ---------------------------------------------------------------------------
# 19.6 read-only / lifecycle no-write
# ---------------------------------------------------------------------------


def test_app_lifecycle_and_probes_write_no_shared_state(tmp_path: Path) -> None:
    from app.api.health import get_health, get_readiness
    from app.main import create_app

    roots = [
        sessions._session_store_path().parent,
        topic_profiles.PROFILE_LIBRARY_DIR,
        topic_notifications.NOTIFICATION_LIBRARY_DIR,
    ]
    before = {str(root): _tree(root) if root.exists() else None for root in roots}

    app = create_app()
    app.openapi()
    get_health()
    get_readiness()

    after = {str(root): _tree(root) if root.exists() else None for root in roots}
    assert after == before
    for root in roots:
        if root.exists():
            assert list(root.glob("*.lock")) == []
            assert list(root.glob(".*lock")) == []


def test_read_only_paths_do_not_touch_existing_state(profile_id: str) -> None:
    topic_notifications._append_delivery_record(
        {"delivery_id": "d-1", "profile_id": profile_id, "created_at": "2026-01-01T00:00:00+00:00"}
    )
    roots = [topic_profiles.PROFILE_LIBRARY_DIR, topic_notifications.NOTIFICATION_LIBRARY_DIR]
    before = {str(root): _tree(root) for root in roots}

    topic_profiles.list_topic_profiles()
    topic_profiles.get_topic_profile(profile_id)
    topic_notifications.get_topic_notification_settings(profile_id)
    topic_notifications.list_topic_notification_deliveries(profile_id)
    topic_notifications.notification_delivery_health_summary()

    assert {str(root): _tree(root) for root in roots} == before


# ---------------------------------------------------------------------------
# 19.7 direct-writer guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_path",
    ["app/auth/sessions.py", "app/services/topic_profiles.py", "app/services/topic_notifications.py"],
)
def test_authoritative_stores_have_no_direct_write_bypass(module_path: str) -> None:
    """Every write to these stores must go through the mutation boundary.

    This is a source guard, not the proof: the concurrency tests above are. It
    exists to stop a new bypass writer being reintroduced silently.
    """
    import ast

    source = (REPO_ROOT / module_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    banned = {"write_json", "write_text", "write_bytes", "ensure_dir"}
    found = [
        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id in banned)
            or (isinstance(node.func, ast.Attribute) and node.func.attr in banned)
        )
    ]
    assert found == [], f"{module_path} writes shared state directly: {found}"


def test_no_public_save_whole_state_helper_remains() -> None:
    for module in (sessions, topic_profiles, topic_notifications):
        exported = [name for name in dir(module) if name.startswith("save_") or name.startswith("_save_")]
        assert exported == [], f"{module.__name__} still exposes {exported}"
