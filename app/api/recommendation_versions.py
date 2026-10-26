"""Explicit local saves of server-generated recommendation versions."""

import sqlite3
from typing import Any, Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.api.temporal_analysis import DecisionRequest
from app.auth.resource_access import ResourceAction, resource_actor_context, resource_actor_for_user
from app.auth.security import require_product_user
from app.db.sqlite_integrity import SqliteIntegrityError
from app.domain import recommendation_versions as domain
from app.repositories import recommendation_versions as repository
from app.repositories.shared_state import SharedStateError
from app.services import topic_dashboard, topic_profiles
from app.services.recommendations import RecommendationReadOnlySafetyError

router = APIRouter(tags=["recommendation-versions"])
ROOT = "/api/topics/{topic_id}/recommendation-versions"


class CaptureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    candidate_type: Literal["gap", "new_topic"]
    candidate_id: str = Field(min_length=1, max_length=200)
    expected_version_id: str = Field(pattern=r"^rver_[a-f0-9]{64}$")


def invoke(
    topic: str, user: Any, operation: Callable[[], dict[str, Any]], *, action: ResourceAction
) -> dict[str, Any]:
    try:
        actor = resource_actor_for_user(user)
        with resource_actor_context(actor):
            try:
                topic_profiles.get_topic_profile(topic, actor=actor, action=action)
            except FileNotFoundError:
                raise HTTPException(404, detail="topic_not_found") from None
            return operation()
    except KeyError:
        raise HTTPException(404, detail="recommendation_version_not_found") from None
    except ValidationError:
        raise HTTPException(503, detail="recommendation_output_invalid") from None
    except ValueError as exc:
        code = str(exc)
        conflicts = {"recommendation_changed", "decision_conflict", "decision_request_conflict"}
        raise HTTPException(409 if code in conflicts else 422,
                            detail=code if code in conflicts | {"invalid_decision", "pagination_invalid"} else "invalid_recommendation_version") from None
    except (SqliteIntegrityError, SharedStateError, OSError, sqlite3.Error, RecommendationReadOnlySafetyError):
        raise HTTPException(503, detail="recommendation_repository_unavailable") from None


@router.get(ROOT)
def versions(topic_id: str, limit: int = Query(10, ge=1, le=100), page: int = Query(1, ge=1), user=Depends(require_product_user)):
    return invoke(topic_id, user, lambda: repository.versions(topic_id, limit, page), action=ResourceAction.READ)


@router.post(ROOT)
def capture(topic_id: str, body: CaptureRequest, response: Response, user=Depends(require_product_user)):
    def operation():
        # A retry after a lost response must still return the saved version, even
        # when the current candidate has changed or is no longer generated.
        try:
            version = repository.get(topic_id, body.expected_version_id)
        except KeyError:
            version = None
        if version is not None:
            if (version["candidate_type"], version["candidate_id"]) != (body.candidate_type, body.candidate_id):
                raise ValueError("recommendation_changed")
            return dict(version=version, created=False)
        # Generate outside the write transaction: the legacy read-only guard
        # refuses active sidecars. This saves exact output, not an atomic corpus snapshot.
        _, candidates = topic_dashboard.build_topic_recommendation_views(topic_id)
        value = next((value for value in candidates if domain.identity(value) == body.expected_version_id
                      and value["topic_id"] == topic_id and value["candidate_type"] == body.candidate_type
                      and value["candidate_id"] == body.candidate_id), None)
        if value is None:
            raise ValueError("recommendation_changed")
        actor = resource_actor_for_user(user)
        saved, created = repository.save(value, actor.subject or actor.actor_type)
        response.status_code = 201 if created else 200
        return dict(version=saved, created=created)
    return invoke(topic_id, user, operation, action=ResourceAction.UPDATE)


@router.get(ROOT + "/{version_id}")
def detail(topic_id: str, version_id: str, user=Depends(require_product_user)):
    return invoke(topic_id, user, lambda: dict(version=repository.get(topic_id, version_id)), action=ResourceAction.READ)


@router.get(ROOT + "/{version_id}/decisions")
def decisions(topic_id: str, version_id: str, limit: int = Query(10, ge=1, le=100), page: int = Query(1, ge=1), user=Depends(require_product_user)):
    return invoke(topic_id, user, lambda: repository.decisions(topic_id, version_id, limit, page), action=ResourceAction.READ)


@router.post(ROOT + "/{version_id}/decisions")
def decide(topic_id: str, version_id: str, body: DecisionRequest, response: Response, user=Depends(require_product_user)):
    def operation():
        actor = resource_actor_for_user(user)
        decision, created = repository.decide(topic_id, version_id, body.request_id, body.state, body.note,
                                             body.expected_previous_id, actor.subject or actor.actor_type)
        response.status_code = 201 if created else 200
        return dict(decision=decision, created=created)
    return invoke(topic_id, user, operation, action=ResourceAction.UPDATE)
