"""Explicit isolated synthetic execution; never a live approval endpoint."""
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.api.local_synthesis import ROOT, invoke
from app.auth.security import require_product_user
from app.repositories import local_synthesis
from app.services import journal_offline as service

router = APIRouter(tags=["journal-offline"])
EXECUTION = ROOT + "/{job_id}/journal-executions/{execution_id}"


def _runtime(request: Request) -> service.OfflineJournalRuntime | None:
    return getattr(request.app.state, "journal_offline_runtime", None)


def _required(request: Request) -> service.OfflineJournalRuntime:
    runtime = _runtime(request)
    if runtime is None:
        raise HTTPException(409, detail="journal_offline_disabled")
    return runtime


def _call(topic: str, user: Any, operation: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    def guarded(actor):
        try:
            return operation(actor)
        except RuntimeError as exc:
            if str(exc) == "journal_worker_start_failed":
                raise HTTPException(503, detail="journal_worker_start_failed") from None
            raise
        except ValueError as exc:
            code = str(exc)
            conflicts = {
                "journal_offline_disabled", "journal_offline_target_mismatch", "journal_offline_actor_mismatch",
                "journal_offline_approval_mismatch", "journal_offline_approval_conflict", "journal_dispatch_closed",
                "journal_authority_conflict", "journal_authority_changed", "journal_authority_revoked",
                "journal_dispatch_authority_expired", "journal_dispatch_authority_mismatch", "journal_dispatch_proof_rejected",
                "journal_dispatch_budget_exhausted", "journal_execution_conflict", "journal_execution_request_conflict",
                "journal_dispatch_conflict", "journal_dispatch_authority_unavailable",
                "journal_execution_unknown_outcome", "journal_execution_attempts_exhausted",
            }
            if code in conflicts:
                raise HTTPException(409, detail=code) from None
            raise
    return invoke(topic, user, guarded)


@router.get(ROOT + "/{job_id}/journal-offline")
def capability(topic_id: str, job_id: str, request: Request, user=Depends(require_product_user)):
    def operation(_):
        local_synthesis.get(topic_id, job_id)
        return service.capabilities(_runtime(request))
    return _call(topic_id, user, operation)


@router.get(EXECUTION + "/offline")
def status(topic_id: str, job_id: str, execution_id: str, request: Request, user=Depends(require_product_user)):
    return _call(topic_id, user, lambda actor: service.status(
        _runtime(request), topic_id, job_id, execution_id, actor.subject or actor.actor_type))


@router.post(EXECUTION + "/offline-approvals")
def approve(topic_id: str, job_id: str, execution_id: str, body: service.Approve,
            request: Request, response: Response, user=Depends(require_product_user)):
    def operation(actor):
        result = _required(request).approve(topic_id, job_id, execution_id, body, actor.subject or actor.actor_type)
        response.status_code = 201 if result["created"] else 200
        return result
    return _call(topic_id, user, operation)


@router.post(EXECUTION + "/offline-dispatch")
def dispatch(topic_id: str, job_id: str, execution_id: str, body: service.OfflineDispatch,
             request: Request, user=Depends(require_product_user)):
    return _call(topic_id, user, lambda actor: dict(execution=_required(request).dispatch(
        topic_id, job_id, execution_id, body, actor.subject or actor.actor_type)))
