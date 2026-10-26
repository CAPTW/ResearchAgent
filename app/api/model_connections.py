"""Connection preferences and write-only local encrypted credentials."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.concurrency import run_in_threadpool

from app.auth.config import get_auth_settings
from app.auth.security import require_admin_user
from app.services import model_connections as service
from app.services import model_credentials as credentials

router = APIRouter(prefix="/settings/model-connections", tags=["settings"])


@router.get("")
def get_connections(_user=Depends(require_admin_user)):
    return service.status()


@router.put("")
async def put_connections(request: Request, _user=Depends(require_admin_user)):
    # Do not return Pydantic input echoes if somebody pastes a secret into a
    # model field or an unknown field. Keys use a separate write-only boundary.
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 16_384:
            raise HTTPException(413, detail="model_connections_too_large")
    try:
        body = service.Update.model_validate_json(bytes(data), strict=True)
    except ValueError:
        raise HTTPException(422, detail="model_connections_invalid") from None
    try:
        return await run_in_threadpool(service.save, body)
    except ValueError:
        raise HTTPException(409, detail="model_connections_changed") from None


class CredentialDelete(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=0)


class CredentialWrite(CredentialDelete):
    api_key: SecretStr = Field(min_length=1, max_length=4096)


def _local_credential_request(request: Request, *, intent_header: str = "x-xrml-credential-action") -> None:
    # No remotely hosted key-upload surface, forwarded-peer trust or DNS rebinding.
    # The packaged desktop loads the same loopback /app origin as this API.
    allowed = {"127.0.0.1", "::1", "localhost"}
    if (get_auth_settings().trust_proxy_headers or not request.client
            or request.client.host not in {"127.0.0.1", "::1"}
            or request.url.hostname not in allowed
            or any(name in request.headers for name in ("forwarded", "x-forwarded-for", "x-forwarded-host"))):
        raise HTTPException(403, detail="model_credentials_local_only")
    origin = request.headers.get("origin")
    expected_origin = f"{request.url.scheme}://{request.url.netloc}"
    if origin is not None and origin != expected_origin:
        raise HTTPException(403, detail="model_credentials_same_origin_required")
    if request.method != "GET" and (origin != expected_origin
            or request.headers.get(intent_header) != "1"):
        raise HTTPException(403, detail="model_credentials_intent_required")


def _credential_result(operation, *args):
    try:
        return JSONResponse(operation(*args), headers={"Cache-Control": "no-store"})
    except credentials.CredentialUnavailable:
        raise HTTPException(503, detail="model_credentials_unavailable", headers={"Cache-Control": "no-store"}) from None
    except ValueError as error:
        code = str(error)
        if code not in {"model_credentials_changed", "model_credentials_connector_invalid", "model_credentials_invalid"}:
            code = "model_credentials_invalid"
        raise HTTPException(409 if code == "model_credentials_changed" else 422, detail=code,
                            headers={"Cache-Control": "no-store"}) from None


@router.get("/{connector_id}/credential")
def get_credential(connector_id: str, request: Request, _user=Depends(require_admin_user)):
    _local_credential_request(request)
    return _credential_result(credentials.status, connector_id)


@router.api_route("/{connector_id}/credential", methods=["PUT", "DELETE"])
async def change_credential(connector_id: str, request: Request, _user=Depends(require_admin_user)):
    _local_credential_request(request)
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 8192:
            raise HTTPException(413, detail="model_credentials_too_large", headers={"Cache-Control": "no-store"})
    try:
        if request.method == "PUT":
            body = CredentialWrite.model_validate_json(bytes(data), strict=True)
            return await run_in_threadpool(_credential_result, credentials.save, connector_id,
                                           body.expected_revision, body.api_key.get_secret_value())
        removal = CredentialDelete.model_validate_json(bytes(data), strict=True)
    except ValueError:
        raise HTTPException(422, detail="model_credentials_invalid", headers={"Cache-Control": "no-store"}) from None
    return await run_in_threadpool(_credential_result, credentials.delete, connector_id, removal.expected_revision)
