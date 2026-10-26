from __future__ import annotations

from dataclasses import asdict
import sqlite3
from typing import Any, Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from app.auth.resource_access import ResourceAction, resource_actor_context, resource_actor_for_user
from app.auth.security import require_product_user
from app.db.sqlite_integrity import SqliteIntegrityError
from app.domain import temporal_analysis as domain
from app.repositories import temporal_analysis as repository
from app.repositories.shared_state import SharedStateError
from app.services import temporal_analysis as service

router = APIRouter(tags=["temporal-analysis"])


class PolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: str = domain.SCHEMA
    formula_registry_version: str = domain.REGISTRY
    baseline_mode: str = "PREVIOUS_SNAPSHOT"
    enabled_metric_ids: list[str] = list(domain.METRIC_IDS)
    enabled_insight_kinds: list[str] = list(domain.INSIGHT_KINDS)
    minimum_paper_count: StrictInt = 1
    minimum_denominator: StrictInt = 1
    minimum_absolute_count_delta: StrictInt = 1
    minimum_percentage_point_delta_basis_points: StrictInt = 100
    factual_template_version: str = domain.TEMPLATE


class ActivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    policy_id: str


class EvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    snapshot_id: str
    policy_id: str | None = None


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=100)
    state: Literal["accepted", "held", "rejected"]
    note: str = Field(default="", max_length=2000)
    expected_previous_id: str | None = Field(default=None, max_length=100)


def invoke(
    topic: str, user: Any, operation: Callable[[], dict[str, Any]], *, action: ResourceAction
) -> dict[str, Any]:
    try:
        actor = resource_actor_for_user(user)
        with resource_actor_context(actor):
            try:
                service.authorize(topic, actor, action=action)
            except FileNotFoundError:
                raise HTTPException(404, detail="topic_not_found") from None
            return operation()
    except KeyError as exc:
        code = exc.args[0]
        raise HTTPException(
            404, detail=code if code in {"policy_not_found", "snapshot_not_found", "insight_not_found"} else "analysis_not_found"
        ) from None
    except ValueError as exc:
        code = str(exc)
        known = {
            "cross_topic_policy",
            "cross_topic_snapshot",
            "no_active_policy",
            "invalid_metric_id",
            "invalid_insight_kind",
            "invalid_threshold",
            "unsupported_policy_schema",
            "unsupported_snapshot_schema",
            "snapshot_baseline_conflict",
            "pagination_invalid",
            "policy_unknown_field",
            "policy_binding_conflict",
            "decision_conflict", "decision_request_conflict", "insight_not_eligible", "invalid_decision",
        }
        raise HTTPException(
            409 if code in {"no_active_policy", "policy_binding_conflict", "snapshot_baseline_conflict",
                           "decision_conflict", "decision_request_conflict", "insight_not_eligible"} else 422,
            detail=code if code in known else "analysis_canonicalization_failed",
        ) from None
    except (SqliteIntegrityError, SharedStateError, OSError):
        raise HTTPException(503, detail="analysis_repository_unavailable") from None
    except sqlite3.IntegrityError as exc:
        if str(exc) == "policy_binding_conflict":
            raise HTTPException(409, detail="policy_binding_conflict") from None
        raise HTTPException(503, detail="analysis_transaction_failed") from None
    except sqlite3.Error:
        raise HTTPException(503, detail="analysis_transaction_failed") from None


@router.get("/api/topics/{topic_id}/analysis/policies")
def policies(
    topic_id: str, limit: int = Query(25, ge=1, le=100), page: int = Query(1, ge=1), user=Depends(require_product_user)
):
    return invoke(topic_id, user, lambda: repository.policies(topic_id, limit, page), action=ResourceAction.READ)


@router.post("/api/topics/{topic_id}/analysis/policies")
def create_policy(topic_id: str, body: PolicyRequest, response: Response, user=Depends(require_product_user)):
    def operation():
        policy, created = repository.create_policy(topic_id, body.model_dump())
        response.status_code = 201 if created else 200
        return dict(policy=asdict(policy), created=created)

    return invoke(topic_id, user, operation, action=ResourceAction.UPDATE)


@router.put("/api/topics/{topic_id}/analysis/policies/active")
def activate(topic_id: str, body: ActivationRequest, user=Depends(require_product_user)):
    return invoke(topic_id, user, lambda: dict(policy=asdict(repository.activate(topic_id, body.policy_id))),
                  action=ResourceAction.UPDATE)


@router.post("/api/topics/{topic_id}/analysis/evaluations")
def evaluate(topic_id: str, body: EvaluationRequest, response: Response, user=Depends(require_product_user)):
    def operation():
        evaluation, created = repository.evaluate(topic_id, body.snapshot_id, body.policy_id)
        response.status_code = 201 if created else 200
        return dict(evaluation=asdict(evaluation), created=created)

    return invoke(topic_id, user, operation, action=ResourceAction.UPDATE)


@router.get("/api/topics/{topic_id}/analysis/series")
def series(
    topic_id: str,
    metric_id: str,
    limit: int = Query(25, ge=1, le=100),
    page: int = Query(1, ge=1),
    snapshot_id: str | None = None,
    user=Depends(require_product_user),
):
    return invoke(topic_id, user, lambda: repository.series(topic_id, metric_id, limit, page, snapshot_id),
                  action=ResourceAction.READ)


@router.get("/api/topics/{topic_id}/analysis/insights")
def insights(
    topic_id: str, limit: int = Query(25, ge=1, le=100), page: int = Query(1, ge=1),
    snapshot_id: str | None = None, user=Depends(require_product_user)
):
    return invoke(topic_id, user, lambda: service.insights(topic_id, limit, page, resource_actor_for_user(user), snapshot_id),
                  action=ResourceAction.READ)


@router.get("/api/topics/{topic_id}/analysis/insights/{insight_id}/decisions")
def decisions(topic_id: str, insight_id: str, limit: int = Query(25, ge=1, le=100),
              page: int = Query(1, ge=1), user=Depends(require_product_user)):
    return invoke(topic_id, user, lambda: repository.decisions(topic_id, insight_id, limit, page), action=ResourceAction.READ)


@router.post("/api/topics/{topic_id}/analysis/insights/{insight_id}/decisions")
def decide(topic_id: str, insight_id: str, body: DecisionRequest, response: Response, user=Depends(require_product_user)):
    def operation():
        actor = resource_actor_for_user(user)
        decision, created = repository.decide(topic_id, insight_id, body.request_id, body.state,
                                             body.note, body.expected_previous_id, actor.subject or actor.actor_type)
        response.status_code = 201 if created else 200
        return dict(decision=decision, created=created)

    return invoke(topic_id, user, operation, action=ResourceAction.UPDATE)
