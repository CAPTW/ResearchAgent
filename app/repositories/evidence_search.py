from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.db.bootstrap import connect_metadata_index


class EvidenceSearchTooExpensive(RuntimeError):
    """Raised when a request-local SQLite progress deadline is exceeded."""


@dataclass(frozen=True)
class RepositorySearchCriteria:
    paperPatterns: tuple[str, ...] = ()
    visibleRunIds: tuple[str, ...] | None = None
    runIds: tuple[str, ...] = ()
    topicIds: tuple[str, ...] = ()
    topicNames: tuple[str, ...] = ()
    reviewSources: tuple[str, ...] = ()
    providerIds: tuple[str, ...] = ()
    reviewLevels: tuple[str, ...] = ()
    occurrenceLimit: int = 5


@dataclass(frozen=True)
class PaperCandidate:
    runId: str
    paperId: str
    title: str
    theme: str
    score: int
    status: str
    published: str | None
    entryUrl: str | None
    pdfUrl: str | None
    dedupClusterId: str | None
    isPrimary: bool
    runTopic: str
    runStatus: str
    runCreatedAt: str
    runUpdatedAt: str


@dataclass(frozen=True)
class ReviewCandidate:
    topicId: str
    runId: str
    paperId: str
    title: str
    sourceProvider: str | None
    publishedAt: str | None
    abstractText: str | None
    textExcerpt: str | None
    fullTextStatus: str
    methodSummary: str | None
    keyFindingsJson: str
    limitationsJson: str
    whyItMatters: str | None
    topicFitRationale: str | None
    reviewModel: str | None
    reviewer: str | None
    reviewedAt: str
    sourceTextSha256: str | None
    fullTextArtifactPath: str | None
    textCharCount: int
    evidenceSpansJson: str
    reviewSourceType: str
    fullTextReviewedAt: str | None


@dataclass(frozen=True)
class EvidenceCandidate:
    runId: str
    evidenceId: str
    sourceId: str
    paperId: str
    title: str
    sourceType: str
    qualityHint: str | None
    confidence: float | None
    artifactPath: str | None
    citationText: str
    snippet: str
    runTopic: str
    runUpdatedAt: str


@dataclass(frozen=True)
class RunCandidate:
    runId: str
    topic: str
    status: str
    createdAt: str
    updatedAt: str
    rootRunId: str
    parentRunId: str | None
    lineageMode: str
    reportPath: str | None


@dataclass(frozen=True)
class ReportSectionCandidate:
    runId: str
    sectionId: str
    title: str
    claimCount: int
    citationCount: int
    updatedAt: str
    runTopic: str
    reportPath: str | None


@dataclass(frozen=True)
class PaperOccurrence:
    runId: str
    paperId: str
    title: str
    theme: str
    score: int
    status: str
    published: str | None
    entryUrl: str | None
    pdfUrl: str | None
    runTopic: str
    runStatus: str
    runCreatedAt: str
    runUpdatedAt: str
    occurrenceCount: int


@dataclass(frozen=True)
class SearchBundle:
    papers: tuple[PaperCandidate, ...]
    reviews: tuple[ReviewCandidate, ...]
    evidence: tuple[EvidenceCandidate, ...]
    runs: tuple[RunCandidate, ...]
    reportSections: tuple[ReportSectionCandidate, ...]
    occurrences: tuple[PaperOccurrence, ...]


def escape_like(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _json_parameter(values: Sequence[str]) -> str:
    return json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))


def _append_json_membership(
    conditions: list[str],
    parameters: dict[str, object],
    *,
    column: str,
    parameter_name: str,
    values: Sequence[str],
) -> None:
    if not values:
        return
    conditions.append(f"{column} IN (SELECT value FROM json_each(:{parameter_name}))")
    parameters[parameter_name] = _json_parameter(values)


class EvidenceSearchRepository:
    def __init__(
        self,
        *,
        timeoutSeconds: float = 2.0,
        progressSteps: int = 1000,
        monotonic: Callable[[], float] = time.monotonic,
        connectFactory: Callable[..., sqlite3.Connection] = connect_metadata_index,
    ) -> None:
        self._timeout_seconds = max(float(timeoutSeconds), 0.0)
        self._progress_steps = max(int(progressSteps), 1)
        self._monotonic = monotonic
        self._connect_factory = connectFactory
        self.lastStatementCount = 0

    def search(self, criteria: RepositorySearchCriteria) -> SearchBundle:
        deadline = self._monotonic() + self._timeout_seconds
        connection = self._connect_factory(read_only=True)
        statement_count = 0

        def trace(statement: str) -> None:
            nonlocal statement_count
            normalized = statement.lstrip().upper()
            if normalized.startswith(("SELECT", "WITH")):
                statement_count += 1

        def progress() -> int:
            return int(self._monotonic() >= deadline)

        connection.set_trace_callback(trace)
        connection.set_progress_handler(progress, self._progress_steps)
        try:
            papers = self._search_papers(connection, criteria)
            reviews = self._search_reviews(connection, criteria)
            evidence = self._search_evidence(connection, criteria)
            runs = self._search_runs(connection, criteria)
            report_sections = self._search_report_sections(connection, criteria)
            candidate_paper_ids = tuple(
                dict.fromkeys(
                    [row.paperId for row in papers]
                    + [row.paperId for row in reviews]
                    + [row.paperId for row in evidence]
                )
            )
            occurrences = (
                self._search_occurrences(connection, criteria, candidate_paper_ids)
                if candidate_paper_ids
                else ()
            )
            return SearchBundle(
                papers=papers,
                reviews=reviews,
                evidence=evidence,
                runs=runs,
                reportSections=report_sections,
                occurrences=occurrences,
            )
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc).lower():
                raise EvidenceSearchTooExpensive("evidence search exceeded its SQLite budget") from exc
            raise
        finally:
            self.lastStatementCount = statement_count
            connection.set_progress_handler(None, 0)
            connection.set_trace_callback(None)
            connection.close()

    @staticmethod
    def _visibility_condition(
        conditions: list[str],
        parameters: dict[str, object],
        *,
        column: str,
        visible_run_ids: tuple[str, ...] | None,
    ) -> None:
        if visible_run_ids is None:
            return
        if not visible_run_ids:
            conditions.append("1 = 0")
            return
        conditions.append(f"{column} IN (SELECT value FROM json_each(:visible_run_ids))")
        parameters["visible_run_ids"] = _json_parameter(visible_run_ids)

    @staticmethod
    def _where(conditions: Sequence[str]) -> str:
        return " WHERE " + " AND ".join(conditions) if conditions else ""

    def _search_papers(
        self,
        connection: sqlite3.Connection,
        criteria: RepositorySearchCriteria,
    ) -> tuple[PaperCandidate, ...]:
        conditions: list[str] = []
        parameters: dict[str, object] = {}
        self._visibility_condition(
            conditions,
            parameters,
            column="p.run_id",
            visible_run_ids=criteria.visibleRunIds,
        )
        _append_json_membership(
            conditions,
            parameters,
            column="p.run_id",
            parameter_name="paper_run_ids",
            values=criteria.runIds,
        )
        if criteria.topicIds:
            if not criteria.topicNames:
                conditions.append("1 = 0")
            else:
                _append_json_membership(
                    conditions,
                    parameters,
                    column="r.topic",
                    parameter_name="paper_topic_names",
                    values=criteria.topicNames,
                )
        if criteria.paperPatterns:
            pattern_conditions: list[str] = []
            columns = ("p.paper_id", "p.title", "p.theme", "COALESCE(p.published, '')", "r.topic")
            for index, pattern in enumerate(criteria.paperPatterns):
                parameter_name = f"paper_pattern_{index}"
                parameters[parameter_name] = pattern
                pattern_conditions.extend(
                    f"LOWER({column}) LIKE :{parameter_name} ESCAPE '\\'" for column in columns
                )
            conditions.append("(" + " OR ".join(pattern_conditions) + ")")
        rows = connection.execute(
            """
            WITH matched_papers AS (
              SELECT p.run_id, p.paper_id, p.title, p.theme, p.score, p.status, p.published,
                     p.entry_url, p.pdf_url, p.dedup_cluster_id, p.is_primary,
                     r.topic AS run_topic, r.status AS run_status,
                     r.created_at AS run_created_at, r.updated_at AS run_updated_at,
                     ROW_NUMBER() OVER (
                       PARTITION BY p.paper_id
                       ORDER BY r.updated_at DESC, p.run_id ASC
                     ) AS paper_rank
              FROM papers AS p
              JOIN runs AS r ON r.run_id = p.run_id
            """
            + self._where(conditions)
            + """
            )
            SELECT run_id, paper_id, title, theme, score, status, published,
                   entry_url, pdf_url, dedup_cluster_id, is_primary,
                   run_topic, run_status, run_created_at, run_updated_at
            FROM matched_papers
            WHERE paper_rank = 1
            ORDER BY run_updated_at DESC, run_id ASC, paper_id ASC
            """,
            parameters,
        ).fetchall()
        return tuple(
            PaperCandidate(
                runId=str(row["run_id"]),
                paperId=str(row["paper_id"]),
                title=str(row["title"]),
                theme=str(row["theme"]),
                score=int(row["score"] or 0),
                status=str(row["status"]),
                published=row["published"],
                entryUrl=row["entry_url"],
                pdfUrl=row["pdf_url"],
                dedupClusterId=row["dedup_cluster_id"],
                isPrimary=bool(row["is_primary"]),
                runTopic=str(row["run_topic"]),
                runStatus=str(row["run_status"]),
                runCreatedAt=str(row["run_created_at"]),
                runUpdatedAt=str(row["run_updated_at"]),
            )
            for row in rows
        )

    def _search_reviews(
        self,
        connection: sqlite3.Connection,
        criteria: RepositorySearchCriteria,
    ) -> tuple[ReviewCandidate, ...]:
        conditions: list[str] = []
        parameters: dict[str, object] = {}
        if criteria.visibleRunIds is not None:
            if not criteria.visibleRunIds:
                conditions.append("1 = 0")
            else:
                parameters["review_visible_run_ids"] = _json_parameter(criteria.visibleRunIds)
                conditions.append(
                    """
                    (
                      pr.run_id IN (SELECT value FROM json_each(:review_visible_run_ids))
                      OR EXISTS (
                        SELECT 1 FROM papers AS visible_paper
                        WHERE visible_paper.paper_id = pr.paper_id
                          AND visible_paper.run_id IN (SELECT value FROM json_each(:review_visible_run_ids))
                      )
                    )
                    """
                )
        if criteria.runIds:
            parameters["review_run_ids"] = _json_parameter(criteria.runIds)
            conditions.append(
                """
                (
                  pr.run_id IN (SELECT value FROM json_each(:review_run_ids))
                  OR EXISTS (
                    SELECT 1 FROM papers AS requested_paper
                    WHERE requested_paper.paper_id = pr.paper_id
                      AND requested_paper.run_id IN (SELECT value FROM json_each(:review_run_ids))
                  )
                )
                """
            )
        _append_json_membership(
            conditions,
            parameters,
            column="pr.topic_id",
            parameter_name="review_topic_ids",
            values=criteria.topicIds,
        )
        _append_json_membership(
            conditions,
            parameters,
            column="pr.review_source_type",
            parameter_name="review_sources",
            values=criteria.reviewSources,
        )
        _append_json_membership(
            conditions,
            parameters,
            column="pr.source_provider",
            parameter_name="provider_ids",
            values=criteria.providerIds,
        )
        if criteria.reviewLevels:
            storage_levels = tuple(
                "reviewed" if value == "full_text_reviewed" else value
                for value in criteria.reviewLevels
            )
            _append_json_membership(
                conditions,
                parameters,
                column="pr.full_text_status",
                parameter_name="review_levels",
                values=storage_levels,
            )
        rows = connection.execute(
            """
            SELECT pr.topic_id, pr.run_id, pr.paper_id, pr.title, pr.source_provider,
                   pr.published_at, pr.abstract_text, pr.text_excerpt, pr.full_text_status,
                   pr.method_summary, pr.key_findings_json, pr.limitations_json,
                   pr.why_it_matters, pr.topic_fit_rationale, pr.review_model, pr.reviewer,
                   pr.reviewed_at, pr.source_text_sha256, pr.full_text_artifact_path,
                   pr.text_char_count, pr.evidence_spans_json, pr.review_source_type,
                   pr.full_text_reviewed_at
            FROM paper_reviews AS pr
            """
            + self._where(conditions)
            + " ORDER BY pr.reviewed_at DESC, pr.topic_id ASC, pr.paper_id ASC, pr.run_id ASC",
            parameters,
        ).fetchall()
        return tuple(
            ReviewCandidate(
                topicId=str(row["topic_id"]),
                runId=str(row["run_id"]),
                paperId=str(row["paper_id"]),
                title=str(row["title"]),
                sourceProvider=row["source_provider"],
                publishedAt=row["published_at"],
                abstractText=row["abstract_text"],
                textExcerpt=row["text_excerpt"],
                fullTextStatus=str(row["full_text_status"]),
                methodSummary=row["method_summary"],
                keyFindingsJson=str(row["key_findings_json"] or "[]"),
                limitationsJson=str(row["limitations_json"] or "[]"),
                whyItMatters=row["why_it_matters"],
                topicFitRationale=row["topic_fit_rationale"],
                reviewModel=row["review_model"],
                reviewer=row["reviewer"],
                reviewedAt=str(row["reviewed_at"]),
                sourceTextSha256=row["source_text_sha256"],
                fullTextArtifactPath=row["full_text_artifact_path"],
                textCharCount=int(row["text_char_count"] or 0),
                evidenceSpansJson=str(row["evidence_spans_json"] or "[]"),
                reviewSourceType=str(row["review_source_type"]),
                fullTextReviewedAt=row["full_text_reviewed_at"],
            )
            for row in rows
        )

    def _search_evidence(
        self,
        connection: sqlite3.Connection,
        criteria: RepositorySearchCriteria,
    ) -> tuple[EvidenceCandidate, ...]:
        conditions: list[str] = []
        parameters: dict[str, object] = {}
        self._visibility_condition(
            conditions,
            parameters,
            column="e.run_id",
            visible_run_ids=criteria.visibleRunIds,
        )
        _append_json_membership(
            conditions,
            parameters,
            column="e.run_id",
            parameter_name="evidence_run_ids",
            values=criteria.runIds,
        )
        if criteria.topicIds:
            if not criteria.topicNames:
                conditions.append("1 = 0")
            else:
                _append_json_membership(
                    conditions,
                    parameters,
                    column="r.topic",
                    parameter_name="evidence_topic_names",
                    values=criteria.topicNames,
                )
        rows = connection.execute(
            """
            SELECT e.run_id, e.evidence_id, e.source_id, e.paper_id, e.title,
                   e.source_type, e.quality_hint, e.confidence, e.artifact_path,
                   e.citation_text, e.snippet, r.topic AS run_topic,
                   r.updated_at AS run_updated_at
            FROM evidence AS e
            JOIN runs AS r ON r.run_id = e.run_id
            """
            + self._where(conditions)
            + " ORDER BY r.updated_at DESC, e.run_id ASC, e.evidence_id ASC",
            parameters,
        ).fetchall()
        return tuple(
            EvidenceCandidate(
                runId=str(row["run_id"]),
                evidenceId=str(row["evidence_id"]),
                sourceId=str(row["source_id"]),
                paperId=str(row["paper_id"]),
                title=str(row["title"]),
                sourceType=str(row["source_type"]),
                qualityHint=row["quality_hint"],
                confidence=float(row["confidence"]) if row["confidence"] is not None else None,
                artifactPath=row["artifact_path"],
                citationText=str(row["citation_text"]),
                snippet=str(row["snippet"]),
                runTopic=str(row["run_topic"]),
                runUpdatedAt=str(row["run_updated_at"]),
            )
            for row in rows
        )

    def _search_runs(
        self,
        connection: sqlite3.Connection,
        criteria: RepositorySearchCriteria,
    ) -> tuple[RunCandidate, ...]:
        conditions: list[str] = []
        parameters: dict[str, object] = {}
        self._visibility_condition(
            conditions,
            parameters,
            column="r.run_id",
            visible_run_ids=criteria.visibleRunIds,
        )
        _append_json_membership(
            conditions,
            parameters,
            column="r.run_id",
            parameter_name="run_ids",
            values=criteria.runIds,
        )
        if criteria.topicIds:
            if not criteria.topicNames:
                conditions.append("1 = 0")
            else:
                _append_json_membership(
                    conditions,
                    parameters,
                    column="r.topic",
                    parameter_name="run_topic_names",
                    values=criteria.topicNames,
                )
        rows = connection.execute(
            """
            SELECT r.run_id, r.topic, r.status, r.created_at, r.updated_at,
                   r.root_run_id, r.parent_run_id, r.lineage_mode, r.report_path
            FROM runs AS r
            """
            + self._where(conditions)
            + " ORDER BY r.updated_at DESC, r.run_id ASC",
            parameters,
        ).fetchall()
        return tuple(
            RunCandidate(
                runId=str(row["run_id"]),
                topic=str(row["topic"]),
                status=str(row["status"]),
                createdAt=str(row["created_at"]),
                updatedAt=str(row["updated_at"]),
                rootRunId=str(row["root_run_id"]),
                parentRunId=row["parent_run_id"],
                lineageMode=str(row["lineage_mode"]),
                reportPath=row["report_path"],
            )
            for row in rows
        )

    def _search_report_sections(
        self,
        connection: sqlite3.Connection,
        criteria: RepositorySearchCriteria,
    ) -> tuple[ReportSectionCandidate, ...]:
        conditions: list[str] = []
        parameters: dict[str, object] = {}
        self._visibility_condition(
            conditions,
            parameters,
            column="s.run_id",
            visible_run_ids=criteria.visibleRunIds,
        )
        _append_json_membership(
            conditions,
            parameters,
            column="s.run_id",
            parameter_name="section_run_ids",
            values=criteria.runIds,
        )
        if criteria.topicIds:
            if not criteria.topicNames:
                conditions.append("1 = 0")
            else:
                _append_json_membership(
                    conditions,
                    parameters,
                    column="r.topic",
                    parameter_name="section_topic_names",
                    values=criteria.topicNames,
                )
        rows = connection.execute(
            """
            SELECT s.run_id, s.section_id, s.title, s.claim_count, s.citation_count,
                   s.updated_at, r.topic AS run_topic, r.report_path
            FROM report_sections AS s
            JOIN runs AS r ON r.run_id = s.run_id
            """
            + self._where(conditions)
            + " ORDER BY s.updated_at DESC, s.run_id ASC, s.section_id ASC",
            parameters,
        ).fetchall()
        return tuple(
            ReportSectionCandidate(
                runId=str(row["run_id"]),
                sectionId=str(row["section_id"]),
                title=str(row["title"]),
                claimCount=int(row["claim_count"] or 0),
                citationCount=int(row["citation_count"] or 0),
                updatedAt=str(row["updated_at"]),
                runTopic=str(row["run_topic"]),
                reportPath=row["report_path"],
            )
            for row in rows
        )

    def _search_occurrences(
        self,
        connection: sqlite3.Connection,
        criteria: RepositorySearchCriteria,
        candidate_paper_ids: tuple[str, ...],
    ) -> tuple[PaperOccurrence, ...]:
        conditions = ["p.paper_id IN (SELECT value FROM json_each(:candidate_paper_ids))"]
        parameters: dict[str, object] = {
            "candidate_paper_ids": _json_parameter(candidate_paper_ids),
            "occurrence_limit": max(1, min(int(criteria.occurrenceLimit), 100)),
        }
        self._visibility_condition(
            conditions,
            parameters,
            column="p.run_id",
            visible_run_ids=criteria.visibleRunIds,
        )
        _append_json_membership(
            conditions,
            parameters,
            column="p.run_id",
            parameter_name="occurrence_run_ids",
            values=criteria.runIds,
        )
        if criteria.topicIds:
            if not criteria.topicNames:
                conditions.append("1 = 0")
            else:
                _append_json_membership(
                    conditions,
                    parameters,
                    column="r.topic",
                    parameter_name="occurrence_topic_names",
                    values=criteria.topicNames,
                )
        rows = connection.execute(
            """
            WITH ranked_occurrences AS (
              SELECT p.run_id, p.paper_id, p.title, p.theme, p.score, p.status,
                     p.published, p.entry_url, p.pdf_url, r.topic AS run_topic,
                     r.status AS run_status, r.created_at AS run_created_at,
                     r.updated_at AS run_updated_at,
                     COUNT(*) OVER (PARTITION BY p.paper_id) AS occurrence_count,
                     ROW_NUMBER() OVER (
                       PARTITION BY p.paper_id
                       ORDER BY r.updated_at DESC, p.run_id ASC
                     ) AS occurrence_rank
              FROM papers AS p
              JOIN runs AS r ON r.run_id = p.run_id
            """
            + self._where(conditions)
            + """
            )
            SELECT run_id, paper_id, title, theme, score, status, published,
                   entry_url, pdf_url, run_topic, run_status, run_created_at,
                   run_updated_at, occurrence_count
            FROM ranked_occurrences
            WHERE occurrence_rank <= :occurrence_limit
            ORDER BY run_updated_at DESC, run_id ASC, paper_id ASC
            """,
            parameters,
        ).fetchall()
        return tuple(
            PaperOccurrence(
                runId=str(row["run_id"]),
                paperId=str(row["paper_id"]),
                title=str(row["title"]),
                theme=str(row["theme"]),
                score=int(row["score"] or 0),
                status=str(row["status"]),
                published=row["published"],
                entryUrl=row["entry_url"],
                pdfUrl=row["pdf_url"],
                runTopic=str(row["run_topic"]),
                runStatus=str(row["run_status"]),
                runCreatedAt=str(row["run_created_at"]),
                runUpdatedAt=str(row["run_updated_at"]),
                occurrenceCount=int(row["occurrence_count"] or 0),
            )
            for row in rows
        )
