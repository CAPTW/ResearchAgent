"""Bounded extractive drafts from frozen local records; no model or source IO."""
from dataclasses import asdict
import hashlib
import math
from typing import Any

from app.domain.temporal_memory import Member, canonical_json, digest, normalized_member

SCHEMA = "LOCAL_EVIDENCE_SYNTHESIS_V1"
REVIEW_SCHEMA = "LOCAL_EVIDENCE_SYNTHESIS_V2"
PIPELINE = "LOCAL_EXTRACTIVE_V1"
MAX_PAPERS = 100
MAX_INPUT_BYTES = 2_000_000
SHARD_SIZE = 5
PLAN = {"pipeline_version": PIPELINE, "shard_size": SHARD_SIZE, "provider_mode": "disabled",
        "sections": ["coverage", "paper_excerpts", "limitations"], "contradiction_assessment": "NOT_ASSESSED"}


def freeze(topic_id: str, profile: dict[str, Any], catalog: list[dict[str, Any]], members: list[Member],
           selection: list[dict[str, Any]], snapshot_id: str,
           review_inputs: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    selected = {item["paper_version_id"]: item["metadata_digest"] for item in selection}
    if not 1 <= len(selection) <= MAX_PAPERS or len(selected) != len(selection):
        raise ValueError("synthesis_selection_invalid")
    indexed = {item["paperVersionId"]: item for item in catalog}
    membership = {item.paper_version_id: item for item in members}
    papers = []
    for identity, expected in sorted(selected.items()):
        item = indexed.get(identity)
        if item is None or item["metadataDigest"] != expected:
            raise ValueError("synthesis_selection_changed")
        review = item["_selectedReview"] or {}
        evidence = []
        for ordinal, span in enumerate(review.get("evidence_spans") or []):
            if not isinstance(span, dict):
                raise ValueError("synthesis_source_invalid")
            frozen = dict(source_evidence_id=span.get("evidence_span_id") or span.get("span_id") or span.get("evidence_id"),
                          locator=span.get("locator"), text=str(span.get("text") or span.get("snippet") or ""),
                          recorded_content_digest=span.get("content_sha256") or span.get("text_sha256") or span.get("content_digest"))
            frozen["excerpt_id"] = "fref_" + digest([identity, ordinal, frozen])
            evidence.append(frozen)
        papers.append(dict(paper_version_id=identity, canonical_paper_id=item["canonicalPaperId"],
                           metadata_digest=item["metadataDigest"], title=item["title"], authors=item["authors"],
                           abstract=str(item["_raw"].get("abstract") or ""), source_identity=item["sourceIdentity"],
                           review_state=item["review"]["state"], rights=dict(state=item["accessState"], label=item["accessLabel"]),
                           evidence=evidence, review_source_run_id=review.get("run_id"),
                           source_text_digest=review.get("source_text_sha256"),
                           member=asdict(normalized_member(membership[identity]))))
        if review_inputs and identity in review_inputs:
            papers[-1]["review_input"] = review_inputs[identity]
    value = dict(schema_version=REVIEW_SCHEMA if review_inputs else SCHEMA, topic_id=topic_id, topic_label=str(profile.get("title") or profile.get("topic") or topic_id),
                 source_snapshot_id=snapshot_id, papers=papers, plan=dict(PLAN))
    validate_inputs(value)
    if len(encoded(value, value).encode("utf-8")) > MAX_INPUT_BYTES:
        raise ValueError("synthesis_input_too_large")
    return value


def identity(inputs: dict[str, Any]) -> str:
    validate_inputs(inputs)
    return "syn_" + digest(inputs)


def encoded(value: Any, inputs: dict[str, Any]) -> str:
    if inputs.get("schema_version") == REVIEW_SCHEMA:
        from app.domain.journal_exchange import preserved_json
        return preserved_json(value)
    return canonical_json(value)


def checkpoint_digest(value: Any, inputs: dict[str, Any]) -> str:
    # V1 retains its canonical checkpoint identities. New V2 checkpoints also
    # detect Unicode normalization of the preserved receipt/excerpt/result.
    return hashlib.sha256(encoded(value, inputs).encode("utf-8")).hexdigest()


def validate_inputs(inputs: dict[str, Any]) -> None:
    """V2 preserves original receipt codepoints alongside legacy identity hashes."""
    from app.domain.journal_review_input import validate_frozen
    has_review = False
    for paper in inputs["papers"]:
        if "review_input" in paper:
            has_review = True
            try:
                frozen = validate_frozen(paper["review_input"])
            except ValueError:
                raise ValueError("synthesis_integrity_invalid") from None
            if frozen["receipt"]["topic_id"] != inputs["topic_id"]:
                raise ValueError("synthesis_integrity_invalid")
    if (has_review and inputs.get("schema_version") != REVIEW_SCHEMA
            or inputs.get("schema_version") == REVIEW_SCHEMA and not has_review):
        raise ValueError("synthesis_integrity_invalid")


def total_steps(inputs: dict[str, Any]) -> int:
    return math.ceil(len(inputs["papers"]) / SHARD_SIZE) + 2


def _quote(value: str) -> str:
    return "\n".join("> " + line for line in value.splitlines())


def candidate(inputs: dict[str, Any], extracts: list[dict[str, Any]]) -> dict[str, Any]:
    papers = inputs["papers"]
    coverage = dict(selected_papers=len(papers), papers_with_abstract=sum(bool(p["abstract"].strip()) for p in papers),
                    papers_with_stored_excerpt=sum(any(e["text"].strip() for e in p["evidence"]) for p in papers),
                    recorded_full_text_reviewed=sum(p["review_state"] == "full_text_reviewed" for p in papers),
                    contradiction_assessment="NOT_ASSESSED", contradiction_count=None)
    lines = ["# 선택 문헌 근거 초안", "", _quote(inputs["topic_label"]), "",
             "## 범위와 근거", "", "로컬에 저장된 초록과 리뷰 발췌를 정리한 초안입니다. 새로운 연구 해석이나 전문 재검토를 수행하지 않았습니다.",
             f"- 선택 논문: {coverage['selected_papers']}", f"- 초록 보존: {coverage['papers_with_abstract']}",
             f"- 리뷰 발췌 보존: {coverage['papers_with_stored_excerpt']}",
             f"- 기존 전문 검토 기록: {coverage['recorded_full_text_reviewed']}", "- 모순 평가: 수행하지 않음", ""]
    references = []
    for number, paper in enumerate(extracts, 1):
        lines += [f"## 문헌 {number}", "", _quote(paper["title"]), "", f"논문 버전: `{paper['paper_version_id']}`", "",
                  "### 저장된 초록", "", _quote(paper["abstract"]) if paper["abstract"].strip() else "초록이 보존되지 않았습니다.", "",
                  "### 저장된 리뷰 발췌", ""]
        if not paper["evidence"]:
            lines += ["리뷰 발췌가 보존되지 않았습니다.", ""]
        for excerpt in paper["evidence"]:
            references.append(dict(paper_version_id=paper["paper_version_id"], **excerpt))
            lines += [f"근거: `{excerpt['excerpt_id']}`", "", _quote(excerpt["text"]) if excerpt["text"].strip() else "위치 정보만 있고 발췌문은 없습니다.", ""]
        if paper.get("review_input"):
            from app.domain import journal_review_input
            context = journal_review_input.attachment(paper)
            lines += ["### 선택한 검토 입력 발췌", "",
                      "과거 검토에 제공한 입력입니다. 검토 결과는 별도의 해석이며 새로운 모델 실행이나 전문 검토 완료를 뜻하지 않습니다.", ""]
            for ref in journal_review_input.references(paper):
                references.append(dict(**ref, excerpt_id=ref["citation_id"]))
                lines += [f"근거: `{ref['citation_id']}`", "", _quote(ref["text"]), ""]
            if context:
                lines += ["보존한 검토 기록과 해석:", "", _quote(encoded(context, inputs)), ""]
    lines += ["## 한계와 다음 검토", "", "보존된 기록의 출처와 인용 맥락을 확인해야 합니다. 전문 파일 전체는 이 입력에 포함되지 않습니다.",
              "논문 간 합의·모순·영향력·신규성을 평가하지 않았습니다. 이 초안은 완료 보고서나 검증된 연구 결론이 아닙니다.", ""]
    return dict(schema_version=inputs["schema_version"], mode="LOCAL_EXTRACTIVE_DRAFT", input_digest=digest(inputs),
                markdown="\n".join(lines), coverage=coverage, references=references)


def step(inputs: dict[str, Any], previous: list[dict[str, Any]]) -> dict[str, Any]:
    if inputs.get("schema_version") not in {SCHEMA, REVIEW_SCHEMA} or inputs.get("plan") != PLAN:
        raise ValueError("synthesis_pipeline_unavailable")
    validate_inputs(inputs)
    index = len(previous)
    shards = total_steps(inputs) - 2
    if index < shards:
        return dict(kind="EXTRACT", papers=inputs["papers"][index * SHARD_SIZE:(index + 1) * SHARD_SIZE])
    expected = candidate(inputs, [p for part in previous[:shards] for p in part["papers"]])
    if index == shards:
        return dict(kind="ASSEMBLE", candidate=expected)
    if index == shards + 1:
        # Both schema and evidence bindings are exact for this deterministic
        # renderer. This is not a scientific truth or entailment validator.
        if previous[-1] != dict(kind="ASSEMBLE", candidate=candidate(inputs, inputs["papers"])):
            raise ValueError("synthesis_candidate_invalid")
        return dict(kind="VALIDATE", method="FROZEN_REFERENCE_CHECK_V1", valid=True,
                    candidate_digest=digest(expected), input_digest=digest(inputs))
    raise ValueError("synthesis_checkpoint_invalid")
