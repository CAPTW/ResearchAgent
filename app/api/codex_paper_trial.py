"""Separate first-output qualification, not ordinary authority or adoption."""
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.api.review_execution import TRANSPORT_ROOT, _call as review_call
from app.auth.resource_access import ResourceActor, resolve_resource_actor
from app.auth.security import require_product_user
from app.domain import codex_paper_trial as domain
from app.domain.review_execution import Scope
from app.repositories import codex_paper_trials as repository
from app.services import codex_paper_trial as service

router = APIRouter(tags=["codex-paper-qualification"])
BASE = TRANSPORT_ROOT.removesuffix("/transport-executions")
ROOT = BASE + "/codex-qualification-attempts"
HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def _runtime(request: Request) -> service.PaperTrialRuntime | None:
    if not getattr(request.app.state, "runtime_bootstrap_ok", False):
        return None
    return getattr(request.app.state, "paper_trial_runtime", None)


def _call(topic: str, version: str, preparation: str, user: Any, response: Response,
          operation: Callable[[ResourceActor, Scope], dict[str, Any]]) -> dict[str, Any]:
    response.headers.update(HEADERS)
    def guarded(scope: Scope) -> dict[str, Any]:
        try:
            actor = resolve_resource_actor()
            service.authenticate(actor, scope)
            return operation(actor, scope)
        except PermissionError:
            raise HTTPException(403, detail="codex_trial_actor_not_allowed", headers=HEADERS) from None
        except ValueError as exc:
            code = str(exc)
            if code.startswith("codex_trial_") and "integrity" not in code:
                raise HTTPException(409, detail=code, headers=HEADERS) from None
            if code in {"paper_codex_closed", "paper_codex_runtime_changed"}:
                raise HTTPException(409, detail=code, headers=HEADERS) from None
            raise
    return review_call(topic, version, preparation, user, guarded)


@router.get(BASE + "/codex-qualification")
def preview(topic_id: str, paper_version_id: str, preparation_id: str, request: Request, response: Response,
            user=Depends(require_product_user)):
    return _call(topic_id, paper_version_id, preparation_id, user, response,
        lambda actor, scope: service.preview(_runtime(request), actor, scope))


@router.post(ROOT)
def submit(topic_id: str, paper_version_id: str, preparation_id: str, body: domain.Submit, request: Request,
           response: Response, user=Depends(require_product_user)):
    return _call(topic_id, paper_version_id, preparation_id, user, response,
        lambda actor, scope: service.submit(_runtime(request), actor, scope, body))


@router.get(ROOT)
def listing(topic_id: str, paper_version_id: str, preparation_id: str, response: Response,
            page: int = Query(1, ge=1), page_size: int = Query(10, ge=1, le=20, alias="pageSize"),
            user=Depends(require_product_user)):
    return _call(topic_id, paper_version_id, preparation_id, user, response,
        lambda actor, scope: repository.listing(scope, page, page_size))


@router.get(ROOT + "/{trial_id}")
def detail(topic_id: str, paper_version_id: str, preparation_id: str, trial_id: str, response: Response,
           user=Depends(require_product_user)):
    return _call(topic_id, paper_version_id, preparation_id, user, response,
        lambda actor, scope: repository.detail(scope, trial_id))


@router.post(ROOT + "/{trial_id}/control")
def control(topic_id: str, paper_version_id: str, preparation_id: str, trial_id: str, body: domain.Control,
            response: Response, user=Depends(require_product_user)):
    return _call(topic_id, paper_version_id, preparation_id, user, response,
        lambda actor, scope: repository.control(scope, trial_id, body, clock=service._now))
