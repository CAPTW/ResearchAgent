"""Pure comparison preparation and capture inspection; no credentials or sender.

The plan is not a grant, execution record, receipt or scientific assessment.
Its common prompt differs from the historical trial: both arms must see the
output schema, even when the API's constrained-output option is absent.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
from typing import Any, Literal

from app.domain import prepared_review
from app.domain.journal_exchange import preserved_json, sha
from app.services.local_anthropic_trial import build_trial_plan
from app.services.providers.anthropic_messages import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, SCHEMA_COMPILER_V2, parse_response, parse_text_response, rejection_diagnostic
from app.services.providers.base import ProviderError, ProviderOutputError, ProviderRefusalError, ProviderReportedFailure

SCHEMA = "ANTHROPIC_OUTPUT_COMPARISON_PLAN_V1"
ARMS = ("text", "structured")


def prepare(contract: dict[str, Any]) -> dict[str, Any]:
    """Propose two requests, with only the output-format setting different."""
    import json

    frozen = prepared_review.validate_contract(contract)
    body, prior_manifest = build_trial_plan(frozen, 300, SCHEMA_COMPILER_V2)
    structured = json.loads(body)
    structured["system"] += "\nRequired output JSON schema:\n" + preserved_json(frozen["provider_request"]["schema"])
    text = deepcopy(structured)
    del text["output_config"]
    arms = {}
    for name, payload in (("text", text), ("structured", structured)):
        encoded = preserved_json(payload).encode("utf-8")
        if len(encoded) > MAX_REQUEST_BYTES:
            raise ValueError("anthropic_comparison_request_too_large")
        arms[name] = dict(body=payload, body_sha256=hashlib.sha256(encoded).hexdigest(), body_bytes=len(encoded))
    plan = dict(schema_version=SCHEMA, state="PREPARED_NOT_AUTHORIZED", authority="NOT_ESTABLISHED",
        live_execution="NOT_RUN", contract=frozen, source_contract_sha256=sha(preserved_json(frozen)),
        source_request_sha256=frozen["request_sha256"], effective_system_sha256=sha(text["system"]),
        common_payload_sha256=sha(preserved_json(text)), arms=arms,
        controlled_difference="output_config.format", shared_schema_context="ORIGINAL_LOCAL_SCHEMA_IN_SYSTEM",
        historical_response_role="REFERENCE_ONLY_NOT_MATCHED_CONTROL",
        policy=dict(endpoint=prior_manifest["endpoint"], method="POST", api_version=prior_manifest["api_version"],
            schema_compiler=SCHEMA_COMPILER_V2, http_timeouts=prior_manifest["http_timeouts"],
            timeout_semantics="PER_OPERATION_INACTIVITY", automatic_retry=False,
            proposed_max_attempts=dict(text=1, structured=1), proposed_order=["text", "structured"],
            stop_after_uncertain_outcome=True, account_binding="NOT_ESTABLISHED",
            redirects=False, environment_proxy=False, tools=[], stream=False,
            temperature_policy="OMITTED_PROVIDER_DEFAULT", thinking_policy="OMITTED_PROVIDER_DEFAULT",
            cost_limit_enforced=False, output_token_limit_enforced=False),
        scientific_support="NOT_ASSESSED", full_document_review="NOT_ESTABLISHED", human_adoption="NOT_ESTABLISHED")
    plan["plan_sha256"] = sha(preserved_json(plan))
    return plan


def validate_plan(plan: dict[str, Any]) -> dict[str, Any]:
    try:
        expected = prepare(plan["contract"])
        if preserved_json(plan) != preserved_json(expected):
            raise ValueError()
        return expected
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError, OverflowError):
        raise ValueError("anthropic_comparison_plan_invalid") from None


def assess_capture(plan: dict[str, Any], *, arm: str, http_status: int, raw: bytes,
                   capture_origin: Literal["SYNTHETIC", "HISTORICAL_REFERENCE", "UNVERIFIED_CAPTURE"]) -> dict[str, Any]:
    """Inspect caller-supplied bytes, without attesting which request made them."""
    frozen = validate_plan(plan)
    if (arm not in ARMS or type(http_status) is not int or not 100 <= http_status <= 599
            or not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES
            or capture_origin not in {"SYNTHETIC", "HISTORICAL_REFERENCE", "UNVERIFIED_CAPTURE"}):
        raise ValueError("anthropic_comparison_capture_invalid")
    result = dict(schema_version="ANTHROPIC_OUTPUT_COMPARISON_ASSESSMENT_V1",
        plan_sha256=frozen["plan_sha256"], arm=arm,
        planned_request_body_sha256=frozen["arms"][arm]["body_sha256"],
        raw_response_sha256=hashlib.sha256(raw).hexdigest(), http_status=http_status, capture_origin=capture_origin,
        request_response_binding="NOT_VERIFIED_OFFLINE_CAPTURE", transport_status="HTTP_REJECTED",
        review_status="NOT_EVALUATED", reason_codes=[], reported_model=None, response_id=None, usage=None,
        final_text=None, provider_diagnostic=rejection_diagnostic(http_status, raw),
        scientific_support="NOT_ASSESSED", full_document_review="NOT_ESTABLISHED", human_adoption="NOT_ESTABLISHED")
    if http_status != 200:
        return result
    try:
        captured = parse_text_response(raw, max_output_bytes=prepared_review.MAX_OUTPUT_BYTES)
    except ProviderRefusalError:
        result["transport_status"] = "REFUSED"
    except ProviderReportedFailure:
        result["transport_status"] = "INCOMPLETE"
    except ProviderError:
        result["transport_status"] = "ENVELOPE_INVALID"
    else:
        result.update(transport_status="FINAL_TEXT_CAPTURED", final_text=captured.text,
            reported_model=captured.model, response_id=captured.response_id, usage=captured.usage)
        try:
            parsed = parse_response(raw, max_output_bytes=prepared_review.MAX_OUTPUT_BYTES)
        except ProviderOutputError:
            result["review_status"] = "REVIEW_JSON_INVALID"
        else:
            reasons = prepared_review.output_rejection_reasons(frozen["contract"], parsed)
            result.update(review_status="CONTRACT_INVALID" if reasons else "CONTRACT_VALID", reason_codes=reasons)
    return result


def write_bundle(plan: dict[str, Any], output_dir: Path) -> None:
    """Write a new directory only; never overwrite a previous plan or history."""
    import json

    frozen = validate_plan(plan)
    if not output_dir.is_absolute():
        raise ValueError("anthropic_comparison_output_must_be_absolute")
    output_dir.mkdir()  # Fails on existing files/directories, including symlinks.
    artifacts = {"plan.json": json.dumps(frozen, ensure_ascii=False, indent=2).encode("utf-8"),
        "contract.json": preserved_json(frozen["contract"]).encode("utf-8")}
    for arm in ARMS:
        artifacts[f"{arm}-request.json"] = preserved_json(frozen["arms"][arm]["body"]).encode("utf-8")
    artifacts["PREVIEW.md"] = preview(frozen).encode("utf-8")
    for name, content in artifacts.items():
        with (output_dir / name).open("xb") as target:
            target.write(content)


def preview(plan: dict[str, Any]) -> str:
    frozen = validate_plan(plan)
    body = frozen["arms"]["text"]["body"]
    packet = frozen["contract"]["packet"]
    return "\n".join([
        "# Anthropic 일반 텍스트 / 구조화 출력 비교 준비", "",
        "상태: PREPARED_NOT_AUTHORIZED · 실제 호출: NOT_RUN", "",
        f"- 모델: `{body['model']}`", f"- 논문 ID: `{packet['paper_id']}`",
        f"- 선택 본문 SHA256: `{packet['source_text_sha256']}`",
        f"- 비교 계획 SHA256: `{frozen['plan_sha256']}`", "",
        "두 요청은 동일한 본문·지시·JSON 스키마를 공유합니다. 일반 텍스트 경로도 JSON 작성을 요청하되,",
        "API의 구조화 출력 강제 설정을 생략합니다. 자유 산문과 다른 과제를 비교하는 시험이 아닙니다.",
        "변경 변수는 output_config.format 유무뿐입니다. 스키마를 공통 지시에 추가했으므로 이전 실패",
        "응답은 과거 참고자료이고 이 비교의 대조군이 아닙니다.", "",
        "| 경로 | 요청 파일 | 요청 SHA256 | 현재 결과 |", "|---|---|---|---|",
        *[f"| {arm} | {arm}-request.json | {frozen['arms'][arm]['body_sha256']} | NOT_RUN |" for arm in ARMS], "",
        f"제안: text 다음 structured, 각각 최대 1회, 요청당 max_tokens={body['max_tokens']}.",
        "자동 재시도 없음. 결과 불명·시간 초과 시 중단하고 상태를 확인합니다. 연결10/읽기300/쓰기30/풀10초는",
        "각 작업의 무응답 제한이며 전체 실행 시간 보장이 아닙니다. 모델 기본 thinking/temperature를 유지합니다.",
        "토큰 요청값은 금액 상한이 아닙니다. 계정·키는 조회하지 않았으며 실제 호출 승인·비용 결정은 별도입니다.", "",
        "판정은 HTTP 상태 → 정상 최종 텍스트 → JSON 객체 → 기존 본문·논문·인용 계약 순서입니다.",
        "CONTRACT_VALID도 과학적 타당성·전체 논문 검토·사람의 채택을 뜻하지 않습니다.",
        "텍스트 수신 후 JSON 검증 실패를 통신 무응답으로 합치지 않습니다. 응답을 고치거나 자동 채택하지 않습니다.",
        "한 쌍의 차이는 원인 후보를 좁히는 관찰이며, 무작위성·시간/순서 효과를 배제한 인과 증명이 아닙니다.", "",
        "이 도구에는 실행·키 조회·승인 발급 기능이 없습니다. 향후 실제 실행은 새 권한과 정확한 요청/계정",
        "연결이 필요합니다. 기존에 소비된 trial/grant는 재사용하지 않습니다.", "",
    ])
