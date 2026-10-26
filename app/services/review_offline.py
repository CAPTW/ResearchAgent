"""App connection test for saved body ranges. No scientific inference or live path."""
from copy import deepcopy
from dataclasses import asdict
import json
import os
from typing import Any

from app.auth.resource_access import ResourceActor
from app.domain import prepared_review
from app.domain.journal_exchange import preserved_json
from app.domain.review_offline import ADAPTER, VERSION, MODEL, MODE, Execute
from app.domain.review_input import validate_packet
from app.repositories import review_offline as repository
from app.services import review_preparation
from app.services.ai_provider import AIReviewProvider, ProviderRequest, ProviderResponse
from app.services.offline_environment import require_isolated_data_root
from app.services.providers.base import ProviderRequest as StructuredRequest, ProviderResult


class SyntheticReviewProvider(AIReviewProvider):
    provider_name = ADAPTER

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        packet = request.packet
        validate_packet(packet)
        if (packet.get("packet_version") not in {"ai-review-packet-v3", "ai-review-packet-v4"} or request.provider_name != ADAPTER
                or request.mode != "stub" or request.explicit_live or request.command or request.model is not None):
            raise ValueError("review_offline_target_mismatch")
        message = "오프라인 합성 연결 시험입니다. 논문 내용과 연구적 타당성은 평가하지 않았습니다."
        draft = dict(paper_id=packet["paper_id"], source_text_sha256=packet["source_text_sha256"],
                     contribution_summary=message, method_summary=message, evidence_summary=message,
                     relevance_to_topic=message, novelty_signal="NOT_ASSESSED", practical_implications=message,
                     limitations=["실제 모델 실행·과학적 검토·논문 전체 검토가 아닙니다. confidence 0은 스키마 호환용 합성 값이며 평가 점수가 아닙니다."],
                     follow_up_questions=[], recommended_tags=[], cited_evidence_span_ids=[packet["evidence_spans"][0]["span_id"]],
                     confidence=0.0, unsupported_claims=[], review_level="full_text_grounded")
        return ProviderResponse(status="accepted", provider_name=ADAPTER, run_id=request.run_id,
                                paper_id=packet["paper_id"], parsed_draft=draft)


class SyntheticStructuredReviewProvider:
    """Fixed, pure local adapter; no injected implementation or live registry.

    This is intentionally safe to call in the existing atomic offline save. A
    transport adapter must use a durable dispatch/unknown-outcome protocol first.
    """
    provider_name = ADAPTER

    def is_configured(self) -> bool:
        return True

    def describe_status(self) -> dict[str, Any]:
        return dict(provider=ADAPTER, model=MODEL, mode=MODE, live_call_enabled=False)

    def generate_json(self, request: StructuredRequest) -> ProviderResult:
        try:
            packet = json.loads(request.prompt)
            version = {
                "PREPARED_PAPER_REVIEW_DRAFT_V1": prepared_review.LEGACY_SCHEMA,
                "PREPARED_PAPER_REVIEW_DRAFT_V2": prepared_review.SCHEMA_V2,
                "PREPARED_PAPER_REVIEW_DRAFT_V3": prepared_review.SCHEMA,
            }[request.schema_name]
            expected = prepared_review.prepare(packet, provider_name=ADAPTER, model=MODEL, schema_version=version)
            if preserved_json(asdict(request)) != preserved_json(expected["provider_request"]):
                raise ValueError()
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
            raise ValueError("review_offline_target_mismatch") from None
        response = SyntheticReviewProvider().generate(ProviderRequest(provider_name=ADAPTER, packet=deepcopy(packet),
            mode="stub", run_id=packet["source_preparation"]["source"]["run_id"]))
        if response.status != "accepted" or response.parsed_draft is None:
            raise ValueError("review_offline_adapter_failed")
        return ProviderResult(provider_name=ADAPTER, model=MODEL, response_id=None,
                              payload=deepcopy(response.parsed_draft), raw_text=preserved_json(response.parsed_draft))


class OfflineReviewRuntime:
    def generate_record(self, packet: dict[str, Any]) -> dict[str, Any]:
        contract = prepared_review.prepare(packet, provider_name=ADAPTER, model=MODEL)
        # Keep this constructor fixed. Arbitrary Provider injection here would
        # place external I/O inside the offline repository's SQLite transaction.
        request = StructuredRequest(**deepcopy(contract["provider_request"]))
        response = SyntheticStructuredReviewProvider().generate_json(request)
        if preserved_json(asdict(request)) != preserved_json(contract["provider_request"]):
            raise ValueError("review_offline_input_changed")
        if response.provider_name != ADAPTER or response.model != MODEL:
            raise ValueError("review_offline_target_mismatch")
        record = prepared_review.result(contract, response)
        if not record["validation"]["accepted"]:
            raise ValueError("review_offline_adapter_failed")
        record["validation"]["execution"] = dict(mode=MODE, adapter_id=ADAPTER, adapter_version=VERSION, scientific_assessment="NOT_ASSESSED")
        return record


def build_runtime() -> OfflineReviewRuntime | None:
    if os.environ.get("XRML_REVIEW_OFFLINE_ENABLED", "0") != "1":
        return None
    require_isolated_data_root("XRML_REVIEW_OFFLINE", "review_offline_configuration_invalid")
    return OfflineReviewRuntime()


def status(runtime: OfflineReviewRuntime | None, topic: str, version: str, preparation_id: str,
           actor: ResourceActor) -> dict[str, Any]:
    occurrences = review_preparation._occurrences(topic, version, actor)
    digest, latest = repository.status(topic, version, preparation_id, actor.subject or actor.actor_type, occurrences)
    return dict(enabled=runtime is not None, mode=MODE, adapter_id=ADAPTER, adapter_version=VERSION,
                reason=None if runtime else "review_offline_disabled", preparation_digest=digest, latest=latest)


def execute(runtime: OfflineReviewRuntime | None, topic: str, version: str, preparation_id: str,
            body: Execute, actor: ResourceActor) -> dict[str, Any]:
    body = Execute.model_validate(body.model_dump(), strict=True)
    occurrences = review_preparation._occurrences(topic, version, actor)
    return repository.save(topic, version, preparation_id, actor.subject or actor.actor_type, body, occurrences,
                           runtime.generate_record if runtime else None)
