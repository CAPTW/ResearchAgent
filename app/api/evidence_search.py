from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.config import AuthSettings, get_auth_settings
from app.auth.security import require_product_user
from app.auth.security_types import ProductUser
from app.repositories.evidence_search import EvidenceSearchTooExpensive
from app.schemas.evidence_search import (
    CorpusKind,
    EvidenceDetailResponse,
    EvidenceSearchResponse,
    ReviewLevel,
    ReviewSource,
)
from app.services.evidence_search import (
    EvidenceSearchNotFound,
    EvidenceSearchService,
    EvidenceSearchValidationError,
)

router = APIRouter(tags=["evidence-search"])
_SAFE_FILTER_VALUE = re.compile(r"[A-Za-z0-9._:-]{1,200}")


def get_evidence_search_service() -> EvidenceSearchService:
    return EvidenceSearchService()


def _error(status_code: int, *, code: str, message: str, retryable: bool = False) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "retryable": retryable},
    )


def _parse_csv(
    raw: str | None,
    *,
    field_name: str,
    maximum: int,
    allowed: set[str] | None = None,
) -> tuple[str, ...]:
    if raw is None:
        return ()
    parts = [part.strip() for part in raw.split(",")]
    if not parts or any(not part for part in parts):
        raise _error(422, code=f"invalid_{field_name}", message=f"{field_name} contains an empty value.")
    if len(parts) > maximum:
        raise _error(422, code=f"invalid_{field_name}", message=f"{field_name} accepts at most {maximum} values.")
    values = tuple(dict.fromkeys(parts))
    if allowed is not None and any(value not in allowed for value in values):
        raise _error(422, code=f"invalid_{field_name}", message=f"{field_name} contains an unsupported value.")
    if allowed is None and any(_SAFE_FILTER_VALUE.fullmatch(value) is None for value in values):
        raise _error(422, code=f"invalid_{field_name}", message=f"{field_name} contains a malformed value.")
    return values


def _raise_service_error(exc: Exception) -> None:
    if isinstance(exc, EvidenceSearchTooExpensive):
        raise _error(
            503,
            code="search_too_expensive",
            message="The evidence search exceeded its local read budget.",
            retryable=True,
        ) from exc
    if isinstance(exc, EvidenceSearchNotFound):
        raise _error(404, code="evidence_result_not_found", message="The evidence result was not found.") from exc
    if isinstance(exc, EvidenceSearchValidationError):
        status_code = 404 if exc.code == "invalid_result_id" else 422
        raise _error(status_code, code=exc.code, message=exc.message) from exc
    raise exc


@router.get("/api/evidence/search", response_model=EvidenceSearchResponse)
def search_evidence(
    q: str = Query(default="", max_length=200),
    topic_ids: str | None = Query(default=None, max_length=4096),
    run_ids: str | None = Query(default=None, max_length=4096),
    corpus_kinds: str | None = Query(default=None, max_length=512),
    review_sources: str | None = Query(default=None, max_length=512),
    provider_ids: str | None = Query(default=None, max_length=2048),
    review_levels: str | None = Query(default=None, max_length=512),
    has_evidence: bool | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=2048),
    user: ProductUser = Depends(require_product_user),
    settings: AuthSettings = Depends(get_auth_settings),
    service: EvidenceSearchService = Depends(get_evidence_search_service),
) -> EvidenceSearchResponse:
    try:
        return service.search(
            rawQuery=q,
            topicIds=_parse_csv(topic_ids, field_name="topic_ids", maximum=20),
            runIds=_parse_csv(run_ids, field_name="run_ids", maximum=20),
            corpusKinds=_parse_csv(
                corpus_kinds,
                field_name="corpus_kinds",
                maximum=len(CorpusKind),
                allowed={value.value for value in CorpusKind},
            ),
            reviewSources=_parse_csv(
                review_sources,
                field_name="review_sources",
                maximum=len(ReviewSource),
                allowed={value.value for value in ReviewSource},
            ),
            providerIds=_parse_csv(provider_ids, field_name="provider_ids", maximum=10),
            reviewLevels=_parse_csv(
                review_levels,
                field_name="review_levels",
                maximum=len(ReviewLevel),
                allowed={value.value for value in ReviewLevel},
            ),
            hasEvidence=has_evidence,
            limit=limit,
            cursor=cursor,
            authEnabled=settings.auth_enabled,
            userId=user.user_id,
        )
    except (EvidenceSearchTooExpensive, EvidenceSearchNotFound, EvidenceSearchValidationError) as exc:
        _raise_service_error(exc)
        raise AssertionError("unreachable") from exc


@router.get("/api/evidence/items/{result_id}", response_model=EvidenceDetailResponse, response_model_exclude_none=True)
def get_evidence_item(
    result_id: str,
    occurrence_run_id: str | None = Query(default=None, max_length=200),
    user: ProductUser = Depends(require_product_user),
    settings: AuthSettings = Depends(get_auth_settings),
    service: EvidenceSearchService = Depends(get_evidence_search_service),
) -> EvidenceDetailResponse:
    if len(result_id) > 2048:
        raise _error(404, code="invalid_result_id", message="The result ID is invalid.")
    if occurrence_run_id and _SAFE_FILTER_VALUE.fullmatch(occurrence_run_id) is None:
        raise _error(422, code="invalid_occurrence_run_id", message="The occurrence run ID is malformed.")
    try:
        return service.detail(
            resultId=result_id,
            occurrenceRunId=occurrence_run_id,
            authEnabled=settings.auth_enabled,
            userId=user.user_id,
        )
    except (EvidenceSearchTooExpensive, EvidenceSearchNotFound, EvidenceSearchValidationError) as exc:
        _raise_service_error(exc)
        raise AssertionError("unreachable") from exc
