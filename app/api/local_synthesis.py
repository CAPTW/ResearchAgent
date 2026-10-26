from typing import Any, Callable
from typing import Literal
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from app.auth.resource_access import ResourceAction, resource_actor_context, resource_actor_for_user
from app.auth.security import require_product_user
from app.db.sqlite_integrity import SqliteIntegrityError
from app.repositories import local_synthesis as repository
from app.repositories import synthesis_reports
from app.repositories import local_journal
from app.repositories import journal_working_copy
from app.repositories import journal_candidates
from app.repositories import journal_reviews
from app.repositories import journal_executions
from app.domain.local_journal import Save as JournalSave
from app.domain.journal_working_copy import Save as WorkingCopySave, encoded as working_copy_json
from app.domain.journal_exchange import Import as JournalImport
from app.domain.journal_review import Save as JournalReviewSave
from app.domain.review_input_journal import Create as ReviewInputJournalCreate
from app.domain.journal_execution import Control as ExecutionControl, Prepare as ExecutionPrepare
from app.repositories.paper_collections import PaperCollectionError
from app.repositories.file_locks import FileLockError
from app.repositories.jsonl import JsonlIntegrityError
from app.repositories.shared_state import SharedStateError
from app.services import topic_profiles
from app.services import journal_connection_preparation
from app.services.paper_bank import PaperBankError
from app.services.paper_occurrences import OccurrenceIdentityInvalid

router = APIRouter(tags=["local-synthesis"])
ROOT = "/api/topics/{topic_id}/local-synthesis"


class ReviewInputSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    receipt_id: str = Field(pattern=r"^aireceipt_[a-f0-9]{64}$")
    component_ids: list[str] = Field(min_length=1, max_length=100)


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    paper_version_id: str = Field(min_length=1, max_length=160)
    metadata_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    review_input: ReviewInputSelection | None = None


class Capture(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=100)
    selection: list[Selection] = Field(min_length=1, max_length=100)


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_step: StrictInt = Field(ge=0, le=21)


class Pause(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    paused: StrictBool


class PreserveExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_result_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=100)
    state: Literal["accepted", "held", "rejected"]
    note: str = Field(max_length=2000)
    expected_previous_id: str | None = Field(default=None, max_length=100)
    expected_binding: str = Field(pattern=r"^[a-f0-9]{64}$")


def invoke(topic: str, user: Any, operation: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    actor = resource_actor_for_user(user)
    try:
        with resource_actor_context(actor):
            profile = topic_profiles.get_topic_profile(topic, actor=actor)
            # Frozen source bodies are a topic workspace resource. Public read
            # access to a system template must not expose another user's draft.
            topic_profiles.authorize_topic_profile(profile, actor=actor, action=ResourceAction.UPDATE)
            return operation(actor)
    except FileNotFoundError:
        raise HTTPException(404, detail="topic_not_found") from None
    except KeyError as exc:
        if exc.args == ("synthesis_review_input_not_found",):
            raise HTTPException(409, detail="synthesis_review_input_not_found") from None
        raise HTTPException(404, detail="synthesis_not_found") from None
    except ValueError as exc:
        code = str(exc)
        conflicts = {"synthesis_selection_changed", "synthesis_request_conflict", "synthesis_checkpoint_conflict",
                     "synthesis_paused", "synthesis_already_complete", "snapshot_capture_conflict", "synthesis_not_ready",
                     "decision_conflict", "decision_request_conflict", "synthesis_binding_changed", "synthesis_export_not_accepted",
                     "journal_version_conflict", "journal_request_conflict", "journal_packet_mismatch", "journal_sources_missing",
                     "journal_working_copy_conflict", "journal_working_copy_request_conflict",
                     "journal_review_conflict", "journal_review_request_conflict", "journal_review_binding_changed", "journal_review_export_not_accepted",
                     "journal_execution_not_succeeded", "journal_execution_result_changed", "journal_execution_conflict",
                     "journal_execution_request_conflict", "journal_execution_not_expired", "journal_execution_input_changed",
                     "model_connection_selection_changed", "model_connection_unavailable",
                     "model_connection_adapter_unavailable", "model_connection_model_required"}
        invalid = {"synthesis_selection_invalid", "synthesis_review_input_invalid", "synthesis_input_too_large", "synthesis_request_invalid", "pagination_invalid",
                   "source_capture_limit_exceeded", "invalid_decision", "synthesis_export_format_invalid", "journal_citation_invalid", "journal_input_invalid", "journal_exchange_invalid",
                   "journal_review_invalid", "journal_review_entries_invalid", "journal_review_incomplete", "journal_review_comparison_same",
                   "journal_working_copy_invalid", "journal_working_copy_base_invalid", "review_input_journal_selection_invalid"}
        raise HTTPException(409 if code in conflicts else 422 if code in invalid else 503,
                            detail=code if code in conflicts | invalid else "synthesis_integrity_unavailable") from None
    except (SqliteIntegrityError, SharedStateError, FileLockError, JsonlIntegrityError, OccurrenceIdentityInvalid,
            PaperBankError, PaperCollectionError, OSError, sqlite3.Error):
        raise HTTPException(503, detail="synthesis_store_unavailable") from None


@router.get(ROOT)
def listing(topic_id: str, page: int = Query(1, ge=1), limit: int = Query(10, ge=1, le=100), user=Depends(require_product_user)):
    return invoke(topic_id, user, lambda _: repository.listing(topic_id, page, limit))


@router.post(ROOT)
def capture(topic_id: str, body: Capture, response: Response, user=Depends(require_product_user)):
    def operation(actor):
        job, created = repository.create(topic_id, [item.model_dump(exclude_none=True) for item in body.selection], body.request_id, actor)
        response.status_code = 201 if created else 200
        return dict(job=job, created=created)
    return invoke(topic_id, user, operation)


@router.get(ROOT + "/reports")
def reports(topic_id: str, page: int = Query(1, ge=1), limit: int = Query(10, ge=1, le=100), user=Depends(require_product_user)):
    return invoke(topic_id, user, lambda _: synthesis_reports.listing(topic_id, page, limit))


@router.get(ROOT + "/journals")
def journals(topic_id: str, page: int = Query(1, ge=1), limit: int = Query(10, ge=1, le=100), user=Depends(require_product_user)):
    return invoke(topic_id, user, lambda _: journal_reviews.overview(topic_id, page, limit))


@router.get(ROOT + "/{job_id}")
def detail(topic_id: str, job_id: str, user=Depends(require_product_user)):
    return invoke(topic_id, user, lambda _: repository.get(topic_id, job_id))


@router.post(ROOT + "/{job_id}/step")
def advance(topic_id: str, job_id: str, body: Step, user=Depends(require_product_user)):
    def operation(_):
        job, advanced = repository.advance(topic_id, job_id, body.expected_step)
        return dict(job=job, advanced=advanced)
    return invoke(topic_id, user, operation)


@router.post(ROOT + "/{job_id}/pause")
def pause(topic_id: str, job_id: str, body: Pause, user=Depends(require_product_user)):
    return invoke(topic_id, user, lambda _: repository.set_paused(topic_id, job_id, body.paused))


@router.get(ROOT + "/{job_id}/decisions")
def decisions(topic_id: str, job_id: str, page: int = Query(1, ge=1), limit: int = Query(10, ge=1, le=100), user=Depends(require_product_user)):
    return invoke(topic_id, user, lambda _: synthesis_reports.history(topic_id, job_id, limit, page))


@router.post(ROOT + "/{job_id}/decisions")
def decide(topic_id: str, job_id: str, body: Decision, response: Response, user=Depends(require_product_user)):
    def operation(actor):
        decision, created = synthesis_reports.decide(topic_id, job_id, **body.model_dump(), actor_id=actor.subject or actor.actor_type)
        response.status_code = 201 if created else 200
        return dict(decision=decision, created=created)
    return invoke(topic_id, user, operation)


@router.get(ROOT + "/{job_id}/report")
def report(topic_id: str, job_id: str, user=Depends(require_product_user)):
    return invoke(topic_id, user, lambda _: synthesis_reports.detail(topic_id, job_id))


@router.get(ROOT + "/{job_id}/report/export")
def export_report(topic_id: str, job_id: str, version_id: str, decision_id: str,
                  format: Literal["markdown", "json"], user=Depends(require_product_user)):
    return invoke(topic_id, user, lambda _: synthesis_reports.export(topic_id, job_id, version_id, decision_id, format))


@router.get(ROOT + "/{job_id}/report/comparison")
def compare_reports(topic_id: str, job_id: str, from_job_id: str, user=Depends(require_product_user)):
    return invoke(topic_id, user, lambda _: synthesis_reports.compare(topic_id, job_id, from_job_id))


@router.get(ROOT + "/{job_id}/journal")
def journal_versions(topic_id: str, job_id: str, page: int = Query(1, ge=1), limit: int = Query(10, ge=1, le=100), user=Depends(require_product_user)):
    return invoke(topic_id, user, lambda _: local_journal.listing(topic_id, job_id, page, limit))


@router.get(ROOT + "/{job_id}/journal-working-copy")
def journal_working_copy_detail(topic_id: str, job_id: str, user=Depends(require_product_user)):
    return invoke(topic_id, user, lambda actor: journal_working_copy.latest(topic_id, job_id, actor.subject or actor.actor_type))


async def require_working_copy_encoding(request: Request, user=Depends(require_product_user)) -> None:
    # Pydantic's invalid-Unicode error includes the invalid input. Reject it
    # before that error reaches the JSON response encoder (which would 500).
    try:
        if len(working_copy_json(await request.json()).encode("utf-8")) > 512_000:
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(422, detail="journal_working_copy_invalid") from None


@router.post(ROOT + "/{job_id}/journal-working-copy", dependencies=[Depends(require_working_copy_encoding)])
def save_journal_working_copy(topic_id: str, job_id: str, body: WorkingCopySave, response: Response, user=Depends(require_product_user)):
    def operation(actor):
        saved, created = journal_working_copy.save(topic_id, job_id, body, actor.subject or actor.actor_type)
        response.status_code = 201 if created else 200
        return dict(working_copy=saved, created=created)
    return invoke(topic_id, user, operation)


@router.post(ROOT + "/{job_id}/journal")
def save_journal(topic_id: str, job_id: str, body: JournalSave, response: Response, user=Depends(require_product_user)):
    def operation(actor):
        version, created = local_journal.save(topic_id, job_id, body, actor.subject or actor.actor_type)
        response.status_code = 201 if created else 200
        return dict(version=version, created=created)
    return invoke(topic_id, user, operation)


@router.get(ROOT + "/{job_id}/journal/{version_id}")
def journal_version(topic_id: str, job_id: str, version_id: str, user=Depends(require_product_user)):
    return invoke(topic_id, user, lambda _: local_journal.detail(topic_id, job_id, version_id))


@router.get(ROOT + "/{job_id}/journal/{version_id}/export")
def export_journal(topic_id: str, job_id: str, version_id: str, format: Literal["markdown", "json"], user=Depends(require_product_user)):
    return invoke(topic_id, user, lambda _: local_journal.export(topic_id, job_id, version_id, format))


@router.get(ROOT + "/{job_id}/journal-exchange")
def journal_packet(topic_id: str, job_id: str, user=Depends(require_product_user)):
    return invoke(topic_id, user, lambda _: journal_candidates.input_packet(topic_id, job_id))


@router.get(ROOT + "/{job_id}/journal-candidates")
def imported_journals(topic_id: str, job_id: str, page: int = Query(1, ge=1), limit: int = Query(10, ge=1, le=100), user=Depends(require_product_user)):
    return invoke(topic_id, user, lambda _: journal_candidates.listing(topic_id, job_id, page, limit))


@router.get(ROOT + "/{job_id}/journal-executions")
def stored_journal_executions(topic_id: str, job_id: str, page: int = Query(1, ge=1), limit: int = Query(10, ge=1, le=100), user=Depends(require_product_user)):
    return invoke(topic_id, user, lambda _: journal_executions.listing(topic_id, job_id, page, limit))


@router.post(ROOT + "/{job_id}/journal-executions")
def prepare_journal_execution(topic_id: str, job_id: str, body: ExecutionPrepare, response: Response, user=Depends(require_product_user)):
    def operation(actor):
        execution, created = journal_executions.prepare(topic_id, job_id, body, actor.subject or actor.actor_type)
        response.status_code = 201 if created else 200
        return dict(execution=execution, created=created)
    return invoke(topic_id, user, operation)


@router.get(ROOT + "/{job_id}/journal-connection")
def journal_connection(topic_id: str, job_id: str, user=Depends(require_product_user)):
    return invoke(topic_id, user, lambda _: journal_connection_preparation.summary(topic_id, job_id))


@router.post(ROOT + "/{job_id}/journal-executions/selected-connection")
def prepare_selected_journal_execution(topic_id: str, job_id: str, body: journal_connection_preparation.Prepare,
                                      response: Response, user=Depends(require_product_user)):
    def operation(actor):
        execution, created = journal_connection_preparation.prepare(topic_id, job_id, body, actor.subject or actor.actor_type)
        response.status_code = 201 if created else 200
        return dict(execution=execution, created=created)
    return invoke(topic_id, user, operation)


@router.post(ROOT + "/{job_id}/journal-executions/{execution_id}/candidate")
def preserve_journal_execution(topic_id: str, job_id: str, execution_id: str, body: PreserveExecution, response: Response, user=Depends(require_product_user)):
    def operation(actor):
        candidate, created = journal_candidates.save_execution(topic_id, job_id, execution_id, body.expected_result_digest, actor.subject or actor.actor_type)
        response.status_code = 201 if created else 200
        return dict(candidate=candidate, created=created)
    return invoke(topic_id, user, operation)


@router.get(ROOT + "/{job_id}/journal-executions/{execution_id}")
def journal_execution_detail(topic_id: str, job_id: str, execution_id: str, user=Depends(require_product_user)):
    return invoke(topic_id, user, lambda _: journal_executions.detail(topic_id, job_id, execution_id))


@router.get(ROOT + "/{job_id}/journal-executions/{execution_id}/codex-preview")
def codex_journal_preview(topic_id: str, job_id: str, execution_id: str, request: Request, user=Depends(require_product_user)):
    return invoke(topic_id, user, lambda actor: request.app.state.journal_codex_preview.inspect(
        topic_id, job_id, execution_id, actor.subject or actor.actor_type))


@router.get(ROOT + "/{job_id}/journal-executions/{execution_id}/codex-compatibility")
def codex_journal_compatibility(topic_id: str, job_id: str, execution_id: str, request: Request, user=Depends(require_product_user)):
    return invoke(topic_id, user, lambda actor: request.app.state.journal_codex_preview.compatibility(
        topic_id, job_id, execution_id, actor.subject or actor.actor_type))


@router.post(ROOT + "/{job_id}/journal-executions/{execution_id}/control")
def control_journal_execution(topic_id: str, job_id: str, execution_id: str, body: ExecutionControl, user=Depends(require_product_user)):
    def operation(actor):
        execution, applied = journal_executions.control(topic_id, job_id, execution_id, body, actor.subject or actor.actor_type)
        return dict(execution=execution, applied=applied)
    return invoke(topic_id, user, operation)


@router.post(ROOT + "/{job_id}/journal-candidates")
def import_journal(topic_id: str, job_id: str, body: JournalImport, response: Response, user=Depends(require_product_user)):
    def operation(actor):
        candidate, created = journal_candidates.save(topic_id, job_id, body.raw_json, actor.subject or actor.actor_type)
        response.status_code = 201 if created else 200
        return dict(candidate=candidate, created=created)
    return invoke(topic_id, user, operation)


@router.get(ROOT + "/{job_id}/journal-review-inputs")
def review_input_journal_options(topic_id: str, job_id: str, user=Depends(require_product_user)):
    return invoke(topic_id, user, lambda _: journal_candidates.review_inputs(topic_id, job_id))


@router.post(ROOT + "/{job_id}/journal-review-input-candidates")
def preserve_review_input_journal(topic_id: str, job_id: str, body: ReviewInputJournalCreate,
                                  response: Response, user=Depends(require_product_user)):
    def operation(actor):
        candidate, created = journal_candidates.save_review_input(topic_id, job_id, body, actor.subject or actor.actor_type)
        response.status_code = 201 if created else 200
        return dict(candidate=candidate, created=created)
    return invoke(topic_id, user, operation)


@router.get(ROOT + "/{job_id}/journal-candidates/{candidate_id}")
def imported_journal(topic_id: str, job_id: str, candidate_id: str, user=Depends(require_product_user)):
    return invoke(topic_id, user, lambda _: journal_candidates.detail(topic_id, job_id, candidate_id))


@router.get(ROOT + "/{job_id}/journal-candidates/{candidate_id}/reviews")
def journal_review_history(topic_id: str, job_id: str, candidate_id: str, page: int = Query(1, ge=1), limit: int = Query(10, ge=1, le=100), user=Depends(require_product_user)):
    return invoke(topic_id, user, lambda _: journal_reviews.history(topic_id, job_id, candidate_id, page, limit))


@router.post(ROOT + "/{job_id}/journal-candidates/{candidate_id}/reviews")
def review_journal(topic_id: str, job_id: str, candidate_id: str, body: JournalReviewSave, response: Response, user=Depends(require_product_user)):
    def operation(actor):
        review, created = journal_reviews.save(topic_id, job_id, candidate_id, body, actor.subject or actor.actor_type)
        response.status_code = 201 if created else 200
        return dict(review=review, created=created)
    return invoke(topic_id, user, operation)


@router.get(ROOT + "/{job_id}/journal-candidates/{candidate_id}/reviews/{review_id}")
def journal_review_detail(topic_id: str, job_id: str, candidate_id: str, review_id: str, user=Depends(require_product_user)):
    return invoke(topic_id, user, lambda _: journal_reviews.detail(topic_id, job_id, candidate_id, review_id))


@router.get(ROOT + "/{job_id}/journal-candidates/{candidate_id}/reviews/{review_id}/export")
def export_reviewed_journal(topic_id: str, job_id: str, candidate_id: str, review_id: str, format: Literal["markdown", "json"], user=Depends(require_product_user)):
    return invoke(topic_id, user, lambda _: journal_reviews.export(topic_id, job_id, candidate_id, review_id, format))


@router.get(ROOT + "/{job_id}/journal-candidates/{candidate_id}/reviews/{review_id}/comparison")
def compare_journal_reviews(topic_id: str, job_id: str, candidate_id: str, review_id: str, from_review_id: str, user=Depends(require_product_user)):
    return invoke(topic_id, user, lambda _: journal_reviews.compare(topic_id, job_id, candidate_id, review_id, from_review_id))


@router.get(ROOT + "/{job_id}/journal-executions/{execution_id}/authority")
def journal_authority_detail(topic_id: str, job_id: str, execution_id: str,
                             authorization_id: str = Query(min_length=1, max_length=100),
                             expected_record_digest: str = Query(pattern=r"^[a-f0-9]{64}$"), user=Depends(require_product_user)):
    from app.repositories import journal_authority
    return invoke(topic_id, user, lambda _: journal_authority.detail(topic_id, job_id, execution_id, authorization_id, expected_record_digest))


class WithdrawJournalAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    authorization_id: str = Field(min_length=1, max_length=100, pattern=r"\S")
    expected_record_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_id: str = Field(min_length=1, max_length=100, pattern=r"\S")
    reason: str = Field(min_length=1, max_length=2000, pattern=r"\S")


@router.post(ROOT + "/{job_id}/journal-executions/{execution_id}/authority/revoke")
def withdraw_journal_authority(topic_id: str, job_id: str, execution_id: str,
                               body: WithdrawJournalAuthority, user=Depends(require_product_user)):
    from app.repositories import journal_authority
    def operation(actor):
        # The same workspace permission as cancellation can only remove an
        # allowance. It cannot create one or attest to its original approval.
        try:
            revocation, applied = journal_authority.revoke(
                topic_id, job_id, execution_id, body.authorization_id, request_id=body.request_id,
                issuer=actor.subject or actor.actor_type, reason=body.reason, expected_record_digest=body.expected_record_digest)
        except ValueError as exc:
            if str(exc) in {"journal_authority_conflict", "journal_authority_changed"}:
                raise HTTPException(409, detail=str(exc)) from None
            raise
        return dict(revocation=revocation, applied=applied,
                    execution=journal_executions.detail(topic_id, job_id, execution_id))
    return invoke(topic_id, user, operation)
