"""Loopback local-admin boundary for an explicitly injected native composition."""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.api.model_connections import _local_credential_request
from app.api.review_execution import TRANSPORT_ROOT, _call as review_call
from app.auth.config import get_auth_settings
from app.auth.resource_access import resource_actor_for_user
from app.auth.security import require_admin_user
from app.domain.review_execution import Scope
from app.services import review_anthropic_native as service
from app.services.local_anthropic_app import LocalAnthropicAppRuntime, OwnerDecision, decide as decide_local_owner

router = APIRouter(tags=["paper-anthropic-native-execution"])
HEADERS = {"Cache-Control": "no-store"}
_CONFLICTS = service._REASONS | {"paper_approval_source_unavailable", "paper_approval_source_mismatch", "paper_approval_not_ready",
    "paper_approval_target_mismatch", "paper_review_authority_conflict", "paper_review_authority_expired"}


def _runtime(request: Request, user: Any) -> service.NativeRuntime | None:
    try:
        _local_credential_request(request, intent_header="x-xrml-paper-anthropic-action")
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), **HEADERS}
        raise
    if (get_auth_settings().auth_mode != "disabled" or user.user_id != "local-internal-user"
            or user.auth_source != "disabled" or not user.is_admin):
        raise HTTPException(403, detail="paper_anthropic_native_local_actor_required", headers=HEADERS)
    if not getattr(request.app.state, "runtime_bootstrap_ok", False):
        raise HTTPException(503, detail="paper_anthropic_native_storage_unavailable", headers=HEADERS)
    runtime = getattr(request.app.state, "paper_anthropic_native_runtime", None)
    return runtime if isinstance(runtime, (service.AnthropicNativeRuntime, LocalAnthropicAppRuntime)) else None


def _call(topic: str, version: str, preparation: str, user: Any, operation: Callable[[Scope], Any]) -> Any:
    def guarded(scope: Scope) -> Any:
        try:
            return operation(scope)
        except PermissionError:
            raise HTTPException(403, detail="paper_anthropic_native_actor_denied", headers=HEADERS) from None
        except ValueError as exc:
            if str(exc) in _CONFLICTS:
                raise HTTPException(409, detail=str(exc), headers=HEADERS) from None
            raise
    try:
        return review_call(topic, version, preparation, user, guarded)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), **HEADERS}
        raise


async def _command(request: Request, user: Any, topic: str, version: str, preparation: str, model: Any,
                   operation: Callable[[service.NativeRuntime | None, Scope, Any], dict[str, Any]]) -> JSONResponse:
    runtime = _runtime(request, user)
    # Authorize the resource before inspecting browser-supplied data. Never let
    # automatic validation echo an accidentally pasted key/proof in a 422 body.
    await run_in_threadpool(_call, topic, version, preparation, user, lambda _: None)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 4096:
            raise HTTPException(413, detail="paper_anthropic_native_command_invalid", headers=HEADERS)
    try:
        body = model.model_validate_json(bytes(raw), strict=True)
    except ValueError:
        raise HTTPException(422, detail="paper_anthropic_native_command_invalid", headers=HEADERS) from None
    result = await run_in_threadpool(_call, topic, version, preparation, user, lambda scope: operation(runtime, scope, body))
    return JSONResponse(result, headers=HEADERS)


@router.get(TRANSPORT_ROOT + "/anthropic-native-preview")
def preview(topic_id: str, paper_version_id: str, preparation_id: str, request: Request, user=Depends(require_admin_user)):
    runtime = _runtime(request, user)
    return JSONResponse(_call(topic_id, paper_version_id, preparation_id, user, lambda scope: service.preview(runtime, scope)), headers=HEADERS)


@router.post(TRANSPORT_ROOT + "/anthropic-native-requests")
async def prepare(topic_id: str, paper_version_id: str, preparation_id: str, request: Request, user=Depends(require_admin_user)):
    return await _command(request, user, topic_id, paper_version_id, preparation_id, service.Prepare, service.prepare)


@router.get(TRANSPORT_ROOT + "/{execution_id}/anthropic-native-approval")
def approval(topic_id: str, paper_version_id: str, preparation_id: str, execution_id: str, request: Request,
             user=Depends(require_admin_user)):
    runtime = _runtime(request, user)
    return JSONResponse(_call(topic_id, paper_version_id, preparation_id, user,
        lambda scope: service.approval_preview(runtime, resource_actor_for_user(user), scope, execution_id)), headers=HEADERS)


@router.post(TRANSPORT_ROOT + "/{execution_id}/anthropic-native-approval")
async def approve(topic_id: str, paper_version_id: str, preparation_id: str, execution_id: str, request: Request,
                  user=Depends(require_admin_user)):
    return await _command(request, user, topic_id, paper_version_id, preparation_id, service.Approve,
        lambda runtime, scope, body: service.approve(runtime, resource_actor_for_user(user), scope, execution_id, body))


@router.post(TRANSPORT_ROOT + "/{execution_id}/anthropic-native-dispatch")
async def dispatch(topic_id: str, paper_version_id: str, preparation_id: str, execution_id: str, request: Request,
                   user=Depends(require_admin_user)):
    return await _command(request, user, topic_id, paper_version_id, preparation_id, service.Dispatch,
        lambda runtime, scope, body: service.dispatch(runtime, scope, execution_id, body))


@router.post(TRANSPORT_ROOT + "/{execution_id}/anthropic-native-revocation")
async def revoke(topic_id: str, paper_version_id: str, preparation_id: str, execution_id: str, request: Request,
                 user=Depends(require_admin_user)):
    return await _command(request, user, topic_id, paper_version_id, preparation_id, service.Dispatch,
        lambda runtime, scope, body: service.revoke(scope, execution_id, body))


@router.get(TRANSPORT_ROOT + "/{execution_id}/anthropic-native-status")
def status(topic_id: str, paper_version_id: str, preparation_id: str, execution_id: str, request: Request,
           user=Depends(require_admin_user)):
    runtime = _runtime(request, user)
    return JSONResponse(_call(topic_id, paper_version_id, preparation_id, user,
        lambda scope: service.status(runtime, scope, execution_id)), headers=HEADERS)


@router.post(TRANSPORT_ROOT + "/{execution_id}/anthropic-native-owner-decision")
async def owner_decision(topic_id: str, paper_version_id: str, preparation_id: str, execution_id: str, request: Request,
                         user=Depends(require_admin_user)):
    def decide(runtime, scope, body):
        return decide_local_owner(runtime, resource_actor_for_user(user), scope, execution_id, body)
    return await _command(request, user, topic_id, paper_version_id, preparation_id, OwnerDecision, decide)
