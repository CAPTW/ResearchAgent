from __future__ import annotations

import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from app.auth.config import AuthSettings
from app.auth.security_types import ProductUser
from app.domain import constants
from app.repositories import shared_state

_LOCK = threading.RLock()
_OPAQUE_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{43}$")
STATE_NAME = "auth_sessions"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _session_store_path() -> Path:
    return constants.STORAGE_ROOT.parent / "auth" / "product_sessions.json"


def is_valid_product_session_id(value: object) -> bool:
    return isinstance(value, str) and _OPAQUE_SESSION_ID.fullmatch(value) is not None


def _is_admin_identity(*, user_id: str, email: str | None, settings: AuthSettings) -> bool:
    normalized_admin_ids = {value.strip() for value in settings.admin_user_ids if value.strip()}
    normalized_admin_emails = {value.strip().lower() for value in settings.admin_user_emails if value.strip()}
    if user_id in normalized_admin_ids:
        return True
    if email and email.strip().lower() in normalized_admin_emails:
        return True
    return False


def _empty_store() -> dict[str, Any]:
    return {"store_version": 1, "sessions": {}}


def _sessions_of(store: dict[str, Any]) -> dict[str, Any]:
    sessions = store.get("sessions")
    return sessions if isinstance(sessions, dict) else {}


def _read_store() -> dict[str, Any]:
    """Read the store without creating anything.

    A corrupt store is *not* silently treated as empty: that would let the next
    write destroy every live session. It raises instead, and callers fail closed
    (no principal is authenticated, no login is issued).
    """
    store = shared_state.load_json_state(
        _session_store_path(),
        state_name=STATE_NAME,
        expected_type=dict,
        default=_empty_store,
    )
    if not isinstance(store.get("sessions"), dict):
        raise shared_state.SharedStateSchemaInvalid(STATE_NAME)
    return store


def _mutate_store(mutate: Callable[[dict[str, Any]], tuple[dict[str, Any], Any]]) -> Any:
    """Apply a mutation to the newest store contents under a cross-process lock.

    The callback always sees the state as of *inside* the lock, so a snapshot
    read earlier can never resurrect a session that was revoked or pruned in the
    meantime.
    """

    def _guarded(store: dict[str, Any]) -> tuple[dict[str, Any], Any]:
        if not isinstance(store.get("sessions"), dict):
            raise shared_state.SharedStateSchemaInvalid(STATE_NAME)
        return mutate(store)

    return shared_state.mutate_json_state(
        _session_store_path(),
        state_name=STATE_NAME,
        expected_type=dict,
        default=_empty_store,
        mutate=_guarded,
    )


def _record_to_user(record: dict[str, Any], *, settings: AuthSettings) -> ProductUser | None:
    expires_at = _parse_iso(str(record.get("expires_at") or ""))
    if expires_at is None or expires_at <= _now():
        return None
    if record.get("revoked_at"):
        return None
    user_id = str(record.get("user_id") or "").strip()
    if not user_id:
        return None
    email = str(record.get("email")).strip() if record.get("email") else None
    display_name = str(record.get("display_name") or user_id).strip() or user_id
    auth_source = str(record.get("auth_source") or "oidc").strip() or "oidc"
    is_admin = bool(record.get("is_admin")) or _is_admin_identity(user_id=user_id, email=email, settings=settings)
    return ProductUser(
        user_id=user_id,
        display_name=display_name,
        email=email,
        auth_source=auth_source,
        is_admin=is_admin,
    )


def create_product_session(user: ProductUser, *, settings: AuthSettings) -> str:
    created_at = _now()
    expires_at = created_at + timedelta(seconds=settings.product_session_ttl_seconds)
    session_id = secrets.token_urlsafe(32)
    record = {
        "session_id": session_id,
        "user_id": user.user_id,
        "display_name": user.display_name,
        "email": user.email,
        "auth_source": user.auth_source,
        "is_admin": user.is_admin,
        "created_at": _iso(created_at),
        "last_seen_at": _iso(created_at),
        "expires_at": _iso(expires_at),
        "revoked_at": None,
    }
    def _apply(store: dict[str, Any]) -> tuple[dict[str, Any], None]:
        store.setdefault("sessions", {})[session_id] = record
        return store, None

    with _LOCK:
        _mutate_store(_apply)
    return session_id


def read_product_session(session_id: str, *, settings: AuthSettings) -> ProductUser | None:
    if not is_valid_product_session_id(session_id):
        return None
    cleaned_session_id = session_id

    def _touch(store: dict[str, Any]) -> tuple[dict[str, Any], ProductUser | None]:
        # Re-resolve the record from the state inside the lock. A session that
        # was revoked or pruned after the caller's read is rejected here instead
        # of being written back to life by a stale whole-store snapshot.
        record = _sessions_of(store).get(cleaned_session_id)
        if not isinstance(record, dict):
            return store, None
        user = _record_to_user(record, settings=settings)
        if user is None:
            return store, None
        record["last_seen_at"] = _iso(_now())
        return store, user

    with _LOCK:
        try:
            # Resolve read-only first. An unknown or already-invalid session must
            # not enter the mutation boundary at all, so a lookup never creates
            # the store directory or a lock file.
            candidate = _sessions_of(_read_store()).get(cleaned_session_id)
            if not isinstance(candidate, dict) or _record_to_user(candidate, settings=settings) is None:
                return None
            return _mutate_store(_touch)
        except shared_state.SharedStateError:
            # Fail closed: an unreadable or contended store authenticates nobody.
            return None


def revoke_product_session(session_id: str) -> bool:
    if not is_valid_product_session_id(session_id):
        return False
    cleaned_session_id = session_id

    def _revoke(store: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        record = _sessions_of(store).get(cleaned_session_id)
        if not isinstance(record, dict):
            return store, False
        record["revoked_at"] = _iso(_now())
        return store, True

    with _LOCK:
        return _mutate_store(_revoke)


def prune_expired_product_sessions(*, settings: AuthSettings) -> int:
    def _prune(store: dict[str, Any]) -> tuple[dict[str, Any], int]:
        # Expiry is recomputed against the state inside the lock, so a session
        # created concurrently by another process is never pruned on the basis
        # of a snapshot taken before it existed.
        now = _now()
        removed = 0
        sessions = _sessions_of(store)
        for session_id, record in list(sessions.items()):
            expires_at = _parse_iso(str(record.get("expires_at") or "")) if isinstance(record, dict) else None
            if expires_at is None or expires_at <= now:
                sessions.pop(session_id, None)
                removed += 1
        return store, removed

    with _LOCK:
        return _mutate_store(_prune)
