"""Explicit, fail-closed SQLite foreign-key migration boundary.

Planning and auditing are read-only.  Applying a migration requires a current
plan digest, an explicit call, an OS-backed migration lock, an exclusive SQLite
transaction, an orphan-free source, and a verified pre-migration backup.  No
automatic repair or startup migration is provided.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Final

from app.db.bootstrap import SCHEDULER_SCHEMA_SQL
from app.db.sqlite_integrity import (
    CODE_MIGRATION_REQUIRED,
    CODE_SCHEMA_MISMATCH,
    REQUIRED_FOREIGN_KEYS,
    REQUIRED_TABLES,
    STATUS_MIGRATION_REQUIRED,
    STATUS_READY,
    STATUS_SCHEMA_MISMATCH,
    STATUS_UNINITIALIZED,
    STORE_METADATA,
    STORE_SCHEDULER,
    SUPPORTED_STORES,
    ForeignKeyInventory,
    RequiredForeignKey,
    SqliteIntegrityError,
    SqliteSchemaMismatch,
    inspect_required_foreign_keys,
    open_sqlite_read_write,
    require_store_write_compatible,
    sqlite_user_tables,
)
from app.db.sqlite_snapshot import (
    CODE_AUDIT_FAILED,
    CODE_SOURCE_SIDECARS_PRESENT,
    SNAPSHOT_STRATEGY,
    SOURCE_OBSERVATION_NONE,
    SOURCE_OBSERVATION_UNSUPPORTED,
    SqliteSourceFingerprint,
    SqliteSnapshotError,
    capture_sqlite_source_fingerprint,
    capture_stable_sqlite_snapshot,
    open_immutable_snapshot,
)
from app.repositories.file_locks import FileLockError, file_lock

MIGRATION_IDS: Final = {
    STORE_METADATA: "metadata_required_foreign_keys_v1",
    STORE_SCHEDULER: "scheduler_required_foreign_keys_v1",
}
SCHEDULER_SCHEMA_RECONCILIATION_ID: Final = (
    "scheduler_schema_reconciliation_v2"
)

SCHEMA_CLASSIFICATION_METADATA_V1: Final = "metadata_required_foreign_keys_v1"
SCHEMA_CLASSIFICATION_SCHEDULER_READY: Final = "scheduler_ready"
SCHEMA_CLASSIFICATION_SCHEDULER_V1: Final = (
    "scheduler_required_foreign_keys_v1"
)
SCHEMA_CLASSIFICATION_SCHEDULER_V2: Final = (
    SCHEDULER_SCHEMA_RECONCILIATION_ID
)
SCHEMA_CLASSIFICATION_UNSUPPORTED: Final = "unsupported_schema"

RUN_JOBS_ADDITIVE_OBJECTS: Final = (
    "run_jobs_table",
    "run_jobs_named_indexes",
)

STATUS_MISSING: Final = "missing"
STATUS_ORPHANS_DETECTED: Final = "orphans_detected"
STATUS_INTEGRITY_FAILED: Final = "integrity_failed"
STATUS_MALFORMED: Final = "malformed"
STATUS_BUSY: Final = "busy"
STATUS_UNREADABLE: Final = "unreadable"

CODE_ORPHANS_DETECTED: Final = "sqlite_fk_orphans_detected"
CODE_PLAN_STALE: Final = "sqlite_fk_plan_stale"
CODE_MIGRATION_BUSY: Final = "sqlite_fk_migration_busy"
CODE_MIGRATION_FAILED: Final = "sqlite_fk_migration_failed"
CODE_BACKUP_FAILED: Final = "sqlite_fk_backup_failed"
CODE_VERIFICATION_FAILED: Final = "sqlite_fk_verification_failed"
CODE_INTEGRITY_CHECK_FAILED: Final = "sqlite_integrity_check_failed"
CODE_STORE_MISSING: Final = "sqlite_store_missing"

DEFAULT_MIGRATION_TIMEOUT_SECONDS: Final = 5.0

BUSY_PHASE_MIGRATION_LOCK: Final = "migration_lock"
BUSY_PHASE_SOURCE_REVALIDATION: Final = "source_revalidation"
BUSY_PHASE_EXCLUSIVE_TRANSACTION: Final = "exclusive_transaction"
BUSY_PHASE_BACKUP_SNAPSHOT: Final = "backup_snapshot"
BUSY_PHASE_COMMIT: Final = "commit"
BUSY_PHASES: Final = frozenset(
    {
        BUSY_PHASE_MIGRATION_LOCK,
        BUSY_PHASE_SOURCE_REVALIDATION,
        BUSY_PHASE_EXCLUSIVE_TRANSACTION,
        BUSY_PHASE_BACKUP_SNAPSHOT,
        BUSY_PHASE_COMMIT,
    }
)

FAILURE_PHASES: Final = frozenset(
    {
        "after_first_replacement_create",
        "after_first_copy",
        "after_later_copy",
        "after_old_table_drop",
        "after_rename",
        "after_index_recreation",
        "before_fk_check",
        "before_quick_check",
        "before_commit",
    }
)
SCHEDULER_V2_FAILURE_PHASES: Final = frozenset(
    {
        "after_backup_verification",
        "after_run_jobs_table_create",
        "after_first_run_jobs_index",
        "after_all_run_jobs_indexes",
        "after_queue_legacy_rename",
        "after_queue_replacement_create",
        "after_queue_copy",
        "after_queue_legacy_drop",
        "after_queue_index_recreation",
        "before_schema_fingerprint_verification",
        "before_fk_check",
        "before_quick_check",
        "before_commit",
    }
)
ALL_FAILURE_PHASES: Final = FAILURE_PHASES | SCHEDULER_V2_FAILURE_PHASES


class SqliteMigrationError(SqliteIntegrityError):
    code = CODE_MIGRATION_FAILED


class SqliteForeignKeyOrphansDetected(SqliteMigrationError):
    code = CODE_ORPHANS_DETECTED


class SqliteForeignKeyPlanStale(SqliteMigrationError):
    code = CODE_PLAN_STALE


class SqliteForeignKeyMigrationBusy(SqliteMigrationError):
    code = CODE_MIGRATION_BUSY

    def __init__(self, store: str, *, phase: str) -> None:
        if phase not in BUSY_PHASES:
            raise ValueError("unsupported SQLite migration busy phase")
        self.phase = phase
        self.retryable = True
        super().__init__(store)


class SqliteForeignKeyBackupFailed(SqliteMigrationError):
    code = CODE_BACKUP_FAILED


class SqliteForeignKeyVerificationFailed(SqliteMigrationError):
    code = CODE_VERIFICATION_FAILED


class SqliteIntegrityCheckFailed(SqliteMigrationError):
    code = CODE_INTEGRITY_CHECK_FAILED


class SqliteStoreMissing(SqliteMigrationError):
    code = CODE_STORE_MISSING


class _InjectedMigrationFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class TableMigrationSpec:
    table: str
    columns: tuple[str, ...]
    legacy_ddl: str
    target_ddl: str


@dataclass(frozen=True)
class SchedulerSchemaProfile:
    classification: str
    migration_id: str
    structural_fingerprint: str
    expected_fingerprint: str
    current_table_count: int
    expected_table_count: int
    missing_additive_objects: tuple[str, ...]
    queue_rebuild_required: bool
    conflicting_object_count: int
    unexpected_object_count: int
    schema_compatible: bool


TABLE_MIGRATIONS: Final = {
    STORE_METADATA: (
        TableMigrationSpec(
            table="run_topics",
            columns=("run_id", "topic"),
            legacy_ddl="""
                CREATE TABLE run_topics (
                  run_id TEXT NOT NULL,
                  topic TEXT NOT NULL,
                  PRIMARY KEY (run_id, topic)
                )
            """,
            target_ddl="""
                CREATE TABLE {table} (
                  run_id TEXT NOT NULL,
                  topic TEXT NOT NULL,
                  PRIMARY KEY (run_id, topic),
                  FOREIGN KEY (run_id) REFERENCES runs(run_id)
                    ON DELETE CASCADE
                    ON UPDATE NO ACTION
                )
            """,
        ),
        TableMigrationSpec(
            table="papers",
            columns=(
                "run_id",
                "paper_id",
                "title",
                "theme",
                "score",
                "status",
                "published",
                "entry_url",
                "pdf_url",
                "dedup_cluster_id",
                "is_primary",
            ),
            legacy_ddl="""
                CREATE TABLE papers (
                  run_id TEXT NOT NULL,
                  paper_id TEXT NOT NULL,
                  title TEXT NOT NULL,
                  theme TEXT NOT NULL,
                  score INTEGER NOT NULL DEFAULT 0,
                  status TEXT NOT NULL,
                  published TEXT,
                  entry_url TEXT,
                  pdf_url TEXT,
                  dedup_cluster_id TEXT,
                  is_primary INTEGER NOT NULL DEFAULT 1,
                  PRIMARY KEY (run_id, paper_id)
                )
            """,
            target_ddl="""
                CREATE TABLE {table} (
                  run_id TEXT NOT NULL,
                  paper_id TEXT NOT NULL,
                  title TEXT NOT NULL,
                  theme TEXT NOT NULL,
                  score INTEGER NOT NULL DEFAULT 0,
                  status TEXT NOT NULL,
                  published TEXT,
                  entry_url TEXT,
                  pdf_url TEXT,
                  dedup_cluster_id TEXT,
                  is_primary INTEGER NOT NULL DEFAULT 1,
                  PRIMARY KEY (run_id, paper_id),
                  FOREIGN KEY (run_id) REFERENCES runs(run_id)
                    ON DELETE CASCADE
                    ON UPDATE NO ACTION
                )
            """,
        ),
        TableMigrationSpec(
            table="evidence",
            columns=(
                "run_id",
                "evidence_id",
                "source_id",
                "paper_id",
                "title",
                "source_type",
                "quality_hint",
                "confidence",
                "artifact_path",
                "citation_text",
                "snippet",
            ),
            legacy_ddl="""
                CREATE TABLE evidence (
                  run_id TEXT NOT NULL,
                  evidence_id TEXT NOT NULL,
                  source_id TEXT NOT NULL,
                  paper_id TEXT NOT NULL,
                  title TEXT NOT NULL,
                  source_type TEXT NOT NULL,
                  quality_hint TEXT,
                  confidence REAL,
                  artifact_path TEXT,
                  citation_text TEXT NOT NULL,
                  snippet TEXT NOT NULL,
                  PRIMARY KEY (run_id, evidence_id)
                )
            """,
            target_ddl="""
                CREATE TABLE {table} (
                  run_id TEXT NOT NULL,
                  evidence_id TEXT NOT NULL,
                  source_id TEXT NOT NULL,
                  paper_id TEXT NOT NULL,
                  title TEXT NOT NULL,
                  source_type TEXT NOT NULL,
                  quality_hint TEXT,
                  confidence REAL,
                  artifact_path TEXT,
                  citation_text TEXT NOT NULL,
                  snippet TEXT NOT NULL,
                  PRIMARY KEY (run_id, evidence_id),
                  FOREIGN KEY (run_id) REFERENCES runs(run_id)
                    ON DELETE CASCADE
                    ON UPDATE NO ACTION
                )
            """,
        ),
        TableMigrationSpec(
            table="report_sections",
            columns=(
                "run_id",
                "section_id",
                "title",
                "claim_count",
                "citation_count",
                "updated_at",
            ),
            legacy_ddl="""
                CREATE TABLE report_sections (
                  run_id TEXT NOT NULL,
                  section_id TEXT NOT NULL,
                  title TEXT NOT NULL,
                  claim_count INTEGER NOT NULL DEFAULT 0,
                  citation_count INTEGER NOT NULL DEFAULT 0,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (run_id, section_id)
                )
            """,
            target_ddl="""
                CREATE TABLE {table} (
                  run_id TEXT NOT NULL,
                  section_id TEXT NOT NULL,
                  title TEXT NOT NULL,
                  claim_count INTEGER NOT NULL DEFAULT 0,
                  citation_count INTEGER NOT NULL DEFAULT 0,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (run_id, section_id),
                  FOREIGN KEY (run_id) REFERENCES runs(run_id)
                    ON DELETE CASCADE
                    ON UPDATE NO ACTION
                )
            """,
        ),
    ),
    STORE_SCHEDULER: (
        TableMigrationSpec(
            table="scheduler_queue_items",
            columns=(
                "queue_item_id",
                "schedule_id",
                "due_at",
                "status",
                "lease_token",
                "lease_owner",
                "lease_acquired_at",
                "lease_expires_at",
                "dispatch_attempts",
                "run_id",
                "error_message",
                "created_at",
                "updated_at",
                "payload_json",
            ),
            legacy_ddl="""
                CREATE TABLE scheduler_queue_items (
                  queue_item_id TEXT PRIMARY KEY,
                  schedule_id TEXT NOT NULL,
                  due_at TEXT NOT NULL,
                  status TEXT NOT NULL,
                  lease_token TEXT,
                  lease_owner TEXT,
                  lease_acquired_at TEXT,
                  lease_expires_at TEXT,
                  dispatch_attempts INTEGER NOT NULL DEFAULT 0,
                  run_id TEXT,
                  error_message TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  UNIQUE(schedule_id, due_at)
                )
            """,
            target_ddl="""
                CREATE TABLE {table} (
                  queue_item_id TEXT PRIMARY KEY,
                  schedule_id TEXT NOT NULL,
                  due_at TEXT NOT NULL,
                  status TEXT NOT NULL,
                  lease_token TEXT,
                  lease_owner TEXT,
                  lease_acquired_at TEXT,
                  lease_expires_at TEXT,
                  dispatch_attempts INTEGER NOT NULL DEFAULT 0,
                  run_id TEXT,
                  error_message TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  UNIQUE(schedule_id, due_at),
                  FOREIGN KEY (schedule_id) REFERENCES scheduler_schedules(schedule_id)
                    ON DELETE CASCADE
                    ON UPDATE NO ACTION
                )
            """,
        ),
    ),
}


@dataclass(frozen=True)
class RelationAudit:
    relationship_id: str
    status: str
    orphan_count: int | None

    def to_payload(self) -> dict[str, object]:
        return {
            "relationship_id": self.relationship_id,
            "status": self.status,
            "orphan_count": self.orphan_count,
        }


RELATION_CHECKED: Final = "checked"
RELATION_CHILD_TABLE_MISSING: Final = "skipped_child_table_missing"
RELATION_CHILD_COLUMN_MISSING: Final = "skipped_child_column_missing"
RELATION_PARENT_TABLE_MISSING: Final = "skipped_parent_table_missing"
RELATION_PARENT_COLUMN_MISSING: Final = "skipped_parent_column_missing"


@dataclass(frozen=True)
class MigrationPlan:
    store: str
    migration_id: str
    status: str
    current_declared_fk_count: int
    target_fk_count: int
    missing_relationships: tuple[str, ...]
    conflicting_relationship_count: int
    unexpected_relationship_count: int
    schema_fingerprint: str | None
    source_fingerprint: str | None
    source_size: int | None
    sidecars_present: tuple[str, ...]
    source_observation_status: str
    snapshot_strategy: str | None
    relation_audits: tuple[RelationAudit, ...]
    orphan_counts: tuple[tuple[str, int], ...]
    total_orphan_count: int
    quick_check_ok: bool
    foreign_key_violation_count: int
    migration_required: bool
    schema_compatible: bool
    eligible: bool
    backup_required: bool
    query_only_enabled: bool
    foreign_keys_enabled: bool
    issue_codes: tuple[str, ...]
    schema_classification: str
    structural_schema_fingerprint: str | None
    expected_schema_fingerprint: str | None
    current_table_count: int
    expected_table_count: int
    missing_additive_objects: tuple[str, ...]
    queue_rebuild_required: bool
    conflicting_object_count: int
    unexpected_object_count: int
    plan_digest: str | None

    def to_payload(self) -> dict[str, object]:
        return {
            "store": self.store,
            "migration_id": self.migration_id,
            "status": self.status,
            "current_declared_foreign_key_count": self.current_declared_fk_count,
            "target_foreign_key_count": self.target_fk_count,
            "missing_relationships": list(self.missing_relationships),
            "conflicting_relationship_count": self.conflicting_relationship_count,
            "unexpected_relationship_count": self.unexpected_relationship_count,
            "schema_fingerprint": self.schema_fingerprint,
            "source_fingerprint": self.source_fingerprint,
            "source_size": self.source_size,
            "sidecars_present": list(self.sidecars_present),
            "source_observation_status": self.source_observation_status,
            "snapshot_strategy": self.snapshot_strategy,
            "relation_audit_coverage": [
                item.to_payload() for item in self.relation_audits
            ],
            "orphan_counts": dict(self.orphan_counts),
            "total_orphan_count": self.total_orphan_count,
            "quick_check_ok": self.quick_check_ok,
            "foreign_key_violation_count": self.foreign_key_violation_count,
            "migration_required": self.migration_required,
            "schema_compatible": self.schema_compatible,
            "eligible": self.eligible,
            "backup_required": self.backup_required,
            "query_only_enabled": self.query_only_enabled,
            "foreign_keys_enabled": self.foreign_keys_enabled,
            "issue_codes": list(self.issue_codes),
            "schema_classification": self.schema_classification,
            "structural_schema_fingerprint": self.structural_schema_fingerprint,
            "expected_schema_fingerprint": self.expected_schema_fingerprint,
            "current_table_count": self.current_table_count,
            "expected_table_count": self.expected_table_count,
            "missing_additive_objects": list(self.missing_additive_objects),
            "queue_rebuild_required": self.queue_rebuild_required,
            "conflicting_object_count": self.conflicting_object_count,
            "unexpected_object_count": self.unexpected_object_count,
            "plan_digest": self.plan_digest,
        }


@dataclass(frozen=True)
class IntegrityAudit:
    store: str
    status: str
    complete: bool
    read_only: bool
    query_only_enabled: bool
    foreign_keys_enabled: bool
    declared_fk_count: int
    expected_fk_count: int
    missing_fk_count: int
    conflicting_fk_count: int
    source_observation_status: str
    snapshot_strategy: str | None
    relation_audits: tuple[RelationAudit, ...]
    orphan_count: int
    quick_check_ok: bool
    foreign_key_violation_count: int
    migration_required: bool
    issue_codes: tuple[str, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "store": self.store,
            "status": self.status,
            "complete": self.complete,
            "read_only": self.read_only,
            "query_only_enabled": self.query_only_enabled,
            "foreign_keys_enabled": self.foreign_keys_enabled,
            "declared_foreign_key_count": self.declared_fk_count,
            "expected_foreign_key_count": self.expected_fk_count,
            "missing_foreign_key_count": self.missing_fk_count,
            "conflicting_foreign_key_count": self.conflicting_fk_count,
            "source_observation_status": self.source_observation_status,
            "snapshot_strategy": self.snapshot_strategy,
            "relation_audit_coverage": [
                item.to_payload() for item in self.relation_audits
            ],
            "orphan_count": self.orphan_count,
            "quick_check_ok": self.quick_check_ok,
            "foreign_key_violation_count": self.foreign_key_violation_count,
            "migration_required": self.migration_required,
            "issue_codes": list(self.issue_codes),
        }


@dataclass(frozen=True)
class MigrationApplyResult:
    store: str
    migration_id: str
    status: str
    applied: bool
    rebuilt_table_count: int
    backup_id: str | None
    backup_sha256: str | None
    plan_digest: str
    declared_fk_count: int
    foreign_key_violation_count: int
    quick_check_ok: bool

    def to_payload(self) -> dict[str, object]:
        return {
            "store": self.store,
            "migration_id": self.migration_id,
            "status": self.status,
            "applied": self.applied,
            "rebuilt_table_count": self.rebuilt_table_count,
            "backup_id": self.backup_id,
            "backup_sha256": self.backup_sha256,
            "plan_digest": self.plan_digest,
            "declared_foreign_key_count": self.declared_fk_count,
            "foreign_key_violation_count": self.foreign_key_violation_count,
            "quick_check_ok": self.quick_check_ok,
        }


def _validate_store(store: str) -> str:
    if store not in SUPPORTED_STORES:
        raise ValueError("unsupported SQLite logical store")
    return store


def _quote(identifier: str) -> str:
    if not identifier or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in identifier):
        raise ValueError("unsafe SQLite identifier")
    return f'"{identifier}"'


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _schema_fingerprint(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "SELECT type, name, tbl_name, COALESCE(sql, '') "
        "FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    safe_rows = [
        {
            "type": str(row[0]),
            "name": str(row[1]),
            "table": str(row[2]),
            "sql_sha256": hashlib.sha256(str(row[3]).encode("utf-8")).hexdigest(),
        }
        for row in rows
    ]
    encoded = json.dumps(safe_rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sqlite_affinity(declared_type: str) -> str:
    normalized = declared_type.upper()
    if "INT" in normalized:
        return "INTEGER"
    if any(token in normalized for token in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if not normalized or "BLOB" in normalized:
        return "BLOB"
    if any(token in normalized for token in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def _default_descriptor(value: object) -> dict[str, object]:
    if value is None:
        return {"kind": "none", "signature": None}
    normalized = " ".join(str(value).strip().split())
    if re.fullmatch(r"[+-]?\d+", normalized):
        kind = "integer_literal"
    elif re.fullmatch(
        r"[+-]?(?:\d+\.\d*|\d*\.\d+)(?:[eE][+-]?\d+)?",
        normalized,
    ):
        kind = "real_literal"
    elif (
        len(normalized) >= 2
        and normalized[0] == normalized[-1]
        and normalized[0] in "'\""
    ):
        kind = "text_literal"
    elif normalized.upper() in {
        "NULL",
        "TRUE",
        "FALSE",
        "CURRENT_TIME",
        "CURRENT_DATE",
        "CURRENT_TIMESTAMP",
    }:
        kind = "keyword_literal"
    else:
        kind = "expression"
    return {"kind": kind, "signature": _text_sha256(normalized)}


def _scheduler_schema_descriptor(
    connection: sqlite3.Connection,
) -> dict[str, object]:
    markers = {
        "user_version": int(
            connection.execute("PRAGMA user_version").fetchone()[0]
        ),
        "application_id": int(
            connection.execute("PRAGMA application_id").fetchone()[0]
        ),
        "explicit_contract": False,
    }
    table_rows = connection.execute(
        "SELECT name, COALESCE(sql, '') FROM sqlite_schema "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables: dict[str, dict[str, object]] = {}
    global_indexes: dict[str, dict[str, object]] = {}
    for table_row in table_rows:
        table = str(table_row[0])
        raw_sql = str(table_row[1] or "")
        columns = []
        for row in connection.execute(
            f"PRAGMA table_xinfo({_quote(table)})"
        ).fetchall():
            declared_type = str(row[2] or "").upper()
            columns.append(
                {
                    "name": str(row[1]),
                    "declared_type": declared_type,
                    "affinity": _sqlite_affinity(declared_type),
                    "not_null": bool(row[3]),
                    "default": _default_descriptor(row[4]),
                    "pk_order": int(row[5]),
                    "hidden": int(row[6]) if len(row) > 6 else 0,
                }
            )

        grouped_foreign_keys: dict[
            int,
            list[sqlite3.Row | tuple[object, ...]],
        ] = {}
        for row in connection.execute(
            f"PRAGMA foreign_key_list({_quote(table)})"
        ).fetchall():
            grouped_foreign_keys.setdefault(int(row[0]), []).append(row)
        foreign_keys = []
        for rows in grouped_foreign_keys.values():
            ordered = sorted(rows, key=lambda item: int(item[1]))
            foreign_keys.append(
                {
                    "child_columns": [str(item[3]) for item in ordered],
                    "parent_table": str(ordered[0][2]),
                    "parent_columns": [str(item[4]) for item in ordered],
                    "on_update": str(ordered[0][5]).upper(),
                    "on_delete": str(ordered[0][6]).upper(),
                    "match": str(ordered[0][7]).upper(),
                }
            )

        indexes = []
        for row in connection.execute(
            f"PRAGMA index_list({_quote(table)})"
        ).fetchall():
            index_name = str(row[1])
            ordered_columns = []
            for index_row in connection.execute(
                f"PRAGMA index_xinfo({_quote(index_name)})"
            ).fetchall():
                if int(index_row[5]) != 1:
                    continue
                ordered_columns.append(
                    {
                        "name": (
                            None
                            if index_row[2] is None
                            else str(index_row[2])
                        ),
                        "descending": bool(index_row[3]),
                        "collation": (
                            None
                            if index_row[4] is None
                            else str(index_row[4])
                        ),
                    }
                )
            sql_row = connection.execute(
                "SELECT COALESCE(sql, '') FROM sqlite_schema "
                "WHERE type = 'index' AND name = ?",
                (index_name,),
            ).fetchone()
            index_sql = str(sql_row[0] if sql_row else "")
            index = {
                "name": index_name,
                "unique": bool(row[2]),
                "origin": str(row[3]),
                "partial": bool(row[4]),
                "ordered_columns": ordered_columns,
                "sql_hash": _text_sha256(index_sql),
            }
            indexes.append(index)
            global_indexes[index_name] = {**index, "table": table}
        tables[table] = {
            "columns": columns,
            "foreign_keys": foreign_keys,
            "indexes": sorted(indexes, key=lambda item: str(item["name"])),
            "definition_hash": _text_sha256(raw_sql),
        }

    triggers = [
        {
            "name": str(row[0]),
            "table": str(row[1]),
            "sql_hash": _text_sha256(str(row[2])),
        }
        for row in connection.execute(
            "SELECT name, tbl_name, COALESCE(sql, '') FROM sqlite_schema "
            "WHERE type = 'trigger' ORDER BY name"
        ).fetchall()
    ]
    views = [
        {
            "name": str(row[0]),
            "sql_hash": _text_sha256(str(row[1])),
        }
        for row in connection.execute(
            "SELECT name, COALESCE(sql, '') FROM sqlite_schema "
            "WHERE type = 'view' ORDER BY name"
        ).fetchall()
    ]
    descriptor: dict[str, object] = {
        "schema_markers": markers,
        "tables": tables,
        "indexes": dict(sorted(global_indexes.items())),
        "triggers": triggers,
        "views": views,
    }
    descriptor["required_objects"] = {
        "tables": sorted(REQUIRED_TABLES[STORE_SCHEDULER]),
        "indexes": sorted(global_indexes),
        "triggers": [str(item["name"]) for item in triggers],
        "views": [str(item["name"]) for item in views],
    }
    descriptor["schema_fingerprint"] = _text_sha256(
        json.dumps(
            descriptor,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return descriptor


@lru_cache(maxsize=1)
def _expected_scheduler_schema_descriptor() -> dict[str, object]:
    connection = open_sqlite_read_write(
        Path(":memory:"),
        store=STORE_SCHEDULER,
        timeout_seconds=DEFAULT_MIGRATION_TIMEOUT_SECONDS,
    )
    try:
        connection.executescript(SCHEDULER_SCHEMA_SQL)
        return _scheduler_schema_descriptor(connection)
    finally:
        connection.close()


def _table_semantics(
    descriptor: dict[str, object],
    table: str,
    *,
    foreign_keys: object | None = None,
) -> dict[str, object] | None:
    tables = descriptor["tables"]
    if not isinstance(tables, dict):
        raise TypeError("invalid scheduler schema descriptor")
    value = tables.get(table)
    if not isinstance(value, dict):
        return None
    return {
        "columns": value["columns"],
        "foreign_keys": (
            value["foreign_keys"]
            if foreign_keys is None
            else foreign_keys
        ),
        "indexes": value["indexes"],
    }


def _scheduler_schema_profile(
    connection: sqlite3.Connection,
) -> SchedulerSchemaProfile:
    current = _scheduler_schema_descriptor(connection)
    expected = _expected_scheduler_schema_descriptor()
    current_tables = current["tables"]
    expected_tables = expected["tables"]
    if not isinstance(current_tables, dict) or not isinstance(
        expected_tables,
        dict,
    ):
        raise TypeError("invalid scheduler schema descriptor")

    expected_names = set(expected_tables)
    current_names = set(current_tables)
    common_names = expected_names - {"run_jobs"}
    markers_match = (
        current["schema_markers"] == expected["schema_markers"]
    )

    common_exact = all(
        _table_semantics(current, table)
        == _table_semantics(expected, table)
        for table in common_names - {"scheduler_queue_items"}
    )
    queue_legacy_exact = _table_semantics(
        current,
        "scheduler_queue_items",
    ) == _table_semantics(
        expected,
        "scheduler_queue_items",
        foreign_keys=[],
    )
    queue_ready_exact = _table_semantics(
        current,
        "scheduler_queue_items",
    ) == _table_semantics(expected, "scheduler_queue_items")
    run_jobs_exact = _table_semantics(
        current,
        "run_jobs",
    ) == _table_semantics(expected, "run_jobs")

    ready = bool(
        markers_match
        and expected_names.issubset(current_names)
        and common_exact
        and queue_ready_exact
        and run_jobs_exact
    )
    v1 = bool(
        markers_match
        and expected_names.issubset(current_names)
        and common_exact
        and queue_legacy_exact
        and run_jobs_exact
    )
    v2 = bool(
        markers_match
        and current_names == common_names
        and common_exact
        and queue_legacy_exact
        and current["triggers"] == expected["triggers"]
        and current["views"] == expected["views"]
    )

    extra_tables = current_names - expected_names
    unexpected_objects = (
        len(extra_tables)
        + len(current["triggers"])
        + len(current["views"])
    )
    conflicting_objects = sum(
        1
        for table in expected_names & current_names
        if table == "scheduler_queue_items"
        and not (queue_ready_exact or queue_legacy_exact)
        or table == "run_jobs"
        and not run_jobs_exact
        or table not in {"scheduler_queue_items", "run_jobs"}
        and _table_semantics(current, table)
        != _table_semantics(expected, table)
    )

    if ready:
        classification = SCHEMA_CLASSIFICATION_SCHEDULER_READY
        migration_id = SCHEDULER_SCHEMA_RECONCILIATION_ID
        missing_additive_objects: tuple[str, ...] = ()
        queue_rebuild_required = False
    elif v1:
        classification = SCHEMA_CLASSIFICATION_SCHEDULER_V1
        migration_id = MIGRATION_IDS[STORE_SCHEDULER]
        missing_additive_objects = ()
        queue_rebuild_required = True
    elif v2:
        classification = SCHEMA_CLASSIFICATION_SCHEDULER_V2
        migration_id = SCHEDULER_SCHEMA_RECONCILIATION_ID
        missing_additive_objects = RUN_JOBS_ADDITIVE_OBJECTS
        queue_rebuild_required = True
    else:
        classification = SCHEMA_CLASSIFICATION_UNSUPPORTED
        migration_id = MIGRATION_IDS[STORE_SCHEDULER]
        missing_additive_objects = ()
        queue_rebuild_required = False

    return SchedulerSchemaProfile(
        classification=classification,
        migration_id=migration_id,
        structural_fingerprint=str(current["schema_fingerprint"]),
        expected_fingerprint=str(expected["schema_fingerprint"]),
        current_table_count=len(current_names),
        expected_table_count=len(expected_names),
        missing_additive_objects=missing_additive_objects,
        queue_rebuild_required=queue_rebuild_required,
        conflicting_object_count=conflicting_objects,
        unexpected_object_count=unexpected_objects,
        schema_compatible=ready or v1 or v2,
    )


def _orphan_count(
    connection: sqlite3.Connection,
    relationship: RequiredForeignKey,
) -> int:
    child_table = _quote(relationship.child_table)
    parent_table = _quote(relationship.parent_table)
    joins = " AND ".join(
        f"child.{_quote(child)} = parent.{_quote(parent)}"
        for child, parent in zip(
            relationship.child_columns,
            relationship.parent_columns,
            strict=True,
        )
    )
    missing_parent = " AND ".join(
        f"parent.{_quote(parent)} IS NULL"
        for parent in relationship.parent_columns
    )
    row = connection.execute(
        f"SELECT COUNT(*) FROM {child_table} AS child "
        f"LEFT JOIN {parent_table} AS parent ON {joins} "
        f"WHERE {missing_parent}"
    ).fetchone()
    return int(row[0] if row is not None else 0)


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(
            f"PRAGMA table_info({_quote(table)})"
        ).fetchall()
    }


def _relation_audits(
    connection: sqlite3.Connection,
    *,
    store: str,
) -> tuple[RelationAudit, ...]:
    tables = sqlite_user_tables(connection)
    columns_by_table: dict[str, set[str]] = {}

    def columns(table: str) -> set[str]:
        if table not in columns_by_table:
            columns_by_table[table] = _table_columns(connection, table)
        return columns_by_table[table]

    results: list[RelationAudit] = []
    for relationship in REQUIRED_FOREIGN_KEYS[store]:
        if relationship.child_table not in tables:
            results.append(
                RelationAudit(
                    relationship.relationship_id,
                    RELATION_CHILD_TABLE_MISSING,
                    None,
                )
            )
            continue
        if not set(relationship.child_columns).issubset(
            columns(relationship.child_table)
        ):
            results.append(
                RelationAudit(
                    relationship.relationship_id,
                    RELATION_CHILD_COLUMN_MISSING,
                    None,
                )
            )
            continue
        if relationship.parent_table not in tables:
            results.append(
                RelationAudit(
                    relationship.relationship_id,
                    RELATION_PARENT_TABLE_MISSING,
                    None,
                )
            )
            continue
        if not set(relationship.parent_columns).issubset(
            columns(relationship.parent_table)
        ):
            results.append(
                RelationAudit(
                    relationship.relationship_id,
                    RELATION_PARENT_COLUMN_MISSING,
                    None,
                )
            )
            continue
        results.append(
            RelationAudit(
                relationship.relationship_id,
                RELATION_CHECKED,
                _orphan_count(connection, relationship),
            )
        )
    return tuple(results)


def _quick_check_ok(connection: sqlite3.Connection) -> bool:
    cursor = connection.execute("PRAGMA quick_check")
    first = cursor.fetchone()
    second = cursor.fetchone()
    return (
        first is not None
        and second is None
        and str(first[0]).lower() == "ok"
    )


def _foreign_key_violation_count(connection: sqlite3.Connection) -> int:
    return sum(1 for _row in connection.execute("PRAGMA foreign_key_check"))


def _required_relationship_contract(store: str) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "child_table": item.child_table,
            "child_columns": item.child_columns,
            "parent_table": item.parent_table,
            "parent_columns": item.parent_columns,
            "on_delete": item.on_delete,
            "on_update": item.on_update,
        }
        for item in REQUIRED_FOREIGN_KEYS[store]
    )


def _plan_digest_fields(payload: dict[str, object]) -> dict[str, object]:
    """Return only stable migration semantics, never audit observations."""

    stable_keys = (
        "store",
        "migration_id",
        "status",
        "current_declared_fk_count",
        "target_fk_count",
        "missing_relationships",
        "conflicting_relationship_count",
        "unexpected_relationship_count",
        "schema_fingerprint",
        "source_fingerprint",
        "source_size",
        "orphan_counts",
        "total_orphan_count",
        "quick_check_ok",
        "foreign_key_violation_count",
        "migration_required",
        "schema_compatible",
        "backup_required",
    )
    relation_audits = payload.get("relation_audits", ())
    fields = {
        **{key: payload[key] for key in stable_keys},
        "relation_audit_coverage": [
            item.to_payload()
            for item in relation_audits
            if isinstance(item, RelationAudit)
        ],
        "required_relationship_contract": _required_relationship_contract(
            str(payload["store"])
        ),
    }
    if payload["migration_id"] == SCHEDULER_SCHEMA_RECONCILIATION_ID:
        for key in (
            "schema_classification",
            "structural_schema_fingerprint",
            "expected_schema_fingerprint",
            "current_table_count",
            "expected_table_count",
            "missing_additive_objects",
            "queue_rebuild_required",
            "conflicting_object_count",
            "unexpected_object_count",
        ):
            fields[key] = payload[key]
    return fields


def _with_plan_digest(
    payload: dict[str, object],
    *,
    include_digest: bool = True,
) -> MigrationPlan:
    if not include_digest:
        return MigrationPlan(**payload, plan_digest=None)
    encoded = json.dumps(
        _plan_digest_fields(payload),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return MigrationPlan(
        **payload,
        plan_digest=hashlib.sha256(encoded).hexdigest(),
    )


def _empty_plan(
    *,
    store: str,
    status: str,
    issue_code: str,
    source_size: int | None = None,
    source_fingerprint: str | None = None,
    sidecars: tuple[str, ...] = (),
    source_observation_status: str = SOURCE_OBSERVATION_UNSUPPORTED,
    snapshot_strategy: str | None = None,
) -> MigrationPlan:
    return _with_plan_digest(
        {
            "store": store,
            "migration_id": MIGRATION_IDS[store],
            "status": status,
            "current_declared_fk_count": 0,
            "target_fk_count": len(REQUIRED_FOREIGN_KEYS[store]),
            "missing_relationships": tuple(
                requirement.relationship_id
                for requirement in REQUIRED_FOREIGN_KEYS[store]
            ),
            "conflicting_relationship_count": 0,
            "unexpected_relationship_count": 0,
            "schema_fingerprint": None,
            "source_fingerprint": source_fingerprint,
            "source_size": source_size,
            "sidecars_present": sidecars,
            "source_observation_status": source_observation_status,
            "snapshot_strategy": snapshot_strategy,
            "relation_audits": (),
            "orphan_counts": (),
            "total_orphan_count": 0,
            "quick_check_ok": False,
            "foreign_key_violation_count": 0,
            "migration_required": False,
            "schema_compatible": False,
            "eligible": False,
            "backup_required": False,
            "query_only_enabled": False,
            "foreign_keys_enabled": False,
            "issue_codes": (issue_code,),
            "schema_classification": SCHEMA_CLASSIFICATION_UNSUPPORTED,
            "structural_schema_fingerprint": None,
            "expected_schema_fingerprint": None,
            "current_table_count": 0,
            "expected_table_count": len(REQUIRED_TABLES[store]),
            "missing_additive_objects": (),
            "queue_rebuild_required": False,
            "conflicting_object_count": 0,
            "unexpected_object_count": 0,
        },
        include_digest=False,
    )


def _plan_from_connection(
    connection: sqlite3.Connection,
    *,
    store: str,
    source_size: int,
    source_fingerprint: str,
    sidecars: tuple[str, ...],
    source_observation_status: str,
    snapshot_strategy: str | None,
    query_only_enabled: bool,
) -> MigrationPlan:
    inventory = inspect_required_foreign_keys(connection, store=store)
    schema_fingerprint = _schema_fingerprint(connection)
    scheduler_profile = (
        _scheduler_schema_profile(connection)
        if store == STORE_SCHEDULER
        else None
    )
    quick_ok = _quick_check_ok(connection)
    violations = _foreign_key_violation_count(connection)
    relation_audits = _relation_audits(connection, store=store)
    orphan_counts = tuple(
        (item.relationship_id, item.orphan_count)
        for item in relation_audits
        if item.status == RELATION_CHECKED and item.orphan_count is not None
    )
    total_orphans = sum(count for _, count in orphan_counts)
    issue_codes: list[str] = []
    if scheduler_profile is None:
        migration_id = MIGRATION_IDS[store]
        schema_classification = SCHEMA_CLASSIFICATION_METADATA_V1
        structural_schema_fingerprint = None
        expected_schema_fingerprint = None
        current_table_count = len(sqlite_user_tables(connection))
        expected_table_count = len(REQUIRED_TABLES[store])
        missing_additive_objects: tuple[str, ...] = ()
        queue_rebuild_required = bool(inventory.missing_relationships)
        conflicting_object_count = 0
        unexpected_object_count = 0
        profile_schema_compatible = inventory.status in {
            STATUS_READY,
            STATUS_MIGRATION_REQUIRED,
        }
        profile_migration_required = (
            inventory.status == STATUS_MIGRATION_REQUIRED
        )
    else:
        migration_id = scheduler_profile.migration_id
        schema_classification = scheduler_profile.classification
        structural_schema_fingerprint = (
            scheduler_profile.structural_fingerprint
        )
        expected_schema_fingerprint = scheduler_profile.expected_fingerprint
        current_table_count = scheduler_profile.current_table_count
        expected_table_count = scheduler_profile.expected_table_count
        missing_additive_objects = (
            scheduler_profile.missing_additive_objects
        )
        queue_rebuild_required = scheduler_profile.queue_rebuild_required
        conflicting_object_count = (
            scheduler_profile.conflicting_object_count
        )
        unexpected_object_count = scheduler_profile.unexpected_object_count
        profile_schema_compatible = scheduler_profile.schema_compatible
        profile_migration_required = scheduler_profile.classification in {
            SCHEMA_CLASSIFICATION_SCHEDULER_V1,
            SCHEMA_CLASSIFICATION_SCHEDULER_V2,
        }

    status = inventory.status
    if not quick_ok:
        status = STATUS_INTEGRITY_FAILED
        issue_codes.append(CODE_INTEGRITY_CHECK_FAILED)
    elif inventory.status == STATUS_UNINITIALIZED:
        issue_codes.append(CODE_SCHEMA_MISMATCH)
    elif not profile_schema_compatible:
        status = STATUS_SCHEMA_MISMATCH
        issue_codes.append(CODE_SCHEMA_MISMATCH)
    elif inventory.status == STATUS_SCHEMA_MISMATCH:
        status = STATUS_SCHEMA_MISMATCH
        issue_codes.append(CODE_SCHEMA_MISMATCH)
    elif total_orphans or violations:
        status = STATUS_ORPHANS_DETECTED
        issue_codes.append(CODE_ORPHANS_DETECTED)
    elif profile_migration_required:
        status = STATUS_MIGRATION_REQUIRED
        issue_codes.append(CODE_MIGRATION_REQUIRED)
    else:
        status = STATUS_READY
    if source_observation_status != SOURCE_OBSERVATION_NONE:
        issue_codes.append(CODE_SOURCE_SIDECARS_PRESENT)

    migration_required = profile_migration_required
    schema_compatible = profile_schema_compatible
    missing_tables_allowed = bool(
        store == STORE_SCHEDULER
        and schema_classification == SCHEMA_CLASSIFICATION_SCHEDULER_V2
        and inventory.missing_tables == ("run_jobs",)
    )
    backup_required = bool(
        migration_required
        and status == STATUS_MIGRATION_REQUIRED
        and quick_ok
        and violations == 0
        and total_orphans == 0
        and inventory.conflicting_count == 0
        and inventory.unexpected_count == 0
        and (not inventory.missing_tables or missing_tables_allowed)
        and all(item.status == RELATION_CHECKED for item in relation_audits)
    )
    eligible = bool(
        backup_required
        and source_observation_status == SOURCE_OBSERVATION_NONE
    )
    return _with_plan_digest(
        {
            "store": store,
            "migration_id": migration_id,
            "status": status,
            "current_declared_fk_count": inventory.declared_count,
            "target_fk_count": inventory.expected_count,
            "missing_relationships": inventory.missing_relationships,
            "conflicting_relationship_count": inventory.conflicting_count,
            "unexpected_relationship_count": inventory.unexpected_count,
            "schema_fingerprint": schema_fingerprint,
            "source_fingerprint": source_fingerprint,
            "source_size": source_size,
            "sidecars_present": sidecars,
            "source_observation_status": source_observation_status,
            "snapshot_strategy": snapshot_strategy,
            "relation_audits": relation_audits,
            "orphan_counts": orphan_counts,
            "total_orphan_count": total_orphans,
            "quick_check_ok": quick_ok,
            "foreign_key_violation_count": violations,
            "migration_required": migration_required,
            "schema_compatible": schema_compatible,
            "eligible": eligible,
            "backup_required": backup_required,
            "query_only_enabled": query_only_enabled,
            "foreign_keys_enabled": True,
            "issue_codes": tuple(dict.fromkeys(issue_codes)),
            "schema_classification": schema_classification,
            "structural_schema_fingerprint": structural_schema_fingerprint,
            "expected_schema_fingerprint": expected_schema_fingerprint,
            "current_table_count": current_table_count,
            "expected_table_count": expected_table_count,
            "missing_additive_objects": missing_additive_objects,
            "queue_rebuild_required": queue_rebuild_required,
            "conflicting_object_count": conflicting_object_count,
            "unexpected_object_count": unexpected_object_count,
        }
    )


def _classify_sqlite_failure(exc: sqlite3.Error) -> str:
    cause = exc.__cause__
    classified = cause if isinstance(cause, sqlite3.Error) else exc
    if isinstance(classified, sqlite3.OperationalError):
        text = str(classified).lower()
        if "locked" in text or "busy" in text:
            return STATUS_BUSY
        return STATUS_UNREADABLE
    return STATUS_MALFORMED


def plan_sqlite_foreign_key_migration(
    *,
    store: str,
    path: Path,
    timeout_seconds: float = DEFAULT_MIGRATION_TIMEOUT_SECONDS,
) -> MigrationPlan:
    """Build a deterministic, count-only, no-write migration plan."""
    resolved_store = _validate_store(store)
    if not path.exists():
        return _empty_plan(
            store=resolved_store,
            status=STATUS_MISSING,
            issue_code=CODE_STORE_MISSING,
        )
    source_size: int | None = None
    source_fingerprint: str | None = None
    sidecars: tuple[str, ...] = ()
    source_observation_status = SOURCE_OBSERVATION_UNSUPPORTED
    try:
        with capture_stable_sqlite_snapshot(path) as snapshot:
            source_size = snapshot.source_fingerprint.main.size
            source_fingerprint = snapshot.source_fingerprint.main.sha256
            sidecars = snapshot.source_observation.sidecars_present
            source_observation_status = snapshot.source_observation.status
            connection = open_immutable_snapshot(
                snapshot,
                store=resolved_store,
                timeout_seconds=timeout_seconds,
            )
            try:
                plan = _plan_from_connection(
                    connection,
                    store=resolved_store,
                    source_size=source_size,
                    source_fingerprint=source_fingerprint,
                    sidecars=sidecars,
                    source_observation_status=source_observation_status,
                    snapshot_strategy=SNAPSHOT_STRATEGY,
                    query_only_enabled=True,
                )
            finally:
                connection.close()
    except SqliteSnapshotError as exc:
        blocked = exc.observation_status != SOURCE_OBSERVATION_UNSUPPORTED
        return _empty_plan(
            store=resolved_store,
            status=STATUS_BUSY if blocked else STATUS_UNREADABLE,
            issue_code=exc.code or CODE_AUDIT_FAILED,
            source_size=exc.source_size,
            source_fingerprint=exc.source_sha256,
            sidecars=exc.sidecars_present,
            source_observation_status=exc.observation_status,
            snapshot_strategy=SNAPSHOT_STRATEGY,
        )
    except (FileNotFoundError, OSError):
        return _empty_plan(
            store=resolved_store,
            status=STATUS_UNREADABLE,
            issue_code="sqlite_store_unavailable",
            source_size=source_size,
            source_fingerprint=source_fingerprint,
            sidecars=sidecars,
            source_observation_status=source_observation_status,
            snapshot_strategy=SNAPSHOT_STRATEGY,
        )
    except SqliteIntegrityError as exc:
        status = _classify_sqlite_failure(exc)
        code = CODE_MIGRATION_BUSY if status == STATUS_BUSY else exc.code
        return _empty_plan(
            store=resolved_store,
            status=status,
            issue_code=code,
            source_size=source_size,
            source_fingerprint=source_fingerprint,
            sidecars=sidecars,
            source_observation_status=source_observation_status,
            snapshot_strategy=SNAPSHOT_STRATEGY,
        )
    except sqlite3.Error as exc:
        status = _classify_sqlite_failure(exc)
        code = CODE_MIGRATION_BUSY if status == STATUS_BUSY else "sqlite_store_unavailable"
        return _empty_plan(
            store=resolved_store,
            status=status,
            issue_code=code,
            source_size=source_size,
            source_fingerprint=source_fingerprint,
            sidecars=sidecars,
            source_observation_status=source_observation_status,
            snapshot_strategy=SNAPSHOT_STRATEGY,
        )
    return plan


def audit_sqlite_integrity(
    *,
    store: str,
    path: Path,
    timeout_seconds: float = DEFAULT_MIGRATION_TIMEOUT_SECONDS,
) -> IntegrityAudit:
    """Run the explicit read-only integrity audit; never used by readiness."""
    plan = plan_sqlite_foreign_key_migration(
        store=store,
        path=path,
        timeout_seconds=timeout_seconds,
    )
    incomplete = {
        STATUS_MISSING,
        STATUS_MALFORMED,
        STATUS_BUSY,
        STATUS_UNREADABLE,
    }
    return IntegrityAudit(
        store=plan.store,
        status=plan.status,
        complete=plan.status not in incomplete,
        read_only=True,
        query_only_enabled=plan.query_only_enabled,
        foreign_keys_enabled=plan.foreign_keys_enabled,
        declared_fk_count=plan.current_declared_fk_count,
        expected_fk_count=plan.target_fk_count,
        missing_fk_count=len(plan.missing_relationships),
        conflicting_fk_count=(
            plan.conflicting_relationship_count
            + plan.unexpected_relationship_count
        ),
        source_observation_status=plan.source_observation_status,
        snapshot_strategy=plan.snapshot_strategy,
        relation_audits=plan.relation_audits,
        orphan_count=plan.total_orphan_count,
        quick_check_ok=plan.quick_check_ok,
        foreign_key_violation_count=plan.foreign_key_violation_count,
        migration_required=plan.migration_required,
        issue_codes=plan.issue_codes,
    )


def _normalize_table_sql(sql: str) -> str:
    collapsed = " ".join(str(sql or "").strip().rstrip(";").split()).lower()
    return re.sub(
        r"^create\s+table\s+(?:if\s+not\s+exists\s+)?"
        r"(?:\"[^\"]+\"|`[^`]+`|\[[^\]]+\]|[^\s(]+)",
        "create table <table>",
        collapsed,
        count=1,
    )


def _table_sql(connection: sqlite3.Connection, table: str) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if row is None or not row[0]:
        raise SqliteSchemaMismatch("sqlite")
    return str(row[0])


def _schema_objects(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[tuple[str, str, str], ...]:
    rows = connection.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE tbl_name = ? AND type IN ('index', 'trigger') "
        "AND sql IS NOT NULL ORDER BY type, name",
        (table,),
    ).fetchall()
    return tuple((str(row[0]), str(row[1]), str(row[2])) for row in rows)


def _views(connection: sqlite3.Connection) -> tuple[tuple[str, str], ...]:
    rows = connection.execute(
        "SELECT name, COALESCE(sql, '') FROM sqlite_master "
        "WHERE type = 'view' ORDER BY rowid"
    ).fetchall()
    return tuple((str(row[0]), str(row[1])) for row in rows)


@lru_cache(maxsize=1)
def _authoritative_scheduler_statements() -> dict[str, str]:
    statements: dict[str, str] = {}
    buffer: list[str] = []
    for line in SCHEDULER_SCHEMA_SQL.splitlines(keepends=True):
        buffer.append(line)
        candidate = "".join(buffer).strip()
        if not candidate or not sqlite3.complete_statement(candidate):
            continue
        match = re.search(
            r"CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX)\s+"
            r"IF\s+NOT\s+EXISTS\s+([a-z0-9_]+)",
            candidate,
            flags=re.IGNORECASE,
        )
        if match is not None:
            statements[match.group(1).lower()] = candidate
        buffer = []
    if "".join(buffer).strip():
        raise RuntimeError("incomplete authoritative scheduler schema")
    expected = {
        "scheduler_queue_items",
        "idx_scheduler_queue_status",
        "idx_scheduler_queue_schedule",
        "run_jobs",
        "idx_run_jobs_active_run",
        "idx_run_jobs_claim",
        "idx_run_jobs_lease",
    }
    if not expected.issubset(statements):
        raise RuntimeError("authoritative scheduler schema is incomplete")
    return statements


def _quote_existing_identifier(identifier: str) -> str:
    escaped = identifier.replace('"', '""')
    return f'"{escaped}"'


def _safe_cell(value: object) -> object:
    if value is None:
        return ["null"]
    if isinstance(value, bytes):
        return ["blob", value.hex()]
    if isinstance(value, bool):
        return ["integer", int(value)]
    if isinstance(value, int):
        return ["integer", value]
    if isinstance(value, float):
        return ["real", value.hex()]
    return ["text", str(value)]


def _table_content_snapshot(
    connection: sqlite3.Connection,
    spec: TableMigrationSpec,
) -> tuple[int, str]:
    return _table_content_snapshot_columns(
        connection,
        table=spec.table,
        columns=spec.columns,
    )


def _table_content_snapshot_columns(
    connection: sqlite3.Connection,
    *,
    table: str,
    columns: tuple[str, ...],
) -> tuple[int, str]:
    selected_columns = ", ".join(_quote(column) for column in columns)
    rows = connection.execute(
        f"SELECT rowid, {selected_columns} "
        f"FROM {_quote(table)} ORDER BY rowid"
    )
    digest = hashlib.sha256()
    row_count = 0
    for row in rows:
        row_count += 1
        encoded = json.dumps(
            [_safe_cell(value) for value in row],
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        digest.update(encoded)
        digest.update(b"\n")
    return row_count, digest.hexdigest()


def _inject(fail_at: str | None, phase: str) -> None:
    if fail_at == phase:
        raise _InjectedMigrationFailure(phase)


def _rollback_quietly(connection: sqlite3.Connection | None) -> None:
    if connection is None or not connection.in_transaction:
        return
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _backup_directory(path: Path) -> Path:
    return path.parent / ".sqlite-fk-backups"


def _migration_lock_path(path: Path) -> Path:
    return path.parent / f".{path.name}.fk-migration.lock"


def _create_verified_backup(
    *,
    store: str,
    path: Path,
    migration_id: str,
    serialized: bytes,
) -> tuple[str, str]:
    try:
        backup_dir = _backup_directory(path)
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_id = (
            f"{store}-{migration_id}-{stamp}-{uuid.uuid4().hex[:12]}"
        )
        backup_path = backup_dir / f"{backup_id}.sqlite3"
        with backup_path.open("xb") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        backup_hash = _sha256_file(backup_path)
        _verify_persistent_backup_from_temporary_snapshot(
            store=store,
            backup_path=backup_path,
            expected_size=len(serialized),
            expected_sha256=backup_hash,
        )
        return backup_id, backup_hash
    except SqliteMigrationError:
        raise
    except (
        OSError,
        sqlite3.Error,
        SqliteIntegrityError,
        SqliteSnapshotError,
    ) as exc:
        raise SqliteForeignKeyBackupFailed(store) from exc


def _require_pristine_backup_fingerprint(
    *,
    store: str,
    fingerprint: SqliteSourceFingerprint,
    expected_size: int,
    expected_sha256: str,
) -> None:
    if (
        fingerprint.sidecars_present
        or fingerprint.main.size != expected_size
        or fingerprint.main.sha256 != expected_sha256
    ):
        raise SqliteForeignKeyBackupFailed(store)


def _verify_persistent_backup_from_temporary_snapshot(
    *,
    store: str,
    backup_path: Path,
    expected_size: int,
    expected_sha256: str,
) -> None:
    try:
        _verify_persistent_backup_from_temporary_snapshot_unchecked(
            store=store,
            backup_path=backup_path,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )
    except SqliteForeignKeyBackupFailed:
        raise
    except (
        OSError,
        sqlite3.Error,
        SqliteIntegrityError,
        SqliteSnapshotError,
    ) as exc:
        raise SqliteForeignKeyBackupFailed(store) from exc


def _verify_persistent_backup_from_temporary_snapshot_unchecked(
    *,
    store: str,
    backup_path: Path,
    expected_size: int,
    expected_sha256: str,
) -> None:
    initial = capture_sqlite_source_fingerprint(backup_path)
    _require_pristine_backup_fingerprint(
        store=store,
        fingerprint=initial,
        expected_size=expected_size,
        expected_sha256=expected_sha256,
    )

    with capture_stable_sqlite_snapshot(backup_path) as snapshot:
        if (
            snapshot.source_observation.status != SOURCE_OBSERVATION_NONE
            or snapshot.source_fingerprint != initial
        ):
            raise SqliteForeignKeyBackupFailed(store)
        _require_pristine_backup_fingerprint(
            store=store,
            fingerprint=capture_sqlite_source_fingerprint(
                snapshot.snapshot_path
            ),
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )

        verification = open_immutable_snapshot(
            snapshot,
            store=store,
            timeout_seconds=DEFAULT_MIGRATION_TIMEOUT_SECONDS,
        )
        try:
            query_only = verification.execute(
                "PRAGMA query_only"
            ).fetchone()
            foreign_keys = verification.execute(
                "PRAGMA foreign_keys"
            ).fetchone()
            if (
                query_only is None
                or int(query_only[0]) != 1
                or foreign_keys is None
                or int(foreign_keys[0]) != 1
                or not _quick_check_ok(verification)
            ):
                raise SqliteForeignKeyBackupFailed(store)
        finally:
            verification.close()

        temporary_final = capture_sqlite_source_fingerprint(
            snapshot.snapshot_path
        )
        _require_pristine_backup_fingerprint(
            store=store,
            fingerprint=temporary_final,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )
        if temporary_final.main != snapshot.snapshot_fingerprint:
            raise SqliteForeignKeyBackupFailed(store)

    persistent_final = capture_sqlite_source_fingerprint(backup_path)
    _require_pristine_backup_fingerprint(
        store=store,
        fingerprint=persistent_final,
        expected_size=expected_size,
        expected_sha256=expected_sha256,
    )
    if persistent_final != initial:
        raise SqliteForeignKeyBackupFailed(store)


def _serialize_source_for_backup(
    connection: sqlite3.Connection,
    *,
    store: str,
) -> bytes:
    try:
        return connection.serialize()
    except sqlite3.Error as exc:
        if _classify_sqlite_failure(exc) == STATUS_BUSY:
            raise SqliteForeignKeyMigrationBusy(
                store,
                phase=BUSY_PHASE_BACKUP_SNAPSHOT,
            ) from exc
        raise SqliteForeignKeyBackupFailed(store) from exc


def _raise_for_ineligible_plan(
    plan: MigrationPlan,
    *,
    expected_plan_digest: str,
    busy_phase: str,
) -> None:
    if plan.source_observation_status != SOURCE_OBSERVATION_NONE:
        raise SqliteForeignKeyMigrationBusy(
            plan.store,
            phase=busy_phase,
        )
    if plan.status == STATUS_BUSY:
        raise SqliteForeignKeyMigrationBusy(
            plan.store,
            phase=busy_phase,
        )
    if plan.plan_digest != expected_plan_digest:
        raise SqliteForeignKeyPlanStale(plan.store)
    if plan.status == STATUS_READY:
        return
    if plan.total_orphan_count or plan.status == STATUS_ORPHANS_DETECTED:
        raise SqliteForeignKeyOrphansDetected(plan.store)
    if plan.status == STATUS_MISSING:
        raise SqliteStoreMissing(plan.store)
    if plan.status in {STATUS_SCHEMA_MISMATCH, STATUS_UNINITIALIZED}:
        raise SqliteSchemaMismatch(plan.store)
    if not plan.quick_check_ok:
        raise SqliteIntegrityCheckFailed(plan.store)
    if not plan.eligible:
        raise SqliteMigrationError(plan.store)


def _tables_to_rebuild(
    store: str,
    missing_relationships: tuple[str, ...],
) -> tuple[TableMigrationSpec, ...]:
    missing = set(missing_relationships)
    by_table = {
        requirement.child_table: requirement.relationship_id
        for requirement in REQUIRED_FOREIGN_KEYS[store]
    }
    return tuple(
        spec
        for spec in TABLE_MIGRATIONS[store]
        if by_table[spec.table] in missing
    )


def _reopen_with_connection_policy(
    *,
    store: str,
    path: Path,
    timeout_seconds: float,
) -> ForeignKeyInventory:
    connection = open_sqlite_read_write(
        path,
        store=store,
        timeout_seconds=timeout_seconds,
        isolation_level=None if store == STORE_SCHEDULER else "",
    )
    try:
        return require_store_write_compatible(connection, store=store)
    finally:
        connection.close()


def _scheduler_table_columns(table: str) -> tuple[str, ...]:
    descriptor = _expected_scheduler_schema_descriptor()
    tables = descriptor["tables"]
    if not isinstance(tables, dict):
        raise RuntimeError("invalid authoritative scheduler schema")
    table_descriptor = tables.get(table)
    if not isinstance(table_descriptor, dict):
        raise RuntimeError("missing authoritative scheduler table")
    columns = table_descriptor["columns"]
    if not isinstance(columns, list):
        raise RuntimeError("invalid authoritative scheduler columns")
    return tuple(str(column["name"]) for column in columns)


def _scheduler_v2_content_snapshot(
    connection: sqlite3.Connection,
) -> dict[str, tuple[int, str]]:
    return {
        table: _table_content_snapshot_columns(
            connection,
            table=table,
            columns=_scheduler_table_columns(table),
        )
        for table in (
            "scheduler_schedules",
            "scheduler_worker_heartbeats",
            "scheduler_queue_items",
        )
    }


def _apply_scheduler_v2_schema(
    connection: sqlite3.Connection,
    *,
    fail_at: str | None,
    before_content: dict[str, tuple[int, str]],
) -> None:
    statements = _authoritative_scheduler_statements()
    connection.execute(statements["run_jobs"])
    _inject(fail_at, "after_run_jobs_table_create")

    run_job_indexes = (
        "idx_run_jobs_active_run",
        "idx_run_jobs_claim",
        "idx_run_jobs_lease",
    )
    for index, name in enumerate(run_job_indexes):
        connection.execute(statements[name])
        if index == 0:
            _inject(fail_at, "after_first_run_jobs_index")
    _inject(fail_at, "after_all_run_jobs_indexes")

    legacy_table = "__xrml_schema_v2_scheduler_queue_items"
    existing = connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name = ?",
        (legacy_table,),
    ).fetchone()
    if existing is not None and int(existing[0]) != 0:
        raise SqliteSchemaMismatch(STORE_SCHEDULER)
    connection.execute(
        f"ALTER TABLE {_quote('scheduler_queue_items')} "
        f"RENAME TO {_quote(legacy_table)}"
    )
    _inject(fail_at, "after_queue_legacy_rename")

    connection.execute(statements["scheduler_queue_items"])
    _inject(fail_at, "after_queue_replacement_create")
    columns = _scheduler_table_columns("scheduler_queue_items")
    selected_columns = ", ".join(_quote(column) for column in columns)
    connection.execute(
        f"INSERT INTO {_quote('scheduler_queue_items')} "
        f"(rowid, {selected_columns}) "
        f"SELECT rowid, {selected_columns} FROM {_quote(legacy_table)} "
        "ORDER BY rowid"
    )
    copied = connection.execute(
        f"SELECT COUNT(*) FROM {_quote('scheduler_queue_items')}"
    ).fetchone()
    if (
        copied is None
        or int(copied[0])
        != before_content["scheduler_queue_items"][0]
    ):
        raise SqliteForeignKeyVerificationFailed(STORE_SCHEDULER)
    _inject(fail_at, "after_queue_copy")

    connection.execute(f"DROP TABLE {_quote(legacy_table)}")
    _inject(fail_at, "after_queue_legacy_drop")
    connection.execute(statements["idx_scheduler_queue_status"])
    connection.execute(statements["idx_scheduler_queue_schedule"])
    _inject(fail_at, "after_queue_index_recreation")


def _verify_scheduler_v2_schema(
    connection: sqlite3.Connection,
    *,
    fail_at: str | None,
    before_content: dict[str, tuple[int, str]],
) -> None:
    _inject(fail_at, "before_schema_fingerprint_verification")
    profile = _scheduler_schema_profile(connection)
    if (
        profile.classification
        != SCHEMA_CLASSIFICATION_SCHEDULER_READY
        or profile.structural_fingerprint != profile.expected_fingerprint
    ):
        raise SqliteForeignKeyVerificationFailed(STORE_SCHEDULER)
    if _scheduler_v2_content_snapshot(connection) != before_content:
        raise SqliteForeignKeyVerificationFailed(STORE_SCHEDULER)
    row = connection.execute(
        f"SELECT COUNT(*) FROM {_quote('run_jobs')}"
    ).fetchone()
    if row is None or int(row[0]) != 0:
        raise SqliteForeignKeyVerificationFailed(STORE_SCHEDULER)


def apply_sqlite_foreign_key_migration(
    *,
    store: str,
    path: Path,
    expected_plan_digest: str,
    timeout_seconds: float = DEFAULT_MIGRATION_TIMEOUT_SECONDS,
    fail_at: str | None = None,
) -> MigrationApplyResult:
    """Apply one exact migration after explicit plan-digest confirmation."""
    resolved_store = _validate_store(store)
    if fail_at is not None and fail_at not in ALL_FAILURE_PHASES:
        raise ValueError("unsupported failure injection phase")
    initial = plan_sqlite_foreign_key_migration(
        store=resolved_store,
        path=path,
        timeout_seconds=timeout_seconds,
    )
    _raise_for_ineligible_plan(
        initial,
        expected_plan_digest=expected_plan_digest,
        busy_phase=BUSY_PHASE_SOURCE_REVALIDATION,
    )
    allowed_failure_phases = (
        SCHEDULER_V2_FAILURE_PHASES
        if initial.migration_id == SCHEDULER_SCHEMA_RECONCILIATION_ID
        and initial.status != STATUS_READY
        else FAILURE_PHASES
    )
    if fail_at is not None and fail_at not in allowed_failure_phases:
        raise ValueError("failure injection phase does not apply")
    if initial.status == STATUS_READY:
        return MigrationApplyResult(
            store=resolved_store,
            migration_id=initial.migration_id,
            status="already_compliant",
            applied=False,
            rebuilt_table_count=0,
            backup_id=None,
            backup_sha256=None,
            plan_digest=initial.plan_digest,
            declared_fk_count=initial.current_declared_fk_count,
            foreign_key_violation_count=initial.foreign_key_violation_count,
            quick_check_ok=initial.quick_check_ok,
        )
    connection = None
    backup_id: str | None = None
    backup_hash: str | None = None
    rebuilt_specs: tuple[TableMigrationSpec, ...] = ()
    applied_migration_id = initial.migration_id
    scheduler_v2_before_content: dict[str, tuple[int, str]] = {}
    try:
        with file_lock(
            _migration_lock_path(path),
            timeout_seconds=timeout_seconds,
        ):
            locked_plan = plan_sqlite_foreign_key_migration(
                store=resolved_store,
                path=path,
                timeout_seconds=timeout_seconds,
            )
            _raise_for_ineligible_plan(
                locked_plan,
                expected_plan_digest=expected_plan_digest,
                busy_phase=BUSY_PHASE_SOURCE_REVALIDATION,
            )
            if locked_plan.status == STATUS_READY:
                return MigrationApplyResult(
                    store=resolved_store,
                    migration_id=locked_plan.migration_id,
                    status="already_compliant",
                    applied=False,
                    rebuilt_table_count=0,
                    backup_id=None,
                    backup_sha256=None,
                    plan_digest=locked_plan.plan_digest,
                    declared_fk_count=locked_plan.current_declared_fk_count,
                    foreign_key_violation_count=locked_plan.foreign_key_violation_count,
                    quick_check_ok=locked_plan.quick_check_ok,
                )
            connection = open_sqlite_read_write(
                path,
                store=resolved_store,
                timeout_seconds=timeout_seconds,
                isolation_level=None,
            )
            try:
                connection.execute("BEGIN EXCLUSIVE")
            except sqlite3.Error as exc:
                raise SqliteForeignKeyMigrationBusy(
                    resolved_store,
                    phase=BUSY_PHASE_EXCLUSIVE_TRANSACTION,
                ) from exc

            serialized_source = _serialize_source_for_backup(
                connection,
                store=resolved_store,
            )
            transaction_source_size = len(serialized_source)
            transaction_source_fingerprint = hashlib.sha256(
                serialized_source
            ).hexdigest()
            if (
                transaction_source_size != locked_plan.source_size
                or transaction_source_fingerprint
                != locked_plan.source_fingerprint
            ):
                raise SqliteForeignKeyPlanStale(resolved_store)

            transaction_plan = _plan_from_connection(
                connection,
                store=resolved_store,
                source_size=transaction_source_size,
                source_fingerprint=transaction_source_fingerprint,
                sidecars=(),
                source_observation_status=SOURCE_OBSERVATION_NONE,
                snapshot_strategy=None,
                query_only_enabled=False,
            )
            _raise_for_ineligible_plan(
                transaction_plan,
                expected_plan_digest=expected_plan_digest,
                busy_phase=BUSY_PHASE_SOURCE_REVALIDATION,
            )
            applied_migration_id = transaction_plan.migration_id
            rebuilt_specs = _tables_to_rebuild(
                resolved_store,
                transaction_plan.missing_relationships,
            )
            if not rebuilt_specs:
                raise SqliteMigrationError(resolved_store)

            is_scheduler_v2 = bool(
                resolved_store == STORE_SCHEDULER
                and transaction_plan.migration_id
                == SCHEDULER_SCHEMA_RECONCILIATION_ID
            )
            before_views = _views(connection)
            before_content: dict[str, tuple[int, str]] = {}
            before_objects: dict[str, tuple[tuple[str, str, str], ...]] = {}
            if is_scheduler_v2:
                if tuple(spec.table for spec in rebuilt_specs) != (
                    "scheduler_queue_items",
                ):
                    raise SqliteSchemaMismatch(resolved_store)
                scheduler_v2_before_content = (
                    _scheduler_v2_content_snapshot(connection)
                )
            else:
                for spec in rebuilt_specs:
                    actual_sql = _table_sql(connection, spec.table)
                    if _normalize_table_sql(
                        actual_sql
                    ) != _normalize_table_sql(spec.legacy_ddl):
                        raise SqliteSchemaMismatch(resolved_store)
                    before_content[spec.table] = _table_content_snapshot(
                        connection,
                        spec,
                    )
                    before_objects[spec.table] = _schema_objects(
                        connection,
                        spec.table,
                    )
                    replacement = f"__xrml_fk_v1_{spec.table}"
                    existing = connection.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE name = ?",
                        (replacement,),
                    ).fetchone()
                    if existing is not None and int(existing[0]) != 0:
                        raise SqliteSchemaMismatch(resolved_store)

            backup_id, backup_hash = _create_verified_backup(
                store=resolved_store,
                path=path,
                migration_id=transaction_plan.migration_id,
                serialized=serialized_source,
            )
            if is_scheduler_v2:
                _inject(fail_at, "after_backup_verification")
                _apply_scheduler_v2_schema(
                    connection,
                    fail_at=fail_at,
                    before_content=scheduler_v2_before_content,
                )
                _verify_scheduler_v2_schema(
                    connection,
                    fail_at=fail_at,
                    before_content=scheduler_v2_before_content,
                )
            else:
                # SQLite validates every view during ALTER TABLE. A view that
                # references a child table is temporarily invalid between DROP
                # and RENAME, so preserve and transactionally recreate views.
                for view_name, _view_sql in reversed(before_views):
                    connection.execute(
                        f"DROP VIEW {_quote_existing_identifier(view_name)}"
                    )

                for index, spec in enumerate(rebuilt_specs):
                    replacement = f"__xrml_fk_v1_{spec.table}"
                    connection.execute(
                        spec.target_ddl.format(table=_quote(replacement))
                    )
                    if index == 0:
                        _inject(
                            fail_at,
                            "after_first_replacement_create",
                        )
                    columns = ", ".join(
                        _quote(column) for column in spec.columns
                    )
                    connection.execute(
                        f"INSERT INTO {_quote(replacement)} "
                        f"(rowid, {columns}) "
                        f"SELECT rowid, {columns} "
                        f"FROM {_quote(spec.table)} ORDER BY rowid"
                    )
                    copied = connection.execute(
                        f"SELECT COUNT(*) FROM {_quote(replacement)}"
                    ).fetchone()
                    if (
                        copied is None
                        or int(copied[0])
                        != before_content[spec.table][0]
                    ):
                        raise SqliteForeignKeyVerificationFailed(
                            resolved_store
                        )
                    if index == 0:
                        _inject(fail_at, "after_first_copy")
                    elif index == 1:
                        _inject(fail_at, "after_later_copy")
                    connection.execute(
                        f"DROP TABLE {_quote(spec.table)}"
                    )
                    if index == 0:
                        _inject(fail_at, "after_old_table_drop")
                    connection.execute(
                        f"ALTER TABLE {_quote(replacement)} "
                        f"RENAME TO {_quote(spec.table)}"
                    )
                    if index == 0:
                        _inject(fail_at, "after_rename")
                    for kind, _name, object_sql in before_objects[
                        spec.table
                    ]:
                        if kind == "index":
                            connection.execute(object_sql)

                _inject(fail_at, "after_index_recreation")
                for _view_name, view_sql in before_views:
                    connection.execute(view_sql)
                for spec in rebuilt_specs:
                    for kind, _name, object_sql in before_objects[
                        spec.table
                    ]:
                        if kind == "trigger":
                            connection.execute(object_sql)

            inventory = inspect_required_foreign_keys(
                connection,
                store=resolved_store,
            )
            if not inventory.ready:
                raise SqliteForeignKeyVerificationFailed(resolved_store)
            _inject(fail_at, "before_fk_check")
            if _foreign_key_violation_count(connection) != 0:
                raise SqliteForeignKeyVerificationFailed(resolved_store)
            _inject(fail_at, "before_quick_check")
            if not _quick_check_ok(connection):
                raise SqliteIntegrityCheckFailed(resolved_store)
            if not is_scheduler_v2 and _views(connection) != before_views:
                raise SqliteForeignKeyVerificationFailed(resolved_store)
            for spec in (() if is_scheduler_v2 else rebuilt_specs):
                if (
                    _table_content_snapshot(connection, spec)
                    != before_content[spec.table]
                ):
                    raise SqliteForeignKeyVerificationFailed(resolved_store)
                if (
                    _schema_objects(connection, spec.table)
                    != before_objects[spec.table]
                ):
                    raise SqliteForeignKeyVerificationFailed(resolved_store)
                actual_sql = _table_sql(connection, spec.table)
                expected_sql = spec.target_ddl.format(table=spec.table)
                if _normalize_table_sql(actual_sql) != _normalize_table_sql(
                    expected_sql
                ):
                    raise SqliteForeignKeyVerificationFailed(resolved_store)
            _inject(fail_at, "before_commit")
            try:
                connection.execute("COMMIT")
            except sqlite3.Error as exc:
                if _classify_sqlite_failure(exc) == STATUS_BUSY:
                    raise SqliteForeignKeyMigrationBusy(
                        resolved_store,
                        phase=BUSY_PHASE_COMMIT,
                    ) from exc
                raise
            connection.close()
            connection = None

            post_inventory = _reopen_with_connection_policy(
                store=resolved_store,
                path=path,
                timeout_seconds=timeout_seconds,
            )
            if not post_inventory.ready:
                raise SqliteForeignKeyVerificationFailed(resolved_store)
    except FileLockError as exc:
        raise SqliteForeignKeyMigrationBusy(
            resolved_store,
            phase=BUSY_PHASE_MIGRATION_LOCK,
        ) from exc
    except _InjectedMigrationFailure as exc:
        _rollback_quietly(connection)
        raise SqliteMigrationError(resolved_store) from exc
    except SqliteMigrationError:
        _rollback_quietly(connection)
        raise
    except SqliteIntegrityError:
        _rollback_quietly(connection)
        raise
    except (OSError, sqlite3.Error) as exc:
        _rollback_quietly(connection)
        raise SqliteMigrationError(resolved_store) from exc
    finally:
        if connection is not None:
            connection.close()

    final_plan = plan_sqlite_foreign_key_migration(
        store=resolved_store,
        path=path,
        timeout_seconds=timeout_seconds,
    )
    if final_plan.status != STATUS_READY:
        raise SqliteForeignKeyVerificationFailed(resolved_store)
    return MigrationApplyResult(
        store=resolved_store,
        migration_id=applied_migration_id,
        status="migration_applied",
        applied=True,
        rebuilt_table_count=len(rebuilt_specs),
        backup_id=backup_id,
        backup_sha256=backup_hash,
        plan_digest=final_plan.plan_digest,
        declared_fk_count=final_plan.current_declared_fk_count,
        foreign_key_violation_count=final_plan.foreign_key_violation_count,
        quick_check_ok=final_plan.quick_check_ok,
    )
