from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

from app.db.sqlite_integrity import (
    DEFAULT_METADATA_WRITE_TIMEOUT_SECONDS,
    DEFAULT_SCHEDULER_WRITE_TIMEOUT_SECONDS,
    STATUS_MIGRATION_REQUIRED,
    STATUS_READY,
    STATUS_SCHEMA_MISMATCH,
    STATUS_UNINITIALIZED,
    STORE_METADATA,
    STORE_SCHEDULER,
    SqliteForeignKeyMigrationRequired,
    SqliteIntegrityError,
    SqliteSchemaMismatch,
    SqliteStoreUnavailable,
    inspect_required_foreign_keys,
    open_sqlite_read_only,
    open_sqlite_read_write,
    require_store_write_compatible,
    sqlite_user_tables,
)
from app.domain.constants import PROJECT_ROOT, STORAGE_ROOT
from app.repositories.files import ensure_dir

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS index_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  topic TEXT NOT NULL,
  topic_slug TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  root_run_id TEXT NOT NULL,
  parent_run_id TEXT,
  lineage_mode TEXT NOT NULL,
  preview_hash TEXT,
  triage_items INTEGER NOT NULL DEFAULT 0,
  reviewed_items INTEGER NOT NULL DEFAULT 0,
  evidence_count INTEGER NOT NULL DEFAULT 0,
  claim_count INTEGER NOT NULL DEFAULT 0,
  report_section_count INTEGER NOT NULL DEFAULT 0,
  research_plan_path TEXT,
  report_path TEXT
);

CREATE TABLE IF NOT EXISTS run_topics (
  run_id TEXT NOT NULL,
  topic TEXT NOT NULL,
  PRIMARY KEY (run_id, topic),
  FOREIGN KEY (run_id) REFERENCES runs(run_id)
    ON DELETE CASCADE
    ON UPDATE NO ACTION
);

CREATE TABLE IF NOT EXISTS papers (
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
);

CREATE TABLE IF NOT EXISTS evidence (
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
);

CREATE TABLE IF NOT EXISTS report_sections (
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
);

CREATE TABLE IF NOT EXISTS paper_reviews (
  topic_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  paper_id TEXT NOT NULL,
  title TEXT NOT NULL,
  source_provider TEXT,
  authors_json TEXT NOT NULL DEFAULT '[]',
  published_at TEXT,
  abstract_text TEXT,
  text_excerpt TEXT,
  full_text_status TEXT NOT NULL,
  relevance_score REAL NOT NULL DEFAULT 0,
  novelty_score REAL NOT NULL DEFAULT 0,
  evidence_strength REAL NOT NULL DEFAULT 0,
  method_summary TEXT,
  key_findings_json TEXT NOT NULL DEFAULT '[]',
  limitations_json TEXT NOT NULL DEFAULT '[]',
  why_it_matters TEXT,
  topic_fit_rationale TEXT,
  review_model TEXT,
  reviewer TEXT,
  reviewed_at TEXT NOT NULL,
  source_text_sha256 TEXT,
  full_text_artifact_path TEXT,
  text_char_count INTEGER NOT NULL DEFAULT 0,
  evidence_spans_json TEXT NOT NULL DEFAULT '[]',
  review_source_type TEXT NOT NULL DEFAULT 'abstract',
  full_text_reviewed_at TEXT,
  PRIMARY KEY (topic_id, run_id, paper_id)
);

CREATE TABLE IF NOT EXISTS ai_review_drafts (
  topic_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  paper_id TEXT NOT NULL,
  source_text_sha256 TEXT NOT NULL DEFAULT '',
  review_level TEXT NOT NULL,
  status TEXT NOT NULL,
  draft_json TEXT NOT NULL,
  validation_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  imported_at TEXT NOT NULL,
  PRIMARY KEY (topic_id, run_id, paper_id, source_text_sha256)
);

CREATE INDEX IF NOT EXISTS idx_runs_topic ON runs(topic);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_papers_run_score ON papers(run_id, score DESC);
CREATE INDEX IF NOT EXISTS idx_papers_theme ON papers(theme);
CREATE INDEX IF NOT EXISTS idx_evidence_source ON evidence(source_id);
CREATE INDEX IF NOT EXISTS idx_paper_reviews_topic ON paper_reviews(topic_id);
CREATE INDEX IF NOT EXISTS idx_paper_reviews_run ON paper_reviews(run_id);
CREATE INDEX IF NOT EXISTS idx_ai_review_drafts_topic ON ai_review_drafts(topic_id, status);
"""


def _sqlite_statements(script: str) -> tuple[str, ...]:
    """Split a SQLite script only at parser-confirmed statement boundaries."""
    statements: list[str] = []
    buffer: list[str] = []
    for character in script:
        buffer.append(character)
        if character == ";" and sqlite3.complete_statement("".join(buffer)):
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer = []
    if "".join(buffer).strip():
        raise RuntimeError("incomplete metadata schema statement")
    return tuple(statements)


_METADATA_SCHEMA_STATEMENTS = _sqlite_statements(SCHEMA_SQL)
_METADATA_TABLES = frozenset(
    {
        "index_state",
        "runs",
        "run_topics",
        "papers",
        "evidence",
        "report_sections",
        "paper_reviews",
        "ai_review_drafts",
    }
)
_METADATA_NAMED_INDEX_TABLES = {
    "idx_runs_topic": "runs",
    "idx_runs_status": "runs",
    "idx_papers_run_score": "papers",
    "idx_papers_theme": "papers",
    "idx_evidence_source": "evidence",
    "idx_paper_reviews_topic": "paper_reviews",
    "idx_paper_reviews_run": "paper_reviews",
    "idx_ai_review_drafts_topic": "ai_review_drafts",
}
_METADATA_EXPECTED_OBJECTS = frozenset(
    {
        *(("table", table, table) for table in _METADATA_TABLES),
        *(
            ("index", index, table)
            for index, table in _METADATA_NAMED_INDEX_TABLES.items()
        ),
    }
)
# Generated from the semantic PRAGMA descriptor of the authoritative SCHEMA_SQL.
# Foreign keys are deliberately classified separately so the recognized
# complete 8-table/0-FK legacy schema remains migration-required.
_METADATA_SCHEMA_STRUCTURE_SHA256 = (
    "d1edcdca347705e1b11458b13f3c1463bb10efe42de0b1a0ad11349f65de8f97"
)

SCHEDULER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS scheduler_schedules (
  schedule_id TEXT PRIMARY KEY,
  profile_id TEXT NOT NULL,
  task_type TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 0,
  deleted INTEGER NOT NULL DEFAULT 0,
  next_run_at TEXT,
  current_run_id TEXT,
  current_run_status TEXT,
  scheduler_state TEXT NOT NULL DEFAULT 'paused',
  lock_token TEXT,
  lock_owner TEXT,
  lock_acquired_at TEXT,
  lock_expires_at TEXT,
  last_worker_id TEXT,
  dispatch_attempts INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scheduler_due
  ON scheduler_schedules(enabled, deleted, next_run_at);

CREATE INDEX IF NOT EXISTS idx_scheduler_lease
  ON scheduler_schedules(lock_expires_at);

CREATE TABLE IF NOT EXISTS scheduler_worker_heartbeats (
  worker_id TEXT PRIMARY KEY,
  mode TEXT NOT NULL,
  backend_mode TEXT NOT NULL,
  enabled INTEGER NOT NULL,
  running INTEGER NOT NULL,
  pid INTEGER,
  thread TEXT,
  tick_seconds REAL,
  started_at TEXT,
  heartbeat_at TEXT NOT NULL,
  last_cycle_summary_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduler_queue_items (
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
);

CREATE INDEX IF NOT EXISTS idx_scheduler_queue_status
  ON scheduler_queue_items(status, lease_expires_at);

CREATE INDEX IF NOT EXISTS idx_scheduler_queue_schedule
  ON scheduler_queue_items(schedule_id, due_at);

CREATE TABLE IF NOT EXISTS run_jobs (
  job_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  handler TEXT NOT NULL,
  payload_schema_version INTEGER NOT NULL DEFAULT 1,
  state TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  available_at TEXT NOT NULL,
  lease_owner TEXT,
  lease_token TEXT,
  lease_expires_at TEXT,
  heartbeat_at TEXT,
  pipeline_started_at TEXT,
  reason_code TEXT,
  error_code TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT
);

-- At most one active (non-terminal) job per run_id, enforced by the database.
CREATE UNIQUE INDEX IF NOT EXISTS idx_run_jobs_active_run
  ON run_jobs(run_id)
  WHERE state IN ('queued', 'leased', 'running');

CREATE INDEX IF NOT EXISTS idx_run_jobs_claim
  ON run_jobs(state, available_at);

CREATE INDEX IF NOT EXISTS idx_run_jobs_lease
  ON run_jobs(state, lease_expires_at);
"""


def metadata_index_path(*, ensure_parent: bool = True) -> Path:
    target = STORAGE_ROOT.parent / "metadata_index.sqlite3"
    if ensure_parent:
        ensure_dir(target.parent)
    return target


def _is_repo_metadata_index_path(path: Path) -> bool:
    repo_metadata_index = PROJECT_ROOT / "storage" / "metadata_index.sqlite3"
    return path.resolve() == repo_metadata_index.resolve()


def _guard_pytest_real_metadata_write(path: Path) -> None:
    if not os.getenv("PYTEST_CURRENT_TEST"):
        return
    if os.getenv("XRML_ALLOW_REAL_STORAGE_WRITES_FOR_TESTS") == "1":
        return
    if _is_repo_metadata_index_path(path):
        raise RuntimeError(
            "Refusing to write repo-local storage/metadata_index.sqlite3 during pytest; "
            "use a temp XRML_DATA_DIR/STORAGE_ROOT fixture for metadata writes."
        )


def metadata_index_sidecar_paths() -> dict[str, Path]:
    target = metadata_index_path(ensure_parent=False)
    return {
        "journal": Path(f"{target}-journal"),
        "wal": Path(f"{target}-wal"),
        "shm": Path(f"{target}-shm"),
    }


def metadata_index_sidecar_status() -> dict[str, dict[str, object]]:
    return {
        name: {
            "path": str(path),
            "exists": path.exists(),
            "size": path.stat().st_size if path.exists() else None,
        }
        for name, path in metadata_index_sidecar_paths().items()
    }


def _metadata_schema_structure_fingerprint(
    connection: sqlite3.Connection,
) -> str | None:
    object_rows = connection.execute(
        "SELECT type, name, tbl_name FROM sqlite_master "
        "WHERE type IN ('table', 'index', 'view', 'trigger') "
        "AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    objects = frozenset(
        (str(row[0]), str(row[1]), str(row[2]))
        for row in object_rows
    )
    # Explicit FK migration preserves independent application extensions
    # (for example, audit views/triggers). The authoritative metadata objects
    # must all be present and exact; unrelated preserved objects are not
    # bootstrap candidates and are left untouched.
    if not _METADATA_EXPECTED_OBJECTS.issubset(objects):
        return None

    descriptor: dict[str, object] = {"tables": {}}
    tables = descriptor["tables"]
    if not isinstance(tables, dict):
        raise TypeError("invalid metadata schema descriptor")
    for table in sorted(_METADATA_TABLES):
        columns = [
            [
                str(row[1]),
                str(row[2]).upper(),
                int(row[3]),
                None if row[4] is None else str(row[4]),
                int(row[5]),
                int(row[6]),
            ]
            for row in connection.execute(
                f'PRAGMA table_xinfo("{table}")'
            ).fetchall()
        ]
        indexes: list[list[object]] = []
        for index in connection.execute(
            f'PRAGMA index_list("{table}")'
        ).fetchall():
            name = str(index[1])
            origin = str(index[3])
            logical_name = "__primary_key__" if origin == "pk" else name
            indexed_columns = [
                [
                    None if row[2] is None else str(row[2]),
                    int(row[3]),
                    None if row[4] is None else str(row[4]),
                    int(row[5]),
                ]
                for row in connection.execute(
                    f'PRAGMA index_xinfo("{name}")'
                ).fetchall()
            ]
            indexes.append(
                [
                    logical_name,
                    int(index[2]),
                    origin,
                    int(index[4]),
                    indexed_columns,
                ]
            )
        tables[table] = {
            "columns": columns,
            "indexes": sorted(indexes, key=lambda item: str(item[0])),
        }
    encoded = json.dumps(
        descriptor,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _metadata_schema_status(connection: sqlite3.Connection) -> str:
    objects = connection.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type IN ('table', 'index', 'view', 'trigger') "
        "AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone()
    if objects is None:
        return STATUS_UNINITIALIZED
    if (
        _metadata_schema_structure_fingerprint(connection)
        != _METADATA_SCHEMA_STRUCTURE_SHA256
    ):
        return STATUS_SCHEMA_MISMATCH
    inventory = inspect_required_foreign_keys(
        connection,
        store=STORE_METADATA,
    )
    if inventory.status in {STATUS_READY, STATUS_MIGRATION_REQUIRED}:
        return inventory.status
    return STATUS_SCHEMA_MISMATCH


def _require_metadata_schema_ready(status: str) -> None:
    # The status already includes the full structure and FK inventory. Reuse it
    # within this validation boundary; never cache it across connections or DDL.
    if status == STATUS_MIGRATION_REQUIRED:
        raise SqliteForeignKeyMigrationRequired(STORE_METADATA)
    if status != STATUS_READY:
        raise SqliteSchemaMismatch(STORE_METADATA)


def _metadata_bootstrap_checkpoint(_phase: str) -> None:
    """Minimal deterministic seam for transaction failure/concurrency tests."""


def _rollback_metadata_bootstrap(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        return
    try:
        connection.rollback()
    except sqlite3.Error:
        pass


def _initialize_metadata_schema(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("BEGIN EXCLUSIVE")
        _metadata_bootstrap_checkpoint("after_transaction_acquisition")

        status = _metadata_schema_status(connection)
        if status == STATUS_READY:
            connection.rollback()
            return
        if status == STATUS_MIGRATION_REQUIRED:
            raise SqliteForeignKeyMigrationRequired(STORE_METADATA)
        if status != STATUS_UNINITIALIZED:
            raise SqliteSchemaMismatch(STORE_METADATA)

        midpoint = len(_METADATA_SCHEMA_STATEMENTS) // 2
        for position, statement in enumerate(
            _METADATA_SCHEMA_STATEMENTS,
            start=1,
        ):
            connection.execute(statement)
            if position == 1:
                _metadata_bootstrap_checkpoint("after_first_table")
            if position == midpoint:
                _metadata_bootstrap_checkpoint("after_mid_schema_object")
        _metadata_bootstrap_checkpoint("after_final_ddl")
        _metadata_bootstrap_checkpoint("before_schema_verification")
        _require_metadata_schema_ready(_metadata_schema_status(connection))
        _metadata_bootstrap_checkpoint("before_commit")
        connection.commit()
    except SqliteIntegrityError:
        _rollback_metadata_bootstrap(connection)
        raise
    except sqlite3.Error:
        _rollback_metadata_bootstrap(connection)
        raise SqliteStoreUnavailable(STORE_METADATA) from None
    except BaseException:
        _rollback_metadata_bootstrap(connection)
        raise


def connect_metadata_index(*, read_only: bool = False) -> sqlite3.Connection:
    target = metadata_index_path(ensure_parent=not read_only)
    if read_only:
        return open_sqlite_read_only(
            target,
            store=STORE_METADATA,
        )

    _guard_pytest_real_metadata_write(target)
    connection = open_sqlite_read_write(
        target,
        store=STORE_METADATA,
        timeout_seconds=DEFAULT_METADATA_WRITE_TIMEOUT_SECONDS,
    )
    try:
        status = _metadata_schema_status(connection)
        if status == STATUS_UNINITIALIZED:
            _initialize_metadata_schema(connection)
        else:
            _require_metadata_schema_ready(status)
        return connection
    except SqliteIntegrityError:
        connection.close()
        raise
    except sqlite3.OperationalError:
        connection.close()
        raise SqliteStoreUnavailable(STORE_METADATA) from None
    except sqlite3.Error:
        connection.close()
        raise SqliteSchemaMismatch(STORE_METADATA) from None
    except BaseException:
        connection.close()
        raise


def scheduler_db_path(*, ensure_parent: bool = True) -> Path:
    target = STORAGE_ROOT.parent / "scheduler.sqlite3"
    if ensure_parent:
        ensure_dir(target.parent)
    return target


def bootstrap_runtime_stores() -> dict[str, str]:
    """Explicit startup mutation boundary for local runtime stores.

    This is the ONLY place that intentionally creates the runtime storage
    directories and applies the metadata / scheduler (incl. durable ``run_jobs``)
    schemas as a startup step. It is never called from a GET/HEAD/OPTIONS route,
    from app factory/import, or from OpenAPI generation. Idempotent: the schema
    statements are ``IF NOT EXISTS`` and re-running is a no-op.

    Returns a bounded per-store status mapping with no path or raw error text.
    """
    results: dict[str, str] = {}
    ensure_dir(STORAGE_ROOT)
    metadata_connection = connect_metadata_index()
    try:
        metadata_connection.execute("SELECT 1 FROM index_state LIMIT 1")
        metadata_connection.commit()
    finally:
        # Explicitly close (sqlite3's context manager only ends a transaction),
        # so startup leaves a settled on-disk state with no lingering sidecars.
        metadata_connection.close()
    results["metadata_index"] = "bootstrap_completed"

    scheduler_connection = connect_scheduler_db()
    try:
        scheduler_connection.execute("SELECT 1 FROM run_jobs LIMIT 1")
    finally:
        # A usable WAL store can have other active writers or read snapshots.
        # Forced truncation would wait for them despite startup needing only
        # the committed schema/data. Normal close retains SQLite's checkpoint
        # and durability policy without making WAL maintenance a readiness gate.
        scheduler_connection.close()
    results["scheduler"] = "bootstrap_completed"
    results["run_executor"] = "bootstrap_completed"
    return results


def connect_scheduler_db_read_only(*, timeout: float = 5.0) -> sqlite3.Connection:
    """Open an EXISTING scheduler DB read-only for inspection.

    Never creates the file, never runs schema DDL, never changes journal mode.
    Raises FileNotFoundError when the store does not exist so callers can report
    a truthful ``missing`` status instead of bootstrapping one.
    """
    target = scheduler_db_path(ensure_parent=False)
    return open_sqlite_read_only(
        target,
        store=STORE_SCHEDULER,
        timeout_seconds=timeout,
    )


def sqlite_tables(connection: sqlite3.Connection) -> set[str]:
    """Bounded read-only schema observation via sqlite_master.

    Indexes by position so it works regardless of the connection's row_factory.
    """
    return sqlite_user_tables(connection)


def connect_scheduler_db() -> sqlite3.Connection:
    connection = open_sqlite_read_write(
        scheduler_db_path(),
        store=STORE_SCHEDULER,
        timeout_seconds=DEFAULT_SCHEDULER_WRITE_TIMEOUT_SECONDS,
        isolation_level=None,
    )
    try:
        existing_tables = sqlite_user_tables(connection)
        if existing_tables:
            # Do not change journal mode or run DDL on a legacy schema.
            require_store_write_compatible(connection, store=STORE_SCHEDULER)
        # This connection is in autocommit mode. Keep all schema DDL and its
        # compatibility check in one transaction, so a failed later statement
        # cannot leave a partially initialized store or require one durable
        # commit per table/index. DEFERRED leaves a complete schema's no-op DDL
        # readable while another WAL connection holds a write transaction.
        connection.executescript("BEGIN DEFERRED;\n" + SCHEDULER_SCHEMA_SQL)
        require_store_write_compatible(connection, store=STORE_SCHEDULER)
        connection.commit()
        connection.execute("PRAGMA journal_mode = WAL")
        return connection
    except SqliteIntegrityError:
        connection.close()
        raise
    except sqlite3.Error as exc:
        connection.close()
        # Extended SQLite codes retain their primary category in the low byte.
        # Contention prevents access; it does not establish a schema mismatch.
        if getattr(exc, "sqlite_errorcode", 0) & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            raise SqliteStoreUnavailable(STORE_SCHEDULER) from exc
        raise SqliteSchemaMismatch(STORE_SCHEDULER) from exc
    except BaseException:
        # close() rolls back any unfinished schema transaction as well as
        # releasing its handle, including validation errors and interruption.
        connection.close()
        raise
