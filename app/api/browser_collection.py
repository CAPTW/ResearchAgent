"""Authenticated app control and a capability-scoped local native bridge."""
import sqlite3
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.auth.config import get_auth_settings
from app.auth.resource_access import resource_actor_for_user, resource_actor_context
from app.auth.security import require_product_user
from app.domain import browser_collection as domain
from app.repositories.shared_state import SharedStateError
from app.services import browser_collection as service

def _local_host(request: Request) -> None:
    if not get_auth_settings().auth_enabled and request.url.hostname not in {"127.0.0.1", "localhost"}:
        raise HTTPException(403, "browser_collection_local_host_required")


router = APIRouter(tags=["browser-collection"], dependencies=[Depends(_local_host)])
ROOT = "/api/topics/{topic_id}/browser-collections"
BRIDGE = "/api/browser-collection-bridge/{job_id}"


def _call(operation: Callable[[], Any]):
    try:
        return operation()
    except (KeyError, FileNotFoundError):
        raise HTTPException(404, "browser_collection_not_found") from None
    except PermissionError:
        raise HTTPException(403, "browser_collection_connection_unavailable_or_expired") from None
    except ValueError as exc:
        code = str(exc)
        if not code.startswith("browser_collection_") or len(code) > 100:
            code = "browser_collection_operation_rejected"
        status = (429 if "rate_limited" in code else 409 if "conflict" in code or "busy" in code
                  else 503 if "unavailable" in code or "store_invalid" in code else 422)
        raise HTTPException(status, code) from None
    except (OSError, sqlite3.Error, SharedStateError):
        raise HTTPException(503, "browser_collection_store_unavailable") from None


def _body(model, value):
    if model in (domain.Search, domain.Create) and isinstance(value, dict):
        if domain.query_contains_credentials(value.get("query")):
            raise HTTPException(422, domain.QUERY_CREDENTIAL_ERROR)
    try:
        return model.model_validate(value, strict=True)
    except ValidationError:
        # Never echo a supplied URL, user metadata or capability in validation errors.
        raise HTTPException(422, "browser_collection_request_invalid") from None


def _app(user, operation):
    actor = resource_actor_for_user(user)
    with resource_actor_context(actor):
        return _call(lambda: operation(actor))


def _token(request: Request) -> str:
    # No website can access this route by permissive CORS. A native host sends
    # no Origin, and must reach the IPv4 loopback listener directly.
    if (request.headers.get("origin") is not None
            or request.url.hostname not in {"127.0.0.1", "localhost"}
            or request.client is None or request.client.host not in {"127.0.0.1", "::1"}):
        raise HTTPException(403, "browser_collection_bridge_local_only")
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer ") or len(header) != 50:
        raise HTTPException(403, "browser_collection_connection_unavailable_or_expired")
    return header[7:]


@router.get(ROOT)
def listing(topic_id: str, user=Depends(require_product_user)):
    return _app(user, lambda actor: service.listing(topic_id, actor))


@router.post(ROOT)
def create(topic_id: str, request: Request, body: Any = Body(...), user=Depends(require_product_user)):
    _origin(request)
    value = _body(domain.Create, body)
    return _app(user, lambda actor: service.create(topic_id, value, actor))


@router.post(ROOT + "/search")
def search(topic_id: str, request: Request, body: Any = Body(...), user=Depends(require_product_user)):
    _origin(request)
    value = _body(domain.Search, body)
    return _app(user, lambda actor: service.search(topic_id, value, actor))


@router.get(ROOT + "/{job_id}")
def detail(topic_id: str, job_id: str, user=Depends(require_product_user)):
    return _app(user, lambda actor: service.detail(job_id, actor, topic=topic_id))


def _origin(request: Request) -> None:
    # In particular, block cross-site simple POSTs to token issuance/revocation.
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise HTTPException(415, "browser_collection_json_required")
    origin = request.headers.get("origin")
    if origin:
        from app.main import _resolve_allowed_origins
        server = urlsplit(str(request.base_url))
        local_origin = f"{server.scheme}://{server.netloc}"
        if origin not in {local_origin, *_resolve_allowed_origins(get_auth_settings())}:
            raise HTTPException(403, "browser_collection_origin_rejected")


@router.post(ROOT + "/{job_id}/connection")
def connection(topic_id: str, job_id: str, request: Request, body: Any = Body(...), user=Depends(require_product_user)):
    _origin(request)
    _body(domain.Empty, body)
    return _app(user, lambda actor: service.connection(topic_id, job_id, actor))


@router.post(ROOT + "/{job_id}/revoke")
def revoke(topic_id: str, job_id: str, request: Request, body: Any = Body(...), user=Depends(require_product_user)):
    _origin(request)
    _body(domain.Empty, body)
    return _app(user, lambda actor: service.revoke(topic_id, job_id, actor))


@router.post(ROOT + "/{job_id}/items/{item_id}/control")
def control(topic_id: str, job_id: str, item_id: str, request: Request,
            body: Any = Body(...), user=Depends(require_product_user)):
    _origin(request)
    value = _body(domain.Control, body)
    return _app(user, lambda actor: service.control(topic_id, job_id, item_id, value, actor))


@router.post(ROOT + "/{job_id}/items/{item_id}/confirm")
def confirm(topic_id: str, job_id: str, item_id: str, request: Request,
            body: Any = Body(...), user=Depends(require_product_user)):
    _origin(request)
    value = _body(domain.Confirm, body)
    return _app(user, lambda actor: service.confirm(topic_id, job_id, item_id, value, actor))


@router.get(ROOT + "/{job_id}/items/{item_id}/pdf")
def preview(topic_id: str, job_id: str, item_id: str, user=Depends(require_product_user)):
    def operation(actor):
        import hashlib
        from app.repositories import browser_collections
        job = service.detail(job_id, actor, topic=topic_id)
        item = service._item(job, item_id)
        if not item["pdf"]:
            raise KeyError("browser_collection_pdf_unavailable")
        path = browser_collections.checked_path(job_id, item["pdf"]["sha256"] + ".pdf")
        if (path.stat().st_size != item["pdf"]["bytes"]
                or hashlib.sha256(path.read_bytes()).hexdigest() != item["pdf"]["sha256"]):
            raise ValueError("browser_collection_pdf_changed")
        return FileResponse(path, media_type="application/pdf", filename=item["candidate"]["arxiv_id"] + ".pdf",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
    return _app(user, operation)


@router.get(BRIDGE)
def bridge_detail(job_id: str, request: Request):
    token = _token(request)
    return _call(lambda: service.detail(job_id, token=token))


@router.post(BRIDGE + "/items/{item_id}/transition")
def bridge_transition(job_id: str, item_id: str, request: Request, body: Any = Body(...)):
    token = _token(request)
    value = _body(domain.Transition, body)
    return _call(lambda: service.transition(job_id, item_id, value, token=token))


@router.post(BRIDGE + "/items/{item_id}/pdf")
async def bridge_pdf(job_id: str, item_id: str, request: Request):
    token = _token(request)
    await run_in_threadpool(_call, lambda: service.detail(job_id, token=token))
    headers = request.headers
    try:
        download_id = int(headers["x-xrml-download-id"])
        download_bytes = int(headers["x-xrml-download-bytes"])
        if download_id < 0 or not 0 < download_bytes <= domain.MAX_PDF_BYTES:
            raise ValueError()
        arguments: dict[str, Any] = dict(token=token, attempt_id=headers["x-xrml-attempt-id"], download_id=download_id,
            source_url=headers["x-xrml-source-url"], pdf_sha256=headers["x-xrml-pdf-sha256"],
            download_state=headers["x-xrml-download-state"], download_bytes=download_bytes)
    except (KeyError, ValueError):
        raise HTTPException(422, "browser_collection_download_receipt_invalid") from None
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > domain.MAX_PDF_BYTES:
            raise HTTPException(413, "browser_collection_pdf_size_invalid")
        body.extend(chunk)
    return await run_in_threadpool(_call, lambda: service.upload(job_id, item_id, bytes(body), **arguments))
