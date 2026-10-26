from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.db.bootstrap import bootstrap_runtime_stores
from app.db.sqlite_integrity import SqliteIntegrityError
from app.domain.path_safety import PathSafetyError
from app.repositories.jsonl import JsonlIntegrityError, JsonlStateBusy
from app.repositories.shared_state import SharedStateBusy, SharedStateError
from app.api.admin import router as admin_router
from app.api.auth import router as auth_router
from app.api.dashboard import router as dashboard_router
from app.api.evidence_search import router as evidence_search_router
from app.api.global_search import router as global_search_router
from app.api.report_draft_assist import router as report_draft_assist_router
from app.api.health import router as health_router
from app.api.model_connections import router as model_connections_router
from app.api.mcp import router as mcp_router
from app.api.prototype import router as prototype_router
from app.api.runs import router as runs_router
from app.auth.config import AuthSettings, get_auth_settings
from app.ops.logging import RequestLoggingMiddleware, configure_logging
from app.ops.runtime import build_deployment_status, enforce_strict_environment_if_requested
from app.services import topic_notifications
from app.services.journal_offline import build_runtime as build_offline_journal_runtime
from app.services.journal_codex_preview import JournalCodexPreview
from app.services.journal_codex_dispatch import CodexJournalRuntime
from app.services.review_approval import PaperApprovalRuntime
from app.services.journal_approval import JournalApprovalRuntime
from app.services.review_codex_execution import CodexPaperRuntime
from app.services.codex_paper_trial import PaperTrialRuntime
from app.services.local_anthropic_trial import LocalAnthropicTrial
from app.services.local_anthropic_app import LocalAnthropicAppRuntime
from app.services.review_offline import build_runtime as build_offline_review_runtime
from app.services.review_execution import build_runtime as build_review_execution_runtime
from app.services.review_transport_offline import build_runtime as build_paper_transport_offline_runtime
from app.services.review_anthropic_execution import build_runtime as build_paper_anthropic_offline_runtime
from app.services.review_anthropic_native import AnthropicNativeRuntime
from app.services.providers import describe_provider_status
from app.services.run_executor import build_run_executor_worker
from app.services.topic_scheduler import build_topic_schedule_worker

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_DIST_ROOT = PROJECT_ROOT / "web" / "dist"


def _resolve_allowed_origins(settings: AuthSettings) -> list[str]:
    origins = {
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        settings.normalized_app_base_url,
        *settings.normalized_allowed_origins,
    }
    return sorted(origin for origin in origins if origin)


def _configure_spa_routes(app: FastAPI, settings: AuthSettings, web_dist: Path) -> None:
    if not web_dist.exists():
        return

    web_base = settings.normalized_web_app_base_path
    index_file = web_dist / "index.html"
    if not index_file.exists():
        return

    resolved_root = web_dist.resolve()

    def _resolve_asset(path_fragment: str) -> Path | None:
        candidate = (resolved_root / path_fragment).resolve()
        try:
            candidate.relative_to(resolved_root)
        except ValueError:
            return None
        if candidate.is_file():
            return candidate
        return None

    @app.get(web_base, include_in_schema=False)
    @app.get(f"{web_base}/", include_in_schema=False)
    async def serve_web_app_index() -> FileResponse:
        return FileResponse(index_file)

    @app.get(f"{web_base}/{{asset_path:path}}", include_in_schema=False)
    async def serve_web_app(asset_path: str) -> FileResponse:
        asset = _resolve_asset(asset_path)
        if asset is not None:
            return FileResponse(asset)
        if Path(asset_path).suffix:
            raise HTTPException(status_code=404, detail="Static asset not found.")
        return FileResponse(index_file)


def create_app(*, web_dist: Path | None = None, journal_codex_runtime: CodexJournalRuntime | None = None,
               paper_codex_runtime: CodexPaperRuntime | None = None,
               paper_approval_runtime: PaperApprovalRuntime | None = None,
               journal_approval_runtime: JournalApprovalRuntime | None = None,
               journal_codex_preview: JournalCodexPreview | None = None,
               paper_trial_runtime: PaperTrialRuntime | None = None,
               paper_anthropic_native_runtime: AnthropicNativeRuntime | LocalAnthropicAppRuntime | None = None,
               local_anthropic_trial: LocalAnthropicTrial | None = None) -> FastAPI:
    configure_logging()
    settings = get_auth_settings()
    schedule_worker = build_topic_schedule_worker()
    run_executor_worker = build_run_executor_worker()
    offline_journal_runtime = build_offline_journal_runtime()
    offline_review_runtime = build_offline_review_runtime()
    review_execution_runtime = build_review_execution_runtime()
    paper_transport_offline_runtime = build_paper_transport_offline_runtime()
    paper_anthropic_offline_runtime = build_paper_anthropic_offline_runtime()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.topic_schedule_worker = schedule_worker
        app.state.run_executor_worker = run_executor_worker
        app.state.journal_offline_runtime = offline_journal_runtime
        app.state.review_offline_runtime = offline_review_runtime
        app.state.review_execution_runtime = review_execution_runtime
        app.state.paper_transport_offline_runtime = paper_transport_offline_runtime
        app.state.paper_anthropic_offline_runtime = paper_anthropic_offline_runtime
        enforce_strict_environment_if_requested(
            build_deployment_status(
                provider=describe_provider_status(),
                scheduler=schedule_worker.snapshot(),
                notification_delivery=topic_notifications.notification_delivery_health_summary(
                    include_resource_activity=False
                ),
            )
        )
        # Explicit startup bootstrap: the single mutation boundary that creates
        # runtime directories and applies the metadata/scheduler/run_jobs schema.
        # The durable worker starts ONLY after this succeeds, so a failed
        # bootstrap can never be masked by a running worker or a ready probe.
        try:
            app.state.runtime_bootstrap = bootstrap_runtime_stores()
            app.state.runtime_bootstrap_ok = True
        except Exception:
            logging.getLogger("app.startup").warning("runtime_bootstrap_failed")
            app.state.runtime_bootstrap = {"status": "bootstrap_failed"}
            app.state.runtime_bootstrap_ok = False

        schedule_worker_started = False
        run_executor_worker_started = False
        try:
            if app.state.runtime_bootstrap_ok:
                schedule_worker.start()
                schedule_worker_started = True
                run_executor_worker.start()
                run_executor_worker_started = True
            yield
        finally:
            try:
                try:
                    try:
                        try:
                            try:
                                if paper_anthropic_native_runtime is not None:
                                    paper_anthropic_native_runtime.stop()
                            finally:
                                if paper_anthropic_offline_runtime is not None:
                                    paper_anthropic_offline_runtime.stop()
                        finally:
                            if paper_transport_offline_runtime is not None:
                                paper_transport_offline_runtime.stop()
                    finally:
                        try:
                            if paper_codex_runtime is not None:
                                paper_codex_runtime.stop()
                        finally:
                            if review_execution_runtime is not None:
                                review_execution_runtime.stop()
                finally:
                    try:
                        if journal_codex_runtime is not None:
                            journal_codex_runtime.stop()
                    finally:
                        if offline_journal_runtime is not None:
                            offline_journal_runtime.stop()
            finally:
                try:
                    if run_executor_worker_started:
                        run_executor_worker.stop()
                finally:
                    if schedule_worker_started:
                        schedule_worker.stop()

    app = FastAPI(title="XRML Research Agent", version="0.1.0", lifespan=lifespan)
    app.state.journal_codex_preview = journal_codex_preview if journal_codex_preview is not None else JournalCodexPreview.from_environment()
    app.state.journal_codex_runtime = journal_codex_runtime
    app.state.paper_codex_runtime = paper_codex_runtime
    app.state.paper_anthropic_native_runtime = paper_anthropic_native_runtime
    app.state.paper_trial_runtime = paper_trial_runtime
    app.state.local_anthropic_trial = local_anthropic_trial
    app.state.paper_approval_runtime = paper_approval_runtime
    app.state.journal_approval_runtime = journal_approval_runtime

    async def _path_safety_handler(_request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, PathSafetyError)
        # A rejected path-derived value is a client error, never a 500, and the
        # raw offending value is never reflected back to the caller or the log.
        return JSONResponse(
            status_code=400,
            content={"detail": {"code": exc.code, "field": exc.field, "reason": exc.reason}},
        )

    app.add_exception_handler(PathSafetyError, _path_safety_handler)

    async def _sqlite_integrity_handler(
        _request: Request,
        exc: Exception,
    ) -> JSONResponse:
        assert isinstance(exc, SqliteIntegrityError)
        return JSONResponse(
            status_code=503,
            content={"detail": {"code": exc.code, "store": exc.store}},
        )

    app.add_exception_handler(SqliteIntegrityError, _sqlite_integrity_handler)

    async def _shared_state_handler(_request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, SharedStateError)
        # Shared state could not be read or committed. The response carries a
        # stable code and the logical state name only - never the path, the
        # stored content, or the parser message - and a contended lock is
        # reported as a retryable conflict rather than a missing resource.
        status_code = 409 if isinstance(exc, SharedStateBusy) else 503
        return JSONResponse(
            status_code=status_code,
            content={"detail": {"code": exc.code, "state": exc.state_name}},
        )

    app.add_exception_handler(SharedStateError, _shared_state_handler)

    async def _jsonl_integrity_handler(
        _request: Request,
        exc: Exception,
    ) -> JSONResponse:
        assert isinstance(exc, JsonlIntegrityError)
        status_code = 409 if isinstance(exc, JsonlStateBusy) else 503
        detail: dict[str, object] = {
            "code": exc.code,
            "complete": False,
            "retryable": isinstance(exc, JsonlStateBusy),
        }
        if exc.line_number is not None:
            detail["lineNumber"] = exc.line_number
        if exc.cause_code is not None:
            detail["causeCode"] = exc.cause_code
        return JSONResponse(status_code=status_code, content={"detail": detail})

    app.add_exception_handler(JsonlIntegrityError, _jsonl_integrity_handler)

    if settings.trust_proxy_headers:
        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_resolve_allowed_origins(settings),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestLoggingMiddleware)

    app.include_router(health_router)
    from app.api.journal_offline import router as journal_offline_router

    app.include_router(journal_offline_router)
    from app.api.journal_codex_dispatch import router as journal_codex_dispatch_router

    app.include_router(journal_codex_dispatch_router)
    from app.api.journal_approval import router as journal_approval_router

    app.include_router(journal_approval_router)
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(evidence_search_router)
    app.include_router(global_search_router)
    app.include_router(dashboard_router)
    app.include_router(model_connections_router)
    from app.api.paper_bank import router as paper_bank_router

    app.include_router(paper_bank_router)
    from app.api.browser_collection import router as browser_collection_router

    app.include_router(browser_collection_router)
    from app.api.saved_analyses import router as saved_analyses_router

    app.include_router(saved_analyses_router)
    from app.api.review_execution import router as review_execution_router
    from app.api.review_codex_execution import router as review_codex_execution_router
    from app.api.review_anthropic_execution import router as review_anthropic_execution_router
    from app.api.codex_paper_trial import router as codex_paper_trial_router
    from app.api.review_approval import router as review_approval_router

    # Literal provider previews precede transport history's dynamic execution ID.
    app.include_router(review_codex_execution_router)
    app.include_router(review_anthropic_execution_router)
    from app.api.review_anthropic_native import router as review_anthropic_native_router
    app.include_router(review_anthropic_native_router)
    app.include_router(codex_paper_trial_router)
    from app.api.local_anthropic_trial import router as local_anthropic_trial_router
    app.include_router(local_anthropic_trial_router)
    app.include_router(review_approval_router)
    app.include_router(review_execution_router)
    from app.api.temporal_memory import router as temporal_memory_router

    app.include_router(temporal_memory_router)
    from app.api.temporal_analysis import router as temporal_analysis_router

    app.include_router(temporal_analysis_router)
    from app.api.recommendation_versions import router as recommendation_versions_router

    app.include_router(recommendation_versions_router)
    from app.api.local_synthesis import router as local_synthesis_router

    app.include_router(local_synthesis_router)
    app.include_router(report_draft_assist_router)
    app.include_router(prototype_router)
    app.include_router(runs_router)
    app.include_router(mcp_router)

    _configure_spa_routes(app, settings, WEB_DIST_ROOT if web_dist is None else web_dist)
    return app


_default_app: FastAPI | None = None
_default_app_lock = Lock()

if TYPE_CHECKING:
    app: FastAPI


def __getattr__(name: str) -> FastAPI:
    # Preserve the public app.main:app ASGI entrypoint without constructing an
    # unused app when desktop hosts or tests import only the factory.
    if name != "app":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    global _default_app
    with _default_app_lock:
        if _default_app is None:
            _default_app = create_app()
        return _default_app
