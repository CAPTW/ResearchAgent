"""Development-only synthetic Messages actions; no user-selected runtime or key."""
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.review_execution import TRANSPORT_ROOT, _call as review_call
from app.auth.security import require_product_user
from app.domain.review_execution import Dispatch, Scope
from app.services import review_anthropic_execution as service

router = APIRouter(tags=['paper-anthropic-offline-execution'])


def _runtime(request: Request) -> service.AnthropicOfflineRuntime | None:
    if not getattr(request.app.state, 'runtime_bootstrap_ok', False):
        return None
    runtime = getattr(request.app.state, 'paper_anthropic_offline_runtime', None)
    return runtime if isinstance(runtime, service.AnthropicOfflineRuntime) else None


def _call(topic: str, version: str, preparation: str, user: Any, operation: Callable[[Scope], dict[str, Any]]) -> dict[str, Any]:
    def guarded(scope: Scope) -> dict[str, Any]:
        try:
            return operation(scope)
        except ValueError as error:
            if str(error) == 'paper_anthropic_offline_disabled':
                raise HTTPException(409, detail=str(error)) from None
            raise
    return review_call(topic, version, preparation, user, guarded)


@router.get(TRANSPORT_ROOT + '/anthropic-preview')
def preview(topic_id: str, paper_version_id: str, preparation_id: str, request: Request, user=Depends(require_product_user)):
    return _call(topic_id, paper_version_id, preparation_id, user, lambda scope: service.preview(_runtime(request), scope))


@router.post(TRANSPORT_ROOT + '/anthropic-requests')
def prepare(topic_id: str, paper_version_id: str, preparation_id: str, request: Request, body: service.Prepare, user=Depends(require_product_user)):
    return _call(topic_id, paper_version_id, preparation_id, user, lambda scope: service.prepare(_runtime(request), scope, body))


@router.post(TRANSPORT_ROOT + '/{execution_id}/anthropic-offline-approval')
def approve(topic_id: str, paper_version_id: str, preparation_id: str, execution_id: str, request: Request,
            body: service.Approve, user=Depends(require_product_user)):
    return _call(topic_id, paper_version_id, preparation_id, user, lambda scope: service.act(_runtime(request), scope, execution_id, 'approve', body))


@router.post(TRANSPORT_ROOT + '/{execution_id}/anthropic-offline-dispatch')
def dispatch(topic_id: str, paper_version_id: str, preparation_id: str, execution_id: str, request: Request,
             body: Dispatch, user=Depends(require_product_user)):
    return _call(topic_id, paper_version_id, preparation_id, user, lambda scope: service.act(_runtime(request), scope, execution_id, 'dispatch', body))


@router.post(TRANSPORT_ROOT + '/{execution_id}/anthropic-offline-revocation')
def revoke(topic_id: str, paper_version_id: str, preparation_id: str, execution_id: str, request: Request,
           body: Dispatch, user=Depends(require_product_user)):
    return _call(topic_id, paper_version_id, preparation_id, user, lambda scope: service.act(_runtime(request), scope, execution_id, 'revoke', body))
