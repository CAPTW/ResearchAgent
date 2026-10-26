"""Human-reviewed local reports bound to frozen synthesis, not synthetic research runs.

Report publication and the accepting decision use one SQLite transaction. The
candidate, stored report and review history are distinct immutable records.
"""
from contextlib import closing
import hashlib
import json
import re
import sqlite3
from typing import Any

from app.db.bootstrap import connect_metadata_index
from app.domain.temporal_memory import digest
from app.domain.local_synthesis import checkpoint_digest, encoded
from app.repositories import local_synthesis
from app.repositories.files import iso_now_utc

SCHEMA = "LOCAL_EVIDENCE_REPORT_V1"
DECISION_SCHEMA = "LOCAL_EVIDENCE_REPORT_DECISION_V1"


def checkpoint(phase: str) -> None:
    """Transaction fault-injection seam."""


def _exists(c: sqlite3.Connection) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE name='synthesis_report_decisions' AND type='table'").fetchone() is not None


def _initialize(c: sqlite3.Connection) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS synthesis_report_versions (
        job_id TEXT PRIMARY KEY, topic_id TEXT NOT NULL, version_id TEXT NOT NULL UNIQUE,
        payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS synthesis_report_decisions (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT NOT NULL UNIQUE,
        topic_id TEXT NOT NULL, job_id TEXT NOT NULL, request_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('accepted','held','rejected')),
        note TEXT NOT NULL CHECK(length(note)<=2000), previous_id TEXT, actor_id TEXT NOT NULL,
        created_at TEXT NOT NULL, candidate_digest TEXT NOT NULL, schema_version TEXT NOT NULL,
        UNIQUE(topic_id,job_id,request_id))""")
    c.execute("CREATE INDEX IF NOT EXISTS synthesis_report_decisions_job ON synthesis_report_decisions(topic_id,job_id,sequence)")
    for table in ("synthesis_report_versions", "synthesis_report_decisions"):
        for operation in ("UPDATE", "DELETE"):
            c.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} BEFORE {operation} ON {table}
                BEGIN SELECT RAISE(ABORT,'synthesis_report_immutable'); END""")
    c.execute("""CREATE TRIGGER IF NOT EXISTS synthesis_report_version_binding BEFORE INSERT ON synthesis_report_versions
        WHEN NOT EXISTS (SELECT 1 FROM local_synthesis_jobs WHERE job_id=NEW.job_id AND topic_id=NEW.topic_id AND state='CANDIDATE_READY')
        BEGIN SELECT RAISE(ABORT,'synthesis_report_binding_invalid'); END""")
    c.execute("""CREATE TRIGGER IF NOT EXISTS synthesis_report_decision_binding BEFORE INSERT ON synthesis_report_decisions
        WHEN NOT EXISTS (SELECT 1 FROM local_synthesis_jobs WHERE job_id=NEW.job_id AND topic_id=NEW.topic_id AND state='CANDIDATE_READY')
        OR NEW.previous_id IS NOT (SELECT decision_id FROM synthesis_report_decisions
            WHERE topic_id=NEW.topic_id AND job_id=NEW.job_id ORDER BY sequence DESC LIMIT 1)
        OR (NEW.state='accepted' AND NOT EXISTS (SELECT 1 FROM synthesis_report_versions WHERE job_id=NEW.job_id AND topic_id=NEW.topic_id))
        BEGIN SELECT RAISE(ABORT,'synthesis_report_binding_invalid'); END""")


def _candidate(c: sqlite3.Connection, topic: str, job_id: str) -> dict[str, Any]:
    job = local_synthesis._read(c, topic, job_id)
    if job["state"] != "CANDIDATE_READY":
        raise ValueError("synthesis_not_ready")
    validation = job["steps"][-1]["output"]
    if (validation.get("kind") != "VALIDATE" or validation.get("valid") is not True
            or validation.get("method") != "FROZEN_REFERENCE_CHECK_V1"
            or job["candidate"].get("mode") != "LOCAL_EXTRACTIVE_DRAFT"
            or validation.get("input_digest") != job["input_digest"]
            or validation.get("candidate_digest") != digest(job["candidate"])):
        raise ValueError("synthesis_integrity_invalid")
    return job


def _latest(c: sqlite3.Connection, topic: str, job_id: str) -> dict[str, Any] | None:
    row = c.execute("SELECT * FROM synthesis_report_decisions WHERE topic_id=? AND job_id=? ORDER BY sequence DESC LIMIT 1", (topic, job_id)).fetchone()
    return dict(row) if row else None


def _version(c: sqlite3.Connection, job: dict[str, Any]) -> dict[str, Any] | None:
    row = c.execute("SELECT * FROM synthesis_report_versions WHERE topic_id=? AND job_id=?", (job["topic_id"], job["job_id"])).fetchone()
    if row is None:
        return None
    value = json.loads(row["payload_json"])
    if (checkpoint_digest(value, job["inputs"]) != row["payload_digest"] or value["version_id"] != row["version_id"]
            or value["input_digest"] != job["input_digest"] or value["candidate_digest"] != digest(job["candidate"])
            or value["content_sha256"] != hashlib.sha256(value["content"].encode("utf-8")).hexdigest()):
        raise ValueError("synthesis_integrity_invalid")
    return value


def history(topic: str, job_id: str, limit: int = 10, page: int = 1) -> dict[str, Any]:
    if not 1 <= limit <= 100 or page < 1:
        raise ValueError("pagination_invalid")
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        _candidate(c, topic, job_id)
        result = dict(items=[], total=0, limit=limit, page=page, latest_id=None)
        if _exists(c):
            args = (topic, job_id)
            result["total"] = c.execute("SELECT count(*) FROM synthesis_report_decisions WHERE topic_id=? AND job_id=?", args).fetchone()[0]
            latest = _latest(c, topic, job_id)
            result["latest_id"] = latest["decision_id"] if latest else None
            result["items"] = [dict(row) for row in c.execute("""SELECT * FROM synthesis_report_decisions
                WHERE topic_id=? AND job_id=? ORDER BY sequence DESC LIMIT ? OFFSET ?""", (*args, limit, (page - 1) * limit))]
        return result


def _report(job: dict[str, Any], decision_id: str, actor_id: str, now: str) -> dict[str, Any]:
    candidate = job["candidate"]
    version_id = "lrep_" + digest([SCHEMA, job["job_id"], digest(candidate)])
    content = "\n".join([
        "# 로컬 근거 보고서", "", "사람이 보존·활용 대상으로 채택한 로컬 근거 초안입니다. 과학적 결론의 검증이나 공개·배포를 뜻하지 않습니다.",
        f"- 보고서 버전: {version_id}", f"- 고정 입력: {job['input_digest']}",
        f"- 주제 스냅샷: {job['inputs']['source_snapshot_id']}", f"- 최초 채택 기록: {decision_id}",
        f"- 저장 시각: {now}", "", "## 채택한 초안 원문", "", candidate["markdown"],
        "## 출처 식별자와 근거 위치", "",
    ])
    # Render source data as JSON within a fence longer than any backtick run in
    # the data. The accompanying JSON export contains the complete frozen input.
    sources = [dict(paper_version_id=p["paper_version_id"], source_identity=p["source_identity"],
                    metadata_digest=p["metadata_digest"], review_source_run_id=p["review_source_run_id"],
                    source_text_digest=p["source_text_digest"], rights=p["rights"],
                    evidence=[{key: value for key, value in e.items() if key != "text"} for e in p["evidence"]])
               for p in job["inputs"]["papers"]]
    encoded = json.dumps(sources, ensure_ascii=False, indent=2)
    fence = "`" * max(3, 1 + max((len(m.group()) for m in re.finditer(r"`+", encoded)), default=0))
    content += f"{fence}json\n{encoded}\n{fence}\n"
    return dict(schema_version=SCHEMA, report_id=job["job_id"], version_id=version_id, version_number=1,
                topic_id=job["topic_id"], created_at=now, actor_id=actor_id, acceptance_decision_id=decision_id,
                input_digest=job["input_digest"], candidate_digest=digest(candidate), mode=candidate["mode"],
                source_snapshot_id=job["inputs"]["source_snapshot_id"], coverage=candidate["coverage"],
                references=candidate["references"], content=content,
                content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest())


def decide(topic: str, job_id: str, *, request_id: str, state: str, note: str,
           expected_previous_id: str | None, expected_binding: str, actor_id: str) -> tuple[dict[str, Any], bool]:
    if state not in {"accepted", "held", "rejected"} or not 1 <= len(request_id) <= 100 or len(note) > 2000 or not actor_id:
        raise ValueError("invalid_decision")
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        job = _candidate(c, topic, job_id)
        _initialize(c)
        fields = dict(state=state, note=note, previous_id=expected_previous_id, candidate_digest=expected_binding, actor_id=actor_id)
        prior = c.execute("SELECT * FROM synthesis_report_decisions WHERE topic_id=? AND job_id=? AND request_id=?", (topic, job_id, request_id)).fetchone()
        if prior:
            if any(prior[key] != value for key, value in fields.items()):
                raise ValueError("decision_request_conflict")
            return dict(prior), False
        latest = _latest(c, topic, job_id)
        if (latest["decision_id"] if latest else None) != expected_previous_id:
            raise ValueError("decision_conflict")
        if expected_binding != digest(job["candidate"]):
            raise ValueError("synthesis_binding_changed")
        decision_id = "lsdec_" + digest([topic, job_id, request_id])
        now = iso_now_utc()
        if state == "accepted" and _version(c, job) is None:
            version = _report(job, decision_id, actor_id, now)
            c.execute("INSERT INTO synthesis_report_versions VALUES (?,?,?,?,?)",
                      (job_id, topic, version["version_id"], encoded(version, job["inputs"]), checkpoint_digest(version, job["inputs"])))
            checkpoint("after_report")
        c.execute("""INSERT INTO synthesis_report_decisions
            (decision_id,topic_id,job_id,request_id,state,note,previous_id,actor_id,created_at,candidate_digest,schema_version)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (decision_id, topic, job_id, request_id, state, note, expected_previous_id,
                                               actor_id, now, expected_binding, DECISION_SCHEMA))
        checkpoint("after_decision")
        return dict(c.execute("SELECT * FROM synthesis_report_decisions WHERE decision_id=?", (decision_id,)).fetchone()), True


def detail(topic: str, job_id: str) -> dict[str, Any]:
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        job = _candidate(c, topic, job_id)
        version = _version(c, job) if _exists(c) else None
        latest = _latest(c, topic, job_id) if _exists(c) else None
        return dict(version=version, latest_decision=latest, candidate_digest=digest(job["candidate"]),
                    export_eligible=bool(version and latest and latest["state"] == "accepted"))


def _saved(c: sqlite3.Connection, topic: str, job_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    job = _candidate(c, topic, job_id)
    version = _version(c, job) if _exists(c) else None
    if version is None:
        raise KeyError(job_id)
    return job, version


def _summary(c: sqlite3.Connection, job: dict[str, Any], version: dict[str, Any]) -> dict[str, Any]:
    latest = _latest(c, job["topic_id"], job["job_id"])
    return dict(job_id=job["job_id"], **{key: version[key] for key in
                ("version_id", "created_at", "input_digest", "candidate_digest", "source_snapshot_id", "content_sha256")},
                paper_count=len(job["inputs"]["papers"]), latest_decision=latest)


def listing(topic: str, page: int = 1, limit: int = 10) -> dict[str, Any]:
    """Page preserved reports, including reports subsequently held or rejected."""
    if not 1 <= limit <= 100 or page < 1:
        raise ValueError("pagination_invalid")
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        if not _exists(c):
            return dict(items=[], total=0, page=page, limit=limit)
        total = c.execute("SELECT count(*) FROM synthesis_report_versions WHERE topic_id=?", (topic,)).fetchone()[0]
        rows = c.execute("""SELECT job_id FROM synthesis_report_versions WHERE topic_id=?
            ORDER BY rowid DESC LIMIT ? OFFSET ?""", (topic, limit, (page - 1) * limit)).fetchall()
        return dict(items=[_summary(c, *_saved(c, topic, row[0])) for row in rows], total=total, page=page, limit=limit)


def _fields(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(field=key, before=before.get(key), after=after.get(key))
            for key in sorted(before.keys() | after.keys()) if before.get(key) != after.get(key)]


def compare(topic: str, job_id: str, from_job_id: str) -> dict[str, Any]:
    """Exact frozen-record differences, not text similarity or scientific claims.

    Match only paper_version_id: two versions of one canonical paper remain
    separate selected records. Both reports and their inputs are verified in a
    single read snapshot. Never consult current source files or render a new body.
    """
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        before, left = _saved(c, topic, from_job_id)
        after, right = _saved(c, topic, job_id)
        old = {p["paper_version_id"]: p for p in before["inputs"]["papers"]}
        new = {p["paper_version_id"]: p for p in after["inputs"]["papers"]}
        papers = []
        counts = dict(added=0, removed=0, changed=0, unchanged=0)
        for identity in sorted(old.keys() | new.keys()):
            a, b = old.get(identity, {}), new.get(identity, {})
            fields = _fields(a, b)
            status = "added" if not a else "removed" if not b else "changed" if fields else "unchanged"
            counts[status] += 1
            papers.append(dict(paper_version_id=identity, title=(b or a)["title"], status=status, fields=fields))
        coverage = [dict(metric=key, before=left["coverage"][key], after=right["coverage"][key],
                         delta=right["coverage"][key] - left["coverage"][key])
                    for key in ("selected_papers", "papers_with_abstract", "papers_with_stored_excerpt", "recorded_full_text_reviewed")]
        return dict(schema_version="LOCAL_EVIDENCE_REPORT_COMPARISON_V1", matching="paper_version_id",
                    before=_summary(c, before, left), after=_summary(c, after, right), counts=counts, papers=papers,
                    input_context=_fields({k: v for k, v in before["inputs"].items() if k != "papers"},
                                          {k: v for k, v in after["inputs"].items() if k != "papers"}),
                    coverage=coverage, scientific_assessment="NOT_ASSESSED")


def export(topic: str, job_id: str, version_id: str, decision_id: str, format: str) -> dict[str, Any]:
    if format not in {"markdown", "json"}:
        raise ValueError("synthesis_export_format_invalid")
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        job = _candidate(c, topic, job_id)
        version = _version(c, job) if _exists(c) else None
        latest = _latest(c, topic, job_id) if _exists(c) else None
        if not version or not latest or latest["state"] != "accepted" or version["version_id"] != version_id or latest["decision_id"] != decision_id:
            raise ValueError("synthesis_export_not_accepted")
        if format == "markdown":
            # The immutable body records initial acceptance. This export cover
            # records the actual review decision authorizing these bytes.
            content = f"<!-- export-review-decision: {decision_id} -->\n" + version["content"]
        else:
            decisions = [dict(row) for row in c.execute("SELECT * FROM synthesis_report_decisions WHERE topic_id=? AND job_id=? ORDER BY sequence", (topic, job_id))]
            content = encoded(dict(schema_version="LOCAL_EVIDENCE_REPORT_EXPORT_V1", version=version,
                                          frozen_input=job["inputs"], candidate=job["candidate"],
                                          validation=job["steps"][-1]["output"], decisions=decisions), job["inputs"])
        return dict(content=content, filename=f"{version_id}.{ 'md' if format == 'markdown' else 'json'}",
                    media_type="text/markdown; charset=utf-8" if format == "markdown" else "application/json",
                    content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest())
