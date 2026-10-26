from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query

from app.auth.security import ProductUser, require_admin_user
from app.schemas.admin import (
    LegacyRunsListResponse,
    LegacyRunsMigrationRequest,
    LegacyRunsMigrationResponse,
    OwnershipAuditResponse,
)
from app.services.ownership_migration import (
    apply_legacy_run_migration,
    build_ownership_audit,
    list_legacy_runs,
    preview_legacy_run_migration,
)

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/legacy-runs", response_model=LegacyRunsListResponse)
def get_legacy_runs(
    status: Literal["unowned", "all"] = Query(default="unowned"),
    q: str | None = Query(default=None),
    include_lineage_summary: bool = Query(default=True),
    _user: ProductUser = Depends(require_admin_user),
):
    del include_lineage_summary
    return list_legacy_runs(status=status, q=q)


@router.post("/legacy-runs/preview-migration", response_model=LegacyRunsMigrationResponse)
def preview_legacy_runs_migration(
    payload: LegacyRunsMigrationRequest,
    user: ProductUser = Depends(require_admin_user),
):
    return preview_legacy_run_migration(payload, acting_user=user)


@router.post("/legacy-runs/apply-migration", response_model=LegacyRunsMigrationResponse)
def apply_legacy_runs_migration(
    payload: LegacyRunsMigrationRequest,
    user: ProductUser = Depends(require_admin_user),
):
    return apply_legacy_run_migration(payload, acting_user=user)


@router.get("/ownership-audit", response_model=OwnershipAuditResponse)
def get_ownership_audit(
    sample_limit: int = Query(default=5, ge=1, le=50),
    _user: ProductUser = Depends(require_admin_user),
):
    return build_ownership_audit(sample_limit=sample_limit)
