"""Paper Codex V2 server composition; browser input cannot supply runtime or proof."""
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.review_execution import TRANSPORT_ROOT, _call as review_call
from app.auth.security import require_product_user
from app.domain.review_execution import Scope
from app.services import review_codex_execution as service

router = APIRouter(tags=["paper-codex-execution"])


def _runtime(request: Request) -> service.CodexPaperRuntime | None:
    if not getattr(request.app.state, "runtime_bootstrap_ok", False):
        return None
    return getattr(request.app.state, "paper_codex_runtime", None)


def _call(topic: str, version: str, preparation: str, user: Any,
          operation: Callable[[Scope], dict[str, Any]]) -> dict[str, Any]:
    def guarded(scope: Scope) -> dict[str, Any]:
        try:
            return operation(scope)
        except ValueError as exc:
            code = str(exc)
            if code in {"paper_codex_disabled", "paper_codex_closed", "paper_codex_runtime_changed",
                        "paper_codex_target_mismatch", "paper_codex_binding_changed", "paper_codex_authority_missing",
                        "paper_codex_authority_mismatch", "paper_review_authority_revoked"}:
                raise HTTPException(409, detail=code) from None
            raise
    return review_call(topic, version, preparation, user, guarded)


@router.get(TRANSPORT_ROOT + "/codex-preview")
def preview(topic_id: str, paper_version_id: str, preparation_id: str, request: Request, user=Depends(require_product_user)):
    return _call(topic_id, paper_version_id, preparation_id, user, lambda scope: service.preview(_runtime(request), scope))


@router.post(TRANSPORT_ROOT + "/codex-requests")
def prepare(topic_id: str, paper_version_id: str, preparation_id: str, body: service.Prepare, request: Request,
            user=Depends(require_product_user)):
    return _call(topic_id, paper_version_id, preparation_id, user, lambda scope: service.prepare(_runtime(request), scope, body))


@router.get(TRANSPORT_ROOT + "/{execution_id}/codex-dispatch")
def status(topic_id: str, paper_version_id: str, preparation_id: str, execution_id: str, request: Request,
           user=Depends(require_product_user)):
    return _call(topic_id, paper_version_id, preparation_id, user,
                 lambda scope: service.status(_runtime(request), scope, execution_id))


@router.post(TRANSPORT_ROOT + "/{execution_id}/codex-dispatch")
def dispatch(topic_id: str, paper_version_id: str, preparation_id: str, execution_id: str, body: service.Dispatch,
             request: Request, user=Depends(require_product_user)):
    return _call(topic_id, paper_version_id, preparation_id, user,
                 lambda scope: service.dispatch(_runtime(request), scope, execution_id, body))
