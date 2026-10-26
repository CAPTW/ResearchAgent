from __future__ import annotations

import sqlite3
from typing import Callable, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from app.auth.resource_access import resource_actor_context, resource_actor_for_user
from app.auth.security import require_product_user
from app.db.sqlite_integrity import SqliteIntegrityError
from app.repositories.shared_state import SharedStateError
from app.repositories.file_locks import FileLockError
from app.repositories.jsonl import JsonlIntegrityError
from app.services.paper_occurrences import OccurrenceIdentityInvalid
from app.services.paper_bank import PaperBankError
from app.repositories.paper_collections import PaperCollectionError
from app.services import temporal_memory as service

router = APIRouter(tags=["temporal-memory"])


def invoke(user: Any, operation: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    actor = resource_actor_for_user(user)
    try:
        with resource_actor_context(actor):
            return operation(actor)
    except FileNotFoundError:
        raise HTTPException(404, detail="topic_not_found") from None
    except KeyError:
        raise HTTPException(404, detail="snapshot_not_found") from None
    except ValueError as exc:
        code = str(exc)
        known = {"cross_topic_pair", "snapshot_schema_conflict", "snapshot_capture_conflict", "pagination_invalid", "source_capture_limit_exceeded", "source_path_unavailable"}
        code = code if code in known else "snapshot_canonicalization_failed"
        raise HTTPException(409 if code in {"snapshot_schema_conflict", "snapshot_capture_conflict"} else 422, detail=code) from None
    except (SqliteIntegrityError, SharedStateError, FileLockError, JsonlIntegrityError,
            OccurrenceIdentityInvalid, PaperBankError, PaperCollectionError, OSError):
        raise HTTPException(503, detail="temporal_repository_unavailable") from None
    except sqlite3.Error:
        raise HTTPException(503, detail="snapshot_transaction_failed") from None


@router.post("/api/topics/{topic_id}/memory/snapshots")
def capture(topic_id: str, response: Response, user=Depends(require_product_user)):
    result = invoke(user, lambda actor: service.capture(topic_id, actor))
    response.status_code = 201 if result["created"] else 200
    return result


@router.get("/api/topics/{topic_id}/memory/snapshots")
def listing(topic_id: str, page: int = Query(1, ge=1), page_size: int = Query(25, alias="pageSize", ge=1, le=100), user=Depends(require_product_user)):
    return invoke(user, lambda actor: service.listing(topic_id, actor, page, page_size))


@router.get("/api/topics/{topic_id}/memory/snapshots/{snapshot_id}")
def detail(topic_id: str, snapshot_id: str, user=Depends(require_product_user)):
    return invoke(user, lambda actor: service.detail(topic_id, snapshot_id, actor))


@router.get("/api/topics/{topic_id}/memory/diff")
def comparison(topic_id: str, from_id: str = Query(alias="from"), to_id: str = Query(alias="to"), user=Depends(require_product_user)):
    return invoke(user, lambda actor: service.comparison(topic_id, from_id, to_id, actor))
