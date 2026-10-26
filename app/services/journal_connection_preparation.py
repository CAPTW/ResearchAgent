"""Freeze an observed connection choice; preparation never grants execution.

Connection settings and execution storage are separate stores. The selection
digest is a precondition on one observed settings snapshot, not a transaction
spanning both stores. Later settings changes cannot retarget the frozen request.
"""
from typing import Any

from pydantic import Field

from app.domain.journal_execution import Prepare as ExecutionPrepare
from app.domain.temporal_memory import digest
from app.repositories import journal_executions, local_synthesis
from app.services import model_connections as connections


class Prepare(ExecutionPrepare):
    connector_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_]+$")
    expected_selection_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


def _selection() -> dict[str, Any]:
    state = connections.read()
    identity, override = connections.selection(state)
    connector = connections.BY_ID.get(identity)
    model = state["models"].get(identity, "") if connector else ""
    reason = ("model_connection_unavailable" if connector is None else
              "model_connection_adapter_unavailable" if not connector.adapter_available else
              "model_connection_model_required" if not model else None)
    value = dict(connector_id=connector.connector_id if connector else None,
        provider_name=connector.provider_name if connector else None, model=model,
        display_name=connector.display_name if connector else "사용 가능한 연결 없음",
        auth_mode=connector.auth_mode if connector else None,
        environment_override=override is not None, ready=reason is None,
        reason=reason, execution_allowed=False)
    # Only the effective choice participates: unrelated models and credential
    # state neither leak into this summary nor invalidate a selected request.
    return value | dict(selection_digest=digest(dict(
        schema_version="JOURNAL_CONNECTION_SELECTION_V1", identity=identity, selection=value)))


def summary(topic: str, job_id: str) -> dict[str, Any]:
    local_synthesis.get(topic, job_id)
    return _selection()


def prepare(topic: str, job_id: str, body: Prepare, actor_id: str) -> tuple[dict[str, Any], bool]:
    original = ExecutionPrepare.model_validate(body.model_dump(exclude={"connector_id", "expected_selection_digest"}))
    connector = connections.BY_ID.get(body.connector_id)
    if connector is None or connector.provider_name != body.provider_name:
        raise ValueError("model_connection_selection_changed")
    identity = "jexec_" + digest([topic, job_id, body.request_id])
    try:
        journal_executions.detail(topic, job_id, identity)
    except KeyError as error:
        if error.args != ("journal_execution_not_found",):
            raise
    else:
        # Replay before any settings read, even if preferences have changed or
        # become unreadable. The repository checks every original field/actor
        # and returns current history; a client digest never authorizes work.
        return journal_executions.prepare(topic, job_id, original, actor_id)
    observed = _selection()
    if (observed["selection_digest"] != body.expected_selection_digest
            or (observed["connector_id"], observed["provider_name"], observed["model"])
            != (body.connector_id, body.provider_name, body.model)):
        raise ValueError("model_connection_selection_changed")
    if not observed["ready"]:
        raise ValueError(observed["reason"])
    frozen = ExecutionPrepare.model_validate(original.model_dump() | dict(
        provider_name=observed["provider_name"], model=observed["model"]))
    return journal_executions.prepare(topic, job_id, frozen, actor_id)
