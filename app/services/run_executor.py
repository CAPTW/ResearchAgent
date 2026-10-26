"""Central durable run-execution boundary.

Replaces fire-and-forget ``threading.Thread(..., daemon=True)`` pipeline starts
with: durable enqueue -> atomic claim -> lease/heartbeat -> fenced terminal
update. Every start path (manual API, dashboard, topic scheduler, rerun, MCP,
CLI) goes through :func:`enqueue_run` and nothing else may invoke the pipeline.

Scope boundary: this is a **local, single-host** executor over the existing
SQLite store. It is not distributed and implements no stage-level resume; a run
whose pipeline already started is never re-executed automatically.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from app.repositories import run_jobs
from app.repositories.runs import load_run_record, save_run_record
from app.domain.path_safety import PathSafetyError, validate_run_id

logger = logging.getLogger("app.run_executor")

MODE_EMBEDDED = "embedded"
MODE_DISABLED = "disabled"

_DEFAULTS = {
    "mode": MODE_EMBEDDED,
    "lease_seconds": 300,
    "heartbeat_seconds": 30,
    "poll_seconds": 1.0,
    "max_attempts": 3,
    "shutdown_grace_seconds": 10.0,
}

# Bounded, validated ranges. Invalid configuration fails closed (never silently
# "succeeds" with an unsafe value).
_BOUNDS = {
    "lease_seconds": (10, 3600),
    "heartbeat_seconds": (1, 600),
    "poll_seconds": (0.05, 60.0),
    "max_attempts": (1, 10),
    "shutdown_grace_seconds": (0.0, 120.0),
}


class RunExecutorConfigError(RuntimeError):
    code = "run_executor_config_invalid"


@dataclass(frozen=True)
class RunExecutorConfig:
    mode: str
    lease_seconds: int
    heartbeat_seconds: int
    poll_seconds: float
    max_attempts: int
    shutdown_grace_seconds: float

    @property
    def enabled(self) -> bool:
        return self.mode == MODE_EMBEDDED


def _env(name: str) -> str | None:
    value = os.getenv(name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _bounded(name: str, raw: str | None, default: float, cast: Callable[[str], float]) -> float:
    if raw is None:
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError) as exc:
        raise RunExecutorConfigError(f"invalid_{name}") from exc
    low, high = _BOUNDS[name]
    if not (low <= value <= high):
        raise RunExecutorConfigError(f"out_of_range_{name}")
    return value


def load_config() -> RunExecutorConfig:
    mode = (_env("XRML_RUN_EXECUTOR_MODE") or _DEFAULTS["mode"]).lower()
    if mode not in {MODE_EMBEDDED, MODE_DISABLED}:
        raise RunExecutorConfigError("invalid_mode")
    lease = int(_bounded("lease_seconds", _env("XRML_RUN_EXECUTOR_LEASE_SECONDS"), _DEFAULTS["lease_seconds"], float))
    heartbeat = int(
        _bounded("heartbeat_seconds", _env("XRML_RUN_EXECUTOR_HEARTBEAT_SECONDS"), _DEFAULTS["heartbeat_seconds"], float)
    )
    if heartbeat * 2 > lease:
        # A lease must outlive several heartbeats or a healthy worker would be
        # reclaimed mid-flight.
        raise RunExecutorConfigError("lease_shorter_than_heartbeat")
    poll = _bounded("poll_seconds", _env("XRML_RUN_EXECUTOR_POLL_SECONDS"), _DEFAULTS["poll_seconds"], float)
    attempts = int(_bounded("max_attempts", _env("XRML_RUN_EXECUTOR_MAX_ATTEMPTS"), _DEFAULTS["max_attempts"], float))
    grace = _bounded(
        "shutdown_grace_seconds",
        _env("XRML_RUN_EXECUTOR_SHUTDOWN_GRACE_SECONDS"),
        _DEFAULTS["shutdown_grace_seconds"],
        float,
    )
    return RunExecutorConfig(
        mode=mode,
        lease_seconds=lease,
        heartbeat_seconds=heartbeat,
        poll_seconds=poll,
        max_attempts=attempts,
        shutdown_grace_seconds=grace,
    )


def enqueue_run(run_id: str, *, max_attempts: int | None = None) -> run_jobs.RunJob:
    """The one durable enqueue boundary for starting a research run.

    Validates the identifier, then durably records the job. Callers must not
    invoke the pipeline directly. Idempotent per active run_id.
    """
    validate_run_id(run_id)
    attempts = max_attempts if max_attempts is not None else load_config().max_attempts
    return run_jobs.enqueue_run_job(run_id, max_attempts=attempts)


def _default_pipeline(run_id: str) -> None:
    # Imported lazily so importing the executor never pulls the whole pipeline.
    from app.services.runs import execute_pipeline_for_worker

    execute_pipeline_for_worker(run_id)


def _mark_run_interrupted(run_id: str, reason: str) -> None:
    """Reflect a truthful non-success terminal state on the public run record."""
    try:
        run = load_run_record(run_id)
    except (FileNotFoundError, PathSafetyError):
        return
    if str(run.get("status")) in {"completed", "failed"}:
        return
    run["status"] = "interrupted"
    run["error"] = reason
    save_run_record(run)


def reconcile(*, now: datetime | None = None) -> dict[str, int]:
    """Startup/periodic reconciliation. Never invokes the pipeline."""
    summary = {"requeued": 0, "interrupted": 0, "max_attempts": 0, "orphaned": 0}
    for job in run_jobs.list_expired_jobs(now=now):
        outcome = run_jobs.recover_expired_job(job, now=now)
        if outcome == "requeued":
            summary["requeued"] += 1
        elif outcome == "interrupted":
            summary["interrupted"] += 1
            _mark_run_interrupted(job.run_id, run_jobs.REASON_RECOVERY_REQUIRED)
        elif outcome == "max_attempts":
            summary["max_attempts"] += 1
            _mark_run_interrupted(job.run_id, run_jobs.REASON_MAX_ATTEMPTS)
    # A job whose run record vanished must never be executed.
    for job in run_jobs.list_active_jobs():
        try:
            load_run_record(job.run_id)
        except (FileNotFoundError, PathSafetyError):
            if run_jobs.dead_letter_job(job.job_id, reason_code=run_jobs.REASON_RUN_RECORD_MISSING):
                summary["orphaned"] += 1
    return summary


class RunExecutorWorker:
    """Managed worker owned by the app lifespan (never a fire-and-forget daemon)."""

    def __init__(
        self,
        *,
        config: RunExecutorConfig | None = None,
        pipeline: Callable[[str], None] | None = None,
        owner: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config or load_config()
        self._pipeline = pipeline or _default_pipeline
        self._owner = owner or f"run-executor-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._clock = clock or run_jobs.utc_now
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.Lock()

    @property
    def owner(self) -> str:
        return self._owner

    @property
    def config(self) -> RunExecutorConfig:
        return self._config

    def snapshot(self) -> dict[str, object]:
        with self._lifecycle_lock:
            running = bool(self._thread and self._thread.is_alive())
            stopping = running and self._stop_event.is_set()
        return {
            "mode": self._config.mode,
            "enabled": self._config.enabled,
            "running": running,
            "stopping": stopping,
            "owner": self._owner,
            "lease_seconds": self._config.lease_seconds,
            "heartbeat_seconds": self._config.heartbeat_seconds,
        }

    # -- single step ----------------------------------------------------
    def run_once(self) -> str | None:
        """Claim and process at most one job. Returns the processed run_id."""
        job = run_jobs.claim_next_job(
            owner=self._owner,
            lease_seconds=self._config.lease_seconds,
            now=self._clock(),
        )
        if job is None:
            return None
        self._process(job)
        return job.run_id

    def _process(self, job: run_jobs.RunJob) -> None:
        token = job.lease_token or ""
        try:
            load_run_record(job.run_id)
        except (FileNotFoundError, PathSafetyError):
            run_jobs.complete_job(
                job.job_id,
                token,
                state=run_jobs.STATE_FAILED,
                reason_code=run_jobs.REASON_RUN_RECORD_MISSING,
                now=self._clock(),
            )
            return

        # Durable started marker BEFORE the pipeline runs; if this update is
        # fenced out we lost the lease and must not execute.
        if not run_jobs.mark_pipeline_started(job.job_id, token, now=self._clock()):
            logger.warning("run_executor_lease_lost_before_start")
            return

        try:
            self._pipeline(job.run_id)
        except Exception:  # noqa: BLE001 - converted to a redacted stable code
            logger.warning("run_executor_pipeline_failed")
            run_jobs.complete_job(
                job.job_id,
                token,
                state=run_jobs.STATE_FAILED,
                reason_code=run_jobs.REASON_PIPELINE_FAILED,
                error_code=run_jobs.REASON_PIPELINE_FAILED,
                now=self._clock(),
            )
            return
        run_jobs.complete_job(job.job_id, token, state=run_jobs.STATE_SUCCEEDED, now=self._clock())

    # -- lifecycle ------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                processed = self.run_once()
            except Exception:  # noqa: BLE001 - one bad job must not kill the loop
                logger.warning("run_executor_cycle_failed")
                processed = None
            if processed is None:
                self._stop_event.wait(self._config.poll_seconds)

    def start(self) -> None:
        if not self._config.enabled:
            logger.info("run_executor_disabled")
            return
        with self._lifecycle_lock:
            if self._thread and self._thread.is_alive():
                return  # Includes a worker still draining after stop's grace period.
            try:
                reconcile(now=self._clock())
            except Exception:  # noqa: BLE001 - reconciliation must not block startup
                logger.warning("run_executor_reconcile_failed")
            self._stop_event.clear()
            # Managed, joinable worker owned by the application lifespan.
            try:
                self._thread = threading.Thread(target=self._loop, name="run-executor-worker", daemon=True)
                self._thread.start()
            except Exception:
                self._stop_event.set()
                self._thread = None
                raise

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stop_event.set()
            thread = self._thread
        # A pipeline may inspect status or ask to stop from its own thread.
        # Never hold the lifecycle lock across join or join the current thread.
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=self._config.shutdown_grace_seconds)
        with self._lifecycle_lock:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None


def build_run_executor_worker() -> RunExecutorWorker:
    """Factory used by the app lifespan; fails closed to disabled on bad config."""
    try:
        config = load_config()
    except RunExecutorConfigError:
        logger.warning("run_executor_config_invalid_disabled")
        config = RunExecutorConfig(
            mode=MODE_DISABLED,
            lease_seconds=int(_DEFAULTS["lease_seconds"]),
            heartbeat_seconds=int(_DEFAULTS["heartbeat_seconds"]),
            poll_seconds=float(_DEFAULTS["poll_seconds"]),
            max_attempts=int(_DEFAULTS["max_attempts"]),
            shutdown_grace_seconds=float(_DEFAULTS["shutdown_grace_seconds"]),
        )
    return RunExecutorWorker(config=config)
