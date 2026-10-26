from __future__ import annotations

import calendar
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import errno
import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import sqlite3
import threading
import time
from typing import Any
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.auth.config import get_auth_settings
from app.auth.resource_access import (
    ResourceAccessDenied,
    ResourceAction,
    ResourceActor,
    ResourceKind,
    authorize_resource_access,
    owner_user_id_for_actor,
    reject_client_authority_fields,
    resolve_resource_actor,
    scoped_system_actor,
)
from app.db.bootstrap import connect_scheduler_db, connect_scheduler_db_read_only
from app.domain.constants import STORAGE_ROOT
from app.repositories import metadata_index
from app.repositories.file_locks import FileLockTimeout, file_lock
from app.repositories.files import ensure_dir, iso_now_utc, read_json, slugify, write_json
from app.repositories.runs import load_run_record, save_run_record
from app.services import topic_notifications, topic_profiles

SCHEDULE_LIBRARY_DIR = STORAGE_ROOT.parent / "topic_schedules"
SCHEDULE_LIBRARY_PATH = SCHEDULE_LIBRARY_DIR / "schedules.json"
SCHEDULER_STATUS_PATH = SCHEDULE_LIBRARY_DIR / "worker_status.json"
DEFAULT_SCHEDULE_TIMEZONE = "Asia/Seoul"
SCHEDULE_LOCK = threading.RLock()
_STORE_LOCK_STATE = threading.local()
STORE_LOCK_TIMEOUT_SECONDS = 10.0
STORE_LOCK_STALE_SECONDS = 90.0
DISPATCH_LEASE_SECONDS = 120
SCHEDULER_HEARTBEAT_STALE_SECONDS = 30
SCHEDULER_RUNTIME_MODE = "single_node_file_backed"
SCHEDULER_STORAGE_BACKEND = "file_json"
DB_SCHEDULER_RUNTIME_MODE = "db_backed"
DB_SCHEDULER_STORAGE_BACKEND = "sqlite"
QUEUE_SCHEDULER_RUNTIME_MODE = "queue_backed"
QUEUE_SCHEDULER_STORAGE_BACKEND = "sqlite_queue"
GATED_SCHEDULER_MODES: dict[str, str] = {}
SCHEDULE_STORE_LOCKED_MESSAGE = "예약 작업 저장소가 현재 다른 작업에 의해 잠겨 있습니다. 잠시 후 다시 시도하세요."
STORE_LOCK_PROTOCOL = "OS_HANDLE_V1"
logger = logging.getLogger(__name__)
SUPPORTED_SCHEDULER_RUNTIME_MODES = {
    SCHEDULER_RUNTIME_MODE,
    DB_SCHEDULER_RUNTIME_MODE,
    QUEUE_SCHEDULER_RUNTIME_MODE,
    *GATED_SCHEDULER_MODES.keys(),
}


class ScheduleStoreLockedError(RuntimeError):
    """Raised when a live schedule-store owner keeps the file-backed lock."""


class ScheduleTimezoneUnavailable(RuntimeError):
    """The saved schedule's clock cannot be resolved in this environment."""


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _distributed_scheduler_requested() -> bool:
    mode = str(os.getenv("SCHEDULER_DISTRIBUTED_MODE") or "").strip().lower()
    return _env_flag("SCHEDULER_DISTRIBUTED", False) or mode in {"1", "true", "yes", "on", "enabled", "distributed"}


class SchedulerBackend:
    mode = "unavailable"
    storage_backend = "unavailable"
    display_name = "Unavailable scheduler backend"
    distributed_ready = False
    multi_node_dispatch_safe = False
    available = False
    read_only = False
    unavailable_reason = "Scheduler backend is unavailable."
    architecture_note = "Scheduler backend is unavailable."

    def capabilities(self) -> dict[str, bool]:
        return {
            "task_definition_store": False,
            "due_task_query": False,
            "dispatch_lease": False,
            "heartbeat_status": False,
            "orphan_recovery": False,
            "next_run_calculation": False,
            "run_trigger_source_tracking": False,
        }

    def descriptor(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "storage_backend": self.storage_backend,
            "display_name": self.display_name,
            "available": self.available,
            "read_only": self.read_only,
            "unavailable_reason": None if self.available else self.unavailable_reason,
            "distributed_ready": self.distributed_ready,
            "multi_node_dispatch_safe": self.multi_node_dispatch_safe,
            "configured_runtime_mode": getattr(self, "configured_runtime_mode", self.mode),
            "configured_runtime_mode_raw": getattr(self, "configured_runtime_mode_raw", self.mode),
            "configured_runtime_mode_source": getattr(self, "configured_runtime_mode_source", "default"),
            "configured_runtime_mode_valid": getattr(self, "configured_runtime_mode_valid", self.available),
            "capabilities": self.capabilities(),
            "architecture_note": self.architecture_note,
        }

    def load_task_definitions(self) -> list[dict[str, Any]]:
        raise RuntimeError(self.unavailable_reason)

    def save_task_definitions(self, schedules: list[dict[str, Any]]) -> None:
        raise RuntimeError(self.unavailable_reason)

    def query_due_task_ids(
        self,
        schedules: list[dict[str, Any]],
        *,
        now: datetime | None = None,
        recover_orphans: bool = False,
        worker_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        raise RuntimeError(self.unavailable_reason)

    def acquire_dispatch_lease(self, schedule: dict[str, Any], *, worker_id: str | None = None) -> str:
        raise RuntimeError(self.unavailable_reason)

    def release_dispatch_lease(self, schedule: dict[str, Any]) -> None:
        raise RuntimeError(self.unavailable_reason)

    def trigger_source_for_task(self, *, trigger: str, task_type: str | None) -> str:
        source_prefix = "topic_schedule_run_now" if trigger == "manual" else "topic_schedule_auto"
        return f"{source_prefix}:{task_type or action_for_task_type(task_type)}"

    def record_heartbeat(self, payload: dict[str, Any]) -> None:
        return None


class FileJsonSchedulerBackend(SchedulerBackend):
    mode = SCHEDULER_RUNTIME_MODE
    storage_backend = SCHEDULER_STORAGE_BACKEND
    display_name = "single-node file-backed scheduler"
    available = True
    unavailable_reason = ""
    architecture_note = (
        "자동 실행, nextRunAt, heartbeat, file-backed leases, duplicate protection, and orphan recovery are real. "
        "This runtime is single-node/file-backed and is not DB/queue-backed distributed scheduling."
    )

    def capabilities(self) -> dict[str, bool]:
        return {
            "task_definition_store": True,
            "due_task_query": True,
            "dispatch_lease": True,
            "heartbeat_status": True,
            "orphan_recovery": True,
            "next_run_calculation": True,
            "run_trigger_source_tracking": True,
        }

    def load_task_definitions(self) -> list[dict[str, Any]]:
        return _materialize_raw_schedules()

    def save_task_definitions(self, schedules: list[dict[str, Any]]) -> None:
        _save_schedules(schedules)

    def query_due_task_ids(
        self,
        schedules: list[dict[str, Any]],
        *,
        now: datetime | None = None,
        recover_orphans: bool = False,
        worker_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        return _sync_runtime_schedules(schedules, now=now, recover_orphans=recover_orphans)

    def acquire_dispatch_lease(self, schedule: dict[str, Any], *, worker_id: str | None = None) -> str:
        return _acquire_dispatch_lease(schedule, worker_id=worker_id)

    def release_dispatch_lease(self, schedule: dict[str, Any]) -> None:
        _clear_dispatch_lease(schedule)

    def trigger_source_for_task(self, *, trigger: str, task_type: str | None) -> str:
        source_prefix = "topic_schedule_run_now" if trigger == "manual" else "topic_schedule_auto"
        return f"{source_prefix}:{task_type or action_for_task_type(task_type)}"


class DbSchedulerBackend(SchedulerBackend):
    mode = DB_SCHEDULER_RUNTIME_MODE
    storage_backend = DB_SCHEDULER_STORAGE_BACKEND
    display_name = "SQLite DB-backed scheduler"
    available = True
    unavailable_reason = ""
    architecture_note = (
        "SQLite DB-backed dispatch leases are real for competing workers/processes sharing the same scheduler DB. "
        "This is not a queue-backed or horizontally distributed multi-node scheduler."
    )
    multi_process_dispatch_safe = True

    def capabilities(self) -> dict[str, bool]:
        return {
            "task_definition_store": True,
            "due_task_query": True,
            "dispatch_lease": True,
            "heartbeat_status": True,
            "orphan_recovery": True,
            "next_run_calculation": True,
            "run_trigger_source_tracking": True,
        }

    def descriptor(self) -> dict[str, Any]:
        descriptor = super().descriptor()
        descriptor["multi_process_dispatch_safe"] = True
        return descriptor

    def _row_to_schedule(self, row: Any) -> dict[str, Any]:
        payload = json.loads(str(row["payload_json"] or "{}"))
        return payload if isinstance(payload, dict) else {}

    def _upsert_schedule(self, connection: Any, schedule: dict[str, Any]) -> None:
        schedule_id = str(schedule.get("schedule_id") or "").strip()
        if not schedule_id:
            return
        connection.execute(
            """
            INSERT INTO scheduler_schedules (
              schedule_id, profile_id, task_type, enabled, deleted, next_run_at,
              current_run_id, current_run_status, scheduler_state, lock_token,
              lock_owner, lock_acquired_at, lock_expires_at, last_worker_id,
              dispatch_attempts, updated_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(schedule_id) DO UPDATE SET
              profile_id = excluded.profile_id,
              task_type = excluded.task_type,
              enabled = excluded.enabled,
              deleted = excluded.deleted,
              next_run_at = excluded.next_run_at,
              current_run_id = excluded.current_run_id,
              current_run_status = excluded.current_run_status,
              scheduler_state = excluded.scheduler_state,
              lock_token = excluded.lock_token,
              lock_owner = excluded.lock_owner,
              lock_acquired_at = excluded.lock_acquired_at,
              lock_expires_at = excluded.lock_expires_at,
              last_worker_id = excluded.last_worker_id,
              dispatch_attempts = excluded.dispatch_attempts,
              updated_at = excluded.updated_at,
              payload_json = excluded.payload_json
            """,
            (
                schedule_id,
                str(schedule.get("profile_id") or ""),
                str(schedule.get("task_type") or ""),
                1 if bool(schedule.get("enabled")) else 0,
                1 if bool(schedule.get("deleted")) else 0,
                schedule.get("next_run_at"),
                schedule.get("current_run_id"),
                schedule.get("current_run_status"),
                str(schedule.get("scheduler_state") or "paused"),
                schedule.get("lock_token"),
                schedule.get("lock_owner"),
                schedule.get("lock_acquired_at"),
                schedule.get("lock_expires_at"),
                schedule.get("last_worker_id"),
                int(schedule.get("dispatch_attempts") or 0),
                str(schedule.get("updated_at") or iso_now_utc()),
                json.dumps(schedule, ensure_ascii=False),
            ),
        )

    def _load_db_schedules(self) -> list[dict[str, Any]]:
        with connect_scheduler_db() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM scheduler_schedules ORDER BY updated_at DESC, schedule_id ASC"
            ).fetchall()
        return [self._row_to_schedule(row) for row in rows]

    def _load_db_schedules_read_only(self) -> list[dict[str, Any]]:
        try:
            with connect_scheduler_db_read_only() as connection:
                rows = connection.execute(
                    "SELECT payload_json FROM scheduler_schedules "
                    "ORDER BY updated_at DESC, schedule_id ASC"
                ).fetchall()
        except (FileNotFoundError, sqlite3.Error):
            return []
        return [self._row_to_schedule(row) for row in rows]

    def _legacy_import_schedule(self, schedule: dict[str, Any]) -> dict[str, Any]:
        imported = dict(schedule)
        imported["scheduler_backend"] = self.mode
        imported["legacy_source_backend"] = SCHEDULER_RUNTIME_MODE
        imported["migration_mode"] = "import_on_first_db_read"
        imported.setdefault("imported_from_legacy_file_at", iso_now_utc())
        imported["updated_at"] = str(imported.get("updated_at") or iso_now_utc())
        return imported

    def _legacy_import_schedules(self, schedules: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self._legacy_import_schedule(schedule) for schedule in schedules]

    def load_task_definitions(self) -> list[dict[str, Any]]:
        db_schedules = self._load_db_schedules()
        # Once the DB store is initialized, it is the authoritative task-definition
        # store for db_backed and queue_backed modes. Importing later file-only
        # schedules would be a silent migration and can double-run definitions when
        # operators switch modes.
        if db_schedules:
            return db_schedules

        file_schedules = _materialize_raw_schedules()
        imported_schedules = self._legacy_import_schedules(file_schedules)
        self.save_task_definitions(imported_schedules)
        return imported_schedules

    def save_task_definitions(self, schedules: list[dict[str, Any]]) -> None:
        with connect_scheduler_db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for schedule in schedules:
                    self._upsert_schedule(connection, schedule)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def query_due_task_ids(
        self,
        schedules: list[dict[str, Any]],
        *,
        now: datetime | None = None,
        recover_orphans: bool = False,
        worker_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        return _sync_runtime_schedules(schedules, now=now, recover_orphans=recover_orphans)

    def acquire_dispatch_lease(self, schedule: dict[str, Any], *, worker_id: str | None = None) -> str:
        schedule_id = str(schedule.get("schedule_id") or "").strip()
        if not schedule_id:
            raise RuntimeError("schedule_id 값이 필요합니다.")
        resolved_now = _utc_now()
        with connect_scheduler_db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT payload_json FROM scheduler_schedules WHERE schedule_id = ?",
                    (schedule_id,),
                ).fetchone()
                if row is None:
                    raise FileNotFoundError(f"Topic schedule not found: {schedule_id}")
                current = self._row_to_schedule(row)
                if _lease_active(current, now=resolved_now):
                    raise RuntimeError("이미 다른 scheduler worker가 예약 작업 dispatch lock을 보유하고 있습니다.")
                token = f"lease-{slugify(schedule_id or 'schedule')}-{uuid.uuid4().hex[:12]}"
                owner = str(worker_id or f"pid-{os.getpid()}").strip()
                current["lock_token"] = token
                current["lock_owner"] = owner
                current["lock_acquired_at"] = resolved_now.isoformat()
                current["lock_expires_at"] = (resolved_now + timedelta(seconds=DISPATCH_LEASE_SECONDS)).isoformat()
                current["last_worker_id"] = owner
                current["dispatch_attempts"] = int(current.get("dispatch_attempts") or 0) + 1
                current["updated_at"] = iso_now_utc()
                self._upsert_schedule(connection, current)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        schedule.clear()
        schedule.update(current)
        return token

    def release_dispatch_lease(self, schedule: dict[str, Any]) -> None:
        schedule_id = str(schedule.get("schedule_id") or "").strip()
        if not schedule_id:
            _clear_dispatch_lease(schedule)
            return
        _clear_dispatch_lease(schedule)
        schedule["updated_at"] = iso_now_utc()
        with connect_scheduler_db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._upsert_schedule(connection, schedule)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def record_heartbeat(self, payload: dict[str, Any]) -> None:
        worker_id = str(payload.get("worker_id") or "").strip()
        if not worker_id:
            return
        with connect_scheduler_db() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO scheduler_worker_heartbeats (
                  worker_id, mode, backend_mode, enabled, running, pid, thread,
                  tick_seconds, started_at, heartbeat_at, last_cycle_summary_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    worker_id,
                    str(payload.get("mode") or ""),
                    self.mode,
                    1 if bool(payload.get("enabled")) else 0,
                    1 if bool(payload.get("running")) else 0,
                    payload.get("pid"),
                    payload.get("thread"),
                    payload.get("tick_seconds"),
                    payload.get("started_at"),
                    str(payload.get("heartbeat_at") or iso_now_utc()),
                    json.dumps(payload.get("last_cycle_summary") or {}, ensure_ascii=False),
                ),
                )


class SchedulerQueueBackend:
    """Durable scheduler queue contract backed by the repo scheduler SQLite DB."""

    def _queue_item_id(self, schedule_id: str, due_at: str) -> str:
        digest = hashlib.sha256(f"{schedule_id}:{due_at}".encode("utf-8")).hexdigest()[:16]
        return f"queue-{slugify(schedule_id or 'schedule')}-{digest}"

    def _row_to_item(self, row: Any, *, now: datetime | None = None) -> dict[str, Any]:
        resolved_now = now or _utc_now()
        payload = json.loads(str(row["payload_json"] or "{}"))
        schedule = payload if isinstance(payload, dict) else {}
        stored_status = str(row["status"] or "")
        lease_expires_at = _parse_iso_datetime(row["lease_expires_at"])
        status = "expired" if stored_status == "leased" and (lease_expires_at is None or lease_expires_at <= resolved_now) else stored_status
        return {
            "queue_item_id": str(row["queue_item_id"] or ""),
            "task_id": str(row["schedule_id"] or ""),
            "schedule_id": str(row["schedule_id"] or ""),
            "topic_id": str(schedule.get("profile_id") or ""),
            "due_at": row["due_at"],
            "scheduled_for": row["due_at"],
            "enqueued_at": row["created_at"],
            "attempt_count": int(row["dispatch_attempts"] or 0),
            "status": status,
            "stored_status": stored_status,
            "worker_id": row["lease_owner"],
            "lease_token": row["lease_token"],
            "lease_acquired_at": row["lease_acquired_at"],
            "lease_expires_at": row["lease_expires_at"],
            "visibility_timeout_seconds": DISPATCH_LEASE_SECONDS,
            "run_id": row["run_id"],
            "error_message": row["error_message"],
            "updated_at": row["updated_at"],
            "trigger_source": "automation_queue",
            "retry_eligible": status in {"expired", "failed"},
            "payload": schedule,
        }

    def list_items(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        with connect_scheduler_db() as connection:
            rows = connection.execute(
                "SELECT * FROM scheduler_queue_items ORDER BY created_at ASC, queue_item_id ASC"
            ).fetchall()
        return [self._row_to_item(row, now=now) for row in rows]

    def get_item(self, queue_item_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        with connect_scheduler_db() as connection:
            row = connection.execute(
                "SELECT * FROM scheduler_queue_items WHERE queue_item_id = ?",
                (queue_item_id,),
            ).fetchone()
        if row is None:
            raise FileNotFoundError(f"Scheduler queue item not found: {queue_item_id}")
        return self._row_to_item(row, now=now)

    def queue_summary(self, *, now: datetime | None = None) -> dict[str, Any]:
        resolved_now = now or _utc_now()
        with connect_scheduler_db() as connection:
            rows = connection.execute("SELECT status, lease_expires_at FROM scheduler_queue_items").fetchall()
        queued = 0
        leased = 0
        expired = 0
        completed = 0
        failed = 0
        for row in rows:
            status = str(row["status"] or "")
            if status == "queued":
                queued += 1
            elif status == "leased":
                lease_expires_at = _parse_iso_datetime(row["lease_expires_at"])
                if lease_expires_at is not None and lease_expires_at <= resolved_now:
                    expired += 1
                else:
                    leased += 1
            elif status == "completed":
                completed += 1
            elif status == "failed":
                failed += 1
        return {
            "total": len(rows),
            "queued": queued,
            "leased": leased,
            "expired": expired,
            "completed": completed,
            "failed": failed,
            "healthy": failed == 0,
        }

    def enqueue(self, connection: Any, schedule: dict[str, Any], *, enqueued_at: datetime | None = None) -> str | None:
        schedule_id = str(schedule.get("schedule_id") or "").strip()
        due_at = str(schedule.get("next_run_at") or "").strip()
        if not schedule_id or not due_at:
            return None
        now_iso = (enqueued_at or _utc_now()).isoformat()
        queue_item_id = self._queue_item_id(schedule_id, due_at)
        connection.execute(
            """
            INSERT OR IGNORE INTO scheduler_queue_items (
              queue_item_id, schedule_id, due_at, status, lease_token, lease_owner,
              lease_acquired_at, lease_expires_at, dispatch_attempts, run_id,
              error_message, created_at, updated_at, payload_json
            ) VALUES (?, ?, ?, 'queued', NULL, NULL, NULL, NULL, 0, NULL, NULL, ?, ?, ?)
            """,
            (
                queue_item_id,
                schedule_id,
                due_at,
                now_iso,
                now_iso,
                json.dumps(schedule, ensure_ascii=False),
            ),
        )
        return queue_item_id

    def lease(
        self,
        connection: Any,
        schedule: dict[str, Any],
        *,
        now: datetime,
        worker_id: str | None = None,
    ) -> bool:
        schedule_id = str(schedule.get("schedule_id") or "").strip()
        due_at = str(schedule.get("next_run_at") or "").strip()
        if not schedule_id or not due_at:
            return False
        now_iso = now.isoformat()
        row = connection.execute(
            """
            SELECT queue_item_id, status, lease_owner, lease_expires_at
            FROM scheduler_queue_items
            WHERE schedule_id = ? AND due_at = ?
            """,
            (schedule_id, due_at),
        ).fetchone()
        if row is None:
            return False
        status = str(row["status"] or "")
        lease_expires_at = _parse_iso_datetime(row["lease_expires_at"])
        expired = status == "leased" and (lease_expires_at is None or lease_expires_at <= now)
        if status != "queued" and not expired:
            return False
        token = f"queue-lease-{slugify(schedule_id or 'schedule')}-{uuid.uuid4().hex[:12]}"
        owner = str(worker_id or f"pid-{os.getpid()}").strip()
        expires_at = (now + timedelta(seconds=DISPATCH_LEASE_SECONDS)).isoformat()
        cursor = connection.execute(
            """
            UPDATE scheduler_queue_items
            SET status = 'leased',
                lease_token = ?,
                lease_owner = ?,
                lease_acquired_at = ?,
                lease_expires_at = ?,
                dispatch_attempts = dispatch_attempts + 1,
                updated_at = ?
            WHERE queue_item_id = ?
              AND (
                status = 'queued'
                OR (status = 'leased' AND (lease_expires_at IS NULL OR lease_expires_at <= ?))
              )
            """,
            (token, owner, now_iso, expires_at, now_iso, row["queue_item_id"], now_iso),
        )
        if cursor.rowcount != 1:
            return False
        schedule["queue_item_id"] = row["queue_item_id"]
        schedule["queue_due_at"] = due_at
        schedule["queue_status"] = "leased"
        schedule["queue_lease_token"] = token
        schedule["queue_lease_owner"] = owner
        schedule["queue_lease_expires_at"] = expires_at
        schedule["scheduler_state"] = "queued"
        schedule["last_worker_id"] = owner
        if expired:
            previous_owner = str(row["lease_owner"] or "unknown worker").strip()
            schedule["last_queue_recovery_at"] = now_iso
            schedule["last_queue_recovery_message"] = f"만료된 queue lease를 회수했습니다: {previous_owner}"
        schedule["updated_at"] = now_iso
        return True

    def acknowledge(
        self,
        queue_item_id: str,
        *,
        run_id: str | None,
        worker_id: str | None = None,
        lease_token: str | None = None,
    ) -> None:
        if not str(lease_token or "").strip():
            raise RuntimeError("queue lease token is required to acknowledge a queue item.")
        now_iso = iso_now_utc()
        with connect_scheduler_db() as connection:
            cursor = connection.execute(
                """
                UPDATE scheduler_queue_items
                SET status = 'completed',
                    run_id = ?,
                    error_message = NULL,
                    lease_token = NULL,
                    lease_owner = COALESCE(?, lease_owner),
                    lease_acquired_at = NULL,
                    lease_expires_at = NULL,
                    updated_at = ?
                WHERE queue_item_id = ?
                  AND status = 'leased'
                  AND lease_token = ?
                """,
                (run_id, worker_id, now_iso, queue_item_id, lease_token),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("queue lease is no longer current; refusing stale queue acknowledgement.")

    def fail(
        self,
        queue_item_id: str,
        *,
        error_message: str | None,
        worker_id: str | None = None,
        lease_token: str | None = None,
    ) -> None:
        if not str(lease_token or "").strip():
            raise RuntimeError("queue lease token is required to fail a queue item.")
        now_iso = iso_now_utc()
        with connect_scheduler_db() as connection:
            cursor = connection.execute(
                """
                UPDATE scheduler_queue_items
                SET status = 'failed',
                    error_message = ?,
                    lease_token = NULL,
                    lease_owner = COALESCE(?, lease_owner),
                    lease_acquired_at = NULL,
                    lease_expires_at = NULL,
                    updated_at = ?
                WHERE queue_item_id = ?
                  AND status = 'leased'
                  AND lease_token = ?
                """,
                (error_message, worker_id, now_iso, queue_item_id, lease_token),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("queue lease is no longer current; refusing stale queue failure recording.")


class QueueSchedulerBackend(DbSchedulerBackend):
    mode = QUEUE_SCHEDULER_RUNTIME_MODE
    storage_backend = QUEUE_SCHEDULER_STORAGE_BACKEND
    display_name = "SQLite queue-backed scheduler"
    architecture_note = (
        "SQLite queue-backed scheduling is real for competing workers/processes sharing the same scheduler DB. "
        "Due schedules are enqueued durably, queue leases expire, and run dispatch remains duplicate-safe. "
        "This is not a horizontally distributed multi-node queue service."
    )
    multi_process_dispatch_safe = True
    queue_backend = SchedulerQueueBackend()

    def capabilities(self) -> dict[str, bool]:
        capabilities = super().capabilities()
        capabilities["durable_queue"] = True
        return capabilities

    def descriptor(self) -> dict[str, Any]:
        descriptor = super().descriptor()
        descriptor["queue"] = self.queue_summary()
        descriptor["multi_process_dispatch_safe"] = True
        return descriptor

    def trigger_source_for_task(self, *, trigger: str, task_type: str | None) -> str:
        if trigger == "manual":
            return super().trigger_source_for_task(trigger=trigger, task_type=task_type)
        return "automation_queue"

    def _queue_item_id(self, schedule_id: str, due_at: str) -> str:
        digest = hashlib.sha256(f"{schedule_id}:{due_at}".encode("utf-8")).hexdigest()[:16]
        return f"queue-{slugify(schedule_id or 'schedule')}-{digest}"

    def _queue_summary_with_connection(self, connection: Any, *, now: datetime | None = None) -> dict[str, Any]:
        resolved_now = now or _utc_now()
        rows = connection.execute(
            """
            SELECT status, lease_expires_at
            FROM scheduler_queue_items
            """
        ).fetchall()
        queued = 0
        leased = 0
        expired = 0
        completed = 0
        failed = 0
        for row in rows:
            status = str(row["status"] or "")
            if status == "queued":
                queued += 1
            elif status == "leased":
                lease_expires_at = _parse_iso_datetime(row["lease_expires_at"])
                if lease_expires_at is not None and lease_expires_at <= resolved_now:
                    expired += 1
                else:
                    leased += 1
            elif status == "completed":
                completed += 1
            elif status == "failed":
                failed += 1
        return {
            "total": len(rows),
            "queued": queued,
            "leased": leased,
            "expired": expired,
            "completed": completed,
            "failed": failed,
            "healthy": failed == 0,
        }

    def queue_summary(self, *, now: datetime | None = None) -> dict[str, Any]:
        return self.queue_backend.queue_summary(now=now)

    def _enqueue_due_schedule(self, connection: Any, schedule: dict[str, Any], *, enqueued_at: datetime) -> None:
        self.queue_backend.enqueue(connection, schedule, enqueued_at=enqueued_at)

    def _lease_queue_item(
        self,
        connection: Any,
        schedule: dict[str, Any],
        *,
        now: datetime,
        worker_id: str | None = None,
    ) -> bool:
        leased = self.queue_backend.lease(connection, schedule, now=now, worker_id=worker_id)
        if leased:
            self._upsert_schedule(connection, schedule)
        return leased

    def query_due_task_ids(
        self,
        schedules: list[dict[str, Any]],
        *,
        now: datetime | None = None,
        recover_orphans: bool = False,
        worker_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        resolved_now = now or _utc_now()
        schedules, due_schedule_ids = _sync_runtime_schedules(
            schedules,
            now=resolved_now,
            recover_orphans=recover_orphans,
        )
        if not due_schedule_ids:
            return schedules, []
        due_id_set = set(due_schedule_ids)
        leased_schedule_ids: list[str] = []
        with connect_scheduler_db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for schedule in schedules:
                    if str(schedule.get("schedule_id") or "").strip() in due_id_set:
                        self._enqueue_due_schedule(connection, schedule, enqueued_at=resolved_now)
                for schedule_id in due_schedule_ids:
                    schedule = next(
                        (item for item in schedules if str(item.get("schedule_id") or "").strip() == schedule_id),
                        None,
                    )
                    if schedule is not None and self._lease_queue_item(connection, schedule, now=resolved_now, worker_id=worker_id):
                        leased_schedule_ids.append(schedule_id)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return schedules, leased_schedule_ids

    def release_dispatch_lease(self, schedule: dict[str, Any]) -> None:
        queue_item_id = str(schedule.get("queue_item_id") or "").strip()
        queue_status = None
        if queue_item_id:
            queue_status = "failed" if schedule.get("last_error") or schedule.get("scheduler_state") == "failed" else "completed"
            now_iso = iso_now_utc()
            run_id = schedule.get("current_run_id") or schedule.get("last_run_id")
            error_message = schedule.get("last_error") or schedule.get("last_failure_message")
            worker_id = schedule.get("queue_lease_owner") or schedule.get("last_worker_id")
            lease_token = str(schedule.get("queue_lease_token") or "").strip() or None
            if queue_status == "failed":
                self.queue_backend.fail(
                    queue_item_id,
                    error_message=str(error_message or ""),
                    worker_id=worker_id,
                    lease_token=lease_token,
                )
            else:
                self.queue_backend.acknowledge(
                    queue_item_id,
                    run_id=str(run_id or ""),
                    worker_id=worker_id,
                    lease_token=lease_token,
                )
            schedule["last_queue_item_id"] = queue_item_id
            schedule["last_queue_status"] = queue_status
            schedule["last_queue_completed_at" if queue_status == "completed" else "last_queue_failed_at"] = now_iso
            schedule["queue_item_id"] = None
            schedule["queue_due_at"] = None
            schedule["queue_status"] = None
            schedule["queue_lease_token"] = None
            schedule["queue_lease_owner"] = None
            schedule["queue_lease_expires_at"] = None
        super().release_dispatch_lease(schedule)

    def record_heartbeat(self, payload: dict[str, Any]) -> None:
        super().record_heartbeat(payload)


class LegacyFileReadOnlySchedulerBackend(SchedulerBackend):
    mode = "legacy_file_read_only"
    storage_backend = SCHEDULER_STORAGE_BACKEND
    display_name = "read-only legacy file scheduler fallback"
    available = False
    read_only = True
    distributed_ready = False
    multi_node_dispatch_safe = False
    architecture_note = (
        "Configured scheduler backend is unavailable. Existing file-backed schedules are shown read-only for operator "
        "visibility, but automatic dispatch and mutations are blocked to avoid duplicate or ambiguous execution."
    )

    def __init__(self, *, configured_backend: str, reason: str):
        self.configured_backend = configured_backend
        self.unavailable_reason = f"{configured_backend} unavailable: {reason}"

    def capabilities(self) -> dict[str, bool]:
        return {
            "task_definition_store": True,
            "due_task_query": False,
            "dispatch_lease": False,
            "heartbeat_status": True,
            "orphan_recovery": False,
            "next_run_calculation": False,
            "run_trigger_source_tracking": False,
        }

    def descriptor(self) -> dict[str, Any]:
        descriptor = super().descriptor()
        descriptor["configured_backend"] = self.configured_backend
        return descriptor

    def load_task_definitions(self) -> list[dict[str, Any]]:
        return _materialize_raw_schedules()

    def save_task_definitions(self, schedules: list[dict[str, Any]]) -> None:
        return None

    def query_due_task_ids(
        self,
        schedules: list[dict[str, Any]],
        *,
        now: datetime | None = None,
        recover_orphans: bool = False,
        worker_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        return schedules, []

    def acquire_dispatch_lease(self, schedule: dict[str, Any], *, worker_id: str | None = None) -> str:
        raise RuntimeError("Configured scheduler backend is unavailable; read-only legacy fallback cannot dispatch tasks.")

    def release_dispatch_lease(self, schedule: dict[str, Any]) -> None:
        return None


FILE_JSON_SCHEDULER_BACKEND = FileJsonSchedulerBackend()
DB_SCHEDULER_BACKEND = DbSchedulerBackend()
QUEUE_SCHEDULER_BACKEND = QueueSchedulerBackend()


class ConfiguredSchedulerBackend:
    """Per-call scheduler backend view with immutable config provenance."""

    def __init__(self, backend: SchedulerBackend, config: dict[str, Any]):
        self._backend = backend
        self.configured_runtime_mode = config["mode"]
        self.configured_runtime_mode_raw = config["raw"]
        self.configured_runtime_mode_source = config["source"]
        self.configured_runtime_mode_valid = bool(config["valid"])

    def __getattr__(self, name: str) -> Any:
        return getattr(self._backend, name)

    def capabilities(self) -> dict[str, bool]:
        return self._backend.capabilities()

    def descriptor(self) -> dict[str, Any]:
        descriptor = self._backend.descriptor()
        descriptor["configured_runtime_mode"] = self.configured_runtime_mode
        descriptor["configured_runtime_mode_raw"] = self.configured_runtime_mode_raw
        descriptor["configured_runtime_mode_source"] = self.configured_runtime_mode_source
        descriptor["configured_runtime_mode_valid"] = self.configured_runtime_mode_valid
        return descriptor


def _db_scheduler_unavailable_reason() -> str | None:
    """Availability check for the opt-in db_backed/queue_backed scheduler backends.

    Deliberately unchanged: selecting a DB-backed backend for real dispatch
    requires an initialised store, and the existing contract requires an
    unavailable backend to be reported with its reason. This path is only reached
    when SCHEDULER_MODE/TOPIC_SCHEDULER_RUNTIME_MODE opts into a DB-backed mode;
    the default single-node file-backed mode used by liveness/readiness never
    reaches it. Store creation therefore remains possible only under an explicit
    DB-backed opt-in, which is recorded as a residual in this lane's report.
    """
    try:
        with connect_scheduler_db():
            return None
    except Exception as exc:
        return str(exc)


def _scheduler_runtime_config(mode: str | None = None) -> dict[str, Any]:
    if mode is not None:
        raw = str(mode).strip().lower()
        source = "argument"
    elif os.getenv("SCHEDULER_MODE") is not None:
        raw = str(os.getenv("SCHEDULER_MODE") or "").strip().lower()
        source = "SCHEDULER_MODE"
    elif os.getenv("TOPIC_SCHEDULER_RUNTIME_MODE") is not None:
        raw = str(os.getenv("TOPIC_SCHEDULER_RUNTIME_MODE") or "").strip().lower()
        source = "TOPIC_SCHEDULER_RUNTIME_MODE"
    elif os.getenv("TOPIC_SCHEDULER_BACKEND") is not None:
        raw = str(os.getenv("TOPIC_SCHEDULER_BACKEND") or "").strip().lower()
        source = "TOPIC_SCHEDULER_BACKEND"
    else:
        raw = SCHEDULER_RUNTIME_MODE
        source = "default"
    valid = raw in SUPPORTED_SCHEDULER_RUNTIME_MODES
    return {
        "raw": raw,
        "source": source,
        "valid": valid,
        "mode": raw if valid else raw,
    }


def _configured_backend(backend: SchedulerBackend, config: dict[str, Any]) -> ConfiguredSchedulerBackend:
    return ConfiguredSchedulerBackend(backend, config)


def get_scheduler_backend(mode: str | None = None) -> SchedulerBackend:
    config = _scheduler_runtime_config(mode)
    requested = str(config["mode"])
    if not config["valid"]:
        return _configured_backend(
            LegacyFileReadOnlySchedulerBackend(
                configured_backend=requested,
                reason=(
                    f"Invalid {config['source']} '{config['raw']}'; supported values are "
                    f"{', '.join(sorted(SUPPORTED_SCHEDULER_RUNTIME_MODES))}."
                ),
            ),
            config,
        )
    if requested == SCHEDULER_RUNTIME_MODE:
        return _configured_backend(FILE_JSON_SCHEDULER_BACKEND, config)
    if requested == DB_SCHEDULER_RUNTIME_MODE:
        unavailable_reason = _db_scheduler_unavailable_reason()
        if unavailable_reason:
            return _configured_backend(
                LegacyFileReadOnlySchedulerBackend(
                    configured_backend=DB_SCHEDULER_RUNTIME_MODE,
                    reason=unavailable_reason,
                ),
                config,
            )
        return _configured_backend(DB_SCHEDULER_BACKEND, config)
    if requested == QUEUE_SCHEDULER_RUNTIME_MODE:
        unavailable_reason = _db_scheduler_unavailable_reason()
        if unavailable_reason:
            return _configured_backend(
                LegacyFileReadOnlySchedulerBackend(
                    configured_backend=QUEUE_SCHEDULER_RUNTIME_MODE,
                    reason=unavailable_reason,
                ),
                config,
            )
        return _configured_backend(QUEUE_SCHEDULER_BACKEND, config)
    if requested in GATED_SCHEDULER_MODES:
        return _configured_backend(
            LegacyFileReadOnlySchedulerBackend(
                configured_backend=requested,
                reason=GATED_SCHEDULER_MODES[requested],
            ),
            config,
        )
    return _configured_backend(SchedulerBackend(), config)


def describe_scheduler_backends() -> dict[str, dict[str, Any]]:
    modes: dict[str, dict[str, Any]] = {
        SCHEDULER_RUNTIME_MODE: {
            "mode": SCHEDULER_RUNTIME_MODE,
            "storage_backend": SCHEDULER_STORAGE_BACKEND,
            "status": "available",
            "available": True,
            "distributed_ready": False,
            "multi_node_dispatch_safe": False,
            "reason": None,
        }
    }
    modes[DB_SCHEDULER_RUNTIME_MODE] = {
        "mode": DB_SCHEDULER_RUNTIME_MODE,
        "storage_backend": DB_SCHEDULER_STORAGE_BACKEND,
        "status": "available",
        "available": True,
        "distributed_ready": False,
        "multi_node_dispatch_safe": False,
        "multi_process_dispatch_safe": True,
        "reason": (
            "SQLite DB-backed dispatch is available for competing workers/processes sharing the same scheduler DB; "
            "it is not a queue-backed multi-node scheduler."
        ),
    }
    modes[QUEUE_SCHEDULER_RUNTIME_MODE] = {
        "mode": QUEUE_SCHEDULER_RUNTIME_MODE,
        "storage_backend": QUEUE_SCHEDULER_STORAGE_BACKEND,
        "status": "available",
        "available": True,
        "distributed_ready": False,
        "multi_node_dispatch_safe": False,
        "multi_process_dispatch_safe": True,
        "reason": (
            "SQLite queue-backed dispatch is available for competing workers/processes sharing the same scheduler DB; "
            "it is not a horizontally distributed multi-node queue service."
        ),
    }
    for mode, reason in GATED_SCHEDULER_MODES.items():
        modes[mode] = {
            "mode": mode,
            "storage_backend": None,
            "status": "unavailable",
            "available": False,
            "distributed_ready": False,
            "multi_node_dispatch_safe": False,
            "reason": reason,
        }
    return modes

DEFAULT_PIPELINE = [
    {
        "step_id": "paper_collection",
        "label": "논문 수집",
        "description": "토픽 쿼리와 카테고리 기준으로 신규 논문 후보를 가져옵니다.",
    },
    {
        "step_id": "storage",
        "label": "저장",
        "description": "run-scoped 저장소에 raw, triage, 이벤트 로그를 기록합니다.",
    },
    {
        "step_id": "analysis",
        "label": "분석",
        "description": "랭킹, 리뷰 상태, 근거 후보, 연구 계획을 계산합니다.",
    },
    {
        "step_id": "report_generation",
        "label": "보고서 생성",
        "description": "주간/토픽 보고서 초안과 export 가능한 미리보기를 생성합니다.",
    },
]

TASK_TYPE_TO_ACTION = {
    "collect_papers": "collect",
    "collect_analyze": "analyze",
    "weekly_report": "report",
    "monthly_report": "report",
    "citation_alert": "analyze",
}

TASK_TYPE_TO_DEFAULT_TITLE = {
    "collect_papers": "{topic_short} 논문 수집",
    "collect_analyze": "{topic_short} 최신 논문 수집 및 분석",
    "weekly_report": "주간 트렌드 보고서 생성",
    "monthly_report": "월간 심층 분석 리포트",
    "citation_alert": "고인용 논문 알림",
}

DEFAULT_TASK_TEMPLATES = [
    {
        "task_type": "collect_analyze",
        "title": TASK_TYPE_TO_DEFAULT_TITLE["collect_analyze"],
        "cadence": "daily_0900",
    },
    {
        "task_type": "weekly_report",
        "title": TASK_TYPE_TO_DEFAULT_TITLE["weekly_report"],
        "cadence": "weekly_monday_1000",
    },
    {
        "task_type": "monthly_report",
        "title": TASK_TYPE_TO_DEFAULT_TITLE["monthly_report"],
        "cadence": "monthly_day_1_1000",
    },
    {
        "task_type": "citation_alert",
        "title": TASK_TYPE_TO_DEFAULT_TITLE["citation_alert"],
        "cadence": "daily_1800",
    },
]

WEEKDAY_LABELS = {
    "monday": "월요일",
    "tuesday": "화요일",
    "wednesday": "수요일",
    "thursday": "목요일",
    "friday": "금요일",
    "saturday": "토요일",
    "sunday": "일요일",
}
WEEKDAY_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
FALLBACK_TIMEZONES = {
    "UTC": timezone.utc,
    "Etc/UTC": timezone.utc,
    "Asia/Seoul": timezone(timedelta(hours=9)),
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _schedule_lock_path() -> Path:
    return SCHEDULE_LIBRARY_DIR / "schedules.lock"


def _schedule_guard_path() -> Path:
    # Permanent OS-lock inode: never unlink it, even when no owner is active.
    return SCHEDULE_LIBRARY_DIR / "schedules.guard"


def _current_lock_hostname() -> str:
    return socket.gethostname().strip().lower()


def _lock_hostname(payload: dict[str, Any]) -> str | None:
    raw = payload.get("hostname") or payload.get("host") or payload.get("owner_hostname")
    text = str(raw or "").strip().lower()
    return text or None


def _lock_same_host(payload: dict[str, Any]) -> bool | None:
    hostname = _lock_hostname(payload)
    if hostname is None:
        return None
    return hostname == _current_lock_hostname()


def _schedule_lock_owner_metadata() -> dict[str, Any]:
    timestamp = iso_now_utc()
    worker_id = (
        str(os.getenv("TOPIC_SCHEDULER_WORKER_ID") or os.getenv("SCHEDULER_WORKER_ID") or "").strip()
        or threading.current_thread().name
    )
    return {
        "lock_protocol": STORE_LOCK_PROTOCOL,
        "pid": os.getpid(),
        "process_id": os.getpid(),
        "hostname": socket.gethostname(),
        "worker_id": worker_id,
        "thread": threading.current_thread().name,
        "owner": "topic_schedule_store",
        "purpose": "schedule_storage_lock",
        "context": str(SCHEDULE_LIBRARY_PATH),
        "createdAt": timestamp,
        "updatedAt": timestamp,
        "heartbeatAt": timestamp,
        "acquired_at": timestamp,
    }


def _coerce_lock_pid(value: Any) -> int | None:
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _process_is_running(pid: int | None) -> bool | None:
    if pid is None:
        return None
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes
        except ImportError:
            return None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        open_process.restype = ctypes.c_void_p
        wait_for_single_object = kernel32.WaitForSingleObject
        wait_for_single_object.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        wait_for_single_object.restype = ctypes.c_uint32
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        synchronize = 0x00100000
        query_limited_information = 0x1000
        wait_object_0 = 0x00000000
        wait_timeout = 0x00000102
        error_invalid_parameter = 87
        error_access_denied = 5
        handle = open_process(synchronize | query_limited_information, False, pid)
        if not handle:
            error_code = ctypes.get_last_error()
            if error_code == error_invalid_parameter:
                return False
            if error_code == error_access_denied:
                return True
            return None
        try:
            state = wait_for_single_object(handle, 0)
            if state == wait_timeout:
                return True
            if state == wait_object_0:
                return False
            return None
        finally:
            close_handle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return True if exc.errno == errno.EPERM else False
    except ValueError:
        return False
    return True


def _read_lock_payload(lock_path: Path) -> tuple[dict[str, Any], bool]:
    try:
        raw = read_json(lock_path, default={})
        return (raw if isinstance(raw, dict) else {}), False
    except Exception:
        return {}, True


def _lock_age_seconds(lock_path: Path, payload: dict[str, Any], *, now: datetime | None = None) -> float | None:
    resolved_now = now or _utc_now()
    for field in ("heartbeatAt", "updatedAt", "createdAt", "heartbeat_at", "updated_at", "created_at", "acquired_at"):
        timestamp = _parse_iso_datetime(str(payload.get(field) or ""))
        if timestamp:
            return (resolved_now - timestamp).total_seconds()
    try:
        return time.time() - lock_path.stat().st_mtime
    except OSError:
        return None


def _recover_legacy_schedule_lock(lock_path: Path) -> bool:
    payload, malformed = _read_lock_payload(lock_path)
    age_seconds = _lock_age_seconds(lock_path, payload)
    pid = _coerce_lock_pid(payload.get("pid"))
    same_host = _lock_same_host(payload)
    owner_running = _process_is_running(pid) if same_host is not False else None
    recoverable = bool(
        owner_running is False
        or (age_seconds is not None and age_seconds > STORE_LOCK_STALE_SECONDS and owner_running is not True)
        or (malformed and age_seconds is not None and age_seconds > STORE_LOCK_STALE_SECONDS)
    )
    if not recoverable:
        return False
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _recover_stale_schedule_lock(lock_path: Path) -> bool:
    # Recovery and new acquisition share the guard, so cleanup cannot remove
    # another new-protocol owner's metadata after observing an unlocked guard.
    try:
        with file_lock(_schedule_guard_path(), timeout_seconds=0):
            payload, _ = _read_lock_payload(lock_path)
            if payload.get("lock_protocol") == STORE_LOCK_PROTOCOL:
                try:
                    lock_path.unlink(missing_ok=True)
                except OSError:
                    return False
                return True
            return _recover_legacy_schedule_lock(lock_path)
    except FileLockTimeout:
        return False


def _observed_lock_status(*, now: datetime | None, os_locked: bool) -> dict[str, Any]:
    lock_path = _schedule_lock_path()
    if not lock_path.exists():
        return {"locked": os_locked, "path": str(lock_path)}
    payload, malformed = _read_lock_payload(lock_path)
    if not os_locked and payload.get("lock_protocol") == STORE_LOCK_PROTOCOL:
        return {"locked": False, "path": str(lock_path)}
    age_seconds = _lock_age_seconds(lock_path, payload, now=now)
    pid = _coerce_lock_pid(payload.get("pid"))
    same_host = _lock_same_host(payload)
    owner_running = _process_is_running(pid) if same_host is not False else None
    return {
        "locked": True,
        "path": str(lock_path),
        "pid": pid if pid is not None else payload.get("pid"),
        "process_id": payload.get("process_id"),
        "hostname": payload.get("hostname"),
        "same_host": same_host,
        "worker_id": payload.get("worker_id"),
        "thread": payload.get("thread"),
        "owner": payload.get("owner"),
        "purpose": payload.get("purpose"),
        "context": payload.get("context"),
        "createdAt": payload.get("createdAt"),
        "updatedAt": payload.get("updatedAt"),
        "heartbeatAt": payload.get("heartbeatAt"),
        "acquired_at": payload.get("acquired_at"),
        "age_seconds": age_seconds,
        "malformed": malformed,
        "owner_running": owner_running,
        "stale": age_seconds is not None and age_seconds > STORE_LOCK_STALE_SECONDS,
    }


def _lock_file_status(*, now: datetime | None = None) -> dict[str, Any]:
    guard = _schedule_guard_path()
    # Read-only status on an untouched library must not create storage.
    if not guard.exists():
        return _observed_lock_status(now=now, os_locked=False)
    try:
        with file_lock(guard, timeout_seconds=0):
            return _observed_lock_status(now=now, os_locked=False)
    except FileLockTimeout:
        return _observed_lock_status(now=now, os_locked=True)


@contextmanager
def _schedule_store_lock():
    depth = getattr(_STORE_LOCK_STATE, "depth", 0)
    if depth > 0:
        _STORE_LOCK_STATE.depth = depth + 1
        try:
            yield
        finally:
            _STORE_LOCK_STATE.depth = depth
        return

    ensure_dir(SCHEDULE_LIBRARY_DIR)
    lock_path = _schedule_lock_path()
    started = time.monotonic()
    _STORE_LOCK_STATE.depth = 1
    try:
        with file_lock(_schedule_guard_path(), timeout_seconds=STORE_LOCK_TIMEOUT_SECONDS):
            marker_owned = False
            try:
                while True:
                    try:
                        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                        break
                    except FileExistsError:
                        payload, _ = _read_lock_payload(lock_path)
                        if payload.get("lock_protocol") == STORE_LOCK_PROTOCOL:
                            # The OS guard proves the previous owner has exited its
                            # critical section, even if a reader prevented unlink.
                            fd = os.open(str(lock_path), os.O_WRONLY | os.O_TRUNC)
                            break
                        if _recover_legacy_schedule_lock(lock_path):
                            continue
                        if time.monotonic() - started >= STORE_LOCK_TIMEOUT_SECONDS:
                            raise ScheduleStoreLockedError(SCHEDULE_STORE_LOCKED_MESSAGE)
                        time.sleep(0.05)
                marker_owned = True  # Also clean up if writing metadata fails.
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(_schedule_lock_owner_metadata(), handle, ensure_ascii=False)
                yield
            finally:
                if marker_owned:
                    try:
                        lock_path.unlink(missing_ok=True)
                    except OSError as error:
                        # Metadata is diagnostic, not ownership. The OS guard is
                        # released regardless; next acquisition can reuse it.
                        logger.warning("schedule_lock_metadata_cleanup_pending", extra={
                            "errno": error.errno, "winerror": getattr(error, "winerror", None),
                        })
    except FileLockTimeout as error:
        raise ScheduleStoreLockedError(SCHEDULE_STORE_LOCKED_MESSAGE) from error
    finally:
        _STORE_LOCK_STATE.depth = 0


def _parse_iso_datetime(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _timezone_from_name(name: str | None):
    resolved_name = str(name or DEFAULT_SCHEDULE_TIMEZONE)
    try:
        return ZoneInfo(resolved_name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        if resolved_name in FALLBACK_TIMEZONES:
            return FALLBACK_TIMEZONES[resolved_name]
        raise ScheduleTimezoneUnavailable(
            f"현재 환경에서 예약 시간대 '{resolved_name}'를 사용할 수 없습니다. "
            "시간대 데이터와 저장된 설정을 확인하세요. UTC로 대체 실행하지 않습니다."
        ) from exc


def _schedule_timezone_error(schedule: dict[str, Any]) -> str | None:
    try:
        _timezone_from_name(schedule.get("timezone"))
    except ScheduleTimezoneUnavailable as exc:
        return str(exc)
    return None


def _schedule_view(schedule: dict[str, Any]) -> dict[str, Any]:
    # Availability is an environment-dependent projection, not a research-run
    # failure or a migration of the saved clock/history.
    view = dict(schedule)
    view.pop("timezone_error", None)
    error = _schedule_timezone_error(schedule)
    if error:
        view["timezone_error"] = error
        view["next_run_hint"] = None
        if not schedule.get("current_run_id"):
            view["scheduler_state"] = "blocked"
    return view


def _next_run_hint(next_run_at: str | None, timezone_name: str | None) -> str | None:
    if not next_run_at:
        return None
    parsed = _parse_iso_datetime(next_run_at)
    if parsed is None:
        return None
    try:
        localized = parsed.astimezone(_timezone_from_name(timezone_name))
    except ScheduleTimezoneUnavailable:
        return None
    return f"다음 실행 {localized.strftime('%m/%d %H:%M')}"


def _topic_short_name(profile: dict[str, Any]) -> str:
    title = str(profile.get("title") or profile.get("topic") or profile.get("profile_id") or "토픽").strip()
    if title == "Large Language Models":
        return "LLM"
    return title


def _validate_task_type(task_type: str) -> str:
    normalized = str(task_type or "").strip()
    if normalized not in TASK_TYPE_TO_ACTION:
        raise RuntimeError(f"지원하지 않는 task_type 입니다: {normalized or '(empty)'}")
    return normalized


def _parse_cadence(cadence: str) -> dict[str, Any]:
    normalized = str(cadence or "").strip().lower()
    if not normalized:
        raise RuntimeError("cadence 값이 필요합니다.")

    if normalized.startswith("daily_") and len(normalized) == len("daily_0000"):
        time_part = normalized.split("_", 1)[1]
        hour = int(time_part[:2])
        minute = int(time_part[2:])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise RuntimeError(f"잘못된 daily cadence 입니다: {cadence}")
        return {"kind": "daily", "hour": hour, "minute": minute}

    if normalized.startswith("weekly_"):
        parts = normalized.split("_")
        if len(parts) == 3 and parts[1] in WEEKDAY_INDEX and len(parts[2]) == 4:
            hour = int(parts[2][:2])
            minute = int(parts[2][2:])
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise RuntimeError(f"잘못된 weekly cadence 입니다: {cadence}")
            return {"kind": "weekly", "weekday": parts[1], "hour": hour, "minute": minute}

    if normalized.startswith("monthly_day_"):
        parts = normalized.split("_")
        if len(parts) == 4 and parts[0] == "monthly" and parts[1] == "day" and len(parts[3]) == 4:
            day = int(parts[2])
            hour = int(parts[3][:2])
            minute = int(parts[3][2:])
            if not (1 <= day <= 31 and 0 <= hour <= 23 and 0 <= minute <= 59):
                raise RuntimeError(f"잘못된 monthly cadence 입니다: {cadence}")
            return {"kind": "monthly", "day": day, "hour": hour, "minute": minute}

    raise RuntimeError(f"지원하지 않는 cadence 형식입니다: {cadence}")


def cadence_label(cadence: str) -> str:
    parsed = _parse_cadence(cadence)
    hour = int(parsed["hour"])
    minute = int(parsed["minute"])
    time_label = f"{hour:02d}:{minute:02d}"
    if parsed["kind"] == "daily":
        return f"매일 {time_label}"
    if parsed["kind"] == "weekly":
        return f"매주 {WEEKDAY_LABELS[parsed['weekday']]} {time_label}"
    return f"매월 {int(parsed['day'])}일 {time_label}"


def _monthly_candidate(year: int, month: int, day: int, hour: int, minute: int, tzinfo: ZoneInfo) -> datetime:
    last_day = calendar.monthrange(year, month)[1]
    safe_day = min(max(day, 1), last_day)
    return datetime(year, month, safe_day, hour, minute, tzinfo=tzinfo)


def compute_next_run_at(
    cadence: str,
    *,
    now: datetime | None = None,
    reference: datetime | None = None,
    timezone_name: str | None = None,
    inclusive: bool = False,
) -> str:
    parsed = _parse_cadence(cadence)
    resolved_now = (now or _utc_now()).astimezone(_timezone_from_name(timezone_name))
    base = (reference or resolved_now).astimezone(_timezone_from_name(timezone_name))
    compare_op = (lambda candidate, point: candidate < point) if inclusive else (lambda candidate, point: candidate <= point)

    if parsed["kind"] == "daily":
        candidate = base.replace(hour=int(parsed["hour"]), minute=int(parsed["minute"]), second=0, microsecond=0)
        while compare_op(candidate, base):
            candidate += timedelta(days=1)
        return candidate.astimezone(timezone.utc).isoformat()

    if parsed["kind"] == "weekly":
        target_weekday = WEEKDAY_INDEX[parsed["weekday"]]
        delta_days = (target_weekday - base.weekday()) % 7
        candidate = (base + timedelta(days=delta_days)).replace(
            hour=int(parsed["hour"]),
            minute=int(parsed["minute"]),
            second=0,
            microsecond=0,
        )
        while compare_op(candidate, base):
            candidate += timedelta(days=7)
        return candidate.astimezone(timezone.utc).isoformat()

    candidate = _monthly_candidate(base.year, base.month, int(parsed["day"]), int(parsed["hour"]), int(parsed["minute"]), base.tzinfo)
    while compare_op(candidate, base):
        year = candidate.year + (1 if candidate.month == 12 else 0)
        month = 1 if candidate.month == 12 else candidate.month + 1
        candidate = _monthly_candidate(year, month, int(parsed["day"]), int(parsed["hour"]), int(parsed["minute"]), base.tzinfo)
    return candidate.astimezone(timezone.utc).isoformat()


def _load_raw_schedules() -> list[dict[str, Any]]:
    # Reading must not create the library directory: status/readiness probes
    # reach this path. Writers (_save_schedules) still ensure_dir before writing.
    schedules = read_json(SCHEDULE_LIBRARY_PATH, default=[])
    if not isinstance(schedules, list):
        return []
    return [item for item in schedules if isinstance(item, dict)]


def _save_schedules(schedules: list[dict[str, Any]]) -> None:
    ensure_dir(SCHEDULE_LIBRARY_DIR)
    write_json(SCHEDULE_LIBRARY_PATH, schedules)
    metadata_index.touch_index_state("last_topic_schedules_updated_at")


def _template_for_task_type(task_type: str) -> dict[str, Any] | None:
    return next((template for template in DEFAULT_TASK_TEMPLATES if template["task_type"] == task_type), None)


def _default_schedule(profile: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
    now = iso_now_utc()
    profile_id = str(profile.get("profile_id", "")).strip()
    task_type = str(template["task_type"])
    title_template = str(template["title"])
    cadence = str(template["cadence"])
    profile_is_template = topic_profiles._profile_resource_kind(profile) == ResourceKind.SYSTEM_TEMPLATE
    return {
        "schedule_id": f"schedule-{profile_id}-{task_type}",
        "profile_id": profile_id,
        "title": title_template.format(topic_short=_topic_short_name(profile)),
        "topic": str(profile.get("topic", "")).strip(),
        "task_type": task_type,
        "kind": "default",
        "resource_kind": (
            ResourceKind.SYSTEM_TEMPLATE.value
            if profile_is_template
            else ResourceKind.INHERITED_OWNER.value
        ),
        "enabled": False,
        "cadence": cadence,
        "cadence_label": cadence_label(cadence),
        "timezone": DEFAULT_SCHEDULE_TIMEZONE,
        "pipeline": DEFAULT_PIPELINE,
        "last_run_id": None,
        "last_status": None,
        "last_run_at": None,
        "last_success_at": None,
        "last_failure_at": None,
        "last_failure_message": None,
        "last_scheduled_at": None,
        "next_run_at": None,
        "next_run_hint": None,
        "current_run_id": None,
        "current_run_status": None,
        "scheduler_state": "paused",
        "last_error": None,
        "last_trigger_source": None,
        "last_worker_id": None,
        "dispatch_attempts": 0,
        "lock_token": None,
        "lock_owner": None,
        "lock_acquired_at": None,
        "lock_expires_at": None,
        "last_recovery_at": None,
        "last_recovery_message": None,
        "owner_user_id": None if profile_is_template else profile.get("owner_user_id"),
        "deleted": False,
        "deleted_at": None,
        "created_at": now,
        "updated_at": now,
    }


def _base_custom_schedule(profile: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    now = iso_now_utc()
    task_type = _validate_task_type(str(raw.get("task_type") or "collect_analyze"))
    cadence = str(raw.get("cadence") or "daily_0900")
    title_template = TASK_TYPE_TO_DEFAULT_TITLE.get(task_type, "{topic_short} 예약 작업")
    profile_is_template = topic_profiles._profile_resource_kind(profile) == ResourceKind.SYSTEM_TEMPLATE
    return {
        "schedule_id": str(raw.get("schedule_id") or f"schedule-{str(profile.get('profile_id') or '').strip()}-{slugify(str(raw.get('title') or title_template)) or 'custom'}").strip(),
        "profile_id": str(profile.get("profile_id", "")).strip(),
        "title": str(raw.get("title") or title_template.format(topic_short=_topic_short_name(profile))).strip(),
        "topic": str(profile.get("topic", "")).strip(),
        "task_type": task_type,
        "kind": str(raw.get("kind") or "custom"),
        "resource_kind": str(
            raw.get("resource_kind")
            or (
                ResourceKind.USER_OWNED.value
                if profile_is_template
                else ResourceKind.INHERITED_OWNER.value
            )
        ),
        "enabled": bool(raw.get("enabled", False)),
        "cadence": cadence,
        "cadence_label": cadence_label(cadence),
        "timezone": str(raw.get("timezone") or DEFAULT_SCHEDULE_TIMEZONE),
        "pipeline": [step for step in raw.get("pipeline", []) if isinstance(step, dict)] or DEFAULT_PIPELINE,
        "last_run_id": raw.get("last_run_id"),
        "last_status": raw.get("last_status"),
        "last_run_at": raw.get("last_run_at"),
        "last_success_at": raw.get("last_success_at"),
        "last_failure_at": raw.get("last_failure_at"),
        "last_failure_message": raw.get("last_failure_message"),
        "last_scheduled_at": raw.get("last_scheduled_at"),
        "next_run_at": raw.get("next_run_at"),
        "next_run_hint": raw.get("next_run_hint"),
        "current_run_id": raw.get("current_run_id"),
        "current_run_status": raw.get("current_run_status"),
        "scheduler_state": str(raw.get("scheduler_state") or "paused"),
        "last_error": raw.get("last_error"),
        "last_trigger_source": raw.get("last_trigger_source"),
        "last_worker_id": raw.get("last_worker_id"),
        "dispatch_attempts": int(raw.get("dispatch_attempts") or 0),
        "lock_token": raw.get("lock_token"),
        "lock_owner": raw.get("lock_owner"),
        "lock_acquired_at": raw.get("lock_acquired_at"),
        "lock_expires_at": raw.get("lock_expires_at"),
        "last_recovery_at": raw.get("last_recovery_at"),
        "last_recovery_message": raw.get("last_recovery_message"),
        "owner_user_id": raw.get("owner_user_id"),
        "deleted": bool(raw.get("deleted", False)),
        "deleted_at": raw.get("deleted_at"),
        "created_at": str(raw.get("created_at") or now),
        "updated_at": str(raw.get("updated_at") or now),
    }


def _normalize_schedule(raw: dict[str, Any], profile: dict[str, Any], template: dict[str, Any] | None = None) -> dict[str, Any]:
    base = _default_schedule(profile, template) if template else _base_custom_schedule(profile, raw)
    passthrough_fields = (
        "schedule_id",
        "title",
        "enabled",
        "cadence",
        "task_type",
        "kind",
        "resource_kind",
        "timezone",
        "pipeline",
        "last_run_id",
        "last_status",
        "last_run_at",
        "last_success_at",
        "last_failure_at",
        "last_failure_message",
        "last_scheduled_at",
        "next_run_at",
        "current_run_id",
        "current_run_status",
        "scheduler_state",
        "last_error",
        "last_trigger_source",
        "last_worker_id",
        "dispatch_attempts",
        "lock_token",
        "lock_owner",
        "lock_acquired_at",
        "lock_expires_at",
        "last_recovery_at",
        "last_recovery_message",
        "owner_user_id",
        "deleted",
        "deleted_at",
        "created_at",
        "updated_at",
    )
    for field in passthrough_fields:
        if field in raw and raw[field] is not None:
            base[field] = raw[field]

    # A persisted legacy record without an owner remains unowned. Only a newly
    # materialized default for an already-owned profile inherits that owner.
    if raw and not str(raw.get("owner_user_id") or "").strip():
        base["owner_user_id"] = None

    base["profile_id"] = str(profile.get("profile_id", "")).strip()
    base["topic"] = str(profile.get("topic", "")).strip()
    default_task_type = str(template.get("task_type")) if template else str(base.get("task_type") or "")
    base["task_type"] = _validate_task_type(str(base.get("task_type") or default_task_type))
    _parse_cadence(str(base.get("cadence") or ""))
    base["cadence"] = str(base["cadence"]).strip().lower()
    base["cadence_label"] = cadence_label(base["cadence"])
    base["title"] = str(base.get("title") or "").strip() or TASK_TYPE_TO_DEFAULT_TITLE[base["task_type"]].format(
        topic_short=_topic_short_name(profile)
    )
    base["enabled"] = bool(base.get("enabled", False))
    base["deleted"] = bool(base.get("deleted", False))
    base["timezone"] = str(base.get("timezone") or DEFAULT_SCHEDULE_TIMEZONE)
    base["pipeline"] = [step for step in base.get("pipeline", []) if isinstance(step, dict)] or DEFAULT_PIPELINE
    # Materialization may persist normalization before a request is rejected.
    # Preserve the old display hint on disk when its clock is unavailable;
    # response projections suppress it without rewriting the source record.
    base["next_run_hint"] = (
        raw.get("next_run_hint") if _schedule_timezone_error(base)
        else _next_run_hint(base.get("next_run_at"), base.get("timezone"))
    )
    return base


def _visible_schedule(schedule: dict[str, Any]) -> bool:
    return not bool(schedule.get("deleted", False))


def _materialize_raw_schedules(*, persist: bool = True) -> list[dict[str, Any]]:
    # This is a private storage materializer used by the scheduler worker. It
    # must retain every profile; request visibility is applied only after the
    # complete store has been loaded.
    profiles = topic_profiles._load_profiles()
    raw_schedules = _load_raw_schedules()
    raw_by_schedule_id = {
        str(item.get("schedule_id", "")).strip(): item
        for item in raw_schedules
        if str(item.get("schedule_id", "")).strip()
    }
    normalized: list[dict[str, Any]] = []
    known_schedule_ids: set[str] = set()

    for profile in profiles:
        profile_id = str(profile.get("profile_id", "")).strip()
        for template in DEFAULT_TASK_TEMPLATES:
            schedule_id = f"schedule-{profile_id}-{template['task_type']}"
            raw = raw_by_schedule_id.get(schedule_id, {})
            normalized_schedule = _normalize_schedule(raw, profile, template)
            known_schedule_ids.add(schedule_id)
            normalized.append(normalized_schedule)

        for raw in raw_schedules:
            if str(raw.get("profile_id", "")).strip() != profile_id:
                continue
            if bool(raw.get("deleted", False)):
                continue
            schedule_id = str(raw.get("schedule_id", "")).strip()
            if not schedule_id or schedule_id in known_schedule_ids:
                continue
            normalized.append(_normalize_schedule(raw, profile, None))
            known_schedule_ids.add(schedule_id)

    deleted_or_unknown_by_id: dict[str, dict[str, Any]] = {}
    known_profile_ids = {str(profile.get("profile_id", "")).strip() for profile in profiles}
    for item in raw_schedules:
        schedule_id = str(item.get("schedule_id", "")).strip()
        if not schedule_id:
            continue
        if item.get("deleted") or str(item.get("profile_id", "")).strip() not in known_profile_ids:
            deleted_or_unknown_by_id[schedule_id] = item
    next_raw = normalized + list(deleted_or_unknown_by_id.values())
    if persist and next_raw != raw_schedules:
        _save_schedules(next_raw)
    return next_raw


def _is_current_run_in_progress(schedule: dict[str, Any], *, recover_orphans: bool) -> tuple[bool, bool]:
    run_id = str(schedule.get("current_run_id") or "").strip()
    if not run_id:
        return False, False
    try:
        run = load_run_record(run_id)
    except FileNotFoundError:
        schedule["current_run_id"] = None
        schedule["current_run_status"] = None
        schedule["scheduler_state"] = "active" if schedule.get("enabled") else "paused"
        return False, True

    status = str(run.get("status") or "unknown")
    schedule["current_run_status"] = status
    if status not in {"queued", "running"}:
        completed_at = run.get("finished_at") or run.get("updated_at") or iso_now_utc()
        schedule["current_run_id"] = None
        schedule["current_run_status"] = None
        schedule["last_status"] = status
        schedule["last_run_at"] = completed_at
        schedule["last_error"] = run.get("error")
        if status == "completed":
            schedule["last_success_at"] = completed_at
            schedule["last_error"] = None
        if status == "failed":
            schedule["last_failure_at"] = completed_at
            schedule["last_failure_message"] = run.get("error")
        if schedule.get("enabled"):
            schedule["scheduler_state"] = "failed" if status == "failed" else "active"
        else:
            schedule["scheduler_state"] = "paused"
        return False, True

    if recover_orphans:
        # Durable-executor source of truth: a run is orphaned only when no
        # active job remains for it. A job that is still queued/leased/running
        # survives a process restart and must not be failed here.
        from app.repositories import run_jobs

        if run_jobs.active_job_for_run(run_id) is None:
            run["status"] = "failed"
            run["finished_at"] = iso_now_utc()
            run["error"] = "프로세스 재시작 또는 worker 중단으로 예약 작업 실행이 중단되었습니다."
            save_run_record(run)
            schedule["current_run_id"] = None
            schedule["current_run_status"] = None
            schedule["last_status"] = "failed"
            schedule["last_run_at"] = run["finished_at"]
            schedule["last_failure_at"] = run["finished_at"]
            schedule["last_failure_message"] = run["error"]
            schedule["last_error"] = run["error"]
            schedule["scheduler_state"] = "failed" if schedule.get("enabled") else "paused"
            topic_notifications.emit_schedule_failure_notification(
                schedule,
                error=str(run["error"]),
                run_id=run_id,
            )
            return False, True

    schedule["scheduler_state"] = "running"
    return True, False


def _ensure_next_run(schedule: dict[str, Any], *, now: datetime) -> bool:
    if _schedule_timezone_error(schedule):
        # Keep saved timestamps and run/lease history intact. Callers must not
        # treat a precomputed next_run_at as dispatchable without its clock.
        return False
    if not schedule.get("enabled"):
        schedule["next_run_at"] = None
        schedule["next_run_hint"] = None
        if not schedule.get("current_run_id"):
            schedule["scheduler_state"] = (
                "blocked"
                if get_auth_settings().auth_enabled
                and schedule.get("last_error") == "schedule_owner_missing"
                else "paused"
            )
        return False

    next_run = _parse_iso_datetime(schedule.get("next_run_at"))
    if next_run is None:
        reference = _parse_iso_datetime(schedule.get("last_scheduled_at"))
        schedule["next_run_at"] = compute_next_run_at(
            str(schedule.get("cadence") or ""),
            now=now,
            reference=reference,
            timezone_name=str(schedule.get("timezone") or DEFAULT_SCHEDULE_TIMEZONE),
            inclusive=reference is None,
        )
    schedule["next_run_hint"] = _next_run_hint(schedule.get("next_run_at"), schedule.get("timezone"))
    if not schedule.get("current_run_id"):
        schedule["scheduler_state"] = "failed" if schedule.get("last_status") == "failed" else "active"
    return True


def _sync_runtime_schedules(raw_schedules: list[dict[str, Any]], *, now: datetime | None = None, recover_orphans: bool = False) -> tuple[list[dict[str, Any]], list[str]]:
    resolved_now = now or _utc_now()
    due_schedule_ids: list[str] = []

    for schedule in raw_schedules:
        if not _visible_schedule(schedule):
            continue
        _recover_expired_dispatch_lease(schedule, now=resolved_now)
        has_active_run, _changed = _is_current_run_in_progress(schedule, recover_orphans=recover_orphans)
        if not _ensure_next_run(schedule, now=resolved_now):
            continue
        next_run = _parse_iso_datetime(schedule.get("next_run_at"))
        if next_run is not None and not has_active_run and next_run <= resolved_now:
            due_schedule_ids.append(str(schedule.get("schedule_id") or ""))

    return raw_schedules, [schedule_id for schedule_id in due_schedule_ids if schedule_id]


def _find_schedule(raw_schedules: list[dict[str, Any]], schedule_id: str) -> dict[str, Any]:
    for schedule in raw_schedules:
        if str(schedule.get("schedule_id") or "").strip() == schedule_id and _visible_schedule(schedule):
            return schedule
    raise FileNotFoundError(f"Topic schedule not found: {schedule_id}")


def _lease_active(schedule: dict[str, Any], *, now: datetime | None = None) -> bool:
    token = str(schedule.get("lock_token") or "").strip()
    if not token:
        return False
    expires_at = _parse_iso_datetime(schedule.get("lock_expires_at"))
    if expires_at is None:
        return False
    return expires_at > (now or _utc_now())


def _lease_expired(schedule: dict[str, Any], *, now: datetime | None = None) -> bool:
    token = str(schedule.get("lock_token") or "").strip()
    if not token:
        return False
    expires_at = _parse_iso_datetime(schedule.get("lock_expires_at"))
    if expires_at is None:
        return True
    return expires_at <= (now or _utc_now())


def _clear_dispatch_lease(schedule: dict[str, Any]) -> None:
    schedule["lock_token"] = None
    schedule["lock_owner"] = None
    schedule["lock_acquired_at"] = None
    schedule["lock_expires_at"] = None


def _recover_expired_dispatch_lease(schedule: dict[str, Any], *, now: datetime) -> bool:
    if not _lease_expired(schedule, now=now):
        return False
    owner = str(schedule.get("lock_owner") or "unknown worker").strip()
    _clear_dispatch_lease(schedule)
    schedule["last_recovery_at"] = now.isoformat()
    schedule["last_recovery_message"] = f"만료된 dispatch lease를 회수했습니다: {owner}"
    if schedule.get("enabled") and not schedule.get("current_run_id"):
        schedule["scheduler_state"] = "active"
    return True


def _acquire_dispatch_lease(schedule: dict[str, Any], *, worker_id: str | None = None, now: datetime | None = None) -> str:
    resolved_now = now or _utc_now()
    if _lease_active(schedule, now=resolved_now):
        raise RuntimeError("이미 다른 scheduler worker가 예약 작업 dispatch lock을 보유하고 있습니다.")
    token = f"lease-{slugify(str(schedule.get('schedule_id') or 'schedule'))}-{uuid.uuid4().hex[:12]}"
    owner = str(worker_id or f"pid-{os.getpid()}").strip()
    schedule["lock_token"] = token
    schedule["lock_owner"] = owner
    schedule["lock_acquired_at"] = resolved_now.isoformat()
    schedule["lock_expires_at"] = (resolved_now + timedelta(seconds=DISPATCH_LEASE_SECONDS)).isoformat()
    schedule["last_worker_id"] = owner
    schedule["dispatch_attempts"] = int(schedule.get("dispatch_attempts") or 0) + 1
    return token


def _schedule_owner_required_for_auto_run(schedule: dict[str, Any]) -> bool:
    return bool(get_auth_settings().auth_enabled)


def _task_title(schedule_or_payload: dict[str, Any], profile: dict[str, Any]) -> str:
    task_type = _validate_task_type(str(schedule_or_payload.get("task_type") or "collect_analyze"))
    requested = str(schedule_or_payload.get("title") or "").strip()
    if requested:
        return requested
    return TASK_TYPE_TO_DEFAULT_TITLE[task_type].format(topic_short=_topic_short_name(profile))


def _build_schedule_create_payload(
    payload: dict[str, Any],
    profile: dict[str, Any],
    *,
    owner_user_id: str | None,
    existing_schedules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    now = iso_now_utc()
    task_type = _validate_task_type(str(payload.get("task_type") or "collect_analyze"))
    cadence = str(payload.get("cadence") or "").strip().lower()
    _parse_cadence(cadence)
    enabled = bool(payload.get("enabled", False))
    base_id = slugify(f"{profile.get('profile_id')}-{task_type}-{payload.get('title') or cadence}") or "schedule"
    schedule_id = f"schedule-{base_id}"
    source_schedules = existing_schedules if existing_schedules is not None else _materialize_raw_schedules()
    existing_ids = {str(item.get("schedule_id", "")).strip() for item in source_schedules if str(item.get("schedule_id", "")).strip()}
    suffix = 2
    while schedule_id in existing_ids:
        schedule_id = f"schedule-{base_id}-{suffix}"
        suffix += 1

    next_run_at = (
        compute_next_run_at(cadence, now=_utc_now(), timezone_name=DEFAULT_SCHEDULE_TIMEZONE, inclusive=True)
        if enabled
        else None
    )
    profile_is_template = topic_profiles._profile_resource_kind(profile) == ResourceKind.SYSTEM_TEMPLATE
    return {
        "schedule_id": schedule_id,
        "profile_id": str(profile.get("profile_id") or "").strip(),
        "title": _task_title(payload, profile),
        "topic": str(profile.get("topic") or "").strip(),
        "task_type": task_type,
        "kind": "custom",
        "resource_kind": (
            ResourceKind.USER_OWNED.value
            if profile_is_template
            else ResourceKind.INHERITED_OWNER.value
        ),
        "enabled": enabled,
        "cadence": cadence,
        "cadence_label": cadence_label(cadence),
        "timezone": DEFAULT_SCHEDULE_TIMEZONE,
        "pipeline": DEFAULT_PIPELINE,
        "last_run_id": None,
        "last_status": None,
        "last_run_at": None,
        "last_success_at": None,
        "last_failure_at": None,
        "last_failure_message": None,
        "last_scheduled_at": None,
        "next_run_at": next_run_at,
        "next_run_hint": _next_run_hint(next_run_at, DEFAULT_SCHEDULE_TIMEZONE),
        "current_run_id": None,
        "current_run_status": None,
        "scheduler_state": "active" if enabled else "paused",
        "last_error": None,
        "last_trigger_source": None,
        "last_worker_id": None,
        "dispatch_attempts": 0,
        "lock_token": None,
        "lock_owner": None,
        "lock_acquired_at": None,
        "lock_expires_at": None,
        "last_recovery_at": None,
        "last_recovery_message": None,
        "owner_user_id": owner_user_id,
        "deleted": False,
        "deleted_at": None,
        "created_at": now,
        "updated_at": now,
    }


def _schedule_run_payload(profile: dict[str, Any], task_type: str) -> dict[str, Any]:
    payload = topic_profiles.to_run_request_payload(profile)
    action = action_for_task_type(task_type)
    if action == "collect":
        payload["skip_summary"] = True
        payload["skip_synthesis"] = True
        return payload
    if action == "analyze":
        payload["skip_synthesis"] = True
        return payload
    return payload


def _schedule_resource_kind(
    schedule: dict[str, Any],
    profile: dict[str, Any],
) -> ResourceKind:
    profile_is_template = topic_profiles._profile_resource_kind(profile) == ResourceKind.SYSTEM_TEMPLATE
    task_type = str(schedule.get("task_type") or "").strip()
    expected_template_id = f"schedule-{str(profile.get('profile_id') or '').strip()}-{task_type}"
    if (
        profile_is_template
        and str(schedule.get("kind") or "") == "default"
        and str(schedule.get("schedule_id") or "").strip() == expected_template_id
    ):
        return ResourceKind.SYSTEM_TEMPLATE
    if profile_is_template:
        return ResourceKind.USER_OWNED
    return ResourceKind.INHERITED_OWNER


def authorize_topic_schedule(
    schedule: dict[str, Any],
    profile: dict[str, Any],
    *,
    actor: ResourceActor | None,
    action: ResourceAction | str,
) -> ResourceActor:
    resolved_actor = topic_profiles.authorize_topic_profile(
        profile,
        actor=actor,
        action=ResourceAction.READ,
    )
    resource_kind = _schedule_resource_kind(schedule, profile)
    owner_user_id = str(schedule.get("owner_user_id") or "").strip() or None
    if (
        resolved_actor.auth_mode != "disabled"
        and resource_kind == ResourceKind.INHERITED_OWNER
    ):
        profile_owner = str(profile.get("owner_user_id") or "").strip() or None
        if not owner_user_id or owner_user_id != profile_owner:
            raise ResourceAccessDenied()
    if (
        resolved_actor.auth_mode != "disabled"
        and resource_kind == ResourceKind.SYSTEM_TEMPLATE
        and owner_user_id
    ):
        raise ResourceAccessDenied()
    return authorize_resource_access(
        actor=resolved_actor,
        resource_kind=resource_kind,
        action=action,
        owner_user_id=owner_user_id,
    )


def _load_task_definitions_read_only(backend: SchedulerBackend) -> list[dict[str, Any]]:
    if backend.mode in {SCHEDULER_RUNTIME_MODE, "legacy_file_read_only"}:
        lock_status = _lock_file_status()
        if lock_status.get("locked"):
            raise ScheduleStoreLockedError(SCHEDULE_STORE_LOCKED_MESSAGE)
        return _materialize_raw_schedules(persist=False)
    if isinstance(backend, DbSchedulerBackend):
        return backend._load_db_schedules_read_only()
    return backend.load_task_definitions()


def _visible_schedules_for_actor(
    schedules: list[dict[str, Any]],
    *,
    actor: ResourceActor,
) -> list[dict[str, Any]]:
    profiles = {
        str(profile.get("profile_id") or "").strip(): profile
        for profile in topic_profiles._load_profiles()
    }
    visible: list[dict[str, Any]] = []
    for schedule in schedules:
        if not _visible_schedule(schedule):
            continue
        profile = profiles.get(str(schedule.get("profile_id") or "").strip())
        if profile is None:
            continue
        try:
            authorize_topic_schedule(
                schedule,
                profile,
                actor=actor,
                action=ResourceAction.LIST,
            )
        except ResourceAccessDenied:
            continue
        visible.append(schedule)
    return visible


def list_topic_schedules(*, actor: ResourceActor | None = None) -> dict[str, Any]:
    resolved_actor = resolve_resource_actor(actor)
    backend = get_scheduler_backend()
    if resolved_actor.auth_mode == "disabled":
        with SCHEDULE_LOCK, _schedule_store_lock():
            raw_schedules = backend.load_task_definitions()
            # Loading already persists normalization/import; sync mutates nested records.
            loaded_schedules = deepcopy(raw_schedules)
            raw_schedules, _ = backend.query_due_task_ids(
                raw_schedules,
                recover_orphans=False,
            )
            if not backend.read_only and raw_schedules != loaded_schedules:
                backend.save_task_definitions(raw_schedules)
            return {
                "items": [
                    _schedule_view(schedule)
                    for schedule in raw_schedules
                    if _visible_schedule(schedule)
                ],
                "backend": backend.descriptor(),
            }
    raw_schedules = _load_task_definitions_read_only(backend)
    return {
        "items": [_schedule_view(schedule) for schedule in _visible_schedules_for_actor(raw_schedules, actor=resolved_actor)],
        "backend": backend.descriptor(),
    }


def list_topic_schedules_safe(*, actor: ResourceActor | None = None) -> dict[str, Any]:
    try:
        response = list_topic_schedules(actor=actor)
        response.setdefault("degraded", False)
        response.setdefault("read_only", False)
        response.setdefault("warnings", [])
        return response
    except ScheduleStoreLockedError as exc:
        backend = get_scheduler_backend()
        lock_status = _lock_file_status()
        return {
            "items": [],
            "backend": backend.descriptor(),
            "degraded": True,
            "read_only": True,
            "warnings": [
                "예약 작업 저장소가 잠겨 있어 예약 작업 목록을 일시적으로 표시할 수 없습니다.",
                str(exc),
            ],
            "lock_error": str(exc),
            "store_lock": lock_status,
        }


def schedule_store_lock_preflight(*, recover_stale: bool = False) -> dict[str, Any]:
    before = _lock_file_status()
    cleanup_attempted = False
    cleanup_result = "not_needed"
    cleanup_error: str | None = None

    if before.get("locked") and recover_stale:
        cleanup_attempted = True
        try:
            cleanup_result = (
                "cleared" if _recover_stale_schedule_lock(_schedule_lock_path()) else "not_safe_or_not_stale"
            )
        except Exception as exc:  # pragma: no cover - defensive operator status path
            cleanup_result = "failed"
            cleanup_error = str(exc)

    after = _lock_file_status()
    locked = bool(after.get("locked"))
    owner_running = after.get("owner_running")
    stale = bool(after.get("stale"))
    malformed = bool(after.get("malformed"))
    store_status = "available"
    ok = True
    read_degraded = False
    write_blocked = False

    if locked:
        ok = False
        read_degraded = True
        write_blocked = True
        if owner_running is True:
            store_status = "locked_live_owner"
        elif stale or malformed:
            store_status = "locked_unrecovered_stale"
        else:
            store_status = "locked_wait_required"

    return {
        "ok": ok,
        "store_status": store_status,
        "lock_present": locked,
        "read_degraded": read_degraded,
        "write_blocked": write_blocked,
        "cleanup_requested": recover_stale,
        "cleanup_attempted": cleanup_attempted,
        "cleanup_result": cleanup_result,
        "cleanup_error": cleanup_error,
        "before": before,
        "after": after,
    }


def get_topic_schedule(
    schedule_id: str,
    *,
    actor: ResourceActor | None = None,
) -> dict[str, Any]:
    resolved_actor = resolve_resource_actor(actor)
    backend = get_scheduler_backend()
    if resolved_actor.auth_mode == "disabled":
        with SCHEDULE_LOCK, _schedule_store_lock():
            raw_schedules = backend.load_task_definitions()
            loaded_schedules = deepcopy(raw_schedules)
            raw_schedules, _ = backend.query_due_task_ids(
                raw_schedules,
                recover_orphans=False,
            )
            if not backend.read_only and raw_schedules != loaded_schedules:
                backend.save_task_definitions(raw_schedules)
            return _schedule_view(_find_schedule(raw_schedules, schedule_id))
    raw_schedules = _load_task_definitions_read_only(backend)
    schedule = _find_schedule(raw_schedules, schedule_id)
    profiles = {
        str(profile.get("profile_id") or "").strip(): profile
        for profile in topic_profiles._load_profiles()
    }
    profile = profiles.get(str(schedule.get("profile_id") or "").strip())
    if profile is None:
        raise ResourceAccessDenied()
    authorize_topic_schedule(
        schedule,
        profile,
        actor=resolved_actor,
        action=ResourceAction.READ,
    )
    return _schedule_view(schedule)


def create_topic_schedule(
    payload: dict[str, Any],
    *,
    actor: ResourceActor | None = None,
) -> dict[str, Any]:
    reject_client_authority_fields(payload)
    resolved_actor = resolve_resource_actor(actor)
    with SCHEDULE_LOCK, _schedule_store_lock():
        backend = get_scheduler_backend()
        if backend.read_only:
            raise RuntimeError("Configured scheduler backend is unavailable; read-only legacy fallback cannot mutate schedules.")
        profile_id = str(payload.get("profile_id") or "").strip()
        if not profile_id:
            raise RuntimeError("profile_id 값이 필요합니다.")
        profile = topic_profiles.get_topic_profile(profile_id, actor=resolved_actor)
        owner_user_id = owner_user_id_for_actor(resolved_actor)
        raw_schedules = backend.load_task_definitions()
        created = _build_schedule_create_payload(
            payload,
            profile,
            owner_user_id=owner_user_id,
            existing_schedules=raw_schedules,
        )
        authorize_topic_schedule(
            created,
            profile,
            actor=resolved_actor,
            action=ResourceAction.CREATE,
        )
        raw_schedules.append(created)
        backend.save_task_definitions(raw_schedules)
        return created


def update_topic_schedule(
    schedule_id: str,
    payload: dict[str, Any],
    *,
    actor: ResourceActor | None = None,
) -> dict[str, Any]:
    reject_client_authority_fields(payload)
    resolved_actor = resolve_resource_actor(actor)
    with SCHEDULE_LOCK, _schedule_store_lock():
        backend = get_scheduler_backend()
        if backend.read_only:
            raise RuntimeError("Configured scheduler backend is unavailable; read-only legacy fallback cannot mutate schedules.")
        raw_schedules = backend.load_task_definitions()
        schedule = _find_schedule(raw_schedules, schedule_id)
        profile = topic_profiles.get_topic_profile(
            str(schedule.get("profile_id") or "").strip(),
            actor=resolved_actor,
        )
        authorize_topic_schedule(
            schedule,
            profile,
            actor=resolved_actor,
            action=ResourceAction.UPDATE,
        )
        if payload.get("deleted"):
            raise RuntimeError("삭제는 delete_topic_schedule()를 사용하세요.")

        if "task_type" in payload and payload["task_type"] is not None:
            schedule["task_type"] = _validate_task_type(str(payload["task_type"]))
        if "title" in payload and payload["title"] is not None:
            schedule["title"] = str(payload["title"]).strip() or schedule["title"]
        if "cadence" in payload and payload["cadence"] is not None:
            cadence = str(payload["cadence"]).strip().lower()
            _parse_cadence(cadence)
            schedule["cadence"] = cadence
            schedule["cadence_label"] = cadence_label(cadence)
            if schedule.get("enabled"):
                schedule["next_run_at"] = compute_next_run_at(
                    cadence,
                    now=_utc_now(),
                    timezone_name=str(schedule.get("timezone") or DEFAULT_SCHEDULE_TIMEZONE),
                    inclusive=True,
                )
                schedule["next_run_hint"] = _next_run_hint(schedule.get("next_run_at"), schedule.get("timezone"))
        if "enabled" in payload and payload["enabled"] is not None:
            schedule["enabled"] = bool(payload["enabled"])
            if schedule["enabled"]:
                if get_auth_settings().auth_enabled and not schedule.get("owner_user_id"):
                    raise ResourceAccessDenied()
                schedule["next_run_at"] = compute_next_run_at(
                    str(schedule.get("cadence") or ""),
                    now=_utc_now(),
                    timezone_name=str(schedule.get("timezone") or DEFAULT_SCHEDULE_TIMEZONE),
                    inclusive=True,
                )
                schedule["next_run_hint"] = _next_run_hint(schedule.get("next_run_at"), schedule.get("timezone"))
                if not schedule.get("current_run_id"):
                    schedule["scheduler_state"] = "active"
            else:
                schedule["next_run_at"] = None
                schedule["next_run_hint"] = None
                if not schedule.get("current_run_id"):
                    schedule["scheduler_state"] = "paused"
        schedule["updated_at"] = iso_now_utc()
        backend.save_task_definitions(raw_schedules)
        return _schedule_view(schedule)


def delete_topic_schedule(
    schedule_id: str,
    *,
    actor: ResourceActor | None = None,
) -> dict[str, Any]:
    resolved_actor = resolve_resource_actor(actor)
    with SCHEDULE_LOCK, _schedule_store_lock():
        backend = get_scheduler_backend()
        if backend.read_only:
            raise RuntimeError("Configured scheduler backend is unavailable; read-only legacy fallback cannot mutate schedules.")
        raw_schedules = backend.load_task_definitions()
        schedule = _find_schedule(raw_schedules, schedule_id)
        profile = topic_profiles.get_topic_profile(
            str(schedule.get("profile_id") or "").strip(),
            actor=resolved_actor,
        )
        authorize_topic_schedule(
            schedule,
            profile,
            actor=resolved_actor,
            action=ResourceAction.DELETE,
        )
        if schedule.get("current_run_id"):
            raise RuntimeError("현재 실행 중인 예약 작업은 삭제할 수 없습니다.")
        schedule["enabled"] = False
        schedule["deleted"] = True
        schedule["deleted_at"] = iso_now_utc()
        schedule["next_run_at"] = None
        schedule["next_run_hint"] = None
        schedule["scheduler_state"] = "paused"
        schedule["updated_at"] = iso_now_utc()
        backend.save_task_definitions(raw_schedules)
        return {"schedule_id": schedule_id, "status": "deleted"}


def action_for_task_type(task_type: str | None) -> str:
    return TASK_TYPE_TO_ACTION.get(str(task_type or "").strip(), "report")


def trigger_topic_schedule_run(
    schedule_id: str,
    *,
    trigger: str,
    actor: ResourceActor | None = None,
    due_at: datetime | None = None,
    worker_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from app.schemas.runs import RunCreateRequest
    from app.services.runs import start_run

    with SCHEDULE_LOCK, _schedule_store_lock():
        backend = get_scheduler_backend()
        if backend.read_only:
            raise RuntimeError("Configured scheduler backend is unavailable; read-only legacy fallback cannot dispatch tasks.")
        raw_schedules = backend.load_task_definitions()
        schedule = _find_schedule(raw_schedules, schedule_id)
        stored_owner = str(schedule.get("owner_user_id") or "").strip() or None
        if trigger == "automatic":
            if get_auth_settings().auth_enabled and not stored_owner:
                schedule["enabled"] = False
                schedule["next_run_at"] = None
                schedule["next_run_hint"] = None
                schedule["scheduler_state"] = "blocked"
                schedule["last_error"] = "schedule_owner_missing"
                schedule["last_failure_at"] = iso_now_utc()
                schedule["last_failure_message"] = "schedule_owner_missing"
                schedule["updated_at"] = iso_now_utc()
                backend.save_task_definitions(raw_schedules)
                raise RuntimeError("schedule_owner_missing")
            resolved_actor = scoped_system_actor(stored_owner)
        else:
            resolved_actor = resolve_resource_actor(actor)
        profile = topic_profiles.get_topic_profile(
            str(schedule.get("profile_id") or ""),
            actor=resolved_actor,
        )
        authorize_topic_schedule(
            schedule,
            profile,
            actor=resolved_actor,
            action=ResourceAction.TRIGGER,
        )
        # Validate before acquiring a lease or starting any research work, even
        # when a next_run_at was saved on a different machine.
        _timezone_from_name(schedule.get("timezone"))
        has_active_run, _ = _is_current_run_in_progress(schedule, recover_orphans=False)
        if has_active_run:
            raise RuntimeError("이미 실행 중인 예약 작업입니다. 현재 실행이 끝난 뒤 다시 시도하세요.")
        dispatch_token = backend.acquire_dispatch_lease(schedule, worker_id=worker_id)
        schedule["updated_at"] = iso_now_utc()
        backend.save_task_definitions(raw_schedules)

        effective_owner = stored_owner
        payload = _schedule_run_payload(profile, str(schedule.get("task_type") or "collect_analyze"))
        try:
            run = start_run(
                RunCreateRequest.model_validate(payload),
                source=backend.trigger_source_for_task(trigger=trigger, task_type=str(schedule.get("task_type") or "")),
                owner_user_id=effective_owner,
                topic_profile_id=str(schedule.get("profile_id") or "").strip() or None,
                schedule_context={
                    "schedule_id": str(schedule.get("schedule_id") or "").strip() or None,
                    "profile_id": str(schedule.get("profile_id") or "").strip() or None,
                    "task_type": str(schedule.get("task_type") or "").strip() or None,
                    "trigger": trigger,
                    "worker_id": worker_id,
                    "dispatch_token": dispatch_token,
                },
            )
            if not str(run.get("run_id") or "").strip():
                raise RuntimeError("예약 작업 run dispatch가 run id를 반환하지 않았습니다.")
        except Exception as exc:
            failure_message = str(exc) or "예약 작업 run dispatch에 실패했습니다."
            schedule["last_failure_at"] = iso_now_utc()
            schedule["last_failure_message"] = failure_message
            schedule["last_error"] = schedule["last_failure_message"]
            schedule["scheduler_state"] = "failed"
            schedule["updated_at"] = iso_now_utc()
            backend.release_dispatch_lease(schedule)
            backend.save_task_definitions(raw_schedules)
            raise
        dispatch_time = iso_now_utc()
        schedule["last_run_id"] = run.get("run_id")
        schedule["last_status"] = run.get("status")
        schedule["last_run_at"] = dispatch_time
        schedule["current_run_id"] = run.get("run_id")
        schedule["current_run_status"] = run.get("status")
        schedule["last_error"] = None
        schedule["last_trigger_source"] = trigger
        schedule["scheduler_state"] = "running"
        backend.release_dispatch_lease(schedule)
        if trigger == "automatic":
            due_datetime = due_at or _parse_iso_datetime(schedule.get("next_run_at")) or _utc_now()
            schedule["last_scheduled_at"] = due_datetime.astimezone(timezone.utc).isoformat()
            schedule["next_run_at"] = compute_next_run_at(
                str(schedule.get("cadence") or ""),
                now=_utc_now(),
                reference=due_datetime,
                timezone_name=str(schedule.get("timezone") or DEFAULT_SCHEDULE_TIMEZONE),
                inclusive=False,
            )
            schedule["next_run_hint"] = _next_run_hint(schedule.get("next_run_at"), schedule.get("timezone"))
        elif schedule.get("enabled"):
            _ensure_next_run(schedule, now=_utc_now())
        schedule["updated_at"] = dispatch_time
        backend.save_task_definitions(raw_schedules)
        return schedule, run


def set_schedule_run_result(
    schedule_id: str,
    run: dict[str, Any],
    *,
    actor: ResourceActor | None = None,
) -> dict[str, Any]:
    resolved_actor = (
        resolve_resource_actor(actor)
        if actor is not None
        else scoped_system_actor(run.get("owner_user_id"))
    )
    with SCHEDULE_LOCK, _schedule_store_lock():
        backend = get_scheduler_backend()
        if backend.read_only:
            raise RuntimeError("Configured scheduler backend is unavailable; read-only legacy fallback cannot mutate schedules.")
        raw_schedules = backend.load_task_definitions()
        schedule = _find_schedule(raw_schedules, schedule_id)
        profile = topic_profiles.get_topic_profile(
            str(schedule.get("profile_id") or ""),
            actor=resolved_actor,
        )
        authorize_topic_schedule(
            schedule,
            profile,
            actor=resolved_actor,
            action=ResourceAction.UPDATE,
        )
        schedule["last_run_id"] = run.get("run_id")
        schedule["last_status"] = run.get("status")
        schedule["last_run_at"] = iso_now_utc()
        if run.get("status") == "completed":
            schedule["last_success_at"] = schedule["last_run_at"]
            schedule["last_error"] = None
        if run.get("status") == "failed":
            schedule["last_failure_at"] = schedule["last_run_at"]
            schedule["last_failure_message"] = run.get("error")
            schedule["last_error"] = run.get("error")
        schedule["current_run_id"] = run.get("run_id")
        schedule["current_run_status"] = run.get("status")
        schedule["scheduler_state"] = "running"
        schedule["updated_at"] = iso_now_utc()
        backend.save_task_definitions(raw_schedules)
        return schedule


def run_scheduler_cycle(*, now: datetime | None = None, recover_orphans: bool = False, worker_id: str | None = None) -> dict[str, Any]:
    with SCHEDULE_LOCK, _schedule_store_lock():
        backend = get_scheduler_backend()
        if backend.read_only:
            raise RuntimeError("Configured scheduler backend is unavailable; read-only legacy fallback cannot dispatch tasks.")
        raw_schedules = backend.load_task_definitions()
        if get_auth_settings().auth_enabled:
            for schedule in raw_schedules:
                if (
                    _visible_schedule(schedule)
                    and bool(schedule.get("enabled"))
                    and not str(schedule.get("owner_user_id") or "").strip()
                ):
                    schedule["enabled"] = False
                    schedule["next_run_at"] = None
                    schedule["next_run_hint"] = None
                    schedule["scheduler_state"] = "blocked"
                    schedule["last_error"] = "schedule_owner_missing"
                    schedule["last_failure_at"] = iso_now_utc()
                    schedule["last_failure_message"] = "schedule_owner_missing"
                    schedule["updated_at"] = iso_now_utc()
        raw_schedules, due_schedule_ids = backend.query_due_task_ids(
            raw_schedules,
            now=now,
            recover_orphans=recover_orphans,
            worker_id=worker_id,
        )
        backend.save_task_definitions(raw_schedules)
        executed: list[str] = []
        errors: list[str] = []
        resolved_now = now or _utc_now()

        for schedule_id in due_schedule_ids:
            try:
                schedule = _find_schedule(raw_schedules, schedule_id)
                due_at = _parse_iso_datetime(schedule.get("next_run_at")) or resolved_now
                trigger_topic_schedule_run(schedule_id, trigger="automatic", due_at=due_at, worker_id=worker_id)
                executed.append(schedule_id)
                raw_schedules = backend.load_task_definitions()
            except Exception as exc:
                raw_schedules = backend.load_task_definitions()
                schedule = next(
                    (item for item in raw_schedules if str(item.get("schedule_id") or "").strip() == schedule_id),
                    None,
                )
                if schedule is not None:
                    schedule["scheduler_state"] = "failed"
                    schedule["last_error"] = str(exc)
                    schedule["last_failure_at"] = iso_now_utc()
                    schedule["last_failure_message"] = str(exc)
                    backend.release_dispatch_lease(schedule)
                    schedule["updated_at"] = iso_now_utc()
                    topic_notifications.emit_schedule_failure_notification(schedule, error=str(exc))
                errors.append(f"{schedule_id}: {exc}")

        backend.save_task_definitions(raw_schedules)
        return {
            "items": [schedule for schedule in raw_schedules if _visible_schedule(schedule)],
            "executed_schedule_ids": executed,
            "errors": errors,
        }


def record_scheduler_heartbeat(
    *,
    worker_id: str,
    mode: str,
    enabled: bool,
    running: bool,
    tick_seconds: float,
    last_cycle_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = iso_now_utc()
    previous = read_json(SCHEDULER_STATUS_PATH, default={}) or {}
    preserve_started_at = (
        isinstance(previous, dict)
        and previous.get("worker_id") == worker_id
        and previous.get("mode") == mode
    )
    payload = {
        "worker_id": worker_id,
        "mode": mode,
        "enabled": enabled,
        "running": running,
        "pid": os.getpid(),
        "thread": threading.current_thread().name,
        "tick_seconds": tick_seconds,
        "started_at": previous.get("started_at") if preserve_started_at else now,
        "heartbeat_at": now,
        "last_cycle_summary": last_cycle_summary or {"executed_schedule_ids": [], "errors": []},
    }
    ensure_dir(SCHEDULE_LIBRARY_DIR)
    write_json(SCHEDULER_STATUS_PATH, payload)
    get_scheduler_backend().record_heartbeat(payload)
    return payload


def _latest_schedule_by_field(schedules: list[dict[str, Any]], field: str) -> dict[str, Any] | None:
    candidates = [schedule for schedule in schedules if str(schedule.get(field) or "").strip()]
    if not candidates:
        return None
    return max(candidates, key=lambda schedule: str(schedule.get(field) or ""))


def _schedule_runtime_summary(*, now: datetime | None = None, schedules: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    resolved_now = now or _utc_now()
    visible_schedules = [schedule for schedule in (schedules if schedules is not None else _load_raw_schedules()) if _visible_schedule(schedule)]
    due_count = 0
    active_dispatch_leases = 0
    expired_dispatch_leases = 0
    for schedule in visible_schedules:
        next_run = _parse_iso_datetime(schedule.get("next_run_at"))
        if (
            bool(schedule.get("enabled")) and next_run is not None and next_run <= resolved_now
            and not schedule.get("current_run_id") and not _schedule_timezone_error(schedule)
        ):
            due_count += 1
        if _lease_active(schedule, now=resolved_now):
            active_dispatch_leases += 1
        elif _lease_expired(schedule, now=resolved_now):
            expired_dispatch_leases += 1
    latest_dispatch = _latest_schedule_by_field(visible_schedules, "last_run_at")
    latest_success = _latest_schedule_by_field(visible_schedules, "last_success_at")
    latest_failure = _latest_schedule_by_field(visible_schedules, "last_failure_at")
    latest_recovery = _latest_schedule_by_field(visible_schedules, "last_recovery_at")
    return {
        "total": len(visible_schedules),
        "enabled": sum(1 for schedule in visible_schedules if bool(schedule.get("enabled"))),
        "running": sum(1 for schedule in visible_schedules if str(schedule.get("scheduler_state") or "") == "running"),
        "failed": sum(1 for schedule in visible_schedules if str(schedule.get("scheduler_state") or "") == "failed"),
        "due": due_count,
        "active_dispatch_leases": active_dispatch_leases,
        "expired_dispatch_leases": expired_dispatch_leases,
        "lease_health": {
            "active": active_dispatch_leases,
            "expired": expired_dispatch_leases,
            "healthy": expired_dispatch_leases == 0,
        },
        "last_dispatch_at": latest_dispatch.get("last_run_at") if latest_dispatch else None,
        "last_dispatch_schedule_id": latest_dispatch.get("schedule_id") if latest_dispatch else None,
        "last_dispatch_worker_id": (
            latest_dispatch.get("last_worker_id") or latest_dispatch.get("lock_owner") if latest_dispatch else None
        ),
        "last_success_at": latest_success.get("last_success_at") if latest_success else None,
        "last_success_schedule_id": latest_success.get("schedule_id") if latest_success else None,
        "last_failure_at": latest_failure.get("last_failure_at") if latest_failure else None,
        "last_failure_schedule_id": latest_failure.get("schedule_id") if latest_failure else None,
        "last_failure_message": latest_failure.get("last_failure_message") if latest_failure else None,
        "orphan_recovery_count": sum(1 for schedule in visible_schedules if schedule.get("last_recovery_at")),
        "last_orphan_recovery_at": latest_recovery.get("last_recovery_at") if latest_recovery else None,
        "last_orphan_recovery_message": latest_recovery.get("last_recovery_message") if latest_recovery else None,
    }


def _scheduler_operator_message(*, configured_mode: str, enabled: bool, status: str, configured_mode_valid: bool = True) -> str:
    if not configured_mode_valid:
        return "TOPIC_SCHEDULER_MODE 값이 지원되지 않습니다. embedded, external, disabled 중 하나로 설정하세요."
    if not enabled:
        return "scheduler가 비활성화되어 자동 실행을 수행하지 않습니다."
    if configured_mode == "external" and status != "healthy":
        return "외부 scheduler worker heartbeat가 없습니다. `python -m app.jobs.topic_schedule_worker` 프로세스를 실행해야 자동 작업이 처리됩니다."
    if status == "healthy":
        return "scheduler heartbeat가 정상이며 자동 작업을 처리할 수 있습니다."
    return "scheduler heartbeat가 지연되었습니다. 자동 실행 상태를 확인하세요."


def _scheduler_runtime_mode_status(*, configured_enabled: bool, configured_mode_valid: bool, status: str) -> str:
    if not configured_mode_valid:
        return "degraded"
    if not configured_enabled:
        return "unavailable"
    if status != "healthy":
        return "degraded"
    return "healthy"


def get_scheduler_runtime_status(
    *,
    expected_mode: str = "embedded",
    embedded_enabled: bool = True,
    runtime_mode: str | None = None,
    actor: ResourceActor | None = None,
    include_resource_activity: bool = True,
) -> dict[str, Any]:
    resolved_actor: ResourceActor | None = None
    if include_resource_activity:
        try:
            resolved_actor = resolve_resource_actor(actor)
        except ResourceAccessDenied:
            # Principal-free readiness/operator probes may expose scheduler
            # capability and heartbeat health, but never resource activity.
            include_resource_activity = False
    backend = get_scheduler_backend(mode=runtime_mode)
    configured_mode_raw = str(os.getenv("TOPIC_SCHEDULER_MODE") or expected_mode or "embedded").strip().lower()
    configured_mode_valid = configured_mode_raw in {"embedded", "external", "disabled"}
    configured_mode = configured_mode_raw
    if not configured_mode_valid:
        configured_mode = "embedded"
    configured_enabled = embedded_enabled and str(os.getenv("TOPIC_SCHEDULER_ENABLED") or "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    if configured_mode == "disabled":
        configured_enabled = False
    raw = read_json(SCHEDULER_STATUS_PATH, default={})
    if not isinstance(raw, dict):
        raw = {}
    heartbeat_at = _parse_iso_datetime(raw.get("heartbeat_at"))
    now = _utc_now()
    heartbeat_age_seconds = (now - heartbeat_at).total_seconds() if heartbeat_at else None
    stale = heartbeat_age_seconds is None or heartbeat_age_seconds > SCHEDULER_HEARTBEAT_STALE_SECONDS
    worker_mode = str(raw.get("mode") or "").strip() or None
    worker_enabled = bool(raw.get("enabled", False)) if raw else None
    mode = configured_mode
    enabled = configured_enabled
    status = "disabled"
    if enabled:
        status = "stale" if stale else "healthy"
    runtime_mode_status = _scheduler_runtime_mode_status(
        configured_enabled=configured_enabled,
        configured_mode_valid=configured_mode_valid,
        status=status,
    )
    backend_capabilities = backend.capabilities()
    operator_message = _scheduler_operator_message(
        configured_mode=configured_mode,
        enabled=enabled,
        status=status,
        configured_mode_valid=configured_mode_valid,
    )
    if backend.read_only:
        runtime_mode_status = "degraded"
        operator_message = (
            "Configured scheduler backend is unavailable; read-only legacy file schedules are visible, "
            "but mutations and automatic dispatch are blocked."
        )
    distributed_requested = _distributed_scheduler_requested()
    distributed_support_status = "unsupported" if distributed_requested else "gated"
    if distributed_requested:
        runtime_mode_status = "degraded"
        operator_message = "distributed multi-node scheduler is not implemented; use single-node/shared-DB worker modes only."
    if not include_resource_activity:
        schedules_for_summary = []
    elif resolved_actor is not None and resolved_actor.auth_mode == "disabled":
        try:
            schedules_for_summary = (
                _load_raw_schedules()
                if backend.mode == SCHEDULER_RUNTIME_MODE
                else backend.load_task_definitions()
            )
        except Exception:
            schedules_for_summary = _load_raw_schedules()
    else:
        try:
            schedules_for_summary = _load_task_definitions_read_only(backend)
        except Exception:
            schedules_for_summary = _load_raw_schedules()
        if resolved_actor is not None:
            schedules_for_summary = _visible_schedules_for_actor(
                schedules_for_summary,
                actor=resolved_actor,
            )
    schedule_summary = _schedule_runtime_summary(now=now, schedules=schedules_for_summary)
    backend_descriptor = backend.descriptor()
    queue_summary = backend_descriptor.get("queue") if isinstance(backend_descriptor.get("queue"), dict) else None
    queue_backend_status = "available" if backend.mode == QUEUE_SCHEDULER_RUNTIME_MODE and backend.available else "not_selected"
    configured_runtime = str(backend_descriptor.get("configured_runtime_mode") or "")
    if configured_runtime == QUEUE_SCHEDULER_RUNTIME_MODE and not backend.available:
        queue_backend_status = "unavailable"
    last_cycle_summary = raw.get("last_cycle_summary") or {"executed_schedule_ids": [], "errors": []}
    if resolved_actor is not None and resolved_actor.auth_mode != "disabled":
        visible_schedule_ids = {
            str(schedule.get("schedule_id") or "").strip()
            for schedule in schedules_for_summary
        }
        last_cycle_summary = {
            "executed_schedule_ids": [
                schedule_id
                for schedule_id in last_cycle_summary.get("executed_schedule_ids", [])
                if str(schedule_id) in visible_schedule_ids
            ],
            "errors": [
                error
                for error in last_cycle_summary.get("errors", [])
                if any(str(error).startswith(f"{schedule_id}:") for schedule_id in visible_schedule_ids)
            ],
        }
        backend_descriptor = dict(backend_descriptor)
        backend_descriptor.pop("queue", None)
        queue_summary = None
    if not include_resource_activity:
        last_cycle_summary = {"executed_schedule_ids": [], "errors": []}
        backend_descriptor = dict(backend_descriptor)
        backend_descriptor.pop("queue", None)
        queue_summary = None

    return {
        "mode": mode,
        "enabled": enabled,
        "worker_mode": worker_mode,
        "worker_enabled": worker_enabled,
        "running": bool(enabled and raw.get("running", False) and not stale),
        "status": status,
        "runtime_mode": backend.mode,
        "runtime_mode_status": runtime_mode_status,
        "runtime_mode_label": backend.display_name,
        "storage_backend": backend.storage_backend,
        "configured_runtime_mode": backend_descriptor.get("configured_runtime_mode"),
        "configured_runtime_mode_raw": backend_descriptor.get("configured_runtime_mode_raw"),
        "configured_runtime_mode_source": backend_descriptor.get("configured_runtime_mode_source"),
        "configured_runtime_mode_valid": backend_descriptor.get("configured_runtime_mode_valid"),
        "distributed_ready": backend.distributed_ready,
        "multi_node_dispatch_safe": backend.multi_node_dispatch_safe,
        "distributed_requested": distributed_requested,
        "distributed_support_status": distributed_support_status,
        "queue_backend_status": queue_backend_status,
        "queue_item_counts": queue_summary or {"queued": 0, "leased": 0, "expired": 0, "failed": 0, "completed": 0, "total": 0},
        "backend": backend_descriptor,
        "available_runtime_modes": describe_scheduler_backends(),
        "architecture_note": backend.architecture_note,
        "dispatch_safety": {
            "dispatch_leases": bool(backend_capabilities.get("dispatch_lease")),
            "duplicate_execution_protection": bool(backend_capabilities.get("dispatch_lease")),
            "orphan_recovery": bool(backend_capabilities.get("orphan_recovery")),
            "durable_queue": bool(backend_capabilities.get("durable_queue", False)),
            "multi_node_dispatch_safe": backend.multi_node_dispatch_safe,
            "multi_process_dispatch_safe": bool(backend.descriptor().get("multi_process_dispatch_safe", False)),
        },
        "worker_id": raw.get("worker_id"),
        "pid": raw.get("pid"),
        "thread": raw.get("thread"),
        "tick_seconds": raw.get("tick_seconds"),
        "started_at": raw.get("started_at"),
        "heartbeat_at": raw.get("heartbeat_at"),
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "stale": stale if enabled else False,
        "last_cycle_summary": last_cycle_summary,
        "status_snapshot": {
            "mode": worker_mode,
            "enabled": worker_enabled,
            "running": bool(raw.get("running", False)),
            "worker_id": raw.get("worker_id"),
            "heartbeat_at": raw.get("heartbeat_at"),
        },
        "currently_leased_task_count": schedule_summary["active_dispatch_leases"],
        "due_task_count": schedule_summary["due"],
        "lease_health": schedule_summary["lease_health"],
        "last_dispatch_at": schedule_summary["last_dispatch_at"],
        "last_dispatch_schedule_id": schedule_summary["last_dispatch_schedule_id"],
        "last_dispatch_worker_id": schedule_summary["last_dispatch_worker_id"],
        "last_success_at": schedule_summary["last_success_at"],
        "last_success_schedule_id": schedule_summary["last_success_schedule_id"],
        "last_failure_at": schedule_summary["last_failure_at"],
        "last_failure_schedule_id": schedule_summary["last_failure_schedule_id"],
        "last_failure_message": schedule_summary["last_failure_message"],
        "orphan_recovery_count": schedule_summary["orphan_recovery_count"],
        "last_orphan_recovery_at": schedule_summary["last_orphan_recovery_at"],
        "last_orphan_recovery_message": schedule_summary["last_orphan_recovery_message"],
        "lock_timeout_seconds": STORE_LOCK_TIMEOUT_SECONDS,
        "dispatch_lease_seconds": DISPATCH_LEASE_SECONDS,
        "configured_mode": configured_mode,
        "configured_mode_raw": configured_mode_raw,
        "configured_mode_valid": configured_mode_valid,
        "requires_external_worker": configured_mode == "external" and enabled,
        "operator_message": operator_message,
        "store_lock": _lock_file_status(now=now),
        "schedule_summary": schedule_summary,
    }


def scheduler_preflight_check(*, runtime_mode: str | None = None, expected_mode: str = "external") -> dict[str, Any]:
    status = get_scheduler_runtime_status(expected_mode=expected_mode, runtime_mode=runtime_mode)
    backend = status.get("backend") if isinstance(status.get("backend"), dict) else {}
    schedule_store_lock = schedule_store_lock_preflight(recover_stale=False)
    blocking_reasons: list[str] = []
    if status.get("configured_runtime_mode_valid") is False:
        blocking_reasons.append(str(backend.get("unavailable_reason") or "Invalid scheduler runtime mode."))
    if backend and backend.get("available") is False:
        blocking_reasons.append(str(backend.get("unavailable_reason") or "Scheduler backend is unavailable."))
    if status.get("distributed_requested") and not status.get("multi_node_dispatch_safe"):
        blocking_reasons.append("distributed multi-node scheduler is not implemented for this runtime.")
    ok = not blocking_reasons
    return {
        "ok": ok,
        "runtime_mode": status.get("runtime_mode"),
        "runtime_mode_status": status.get("runtime_mode_status"),
        "runtime_mode_label": status.get("runtime_mode_label"),
        "storage_backend": status.get("storage_backend"),
        "configured_runtime_mode": status.get("configured_runtime_mode"),
        "configured_runtime_mode_raw": status.get("configured_runtime_mode_raw"),
        "configured_runtime_mode_source": status.get("configured_runtime_mode_source"),
        "configured_runtime_mode_valid": status.get("configured_runtime_mode_valid"),
        "queue_backend_status": status.get("queue_backend_status"),
        "queue_item_counts": status.get("queue_item_counts"),
        "distributed_requested": status.get("distributed_requested"),
        "distributed_support_status": status.get("distributed_support_status"),
        "distributed_ready": status.get("distributed_ready"),
        "multi_node_dispatch_safe": status.get("multi_node_dispatch_safe"),
        "multi_process_dispatch_safe": status.get("dispatch_safety", {}).get("multi_process_dispatch_safe"),
        "worker_id": status.get("worker_id"),
        "heartbeat_at": status.get("heartbeat_at"),
        "due_task_count": status.get("due_task_count"),
        "currently_leased_task_count": status.get("currently_leased_task_count"),
        "lease_health": status.get("lease_health"),
        "schedule_store_lock": schedule_store_lock,
        "blocking_reasons": list(dict.fromkeys(item for item in blocking_reasons if item)),
        "operator_message": status.get("operator_message"),
    }
