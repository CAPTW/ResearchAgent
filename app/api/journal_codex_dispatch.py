"""Manually use existing server authority; never accept browser proof or issue it."""
from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.local_synthesis import ROOT, invoke
from app.auth.security import require_product_user
from app.services import journal_codex_dispatch as service

router = APIRouter(tags=["journal-codex-dispatch"])
TARGET = ROOT + "/{job_id}/journal-executions/{execution_id}/codex-dispatch"


def _call(topic, user, operation):
    def guarded(actor):
        try:
            return operation(actor.subject or actor.actor_type)
        except RuntimeError as exc:
            if str(exc) == "journal_worker_start_failed":
                raise HTTPException(503, detail="journal_worker_start_failed") from None
            raise
        except ValueError as exc:
            code = str(exc)
            conflicts = {"journal_codex_dispatch_disabled", "journal_codex_dispatch_actor_mismatch",
                "journal_codex_dispatch_binding_changed", "journal_codex_dispatch_authority_mismatch",
                "journal_codex_dispatch_target_mismatch", "journal_codex_runtime_changed",
                "journal_dispatch_closed", "journal_dispatch_conflict", "journal_authority_changed",
                "journal_authority_revoked", "journal_dispatch_authority_expired", "journal_dispatch_authority_mismatch",
                "journal_dispatch_proof_rejected", "journal_dispatch_budget_exhausted", "journal_execution_conflict",
                "journal_execution_unknown_outcome", "journal_execution_attempts_exhausted"}
            if code in conflicts:
                raise HTTPException(409, detail=code) from None
            raise
    return invoke(topic, user, guarded)


@router.get(TARGET)
def status(topic_id: str, job_id: str, execution_id: str, request: Request, user=Depends(require_product_user)):
    return _call(topic_id, user, lambda actor: service.status(
        request.app.state.journal_codex_runtime, topic_id, job_id, execution_id, actor))


@router.post(TARGET)
def dispatch(topic_id: str, job_id: str, execution_id: str, body: service.Request,
             request: Request, user=Depends(require_product_user)):
    return _call(topic_id, user, lambda actor: dict(execution=service.dispatch(
        request.app.state.journal_codex_runtime, topic_id, job_id, execution_id, body, actor)))
