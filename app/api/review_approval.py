"""Explicit authenticated app approval; no browser-provided grant or proof."""
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.review_execution import TRANSPORT_ROOT, _call as review_call
from app.auth.resource_access import ResourceActor, resolve_resource_actor
from app.auth.security import require_product_user
from app.domain.review_execution import Scope
from app.services import review_approval as service

router = APIRouter(tags=["paper-review-approval"])
TARGET = TRANSPORT_ROOT + "/{execution_id}/codex-approval"


def _runtime(request: Request) -> service.PaperApprovalRuntime | None:
    if not getattr(request.app.state, "runtime_bootstrap_ok", False):
        return None
    return getattr(request.app.state, "paper_approval_runtime", None)


def _call(topic: str, version: str, preparation: str, user: Any,
          operation: Callable[[ResourceActor, Scope], dict[str, Any]]) -> dict[str, Any]:
    def guarded(scope: Scope) -> dict[str, Any]:
        try:
            return operation(resolve_resource_actor(), scope)
        except PermissionError as exc:
            raise HTTPException(403, detail=str(exc)) from None
        except ValueError as exc:
            if str(exc) in {"paper_approval_disabled", "paper_approval_changed", "paper_approval_target_mismatch",
                            "paper_approval_not_ready", "paper_approval_source_mismatch"}:
                raise HTTPException(409, detail=str(exc)) from None
            if str(exc) == "paper_approval_source_unavailable":
                raise HTTPException(503, detail=str(exc)) from None
            raise
    return review_call(topic, version, preparation, user, guarded)


@router.get(TARGET)
def preview(topic_id: str, paper_version_id: str, preparation_id: str, execution_id: str,
            request: Request, user=Depends(require_product_user)):
    return _call(topic_id, paper_version_id, preparation_id, user,
        lambda actor, scope: service.preview(_runtime(request), actor, scope, execution_id))


@router.post(TARGET)
def approve(topic_id: str, paper_version_id: str, preparation_id: str, execution_id: str, body: service.Approve,
            request: Request, user=Depends(require_product_user)):
    return _call(topic_id, paper_version_id, preparation_id, user,
        lambda actor, scope: service.approve(_runtime(request), actor, scope, execution_id, body))
