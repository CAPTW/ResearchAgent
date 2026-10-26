"""Topic and owner scoped offline capture import; never invokes a Provider."""
import json
import sqlite3
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.auth.resource_access import ResourceAction, resource_actor_context, resource_actor_for_user
from app.auth.security import require_product_user
from app.db.sqlite_integrity import SqliteIntegrityError
from app.domain import saved_analysis as domain
from app.domain import journal_review, saved_analysis_journal
from app.repositories import saved_analyses as store
from app.repositories import saved_analysis_journals as journals
from app.repositories.shared_state import SharedStateError
from app.services import topic_profiles
from app.services import captured_analyses
from app.api.local_anthropic_trial import _runtime

router = APIRouter(prefix="/api/topics/{topic_id}/saved-analyses", tags=["saved-analyses"])


def _call(topic: str, user: Any, operation: Callable[[str], dict[str, Any]]) -> JSONResponse:
    actor = resource_actor_for_user(user)
    try:
        with resource_actor_context(actor):
            profile = topic_profiles.get_topic_profile(topic, actor=actor)
            topic_profiles.authorize_topic_profile(profile, actor=actor, action=ResourceAction.UPDATE)
            if not actor.subject:
                raise KeyError()
            return JSONResponse(operation(actor.subject), headers={"Cache-Control": "no-store"})
    except (KeyError, FileNotFoundError, PermissionError):
        raise HTTPException(404, "saved_analysis_not_found") from None
    except ValueError as exc:
        code = str(exc)
        if code in {"anthropic_trial_disabled", "anthropic_trial_binding_changed", "anthropic_trial_capture_changed", "anthropic_trial_analysis_unavailable",
                    "saved_analysis_request_conflict", "saved_analysis_review_conflict", "saved_analysis_journal_source_changed",
                    "journal_review_request_conflict", "journal_review_conflict", "journal_review_binding_changed", "journal_review_export_not_accepted"}:
            raise HTTPException(409, code) from None
        if code in {"saved_analysis_bundle_invalid", "saved_analysis_journal_selection_invalid", "journal_review_entries_invalid",
                    "journal_review_incomplete", "journal_review_comparison_same", "synthesis_export_format_invalid"}:
            raise HTTPException(422, code) from None
        raise HTTPException(503, "saved_analysis_unavailable") from None
    except (OSError, sqlite3.Error, SqliteIntegrityError, SharedStateError):
        raise HTTPException(503, "saved_analysis_unavailable") from None


async def _body(request: Request, maximum: int) -> Any:
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > maximum:
            raise HTTPException(413, "saved_analysis_input_too_large")
    try:
        return json.loads(data)
    except (ValueError, RecursionError):
        raise HTTPException(422, "saved_analysis_bundle_invalid") from None


@router.get("")
def listing(topic_id: str, page: int = Query(1, ge=1, le=1_000_000), limit: int = Query(10, ge=1, le=100), user=Depends(require_product_user)):
    return _call(topic_id, user, lambda actor: store.listing(topic_id, actor, page=page, limit=limit))


@router.post("")
async def save(topic_id: str, request: Request, user=Depends(require_product_user)):
    value = await _body(request, domain.MAX_BUNDLE_BYTES)
    return await run_in_threadpool(_call, topic_id, user, lambda actor: store.save(topic_id, actor, value))


@router.get("/{analysis_id}")
def detail(topic_id: str, analysis_id: str, user=Depends(require_product_user)):
    return _call(topic_id, user, lambda actor: store.detail(topic_id, analysis_id, actor))


@router.get('/sources/anthropic-trial')
def captured_trial(topic_id: str, request: Request, user=Depends(require_product_user)):
    runtime = _runtime(request, user, intent_header='x-xrml-analysis-save')
    return _call(topic_id, user, lambda actor: captured_analyses.preview(runtime, actor))


@router.post('/sources/anthropic-trial')
async def save_captured_trial(topic_id: str, request: Request, user=Depends(require_product_user)):
    runtime = _runtime(request, user, intent_header='x-xrml-analysis-save')
    value = await _body(request, 1024)
    try:
        body = captured_analyses.Save.model_validate(value)
    except ValueError:
        raise HTTPException(422, 'saved_analysis_capture_command_invalid') from None
    return await run_in_threadpool(_call, topic_id, user, lambda actor: captured_analyses.save(runtime, topic_id, actor, body))


@router.post("/{analysis_id}/decisions")
async def decide(topic_id: str, analysis_id: str, request: Request, user=Depends(require_product_user)):
    value = await _body(request, 40_000)
    try:
        body = domain.Decision.model_validate(value)
    except ValueError:
        raise HTTPException(422, "saved_analysis_review_invalid") from None
    return await run_in_threadpool(_call, topic_id, user, lambda actor: store.decide(topic_id, analysis_id, actor, body))


@router.get('/{analysis_id}/journals/components')
def journal_components(topic_id: str, analysis_id: str, user=Depends(require_product_user)):
    return _call(topic_id, user, lambda actor: journals.components(topic_id, analysis_id, actor))


@router.get('/{analysis_id}/journals')
def journal_list(topic_id: str, analysis_id: str, page: int = Query(1, ge=1, le=1_000_000), limit: int = Query(10, ge=1, le=100), user=Depends(require_product_user)):
    return _call(topic_id, user, lambda actor: journals.listing(topic_id, analysis_id, actor, page, limit))


@router.post('/{analysis_id}/journals')
async def journal_create(topic_id: str, analysis_id: str, request: Request, user=Depends(require_product_user)):
    value = await _body(request, 40_000)
    try:
        body = saved_analysis_journal.Create.model_validate(value)
    except ValueError:
        raise HTTPException(422, 'saved_analysis_journal_selection_invalid') from None
    return await run_in_threadpool(_call, topic_id, user, lambda actor: journals.create(topic_id, analysis_id, actor, body))


@router.get('/{analysis_id}/journals/{candidate_id}')
def journal_detail(topic_id: str, analysis_id: str, candidate_id: str, user=Depends(require_product_user)):
    return _call(topic_id, user, lambda actor: journals.detail(topic_id, analysis_id, actor, candidate_id))


@router.get('/{analysis_id}/journals/{candidate_id}/reviews')
def journal_history(topic_id: str, analysis_id: str, candidate_id: str, page: int = Query(1, ge=1, le=1_000_000), limit: int = Query(10, ge=1, le=100), user=Depends(require_product_user)):
    return _call(topic_id, user, lambda actor: journals.history(topic_id, analysis_id, actor, candidate_id, page, limit))


@router.post('/{analysis_id}/journals/{candidate_id}/reviews')
async def journal_save_review(topic_id: str, analysis_id: str, candidate_id: str, request: Request, user=Depends(require_product_user)):
    value = await _body(request, 400_000)
    try:
        body = journal_review.Save.model_validate(value)
        # Fail invalid Unicode as user input, before JSON storage or digest work.
        from app.domain.journal_exchange import preserved_json
        preserved_json(body.model_dump()).encode('utf-8')
    except ValueError:
        raise HTTPException(422, 'journal_review_invalid') from None
    def save(actor: str) -> dict[str, Any]:
        review, created = journals.save_review(topic_id, analysis_id, actor, candidate_id, body)
        return dict(review=review, created=created)
    return await run_in_threadpool(_call, topic_id, user, save)


@router.get('/{analysis_id}/journals/{candidate_id}/reviews/{review_id}')
def journal_review_detail(topic_id: str, analysis_id: str, candidate_id: str, review_id: str, user=Depends(require_product_user)):
    return _call(topic_id, user, lambda actor: journals.review_detail(topic_id, analysis_id, actor, candidate_id, review_id))


@router.get('/{analysis_id}/journals/{candidate_id}/reviews/{review_id}/comparison')
def journal_compare(topic_id: str, analysis_id: str, candidate_id: str, review_id: str, from_review_id: str = Query(min_length=1, max_length=100), user=Depends(require_product_user)):
    return _call(topic_id, user, lambda actor: journals.compare(topic_id, analysis_id, actor, candidate_id, review_id, from_review_id))


@router.get('/{analysis_id}/journals/{candidate_id}/reviews/{review_id}/export')
def journal_export(topic_id: str, analysis_id: str, candidate_id: str, review_id: str, format: str = Query('markdown'), user=Depends(require_product_user)):
    return _call(topic_id, user, lambda actor: journals.export(topic_id, analysis_id, actor, candidate_id, review_id, format))
