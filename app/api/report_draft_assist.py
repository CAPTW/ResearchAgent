from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.auth.security import require_product_user
from app.schemas.dashboard import ReportDraftAssistSelectRequest
from app.services.report_draft_assist_registry import (
    ReportDraftAssistError,
    describe_report_draft_assist,
    refresh_report_draft_assist,
    select_report_draft_provider,
)

router = APIRouter(tags=["dashboard"])


def _raise_report_draft_assist_error(exc: ReportDraftAssistError) -> None:
    raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc


@router.get("/settings/report-draft-assist")
def get_report_draft_assist_status(_user=Depends(require_product_user)):
    return describe_report_draft_assist(refresh=False)


@router.post("/settings/report-draft-assist/refresh")
def refresh_report_draft_assist_status(_user=Depends(require_product_user)):
    return refresh_report_draft_assist()


@router.post("/settings/report-draft-assist/select")
def select_report_draft_assist_provider(
    payload: ReportDraftAssistSelectRequest,
    _user=Depends(require_product_user),
):
    try:
        return select_report_draft_provider(payload.provider_id)
    except ReportDraftAssistError as exc:
        _raise_report_draft_assist_error(exc)
