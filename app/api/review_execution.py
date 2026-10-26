"""Actor-scoped prepared-paper execution. The public API cannot select a target."""
import sqlite3
from typing import Any, Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.auth.resource_access import resource_actor_context, resource_actor_for_user
from app.auth.security import require_product_user
from app.domain.review_execution import Control, Dispatch, Prepare, Scope
from app.domain import review_transport_execution as transport
from app.db.sqlite_integrity import SqliteIntegrityError
from app.repositories import review_executions as repository
from app.repositories import review_transport_history as history
from app.services import review_execution as service
from app.services import review_transport_offline as offline

router = APIRouter(tags=["paper-review-execution"])
ROOT = "/api/topics/{topic_id}/paper-bank/{paper_version_id}/review-preparations/{preparation_id}/executions"
TRANSPORT_ROOT = ROOT.removesuffix("/executions") + "/transport-executions"


class TransportControl(Dispatch):
    action: Literal["cancel", "recover", "save_result"]


def _runtime(request: Request) -> service.OfflineExecutionRuntime | None:
    if not getattr(request.app.state, "runtime_bootstrap_ok", False):
        return None
    return getattr(request.app.state, "review_execution_runtime", None)


def _offline(request: Request) -> offline.OfflineTransportRuntime | None:
    if not getattr(request.app.state, "runtime_bootstrap_ok", False):
        return None
    runtime = getattr(request.app.state, "paper_transport_offline_runtime", None)
    return runtime if isinstance(runtime, offline.OfflineTransportRuntime) else None


def _call(topic: str, version: str, preparation: str, user: Any, operation: Callable[[Scope], dict[str, Any]]) -> dict[str, Any]:
    actor = resource_actor_for_user(user)
    with resource_actor_context(actor):
        try:
            return operation(service.scope(topic, version, preparation, actor))
        except (KeyError, FileNotFoundError):
            raise HTTPException(404, detail="review_execution_not_found") from None
        except ValueError as exc:
            code = str(exc)
            if code == "review_execution_page_invalid":
                raise HTTPException(422, detail=code) from None
            if code in {"review_execution_disabled", "review_execution_preparation_changed", "review_execution_unknown_outcome",
                        "review_execution_busy", "review_execution_request_conflict", "review_execution_changed",
                        "review_execution_not_approved", "review_execution_result_not_received", "review_execution_not_expired",
                        "review_execution_transition_invalid", "review_execution_transport_mismatch",
                        "paper_review_authority_conflict", "paper_review_authority_expired",
                        "paper_review_authority_missing", "paper_review_trusted_authority_required"}:
                raise HTTPException(409, detail=code) from None
            raise HTTPException(503, detail="review_execution_integrity_invalid") from None
        except SqliteIntegrityError:
            raise
        except (sqlite3.Error, OSError, RuntimeError):
            raise HTTPException(503, detail="review_execution_unavailable") from None


@router.get(ROOT)
def listing(topic_id: str, paper_version_id: str, preparation_id: str, request: Request,
            page: int = Query(1, ge=1), page_size: int = Query(10, ge=1, le=50, alias="pageSize"),
            user=Depends(require_product_user)):
    runtime = _runtime(request)
    enabled = runtime is not None and runtime.enabled
    return _call(topic_id, paper_version_id, preparation_id, user, lambda scope: dict(
        **repository.listing(scope, page, page_size), enabled=enabled, mode="OFFLINE_SYNTHETIC",
        reason=None if enabled else "review_execution_disabled"))


@router.get(ROOT + "/{execution_id}")
def detail(topic_id: str, paper_version_id: str, preparation_id: str, execution_id: str,
           user=Depends(require_product_user)):
    return _call(topic_id, paper_version_id, preparation_id, user, lambda scope: repository.detail(scope, execution_id))


@router.post(ROOT)
def prepare(topic_id: str, paper_version_id: str, preparation_id: str, body: Prepare, request: Request,
            user=Depends(require_product_user)):
    runtime = _runtime(request)
    return _call(topic_id, paper_version_id, preparation_id, user, lambda scope: repository.prepare(
        scope, body, enabled=runtime is not None and runtime.enabled))


@router.post(ROOT + "/{execution_id}/controls")
def control(topic_id: str, paper_version_id: str, preparation_id: str, execution_id: str, body: Control,
            request: Request, user=Depends(require_product_user)):
    runtime = _runtime(request)
    return _call(topic_id, paper_version_id, preparation_id, user, lambda scope: repository.control(
        scope, execution_id, body, enabled=runtime is not None and runtime.enabled))


@router.post(ROOT + "/{execution_id}/dispatch")
def dispatch(topic_id: str, paper_version_id: str, preparation_id: str, execution_id: str, body: Dispatch,
             request: Request, user=Depends(require_product_user)):
    runtime = _runtime(request)
    return _call(topic_id, paper_version_id, preparation_id, user, lambda scope:
        runtime.dispatch(scope, execution_id, body) if runtime is not None
        else repository.claim(scope, execution_id, body, enabled=False)[0])


@router.get(TRANSPORT_ROOT)
def transport_listing(topic_id: str, paper_version_id: str, preparation_id: str, request: Request,
                      page: int = Query(1, ge=1), page_size: int = Query(10, ge=1, le=50, alias="pageSize"),
                      user=Depends(require_product_user)):
    # Capabilities describe this registered surface, not installed tools, account
    # credentials or arbitrary app-state injections. No live issuer is wired.
    def listing(scope: Scope) -> dict[str, Any]:
        result = history.listing(scope, page, page_size)
        return dict(result, items=[offline.project(item) for item in result["items"]], offline=offline.describe(_offline(request)),
            capabilities=dict(create=False, approve=False, dispatch=False,
            cancel=True, recover=True, save_result=True, reason="paper_review_execution_not_configured",
            issuer_status="NOT_CONFIGURED", provider_status="UNREGISTERED", live_execution="NOT_VERIFIED"))
    return _call(topic_id, paper_version_id, preparation_id, user, listing)


@router.get(TRANSPORT_ROOT + "/{execution_id}")
def transport_detail(topic_id: str, paper_version_id: str, preparation_id: str, execution_id: str,
                     user=Depends(require_product_user)):
    return _call(topic_id, paper_version_id, preparation_id, user, lambda scope: offline.project(history.detail(scope, execution_id)))


@router.post(TRANSPORT_ROOT + "/{execution_id}/controls")
def transport_control(topic_id: str, paper_version_id: str, preparation_id: str, execution_id: str,
                      body: TransportControl, user=Depends(require_product_user)):
    def perform(scope: Scope) -> dict[str, Any]:
        repository.control(scope, execution_id, Control(**body.model_dump()), enabled=False, schema=transport.SCHEMA)
        # Re-read after commit. A lost response is safe: the exact control ID is
        # replayed by the repository without running a process or issuing a grant.
        return offline.project(history.detail(scope, execution_id))
    return _call(topic_id, paper_version_id, preparation_id, user, perform)


@router.post(TRANSPORT_ROOT + "/offline-requests")
def offline_prepare(topic_id: str, paper_version_id: str, preparation_id: str, body: Prepare, request: Request,
                    user=Depends(require_product_user)):
    return _call(topic_id, paper_version_id, preparation_id, user, lambda scope: offline.prepare(scope, body, _offline(request)))


@router.post(TRANSPORT_ROOT + "/{execution_id}/offline-approval")
def offline_approve(topic_id: str, paper_version_id: str, preparation_id: str, execution_id: str,
                    body: offline.Approve, request: Request, user=Depends(require_product_user)):
    return _call(topic_id, paper_version_id, preparation_id, user,
        lambda scope: offline.act(scope, execution_id, "approve", body, _offline(request)))


@router.post(TRANSPORT_ROOT + "/{execution_id}/offline-dispatch")
def offline_dispatch(topic_id: str, paper_version_id: str, preparation_id: str, execution_id: str,
                     body: Dispatch, request: Request, user=Depends(require_product_user)):
    return _call(topic_id, paper_version_id, preparation_id, user,
        lambda scope: offline.act(scope, execution_id, "dispatch", body, _offline(request)))


@router.post(TRANSPORT_ROOT + "/{execution_id}/offline-revocation")
def offline_revoke(topic_id: str, paper_version_id: str, preparation_id: str, execution_id: str,
                   body: Dispatch, request: Request, user=Depends(require_product_user)):
    return _call(topic_id, paper_version_id, preparation_id, user,
        lambda scope: offline.act(scope, execution_id, "revoke", body, _offline(request)))
