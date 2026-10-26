from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from app.repositories.files import iso_now_utc, read_json, write_json, write_text
from app.repositories.artifact_refs import normalize_payload_references, resolve_payload_references
from app.repositories.runs import build_run_paths, load_run_record, save_run_record
from app.services import evidence, research_domain
from app.services.providers import ProviderError, ProviderRefusalError, ProviderRequest, ProviderUnavailableError, get_active_provider

RESEARCH_PLAN_JSON = "research_plan.json"
RESEARCH_PLAN_MARKDOWN = "research_plan.md"


def _artifact_paths(run_id: str) -> tuple[Path, Path]:
    reports_dir = build_run_paths(run_id).reports_dir
    return reports_dir / RESEARCH_PLAN_JSON, reports_dir / RESEARCH_PLAN_MARKDOWN


def load_plan_if_present(run_id: str) -> evidence.ResearchPlanArtifact | None:
    plan_path, _ = _artifact_paths(run_id)
    payload = read_json(plan_path, default=None)
    if not isinstance(payload, dict):
        return None
    return evidence.ResearchPlanArtifact.model_validate(resolve_payload_references(payload, run_id=run_id))


def _section_title(section_id: str) -> str:
    mapping = {
        "executive-summary": "Executive Summary",
        "top-papers": "Top Papers",
        "trend-signals": "Trend Signals",
        "research-gaps": "Research Gaps / Open Questions",
        "next-experiments": "Proposed Experiment Scaffolds (Not Executed)",
    }
    return mapping.get(section_id, section_id.replace("-", " ").title())


def _provider_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "sections": {
                "type": "array",
                "minItems": 4,
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "section_id": {
                            "type": "string",
                            "enum": ["executive-summary", "trend-signals", "research-gaps", "next-experiments", "top-papers"],
                        },
                        "summary": {"type": "string"},
                        "paper_ids": {"type": "array", "items": {"type": "string"}},
                        "claims": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "claim_id": {"type": "string"},
                                    "claim_text": {"type": "string"},
                                    "importance": {"type": "string", "enum": ["high", "medium", "low"]},
                                    "citations": {
                                        "type": "array",
                                        "minItems": 1,
                                        "items": {
                                            "type": "object",
                                            "additionalProperties": False,
                                            "properties": {"evidence_id": {"type": "string"}},
                                            "required": ["evidence_id"],
                                        },
                                    },
                                },
                                "required": ["claim_id", "claim_text", "importance", "citations"],
                            },
                        },
                    },
                    "required": ["section_id", "summary", "paper_ids", "claims"],
                },
            }
        },
        "required": ["sections"],
    }


def _provider_instructions(
    domain_context: research_domain.ResearchDomainContext,
) -> str:
    return (
        f"You are generating an evidence-backed research brief about {domain_context.display_name}. "
        "Use only the supplied paper and evidence catalog. Every claim must cite one or more evidence_id values from the catalog. "
        "Do not invent papers, citations, artifact paths, or evidence IDs. "
        "Do not infer an application area, deployment environment, hardware, user group, or experimental method that is absent from the supplied context and evidence. "
        "The next-experiments section is a proposal-only planning section: state that it was not executed, do not claim validation or completion, and do not invent datasets, sample sizes, metrics, thresholds, environments, observations, or results. "
        "Prefer concise, concrete claims grounded in the supplied snippets. "
        "Produce JSON that matches the provided schema."
    )


def _provider_prompt(
    run: dict[str, object],
    context: dict[str, object],
    domain_context: research_domain.ResearchDomainContext,
) -> str:
    return json.dumps(
        {
            "topic": run.get("topic", ""),
            "run_id": run.get("run_id", ""),
            "domain_context": {
                "display_name": domain_context.display_name,
                "topic": domain_context.topic,
                "focus_terms": list(domain_context.focus_terms),
                "search_queries": list(domain_context.search_queries),
                "research_questions": list(domain_context.research_questions),
                "source": domain_context.source.value,
            },
            "triage_count": int((run.get("counts") or {}).get("triage_items", 0) or 0),
            "themes": context.get("themes", []),
            "papers": context.get("papers", []),
            "evidence": context.get("evidence", []),
            "required_sections": [
                "executive-summary",
                "trend-signals",
                "research-gaps",
                "next-experiments",
                "top-papers",
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


def _fallback_sections(
    run: dict[str, object],
    context: dict[str, object],
    evidence_rows: list[evidence.EvidenceReference],
    domain_context: research_domain.ResearchDomainContext,
) -> list[evidence.ResearchPlanSection]:
    grouped_by_source: dict[str, list[evidence.EvidenceReference]] = defaultdict(list)
    for row in evidence_rows:
        grouped_by_source[row.source_id].append(row)
    papers = context.get("papers", [])
    themes = context.get("themes", [])

    def claim_from_source(*, claim_id: str, claim_text: str, source_ids: list[str], importance: str = "medium") -> evidence.EvidenceBackedClaim:
        citations: list[evidence.ClaimCitation] = []
        for source_id in source_ids:
            for row in grouped_by_source.get(source_id, [])[:2]:
                citations.append(
                    evidence.ClaimCitation(
                        evidence_id=row.evidence_id,
                        source_id=row.source_id,
                        paper_id=row.paper_id,
                        artifact_path=row.artifact_path,
                        citation_text=row.citation_text,
                        snippet=row.snippet,
                    )
                )
        return evidence.EvidenceBackedClaim(
            claim_id=claim_id,
            claim_text=claim_text,
            importance=importance,
            source_ids=source_ids,
            citations=citations,
        )

    paper_rows = [paper for paper in papers if isinstance(paper, dict)]
    theme_rows = [theme for theme in themes if isinstance(theme, dict)]
    top_source_ids = [
        source_id
        for paper in paper_rows
        if (source_id := str(paper.get("paper_id", "")).strip())
        and source_id in grouped_by_source
    ][:3]
    if not top_source_ids:
        top_source_ids = list(grouped_by_source)[:3]
    top_theme_names = [str(theme.get("theme", "")).strip() for theme in theme_rows[:3] if str(theme.get("theme", "")).strip()]
    top_theme_text = ", ".join(top_theme_names) if top_theme_names else "current ranked set"
    domain_label = research_domain.markdown_inline(domain_context.display_name)

    if not top_source_ids:
        return [
            evidence.ResearchPlanSection(
                section_id="executive-summary",
                title=_section_title("executive-summary"),
                summary=(
                    f"No evidence-backed findings were produced for {domain_label}; "
                    "the current run contains no citable evidence."
                ),
            ),
            evidence.ResearchPlanSection(
                section_id="top-papers",
                title=_section_title("top-papers"),
                summary="No ranked paper has citable evidence in this run.",
            ),
            evidence.ResearchPlanSection(
                section_id="trend-signals",
                title=_section_title("trend-signals"),
                summary="Trend signals were not inferred from an empty evidence set.",
            ),
            evidence.ResearchPlanSection(
                section_id="research-gaps",
                title=_section_title("research-gaps"),
                summary=(
                    "The absence of citable evidence is a collection limitation, "
                    "not evidence of a substantive research gap."
                ),
            ),
            evidence.ResearchPlanSection(
                section_id="next-experiments",
                title=_section_title("next-experiments"),
                summary=(
                    "No experiment proposal was executed. Collect and review evidence before "
                    "selecting a method, dataset, sample size, metric, baseline, or environment."
                ),
            ),
        ]

    return [
        evidence.ResearchPlanSection(
            section_id="executive-summary",
            title=_section_title("executive-summary"),
            summary=f"The run highlights {top_theme_text} as the main concentration across the highest-ranked papers.",
            claims=[
                claim_from_source(
                    claim_id="executive-summary-1",
                    claim_text=f"The top-ranked evidence clusters around {top_theme_text}, indicating the current frontier emphasis in this run.",
                    source_ids=top_source_ids,
                    importance="high",
                )
            ],
        ),
        evidence.ResearchPlanSection(
            section_id="top-papers",
            title=_section_title("top-papers"),
            summary="The top paper set anchors the current evidence bundle.",
            paper_ids=[str(paper.get("paper_id", "")).strip() for paper in paper_rows[:5] if str(paper.get("paper_id", "")).strip()],
            claims=[
                claim_from_source(
                    claim_id=f"top-papers-{index}",
                    claim_text=f"{paper['title']} is a priority reading candidate for the current topic.",
                    source_ids=[str(paper["paper_id"])],
                    importance="medium",
                )
                for index, paper in enumerate(paper_rows[:3], start=1)
                if str(paper.get("paper_id", "")).strip()
            ],
        ),
        evidence.ResearchPlanSection(
            section_id="trend-signals",
            title=_section_title("trend-signals"),
            summary="Theme counts and ranked evidence show where momentum is accumulating.",
            claims=[
                claim_from_source(
                    claim_id=f"trend-signals-{index}",
                    claim_text=f"{theme['theme']} appears repeatedly in the current high-signal set and should be tracked as an active trend.",
                    source_ids=top_source_ids[:2] or top_source_ids,
                    importance="medium",
                )
                for index, theme in enumerate(theme_rows[:3], start=1)
                if str(theme.get("theme", "")).strip()
            ],
        ),
        evidence.ResearchPlanSection(
            section_id="research-gaps",
            title=_section_title("research-gaps"),
            summary=(
                f"The current {domain_label} evidence suggests unresolved questions, "
                "but conclusions remain limited to this run's reviewed sources."
            ),
            claims=[
                claim_from_source(
                    claim_id="research-gaps-1",
                    claim_text=(
                        f"The reviewed {domain_label} evidence is limited to the current paper set, "
                        "so broader landscape conclusions require additional sources."
                    ),
                    source_ids=top_source_ids[:2] or top_source_ids,
                    importance="high",
                ),
                claim_from_source(
                    claim_id="research-gaps-2",
                    claim_text=(
                        "Differences in data, methods, and evaluation conditions across the current "
                        "sources require comparison before their results can be generalized."
                    ),
                    source_ids=top_source_ids[1:3] or top_source_ids,
                    importance="medium",
                ),
            ],
        ),
        evidence.ResearchPlanSection(
            section_id="next-experiments",
            title=_section_title("next-experiments"),
            summary=(
                "These are non-executable research design proposals only. No experiment was "
                "executed, measured, completed, or validated in this run."
            ),
            claims=[
                claim_from_source(
                    claim_id="next-experiments-1",
                    claim_text=(
                        "Proposal only: a future study could select one evidence-supported "
                        "baseline after its dataset, sample size, evaluation metric, environment, "
                        "and implementation are resolved."
                    ),
                    source_ids=top_source_ids[:1] or top_source_ids,
                    importance="high",
                ),
                claim_from_source(
                    claim_id="next-experiments-2",
                    claim_text=(
                        "Planning step only: a future design could compare documented study "
                        "conditions across the current evidence bundle without treating the "
                        "proposal as an observation or result."
                    ),
                    source_ids=top_source_ids[:2] or top_source_ids,
                    importance="medium",
                ),
            ],
        ),
    ]


def _normalize_provider_sections(
    provider_payload: dict[str, object],
    evidence_rows: list[evidence.EvidenceReference],
) -> list[evidence.ResearchPlanSection]:
    evidence_catalog = {row.evidence_id: row for row in evidence_rows}
    normalized_sections: list[evidence.ResearchPlanSection] = []
    seen_ids: set[str] = set()
    for raw_section in provider_payload.get("sections") or []:
        if not isinstance(raw_section, dict):
            continue
        section_id = str(raw_section.get("section_id", "")).strip()
        if not section_id or section_id in seen_ids:
            continue
        seen_ids.add(section_id)
        claims: list[evidence.EvidenceBackedClaim] = []
        for raw_claim in raw_section.get("claims") or []:
            if not isinstance(raw_claim, dict):
                continue
            citations = evidence.resolve_citations(evidence_catalog, raw_claim.get("citations"))
            if not citations:
                continue
            claim_text = str(raw_claim.get("claim_text", "")).strip()
            if section_id == "next-experiments":
                claim_text = (
                    "Evidence-linked rationale is available for a future research design "
                    "proposal; this run did not execute, measure, complete, or validate it."
                )
            claims.append(
                evidence.EvidenceBackedClaim(
                    claim_id=str(raw_claim.get("claim_id", "")).strip() or f"{section_id}-{len(claims) + 1}",
                    claim_text=claim_text,
                    importance=str(raw_claim.get("importance", "medium")).strip() or "medium",
                    source_ids=sorted({citation.source_id for citation in citations}),
                    citations=citations,
                )
            )
        summary = str(raw_section.get("summary", "")).strip()
        if section_id == "next-experiments":
            summary = (
                "Non-executable research design proposals only; this run did not execute, "
                "measure, complete, or validate an experiment."
            )
        normalized_sections.append(
            evidence.ResearchPlanSection(
                section_id=section_id,
                title=_section_title(section_id),
                summary=summary,
                paper_ids=[str(paper_id) for paper_id in raw_section.get("paper_ids", []) if str(paper_id).strip()],
                claims=claims,
            )
        )
    return normalized_sections


def render_plan_markdown(plan: evidence.ResearchPlanArtifact) -> str:
    lines: list[str] = [
        f"# Evidence-backed Research Report — {research_domain.markdown_inline(plan.topic)}",
        "",
    ]
    lines.append(f"- Generated at: {plan.generated_at}")
    lines.append(f"- Generator mode: {plan.generator.mode}")
    if plan.generator.provider_name:
        lines.append(f"- Provider: {plan.generator.provider_name} / {plan.generator.model}")
    lines.append(f"- Evidence items considered: {plan.evidence_count}")
    lines.append("")
    for section in plan.sections:
        lines.append(f"## {section.title}")
        if section.section_id == "next-experiments":
            lines.append(
                "- artifact_kind=research_design_scaffold | execution_status=not_executed | "
                "executable=false | runner_available=false"
            )
            lines.append(
                "- unresolved inputs: dataset/study materials, sample size, evaluation metric, "
                "baseline, execution environment, implementation"
            )
            lines.append("")
        if section.summary:
            lines.append(section.summary)
            lines.append("")
        if section.paper_ids:
            lines.append(f"- linked papers: {', '.join(section.paper_ids)}")
        for claim in section.claims:
            lines.append(f"- {claim.claim_text}")
            for citation in claim.citations:
                lines.append(
                    f"  - evidence_id={citation.evidence_id} | source_id={citation.source_id} | artifact_path={citation.artifact_path or 'n/a'}"
                )
                lines.append(f"  - citation: {citation.citation_text}")
                lines.append(f"  - snippet: {citation.snippet}")
        if not section.claims:
            lines.append("- (no evidence-backed claims)")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def generate_run_research_plan(
    run_id: str,
    *,
    domain_context: research_domain.ResearchDomainContext | None = None,
) -> evidence.ResearchPlanArtifact:
    run = load_run_record(run_id)
    resolved_domain_context = (
        domain_context
        or research_domain.research_domain_context_for_run(run)
    )
    evidence_rows = evidence.collect_run_evidence(run_id)
    context = evidence.collect_research_context(run_id)
    provider = get_active_provider()
    warnings: list[str] = []
    generator = evidence.ResearchPlanGeneratorInfo(mode="heuristic")

    if provider is not None:
        try:
            result = provider.generate_json(
                ProviderRequest(
                    schema_name="xrml_research_plan",
                    schema=_provider_schema(),
                    instructions=_provider_instructions(resolved_domain_context),
                    prompt=_provider_prompt(run, context, resolved_domain_context),
                    metadata={"run_id": run_id, "topic": run.get("topic", "")},
                    temperature=0.2,
                    max_output_tokens=2200,
                )
            )
            sections = _normalize_provider_sections(result.payload, evidence_rows)
            if sections:
                generator = evidence.ResearchPlanGeneratorInfo(
                    mode="provider",
                    provider_name=result.provider_name,
                    model=result.model,
                    response_id=result.response_id,
                )
            else:
                warnings.append("Provider returned no valid evidence-linked claims; heuristic fallback applied.")
                sections = _fallback_sections(
                    run,
                    context,
                    evidence_rows,
                    resolved_domain_context,
                )
        except (ProviderError, ProviderRefusalError, ProviderUnavailableError) as exc:
            warnings.append(str(exc))
            sections = _fallback_sections(
                run,
                context,
                evidence_rows,
                resolved_domain_context,
            )
    else:
        warnings.append("No active provider configured; heuristic fallback applied.")
        sections = _fallback_sections(
            run,
            context,
            evidence_rows,
            resolved_domain_context,
        )

    if not any(section.section_id == "top-papers" for section in sections):
        paper_rows = [paper for paper in context.get("papers", []) if isinstance(paper, dict)]
        sections.append(
            evidence.ResearchPlanSection(
                section_id="top-papers",
                title=_section_title("top-papers"),
                summary="Top papers carried over from the ranked shortlist.",
                paper_ids=[str(paper.get("paper_id", "")).strip() for paper in paper_rows[:5] if str(paper.get("paper_id", "")).strip()],
            )
        )

    plan = evidence.ResearchPlanArtifact(
        run_id=run_id,
        topic=str(run.get("topic", "")),
        generated_at=iso_now_utc(),
        evidence_count=len(evidence_rows),
        source_count=len({row.source_id for row in evidence_rows}),
        generator=evidence.ResearchPlanGeneratorInfo(
            mode=generator.mode,
            provider_name=generator.provider_name,
            model=generator.model,
            response_id=generator.response_id,
            warnings=warnings,
        ),
        sections=sections,
    )
    plan_path, markdown_path = _artifact_paths(run_id)
    write_json(plan_path, normalize_payload_references(plan.model_dump(), run_id=run_id))
    write_text(markdown_path, render_plan_markdown(plan))

    run.setdefault("artifacts", {})["research_plan_json"] = str(plan_path)
    run["artifacts"]["research_plan_markdown"] = str(markdown_path)
    run.setdefault("counts", {})["research_plan_claims"] = sum(len(section.claims) for section in plan.sections)
    run.setdefault("counts", {})["research_plan_evidence"] = plan.evidence_count
    save_run_record(run)
    return plan
