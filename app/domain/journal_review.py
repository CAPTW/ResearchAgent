"""Versioned human judgments, distinct from immutable machine binding checks."""
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.journal_exchange import SECTION_LABELS, review_input_lines, sha
from app.domain.local_journal import quote
from app.domain.temporal_memory import digest
from app.domain.generated_journal import output_provenance_lines

SCHEMA = "JOURNAL_HUMAN_REVIEW_V1"
REPORT_SCHEMA = "HUMAN_REVIEWED_JOURNAL_V1"
ASSESSMENTS = {"not_assessed": "미검토", "supported": "근거와 부합", "limited": "제한적으로 부합", "unsupported": "근거 부족"}
KINDS = {"observation": "관찰 문장", "interpretation": "해석 문장", "question": "질문"}


class EntryReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    entry_id: str = Field(pattern=r"^jentry_[a-f0-9]{64}$")
    assessment: Literal["not_assessed", "supported", "limited", "unsupported"]
    note: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def reasons(self):
        if self.assessment in {"limited", "unsupported"} and not self.note.strip():
            raise ValueError("journal_review_reason_required")
        return self


class Save(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=100)
    expected_previous_id: str | None = Field(default=None, max_length=100)
    expected_candidate_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    state: Literal["reviewing", "accepted", "held", "rejected"]
    note: str = Field(default="", max_length=2000)
    entries: list[EntryReview] = Field(min_length=1, max_length=30)


def entries(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for section in candidate["proposal"]["sections"]:
        for position, entry in enumerate(section["entries"]):
            binding = next((b for b in candidate.get('component_bindings', []) if b['section'] == section['kind'] and b['position'] == position), None)
            result.append(dict(entry_id="jentry_" + digest([candidate["candidate_id"], section["kind"], position, entry]),
                               section=section["kind"], position=position, **entry, **({'source_component': binding} if binding else {})))
    return result


def validate(candidate: dict[str, Any], body: Save) -> None:
    expected = [entry["entry_id"] for entry in entries(candidate)]
    received = [entry.entry_id for entry in body.entries]
    if len(received) != len(set(received)) or set(received) != set(expected):
        raise ValueError("journal_review_entries_invalid")
    if body.expected_candidate_digest != digest(candidate):
        raise ValueError("journal_review_binding_changed")
    if body.state == "accepted" and any(entry.assessment not in {"supported", "limited"} for entry in body.entries):
        raise ValueError("journal_review_incomplete")


def report(candidate: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    version = "jrpt_" + digest([REPORT_SCHEMA, review["review_id"]])
    notes = {entry["entry_id"]: entry for entry in review["entries"]}
    numbers = {ref["citation_id"]: n for n, ref in enumerate(candidate["citations"], 1)}
    lines = ["# 사람이 검토한 로컬 저널", "", quote(candidate["proposal"]["title"]), "",
             "사용자가 보존 근거와 문장을 검토해 활용 대상으로 채택했습니다. 독립적인 과학적 검증·모순 평가·Provider 실행 확인이나 공개·배포를 뜻하지 않습니다.",
             "제한적 부합 판단과 메모도 이 채택본의 일부입니다. 질문에 대한 부합은 질문의 문맥에 관한 판단이며 답을 확인했다는 뜻이 아닙니다.", "",
             f"- 채택본: {version}", f"- 검토 기록: {review['review_id']}", f"- 검토자: {review['actor_id']}",
             f"- 검토 시각: {review['created_at']}", f"- 후보: {candidate['candidate_id']}",
             (f"- 선택한 분석 항목: {candidate['coverage']['selected_components']}" if candidate['schema_version'] == 'SAVED_ANALYSIS_JOURNAL_CANDIDATE_V1'
              else f"- 인용한 논문: {candidate['coverage']['cited_papers']} / {candidate['coverage']['selected_papers']}"), "",
             "## 검토 메모", "", quote(review["note"] or "메모 없음"), ""]
    if candidate['schema_version'] == 'SAVED_ANALYSIS_JOURNAL_CANDIDATE_V1':
        from app.domain.saved_analysis_journal import provenance_lines
        lines += provenance_lines(candidate)
    if candidate['schema_version'] == 'REVIEW_INPUT_JOURNAL_CANDIDATE_V1':
        from app.domain.review_input_journal import provenance_lines as review_input_provenance_lines
        lines += review_input_provenance_lines(candidate)
    if candidate["schema_version"] == "GENERATED_JOURNAL_CANDIDATE_V1":
        receipt = candidate["execution"]
        contract, reported = receipt["contract"], receipt["provider_reported"]
        lines += ["## 보존된 생성 출처", "", "어댑터가 보고한 실행 정보입니다. 실제 외부 모델 실행의 독립적인 증명이 아닙니다.", "",
                  quote(f"실행 기록: {contract['execution_id']} · 시도: {receipt['attempt']}"),
                  quote(f"요청 Provider·모델: {contract['specification']['provider_name']} · {contract['specification']['model']}"),
                  quote(f"응답 Provider·모델: {reported['provider_name']} · {reported['model']}"),
                  quote(f"응답 ID: {reported['response_id'] or '기록 없음'}"),
                  quote(f"입력 묶음: {candidate['packet']['packet_id']}"),
                  quote(f"실행 결과 SHA-256: {receipt['result_digest']}"), ""]
        lines += output_provenance_lines(receipt)
    for index, entry in enumerate(entries(candidate), 1):
        judgment = notes[entry["entry_id"]]
        if entry.get('source_component'):
            lines += [quote('원래 분석 항목: ' + entry['source_component']['label'] + ' · ' + entry['source_component']['component_id']), '']
            if candidate['schema_version'] == 'REVIEW_INPUT_JOURNAL_CANDIDATE_V1':
                lines += [quote('원래 항목의 근거 상태: ' + entry['source_component']['support']), '']
        lines += [f"## {index}. {SECTION_LABELS[entry['section']]} · {KINDS[entry['kind']]}", "", quote(entry["text"]), "",
                  "인용: " + ", ".join(f"[{numbers[ref]}]" for ref in entry["citation_ids"]), "",
                  "사용자 판단: " + ASSESSMENTS[judgment["assessment"]], "", quote(judgment["note"] or "메모 없음"), ""]
    for index, ref in enumerate(candidate["citations"], 1):
        lines += [f"## 인용 [{index}]", "", quote(ref["title"]), "", quote(ref["text"]), "",
                  quote(json.dumps({k: v for k, v in ref.items() if k not in {"title", "text"}}, ensure_ascii=False)), ""]
    lines += review_input_lines(candidate["packet"])
    content = "\n".join(lines)
    return dict(schema_version=REPORT_SCHEMA, version_id=version, review_id=review["review_id"],
                candidate_digest=review["candidate_digest"], content=content, content_sha256=sha(content))


def comparison(candidate: dict[str, Any], before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    old = {entry["entry_id"]: entry for entry in before["entries"]}
    new = {entry["entry_id"]: entry for entry in after["entries"]}
    compared = []
    for entry in entries(candidate):
        previous, current = old[entry["entry_id"]], new[entry["entry_id"]]
        changes = {key: previous[key] != current[key] for key in ("assessment", "note")}
        compared.append(dict(**entry, before=previous, after=current, changes=changes, changed=any(changes.values())))

    def summary(review: dict[str, Any]) -> dict[str, Any]:
        return {**{key: review[key] for key in ("review_id", "state", "note", "actor_id", "created_at")},
                "report_version_id": review["report"]["version_id"] if review["report"] else None}

    changed = sum(entry["changed"] for entry in compared)
    return dict(schema_version="JOURNAL_REVIEW_COMPARISON_V1" if "job_id" in candidate else "SAVED_ANALYSIS_JOURNAL_COMPARISON_V1", topic_id=candidate["topic_id"], candidate_id=candidate["candidate_id"],
                **({"job_id": candidate["job_id"]} if "job_id" in candidate else {"analysis_id": candidate["analysis_id"]}),
                candidate_digest=digest(candidate), input_digest=candidate["input_digest"], packet_id=candidate["packet"]["packet_id"],
                title=candidate["proposal"]["title"], **{"from": summary(before), "to": summary(after)},
                overall_changes={key: before[key] != after[key] for key in ("state", "note")},
                counts=dict(total=len(compared), changed=changed, unchanged=len(compared) - changed,
                            assessment_changed=sum(e["changes"]["assessment"] for e in compared), note_changed=sum(e["changes"]["note"] for e in compared)),
                entries=compared, citations=candidate["citations"], scientific_assessment="NOT_ASSESSED")
