"""Local admin/same-origin UI for one server-configured excerpt qualification."""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.api.model_connections import _local_credential_request
from app.auth.config import get_auth_settings
from app.auth.security import require_admin_user
from app.services.local_anthropic_trial import LocalAnthropicTrial

router = APIRouter(prefix="/settings/model-connections/anthropic_api/first-trial", tags=["settings"])
HEADERS = {"Cache-Control": "no-store"}


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def _runtime(request: Request, user: Any, *, intent_header: str = 'x-xrml-trial-action') -> LocalAnthropicTrial | None:
    _local_credential_request(request, intent_header=intent_header)
    if (get_auth_settings().auth_mode != "disabled" or user.user_id != "local-internal-user"
            or user.auth_source != 'disabled' or not user.is_admin):
        raise HTTPException(403, detail="anthropic_trial_local_actor_required", headers=HEADERS)
    if not getattr(request.app.state, "runtime_bootstrap_ok", False):
        raise HTTPException(503, detail="anthropic_trial_storage_unavailable", headers=HEADERS)
    runtime = getattr(request.app.state, "local_anthropic_trial", None)
    return runtime if isinstance(runtime, LocalAnthropicTrial) else None


def _call(runtime: LocalAnthropicTrial, operation: str, expected: str | None = None) -> JSONResponse:
    try:
        if operation == "inspect":
            result = runtime.inspect()
        elif operation == "capture":
            result = runtime.read_capture()
        elif operation == "execute" and expected is not None:
            result = runtime.execute(expected)
        elif operation == "cancel" and expected is not None:
            result = runtime.cancel(expected)
        else:
            raise ValueError("anthropic_trial_command_invalid")
        return JSONResponse(result, headers=HEADERS)
    except ValueError as exc:
        code = str(exc)
        allowed = {"anthropic_trial_binding_changed", "anthropic_trial_expired", "anthropic_trial_capture_unavailable",
                   "anthropic_trial_credential_unavailable", "model_credentials_changed", "model_credentials_invalid"}
        if code in allowed:
            raise HTTPException(409, detail=code, headers=HEADERS) from None
        raise HTTPException(503, detail="anthropic_trial_state_unavailable", headers=HEADERS) from None
    except Exception:
        # Transport/storage/validator errors never expose keys, response text or paths.
        raise HTTPException(503, detail="anthropic_trial_state_unavailable", headers=HEADERS) from None


@router.get("")
def inspect(request: Request, user=Depends(require_admin_user)):
    runtime = _runtime(request, user)
    if runtime is None:
        return JSONResponse(dict(enabled=False, reason="anthropic_trial_disabled", trial=None), headers=HEADERS)
    return _call(runtime, "inspect")


@router.get("/capture")
def capture(request: Request, user=Depends(require_admin_user)):
    runtime = _runtime(request, user)
    if runtime is None:
        raise HTTPException(409, detail="anthropic_trial_disabled", headers=HEADERS)
    return _call(runtime, "capture")


@router.post("/{operation}")
async def command(operation: str, request: Request, user=Depends(require_admin_user)):
    runtime = _runtime(request, user)
    if runtime is None:
        raise HTTPException(409, detail="anthropic_trial_disabled", headers=HEADERS)
    if operation not in {"execute", "cancel"}:
        raise HTTPException(404, detail="anthropic_trial_command_invalid", headers=HEADERS)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 1024:
            raise HTTPException(413, detail="anthropic_trial_command_invalid", headers=HEADERS)
    try:
        body = Command.model_validate_json(bytes(raw), strict=True)
    except ValueError:
        raise HTTPException(422, detail="anthropic_trial_command_invalid", headers=HEADERS) from None
    return await run_in_threadpool(_call, runtime, operation, body.expected_binding_sha256)
