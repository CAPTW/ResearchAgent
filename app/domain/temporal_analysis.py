"""Versioned, deterministic measurements of captured local state. No IO or inference."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from fractions import Fraction
import re
import unicodedata
from typing import Any

from app.domain.temporal_memory import TopicSnapshot, Member, EVENT_TYPES, canonical_json, digest, diff

REGISTRY = "M3B_METRIC_FORMULA_REGISTRY_V1"
SCHEMA = "M3B_ANALYSIS_POLICY_V1"
TEMPLATE = "M3B_FACTUAL_INSIGHT_TEMPLATE_V1"
ACCEPTANCE = "ACCEPTED_LOCAL_DETERMINISTIC"
METRIC_IDS = (
    "corpus.paper_count",
    "corpus.paper_version_count",
    "coverage.reviewed_paper_ratio",
    "coverage.full_text_available_ratio",
    "coverage.evidence_backed_paper_ratio",
    "coverage.rights_known_ratio",
    *(f"delta.{kind.lower()}_count" for kind in EVENT_TYPES),
)
INSIGHT_KINDS = (
    "CORPUS_SIZE_CHANGE",
    "REVIEW_COVERAGE_CHANGE",
    "FULL_TEXT_COVERAGE_CHANGE",
    "EVIDENCE_COVERAGE_CHANGE",
    "RIGHTS_KNOWN_COVERAGE_CHANGE",
    "PAPER_VERSION_ACTIVITY",
)
INSIGHT_METRICS = dict(zip(INSIGHT_KINDS, (METRIC_IDS[0], *METRIC_IDS[2:6], METRIC_IDS[8]), strict=True))


@dataclass(frozen=True)
class Formula:
    metric_id: str
    unit: str
    formula_version: str = "1"


FORMULAS = {key: Formula(key, "RATIO" if key.startswith("coverage.") else "COUNT") for key in METRIC_IDS}


@dataclass(frozen=True)
class AnalysisPolicy:
    policy_id: str
    topic_id: str
    created_at: str
    schema_version: str = SCHEMA
    formula_registry_version: str = REGISTRY
    baseline_mode: str = "PREVIOUS_SNAPSHOT"
    enabled_metric_ids: tuple[str, ...] = METRIC_IDS
    enabled_insight_kinds: tuple[str, ...] = INSIGHT_KINDS
    minimum_paper_count: int = 1
    minimum_denominator: int = 1
    minimum_absolute_count_delta: int = 1
    minimum_percentage_point_delta_basis_points: int = 100
    factual_template_version: str = TEMPLATE
    acceptance_state: str = ACCEPTANCE


def policy(topic_id: str, config: dict[str, Any], created_at: str) -> AnalysisPolicy:
    defaults = asdict(AnalysisPolicy("", topic_id, created_at))
    for key in ("policy_id", "topic_id", "created_at", "acceptance_state"):
        defaults.pop(key)
    if set(config) - defaults.keys():
        raise ValueError("policy_unknown_field")
    defaults.update(config)
    for key, expected in [
        ("schema_version", SCHEMA),
        ("formula_registry_version", REGISTRY),
        ("baseline_mode", "PREVIOUS_SNAPSHOT"),
        ("factual_template_version", TEMPLATE),
    ]:
        if defaults[key] != expected:
            raise ValueError("unsupported_policy_schema")
    for key, allowed in [("enabled_metric_ids", METRIC_IDS), ("enabled_insight_kinds", INSIGHT_KINDS)]:
        values = defaults[key]
        if not isinstance(values, (list, tuple)) or any(not isinstance(v, str) or v not in allowed for v in values):
            raise ValueError("invalid_metric_id" if key == "enabled_metric_ids" else "invalid_insight_kind")
        defaults[key] = tuple(sorted(set(values)))
    for key in (
        "minimum_paper_count",
        "minimum_denominator",
        "minimum_absolute_count_delta",
        "minimum_percentage_point_delta_basis_points",
    ):
        value = defaults[key]
        if type(value) is not int or not 0 <= value <= (10000 if key.endswith("basis_points") else 1000000):
            raise ValueError("invalid_threshold")
    # Canonical JSON validates Unicode, collisions and non-finite data before persistence.
    topic_id = unicodedata.normalize("NFC", topic_id)
    identity = digest(dict(topic_id=topic_id, **defaults))
    return AnalysisPolicy("apol_" + identity, topic_id, created_at, **defaults)


@dataclass(frozen=True)
class AnalysisEvaluation:
    evaluation_id: str
    topic_id: str
    snapshot_id: str
    baseline_snapshot_id: str | None
    policy_id: str
    formula_registry_version: str
    created_at: str
    snapshot_corpus_digest: str
    state: str
    metric_point_count: int
    insight_record_count: int
    acceptance_state: str = ACCEPTANCE


@dataclass(frozen=True)
class TemporalMetricPoint:
    metric_point_id: str
    evaluation_id: str
    topic_id: str
    policy_id: str
    metric_id: str
    source_snapshot_id: str
    baseline_snapshot_id: str | None
    formula_registry_version: str
    formula_version: str
    calculated_at: str
    state: str
    numerator: int | None
    denominator: int | None
    count_value: int | None
    unit: str
    source_entity_count: int
    source_entity_digest: str
    source_evidence_count: int
    source_evidence_digest: str
    reason_code: str
    acceptance_state: str = ACCEPTANCE


@dataclass(frozen=True)
class EvidenceBackedInsightRecord:
    insight_record_id: str
    evaluation_id: str
    topic_id: str
    policy_id: str
    insight_kind: str
    source_snapshot_id: str
    baseline_snapshot_id: str | None
    metric_point_ids: tuple[str, ...]
    source_entity_ids: tuple[str, ...]
    source_evidence_ids: tuple[str, ...]
    source_entity_digest: str
    source_evidence_digest: str
    before_numerator: int | None
    before_denominator: int | None
    after_numerator: int | None
    after_denominator: int | None
    absolute_delta: int | None
    percentage_point_delta_basis_points: int | None
    template_version: str
    rendered_factual_text: str
    calculated_at: str
    state: str
    reason_code: str
    window: tuple[str | None, str]
    formula_registry_version: str = REGISTRY
    formula_version: str = "1"
    confidence_class: str = "DETERMINISTIC_EXACT"
    acceptance_state: str = ACCEPTANCE


def _sha(value: str | None) -> bool:
    return bool(value and re.fullmatch("[0-9a-fA-F]{64}", value))


def _qualifies(member: Member, metric: str) -> bool | None:
    if metric == METRIC_IDS[2]:
        if member.review_state in {"abstract_reviewed", "full_text_available", "full_text_reviewed"}:
            return True
        return False if member.review_state == "not_reviewed" else None
    if metric == METRIC_IDS[3]:
        if member.full_text_state in {"full_text_available", "reviewed"}:
            return True if _sha(member.full_text_content_digest) else None
        return False if member.full_text_state == "abstract_only" else None
    if metric == METRIC_IDS[4]:
        if member.evidence_state != "available":
            return None
        ids = set(member.evidence_ids)
        if any(not key.strip() for key in ids):
            return None
        valid = {key for key, value in member.evidence_digests if _sha(value)}
        if ids and not ids <= valid:
            return None
        return bool(ids)
    if member.rights_access_state in {"open_access", "restricted", "declared"}:
        return True
    return None


def value(snapshot: TopicSnapshot, metric: str) -> tuple[str, int | None, int | None, int | None, str]:
    """state, exact numerator, exact denominator, count, reason."""
    works = {m.canonical_paper_id for m in snapshot.members}
    if metric in METRIC_IDS[:2]:
        count = len(works) if metric == METRIC_IDS[0] else len({m.paper_version_id for m in snapshot.members})
        return "AVAILABLE", None, None, count, "EXACT"
    if not works:
        return "INSUFFICIENT_DENOMINATOR", None, 0, None, "ZERO_DENOMINATOR"
    total = 0
    for work in sorted(works):
        states = [_qualifies(m, metric) for m in snapshot.members if m.canonical_paper_id == work]
        if True in states:
            total += 1
        elif None in states:
            return "UNAVAILABLE", None, len(works), None, metric.split(".")[1].upper() + "_AUTHORITY_UNAVAILABLE"
    return "AVAILABLE", total, len(works), None, "EXACT"


def evaluate(
    p: AnalysisPolicy, current: TopicSnapshot, baseline: TopicSnapshot | None, clock: str
) -> tuple[AnalysisEvaluation, tuple[TemporalMetricPoint, ...], tuple[EvidenceBackedInsightRecord, ...]]:
    if current.topic_id != p.topic_id or baseline and baseline.topic_id != p.topic_id:
        raise ValueError("cross_topic_snapshot")
    if current.schema_version != 1 or baseline and baseline.schema_version != 1:
        raise ValueError("unsupported_snapshot_schema")
    if current.parent_snapshot_id != (baseline.snapshot_id if baseline else None):
        raise ValueError("snapshot_baseline_conflict")
    base_id = baseline.snapshot_id if baseline else None
    preimage: dict[str, Any] = dict(
        topic_id=p.topic_id,
        snapshot_id=current.snapshot_id,
        baseline_snapshot_id=base_id,
        policy_id=p.policy_id,
        formula_registry_version=REGISTRY,
        snapshot_corpus_digest=current.corpus_digest,
    )
    evaluation_id = "aeval_" + digest(preimage)
    members = (*current.members, *(baseline.members if baseline else ()))
    entities = tuple(
        sorted(
            {
                x
                for m in members
                for x in (m.canonical_paper_id, m.paper_version_id, *m.occurrence_ids, *m.source_entity_ids)
            }
        )
    )
    evidence = tuple(sorted({x for m in members for x in m.evidence_ids}))
    entity_digest = digest(entities)
    evidence_digest = digest(sorted({entry for m in members for entry in m.evidence_digests}))
    events = diff(baseline, current) if baseline else ()
    points: list[TemporalMetricPoint] = []
    numerator: int | None
    denominator: int | None
    count: int | None
    for metric in METRIC_IDS:
        if metric not in p.enabled_metric_ids:
            continue
        if metric.startswith("delta."):
            kind = metric.removeprefix("delta.").removesuffix("_count").upper()
            state, numerator, denominator, count, reason = (
                ("AVAILABLE", None, None, sum(e.event_type == kind for e in events), "EXACT")
                if baseline
                else ("INSUFFICIENT_BASELINE", None, None, None, "PREVIOUS_SNAPSHOT_REQUIRED")
            )
        else:
            state, numerator, denominator, count, reason = value(current, metric)
        data: dict[str, Any] = dict(
            evaluation_id=evaluation_id,
            topic_id=p.topic_id,
            policy_id=p.policy_id,
            metric_id=metric,
            source_snapshot_id=current.snapshot_id,
            baseline_snapshot_id=base_id,
            formula_registry_version=REGISTRY,
            formula_version=FORMULAS[metric].formula_version,
            state=state,
            numerator=numerator,
            denominator=denominator,
            count_value=count,
            unit=FORMULAS[metric].unit,
            source_entity_count=len(entities),
            source_entity_digest=entity_digest,
            source_evidence_count=len(evidence),
            source_evidence_digest=evidence_digest,
            reason_code=reason,
        )
        points.append(TemporalMetricPoint(metric_point_id="tmetric_" + digest(data), calculated_at=clock, **data))
    by_metric = {point.metric_id: point for point in points}
    records: list[EvidenceBackedInsightRecord] = []
    for kind in INSIGHT_KINDS:
        if kind not in p.enabled_insight_kinds:
            continue
        metric = INSIGHT_METRICS[kind]
        point = by_metric.get(metric)
        before_n = before_d = after_n = after_d = absolute = basis_points = None
        if point:
            after_n, after_d = (
                (point.count_value, 1) if point.unit == "COUNT" else (point.numerator, point.denominator)
            )
        state, reason = "INSUFFICIENT_DATA", "PREVIOUS_SNAPSHOT_REQUIRED"
        exact_delta: Fraction | None = None
        if not point:
            state, reason = "UNAVAILABLE", "METRIC_DISABLED"
        elif baseline:
            if kind == "PAPER_VERSION_ACTIVITY":
                before_n, after_n = 0, point.count_value
                before_d = after_d = 1
                source_state, source_reason = point.state, point.reason_code
            else:
                bs, bn, bd, bc, br = value(baseline, metric)
                before_n, before_d = (bc, 1) if point.unit == "COUNT" else (bn, bd)
                after_n, after_d = (
                    (point.count_value, 1) if point.unit == "COUNT" else (point.numerator, point.denominator)
                )
                source_state, source_reason = (bs, br) if bs != "AVAILABLE" else (point.state, point.reason_code)
            if source_state == "UNAVAILABLE":
                state, reason = "UNAVAILABLE", source_reason
            elif source_state != "AVAILABLE" or not before_d or not after_d:
                state, reason = "INSUFFICIENT_DATA", source_reason
            elif before_n is not None and after_n is not None:
                if point.unit == "COUNT":
                    absolute = after_n - before_n
                    threshold_pass = abs(absolute) >= p.minimum_absolute_count_delta
                else:
                    exact_delta = (Fraction(after_n, after_d) - Fraction(before_n, before_d)) * 10000
                    basis_points = int(exact_delta)  # toward zero; thresholds always use exact Fraction
                    threshold_pass = abs(exact_delta) >= p.minimum_percentage_point_delta_basis_points
                papers = min(
                    len({m.canonical_paper_id for m in baseline.members}),
                    len({m.canonical_paper_id for m in current.members}),
                )
                if papers < p.minimum_paper_count or (
                    point.unit == "RATIO" and min(before_d, after_d) < p.minimum_denominator
                ):
                    state, reason = "INSUFFICIENT_DATA", "POLICY_MINIMUM_NOT_MET"
                elif not entities or any(not m.membership_digest for m in members):
                    state, reason = "UNAVAILABLE", "PROVENANCE_INCOMPLETE"
                else:
                    state, reason = (
                        ("ACCEPTED", "THRESHOLD_MET")
                        if threshold_pass
                        else ("BELOW_POLICY_THRESHOLD", "THRESHOLD_NOT_MET")
                    )
        if before_n is not None and after_n is not None and before_d and after_d:
            if kind == "PAPER_VERSION_ACTIVITY":
                text = f"{after_n} paper versions changed between the selected snapshots."
            elif point and point.unit == "COUNT":
                text = f"Captured paper count changed from {before_n} to {after_n}; count delta {absolute if absolute is not None else 'unavailable'}."
            else:
                # Exact rational prose avoids decimal rounding in persisted identity or evidence.
                text = f"{metric} changed from {before_n}/{before_d} to {after_n}/{after_d}."
        else:
            text = f"{kind}: {state}; {reason}."
        data = dict(
            evaluation_id=evaluation_id,
            topic_id=p.topic_id,
            policy_id=p.policy_id,
            insight_kind=kind,
            source_snapshot_id=current.snapshot_id,
            baseline_snapshot_id=base_id,
            metric_point_ids=(point.metric_point_id,) if point else (),
            source_entity_ids=entities,
            source_evidence_ids=evidence,
            source_entity_digest=entity_digest,
            source_evidence_digest=evidence_digest,
            before_numerator=before_n,
            before_denominator=before_d,
            after_numerator=after_n,
            after_denominator=after_d,
            absolute_delta=absolute,
            percentage_point_delta_basis_points=basis_points,
            template_version=TEMPLATE,
            state=state,
            reason_code=reason,
        )
        canonical_json(data)
        records.append(
            EvidenceBackedInsightRecord(
                insight_record_id="insight_" + digest(data),
                rendered_factual_text=text,
                calculated_at=clock,
                window=(baseline.created_at if baseline else None, current.created_at),
                **data,
            )
        )
    evaluation = AnalysisEvaluation(
        **preimage,
        evaluation_id=evaluation_id,
        created_at=clock,
        state="COMPLETED",
        metric_point_count=len(points),
        insight_record_count=len(records),
    )
    return evaluation, tuple(points), tuple(records)
