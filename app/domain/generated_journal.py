"""Preserved adapter results with origin distinct from user-imported prose."""
import json
from typing import Any

from app.domain import journal_exchange as exchange, journal_execution as execution
from app.domain.local_journal import quote
from app.domain.temporal_memory import digest
from app.services.providers.base import ProviderResult

SCHEMA = "GENERATED_JOURNAL_CANDIDATE_V1"
ORIGIN = "RECORDED_ADAPTER_RESULT"


def identity(contract: dict[str, Any], event: dict[str, Any]) -> str:
    return "jgen_" + digest([SCHEMA, contract["execution_id"], event["attempt"], digest(event["result"])])


def _validate_manifest(contract: dict[str, Any], manifest: dict[str, Any]) -> None:
    from app.services.providers.claude_journal_plan import build_plan as claude_plan
    from app.services.providers.antigravity_journal_plan import build_plan as antigravity_plan
    from app.services.providers.grok_journal_plan import build_plan as grok_plan
    from app.services.providers.anthropic_journal_wire import MANIFEST_SCHEMA as anthropic_schema, build_wire as anthropic_plan
    from app.services.providers.codex_journal_plan_v2 import MANIFEST_SCHEMA as codex_v2_schema, build_plan as codex_v2_plan
    plans = {"CLAUDE_JOURNAL_CLI_V1": claude_plan, "ANTIGRAVITY_JOURNAL_CLI_V1": antigravity_plan, "GROK_JOURNAL_ACP_V1": grok_plan}
    expected: object
    if manifest.get("schema_version") == anthropic_schema:
        expected = anthropic_plan(contract, manifest["timeout_seconds"])[-1]
    elif manifest.get("schema_version") == codex_v2_schema:
        expected = codex_v2_plan(contract, runtime_identity=manifest["runtime_identity"],
            reasoning_effort=manifest["reasoning_effort"], timeout_seconds=manifest["timeout_seconds"])[-1]
    elif manifest.get("schema_version") in plans:
        expected = plans[manifest["schema_version"]](contract, runtime_identity=manifest["runtime_identity"], timeout_seconds=manifest["timeout_seconds"])[-1]
    else:
        raise ValueError("journal_candidate_integrity_invalid")
    if exchange.preserved_json(manifest) != exchange.preserved_json(expected):
        raise ValueError("journal_candidate_integrity_invalid")


def output_provenance_lines(receipt: dict[str, Any]) -> list[str]:
    if receipt["schema_version"] != "JOURNAL_EXECUTION_RECEIPT_V2":
        return []
    if receipt["adapter_manifest"]["schema_version"] == "CODEX_JOURNAL_CLI_V2":
        return ["## 구조화된 결과의 보존 방식", "",
                "이 원고는 Codex 최종 메시지의 JSON을 개행·문자 그대로 보존합니다. 전체 CLI 출력 로그는 Journal 결과에 보존되지 않습니다.",
                "모델·계정·과금 경로 및 과학적 검토 완료는 독립적으로 확인되지 않았습니다.",
                quote(f"전송 계획: {receipt['adapter_manifest']['schema_version']}"),
                quote(f"전송 계획 SHA-256: {digest(receipt['adapter_manifest'])}"), ""]
    if receipt["adapter_manifest"]["schema_version"] == "ANTHROPIC_JOURNAL_WIRE_V1":
        return ["## 구조화된 결과의 보존 방식", "",
                "이 원고는 Anthropic Messages의 content text JSON을 개행·문자 그대로 보존합니다. 전체 HTTP 응답은 보존되지 않습니다.",
                "모델·계정·API 과금 경로 및 과학적 검토 완료는 독립적으로 확인되지 않았습니다.",
                quote(f"전송 계획: {receipt['adapter_manifest']['schema_version']}"),
                quote(f"전송 계획 SHA-256: {digest(receipt['adapter_manifest'])}"), ""]
    if receipt["adapter_manifest"]["schema_version"] == "GROK_JOURNAL_ACP_V1":
        return ["## 구조화된 결과의 보존 방식", "",
                "이 원고는 Grok ACP 응답 문장에서 추출한 JSON을 개행·문자 그대로 보존합니다. 생각 조각과 전체 ACP 대화 로그는 Journal 결과에 보존되지 않습니다.",
                "모델·계정·구독 과금 경로 및 과학적 검토 완료는 독립적으로 확인되지 않았습니다.",
                quote(f"전송 계획: {receipt['adapter_manifest']['schema_version']}"),
                quote(f"전송 계획 SHA-256: {digest(receipt['adapter_manifest'])}"), ""]
    if receipt["adapter_manifest"]["schema_version"] == "ANTIGRAVITY_JOURNAL_CLI_V1":
        return ["## 구조화된 결과의 보존 방식", "",
                "이 원고는 Antigravity 최종 결과 응답의 JSON을 받은 그대로 보존합니다. 중간 문장 조각과 전체 NDJSON 출력 로그는 Journal 결과에 보존되지 않습니다.",
                "모델·계정·구독 과금 경로 및 과학적 검토 완료는 독립적으로 확인되지 않았습니다.",
                quote(f"전송 계획: {receipt['adapter_manifest']['schema_version']}"),
                quote(f"전송 계획 SHA-256: {digest(receipt['adapter_manifest'])}"), ""]
    return ["## 구조화된 결과의 보존 방식", "",
            "이 원고의 JSON은 Claude 구조화된 결과에서 추출해 직렬화한 값입니다. CLI 표준 출력 원문은 Journal 결과에 보존되지 않습니다.",
            "모델·계정·구독 과금 경로 및 과학적 검토 완료는 독립적으로 확인되지 않았습니다.",
            quote(f"전송 계획: {receipt['adapter_manifest']['schema_version']}"),
            quote(f"전송 계획 SHA-256: {digest(receipt['adapter_manifest'])}"), ""]


def assemble(contract: dict[str, Any], event: dict[str, Any], actor: str, created: str,
             *, adapter_manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    if event["state"] != "SUCCEEDED" or event["result"] is None:
        raise ValueError("journal_execution_not_succeeded")
    result = event["result"]
    execution.validate_result(result, contract)
    proposal, frozen, citations = exchange.parse(result["raw_json"]), contract["packet"], result["citations"]
    selected, cited = frozen["selected_paper_version_ids"], {ref["paper_version_id"] for ref in citations}
    coverage = exchange.citation_coverage(frozen, citations)
    candidate_id = identity(contract, event)
    receipt = dict(schema_version="JOURNAL_EXECUTION_RECEIPT_V1", contract=contract, attempt=event["attempt"],
                   completed_at=event["at"], result_digest=digest(result), provider_reported=result["provider_reported"])
    if adapter_manifest is not None:
        _validate_manifest(contract, adapter_manifest)
        receipt.update(schema_version="JOURNAL_EXECUTION_RECEIPT_V2", adapter_manifest=json.loads(exchange.preserved_json(adapter_manifest)),
                       adapter_manifest_digest=digest(adapter_manifest))
    json_label = "추출 JSON" if adapter_manifest is not None else "원본 JSON"
    if adapter_manifest is not None and adapter_manifest["schema_version"] in {"ANTIGRAVITY_JOURNAL_CLI_V1", "CODEX_JOURNAL_CLI_V2"}:
        json_label = "결과 JSON"
    if adapter_manifest is not None and adapter_manifest["schema_version"] in {"GROK_JOURNAL_ACP_V1", "ANTHROPIC_JOURNAL_WIRE_V1"}:
        json_label = "응답 JSON"
    lines = ["# 저장된 생성 결과 후보 · 미검토", "", quote(proposal.title), "",
             "성공 상태로 저장된 어댑터 응답에서 보존했습니다. 모델·응답 식별자는 어댑터가 보고한 값이며 실제 외부 모델 실행의 독립적인 증명이 아닙니다.",
             "고정 입력·인용 연결만 검사했습니다. 문장 지지 여부·과학적 타당성·모순은 미평가이며 채택된 보고서가 아닙니다.", "",
             f"- 후보: {candidate_id}", f"- 실행 기록: {contract['execution_id']}", f"- 시도: {event['attempt']}",
             f"- 입력 묶음: {frozen['packet_id']}", f"- {json_label} SHA-256: {result['raw_sha256']}",
             f"- 인용된 논문: {len(cited)} / {len(selected)}", "",
             "## 실행 출처", "", quote(str(receipt["provider_reported"]["provider_name"])),
             quote(str(receipt["provider_reported"]["model"])), ""]
    markdown = "\n".join(lines + output_provenance_lines(receipt) + exchange.manuscript_lines(proposal, citations) + exchange.review_input_lines(frozen))
    return dict(schema_version=SCHEMA, candidate_id=candidate_id, topic_id=contract["topic_id"], job_id=contract["job_id"],
                input_digest=frozen["input_digest"], origin=ORIGIN, preserved_by=actor, preserved_at=created, review_state="UNREVIEWED",
                packet=frozen, proposal=proposal.model_dump(), citations=citations, coverage=coverage, execution=receipt,
                validation=result["validation"], raw_json=result["raw_json"], raw_sha256=result["raw_sha256"],
                markdown=markdown, content_sha256=exchange.sha(markdown))


def validate(value: dict[str, Any], job: dict[str, Any]) -> None:
    receipt, frozen = value["execution"], value["packet"]
    contract = receipt["contract"]
    execution.validate_contract(contract, job)
    if receipt["schema_version"] == "JOURNAL_EXECUTION_RECEIPT_V2":
        if (set(receipt) != {"schema_version", "contract", "attempt", "completed_at", "result_digest", "provider_reported",
                           "adapter_manifest", "adapter_manifest_digest"}
                or receipt["adapter_manifest_digest"] != digest(receipt["adapter_manifest"])
                or quote(f"전송 계획 SHA-256: {receipt['adapter_manifest_digest']}") not in value["markdown"]):
            raise ValueError("journal_candidate_integrity_invalid")
        _validate_manifest(contract, receipt["adapter_manifest"])
    elif receipt["schema_version"] != "JOURNAL_EXECUTION_RECEIPT_V1" or "adapter_manifest" in receipt:
        raise ValueError("journal_candidate_integrity_invalid")
    result = execution.result(contract, ProviderResult(**receipt["provider_reported"], payload=value["proposal"], raw_text=value["raw_json"]))
    expected_coverage = exchange.citation_coverage(frozen, result["citations"])
    if (value["schema_version"] != SCHEMA or value["origin"] != ORIGIN or value["review_state"] != "UNREVIEWED"
            or frozen != contract["packet"]
            or type(receipt["attempt"]) is not int or not 1 <= receipt["attempt"] <= contract["specification"]["max_attempts"]
            or value["candidate_id"] != identity(contract, dict(attempt=receipt["attempt"], result=result))
            or any(value[key] != job[key] for key in ("topic_id", "job_id", "input_digest"))
            or receipt["result_digest"] != digest(result) or value["citations"] != result["citations"]
            or value["coverage"] != expected_coverage or value["validation"] != result["validation"]
            or value["raw_sha256"] != result["raw_sha256"] or value["content_sha256"] != exchange.sha(value["markdown"])):
        raise ValueError("journal_candidate_integrity_invalid")
    if (frozen.get("schema_version") == exchange.REVIEW_PACKET_SCHEMA
            and exchange.preserved_json(value["coverage"]) != exchange.preserved_json(expected_coverage)):
        raise ValueError("journal_candidate_integrity_invalid")
