"""User-authored review prose bound to exact frozen text, not model synthesis."""
import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.temporal_memory import canonical_json, digest

SCHEMA = "LOCAL_CITED_JOURNAL_V1"
KINDS = {"observation": "사용자 관찰", "interpretation": "사용자 해석", "question": "후속 질문"}


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["observation", "interpretation", "question"]
    text: str = Field(min_length=1, max_length=3000)
    citation_ids: list[str] = Field(min_length=1, max_length=20)

    @field_validator("text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("journal_text_required")
        return value

    @field_validator("citation_ids")
    @classmethod
    def unique_refs(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(not 1 <= len(item) <= 100 for item in value):
            raise ValueError("journal_citation_invalid")
        return value


class Save(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=100)
    expected_previous_id: str | None = Field(default=None, max_length=100)
    expected_input_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    title: str = Field(min_length=1, max_length=200)
    entries: list[Entry] = Field(min_length=1, max_length=30)

    @field_validator("title")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("journal_title_required")
        return value


def references(job: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for paper in job["inputs"]["papers"]:
        common = {key: paper[key] for key in ("paper_version_id", "canonical_paper_id", "title", "source_identity",
                  "metadata_digest", "review_state", "rights", "review_source_run_id", "source_text_digest")}
        if paper["abstract"].strip():
            result.append(dict(**common, citation_id="aref_" + digest([paper["paper_version_id"], paper["abstract"]]),
                               kind="abstract", text=paper["abstract"], locator=None, source_evidence_id=None))
        for ref in paper["evidence"]:
            if ref["text"].strip():
                result.append(dict(**common, citation_id=ref["excerpt_id"], kind="review_excerpt",
                                   **{k: ref[k] for k in ("text", "locator", "source_evidence_id", "recorded_content_digest")}))
        if job["inputs"].get("schema_version") == "LOCAL_EVIDENCE_SYNTHESIS_V2":
            from app.domain.journal_review_input import references as input_references
            result.extend(input_references(paper))
    return result


def quote(text: str) -> str:
    return "\n".join("> " + line for line in text.splitlines())


def assemble(job: dict[str, Any], body: Save, actor_id: str, created_at: str, version_id: str) -> dict[str, Any]:
    available = {r["citation_id"]: r for r in references(job)}
    used = sorted({ref for entry in body.entries for ref in entry.citation_ids})
    if any(ref not in available for ref in used):
        raise ValueError("journal_citation_invalid")
    citations = [available[ref] for ref in used]
    numbers = {ref: index for index, ref in enumerate(used, 1)}
    lines = ["# 근거 연결 검토 초안", "", quote(body.title), "",
             "사용자가 작성한 관찰·해석·질문입니다. 인용 연결은 원문 보존과 식별자 일치만 확인하며 문장의 타당성·함의·모순을 검증하지 않습니다.",
             "채택된 보고서가 아니며 AI 합성 또는 전문 전체 검토를 수행하지 않았습니다.", "",
             f"- 검토 초안 버전: {version_id}", f"- 고정 입력: {job['input_digest']}",
             f"- 주제 스냅샷: {job['inputs']['source_snapshot_id']}", f"- 저장 시각: {created_at}", ""]
    for index, entry in enumerate(body.entries, 1):
        lines += [f"## 문장 {index} · {KINDS[entry.kind]}", "", quote(entry.text), "",
                  "연결 인용: " + ", ".join(f"[{numbers[ref]}]" for ref in entry.citation_ids), ""]
    for index, ref in enumerate(citations, 1):
        label = {"abstract": "저장된 초록", "review_excerpt": "저장된 리뷰 발췌", "review_input_excerpt": "선택한 검토 입력 발췌"}[ref["kind"]]
        lines += [f"## 인용 [{index}] · {label}", "",
                  quote(ref["title"]), "", f"논문 버전: `{ref['paper_version_id']}`", f"근거: `{ref['citation_id']}`", "", quote(ref["text"]), "",
                  "보존된 출처·위치:", quote(json.dumps({k: v for k, v in ref.items() if k not in {"text", "title"}}, ensure_ascii=False)), ""]
    with_review_input = job["inputs"].get("schema_version") == "LOCAL_EVIDENCE_SYNTHESIS_V2"
    if with_review_input:
        from app.domain.journal_exchange import packet, review_input_lines
        lines += review_input_lines(packet(job))
    content = "\n".join(lines)
    value = dict(schema_version=SCHEMA, version_id=version_id, topic_id=job["topic_id"], job_id=job["job_id"],
                input_digest=job["input_digest"], candidate_digest=digest(job["candidate"]),
                source_snapshot_id=job["inputs"]["source_snapshot_id"], previous_id=body.expected_previous_id,
                actor_id=actor_id, created_at=created_at, title=body.title,
                entries=[entry.model_dump() for entry in body.entries], citations=citations,
                validation=dict(method="FROZEN_CITATION_BINDING_V1", binding="VERIFIED", scientific_assessment="NOT_ASSESSED"),
                markdown=content)
    # V1 storage is NFC canonical JSON. Hash and return the exact stored body;
    # hashing the pre-normalized text makes a successful save unreadable.
    if not with_review_input:
        value = json.loads(canonical_json(value))
    value["content_sha256"] = hashlib.sha256(value["markdown"].encode("utf-8")).hexdigest()
    return value
