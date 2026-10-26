from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any


DIMENSION_MAX_SCORE = 10
# Stable identifier for this deterministic rubric. Bump when the scoring changes so
# persisted quality summaries can be interpreted against the rubric that produced them.
RUBRIC_VERSION = "weekly_journal_h005_v1"
# The rubric is a fixed, provider-free heuristic scorer. This label is surfaced in the
# result so downstream consumers never mistake it for LLM/provider judgement.
EVALUATION_METHOD = "deterministic_rubric"
RUBRIC_DIMENSIONS = [
    "evidence_grounding",
    "full_text_truthfulness",
    "abstract_only_labeling",
    "provenance_honesty",
    "oauth_provider_overclaim_guard",
    "readability",
    "methodology_limitations",
    "coverage_disclosure",
    "research_direction_usefulness",
    "portable_artifact_path_hygiene",
]
DEFAULT_THRESHOLD = 72
H005_CATEGORY_MAX_SCORE = 2
H005_CATEGORIES = [
    "Structure and readability",
    "Evidence traceability",
    "Full-text honesty",
    "Citation integrity",
    "Coverage disclosure",
    "Weekly Journal readability",
    "Research gap / next topic quality",
    "Provider truthfulness",
]
CRITICAL_FAIL_REASONS = {
    "fabricated_evidence_marker",
    "fabricated_citation_marker",
    "citation_reference_unmatched",
    "full_text_overclaim",
    "all_papers_reviewed_overclaim",
    "abstract_only_described_as_full_text_reviewed",
    "full_text_available_described_as_reviewed",
    "reviewed_missing_source_text_sha256",
    "reviewed_missing_evidence_spans",
    "provider_generated_report_claim",
    "real_provider_orchestration_overclaim",
    "version_smoke_provider_synthesis_overclaim",
    "synthetic_provider_described_as_real",
    "live_provider_verified_claim",
    "production_ready_claim",
    "production_oauth_overclaim",
    "oauth_ready_claim",
    "mock_exchange_overclaim",
    "unsupported_novelty_claim",
    "untraceable_major_claim",
}


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(str(item) for item in items if str(item).strip()))


def _has_any(text: str, needles: list[str]) -> bool:
    return any(needle in text for needle in needles)


def _line_has_bad_status_label(line: str, status: str) -> bool:
    low = line.lower()
    if status not in low:
        return False
    if "검토 범위" in line or "status counts" in low or "full_text_reviewed " in low:
        return False
    if "not reviewed" in low or "not counted" in low or "초록" in line or "이번 보고서에서 본문 근거로 사용하지 않음" in line:
        return False
    if status == "abstract_only":
        return bool(re.search(r"(full[-_ ]text[_ -]?reviewed|read in full|전문\s*검토)", low))
    if status == "full_text_available":
        return bool(re.search(r"(full[-_ ]text[_ -]?reviewed|reviewed evidence|전문\s*검토)", low))
    return False


def _has_full_text_overclaim(markdown: str) -> bool:
    low = markdown.lower()
    if "all collected papers were not read in full" in low:
        return False
    patterns = [
        r"\ball\s+(collected\s+)?papers\s+were\s+(read\s+in\s+full|full[- ]text\s+(reviewed|read))",
        r"\b100%\s+full[- ]text\s+(reviewed|read)",
        r"모든\s+.*(전문|full[- ]text).*(읽|검토)",
    ]
    return any(re.search(pattern, low) for pattern in patterns)


def _contains_local_absolute_path(markdown: str) -> bool:
    patterns = [
        r"\b[A-Za-z]:\\Users\\",
        r"\b[A-Za-z]:/Users/",
        r"/home/[A-Za-z0-9_.-]+/",
        r"/Users/[A-Za-z0-9_.-]+/",
    ]
    return any(re.search(pattern, markdown) for pattern in patterns)


def _line_negates_claim(line: str) -> bool:
    low = line.lower()
    return any(
        marker in low
        for marker in [
            "not ",
            "no ",
            "without ",
            "does not",
            "do not",
            "not proven",
            "not verified",
            "아님",
            "아니",
            "않",
            "없",
            "미검증",
        ]
    )


def _forbidden_overclaim_reasons(markdown: str) -> list[str]:
    reasons: list[str] = []
    patterns = {
        "journal_grade_claim": [
            r"\bjournal[- ]grade\b",
            r"저널급\s*보장",
        ],
        "production_ready_claim": [
            r"\bproduction ready\b",
            r"프로덕션\s*준비",
        ],
        "live_provider_verified_claim": [
            r"\blive provider verified\b",
            r"라이브\s*(provider|프로바이더)\s*검증",
        ],
        "provider_generated_report_claim": [
            r"\b(gemini|codex|claude|antigravity)\s+generated\b",
            r"(gemini|codex|claude|antigravity).{0,24}(보고서|report).{0,24}(생성|generated)",
        ],
        "orchestration_ready_claim": [
            r"\borchestration ready\b",
            r"오케스트레이션\s*완료",
        ],
        "oauth_ready_claim": [
            r"\boauth ready\b",
            r"oauth\s*완료",
        ],
        "untraceable_major_claim": [
            r"\buntraceable major claim\b",
            r"\bmajor claim without evidence\b",
        ],
    }
    for line in markdown.splitlines():
        if _line_negates_claim(line):
            continue
        low = line.lower()
        for reason, regexes in patterns.items():
            if any(re.search(pattern, low) for pattern in regexes):
                reasons.append(reason)
    return _unique(reasons)


def _evidence_table_paper_ids(markdown: str) -> list[str]:
    ids: list[str] = []
    in_paper_table = False
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            in_paper_table = False
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells and cells[0].lower() == "paper_id":
            in_paper_table = True
            continue
        if not in_paper_table:
            continue
        if not cells or set(cells[0]) <= {"-", " "}:
            continue
        paper_id = cells[0].strip("` ")
        if paper_id and paper_id.lower() != "paper_id":
            ids.append(paper_id)
    return _unique(ids)


def _score_status(score: int, max_score: int, reasons: list[str]) -> str:
    if score <= max_score // 2:
        return "fail"
    if reasons:
        return "warning"
    return "pass"


def _dimension(score: int, reasons: list[str] | None = None) -> dict[str, Any]:
    normalized = max(0, min(DIMENSION_MAX_SCORE, int(score)))
    reason_list = _unique(reasons or [])
    return {
        "score": normalized,
        "max_score": DIMENSION_MAX_SCORE,
        "status": _score_status(normalized, DIMENSION_MAX_SCORE, reason_list),
        "reasons": reason_list,
    }


def _category(score: int, reasons: list[str] | None = None) -> dict[str, Any]:
    normalized = max(0, min(H005_CATEGORY_MAX_SCORE, int(score)))
    reason_list = _unique(reasons or [])
    if normalized == 0:
        status = "fail"
    elif normalized == H005_CATEGORY_MAX_SCORE and not reason_list:
        status = "pass"
    else:
        status = "warning"
    return {
        "score": normalized,
        "max_score": H005_CATEGORY_MAX_SCORE,
        "status": status,
        "reasons": reason_list,
    }


def _dimension_to_category_score(dimension: dict[str, Any]) -> int:
    score = int(dimension.get("score") or 0)
    if score >= 8:
        return 2
    if score >= 5:
        return 1
    return 0


def _critical_fail_flags(failure_reasons: list[str]) -> list[str]:
    return _unique([reason for reason in failure_reasons if reason in CRITICAL_FAIL_REASONS])


def _quality_classification(
    *,
    overall_status: str,
    h005_total_score: int,
    h005_max_score: int,
    critical_fail_flags: list[str],
    category_scores: dict[str, dict[str, Any]],
) -> str:
    if critical_fail_flags:
        return "blocked_truthfulness_issue"
    if overall_status == "fail":
        return "needs_revision"
    if h005_total_score >= h005_max_score and all(item["status"] == "pass" for item in category_scores.values()):
        return "journal_ready_local_rc"
    return "usable_with_warnings"


def _status_counts(reviews: list[dict[str, Any]]) -> Counter:
    return Counter(str(review.get("full_text_status") or "unavailable") for review in reviews)


def _reviewed_metadata_failures(reviews: list[dict[str, Any]]) -> list[str]:
    failures: list[str] = []
    for review in reviews:
        if str(review.get("full_text_status") or "") != "reviewed":
            continue
        if not str(review.get("source_text_sha256") or "").strip():
            failures.append("reviewed_missing_source_text_sha256")
        spans = review.get("evidence_spans") if isinstance(review.get("evidence_spans"), list) else []
        if not spans:
            failures.append("reviewed_missing_evidence_spans")
    return _unique(failures)


def _provider_real_count(provider_provenance: dict[str, Any] | None) -> int:
    provider_provenance = provider_provenance or {}
    return int(
        provider_provenance.get("realProviderAcceptedCount")
        or provider_provenance.get("realProviderCount")
        or provider_provenance.get("real_provider_count")
        or 0
    )


def _provider_synthetic_count(provider_provenance: dict[str, Any] | None) -> int:
    provider_provenance = provider_provenance or {}
    return int(
        provider_provenance.get("fixtureSyntheticCount")
        or provider_provenance.get("providerOrchestratedSyntheticCount")
        or provider_provenance.get("syntheticCount")
        or 0
    )


def _live_oauth_proven(oauth_proof: dict[str, Any] | None) -> bool:
    oauth_proof = oauth_proof or {}
    return bool(
        oauth_proof.get("liveConnectorProof")
        or oauth_proof.get("readOnlyConnectorSmokePassed")
        or oauth_proof.get("read_only_connector_smoke_passed")
    )


def evaluate_weekly_journal_quality(
    report_markdown: str,
    *,
    metadata: dict[str, Any] | None = None,
    reviews: list[dict[str, Any]] | None = None,
    provider_provenance: dict[str, Any] | None = None,
    oauth_proof: dict[str, Any] | None = None,
    expected_topic: str | None = None,
    threshold: int = DEFAULT_THRESHOLD,
    evaluated_at: str | None = None,
) -> dict[str, Any]:
    markdown = str(report_markdown or "")
    lower = markdown.lower()
    reviews = list(reviews or [])
    metadata = metadata or {}
    counts = _status_counts(reviews)
    reviewed_count = int(metadata.get("reviewed_count") or counts.get("reviewed", 0))
    abstract_only_count = int(metadata.get("abstract_only_count") or counts.get("abstract_only", 0))
    full_text_available_count = int(metadata.get("full_text_available_count") or counts.get("full_text_available", 0))
    total_count = int(metadata.get("total_count") or len(reviews) or reviewed_count + abstract_only_count + full_text_available_count)
    known_paper_ids = {str(review.get("paper_id") or "").strip() for review in reviews if str(review.get("paper_id") or "").strip()}
    cited_paper_ids = _evidence_table_paper_ids(markdown)
    unmatched_citation_ids = sorted([paper_id for paper_id in cited_paper_ids if known_paper_ids and paper_id not in known_paper_ids])

    evidence_table_detected = bool(
        re.search(r"\|\s*paper_id\s*\|.*full_text_status.*source_text_sha256", markdown, flags=re.IGNORECASE | re.DOTALL)
    )
    limitations_detected = _has_any(lower, ["한계와 리스크", "limitations", "methodological limitations", "한계"])
    provenance_detected = _has_any(lower, ["provider", "provenance", "source/provider", "oauth", "source basis"])
    executive_summary_detected = "executive summary" in lower
    coverage_disclosure_detected = _has_any(
        lower,
        [
            "검토 범위",
            "보고서 입력 기준",
            "full-text reviewed coverage",
            "reviewed count",
        ],
    )
    research_direction_detected = _has_any(
        lower,
        ["research gaps", "new topic candidates", "다음 주 모니터링", "follow-up direction", "recommendation"],
    )
    method_detected = _has_any(lower, ["방법론", "methodology", "method", "collection/review"])

    failure_reasons: list[str] = []
    warning_reasons: list[str] = []
    dimension_scores: dict[str, dict[str, Any]] = {}

    if not evidence_table_detected:
        failure_reasons.append("missing_evidence_table")
    if reviewed_count and "source_text_sha256" not in lower:
        failure_reasons.append("missing_source_text_sha256_reference")
    if total_count and not coverage_disclosure_detected:
        failure_reasons.append("missing_coverage_disclosure")
    if "fabricated evidence" in lower:
        failure_reasons.append("fabricated_evidence_marker")
    if "fabricated citation" in lower or "fabricated reference" in lower:
        failure_reasons.append("fabricated_citation_marker")
    if unmatched_citation_ids:
        failure_reasons.append("citation_reference_unmatched")
    if _has_full_text_overclaim(markdown) and reviewed_count < total_count:
        failure_reasons.append("full_text_overclaim")
    if reviewed_count < total_count and re.search(r"\ball\s+papers\s+(were\s+)?reviewed\b|전체\s*본문\s*검토", lower):
        failure_reasons.append("all_papers_reviewed_overclaim")
    for line in markdown.splitlines():
        if _line_has_bad_status_label(line, "abstract_only"):
            failure_reasons.append("abstract_only_described_as_full_text_reviewed")
        if _line_has_bad_status_label(line, "full_text_available"):
            failure_reasons.append("full_text_available_described_as_reviewed")
    if abstract_only_count and not (_has_any(lower, ["abstract_only", "초록만 검토", "초록 기반", "abstract-only"])):
        failure_reasons.append("abstract_only_not_labeled")
    if abstract_only_count and not limitations_detected:
        failure_reasons.append("missing_limitations_for_abstract_only")
    if full_text_available_count and "full_text_available" not in lower:
        warning_reasons.append("full_text_available_not_labeled")

    failure_reasons.extend(_reviewed_metadata_failures(reviews))

    real_provider_count = _provider_real_count(provider_provenance)
    synthetic_provider_count = _provider_synthetic_count(provider_provenance)
    if real_provider_count <= 0 and re.search(r"(real provider orchestration|gemini.*claude.*codex.*orchestration|proportional live orchestration).*complete", lower):
        failure_reasons.append("real_provider_orchestration_overclaim")
    if re.search(r"(version/help smoke|version smoke|help smoke).*(real provider synthesis|paper review generation)", lower):
        failure_reasons.append("version_smoke_provider_synthesis_overclaim")
    if synthetic_provider_count and re.search(r"(synthetic|fixture|stub).*(real provider output|external ai output)", lower):
        failure_reasons.append("synthetic_provider_described_as_real")
    if not _live_oauth_proven(oauth_proof) and re.search(r"(production\s+)?(chatgpt/codex\s+)?oauth.*complete", lower):
        failure_reasons.append("production_oauth_overclaim")
    if not _live_oauth_proven(oauth_proof) and re.search(r"mock exchange.*live connector proof", lower):
        failure_reasons.append("mock_exchange_overclaim")
    failure_reasons.extend(_forbidden_overclaim_reasons(markdown))
    if _contains_local_absolute_path(markdown):
        failure_reasons.append("local_absolute_path_leak")

    evidence_score = 0
    evidence_reasons: list[str] = []
    if evidence_table_detected:
        evidence_score += 4
    else:
        evidence_reasons.append("missing_evidence_table")
    if reviewed_count == 0 or "source_text_sha256" in lower:
        evidence_score += 2
    else:
        evidence_reasons.append("missing_source_text_sha256_reference")
    if reviewed_count == 0 or _has_any(lower, ["evidence snippets", "evidence ref", "evidence appendix", "근거"]):
        evidence_score += 2
    else:
        evidence_reasons.append("missing_evidence_snippets_or_refs")
    if "paper_id" in lower:
        evidence_score += 2
    else:
        evidence_reasons.append("missing_paper_identifier")
    dimension_scores["evidence_grounding"] = _dimension(evidence_score, evidence_reasons)

    truth_score = DIMENSION_MAX_SCORE
    truth_reasons = [reason for reason in failure_reasons if reason.startswith("reviewed_missing") or "full_text" in reason]
    truth_score -= min(8, len(truth_reasons) * 3)
    dimension_scores["full_text_truthfulness"] = _dimension(truth_score, truth_reasons)

    abstract_score = DIMENSION_MAX_SCORE
    abstract_reasons: list[str] = []
    if abstract_only_count and not _has_any(lower, ["abstract_only", "초록만 검토", "초록 기반", "abstract-only"]):
        abstract_score -= 6
        abstract_reasons.append("abstract_only_not_labeled")
    if abstract_only_count and not limitations_detected:
        abstract_score -= 4
        abstract_reasons.append("missing_limitations_for_abstract_only")
    dimension_scores["abstract_only_labeling"] = _dimension(abstract_score, abstract_reasons)

    provenance_score = 0
    provenance_reasons: list[str] = []
    if provenance_detected:
        provenance_score += 4
    else:
        provenance_reasons.append("missing_provenance_wording")
    if synthetic_provider_count == 0 or _has_any(lower, ["synthetic provider", "fixture", "test artifacts", "local fixture"]):
        provenance_score += 3
    else:
        provenance_reasons.append("synthetic_provider_not_labeled")
    if real_provider_count > 0 or _has_any(lower, ["operator-accepted real provider accepted drafts: 0", "real provider accepted count", "real provider accepted drafts: 0"]):
        provenance_score += 3
    else:
        provenance_reasons.append("real_provider_zero_not_labeled")
    dimension_scores["provenance_honesty"] = _dimension(provenance_score, provenance_reasons)

    overclaim_failures = [
        reason
        for reason in failure_reasons
        if reason
        in {
            "real_provider_orchestration_overclaim",
            "version_smoke_provider_synthesis_overclaim",
            "synthetic_provider_described_as_real",
            "production_oauth_overclaim",
            "provider_generated_report_claim",
            "live_provider_verified_claim",
            "production_ready_claim",
            "orchestration_ready_claim",
            "oauth_ready_claim",
            "mock_exchange_overclaim",
        }
    ]
    dimension_scores["oauth_provider_overclaim_guard"] = _dimension(
        DIMENSION_MAX_SCORE - min(10, len(overclaim_failures) * 4),
        overclaim_failures,
    )

    readability_score = 0
    readability_reasons: list[str] = []
    section_count = markdown.count("\n## ")
    if executive_summary_detected:
        readability_score += 3
    else:
        readability_reasons.append("missing_executive_summary")
        failure_reasons.append("missing_executive_summary")
    if section_count >= 5:
        readability_score += 3
    else:
        readability_reasons.append("too_few_sections")
    if len(max(markdown.split("\n\n"), key=len, default="")) < 3000:
        readability_score += 2
    else:
        readability_reasons.append("excessive_wall_text")
    if _has_any(lower, ["핵심", "takeaway", "monitoring", "모니터링"]):
        readability_score += 2
    else:
        readability_reasons.append("missing_concise_takeaways")
    dimension_scores["readability"] = _dimension(readability_score, readability_reasons)

    methodology_score = 0
    methodology_reasons: list[str] = []
    if method_detected:
        methodology_score += 3
    else:
        methodology_reasons.append("missing_methodology")
    if limitations_detected:
        methodology_score += 3
    else:
        methodology_reasons.append("missing_limitations")
    if abstract_only_count == 0 or _has_any(lower, ["abstract_only", "초록만 검토", "초록 기반"]):
        methodology_score += 2
    else:
        methodology_reasons.append("abstract_only_limitation_missing")
    if _has_any(lower, ["provider/oAuth".lower(), "oauth", "provider"]):
        methodology_score += 2
    else:
        methodology_reasons.append("provider_oauth_limitations_missing")
    dimension_scores["methodology_limitations"] = _dimension(methodology_score, methodology_reasons)

    coverage_score = 0
    coverage_reasons: list[str] = []
    if coverage_disclosure_detected:
        coverage_score += 5
    else:
        coverage_reasons.append("missing_coverage_disclosure")
    if total_count and _has_any(lower, ["full_text_reviewed", "reviewed"]):
        coverage_score += 2
    else:
        coverage_reasons.append("missing_reviewed_count")
    if abstract_only_count == 0 or "abstract_only" in lower:
        coverage_score += 1
    else:
        coverage_reasons.append("missing_abstract_only_count")
    if full_text_available_count == 0 or "full_text_available" in lower:
        coverage_score += 1
    else:
        coverage_reasons.append("missing_full_text_available_count")
    if _has_any(lower, ["not read in full", "not counted as reviewed", "본문 근거로 사용하지 않음", "full-text를 검토했다고 가정하지 않습니다"]):
        coverage_score += 1
    else:
        coverage_reasons.append("missing_coverage_limitation_note")
    dimension_scores["coverage_disclosure"] = _dimension(coverage_score, coverage_reasons)

    research_score = 0
    research_reasons: list[str] = []
    if research_direction_detected:
        research_score += 5
    else:
        research_reasons.append("missing_research_direction")
    if _has_any(lower, ["evidence-linked", "근거", "source_text_sha256", "supporting paper"]):
        research_score += 3
    else:
        research_reasons.append("research_direction_not_evidence_linked")
    if not re.search(r"(breakthrough|novel|state[- ]of[- ]the[- ]art).*without evidence", lower):
        research_score += 2
    else:
        research_reasons.append("unsupported_novelty_claim")
        failure_reasons.append("unsupported_novelty_claim")
    dimension_scores["research_direction_usefulness"] = _dimension(research_score, research_reasons)

    path_score = DIMENSION_MAX_SCORE
    path_reasons: list[str] = []
    if _contains_local_absolute_path(markdown):
        path_score = 0
        path_reasons.append("local_absolute_path_leak")
    elif re.search(r"\b(run:|storage:|project:)", markdown):
        path_score = DIMENSION_MAX_SCORE
    elif "full_text_artifact_path" in lower:
        path_score = 7
        path_reasons.append("artifact_refs_not_portable_or_absent")
    dimension_scores["portable_artifact_path_hygiene"] = _dimension(path_score, path_reasons)

    failure_reasons = _unique(failure_reasons)
    warning_reasons = _unique([*warning_reasons, *[reason for item in dimension_scores.values() for reason in item["reasons"]]])
    overall_score = sum(int(item["score"]) for item in dimension_scores.values())
    max_score = len(RUBRIC_DIMENSIONS) * DIMENSION_MAX_SCORE
    if failure_reasons:
        overall_status = "fail"
    elif overall_score < int(threshold):
        overall_status = "fail"
        failure_reasons.append("quality_threshold_not_met")
    elif warning_reasons and overall_score < max_score:
        overall_status = "warning"
    else:
        overall_status = "pass"
    critical_flags = _critical_fail_flags(_unique(failure_reasons))
    category_scores = {
        "Structure and readability": _category(
            min(
                _dimension_to_category_score(dimension_scores["readability"]),
                _dimension_to_category_score(dimension_scores["methodology_limitations"]),
            ),
            [
                *dimension_scores["readability"]["reasons"],
                *dimension_scores["methodology_limitations"]["reasons"],
            ],
        ),
        "Evidence traceability": _category(
            0 if {"missing_evidence_table", "reviewed_missing_evidence_spans"} & set(failure_reasons) else _dimension_to_category_score(dimension_scores["evidence_grounding"]),
            dimension_scores["evidence_grounding"]["reasons"],
        ),
        "Full-text honesty": _category(
            0
            if {
                "full_text_overclaim",
                "all_papers_reviewed_overclaim",
                "abstract_only_described_as_full_text_reviewed",
                "full_text_available_described_as_reviewed",
                "reviewed_missing_source_text_sha256",
                "reviewed_missing_evidence_spans",
            }
            & set(failure_reasons)
            else min(
                _dimension_to_category_score(dimension_scores["full_text_truthfulness"]),
                _dimension_to_category_score(dimension_scores["abstract_only_labeling"]),
            ),
            [
                *dimension_scores["full_text_truthfulness"]["reasons"],
                *dimension_scores["abstract_only_labeling"]["reasons"],
            ],
        ),
        "Citation integrity": _category(
            0
            if {"fabricated_citation_marker", "citation_reference_unmatched"} & set(failure_reasons)
            else 2
            if cited_paper_ids and not unmatched_citation_ids
            else 1,
            [f"unmatched:{paper_id}" for paper_id in unmatched_citation_ids],
        ),
        "Coverage disclosure": _category(
            0 if "missing_coverage_disclosure" in failure_reasons else _dimension_to_category_score(dimension_scores["coverage_disclosure"]),
            dimension_scores["coverage_disclosure"]["reasons"],
        ),
        "Weekly Journal readability": _category(
            _dimension_to_category_score(dimension_scores["readability"]),
            dimension_scores["readability"]["reasons"],
        ),
        "Research gap / next topic quality": _category(
            0 if "unsupported_novelty_claim" in failure_reasons else _dimension_to_category_score(dimension_scores["research_direction_usefulness"]),
            dimension_scores["research_direction_usefulness"]["reasons"],
        ),
        "Provider truthfulness": _category(
            0
            if set(overclaim_failures) & set(failure_reasons)
            else min(
                _dimension_to_category_score(dimension_scores["oauth_provider_overclaim_guard"]),
                _dimension_to_category_score(dimension_scores["provenance_honesty"]),
            ),
            [
                *dimension_scores["oauth_provider_overclaim_guard"]["reasons"],
                *dimension_scores["provenance_honesty"]["reasons"],
            ],
        ),
    }
    h005_total_score = sum(int(item["score"]) for item in category_scores.values())
    h005_max_score = len(H005_CATEGORIES) * H005_CATEGORY_MAX_SCORE
    report_quality_classification = _quality_classification(
        overall_status=overall_status,
        h005_total_score=h005_total_score,
        h005_max_score=h005_max_score,
        critical_fail_flags=critical_flags,
        category_scores=category_scores,
    )

    return {
        "overall_status": overall_status,
        "overall_score": overall_score,
        "max_score": max_score,
        "threshold": int(threshold),
        "dimension_scores": dimension_scores,
        "category_scores": category_scores,
        "h005_total_score": h005_total_score,
        "h005_max_score": h005_max_score,
        "critical_fail_flags": critical_flags,
        "report_quality_classification": report_quality_classification,
        "failure_reasons": _unique(failure_reasons),
        "warning_reasons": _unique(warning_reasons),
        "checked_at": evaluated_at or datetime.now(timezone.utc).isoformat(),
        "rubric_version": RUBRIC_VERSION,
        "evaluation_method": EVALUATION_METHOD,
        "report_kind": "weekly_journal",
        "topic": expected_topic or metadata.get("topic"),
        "reviewed_count": reviewed_count,
        "abstract_only_count": abstract_only_count,
        "full_text_available_count": full_text_available_count,
        "coverage_disclosure_detected": coverage_disclosure_detected,
        "cited_paper_ids": cited_paper_ids,
        "known_paper_ids": sorted(known_paper_ids),
        "unmatched_citation_ids": unmatched_citation_ids,
        "evidence_table_detected": evidence_table_detected,
        "limitations_detected": limitations_detected,
        "provenance_detected": provenance_detected,
        "overclaim_detected": bool(
            {
                "full_text_overclaim",
                "real_provider_orchestration_overclaim",
                "production_oauth_overclaim",
                "mock_exchange_overclaim",
                "synthetic_provider_described_as_real",
            }
            & set(failure_reasons)
        ),
    }
