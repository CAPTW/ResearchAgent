"""Register server-supplied Journal authority after app-user authorization."""
from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.local_synthesis import ROOT, invoke
from app.auth.security import require_product_user
from app.services import journal_approval as service

router = APIRouter(tags=["journal-approval"])
TARGET = ROOT + "/{job_id}/journal-executions/{execution_id}/codex-approval"


def _runtime(request: Request) -> service.JournalApprovalRuntime | None:
    if not getattr(request.app.state, "runtime_bootstrap_ok", False):
        return None
    return getattr(request.app.state, "journal_approval_runtime", None)


def _call(topic, user, operation):
    def guarded(actor):
        try:
            return operation(actor)
        except PermissionError as exc:
            raise HTTPException(403, detail=str(exc)) from None
        except ValueError as exc:
            code = str(exc)
            if code in {"journal_approval_disabled", "journal_approval_changed", "journal_approval_target_mismatch",
                        "journal_approval_not_ready", "journal_approval_source_mismatch", "journal_authority_conflict",
                        "journal_authority_clock_invalid", "journal_dispatch_authority_expired",
                        "journal_dispatch_budget_exhausted"}:
                raise HTTPException(409, detail=code) from None
            if code == "journal_approval_source_unavailable":
                raise HTTPException(503, detail=code) from None
            raise
        except RuntimeError:
            raise HTTPException(503, detail="journal_approval_unavailable") from None
    return invoke(topic, user, guarded)


@router.get(TARGET)
def preview(topic_id: str, job_id: str, execution_id: str, request: Request, user=Depends(require_product_user)):
    return _call(topic_id, user, lambda actor: service.preview(_runtime(request), actor, topic_id, job_id, execution_id))


@router.post(TARGET)
def approve(topic_id: str, job_id: str, execution_id: str, body: service.Approve,
            request: Request, user=Depends(require_product_user)):
    return _call(topic_id, user, lambda actor: service.approve(_runtime(request), actor, topic_id, job_id, execution_id, body))
