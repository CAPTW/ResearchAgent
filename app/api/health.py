from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.ops.runtime import build_deployment_status
from app.ops.runtime_stores import inspect_runtime_stores
from app.repositories import metadata_index
from app.services import topic_notifications, topic_schedules
from app.services.providers import describe_provider_status

router = APIRouter(tags=["health"])


@router.get("/health")
def get_health() -> dict[str, str]:
    """Liveness: process responsiveness only.

    Opens no store, creates nothing, starts no worker, and makes no
    network/provider/OAuth/MCP call.
    """
    return {"status": "ok"}


def _runtime_bootstrap_ok(request: Request) -> bool | None:
    outcome = getattr(request.app.state, "runtime_bootstrap_ok", None)
    return outcome if isinstance(outcome, bool) else None


@router.get("/health/ready")
def get_readiness(
    bootstrap_ok: Annotated[bool | None, Depends(_runtime_bootstrap_ok)] = None,
) -> dict[str, object]:
    """Readiness: observation only.

    Reports whether the configured local stores are usable without creating a
    directory, DB file, schema, or temporary write-probe file, and without
    starting a worker or claiming/recovering a job.
    """
    provider = describe_provider_status()
    scheduler = topic_schedules.get_scheduler_runtime_status()
    notification_delivery = topic_notifications.notification_delivery_health_summary()
    deployment = build_deployment_status(
        provider=provider,
        scheduler=scheduler,
        notification_delivery=notification_delivery,
        observe_only=True,
    )
    runtime_stores = inspect_runtime_stores()
    status = deployment["readiness"]
    if bootstrap_ok is False or runtime_stores["ready"] is not True:
        status = "not_ready"
    return {
        "status": status,
        "deployment": deployment,
        "provider": provider,
        "scheduler": scheduler,
        "notification_delivery": notification_delivery,
        "metadata_index_state": metadata_index.get_index_state(observe_only=True),
        "runtime_stores": runtime_stores,
        "runtime_bootstrap": {
            "status": "completed" if bootstrap_ok is True else "failed" if bootstrap_ok is False else "not_observed",
        },
    }
