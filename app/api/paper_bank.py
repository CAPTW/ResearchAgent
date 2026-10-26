from __future__ import annotations

import sqlite3
from typing import Any, Callable
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.auth.resource_access import resource_actor_context, resource_actor_for_user
from app.auth.security import require_product_user
from app.db.sqlite_integrity import SqliteIntegrityError
from app.domain.review_preparation import Prepare as ReviewPreparation
from app.domain.review_preparation import Preview as ReviewPreparationPreview
from app.domain.review_offline import Execute as OfflineReviewRequest
from app.domain.review_pdf_extraction import Extract as PdfExtractionRequest
from app.repositories import paper_collections
from app.repositories.shared_state import SharedStateError
from app.repositories.file_locks import FileLockError
from app.schemas.paper_bank import (
    PaperBankDetailResponse,
    PaperBankExportRequest,
    PaperBankExportResponse,
    PaperBankResponse,
    PaperCollectionBulkSaveRequest,
    PaperCollectionBulkSaveResponse,
    PaperCollectionCreateRequest,
    PaperCollectionDeleteResponse,
    PaperCollectionListResponse,
    PaperCollectionRenameRequest,
    PaperCollectionResponse,
)
from app.services import paper_bank, paper_occurrences, review_input_history, review_preparation, review_offline, review_pdf_extraction

router = APIRouter(tags=["paper-bank"])


def _paper_bank_error(exc: Exception) -> HTTPException:
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail="topic_not_found")
    if isinstance(exc, paper_occurrences.OccurrenceIdentityInvalid):
        return HTTPException(status_code=503, detail=exc.code)
    if isinstance(exc, paper_bank.PaperVersionNotFound):
        return HTTPException(status_code=404, detail=exc.code)
    if isinstance(exc, paper_bank.PaperBankQueryInvalid):
        return HTTPException(status_code=422, detail=exc.detail or exc.code)
    if isinstance(exc, paper_bank.PaperBankUnavailable):
        return HTTPException(status_code=503, detail=exc.detail or exc.code)
    return HTTPException(status_code=500, detail="paper_bank_failed")


def _collection_error(exc: Exception) -> HTTPException:
    if isinstance(exc, paper_collections.CollectionNotFound):
        return HTTPException(status_code=404, detail=exc.code)
    if isinstance(exc, paper_collections.CollectionConflict):
        return HTTPException(status_code=409, detail=exc.detail or exc.code)
    if isinstance(exc, paper_collections.CollectionValidationError):
        return HTTPException(status_code=422, detail=exc.detail or exc.code)
    if isinstance(exc, SharedStateError):
        return HTTPException(status_code=503, detail="paper_collection_store_unavailable")
    return HTTPException(status_code=500, detail="paper_collection_failed")


@router.get("/api/topics/{topic_id}/paper-bank", response_model=PaperBankResponse)
def get_topic_paper_bank(
    topic_id: str,
    q: str = Query(default=""),
    provider: str | None = Query(default=None),
    collection_id: str | None = Query(default=None, alias="collectionId"),
    review_state: str | None = Query(default=None, alias="reviewState"),
    sort: str = Query(default="published_desc"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100, alias="pageSize"),
    year: str | None = Query(default=None),
    paper_type: str | None = Query(default=None, alias="paperType"),
    open_access: str | None = Query(default=None, alias="openAccess"),
    citation_min: int | None = Query(default=None, alias="citationMin"),
    impact_min: float | None = Query(default=None, alias="impactMin"),
    user=Depends(require_product_user),
):
    unsupported = {
        "paperType": paper_type,
        "citationMin": citation_min,
        "impactMin": impact_min,
    }
    selected_unsupported = sorted(key for key, value in unsupported.items() if value is not None)
    if selected_unsupported:
        raise HTTPException(
            status_code=422,
            detail={"code": "paper_bank_filter_unsupported", "filters": selected_unsupported},
        )
    actor = resource_actor_for_user(user)
    try:
        with resource_actor_context(actor):
            return paper_bank.list_topic_papers(
                topic_id,
                q=q,
                provider=provider,
                collection_id=collection_id,
                review_state=review_state,
                year=year,
                open_access=open_access,
                sort=sort,
                page=page,
                page_size=page_size,
                actor=actor,
            )
    except (
        FileNotFoundError,
        paper_bank.PaperBankError,
        paper_occurrences.OccurrenceIdentityInvalid,
    ) as exc:
        raise _paper_bank_error(exc) from exc


@router.post(
    "/api/topics/{topic_id}/paper-bank/export",
    response_model=PaperBankExportResponse,
)
def export_topic_paper_bank(
    topic_id: str,
    request: PaperBankExportRequest,
    user=Depends(require_product_user),
):
    actor = resource_actor_for_user(user)
    try:
        with resource_actor_context(actor):
            return paper_bank.export_topic_papers_csv(
                topic_id,
                q=request.q,
                provider=request.provider,
                collection_id=request.collectionId,
                year=request.year,
                review_state=request.reviewState,
                open_access=request.openAccess,
                sort=request.sort,
                page=request.page,
                page_size=request.pageSize,
                actor=actor,
            )
    except (FileNotFoundError, paper_bank.PaperBankError, paper_occurrences.OccurrenceIdentityInvalid) as exc:
        raise _paper_bank_error(exc) from exc


@router.get(
    "/api/topics/{topic_id}/paper-bank/exports/{export_id}/download",
)
def download_topic_paper_bank_export(
    topic_id: str,
    export_id: str,
    user=Depends(require_product_user),
):
    del user  # dependency enforces the product-user boundary; no user data is returned.
    try:
        path, metadata = paper_bank.resolve_paper_bank_export_download(topic_id, export_id)
    except paper_bank.PaperBankError as exc:
        raise _paper_bank_error(exc) from exc
    return FileResponse(
        path,
        media_type=metadata["mediaType"],
        filename=metadata["fileName"],
        headers={"X-XRML-Artifact-SHA256": metadata["contentSha256"]},
    )


@router.get(
    "/api/topics/{topic_id}/paper-bank/{paper_version_id}",
    response_model=PaperBankDetailResponse,
)
def get_topic_paper_detail(
    topic_id: str,
    paper_version_id: str,
    user=Depends(require_product_user),
):
    actor = resource_actor_for_user(user)
    try:
        with resource_actor_context(actor):
            return paper_bank.get_topic_paper_detail(topic_id, paper_version_id, actor=actor)
    except (
        FileNotFoundError,
        paper_bank.PaperBankError,
        paper_occurrences.OccurrenceIdentityInvalid,
    ) as exc:
        raise _paper_bank_error(exc) from exc


def _review_input_read(user: Any, operation: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    actor = resource_actor_for_user(user)
    try:
        with resource_actor_context(actor):
            return operation(actor)
    except (FileNotFoundError, KeyError, paper_bank.PaperVersionNotFound):
        raise HTTPException(404, detail="review_input_not_found") from None
    except (OSError, sqlite3.Error, SqliteIntegrityError, SharedStateError, ValueError,
            paper_bank.PaperBankError, paper_occurrences.OccurrenceIdentityInvalid):
        raise HTTPException(503, detail="review_input_store_unavailable") from None


@router.get("/api/topics/{topic_id}/paper-bank/{paper_version_id}/review-inputs")
def list_review_inputs(
    topic_id: str, paper_version_id: str, page: int = Query(1, ge=1),
    page_size: int = Query(10, alias="pageSize", ge=1, le=20), user=Depends(require_product_user),
):
    return _review_input_read(user, lambda actor: review_input_history.listing(
        topic_id, paper_version_id, actor, page=page, page_size=page_size))


@router.get("/api/topics/{topic_id}/paper-bank/{paper_version_id}/review-inputs/{receipt_id}")
def get_review_input(topic_id: str, paper_version_id: str, receipt_id: str, user=Depends(require_product_user)):
    return _review_input_read(user, lambda actor: review_input_history.detail(topic_id, paper_version_id, receipt_id, actor))


def _review_preparation_call(user: Any, operation: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    actor = resource_actor_for_user(user)
    try:
        with resource_actor_context(actor):
            # The service applies topic workspace UPDATE and visible occurrence
            # checks for reads, new preparations and request replay alike.
            return operation(actor)
    except (FileNotFoundError, KeyError, paper_bank.PaperVersionNotFound):
        raise HTTPException(404, detail="review_preparation_not_found") from None
    except ValueError as exc:
        code = str(exc)
        if code in {"review_source_coverage_limit_exceeded", "review_pdf_extraction_limit_exceeded", "review_pdf_attachment_limit_exceeded"}:
            raise HTTPException(503, detail=code) from None
        conflicts = {"review_preparation_source_changed", "review_preparation_request_conflict",
                     "review_pdf_source_changed", "review_pdf_extraction_conflict"}
        invalid = {"review_preparation_range_invalid", "review_preparation_request_invalid",
                   "review_preparation_source_too_large", "review_preparation_source_unsupported",
                   "review_preparation_source_invalid", "review_preparation_pagination_invalid",
                   "review_pdf_source_invalid", "review_pdf_source_too_large", "review_pdf_extraction_failed",
                   "review_pdf_engine_unavailable", "review_pdf_encrypted", "review_pdf_page_limit_exceeded",
                   "review_pdf_attachment_request_invalid"}
        raise HTTPException(409 if code in conflicts else 422 if code in invalid else 503,
                            detail=code if code in conflicts | invalid else "review_preparation_integrity_unavailable") from None
    except (OSError, sqlite3.Error, SqliteIntegrityError, SharedStateError, FileLockError,
            paper_bank.PaperBankError, paper_occurrences.OccurrenceIdentityInvalid):
        raise HTTPException(503, detail="review_preparation_store_unavailable") from None


@router.get("/api/topics/{topic_id}/paper-bank/{paper_version_id}/review-preparations/sources")
def get_review_preparation_sources(topic_id: str, paper_version_id: str, user=Depends(require_product_user)):
    return _review_preparation_call(user, lambda actor: review_preparation.sources(topic_id, paper_version_id, actor))


@router.post("/api/topics/{topic_id}/paper-bank/{paper_version_id}/review-preparations/preview")
def preview_review_range(topic_id: str, paper_version_id: str,
                         body: Any = Body(..., json_schema_extra=ReviewPreparationPreview.model_json_schema()),
                         user=Depends(require_product_user)):
    try:
        request = ReviewPreparationPreview.model_validate(body, strict=True)
    except ValidationError:
        # Bound the error response rather than echoing invalid Unicode input.
        raise HTTPException(422, detail="review_preparation_request_invalid") from None
    return _review_preparation_call(user, lambda actor: review_preparation.preview(topic_id, paper_version_id, request, actor))


@router.post("/api/topics/{topic_id}/paper-bank/{paper_version_id}/review-preparations")
def prepare_review_range(topic_id: str, paper_version_id: str,
                         body: Any = Body(..., json_schema_extra=ReviewPreparation.model_json_schema()),
                         user=Depends(require_product_user)):
    try:
        request = ReviewPreparation.model_validate(body, strict=True)
    except ValidationError:
        # Do not echo untrusted strings: escaped lone surrogates cannot be
        # encoded by FastAPI's default validation-error JSON response.
        raise HTTPException(422, detail="review_preparation_request_invalid") from None
    return _review_preparation_call(user, lambda actor: review_preparation.prepare(topic_id, paper_version_id, request, actor))


@router.get("/api/topics/{topic_id}/paper-bank/{paper_version_id}/review-preparations")
def list_review_preparations(topic_id: str, paper_version_id: str, page: int = Query(1, ge=1),
                             page_size: int = Query(10, alias="pageSize", ge=1, le=20), user=Depends(require_product_user)):
    return _review_preparation_call(user, lambda actor: review_preparation.listing(
        topic_id, paper_version_id, actor, page=page, page_size=page_size))


@router.get("/api/topics/{topic_id}/paper-bank/{paper_version_id}/review-preparations/pdf-attachments")
def list_pdf_attachments(topic_id: str, paper_version_id: str, user=Depends(require_product_user)):
    from app.services import review_pdf_attachment
    return _review_preparation_call(user, lambda actor: review_pdf_attachment.listing(topic_id, paper_version_id, actor))


@router.put("/api/topics/{topic_id}/paper-bank/{paper_version_id}/review-preparations/pdf-attachments/{target_id}/{pdf_sha256}")
async def attach_local_pdf(topic_id: str, paper_version_id: str, target_id: str, pdf_sha256: str,
                           request: Request, user=Depends(require_product_user)):
    from app.domain import review_pdf_attachment as domain, review_pdf_extraction as extraction
    from app.services import review_pdf_attachment
    # Authorize before consuming a potentially large body. The service checks
    # again after reception and immediately before linking the published file.
    await run_in_threadpool(_review_preparation_call, user,
        lambda actor: dict(occurrence=review_pdf_attachment.authorize(topic_id, paper_version_id, target_id, actor)))
    try:
        domain.reference(pdf_sha256)
    except ValueError:
        raise HTTPException(422, detail="review_pdf_attachment_request_invalid") from None
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/pdf" or request.query_params:
        raise HTTPException(422, detail="review_pdf_attachment_request_invalid")
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdecimal() or int(declared) > extraction.MAX_PDF_BYTES):
        raise HTTPException(413, detail="review_pdf_source_too_large")
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > extraction.MAX_PDF_BYTES:
            raise HTTPException(413, detail="review_pdf_source_too_large")
        raw.extend(chunk)
    if not raw or (declared is not None and len(raw) != int(declared)):
        raise HTTPException(422, detail="review_pdf_attachment_request_invalid")
    return await run_in_threadpool(_review_preparation_call, user,
        lambda actor: review_pdf_attachment.attach(topic_id, paper_version_id, target_id, pdf_sha256, bytes(raw), actor))


@router.get("/api/topics/{topic_id}/paper-bank/{paper_version_id}/review-preparations/pdf-extractions")
def get_pdf_extractions(topic_id: str, paper_version_id: str, user=Depends(require_product_user)):
    return _review_preparation_call(user, lambda actor: review_pdf_extraction.inventory(topic_id, paper_version_id, actor))


@router.post("/api/topics/{topic_id}/paper-bank/{paper_version_id}/review-preparations/pdf-extractions")
def extract_local_pdf(topic_id: str, paper_version_id: str,
                      body: Any = Body(..., json_schema_extra=PdfExtractionRequest.model_json_schema()),
                      user=Depends(require_product_user)):
    try:
        request = PdfExtractionRequest.model_validate(body, strict=True)
    except ValidationError:
        raise HTTPException(422, detail="review_preparation_request_invalid") from None
    return _review_preparation_call(user, lambda actor: review_pdf_extraction.extract(topic_id, paper_version_id, request, actor))


@router.get("/api/topics/{topic_id}/paper-bank/{paper_version_id}/review-preparations/pdf-extractions/{extraction_id}")
def get_pdf_extraction(topic_id: str, paper_version_id: str, extraction_id: str, user=Depends(require_product_user)):
    return _review_preparation_call(user, lambda actor: review_pdf_extraction.detail(topic_id, paper_version_id, extraction_id, actor))


@router.get("/api/topics/{topic_id}/paper-bank/{paper_version_id}/review-preparations/pdf-extractions/{extraction_id}/coverage")
def get_pdf_extraction_coverage(topic_id: str, paper_version_id: str, extraction_id: str, user=Depends(require_product_user)):
    from app.services import review_document_coverage
    return _review_preparation_call(user, lambda actor: review_document_coverage.overview(topic_id, paper_version_id, extraction_id, actor))


def _document_plan_call(user: Any, operation: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    actor = resource_actor_for_user(user)
    try:
        with resource_actor_context(actor):
            return operation(actor)
    except (KeyError, FileNotFoundError, paper_bank.PaperVersionNotFound):
        raise HTTPException(404, detail="document_plan_not_found") from None
    except ValueError as exc:
        code = str(exc)
        if code in {"document_plan_request_conflict", "review_preparation_source_changed"}:
            raise HTTPException(409, detail=code) from None
        if code in {"document_plan_budget_too_small", "document_plan_budget_invalid", "document_plan_connector_invalid",
                "document_plan_section_limit_exceeded", "document_plan_unit_limit_exceeded", "review_preparation_source_invalid",
                "review_preparation_source_too_large", "review_preparation_source_unsupported"}:
            raise HTTPException(422, detail=code) from None
        raise HTTPException(503, detail="document_plan_integrity_unavailable") from None
    except (OSError, sqlite3.Error, SqliteIntegrityError, SharedStateError, paper_bank.PaperBankError):
        raise HTTPException(503, detail="document_plan_store_unavailable") from None


@router.get("/api/topics/{topic_id}/paper-bank/{paper_version_id}/review-preparations/document-plans/configuration")
def document_plan_configuration(topic_id: str, paper_version_id: str, user=Depends(require_product_user)):
    from app.services import review_document_plan
    return _document_plan_call(user, lambda actor: review_document_plan.configuration(topic_id, paper_version_id, actor))


@router.get("/api/topics/{topic_id}/paper-bank/{paper_version_id}/review-preparations/document-plans")
def list_document_plans(topic_id: str, paper_version_id: str, page: int = Query(1, ge=1, le=1_000_000), user=Depends(require_product_user)):
    from app.services import review_document_plan
    return _document_plan_call(user, lambda actor: review_document_plan.listing(topic_id, paper_version_id, actor, page))


@router.post("/api/topics/{topic_id}/paper-bank/{paper_version_id}/review-preparations/document-plans")
def create_document_plan(topic_id: str, paper_version_id: str, body: Any = Body(...), user=Depends(require_product_user)):
    from app.domain.review_document_plan import Plan
    from app.services import review_document_plan
    try:
        request = Plan.model_validate(body)
    except ValidationError:
        raise HTTPException(422, detail="document_plan_request_invalid") from None
    return _document_plan_call(user, lambda actor: review_document_plan.create(topic_id, paper_version_id, request, actor))


@router.get("/api/topics/{topic_id}/paper-bank/{paper_version_id}/review-preparations/document-plans/{plan_id}")
def get_document_plan(topic_id: str, paper_version_id: str, plan_id: str, user=Depends(require_product_user)):
    from app.services import review_document_plan
    return _document_plan_call(user, lambda actor: review_document_plan.detail(topic_id, paper_version_id, plan_id, actor))


@router.post("/api/topics/{topic_id}/paper-bank/{paper_version_id}/review-preparations/document-plans/{plan_id}/units/{index}/prepare")
def prepare_document_unit(topic_id: str, paper_version_id: str, plan_id: str, index: int, user=Depends(require_product_user)):
    from app.services import review_document_plan
    return _document_plan_call(user, lambda actor: review_document_plan.prepare_unit(topic_id, paper_version_id, plan_id, index, actor))


@router.get("/api/topics/{topic_id}/paper-bank/{paper_version_id}/review-preparations/{preparation_id}")
def get_review_preparation(topic_id: str, paper_version_id: str, preparation_id: str, user=Depends(require_product_user)):
    return _review_preparation_call(user, lambda actor: review_preparation.detail(topic_id, paper_version_id, preparation_id, actor))


@router.get("/api/topics/{topic_id}/paper-bank/{paper_version_id}/review-preparations/{preparation_id}/coverage")
def get_review_source_coverage(topic_id: str, paper_version_id: str, preparation_id: str, user=Depends(require_product_user)):
    return _review_preparation_call(user, lambda actor: review_preparation.coverage(topic_id, paper_version_id, preparation_id, actor))


def _review_offline_call(user: Any, operation: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    actor = resource_actor_for_user(user)
    try:
        with resource_actor_context(actor):
            return operation(actor)
    except (FileNotFoundError, KeyError, paper_bank.PaperVersionNotFound):
        raise HTTPException(404, detail="review_preparation_not_found") from None
    except ValueError as exc:
        conflicts = {"review_offline_disabled", "review_offline_request_conflict", "review_offline_preparation_changed"}
        raise HTTPException(409 if str(exc) in conflicts else 503,
                            detail=str(exc) if str(exc) in conflicts else "review_offline_unavailable") from None
    except (OSError, sqlite3.Error, SqliteIntegrityError, SharedStateError,
            paper_bank.PaperBankError, paper_occurrences.OccurrenceIdentityInvalid):
        raise HTTPException(503, detail="review_offline_unavailable") from None


@router.get("/api/topics/{topic_id}/paper-bank/{paper_version_id}/review-preparations/{preparation_id}/offline-review")
def get_offline_review_status(topic_id: str, paper_version_id: str, preparation_id: str,
                              request: Request, user=Depends(require_product_user)):
    runtime = getattr(request.app.state, "review_offline_runtime", None)
    return _review_offline_call(user, lambda actor: review_offline.status(runtime, topic_id, paper_version_id, preparation_id, actor))


@router.post("/api/topics/{topic_id}/paper-bank/{paper_version_id}/review-preparations/{preparation_id}/offline-reviews")
def execute_offline_review(topic_id: str, paper_version_id: str, preparation_id: str, request: Request,
                           body: Any = Body(..., json_schema_extra=OfflineReviewRequest.model_json_schema()),
                           user=Depends(require_product_user)):
    try:
        validated = OfflineReviewRequest.model_validate(body, strict=True)
    except ValidationError:
        raise HTTPException(422, detail="review_offline_request_invalid") from None
    runtime = getattr(request.app.state, "review_offline_runtime", None)
    return _review_offline_call(user, lambda actor: review_offline.execute(runtime, topic_id, paper_version_id, preparation_id, validated, actor))


def _require_topic(topic_id: str, user) -> None:
    actor = resource_actor_for_user(user)
    try:
        with resource_actor_context(actor):
            paper_bank._load_topic_profile(topic_id, actor=actor)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="topic_not_found") from exc


@router.get(
    "/api/topics/{topic_id}/paper-collections",
    response_model=PaperCollectionListResponse,
)
def get_paper_collections(topic_id: str, user=Depends(require_product_user)):
    _require_topic(topic_id, user)
    try:
        return paper_collections.list_collections(topic_id)
    except (paper_collections.PaperCollectionError, SharedStateError) as exc:
        raise _collection_error(exc) from exc


@router.post(
    "/api/topics/{topic_id}/paper-collections",
    response_model=PaperCollectionResponse,
    status_code=201,
)
def create_paper_collection(
    topic_id: str,
    request: PaperCollectionCreateRequest,
    user=Depends(require_product_user),
):
    _require_topic(topic_id, user)
    try:
        return paper_collections.create_collection(topic_id, request.name)
    except (paper_collections.PaperCollectionError, SharedStateError) as exc:
        raise _collection_error(exc) from exc


@router.patch(
    "/api/topics/{topic_id}/paper-collections/{collection_id}",
    response_model=PaperCollectionResponse,
)
def rename_paper_collection(
    topic_id: str,
    collection_id: str,
    request: PaperCollectionRenameRequest,
    user=Depends(require_product_user),
):
    _require_topic(topic_id, user)
    try:
        return paper_collections.rename_collection(topic_id, collection_id, request.name)
    except (paper_collections.PaperCollectionError, SharedStateError) as exc:
        raise _collection_error(exc) from exc


@router.delete(
    "/api/topics/{topic_id}/paper-collections/{collection_id}",
    response_model=PaperCollectionDeleteResponse,
)
def delete_paper_collection(
    topic_id: str,
    collection_id: str,
    user=Depends(require_product_user),
):
    _require_topic(topic_id, user)
    try:
        return paper_collections.delete_collection(topic_id, collection_id)
    except (paper_collections.PaperCollectionError, SharedStateError) as exc:
        raise _collection_error(exc) from exc


@router.post(
    "/api/topics/{topic_id}/paper-collections/{collection_id}/members:bulk",
    response_model=PaperCollectionBulkSaveResponse,
)
def add_paper_collection_members_bulk(
    topic_id: str,
    collection_id: str,
    request: PaperCollectionBulkSaveRequest,
    user=Depends(require_product_user),
):
    actor = resource_actor_for_user(user)
    try:
        with resource_actor_context(actor):
            members, _unique_count = paper_bank.resolve_paper_versions(
                topic_id,
                request.selectedPaperVersionIds,
                actor=actor,
            )
        return paper_collections.add_members_atomic(
            topic_id,
            collection_id,
            members,
            requested_count=len(request.selectedPaperVersionIds),
        )
    except (FileNotFoundError, paper_bank.PaperBankError, paper_occurrences.OccurrenceIdentityInvalid) as exc:
        raise _paper_bank_error(exc) from exc
    except (paper_collections.PaperCollectionError, SharedStateError) as exc:
        raise _collection_error(exc) from exc


@router.put(
    "/api/topics/{topic_id}/paper-collections/{collection_id}/members/{paper_version_id}",
    response_model=PaperCollectionResponse,
)
def add_paper_collection_member(
    topic_id: str,
    collection_id: str,
    paper_version_id: str,
    user=Depends(require_product_user),
):
    actor = resource_actor_for_user(user)
    try:
        with resource_actor_context(actor):
            detail = paper_bank.get_topic_paper_detail(topic_id, paper_version_id, actor=actor)
        return paper_collections.add_member(
            topic_id,
            collection_id,
            canonical_paper_id=detail["canonicalPaperId"],
            paper_version_id=detail["paperVersionId"],
        )
    except (FileNotFoundError, paper_bank.PaperBankError) as exc:
        raise _paper_bank_error(exc) from exc
    except (paper_collections.PaperCollectionError, SharedStateError) as exc:
        raise _collection_error(exc) from exc


@router.delete(
    "/api/topics/{topic_id}/paper-collections/{collection_id}/members/{paper_version_id}",
    response_model=PaperCollectionResponse,
)
def remove_paper_collection_member(
    topic_id: str,
    collection_id: str,
    paper_version_id: str,
    user=Depends(require_product_user),
):
    _require_topic(topic_id, user)
    try:
        return paper_collections.remove_member(topic_id, collection_id, paper_version_id)
    except (paper_collections.PaperCollectionError, SharedStateError) as exc:
        raise _collection_error(exc) from exc
