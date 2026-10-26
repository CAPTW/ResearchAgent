"""Atomic additive analysis records in the existing metadata store."""

from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, fields
import json
import sqlite3
from typing import Any

from app.db.bootstrap import connect_metadata_index
from app.domain import temporal_analysis as domain
from app.domain.temporal_memory import canonical_json
from app.repositories import temporal_memory as memory
from app.repositories.files import iso_now_utc

MODELS = {
    "analysis_policies": domain.AnalysisPolicy,
    "analysis_evaluations": domain.AnalysisEvaluation,
    "temporal_metric_points": domain.TemporalMetricPoint,
    "temporal_insight_records": domain.EvidenceBackedInsightRecord,
}
KEYS = {
    "analysis_policies": "policy_id",
    "analysis_evaluations": "evaluation_id",
    "temporal_metric_points": "metric_point_id",
    "temporal_insight_records": "insight_record_id",
}
SETS = {
    "enabled_metric_ids",
    "enabled_insight_kinds",
    "metric_point_ids",
    "source_entity_ids",
    "source_evidence_ids",
    "window",
}
INTEGERS = {
    "minimum_paper_count",
    "minimum_denominator",
    "minimum_absolute_count_delta",
    "minimum_percentage_point_delta_basis_points",
    "metric_point_count",
    "insight_record_count",
    "numerator",
    "denominator",
    "count_value",
    "source_entity_count",
    "source_evidence_count",
    "before_numerator",
    "before_denominator",
    "after_numerator",
    "after_denominator",
    "absolute_delta",
    "percentage_point_delta_basis_points",
}
NULLABLE = {
    "baseline_snapshot_id",
    "numerator",
    "denominator",
    "count_value",
    "before_numerator",
    "before_denominator",
    "after_numerator",
    "after_denominator",
    "absolute_delta",
    "percentage_point_delta_basis_points",
}


def checkpoint(phase: str) -> None:
    """Transaction failure injection seam; production has no side effect."""


def initialize(c: sqlite3.Connection) -> None:
    for table, model in MODELS.items():
        columns = [
            f"{f.name} {'INTEGER' if f.name in INTEGERS else 'TEXT'}"
            + (" PRIMARY KEY" if f.name == KEYS[table] else "" if f.name in NULLABLE else " NOT NULL")
            for f in fields(model)
        ]
        extra = {
            "temporal_metric_points": ", UNIQUE(evaluation_id,metric_id)",
            "temporal_insight_records": ", UNIQUE(evaluation_id,insight_kind)",
            "analysis_evaluations": ", UNIQUE(topic_id,snapshot_id,policy_id,formula_registry_version,snapshot_corpus_digest)",
        }.get(table, "")
        c.execute(f"CREATE TABLE IF NOT EXISTS {table} ({','.join(columns)}{extra})")
        for operation in ("UPDATE", "DELETE"):
            c.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} BEFORE {operation} ON {table}
                         BEGIN SELECT RAISE(ABORT,'analysis_record_immutable'); END""")
    c.execute("""CREATE TABLE IF NOT EXISTS topic_analysis_policy_bindings
                 (topic_id TEXT PRIMARY KEY, policy_id TEXT NOT NULL, activated_at TEXT NOT NULL)""")
    for operation in ("INSERT", "UPDATE"):
        c.execute(f"""CREATE TRIGGER IF NOT EXISTS analysis_binding_{operation.lower()}_guard
                     BEFORE {operation} ON topic_analysis_policy_bindings WHEN NOT EXISTS
                     (SELECT 1 FROM analysis_policies WHERE topic_id=NEW.topic_id AND policy_id=NEW.policy_id)
                     BEGIN SELECT RAISE(ABORT,'policy_binding_conflict'); END""")
    c.execute("""CREATE TRIGGER IF NOT EXISTS analysis_evaluation_reference_guard BEFORE INSERT ON analysis_evaluations
                 WHEN NOT EXISTS (SELECT 1 FROM analysis_policies WHERE policy_id=NEW.policy_id AND topic_id=NEW.topic_id)
                 OR NOT EXISTS (SELECT 1 FROM topic_snapshots WHERE snapshot_id=NEW.snapshot_id AND topic_id=NEW.topic_id
                   AND corpus_digest=NEW.snapshot_corpus_digest AND parent_snapshot_id IS NEW.baseline_snapshot_id)
                 BEGIN SELECT RAISE(ABORT,'evaluation_reference_invalid'); END""")
    for table, count in [
        ("temporal_metric_points", "metric_point_count"),
        ("temporal_insight_records", "insight_record_count"),
    ]:
        c.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_reference_guard BEFORE INSERT ON {table}
                     WHEN NOT EXISTS (SELECT 1 FROM analysis_evaluations WHERE evaluation_id=NEW.evaluation_id
                       AND topic_id=NEW.topic_id AND policy_id=NEW.policy_id AND snapshot_id=NEW.source_snapshot_id
                       AND baseline_snapshot_id IS NEW.baseline_snapshot_id)
                     OR (SELECT count(*) FROM {table} WHERE evaluation_id=NEW.evaluation_id) >=
                        (SELECT {count} FROM analysis_evaluations WHERE evaluation_id=NEW.evaluation_id)
                     BEGIN SELECT RAISE(ABORT,'analysis_reference_or_seal_invalid'); END""")


def _exists(c: sqlite3.Connection) -> bool:
    return (
        c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis_policies'").fetchone() is not None
    )


def _insert(c: sqlite3.Connection, table: str, record: Any) -> None:
    data = asdict(record)
    c.execute(
        f"INSERT INTO {table} ({','.join(data)}) VALUES ({','.join('?' for _ in data)})",
        [canonical_json(v) if k in SETS else v for k, v in data.items()],
    )


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {k: tuple(json.loads(v)) if k in SETS else v for k, v in dict(row).items()}


def _policy(c: sqlite3.Connection, topic: str, identity: str) -> domain.AnalysisPolicy:
    row = c.execute("SELECT * FROM analysis_policies WHERE policy_id=?", (identity,)).fetchone()
    if row is None:
        raise KeyError("policy_not_found")
    if row["topic_id"] != topic:
        raise ValueError("cross_topic_policy")
    return domain.AnalysisPolicy(**_row(row))


def create_policy(topic: str, config: dict[str, Any]) -> tuple[domain.AnalysisPolicy, bool]:
    p = domain.policy(topic, config, iso_now_utc())
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        initialize(c)
        if c.execute("SELECT 1 FROM analysis_policies WHERE policy_id=?", (p.policy_id,)).fetchone():
            return _policy(c, topic, p.policy_id), False
        _insert(c, "analysis_policies", p)
        return p, True


def activate(topic: str, policy_id: str) -> domain.AnalysisPolicy:
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        initialize(c)
        p = _policy(c, topic, policy_id)
        c.execute(
            """INSERT INTO topic_analysis_policy_bindings VALUES (?,?,?) ON CONFLICT(topic_id)
                     DO UPDATE SET policy_id=excluded.policy_id,activated_at=excluded.activated_at""",
            (topic, policy_id, iso_now_utc()),
        )
        return p


def _pagination(limit: int, page: int) -> None:
    if not 1 <= limit <= 100 or page < 1:
        raise ValueError("pagination_invalid")


def policies(topic: str, limit: int = 25, page: int = 1) -> dict[str, Any]:
    _pagination(limit, page)
    result: dict[str, Any] = dict(
        items=[], total=0, page=page, limit=limit, active_policy_id=None, active_policy=None, activated_at=None
    )
    with closing(connect_metadata_index(read_only=True)) as c:
        if not _exists(c):
            return result
        binding = c.execute("SELECT * FROM topic_analysis_policy_bindings WHERE topic_id=?", (topic,)).fetchone()
        if binding:
            result.update(active_policy_id=binding["policy_id"], activated_at=binding["activated_at"])
            result["active_policy"] = asdict(_policy(c, topic, binding["policy_id"]))
        result["items"] = [
            dict(_row(r), active=r["policy_id"] == result["active_policy_id"])
            for r in c.execute(
                "SELECT * FROM analysis_policies WHERE topic_id=? ORDER BY created_at DESC,policy_id ASC LIMIT ? OFFSET ?",
                (topic, limit, (page - 1) * limit),
            )
        ]
        result["total"] = c.execute("SELECT count(*) FROM analysis_policies WHERE topic_id=?", (topic,)).fetchone()[0]
    return result


def evaluate(topic: str, snapshot_id: str, policy_id: str | None = None) -> tuple[domain.AnalysisEvaluation, bool]:
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        initialize(c)
        if not memory._exists(c):
            raise KeyError("snapshot_not_found")
        current = memory._load(c, snapshot_id)
        if current.topic_id != topic:
            raise ValueError("cross_topic_snapshot")
        if policy_id is None:
            binding = c.execute(
                "SELECT policy_id FROM topic_analysis_policy_bindings WHERE topic_id=?", (topic,)
            ).fetchone()
            if not binding:
                raise ValueError("no_active_policy")
            policy_id = binding[0]
        p = _policy(c, topic, policy_id)
        baseline = memory._load(c, current.parent_snapshot_id) if current.parent_snapshot_id else None
        evaluation, points, records = domain.evaluate(p, current, baseline, iso_now_utc())
        existing = c.execute(
            "SELECT * FROM analysis_evaluations WHERE evaluation_id=?", (evaluation.evaluation_id,)
        ).fetchone()
        if existing:
            return domain.AnalysisEvaluation(**_row(existing)), False
        _insert(c, "analysis_evaluations", evaluation)
        checkpoint("after_evaluation")
        for point in points:
            _insert(c, "temporal_metric_points", point)
        checkpoint("after_metrics")
        for record in records:
            _insert(c, "temporal_insight_records", record)
        checkpoint("after_insights")
        return evaluation, True


def _listing(
    topic: str, table: str, limit: int, page: int, metric: str | None = None, snapshot_id: str | None = None
) -> dict[str, Any]:
    _pagination(limit, page)
    result: dict[str, Any] = dict(items=[], total=0, limit=limit, page=page)
    with closing(connect_metadata_index(read_only=True)) as c:
        if not _exists(c):
            return result
        condition, args = "a.topic_id=?", [topic]
        if metric is not None:
            condition += " AND a.metric_id=?"
            args.append(metric)
        if snapshot_id is not None:
            condition += " AND a.source_snapshot_id=?"
            args.append(snapshot_id)
        result["total"] = c.execute(f"SELECT count(*) FROM {table} a WHERE {condition}", args).fetchone()[0]
        # A policy may exist before the first M3-A capture creates snapshot tables.
        # Empty resources need no join and must not bootstrap storage from GET.
        if result["total"] == 0:
            return result
        result["items"] = [
            _row(r)
            for r in c.execute(
                f"""SELECT a.*,s.created_at AS snapshot_created_at FROM {table} a
                JOIN topic_snapshots s ON a.source_snapshot_id=s.snapshot_id WHERE {condition}
                ORDER BY s.created_at ASC,s.snapshot_id ASC,a.{KEYS[table]} ASC LIMIT ? OFFSET ?""",
                [*args, limit, (page - 1) * limit],
            )
        ]
    return result


def series(topic: str, metric_id: str, limit: int = 25, page: int = 1, snapshot_id: str | None = None) -> dict[str, Any]:
    if metric_id not in domain.METRIC_IDS:
        raise ValueError("invalid_metric_id")
    return _listing(topic, "temporal_metric_points", limit, page, metric_id, snapshot_id)


def insights(topic: str, limit: int = 25, page: int = 1, snapshot_id: str | None = None) -> dict[str, Any]:
    return _listing(topic, "temporal_insight_records", limit, page, snapshot_id=snapshot_id)


def _insight(c: sqlite3.Connection, topic: str, identity: str) -> sqlite3.Row:
    row = c.execute("SELECT * FROM temporal_insight_records WHERE topic_id=? AND insight_record_id=?",
                    (topic, identity)).fetchone() if _exists(c) else None
    if row is None:
        raise KeyError("insight_not_found")
    return row


def _decisions_exist(c: sqlite3.Connection) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='temporal_insight_decisions'").fetchone() is not None


def decisions(topic: str, insight_id: str, limit: int = 25, page: int = 1) -> dict[str, Any]:
    """Read history without creating a schema or recalculating the insight."""
    _pagination(limit, page)
    result: dict[str, Any] = dict(items=[], total=0, latest_id=None, limit=limit, page=page)
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        _insight(c, topic, insight_id)
        if not _decisions_exist(c):
            return result
        args = (topic, insight_id)
        result["total"] = c.execute("SELECT count(*) FROM temporal_insight_decisions WHERE topic_id=? AND insight_record_id=?", args).fetchone()[0]
        latest = c.execute("SELECT decision_id FROM temporal_insight_decisions WHERE topic_id=? AND insight_record_id=? ORDER BY sequence DESC LIMIT 1", args).fetchone()
        result["latest_id"] = latest[0] if latest else None
        result["items"] = [dict(row) for row in c.execute(
            "SELECT * FROM temporal_insight_decisions WHERE topic_id=? AND insight_record_id=? ORDER BY sequence DESC LIMIT ? OFFSET ?",
            (*args, limit, (page - 1) * limit))]
    return result


def decide(topic: str, insight_id: str, request_id: str, state: str, note: str,
           expected_previous_id: str | None, actor_id: str) -> tuple[dict[str, Any], bool]:
    """Append one human decision; retries replay, competing edits conflict."""
    if state not in {"accepted", "held", "rejected"} or not 1 <= len(request_id) <= 100 or len(note) > 2000 or not actor_id:
        raise ValueError("invalid_decision")
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        insight = _insight(c, topic, insight_id)
        if state == "accepted" and insight["state"] != "ACCEPTED":
            raise ValueError("insight_not_eligible")
        c.execute("""CREATE TABLE IF NOT EXISTS temporal_insight_decisions (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id TEXT NOT NULL UNIQUE, topic_id TEXT NOT NULL,
            insight_record_id TEXT NOT NULL,
            request_id TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('accepted','held','rejected')),
            note TEXT NOT NULL CHECK(length(note)<=2000), previous_id TEXT,
            actor_id TEXT NOT NULL, created_at TEXT NOT NULL,
            schema_version TEXT NOT NULL CHECK(schema_version='M3C_INSIGHT_DECISION_V1'),
            UNIQUE(topic_id,insight_record_id,request_id))""")
        for operation in ("UPDATE", "DELETE"):
            c.execute(f"""CREATE TRIGGER IF NOT EXISTS insight_decisions_no_{operation.lower()}
                BEFORE {operation} ON temporal_insight_decisions
                BEGIN SELECT RAISE(ABORT,'decision_immutable'); END""")
        c.execute("""CREATE TRIGGER IF NOT EXISTS insight_decisions_binding BEFORE INSERT ON temporal_insight_decisions
            WHEN NOT EXISTS (SELECT 1 FROM temporal_insight_records WHERE insight_record_id=NEW.insight_record_id AND topic_id=NEW.topic_id)
            OR (NEW.state='accepted' AND NOT EXISTS (SELECT 1 FROM temporal_insight_records WHERE insight_record_id=NEW.insight_record_id AND state='ACCEPTED'))
            OR NEW.previous_id IS NOT (SELECT decision_id FROM temporal_insight_decisions WHERE topic_id=NEW.topic_id AND insight_record_id=NEW.insight_record_id ORDER BY sequence DESC LIMIT 1)
            BEGIN SELECT RAISE(ABORT,'decision_binding_invalid'); END""")
        existing = c.execute("SELECT * FROM temporal_insight_decisions WHERE topic_id=? AND insight_record_id=? AND request_id=?",
                             (topic, insight_id, request_id)).fetchone()
        if existing:
            if (existing["state"], existing["note"], existing["previous_id"], existing["actor_id"]) != (state, note, expected_previous_id, actor_id):
                raise ValueError("decision_request_conflict")
            return dict(existing), False
        latest = c.execute("SELECT decision_id FROM temporal_insight_decisions WHERE topic_id=? AND insight_record_id=? ORDER BY sequence DESC LIMIT 1",
                           (topic, insight_id)).fetchone()
        if (latest[0] if latest else None) != expected_previous_id:
            raise ValueError("decision_conflict")
        identity = "idec_" + domain.digest([topic, insight_id, request_id])
        c.execute("""INSERT INTO temporal_insight_decisions
            (decision_id,topic_id,insight_record_id,request_id,state,note,previous_id,actor_id,created_at,schema_version)
            VALUES (?,?,?,?,?,?,?,?,?,'M3C_INSIGHT_DECISION_V1')""",
            (identity, topic, insight_id, request_id, state, note, expected_previous_id, actor_id, iso_now_utc()))
        checkpoint("after_decision")
        row = c.execute("SELECT * FROM temporal_insight_decisions WHERE decision_id=?", (identity,)).fetchone()
        return dict(row), True
