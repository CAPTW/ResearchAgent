"""Deterministic, corpus-bounded research-gap analysis for Prototype v0.6.

The records in this module are projections over the existing immutable topic
snapshot, run, review, evidence, claim, and report records.  They live inside
the prototype workspace authority document; no evidence or report content is
copied into another repository and no external service is consulted.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any

from app.repositories.files import iso_now_utc
from app.services import prototype_real_corpus

PROFILE_METHOD = "DETERMINISTIC_STRUCTURED_REVIEW_AND_EVIDENCE_PROFILE_V1"
PROFILE_VERSION = "study_profile_v1"
MATRIX_VERSION = "evidence_matrix_v1"
GAP_RULE_VERSION = "research_gap_rules_v1"
SCORING_POLICY = "research_recommendation_scoring_v1"
COLLISION_POLICY = "current_corpus_collision_v1"
SCOPE_QUALIFIER = "WITHIN_CURRENT_ACCEPTED_CORPUS"
GLOBAL_NOVELTY_STATUS = "NOT_ASSESSED_OUTSIDE_CURRENT_CORPUS"
LOCAL_ACTOR = "LOCAL_PROTOTYPE_USER"

RESEARCH_LIST_KEYS = (
    "analysis_scopes",
    "study_profiles",
    "evidence_matrices",
    "gap_analyses",
    "research_gaps",
    "research_recommendations",
    "recommendation_decisions",
    "recommendation_origins",
    "gap_comparisons",
)

DESIGN_TYPES = {
    "systematic_review", "meta_analysis", "randomized_trial", "controlled_trial",
    "cohort", "case_control", "cross_sectional", "qualitative", "mixed_methods",
    "technical_validation", "protocol", "commentary", "unknown",
}
EVIDENCE_DIRECTIONS = {"positive", "negative", "null", "mixed", "uncertain", "not_applicable", "unknown"}
EVIDENCE_STRENGTHS = {"high", "moderate", "low", "insufficient", "not_assessed"}
FOLLOW_UP_BUCKETS = {"immediate", "short_term", "medium_term", "long_term", "mixed", "not_reported", "not_applicable"}
LIMITATION_CODES = {
    "small_sample", "short_follow_up", "heterogeneous_methods", "heterogeneous_population",
    "heterogeneous_outcomes", "limited_control_group", "limited_blinding", "selection_bias",
    "publication_bias", "limited_external_validity", "measurement_limitations",
    "insufficient_replication", "technology_access_constraint", "implementation_constraint",
    "cost_constraint", "safety_or_adverse_event_uncertainty", "insufficient_reporting",
    "other_bounded", "unknown",
}
GAP_TYPES = {
    "recurring_limitation", "underrepresented_population", "underrepresented_context",
    "methodological_gap", "measurement_gap", "longitudinal_gap", "replication_gap",
    "evidence_strength_gap", "conflicting_findings", "coverage_loss",
    "new_unreviewed_evidence", "data_availability_gap",
}
RECOMMENDATION_TYPES = {
    "research_question_only", "hypothesis_candidate", "replication_study",
    "comparative_study", "longitudinal_study", "measurement_validation",
    "implementation_study", "systematic_update", "data_collection_study",
}
DECISION_STATUSES = {"accepted", "held", "rejected"}
DECISION_REASONS = {
    "evidence_basis_sufficient_for_follow_up", "high_practical_relevance",
    "clear_falsifiable_question", "needs_more_evidence", "scope_too_broad",
    "feasibility_uncertain", "close_current_corpus_collision",
    "insufficient_measurement_definition", "low_significance_within_current_scope",
    "manual_owner_decision",
}
FORBIDDEN_GLOBAL_CLAIMS = (
    "no research exists", "never been studied", "globally novel", "global novelty",
)

DEFAULT_SCOPE = {
    "target_population_facets": [
        "rehabilitation_participants", "clinical_learners", "healthcare_trainees",
    ],
    "target_context_facets": ["rehabilitation", "telerehabilitation", "clinical_training"],
    "target_intervention_facets": ["virtual_reality", "augmented_reality", "immersive_systems"],
    "target_outcome_facets": [
        "functional_outcome", "engagement", "learning_training_outcome", "adherence",
        "safety", "implementation_feasibility",
    ],
    "target_method_facets": [
        "comparative_evaluation", "longitudinal_evaluation", "controlled_evaluation",
        "implementation_evaluation",
    ],
    "minimum_independent_papers": 2,
    "minimum_evidence_spans": 2,
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_SAMPLE_RE = re.compile(r"\b(?:n\s*=\s*|sample(?:\s+size)?\s+(?:of\s+)?)(\d{1,6})\b", re.I)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _record_digest(record: dict[str, Any], field: str) -> str:
    return _digest({key: value for key, value in record.items() if key != field})


def _semantic_record(record: dict[str, Any], *, omit: set[str] | None = None) -> dict[str, Any]:
    ignored = {"created_at", "started_at", "completed_at"} | (omit or set())
    return {key: value for key, value in record.items() if key not in ignored}


def ensure_state_shape(state: dict[str, Any]) -> dict[str, Any]:
    for key in RESEARCH_LIST_KEYS:
        if key not in state:
            state[key] = []
    return state


def _unique(rows: list[dict[str, Any]], field: str, code: str) -> None:
    values = [str(row.get(field) or "") for row in rows]
    if any(not value for value in values) or len(values) != len(set(values)):
        _error(code)


def _error(code: str, *, status_code: int = 409) -> None:
    from app.services.prototype_workspace import PrototypeWorkspaceError

    raise PrototypeWorkspaceError(code, status_code=status_code)


def validate_state(state: dict[str, Any]) -> None:
    for key in RESEARCH_LIST_KEYS:
        if not isinstance(state.get(key), list):
            _error("research_workspace_schema_invalid")
    identities = {
        "analysis_scopes": "scope_id", "study_profiles": "study_profile_id",
        "evidence_matrices": "matrix_id", "gap_analyses": "analysis_id",
        "research_gaps": "gap_id", "research_recommendations": "recommendation_id",
        "recommendation_decisions": "decision_event_id", "recommendation_origins": "origin_id",
        "gap_comparisons": "comparison_id",
    }
    for key, field in identities.items():
        rows = state[key]
        if rows:
            _unique(rows, field, "research_workspace_identity_invalid")
    digest_fields = {
        "gap_analyses": "analysis_digest",
        "research_gaps": "gap_digest", "research_recommendations": "recommendation_digest",
        "recommendation_decisions": "decision_digest", "recommendation_origins": "origin_digest",
        "gap_comparisons": "comparison_digest",
    }
    for key, digest_field in digest_fields.items():
        for row in state[key]:
            if row.get(digest_field) != _record_digest(row, digest_field):
                _error("research_workspace_digest_mismatch")
    for key, digest_field in (
        ("analysis_scopes", "scope_digest"),
        ("study_profiles", "profile_digest"),
        ("evidence_matrices", "matrix_digest"),
    ):
        for row in state[key]:
            if row.get(digest_field) != _digest(_semantic_record(row, omit={digest_field})):
                _error("research_workspace_digest_mismatch")
    topic_ids = {str(row.get("topic_id")) for row in state["topics"]}
    snapshot_ids = {str(row.get("snapshot_id")) for row in state["snapshots"]}
    analysis_ids = {str(row.get("analysis_id")) for row in state["gap_analyses"]}
    gap_ids = {str(row.get("gap_id")) for row in state["research_gaps"]}
    recommendation_ids = {str(row.get("recommendation_id")) for row in state["research_recommendations"]}
    for row in state["gap_analyses"]:
        if row.get("topic_id") not in topic_ids or row.get("snapshot_id") not in snapshot_ids:
            _error("research_analysis_boundary_invalid")
    for row in state["research_gaps"]:
        if row.get("analysis_id") not in analysis_ids or row.get("gap_type") not in GAP_TYPES:
            _error("research_gap_boundary_invalid")
        if row.get("scope_qualifier") != SCOPE_QUALIFIER:
            _error("research_gap_scope_invalid")
        statement = str(row.get("bounded_statement") or "").lower()
        if any(text in statement for text in FORBIDDEN_GLOBAL_CLAIMS):
            _error("research_gap_global_claim_rejected")
        if not (row.get("evidence_ids") or row.get("supporting_profile_ids") or row.get("change_event_ids")):
            _error("research_gap_support_required")
    for row in state["research_recommendations"]:
        if row.get("analysis_id") not in analysis_ids:
            _error("research_recommendation_analysis_invalid")
        if row.get("recommendation_type") not in RECOMMENDATION_TYPES:
            _error("research_recommendation_type_invalid")
        if row.get("global_novelty_status") != GLOBAL_NOVELTY_STATUS:
            _error("research_recommendation_novelty_invalid")
        if not row.get("supporting_gap_ids") or set(row["supporting_gap_ids"]) - gap_ids:
            _error("research_recommendation_gap_invalid")
        expected = round(
            0.15 * int(row["corpus_relative_novelty_score"])
            + 0.20 * int(row["feasibility_score"])
            + 0.25 * int(row["significance_score"])
            + 0.20 * int(row["falsifiability_score"])
            + 0.10 * int(row["evidence_readiness_score"])
            + 0.10 * int(row["timeliness_score"])
        )
        if int(row.get("overall_score") or -1) != expected:
            _error("research_recommendation_score_invalid")
    for row in state["recommendation_decisions"]:
        if row.get("recommendation_id") not in recommendation_ids:
            _error("research_decision_recommendation_invalid")
        if row.get("requested_status") not in DECISION_STATUSES:
            _error("research_decision_status_invalid")
    for row in state["recommendation_origins"]:
        if row.get("recommendation_id") not in recommendation_ids or row.get("new_topic_id") not in topic_ids:
            _error("research_origin_boundary_invalid")


def _scope_record(topic_id: str, created_at: str) -> dict[str, Any]:
    basis = {"topic_id": topic_id, "scope_version": 1, **DEFAULT_SCOPE}
    scope_id = f"scope-{_digest(basis)[:24]}"
    record = {"scope_id": scope_id, "topic_id": topic_id, "scope_version": 1, **deepcopy(DEFAULT_SCOPE), "created_at": created_at}
    record["scope_digest"] = _digest(_semantic_record(record, omit={"scope_digest"}))
    return record


def _find_or_create_scope(state: dict[str, Any], topic_id: str, created_at: str) -> dict[str, Any]:
    expected = _scope_record(topic_id, created_at)
    existing = next((row for row in state["analysis_scopes"] if row.get("scope_id") == expected["scope_id"]), None)
    if existing:
        return existing
    state["analysis_scopes"].append(expected)
    return expected


def _text_rows(evidence: list[dict[str, Any]], paper_id: str) -> list[dict[str, Any]]:
    return [row for row in evidence if str(row.get("paper_id") or "") == paper_id and not row.get("stale")]


def _tags(text: str, mapping: dict[str, tuple[str, ...]]) -> list[str]:
    return sorted(name for name, needles in mapping.items() if any(needle in text for needle in needles))


def _design_type(text: str) -> str:
    ordered = (
        ("systematic_review", ("systematic review",)), ("meta_analysis", ("meta-analysis", "meta analysis")),
        ("randomized_trial", ("randomized trial", "randomised trial")),
        ("controlled_trial", ("controlled trial",)), ("case_control", ("case-control", "case control")),
        ("cross_sectional", ("cross-sectional", "cross sectional")), ("cohort", ("cohort",)),
        ("mixed_methods", ("mixed-method", "mixed method")), ("qualitative", ("qualitative",)),
        ("technical_validation", ("technical validation",)), ("protocol", ("study protocol",)),
        ("commentary", ("commentary",)),
    )
    return next((name for name, needles in ordered if any(value in text for value in needles)), "unknown")


def _limitation_codes(text: str) -> list[str]:
    mapping = {
        "small_sample": ("small sample", "limited sample"),
        "short_follow_up": ("short follow-up", "short follow up", "longer-term outcomes", "long-term outcomes remain", "limited follow-up"),
        "heterogeneous_methods": ("heterogeneous method",), "heterogeneous_population": ("heterogeneous population",),
        "heterogeneous_outcomes": ("heterogeneous outcome",), "limited_control_group": ("limited control group", "no control group"),
        "limited_blinding": ("limited blinding", "not blinded"), "selection_bias": ("selection bias",),
        "publication_bias": ("publication bias",), "limited_external_validity": ("may not generalize", "limited generalizability", "external validity"),
        "measurement_limitations": ("measurement limitation", "inconsistent measure",),
        "insufficient_replication": ("requires replication", "not replicated"),
        "technology_access_constraint": ("technology access", "device access"),
        "implementation_constraint": ("implementation constraint", "implementation barrier"),
        "cost_constraint": ("cost constraint", "cost barrier"),
        "safety_or_adverse_event_uncertainty": ("adverse event uncertainty", "safety remains uncertain"),
        "insufficient_reporting": ("insufficient reporting", "not reported"),
    }
    return _tags(text, mapping)


def _profile(article: dict[str, Any], rows: list[dict[str, Any]], claims: list[dict[str, Any]], context: dict[str, Any], created_at: str) -> dict[str, Any]:
    paper_id = str(article["pmcid"])
    all_text = " ".join(str(row.get("normalized_quote") or "").lower() for row in rows)
    finding_text = " ".join(str(row.get("normalized_quote") or "").lower() for row in rows if row.get("claim_category") == "principal_finding")
    population_tags = _tags(all_text, {
        "rehabilitation_participants": ("rehabilitation participants", "rehabilitation patients", "adult participants"),
        "clinical_learners": ("clinical learners", "medical students", "nursing students"),
        "healthcare_trainees": ("healthcare trainees", "clinical trainees", "resident trainees"),
    })
    context_tags = _tags(all_text, {
        "rehabilitation": ("rehabilitation",), "telerehabilitation": ("telerehabilitation", "remote rehabilitation"),
        "clinical_training": ("clinical training",),
    })
    intervention_tags = _tags(all_text, {
        "virtual_reality": ("virtual reality", " vr "), "augmented_reality": ("augmented reality", " ar "),
        "immersive_systems": ("immersive system", "immersive technolog"),
    })
    comparator_tags = _tags(all_text, {
        "control_group": ("control group",), "usual_care": ("usual care",), "baseline": ("compared with baseline", "baseline comparison"),
    })
    outcome_tags = _tags(all_text, {
        "functional_outcome": ("functional outcome",), "engagement": ("engagement",),
        "learning_training_outcome": ("learning outcome", "training outcome", "task performance"),
        "adherence": ("adherence",), "safety": ("safety", "adverse event"),
        "implementation_feasibility": ("implementation feasibility", "feasibility"),
    })
    method_tags = _tags(all_text, {
        "comparative_evaluation": ("comparative evaluation", "comparison context"),
        "longitudinal_evaluation": ("longitudinal evaluation",),
        "controlled_evaluation": ("controlled evaluation", "control group", "controlled trial"),
        "implementation_evaluation": ("implementation evaluation",),
    })
    directions: set[str] = set()
    if any(word in finding_text for word in ("improved", "increase", "better", "benefit")):
        directions.add("positive")
    if any(word in finding_text for word in ("worse", "decrease", "harm")):
        directions.add("negative")
    if any(word in finding_text for word in ("no difference", "null result", "did not improve")):
        directions.add("null")
    evidence_direction = "mixed" if len(directions) > 1 else (next(iter(directions)) if directions else "unknown")
    strengths = {str(row.get("confidence_strength") or "not_assessed") for row in rows}
    strengths &= EVIDENCE_STRENGTHS
    strength_order = {"not_assessed": 0, "insufficient": 1, "low": 2, "moderate": 3, "high": 4}
    evidence_strength = min(strengths, key=lambda value: strength_order[value]) if strengths else "not_assessed"
    follow_up = "not_reported"
    if "long-term follow-up" in all_text or "long term follow-up" in all_text:
        follow_up = "long_term"
    elif "medium-term follow-up" in all_text:
        follow_up = "medium_term"
    elif "short-term follow-up" in all_text:
        follow_up = "short_term"
    elif "immediate outcome" in all_text:
        follow_up = "immediate"
    sample_match = _SAMPLE_RE.search(all_text)
    sample_size = int(sample_match.group(1)) if sample_match else None
    evidence_ids = sorted(str(row["evidence_id"]) for row in rows)
    claim_ids = sorted(str(row["claim_id"]) for row in claims if paper_id in {str(value) for value in row.get("paper_ids") or []})
    unknown_fields = sorted(name for name, value in {
        "design_type": _design_type(all_text), "population_tags": population_tags,
        "comparator_tags": comparator_tags, "follow_up_bucket": follow_up,
        "sample_size_value": sample_size,
    }.items() if value in (None, [], "unknown", "not_reported"))
    basis = {
        "topic_id": context["topic_id"], "snapshot_id": context["snapshot_id"], "run_id": context["run_id"],
        "paper_id": paper_id, "source_content_digest": article.get("content_digest"), "profile_version": PROFILE_VERSION,
    }
    profile = {
        "study_profile_id": f"profile-{_digest(basis)[:28]}", **basis,
        "paper_version_id": f"{paper_id}:{str(article.get('content_digest') or '')[:16]}",
        "review_id": article.get("review_id"), "extraction_method": PROFILE_METHOD,
        "design_type": _design_type(all_text), "population_tags": population_tags,
        "context_tags": context_tags, "intervention_tags": intervention_tags,
        "comparator_tags": comparator_tags, "outcome_tags": outcome_tags, "method_tags": method_tags,
        "follow_up_bucket": follow_up, "evidence_direction": evidence_direction,
        "evidence_strength": evidence_strength, "limitation_codes": _limitation_codes(all_text),
        "data_availability_codes": _tags(all_text, {"explicit_unavailable": ("data unavailable", "data are not available"), "restricted": ("restricted data",)}),
        "sample_size_value": sample_size, "sample_size_known": sample_size is not None,
        "coverage_section_count": int(article.get("usable_section_count") or 0),
        "coverage_paragraph_count": int(article.get("usable_paragraph_count") or 0),
        "evidence_ids": evidence_ids, "claim_ids": claim_ids, "unknown_fields": unknown_fields,
        "created_at": created_at, "stale": False,
    }
    profile["profile_digest"] = _digest(_semantic_record(profile, omit={"profile_digest"}))
    return profile


MATRIX_COLUMNS = (
    "design_type", "population_tags", "context_tags", "intervention_tags", "comparator_tags",
    "outcome_tags", "method_tags", "follow_up_bucket", "evidence_direction", "evidence_strength",
    "limitation_codes", "data_availability_codes", "sample_size_value",
)


def _matrix(profiles: list[dict[str, Any]], context: dict[str, Any], created_at: str) -> dict[str, Any]:
    rows = []
    for profile in sorted(profiles, key=lambda row: str(row["paper_id"])):
        cells = {}
        for column in MATRIX_COLUMNS:
            value = profile.get(column)
            if value in (None, [], "unknown", "not_reported"):
                normalized: Any = "unknown"
            else:
                normalized = value
            cells[column] = {
                "value": deepcopy(normalized),
                "evidence_ids": [] if normalized == "unknown" else deepcopy(profile["evidence_ids"]),
            }
        rows.append({"study_profile_id": profile["study_profile_id"], "paper_id": profile["paper_id"], "cells": cells})
    semantic = {
        "topic_id": context["topic_id"], "snapshot_id": context["snapshot_id"], "run_id": context["run_id"],
        "matrix_version": MATRIX_VERSION, "columns": list(MATRIX_COLUMNS), "rows": rows,
        "source_paper_count": len(profiles),
    }
    matrix = {"matrix_id": f"matrix-{_digest(semantic)[:28]}", **semantic, "created_at": created_at}
    matrix["matrix_digest"] = _digest(_semantic_record(matrix, omit={"matrix_digest"}))
    return matrix


def _analysis_context(state: dict[str, Any], topic_id: str, snapshot_id: str) -> dict[str, Any]:
    from app.services import prototype_workspace as workspace

    topic = workspace._topic(state, topic_id)
    snapshot = workspace._snapshot(state, snapshot_id, topic_id=topic_id)
    if topic.get("topic_kind") != "real_open_access":
        _error("research_analysis_real_corpus_required")
    included = [row for row in snapshot.get("items") or [] if row.get("curation_state") == "included"]
    if len(included) < 3:
        _error("research_analysis_insufficient_accepted_corpus")
    if any(
        row.get("license_status") != "accepted"
        or row.get("full_text_status") != "available"
        or row.get("stale_status") != "current"
        for row in included
    ):
        _error("research_analysis_untrusted_source")
    work_identities = [str(row.get("work_identity_digest") or "") for row in included]
    if any(not value for value in work_identities) or len(work_identities) != len(set(work_identities)):
        _error("research_analysis_duplicate_work_identity")
    matching_runs = [
        row for row in workspace._workspace_runs(topic_id, state)
        if row.get("snapshot_id") == snapshot_id
        and row.get("status") == "completed"
        and row.get("report_version_id")
        and int(row.get("provider_calls") or 0) == 0
    ]
    if not matching_runs:
        _error("research_analysis_completed_run_required")
    run = matching_runs[0]
    try:
        public = prototype_real_corpus.load_public_record(str(run["run_id"]))
    except prototype_real_corpus.RealCorpusError:
        _error("research_analysis_public_record_invalid")
    if public.get("status") != "complete" or int(public.get("provider_calls") or 0) != 0:
        _error("research_analysis_public_record_invalid")
    expected_pmcids = {str(row.get("pmcid") or "") for row in included}
    articles = list(public.get("articles") or [])
    if expected_pmcids != {str(row.get("pmcid") or "") for row in articles}:
        _error("research_analysis_snapshot_run_mismatch")
    item_by_pmcid = {str(row["pmcid"]): row for row in included}
    for article in articles:
        pmcid = str(article.get("pmcid") or "")
        item = item_by_pmcid[pmcid]
        if (
            not article.get("rights_accepted")
            or not article.get("full_text_retrieved")
            or article.get("content_digest") != item.get("source_content_digest")
            or article.get("review_id") is None
        ):
            _error("research_analysis_source_identity_mismatch")
    evidence = list(public.get("evidence") or [])
    claims = list(public.get("claims") or [])
    article_digest = {str(row["pmcid"]): row.get("content_digest") for row in articles}
    evidence_ids: set[str] = set()
    for row in evidence:
        evidence_id = str(row.get("evidence_id") or "")
        paper_id = str(row.get("paper_id") or "")
        if (
            not evidence_id
            or evidence_id in evidence_ids
            or paper_id not in expected_pmcids
            or row.get("source_content_digest") != article_digest[paper_id]
            or row.get("stale")
            or not row.get("paragraph_id")
            or not row.get("section_path")
        ):
            _error("research_analysis_evidence_boundary_invalid")
        evidence_ids.add(evidence_id)
    claim_ids: set[str] = set()
    for row in claims:
        claim_id = str(row.get("claim_id") or "")
        linked = {str(value) for value in row.get("evidence_ids") or []}
        papers = {str(value) for value in row.get("paper_ids") or []}
        if not claim_id or claim_id in claim_ids or not linked or linked - evidence_ids or papers - expected_pmcids:
            _error("research_analysis_claim_boundary_invalid")
        claim_ids.add(claim_id)
    related_changesets = {
        str(row.get("changeset_id")) for row in state.get("changesets") or []
        if row.get("resulting_snapshot_id") == snapshot_id
    }
    curation_event_ids = sorted(
        str(row["event_id"]) for row in state.get("curation_events") or []
        if row.get("resulting_change_set_id") in related_changesets
    )
    briefing_ids = sorted(
        str(row["briefing_id"]) for row in state.get("briefings") or []
        if row.get("topic_id") == topic_id
        and snapshot_id in {
            str(row.get("before_snapshot_id") or ""), str(row.get("after_snapshot_id") or ""),
            str(row.get("source_snapshot_id") or ""),
        }
    )
    return {
        "topic": topic, "snapshot": snapshot, "topic_id": topic_id, "snapshot_id": snapshot_id,
        "run": run, "run_id": str(run["run_id"]), "report_version_id": str(run["report_version_id"]),
        "public": public, "articles": articles, "evidence": evidence, "claims": claims,
        "included": included, "briefing_ids": briefing_ids, "change_event_ids": curation_event_ids,
    }


def _gap_record(
    context: dict[str, Any], scope: dict[str, Any], analysis_id: str, created_at: str, *,
    gap_key: str, gap_type: str, title: str, statement: str, detection_rule: str,
    materiality: str, confidence: str, profiles: list[dict[str, Any]],
    evidence_ids: list[str] | None = None, claim_ids: list[str] | None = None,
    constraint_profiles: list[dict[str, Any]] | None = None,
    unknown_dimensions: list[str] | None = None, change_event_ids: list[str] | None = None,
    counterevidence_present: bool = False,
) -> dict[str, Any]:
    if gap_type not in GAP_TYPES:
        _error("research_gap_type_invalid")
    profile_ids = sorted(str(row["study_profile_id"]) for row in profiles)
    paper_ids = sorted({str(row["paper_id"]) for row in profiles})
    linked_evidence = sorted(set(evidence_ids or [value for row in profiles for value in row["evidence_ids"]]))
    linked_claims = sorted(set(claim_ids or [value for row in profiles for value in row["claim_ids"]]))
    basis = {"analysis_id": analysis_id, "gap_key": gap_key, "gap_version": 1}
    record = {
        "gap_id": f"gap-{_digest(basis)[:28]}", "gap_key": gap_key,
        "topic_id": context["topic_id"], "snapshot_id": context["snapshot_id"],
        "run_id": context["run_id"], "report_version_id": context["report_version_id"],
        "scope_id": scope["scope_id"], "scope_version": scope["scope_version"],
        "analysis_id": analysis_id, "gap_version": 1, "gap_type": gap_type,
        "title": title, "bounded_statement": f"Within the current accepted corpus, {statement}",
        "scope_qualifier": SCOPE_QUALIFIER, "detection_rule": detection_rule,
        "materiality": materiality, "confidence": confidence,
        "independent_paper_ids": paper_ids,
        "review_ids": sorted(str(row["review_id"]) for row in profiles if row.get("review_id")),
        "evidence_ids": linked_evidence, "claim_ids": linked_claims,
        "briefing_ids": deepcopy(context["briefing_ids"]),
        "change_event_ids": sorted(set(change_event_ids or [])),
        "supporting_profile_ids": profile_ids,
        "constraint_profile_ids": sorted(str(row["study_profile_id"]) for row in constraint_profiles or []),
        "unknown_or_missing_dimensions": sorted(set(unknown_dimensions or [])),
        "counterevidence_present": counterevidence_present, "stale": False, "status": "detected",
        "generation_eligible": True, "created_at": created_at,
    }
    record["gap_digest"] = _record_digest(record, "gap_digest")
    return record


def _detect_gaps(state: dict[str, Any], context: dict[str, Any], scope: dict[str, Any], analysis_id: str, profiles: list[dict[str, Any]], created_at: str) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    for code in sorted(LIMITATION_CODES - {"unknown"}):
        matched = [row for row in profiles if code in row["limitation_codes"]]
        if len({row["paper_id"] for row in matched}) >= 2:
            outcomes = sorted({value for row in matched for value in row["outcome_tags"]})
            contexts = sorted({value for row in matched for value in row["context_tags"]})
            gaps.append(_gap_record(
                context, scope, analysis_id, created_at, gap_key=f"G01:{code}",
                gap_type="recurring_limitation", title=f"Recurring limitation: {code.replace('_', ' ')}",
                statement=(f"the limitation code '{code}' recurs across {len(matched)} independent papers; "
                           f"affected explicit outcomes are {outcomes or ['unknown']} and contexts are {contexts or ['unknown']}."),
                detection_rule="G01_RECURRING_LIMITATION_V1", materiality="actionable",
                confidence="high" if len(matched) >= 3 else "moderate", profiles=matched,
            ))
    for facet in scope["target_population_facets"]:
        matched = [row for row in profiles if facet in row["population_tags"]]
        if len(matched) <= 1 and len(profiles) >= 3:
            gaps.append(_gap_record(
                context, scope, analysis_id, created_at, gap_key=f"G02:{facet}",
                gap_type="underrepresented_population", title=f"Underrepresented population: {facet.replace('_', ' ')}",
                statement=(f"{len(matched)} of {len(profiles)} accepted study profiles explicitly cover '{facet}', "
                           "so it is underrepresented within this accepted corpus; unknown population fields are not treated as exclusion."),
                detection_rule="G02_UNDERREPRESENTED_POPULATION_V1", materiality="exploratory", confidence="moderate",
                profiles=profiles, unknown_dimensions=["unknown_population_fields_not_counted_as_absence"],
            ))
    for facet in scope["target_context_facets"]:
        matched = [row for row in profiles if facet in row["context_tags"]]
        if len(matched) <= 1 and len(profiles) >= 3:
            gaps.append(_gap_record(
                context, scope, analysis_id, created_at, gap_key=f"G03:{facet}",
                gap_type="underrepresented_context", title=f"Underrepresented context: {facet.replace('_', ' ')}",
                statement=(f"{len(matched)} of {len(profiles)} accepted study profiles explicitly cover '{facet}', "
                           "so it is underrepresented within this accepted corpus; unknown context fields are not treated as exclusion."),
                detection_rule="G03_UNDERREPRESENTED_CONTEXT_V1", materiality="exploratory", confidence="moderate",
                profiles=profiles, unknown_dimensions=["unknown_context_fields_not_counted_as_absence"],
            ))
    represented = sum(bool(row["intervention_tags"] or row["outcome_tags"]) for row in profiles)
    if represented >= 2:
        for facet in scope["target_method_facets"]:
            matched = [row for row in profiles if facet in row["method_tags"]]
            if len(matched) <= 1:
                gaps.append(_gap_record(
                    context, scope, analysis_id, created_at, gap_key=f"G04:{facet}", gap_type="methodological_gap",
                    title=f"Method coverage gap: {facet.replace('_', ' ')}",
                    statement=(f"interventions or outcomes are explicit in {represented} profiles while only {len(matched)} "
                               f"profile explicitly records '{facet}'; design values remain unknown unless stated in body evidence."),
                    detection_rule="G04_METHODOLOGICAL_GAP_V1", materiality="actionable", confidence="moderate",
                    profiles=profiles, unknown_dimensions=["explicit_design_or_method_not_reported"],
                ))
    longitudinal = [row for row in profiles if "short_follow_up" in row["limitation_codes"]]
    if len(longitudinal) >= 2 and sum(bool(row["intervention_tags"] or row["outcome_tags"]) for row in longitudinal) >= 2:
        gaps.append(_gap_record(
            context, scope, analysis_id, created_at, gap_key="G06:long_term_follow_up", gap_type="longitudinal_gap",
            title="Long-term follow-up gap",
            statement=(f"{len(longitudinal)} independent papers explicitly identify longer-term outcome uncertainty or limited follow-up; "
                       "unreported follow-up alone is not used as evidence."),
            detection_rule="G06_LONGITUDINAL_GAP_V1", materiality="high_priority", confidence="high",
            profiles=longitudinal,
        ))
    measurement = [row for row in profiles if "measurement_limitations" in row["limitation_codes"]]
    if len(measurement) >= 2:
        gaps.append(_gap_record(
            context, scope, analysis_id, created_at, gap_key="G05:explicit_measurement_limitations",
            gap_type="measurement_gap", title="Recurring explicit measurement limitations",
            statement=(f"{len(measurement)} independent papers explicitly record measurement limitations; "
                       "different wording alone is not treated as metric incompatibility."),
            detection_rule="G05_MEASUREMENT_GAP_V1", materiality="actionable", confidence="moderate",
            profiles=measurement,
        ))
    evidence_by_id = {str(row["evidence_id"]): row for row in context["evidence"]}
    claims_by_category: dict[str, list[dict[str, Any]]] = {}
    for claim in context["claims"]:
        claims_by_category.setdefault(str(claim.get("claim_category") or "unknown"), []).append(claim)
    for category, claims in sorted(claims_by_category.items()):
        paper_ids = sorted({str(value) for claim in claims for value in claim.get("paper_ids") or []})
        if len(paper_ids) == 1:
            matched_profiles = [row for row in profiles if row["paper_id"] in paper_ids]
            linked_evidence = sorted({str(value) for claim in claims for value in claim.get("evidence_ids") or []})
            gaps.append(_gap_record(
                context, scope, analysis_id, created_at, gap_key=f"G07:{category}:{paper_ids[0]}",
                gap_type="replication_gap", title=f"Single-paper result category: {category.replace('_', ' ')}",
                statement=(f"the current grounded result category '{category}' depends on one independent paper "
                           "and has no second accepted paper support or constraint."),
                detection_rule="G07_REPLICATION_GAP_V1", materiality="exploratory", confidence="low",
                profiles=matched_profiles, evidence_ids=linked_evidence,
                claim_ids=[str(claim["claim_id"]) for claim in claims],
            ))
        for claim in claims:
            linked = [evidence_by_id[str(value)] for value in claim.get("evidence_ids") or [] if str(value) in evidence_by_id]
            if linked and all(str(row.get("confidence_strength") or "") in {"low", "insufficient"} for row in linked):
                claim_papers = {str(value) for value in claim.get("paper_ids") or []}
                matched_profiles = [row for row in profiles if row["paper_id"] in claim_papers]
                gaps.append(_gap_record(
                    context, scope, analysis_id, created_at, gap_key=f"G08:{claim['claim_id']}",
                    gap_type="evidence_strength_gap", title="Low-strength evidence for a current grounded claim",
                    statement=(f"grounded claim '{claim['claim_id']}' is linked only to low or insufficient-strength current evidence."),
                    detection_rule="G08_EVIDENCE_STRENGTH_GAP_V1", materiality="actionable", confidence="high",
                    profiles=matched_profiles, evidence_ids=[str(row["evidence_id"]) for row in linked],
                    claim_ids=[str(claim["claim_id"])],
                ))
    for index, left in enumerate(profiles):
        for right in profiles[index + 1:]:
            directions = {left["evidence_direction"], right["evidence_direction"]}
            opposing = directions in ({"positive", "negative"}, {"positive", "null"}, {"negative", "null"}) or "mixed" in directions
            compatible = all(
                set(left[field]) & set(right[field])
                for field in ("population_tags", "context_tags", "intervention_tags", "outcome_tags")
            )
            if opposing and compatible and "unknown" not in directions:
                gaps.append(_gap_record(
                    context, scope, analysis_id, created_at,
                    gap_key=f"G09:{min(left['paper_id'], right['paper_id'])}:{max(left['paper_id'], right['paper_id'])}",
                    gap_type="conflicting_findings", title="Compatible profiles with explicitly differing directions",
                    statement=(f"papers {left['paper_id']} and {right['paper_id']} share explicit population, context, "
                               f"intervention, and outcome facets but record directions {sorted(directions)}."),
                    detection_rule="G09_CONFLICTING_FINDINGS_V1", materiality="actionable", confidence="moderate",
                    profiles=[left, right], counterevidence_present=True,
                ))
    parent_id = context["snapshot"].get("parent_snapshot_id")
    if parent_id and context["change_event_ids"]:
        from app.services import prototype_workspace as workspace

        parent = workspace._snapshot(state, str(parent_id), topic_id=context["topic_id"])
        current = context["snapshot"]
        if int(current.get("evidence_count") or 0) < int(parent.get("evidence_count") or 0):
            gaps.append(_gap_record(
                context, scope, analysis_id, created_at, gap_key=f"G10:{parent_id}:{context['snapshot_id']}",
                gap_type="coverage_loss", title="Curated corpus coverage loss",
                statement=(f"curation from snapshot version {parent['snapshot_version']} to {current['snapshot_version']} reduced "
                           f"accepted evidence coverage by {int(parent['evidence_count']) - int(current['evidence_count'])} spans; "
                           "historical reports and evidence remain unchanged."),
                detection_rule="G10_COVERAGE_LOSS_V1", materiality="actionable", confidence="high",
                profiles=profiles, change_event_ids=context["change_event_ids"],
            ))
    unreviewed = [row for row in context["included"] if row.get("review_status") != "reviewed"]
    if unreviewed:
        gaps.append(_gap_record(
            context, scope, analysis_id, created_at, gap_key="G11:new_unreviewed_evidence",
            gap_type="new_unreviewed_evidence", title="Accepted source identity awaits full-text review",
            statement=(f"{len(unreviewed)} accepted current-snapshot source identities have not completed review or evidence extraction."),
            detection_rule="G11_NEW_UNREVIEWED_EVIDENCE_V1", materiality="actionable", confidence="high",
            profiles=profiles, change_event_ids=context["change_event_ids"],
            unknown_dimensions=["unreviewed_source_not_used_for_recommendation"],
        ))
        gaps[-1]["generation_eligible"] = False
        gaps[-1]["gap_digest"] = _record_digest(gaps[-1], "gap_digest")
    data_limited = [row for row in profiles if row["data_availability_codes"]]
    if data_limited:
        gaps.append(_gap_record(
            context, scope, analysis_id, created_at, gap_key="G12:explicit_data_availability",
            gap_type="data_availability_gap", title="Explicit data-availability constraint",
            statement=(f"{len(data_limited)} independent paper profiles explicitly record a structured data-availability constraint; "
                       "missing metadata alone is not treated as evidence."),
            detection_rule="G12_DATA_AVAILABILITY_GAP_V1", materiality="exploratory",
            confidence="moderate" if len(data_limited) >= 2 else "low", profiles=data_limited,
        ))
    unique: dict[str, dict[str, Any]] = {}
    for gap in gaps:
        unique[str(gap["gap_key"])] = gap
    return [unique[key] for key in sorted(unique)]


def _normalize_question(value: str) -> str:
    return " ".join(_TOKEN_RE.findall(value.lower()))


def _similarity(left: str, right: str) -> float:
    left_tokens, right_tokens = set(_TOKEN_RE.findall(left.lower())), set(_TOKEN_RE.findall(right.lower()))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _collision(state: dict[str, Any], context: dict[str, Any], question: str, facets: dict[str, list[str]]) -> dict[str, Any]:
    targets = [{"target_id": context["topic_id"], "question": str(context["topic"].get("research_question") or ""), "facets": {}}]
    for row in state["research_recommendations"]:
        if row.get("topic_id") == context["topic_id"] and row.get("source_snapshot_id") == context["snapshot_id"] and not row.get("stale"):
            targets.append({"target_id": row["recommendation_id"], "question": row["research_question"], "facets": row.get("collision_facets") or {}})
    for origin in state["recommendation_origins"]:
        if origin.get("source_topic_id") == context["topic_id"]:
            topic = next((row for row in state["topics"] if row.get("topic_id") == origin.get("new_topic_id")), None)
            if topic:
                targets.append({"target_id": topic["topic_id"], "question": str(topic.get("research_question") or ""), "facets": {}})
    comparisons = []
    normalized = _normalize_question(question)
    for target in targets:
        target_normalized = _normalize_question(target["question"])
        token_similarity = _similarity(question, target["question"])
        target_facets = target.get("facets") or {}
        overlaps = sorted(
            name for name, values in facets.items()
            if set(values) & set(target_facets.get(name) or [])
        )
        comparisons.append({
            "target_id": target["target_id"], "normalized_equal": bool(normalized and normalized == target_normalized),
            "token_set_similarity": round(token_similarity, 4), "overlapping_dimensions": overlaps,
        })
    maximum = max((row["token_set_similarity"] for row in comparisons), default=0.0)
    if any(row["normalized_equal"] for row in comparisons):
        result = "duplicate_candidate"
    elif maximum >= 0.78:
        result = "close_collision"
    elif maximum >= 0.42:
        result = "partial_overlap"
    elif not normalized:
        result = "insufficient_structure"
    else:
        result = "no_close_collision"
    collision = {
        "comparison_target_ids": [row["target_id"] for row in comparisons],
        "maximum_similarity": maximum,
        "overlapping_dimensions": sorted({value for row in comparisons for value in row["overlapping_dimensions"]}),
        "result": result, "policy_version": COLLISION_POLICY,
        "comparisons": comparisons,
        "disclosure": "No close collision found within the current accepted corpus. External novelty has not been assessed." if result == "no_close_collision" else "Collision assessment is limited to the current accepted corpus; external novelty has not been assessed.",
    }
    collision["collision_digest"] = _digest(collision)
    return collision


def _score_label(score: int) -> str:
    if score <= 39:
        label = "limited basis"
    elif score <= 59:
        label = "exploratory"
    elif score <= 74:
        label = "review-worthy"
    elif score <= 89:
        label = "strong candidate"
    else:
        label = "high-priority candidate"
    return f"{label} within current corpus"


def _recommendation_template(gap: dict[str, Any]) -> dict[str, Any] | None:
    key = str(gap["gap_key"])
    if key == "G01:small_sample":
        return {
            "recommendation_key": "replicate-task-performance-larger-sample",
            "recommendation_type": "replication_study",
            "title": "Replicate immersive rehabilitation task-performance findings",
            "research_question": "Among rehabilitation participants, does an immersive rehabilitation protocol improve task performance versus a prespecified comparison under a larger replication sample?",
            "candidate_hypothesis": "Rehabilitation participants receiving the immersive protocol will show a larger prespecified task-performance improvement than the comparison group over the intervention period.",
            "null_hypothesis": "The prespecified task-performance change will not differ between the immersive protocol and comparison groups over the intervention period.",
            "target_population": ["rehabilitation_participants"], "target_context": ["rehabilitation"],
            "intervention_or_exposure": ["immersive rehabilitation protocol"], "comparator": ["prespecified comparison group"],
            "primary_outcomes": ["task performance"], "secondary_outcomes": ["engagement", "safety"],
            "proposed_study_design": "preregistered replication with a prespecified comparison group",
            "time_horizon": "intervention period with a prespecified follow-up",
        }
    if key == "G06:long_term_follow_up":
        return {
            "recommendation_key": "longitudinal-durability-immersive-rehabilitation",
            "recommendation_type": "longitudinal_study",
            "title": "Evaluate durability of immersive rehabilitation outcomes",
            "research_question": "Among rehabilitation participants, are task-performance gains after an immersive rehabilitation protocol sustained at a prespecified longer-term follow-up versus baseline or comparison care?",
            "candidate_hypothesis": "Task-performance gains after the immersive protocol will remain greater than baseline or comparison care at the prespecified longer-term follow-up.",
            "null_hypothesis": "Task-performance gains will not remain greater than baseline or comparison care at the prespecified longer-term follow-up.",
            "target_population": ["rehabilitation_participants"], "target_context": ["rehabilitation"],
            "intervention_or_exposure": ["immersive rehabilitation protocol"], "comparator": ["baseline or comparison care"],
            "primary_outcomes": ["task performance at longer-term follow-up"], "secondary_outcomes": ["adherence", "safety"],
            "proposed_study_design": "prospective longitudinal controlled evaluation",
            "time_horizon": "prespecified longer-term follow-up",
        }
    if key == "G02:clinical_learners":
        return {
            "recommendation_key": "clinical-learners-comparative-training",
            "recommendation_type": "comparative_study",
            "title": "Compare immersive and standard training for clinical learners",
            "research_question": "Among clinical learners, does an immersive clinical-training protocol improve prespecified training performance versus standard training within the training period?",
            "candidate_hypothesis": "Clinical learners receiving immersive training will improve more on the prespecified training-performance measure than learners receiving standard training.",
            "null_hypothesis": "Improvement on the prespecified training-performance measure will not differ between immersive and standard training.",
            "target_population": ["clinical_learners"], "target_context": ["clinical_training"],
            "intervention_or_exposure": ["immersive clinical training"], "comparator": ["standard clinical training"],
            "primary_outcomes": ["training performance"], "secondary_outcomes": ["engagement", "implementation feasibility"],
            "proposed_study_design": "prospective comparative controlled evaluation",
            "time_horizon": "defined training period",
        }
    if key == "G03:telerehabilitation":
        return {
            "recommendation_key": "telerehabilitation-implementation-feasibility",
            "recommendation_type": "implementation_study",
            "title": "Assess immersive telerehabilitation feasibility",
            "research_question": "Among rehabilitation participants, is a bounded immersive telerehabilitation protocol feasible and acceptably safe compared with the existing delivery baseline during a prespecified implementation period?",
            "candidate_hypothesis": "The immersive telerehabilitation protocol will meet prespecified feasibility and safety thresholds relative to the existing delivery baseline.",
            "null_hypothesis": "The immersive telerehabilitation protocol will not meet the prespecified feasibility or safety thresholds relative to the existing delivery baseline.",
            "target_population": ["rehabilitation_participants"], "target_context": ["telerehabilitation"],
            "intervention_or_exposure": ["immersive telerehabilitation protocol"], "comparator": ["existing delivery baseline"],
            "primary_outcomes": ["implementation feasibility", "safety"], "secondary_outcomes": ["adherence", "engagement"],
            "proposed_study_design": "prospective implementation evaluation with prespecified thresholds",
            "time_horizon": "prespecified implementation period",
        }
    if key == "G04:controlled_evaluation":
        return {
            "recommendation_key": "controlled-immersive-functional-outcomes",
            "recommendation_type": "comparative_study",
            "title": "Use a controlled evaluation for immersive functional outcomes",
            "research_question": "Among rehabilitation participants, does immersive rehabilitation improve a prespecified functional outcome versus a concurrent control during the intervention period?",
            "candidate_hypothesis": "Immersive rehabilitation will produce a larger improvement in the prespecified functional outcome than the concurrent control.",
            "null_hypothesis": "Improvement in the prespecified functional outcome will not differ from the concurrent control.",
            "target_population": ["rehabilitation_participants"], "target_context": ["rehabilitation"],
            "intervention_or_exposure": ["immersive rehabilitation"], "comparator": ["concurrent control"],
            "primary_outcomes": ["prespecified functional outcome"], "secondary_outcomes": ["task performance", "safety"],
            "proposed_study_design": "controlled comparative evaluation",
            "time_horizon": "intervention period",
        }
    return None


def _build_recommendations(state: dict[str, Any], context: dict[str, Any], analysis_id: str, gaps: list[dict[str, Any]], profiles: list[dict[str, Any]], created_at: str) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []
    shadow = deepcopy(state)
    profile_by_id = {str(row["study_profile_id"]): row for row in profiles}
    for gap in gaps:
        template = _recommendation_template(gap)
        if template is None:
            continue
        facets = {
            "population": list(template["target_population"]), "context": list(template["target_context"]),
            "intervention": list(template["intervention_or_exposure"]), "comparator": list(template["comparator"]),
            "outcome": list(template["primary_outcomes"]), "design": [template["proposed_study_design"]],
        }
        collision = _collision(shadow, context, str(template["research_question"]), facets)
        if collision["result"] == "duplicate_candidate":
            continue
        support_profiles = [profile_by_id[value] for value in gap["supporting_profile_ids"] if value in profile_by_id]
        supporting_papers = sorted({str(row["paper_id"]) for row in support_profiles})
        supporting_evidence = sorted({value for row in support_profiles for value in row["evidence_ids"]})
        constraint_profiles = [row for row in support_profiles if row["limitation_codes"]]
        constraint_papers = sorted({str(row["paper_id"]) for row in constraint_profiles})
        constraint_evidence = sorted({value for row in constraint_profiles for value in row["evidence_ids"]})
        collision_novelty = {"no_close_collision": 86, "partial_overlap": 68, "close_collision": 44, "insufficient_structure": 25}[collision["result"]]
        missing_count = sum(len(row["unknown_fields"]) for row in support_profiles)
        feasibility = max(35, 78 - min(30, missing_count * 2))
        significance = min(94, 58 + 6 * len(supporting_papers) + (10 if gap["materiality"] == "high_priority" else 4))
        falsifiability = 88
        readiness = min(92, 50 + 6 * len(supporting_papers) + min(20, len(supporting_evidence)))
        timeliness = 75 if gap["gap_type"] == "coverage_loss" or context["briefing_ids"] else 58
        overall = round(
            0.15 * collision_novelty + 0.20 * feasibility + 0.25 * significance
            + 0.20 * falsifiability + 0.10 * readiness + 0.10 * timeliness
        )
        if len(supporting_papers) < 2 and template["recommendation_type"] != "replication_study":
            overall = min(59, overall)
        limitations = sorted({code for row in constraint_profiles for code in row["limitation_codes"]})
        basis = {"analysis_id": analysis_id, "recommendation_key": template["recommendation_key"], "recommendation_version": 1}
        record = {
            "recommendation_id": f"recommendation-{_digest(basis)[:28]}",
            "recommendation_key": template["recommendation_key"],
            "topic_id": context["topic_id"], "source_snapshot_id": context["snapshot_id"],
            "source_run_id": context["run_id"], "source_report_version_id": context["report_version_id"],
            "analysis_id": analysis_id, "recommendation_version": 1,
            "recommendation_type": template["recommendation_type"], "title": template["title"],
            "research_question": template["research_question"], "candidate_hypothesis": template["candidate_hypothesis"],
            "null_hypothesis": template["null_hypothesis"], "hypothesis_status": "falsifiable_candidate",
            "target_population": template["target_population"], "target_context": template["target_context"],
            "intervention_or_exposure": template["intervention_or_exposure"], "comparator": template["comparator"],
            "primary_outcomes": template["primary_outcomes"], "secondary_outcomes": template["secondary_outcomes"],
            "proposed_study_design": template["proposed_study_design"],
            "minimum_data_requirements": ["prespecified eligible sample", "current outcome observations", "documented comparison or baseline"],
            "measurement_requirements": ["prespecified primary outcome", "documented measurement timing", "explicit missing-data handling"],
            "time_horizon": template["time_horizon"], "supporting_gap_ids": [gap["gap_id"]],
            "supporting_paper_ids": supporting_papers, "supporting_evidence_ids": supporting_evidence,
            "constraint_paper_ids": constraint_papers, "constraint_evidence_ids": constraint_evidence,
            "contradictory_evidence_ids": [],
            "assumptions": ["The candidate question is bounded to the current accepted corpus and requires prospective validation."],
            "limitations": limitations or ["none_identified_in_current_corpus"],
            "ethics_or_practical_considerations": ["prospective ethics review", "accessibility and adverse-event monitoring"],
            "corpus_relative_novelty_score": collision_novelty, "global_novelty_status": GLOBAL_NOVELTY_STATUS,
            "feasibility_score": feasibility, "significance_score": significance,
            "falsifiability_score": falsifiability, "evidence_readiness_score": readiness,
            "timeliness_score": timeliness, "overall_score": overall, "score_label": _score_label(overall),
            "score_explanation": "Decision-support score under research_recommendation_scoring_v1; it is not scientific validation.",
            "scoring_policy_version": SCORING_POLICY, "confidence": gap["confidence"], "status": "generated",
            "created_at": created_at, "stale": False, "collision": collision, "collision_facets": facets,
            "support_acceptance": "deterministic_rule_validated_current_gap",
        }
        record["recommendation_digest"] = _record_digest(record, "recommendation_digest")
        recommendations.append(record)
        shadow["research_recommendations"].append(record)
    return recommendations


def _current_status(state: dict[str, Any], recommendation: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    decisions = sorted(
        (row for row in state["recommendation_decisions"] if row.get("recommendation_id") == recommendation["recommendation_id"]),
        key=lambda row: (str(row.get("created_at") or ""), str(row.get("decision_event_id") or "")),
    )
    origin = next((row for row in state["recommendation_origins"] if row.get("recommendation_id") == recommendation["recommendation_id"]), None)
    if origin:
        return "promoted", decisions[-1] if decisions else None
    return (str(decisions[-1]["requested_status"]), decisions[-1]) if decisions else ("generated", None)


def _decorate_recommendation(state: dict[str, Any], recommendation: dict[str, Any]) -> dict[str, Any]:
    status, latest = _current_status(state, recommendation)
    decisions = sorted(
        (deepcopy(row) for row in state["recommendation_decisions"] if row.get("recommendation_id") == recommendation["recommendation_id"]),
        key=lambda row: (str(row.get("created_at") or ""), str(row.get("decision_event_id") or "")),
    )
    origin = next((deepcopy(row) for row in state["recommendation_origins"] if row.get("recommendation_id") == recommendation["recommendation_id"]), None)
    source_topic = next((row for row in state["topics"] if row.get("topic_id") == recommendation.get("topic_id")), None)
    current_for_active_snapshot = bool(
        source_topic and source_topic.get("active_snapshot_id") == recommendation.get("source_snapshot_id")
    )
    return {
        **deepcopy(recommendation), "status": status, "stored_status": recommendation["status"],
        "latest_decision": deepcopy(latest), "decision_history": decisions, "promotion_origin": origin,
        "current_for_active_snapshot": current_for_active_snapshot,
        "historical": not current_for_active_snapshot,
    }


def _analysis_detail(state: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    analysis_id = str(analysis["analysis_id"])
    matrix = next((deepcopy(row) for row in state["evidence_matrices"] if row.get("matrix_id") == analysis.get("matrix_id")), None)
    profiles = sorted(
        (deepcopy(row) for row in state["study_profiles"] if row.get("study_profile_id") in set(analysis.get("study_profile_ids") or [])),
        key=lambda row: str(row.get("paper_id") or ""),
    )
    gaps = sorted(
        (deepcopy(row) for row in state["research_gaps"] if row.get("analysis_id") == analysis_id),
        key=lambda row: (str(row.get("gap_type") or ""), str(row.get("title") or "")),
    )
    recommendations = sorted(
        (_decorate_recommendation(state, row) for row in state["research_recommendations"] if row.get("analysis_id") == analysis_id),
        key=lambda row: (-int(row.get("overall_score") or 0), str(row.get("title") or "")),
    )
    scope = next((deepcopy(row) for row in state["analysis_scopes"] if row.get("scope_id") == analysis.get("scope_id")), None)
    return {
        **deepcopy(analysis), "scope": scope, "study_profiles": profiles, "evidence_matrix": matrix,
        "gaps": gaps, "recommendations": recommendations,
        "warnings": [
            "All gaps and novelty scores are bounded to the current accepted corpus.",
            "External novelty has not been assessed.",
            "Recommendations are decision support, not scientific validation.",
        ],
        "provider_calls": 0, "external_network_requests": 0,
    }


def analyze_topic(topic_id: str, snapshot_id: str) -> dict[str, Any]:
    from app.services import prototype_workspace as workspace

    safe_topic = workspace._safe_identifier(topic_id, code="topic_id_invalid")
    safe_snapshot = workspace._safe_identifier(snapshot_id, code="snapshot_id_invalid")
    initial_state = workspace._load_state()
    context = _analysis_context(initial_state, safe_topic, safe_snapshot)
    created_at = iso_now_utc()
    scope_preview = _scope_record(safe_topic, created_at)
    key_basis = {
        "topic_id": safe_topic, "snapshot_id": safe_snapshot, "run_id": context["run_id"],
        "report_version_id": context["report_version_id"], "scope_digest": scope_preview["scope_digest"],
        "profile_version": PROFILE_VERSION, "gap_rule_version": GAP_RULE_VERSION,
        "scoring_policy_version": SCORING_POLICY, "input_manifest_digest": context["snapshot"]["manifest_digest"],
        "public_record_digest": context["public"].get("record_digest"),
    }
    analysis_key_digest = _digest(key_basis)
    analysis_id = f"gap-analysis-{analysis_key_digest[:28]}"
    existing = next((row for row in initial_state["gap_analyses"] if row.get("analysis_id") == analysis_id), None)
    if existing:
        if existing.get("status") == "COMPLETED":
            return _analysis_detail(initial_state, existing)
        _error("research_analysis_already_in_progress")
    running = {
        "analysis_id": analysis_id, "analysis_key_digest": analysis_key_digest,
        "topic_id": safe_topic, "snapshot_id": safe_snapshot, "run_id": context["run_id"],
        "report_version_id": context["report_version_id"], "scope_id": scope_preview["scope_id"],
        "scope_version": scope_preview["scope_version"], "profile_version": PROFILE_VERSION,
        "gap_rule_version": GAP_RULE_VERSION, "scoring_policy_version": SCORING_POLICY,
        "input_paper_count": len(context["articles"]), "input_review_count": len(context["articles"]),
        "input_evidence_count": len(context["evidence"]), "input_claim_count": len(context["claims"]),
        "input_briefing_count": len(context["briefing_ids"]), "input_change_event_count": len(context["change_event_ids"]),
        "matrix_id": None, "matrix_digest": None, "input_manifest_digest": context["snapshot"]["manifest_digest"],
        "source_record_digest": context["public"].get("record_digest"),
        "study_profile_ids": [], "started_at": created_at, "completed_at": None,
        "status": "RUNNING", "gap_count": 0, "recommendation_count": 0,
    }
    running["analysis_digest"] = _record_digest(running, "analysis_digest")

    def begin(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        _analysis_context(state, safe_topic, safe_snapshot)
        scope = _find_or_create_scope(state, safe_topic, created_at)
        running["scope_id"] = scope["scope_id"]
        if any(row.get("analysis_id") == analysis_id for row in state["gap_analyses"]):
            _error("research_analysis_already_in_progress")
        state["gap_analyses"].append(deepcopy(running))
        return state, deepcopy(running)

    workspace._mutate_state(begin)
    try:
        profiles = [
            _profile(article, _text_rows(context["evidence"], str(article["pmcid"])), context["claims"], context, created_at)
            for article in context["articles"]
        ]
        matrix = _matrix(profiles, context, created_at)
        gaps = _detect_gaps(initial_state, context, scope_preview, analysis_id, profiles, created_at)
        recommendations = _build_recommendations(initial_state, context, analysis_id, gaps, profiles, created_at)
        if not gaps or not recommendations:
            _error("research_analysis_insufficient_structural_support")
    except Exception:
        def fail(state: dict[str, Any]) -> tuple[dict[str, Any], None]:
            row = next(item for item in state["gap_analyses"] if item.get("analysis_id") == analysis_id)
            if row.get("status") == "RUNNING":
                row.update({"status": "FAILED_VALIDATION", "completed_at": iso_now_utc()})
                row["analysis_digest"] = _record_digest(row, "analysis_digest")
            return state, None
        workspace._mutate_state(fail)
        raise

    def complete(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        fresh = _analysis_context(state, safe_topic, safe_snapshot)
        if fresh["public"].get("record_digest") != context["public"].get("record_digest"):
            _error("research_analysis_source_changed")
        row = next(item for item in state["gap_analyses"] if item.get("analysis_id") == analysis_id)
        if row.get("status") != "RUNNING":
            _error("research_analysis_state_conflict")
        for profile in profiles:
            if not any(item.get("study_profile_id") == profile["study_profile_id"] for item in state["study_profiles"]):
                state["study_profiles"].append(profile)
        if not any(item.get("matrix_id") == matrix["matrix_id"] for item in state["evidence_matrices"]):
            state["evidence_matrices"].append(matrix)
        state["research_gaps"].extend(gaps)
        state["research_recommendations"].extend(recommendations)
        row.update({
            "matrix_id": matrix["matrix_id"], "matrix_digest": matrix["matrix_digest"],
            "study_profile_ids": sorted(profile["study_profile_id"] for profile in profiles),
            "completed_at": iso_now_utc(), "status": "COMPLETED",
            "gap_count": len(gaps), "recommendation_count": len(recommendations),
        })
        row["analysis_digest"] = _record_digest(row, "analysis_digest")
        return state, deepcopy(row)

    completed = workspace._mutate_state(complete)
    return _analysis_detail(workspace._load_state(), completed)


def get_analysis(topic_id: str, analysis_id: str) -> dict[str, Any]:
    from app.services import prototype_workspace as workspace

    state = workspace._load_state()
    topic = workspace._safe_identifier(topic_id, code="topic_id_invalid")
    safe_id = workspace._safe_identifier(analysis_id, code="analysis_id_invalid")
    row = next((item for item in state["gap_analyses"] if item.get("analysis_id") == safe_id), None)
    if not row:
        _error("research_analysis_not_found", status_code=404)
    if row.get("topic_id") != topic:
        _error("cross_topic_research_analysis")
    return _analysis_detail(state, row)


def decide_recommendation(topic_id: str, recommendation_id: str, *, requested_status: str, reason_code: str, bounded_note: str | None) -> dict[str, Any]:
    from app.services import prototype_workspace as workspace

    safe_topic = workspace._safe_identifier(topic_id, code="topic_id_invalid")
    safe_recommendation = workspace._safe_identifier(recommendation_id, code="recommendation_id_invalid")
    if requested_status not in DECISION_STATUSES:
        _error("research_decision_status_invalid", status_code=400)
    if reason_code not in DECISION_REASONS:
        _error("research_decision_reason_invalid", status_code=400)
    note = workspace._safe_text(bounded_note, maximum=240) or None
    now = iso_now_utc()

    def mutation(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        recommendation = next((row for row in state["research_recommendations"] if row.get("recommendation_id") == safe_recommendation), None)
        if not recommendation:
            _error("research_recommendation_not_found", status_code=404)
        if recommendation.get("topic_id") != safe_topic:
            _error("cross_topic_recommendation_reference")
        previous, _latest = _current_status(state, recommendation)
        if previous == "promoted":
            _error("promoted_recommendation_decision_locked")
        if previous == requested_status:
            _error("research_decision_no_change", status_code=400)
        identity = {
            "recommendation_id": safe_recommendation, "previous_status": previous,
            "requested_status": requested_status, "reason_code": reason_code, "bounded_note": note,
            "created_at": now, "sequence": sum(1 for row in state["recommendation_decisions"] if row.get("recommendation_id") == safe_recommendation) + 1,
        }
        event = {
            "decision_event_id": f"decision-{_digest(identity)[:28]}", "recommendation_id": safe_recommendation,
            "topic_id": safe_topic, "analysis_id": recommendation["analysis_id"],
            "previous_status": previous, "requested_status": requested_status,
            "reason_code": reason_code, "bounded_note": note, "actor_mode": LOCAL_ACTOR,
            "created_at": now,
        }
        event["decision_digest"] = _record_digest(event, "decision_digest")
        state["recommendation_decisions"].append(event)
        return state, deepcopy(event)

    event = workspace._mutate_state(mutation)
    state = workspace._load_state()
    recommendation = next(row for row in state["research_recommendations"] if row.get("recommendation_id") == safe_recommendation)
    return {"decision_event": event, "recommendation": _decorate_recommendation(state, recommendation), "provider_calls": 0, "network_requests": 0}


def promote_recommendation(topic_id: str, recommendation_id: str, *, confirmed: bool) -> dict[str, Any]:
    from app.services import prototype_workspace as workspace

    if not confirmed:
        _error("recommendation_promotion_confirmation_required", status_code=400)
    safe_topic = workspace._safe_identifier(topic_id, code="topic_id_invalid")
    safe_recommendation = workspace._safe_identifier(recommendation_id, code="recommendation_id_invalid")
    now = iso_now_utc()

    def mutation(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        recommendation = next((row for row in state["research_recommendations"] if row.get("recommendation_id") == safe_recommendation), None)
        if not recommendation:
            _error("research_recommendation_not_found", status_code=404)
        if recommendation.get("topic_id") != safe_topic:
            _error("cross_topic_recommendation_reference")
        existing_origin = next((row for row in state["recommendation_origins"] if row.get("recommendation_id") == safe_recommendation), None)
        if existing_origin:
            existing_topic = workspace._topic(state, str(existing_origin["new_topic_id"]))
            return state, {"duplicate": True, "origin": deepcopy(existing_origin), "topic_id": existing_topic["topic_id"]}
        status, latest = _current_status(state, recommendation)
        if status != "accepted" or not latest:
            _error("recommendation_acceptance_required")
        if recommendation.get("stale"):
            _error("stale_recommendation_promotion_blocked")
        analysis = next((row for row in state["gap_analyses"] if row.get("analysis_id") == recommendation.get("analysis_id")), None)
        if not analysis or analysis.get("status") != "COMPLETED":
            _error("recommendation_analysis_not_current")
        snapshot = workspace._snapshot(state, str(recommendation["source_snapshot_id"]), topic_id=safe_topic)
        source_topic = workspace._topic(state, safe_topic)
        if source_topic.get("active_snapshot_id") != snapshot.get("snapshot_id"):
            _error("historical_recommendation_promotion_blocked")
        if snapshot.get("manifest_digest") != analysis.get("input_manifest_digest"):
            _error("recommendation_source_identity_changed")
        context = _analysis_context(state, safe_topic, str(snapshot["snapshot_id"]))
        if context["run_id"] != recommendation.get("source_run_id") or context["report_version_id"] != recommendation.get("source_report_version_id"):
            _error("recommendation_source_identity_changed")
        gap_ids = set(recommendation.get("supporting_gap_ids") or [])
        valid_gaps = {
            str(row["gap_id"]) for row in state["research_gaps"]
            if row.get("analysis_id") == analysis["analysis_id"] and not row.get("stale") and row.get("generation_eligible")
        }
        if not gap_ids or gap_ids - valid_gaps:
            _error("recommendation_gap_identity_changed")
        topic_identity = {"recommendation_id": safe_recommendation, "decision_event_id": latest["decision_event_id"]}
        new_topic_id = f"topic-recommended-{_digest(topic_identity)[:20]}"
        title = workspace._safe_text(f"Research candidate: {recommendation['title']}", maximum=160, required=True)
        if any(row.get("topic_id") != new_topic_id and row.get("slug") == workspace.slugify(title) for row in state["topics"]):
            title = workspace._safe_text(f"{title[:140]} {new_topic_id[-8:]}", maximum=160, required=True)
        description = workspace._safe_text(
            "Corpus-bounded candidate promoted for local follow-up. It is decision support, not scientific validation.",
            maximum=500,
        )
        topic = workspace._topic_record(
            topic_id=new_topic_id, display_name=title, description=description,
            research_question=workspace._safe_text(recommendation["research_question"], maximum=500),
            topic_kind="local_empty", source_policy="local_only_no_external_source", language="en",
            publication_date_cutoff=None, license_policy="local_only", refresh_mode="disabled",
            provider_posture="deterministic_only", created_at=now,
        )
        origin_id = f"origin-{_digest({'new_topic_id': new_topic_id, **topic_identity})[:28]}"
        topic.update({
            "research_origin_id": origin_id, "origin_recommendation_id": safe_recommendation,
            "bounded_rationale": recommendation["score_explanation"],
            "candidate_hypothesis": recommendation["candidate_hypothesis"],
            "suggested_study_design": recommendation["proposed_study_design"],
            "suggested_population": deepcopy(recommendation["target_population"]),
            "suggested_context": deepcopy(recommendation["target_context"]),
            "suggested_outcomes": deepcopy(recommendation["primary_outcomes"]),
        })
        origin = {
            "origin_id": origin_id, "new_topic_id": new_topic_id, "source_topic_id": safe_topic,
            "source_snapshot_id": recommendation["source_snapshot_id"], "source_run_id": recommendation["source_run_id"],
            "source_report_version_id": recommendation["source_report_version_id"], "analysis_id": recommendation["analysis_id"],
            "gap_ids": sorted(gap_ids), "recommendation_id": safe_recommendation,
            "decision_event_id": latest["decision_event_id"], "created_at": now,
        }
        origin["origin_digest"] = _record_digest(origin, "origin_digest")
        state["topics"].append(topic)
        state["settings_versions"].append(workspace._settings_version(topic, changed_fields=[], created_at=now))
        state["recommendation_origins"].append(origin)
        return state, {"duplicate": False, "origin": deepcopy(origin), "topic_id": new_topic_id}

    result = workspace._mutate_state(mutation)
    return {
        "duplicate": result["duplicate"], "origin": result["origin"],
        "topic": workspace.get_topic(str(result["topic_id"])),
        "automatic_snapshot_count": 0, "automatic_run_count": 0, "automatic_report_count": 0,
        "automatic_schedule_count": 0, "provider_calls": 0, "network_requests": 0,
    }


def compare_recommendations(topic_id: str, recommendation_ids: list[str]) -> dict[str, Any]:
    from app.services import prototype_workspace as workspace

    state = workspace._load_state()
    safe_topic = workspace._safe_identifier(topic_id, code="topic_id_invalid")
    safe_ids = [workspace._safe_identifier(value, code="recommendation_id_invalid") for value in recommendation_ids]
    if len(safe_ids) < 2 or len(safe_ids) != len(set(safe_ids)) or len(safe_ids) > 5:
        _error("recommendation_comparison_requires_two_to_five_unique_ids", status_code=400)
    rows = [row for row in state["research_recommendations"] if row.get("recommendation_id") in set(safe_ids)]
    if len(rows) != len(safe_ids):
        _error("research_recommendation_not_found", status_code=404)
    if any(row.get("topic_id") != safe_topic for row in rows):
        _error("cross_topic_recommendation_reference")
    ordered = [_decorate_recommendation(state, next(row for row in rows if row["recommendation_id"] == value)) for value in safe_ids]
    fields = (
        "recommendation_type", "research_question", "proposed_study_design", "target_population",
        "target_context", "primary_outcomes", "supporting_gap_ids", "supporting_paper_ids",
        "corpus_relative_novelty_score", "feasibility_score", "significance_score",
        "falsifiability_score", "evidence_readiness_score", "timeliness_score", "overall_score",
        "confidence", "status", "global_novelty_status",
    )
    return {"topic_id": safe_topic, "recommendation_ids": safe_ids, "columns": [{key: row.get(key) for key in fields} | {"recommendation_id": row["recommendation_id"], "title": row["title"]} for row in ordered], "provider_calls": 0, "network_requests": 0}


def compare_analyses(topic_id: str, before_analysis_id: str, after_analysis_id: str) -> dict[str, Any]:
    from app.services import prototype_workspace as workspace

    state = workspace._load_state()
    safe_topic = workspace._safe_identifier(topic_id, code="topic_id_invalid")
    before_id = workspace._safe_identifier(before_analysis_id, code="analysis_id_invalid")
    after_id = workspace._safe_identifier(after_analysis_id, code="analysis_id_invalid")
    if before_id == after_id:
        _error("research_comparison_requires_distinct_analyses", status_code=400)
    analyses = {str(row["analysis_id"]): row for row in state["gap_analyses"] if row.get("analysis_id") in {before_id, after_id}}
    if set(analyses) != {before_id, after_id}:
        _error("research_analysis_not_found", status_code=404)
    before, after = analyses[before_id], analyses[after_id]
    if before.get("topic_id") != safe_topic or after.get("topic_id") != safe_topic:
        _error("cross_topic_research_analysis")
    if before.get("status") != "COMPLETED" or after.get("status") != "COMPLETED":
        _error("research_comparison_requires_completed_analyses")
    comparison_basis = {"topic_id": safe_topic, "before_analysis_id": before_id, "after_analysis_id": after_id}
    comparison_id = f"gap-comparison-{_digest(comparison_basis)[:28]}"
    existing = next((row for row in state["gap_comparisons"] if row.get("comparison_id") == comparison_id), None)
    if existing:
        return deepcopy(existing)
    before_gaps = {str(row["gap_key"]): row for row in state["research_gaps"] if row.get("analysis_id") == before_id}
    after_gaps = {str(row["gap_key"]): row for row in state["research_gaps"] if row.get("analysis_id") == after_id}
    before_recs = {str(row["recommendation_key"]): row for row in state["research_recommendations"] if row.get("analysis_id") == before_id}
    after_recs = {str(row["recommendation_key"]): row for row in state["research_recommendations"] if row.get("analysis_id") == after_id}
    retained_gap_keys = sorted(set(before_gaps) & set(after_gaps))
    retained_rec_keys = sorted(set(before_recs) & set(after_recs))
    after_snapshot = workspace._snapshot(state, str(after["snapshot_id"]), topic_id=safe_topic)
    attribution = "corpus_curation" if after_snapshot.get("parent_snapshot_id") == before.get("snapshot_id") else (
        "settings_or_scope_change" if before.get("scope_id") != after.get("scope_id") else
        "rule_or_scoring_policy_change" if (before.get("gap_rule_version"), before.get("scoring_policy_version")) != (after.get("gap_rule_version"), after.get("scoring_policy_version")) else
        "evidence_change"
    )
    record = {
        "comparison_id": comparison_id, "topic_id": safe_topic,
        "before_snapshot_id": before["snapshot_id"], "after_snapshot_id": after["snapshot_id"],
        "before_analysis_id": before_id, "after_analysis_id": after_id,
        "gaps_added": [after_gaps[key]["gap_id"] for key in sorted(set(after_gaps) - set(before_gaps))],
        "gaps_removed": [before_gaps[key]["gap_id"] for key in sorted(set(before_gaps) - set(after_gaps))],
        "gaps_retained": [{"gap_key": key, "before_gap_id": before_gaps[key]["gap_id"], "after_gap_id": after_gaps[key]["gap_id"]} for key in retained_gap_keys],
        "gaps_changed": [key for key in retained_gap_keys if (before_gaps[key]["independent_paper_ids"], before_gaps[key]["evidence_ids"], before_gaps[key]["confidence"]) != (after_gaps[key]["independent_paper_ids"], after_gaps[key]["evidence_ids"], after_gaps[key]["confidence"])],
        "recommendation_ids_added": [after_recs[key]["recommendation_id"] for key in sorted(set(after_recs) - set(before_recs))],
        "recommendation_ids_removed": [before_recs[key]["recommendation_id"] for key in sorted(set(before_recs) - set(after_recs))],
        "recommendation_score_deltas": [{"recommendation_key": key, "before_recommendation_id": before_recs[key]["recommendation_id"], "after_recommendation_id": after_recs[key]["recommendation_id"], "delta": int(after_recs[key]["overall_score"]) - int(before_recs[key]["overall_score"])} for key in retained_rec_keys],
        "support_paper_deltas": [{"recommendation_key": key, "delta": len(after_recs[key]["supporting_paper_ids"]) - len(before_recs[key]["supporting_paper_ids"])} for key in retained_rec_keys],
        "support_evidence_deltas": [{"recommendation_key": key, "delta": len(after_recs[key]["supporting_evidence_ids"]) - len(before_recs[key]["supporting_evidence_ids"])} for key in retained_rec_keys],
        "confidence_deltas": [{"recommendation_key": key, "before": before_recs[key]["confidence"], "after": after_recs[key]["confidence"]} for key in retained_rec_keys],
        "stale_transitions": [{"recommendation_id": row["recommendation_id"], "transition": "historical_not_current"} for row in before_recs.values()],
        "change_attribution": attribution,
        "interpretation": "Reduced accepted-corpus coverage may change corpus-bounded gaps and scores; historical evidence and recommendations remain unchanged.",
        "created_at": iso_now_utc(), "provider_calls": 0, "network_requests": 0,
    }
    record["comparison_digest"] = _record_digest(record, "comparison_digest")

    def mutation(current: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        found = next((row for row in current["gap_comparisons"] if row.get("comparison_id") == comparison_id), None)
        if found:
            return current, deepcopy(found)
        current["gap_comparisons"].append(record)
        return current, deepcopy(record)

    return workspace._mutate_state(mutation)


def export_analysis(topic_id: str, analysis_id: str) -> dict[str, Any]:
    detail = get_analysis(topic_id, analysis_id)
    if detail.get("status") != "COMPLETED":
        _error("research_export_requires_completed_analysis")
    lines = [
        f"# Research gaps and recommendations — {detail['analysis_id']}", "",
        "> All findings are bounded to the current accepted corpus. External novelty has not been assessed.",
        "> Recommendations are decision support, not scientific validation.", "",
        "## Lineage", "",
        f"- Topic: `{detail['topic_id']}`", f"- Snapshot: `{detail['snapshot_id']}`",
        f"- Run: `{detail['run_id']}`", f"- Report: `{detail['report_version_id']}`",
        f"- Scope: `{detail['scope_id']}` version {detail['scope_version']}",
        f"- Evidence matrix: `{detail['matrix_digest']}`", "", "## Evidence matrix", "",
        "| Paper | Design | Population | Context | Intervention | Outcomes | Follow-up | Limitations |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for profile in detail["study_profiles"]:
        lines.append("| " + " | ".join([
            str(profile["paper_id"]), str(profile["design_type"]), ", ".join(profile["population_tags"]) or "unknown",
            ", ".join(profile["context_tags"]) or "unknown", ", ".join(profile["intervention_tags"]) or "unknown",
            ", ".join(profile["outcome_tags"]) or "unknown", str(profile["follow_up_bucket"]),
            ", ".join(profile["limitation_codes"]) or "none identified",
        ]) + " |")
    lines.extend(["", "## Research gaps", ""])
    for gap in detail["gaps"]:
        lines.extend([f"### {gap['title']}", "", gap["bounded_statement"], "", f"Evidence: {', '.join(gap['evidence_ids']) or 'structural matrix/change event'}", ""])
    lines.extend(["## Candidate recommendations", ""])
    for recommendation in detail["recommendations"]:
        lines.extend([
            f"### {recommendation['title']}", "", recommendation["research_question"], "",
            f"- Status: `{recommendation['status']}`", f"- Score: {recommendation['overall_score']} ({recommendation['score_label']})",
            f"- Global novelty: `{recommendation['global_novelty_status']}`",
            f"- Supporting gaps: {', '.join(recommendation['supporting_gap_ids'])}",
            f"- Supporting evidence: {', '.join(recommendation['supporting_evidence_ids'])}",
            f"- Constraints: {', '.join(recommendation['limitations'])}", "",
        ])
    content = "\n".join(lines).strip() + "\n"
    return {
        "filename": f"research-gaps-{detail['analysis_id']}.md", "media_type": "text/markdown; charset=utf-8",
        "content": content, "content_digest": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "provider_calls": 0, "network_requests": 0,
    }


def decorate_topic(state: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    topic_id = str(detail["topic_id"])
    analyses = sorted(
        (deepcopy(row) for row in state["gap_analyses"] if row.get("topic_id") == topic_id),
        key=lambda row: (str(row.get("completed_at") or row.get("started_at") or ""), str(row.get("analysis_id") or "")),
        reverse=True,
    )
    analysis_details = [_analysis_detail(state, row) for row in analyses]
    current = next((row for row in analysis_details if row.get("snapshot_id") == detail.get("active_snapshot_id") and row.get("status") == "COMPLETED"), None)
    comparisons = sorted(
        (deepcopy(row) for row in state["gap_comparisons"] if row.get("topic_id") == topic_id),
        key=lambda row: str(row.get("created_at") or ""), reverse=True,
    )
    origin = next((deepcopy(row) for row in state["recommendation_origins"] if row.get("new_topic_id") == topic_id), None)
    return {
        **detail,
        "research_gaps_workspace": {
            "analyses": analysis_details, "current_analysis": current,
            "comparisons": comparisons, "promoted_origin": origin,
            "analysis_count": len(analyses),
            "gap_count": sum(int(row.get("gap_count") or 0) for row in analyses if row.get("status") == "COMPLETED"),
            "recommendation_count": sum(int(row.get("recommendation_count") or 0) for row in analyses if row.get("status") == "COMPLETED"),
            "provider_calls": 0, "network_requests": 0,
        },
    }


def workspace_totals(state: dict[str, Any]) -> dict[str, int]:
    completed = [row for row in state["gap_analyses"] if row.get("status") == "COMPLETED"]
    return {
        "research_gap_analyses": len(completed), "research_gaps": len(state["research_gaps"]),
        "research_recommendations": len(state["research_recommendations"]),
        "accepted_recommendations": sum(1 for row in state["research_recommendations"] if _current_status(state, row)[0] == "accepted"),
        "promoted_recommendations": len(state["recommendation_origins"]),
    }
