from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.config import AuthSettings, get_auth_settings
from app.auth.security import require_product_user
from app.auth.security_types import ProductUser
from app.schemas.global_search import GlobalSearchResponse, SearchHelpResponse
from app.services.global_search import GlobalSearchService, SearchQueryError

router = APIRouter(tags=["global-search"])


def get_global_search_service() -> GlobalSearchService:
    return GlobalSearchService()


def _error(exc: SearchQueryError) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": exc.code, "message": exc.message})


@router.get("/api/search/help", response_model=SearchHelpResponse)
def get_search_help() -> dict:
    return GlobalSearchService.search_help()


@router.get("/api/search", response_model=GlobalSearchResponse)
def global_search(
    q: str = Query(default="", max_length=200),
    kind: str | None = Query(default=None, max_length=200),
    kinds: str | None = Query(default=None, max_length=200),
    topic_ids: str | None = Query(default=None, max_length=4096),
    limit: int = Query(default=25, ge=1, le=100),
    user: ProductUser = Depends(require_product_user),
    settings: AuthSettings = Depends(get_auth_settings),
    service: GlobalSearchService = Depends(get_global_search_service),
) -> GlobalSearchResponse:
    raw_kinds = kinds if kinds is not None else kind
    selected_kinds = tuple(part.strip() for part in raw_kinds.split(",")) if raw_kinds is not None else None
    selected_topics = tuple(part.strip() for part in (topic_ids or "").split(",") if part.strip())
    try:
        result = service.search(
            q,
            topic_ids=selected_topics,
            kinds=selected_kinds,
            limit=limit,
            auth_enabled=settings.auth_enabled,
            user_id=user.user_id,
        )
        return GlobalSearchResponse.model_validate(result)
    except SearchQueryError as exc:
        raise _error(exc) from exc
