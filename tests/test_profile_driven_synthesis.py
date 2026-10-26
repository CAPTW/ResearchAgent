from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.schemas.runs import RunCreateRequest
from app.services import (
    evidence,
    experiments,
    notes,
    profile,
    reporting,
    research_domain,
    research_plan,
    reviews,
    runs,
    synthesis,
)

UNSUPPORTED_XR_LANGUAGE = re.compile(
    r"(?i)(?<![a-z0-9])(?:xr|ar|vr|hmd|slam)(?![a-z0-9])|"
    r"headset|immersive|extended reality|augmented reality|virtual reality"
)
FIXED_NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


def _context(topic: str, *, source=research_domain.DomainContextSource.RUN_PROFILE_SNAPSHOT):
    return research_domain.build_research_domain_context(
        profile={"title": topic, "topic": topic, "queries": [topic]},
        source=source,
    )


def _triage_item(
    *,
    paper_id: str,
    title: str,
    tags: list[str],
    published: str = "2026-07-26T00:00:00Z",
) -> dict:
    return {
        "paper_id": paper_id,
        "title": title,
        "published": published,
        "updated": published,
        "score": 90,
        "entry_url": f"https://example.invalid/{paper_id}",
        "triage": {"hits_primary": tags},
    }


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(f"{json.dumps(row, ensure_ascii=False)}\n" for row in rows),
        encoding="utf-8",
    )


def _render_synthesis(
    tmp_path: Path,
    *,
    name: str,
    domain_context: research_domain.ResearchDomainContext,
    rows: list[dict],
) -> str:
    triage_path = tmp_path / f"{name}.jsonl"
    output_path = tmp_path / f"{name}.md"
    _write_jsonl(triage_path, rows)
    result = synthesis.synthesize_weekly(
        triage_path=triage_path,
        out_path=output_path,
        top_n=15,
        domain_context=domain_context,
        generated_at=FIXED_NOW,
    )
    assert result["status"] == "ok"
    return output_path.read_text(encoding="utf-8")


def _review(
    *,
    paper_id: str,
    title: str,
    abstract: str,
    status: str = "abstract_only",
) -> dict:
    return {
        "topic_id": "synthetic-topic",
        "run_id": "synthetic-run",
        "paper_id": paper_id,
        "title": title,
        "source_provider": "synthetic",
        "abstract_text": abstract,
        "full_text_status": status,
        "relevance_score": 80,
        "evidence_strength": 50,
        "method_summary": "The paper compares two documented methods.",
        "key_findings": ["A reported finding from the source."],
        "limitations": ["The available evidence is limited."],
        "why_it_matters": "It informs the selected research topic.",
        "authors": ["Synthetic Author"],
        "published_at": "2026-07-20",
    }


def _disable_weekly_external_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        reporting.provider_review_coverage,
        "build_provider_review_coverage_summary",
        lambda *args, **kwargs: {
            "providerOrchestratedAcceptedCount": 0,
            "fixtureSyntheticCount": 0,
            "realProviderAcceptedCount": 0,
            "coverageWarning": None,
        },
    )
    monkeypatch.setattr(
        reporting,
        "_research_recommendations_section",
        lambda **kwargs: [],
    )


def test_domain_context_is_bounded_deterministic_pure_and_redacted(tmp_path: Path) -> None:
    raw = {
        "resource_kind": "system_template",
        "profile_id": "secret-internal-id",
        "owner_user_id": "owner-secret",
        "provider": "provider-secret",
        "title": "Battery\x00 Recycling <script>alert(1)</script>",
        "topic": "Battery\nRecycling",
        "description": " Circular\tmaterials ",
        "queries": [" lithium recycling ", "LITHIUM RECYCLING", *[f"query-{index}" for index in range(30)]],
        "keywords": {"primary": ["lithium", "lithium", *[f"term-{index}" for index in range(30)]]},
        "research_questions": [f"question-{index}" for index in range(20)],
    }

    first = research_domain.build_research_domain_context(profile=raw)
    second = research_domain.build_research_domain_context(profile=deepcopy(raw))
    snapshot = first.to_snapshot()

    assert first == second
    assert first.source == research_domain.DomainContextSource.SYSTEM_TEMPLATE
    assert "\x00" not in first.display_name
    assert "\n" not in first.topic
    assert len(first.focus_terms) == research_domain.MAX_FOCUS_TERMS
    assert len(first.search_queries) == research_domain.MAX_SEARCH_QUERIES
    assert len(first.research_questions) == research_domain.MAX_RESEARCH_QUESTIONS
    assert "owner_user_id" not in snapshot
    assert "profile_id" not in snapshot
    assert "provider" not in snapshot
    assert "owner-secret" not in json.dumps(snapshot)
    assert "provider-secret" not in json.dumps(snapshot)
    assert "&lt;script&gt;" in research_domain.markdown_inline(first.display_name)
    assert list(tmp_path.iterdir()) == []


def test_domain_context_sources_and_legacy_neutral_fallback() -> None:
    snapshot_context = _context("Maritime Decarbonization")
    assert (
        research_domain.research_domain_context_for_run(
            {"domain_context": snapshot_context.to_snapshot(), "topic": "Changed live topic"}
        )
        == snapshot_context
    )

    topic_only = research_domain.research_domain_context_for_run(
        {"topic": "Educational Assessment"}
    )
    legacy = research_domain.research_domain_context_for_run({})

    assert topic_only.source == research_domain.DomainContextSource.RUN_TOPIC
    assert topic_only.display_name == "Educational Assessment"
    assert legacy.source == research_domain.DomainContextSource.LEGACY_NEUTRAL
    assert legacy.display_name == research_domain.NEUTRAL_DOMAIN_LABEL
    assert not UNSUPPORTED_XR_LANGUAGE.search(legacy.display_name)
    with pytest.raises(
        research_domain.DomainContextError,
        match="^domain_context_invalid$",
    ):
        research_domain.research_domain_context_for_run(
            {"domain_context": {"schema": "unknown"}}
        )


def test_run_profile_snapshot_is_additive_immutable_and_inherited() -> None:
    request = RunCreateRequest(
        topic="Battery Recycling",
        queries=["lithium recovery", "closed-loop materials"],
    ).model_dump()
    first_profile = profile.build_run_profile(request)
    first_context = research_domain.research_domain_context_from_snapshot(
        first_profile["domain_context"]
    )

    inherited_profile = profile.build_run_profile(
        request,
        base_profile=deepcopy(first_profile),
    )
    inherited_context = research_domain.research_domain_context_from_snapshot(
        inherited_profile["domain_context"]
    )
    updated_profile = profile.build_run_profile(
        {**request, "topic": "Grid Reliability", "queries": ["power grid resilience"]},
        base_profile=deepcopy(first_profile),
    )
    updated_context = research_domain.research_domain_context_from_snapshot(
        updated_profile["domain_context"]
    )

    assert first_context == inherited_context
    assert first_context.display_name == "Battery Recycling"
    assert updated_context.display_name == "Grid Reliability"
    assert "lithium recovery" not in updated_context.search_queries
    assert first_context.display_name == "Battery Recycling"


def test_run_creation_persists_the_domain_context_without_live_profile_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved: list[dict] = []
    snapped: list[dict] = []

    def fake_create_run_record(**kwargs):
        return {
            "run_id": "synthetic-run",
            "topic": kwargs["topic"],
            "request": kwargs["request_payload"],
            "source": kwargs["source"],
            "write_legacy": kwargs["write_legacy"],
            "artifacts": {},
        }

    def fake_snapshot_profile(inputs_dir, run_profile):
        snapped.append(deepcopy(run_profile))
        return {
            "profile": "run:inputs/profile.json",
            "sources": "run:inputs/sources.json",
            "keywords": "run:inputs/keywords.json",
        }

    monkeypatch.setattr(runs, "create_run_record", fake_create_run_record)
    monkeypatch.setattr(runs, "snapshot_profile", fake_snapshot_profile)
    monkeypatch.setattr(
        runs,
        "save_run_record",
        lambda run: saved.append(deepcopy(run)),
    )
    monkeypatch.setattr(runs.metadata_index, "refresh_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(runs, "_append_event", lambda *args, **kwargs: None)

    created = runs.create_run_from_request(
        RunCreateRequest(
            topic="Educational Assessment",
            queries=["formative assessment"],
        ),
        write_legacy=False,
        topic_profile_id="internal-profile-id",
    )

    persisted_context = research_domain.research_domain_context_from_snapshot(
        created["domain_context"]
    )
    assert persisted_context.display_name == "Educational Assessment"
    assert persisted_context.search_queries == ("formative assessment",)
    assert snapped[0]["domain_context"] == created["domain_context"]
    assert saved[-1]["domain_context"] == created["domain_context"]
    assert "internal-profile-id" not in json.dumps(created["domain_context"])


def test_generic_llm_and_xr_synthesis_use_only_their_own_context(
    tmp_path: Path,
) -> None:
    generic = _render_synthesis(
        tmp_path,
        name="generic",
        domain_context=_context("Battery Recycling"),
        rows=[
            _triage_item(
                paper_id="battery-1",
                title="Closed-loop Lithium Recovery",
                tags=["lithium", "recycling"],
            )
        ],
    )
    llm = _render_synthesis(
        tmp_path,
        name="llm",
        domain_context=_context("Large Language Models"),
        rows=[
            _triage_item(
                paper_id="llm-1",
                title="Retrieval for Language Models",
                tags=["retrieval", "language model"],
            )
        ],
    )
    xr = _render_synthesis(
        tmp_path,
        name="xr",
        domain_context=_context("XR and AR Systems"),
        rows=[
            _triage_item(
                paper_id="xr-1",
                title="Tracked XR Interaction",
                tags=["XR", "tracking"],
            )
        ],
    )

    assert "# Weekly Research Brief — Battery Recycling" in generic
    assert "lithium" in generic
    assert not UNSUPPORTED_XR_LANGUAGE.search(generic)
    assert "Large Language Models" in llm
    assert "Battery Recycling" not in llm
    assert not UNSUPPORTED_XR_LANGUAGE.search(llm)
    assert "XR and AR Systems" in xr
    assert "Tracked XR Interaction" in xr


def test_synthesis_preserves_evidence_owned_xr_text_without_domain_overclaim(
    tmp_path: Path,
) -> None:
    title = "Virtual Reality Interfaces for Rehabilitation"
    content = _render_synthesis(
        tmp_path,
        name="evidence-xr",
        domain_context=_context("Rehabilitation Outcomes"),
        rows=[
            _triage_item(
                paper_id="rehab-1",
                title=title,
                tags=["rehabilitation"],
            )
        ],
    )

    assert title in content
    assert "# Weekly Research Brief — Rehabilitation Outcomes" in content
    assert "deployment-ready XR" not in content
    assert "headset study" not in content


def test_synthesis_has_no_cross_run_or_parallel_context_leakage(
    tmp_path: Path,
) -> None:
    rows = [
        _triage_item(
            paper_id="shared-1",
            title="A Domain-Neutral Evaluation",
            tags=["evaluation"],
        )
    ]
    contexts = {
        "xr-first": _context("XR Systems"),
        "generic-first": _context("Battery Recycling"),
        "llm": _context("Large Language Models"),
        "generic-second": _context("Battery Recycling"),
    }

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            name: executor.submit(
                _render_synthesis,
                tmp_path,
                name=name,
                domain_context=context,
                rows=rows,
            )
            for name, context in contexts.items()
        }
    outputs = {name: future.result() for name, future in futures.items()}

    assert outputs["generic-first"] == outputs["generic-second"]
    assert not UNSUPPORTED_XR_LANGUAGE.search(outputs["generic-first"])
    assert "Battery Recycling" not in outputs["llm"]
    assert "Large Language Models" not in outputs["generic-second"]
    assert "XR Systems" not in outputs["generic-second"]


def test_synthesis_with_no_recent_evidence_is_truthful(tmp_path: Path) -> None:
    content = _render_synthesis(
        tmp_path,
        name="no-recent",
        domain_context=_context("Battery Recycling"),
        rows=[
            _triage_item(
                paper_id="old-1",
                title="Old Battery Study",
                tags=["battery"],
                published="2025-01-01T00:00:00Z",
            )
        ],
    )

    assert "Selected top: 0" in content
    assert "evidence-backed 연구 공백을 도출하지 않았습니다" in content
    assert "근거가 부족하므로 구체적인 가설, 방법, 데이터, 지표 또는 결과를 제안하지 않습니다" in content
    assert not UNSUPPORTED_XR_LANGUAGE.search(content)


def test_notes_and_experiment_scaffold_use_the_explicit_domain_context(
    tmp_path: Path,
) -> None:
    item = _triage_item(
        paper_id="battery-2",
        title="Hydrometallurgical Recovery",
        tags=[],
    )
    note_paths = notes.generate_notes(
        items=[item],
        template_path=Path("templates/paper_summary_template.md"),
        out_dir=tmp_path / "notes",
        top_n=1,
        source_label="synthetic",
        domain_context=_context("Battery Recycling"),
    )
    triage_path = tmp_path / "triage.jsonl"
    _write_jsonl(triage_path, [item])
    experiment_result = experiments.create_experiment_scaffold(
        source_path=triage_path,
        rank=1,
        out_root=tmp_path / "experiments",
        domain_context=_context("Battery Recycling"),
    )
    note = Path(note_paths[0]).read_text(encoding="utf-8")
    readme = (
        Path(experiment_result["experiment_dir"]) / "README.md"
    ).read_text(encoding="utf-8")
    config = json.loads(
        (Path(experiment_result["experiment_dir"]) / "config.json").read_text(
            encoding="utf-8"
        )
    )

    assert "## Battery Recycling Relevance" in note
    assert "Battery Recycling" in readme
    assert not UNSUPPORTED_XR_LANGUAGE.search(note)
    assert not UNSUPPORTED_XR_LANGUAGE.search(readme)
    assert config["artifact_kind"] == "research_design_scaffold"
    assert config["execution_status"] == "not_executed"
    assert config["executable"] is False
    assert config["runner_available"] is False
    assert config["proposed_evaluation"]["measured_values"] == []


def _evidence_reference(
    *,
    snippet: str = "A synthetic result about battery recovery.",
) -> evidence.EvidenceReference:
    return evidence.EvidenceReference(
        evidence_id="evidence-1",
        source_id="paper-1",
        paper_id="paper-1",
        title="Battery Recovery Evidence",
        source_type="abstract",
        snippet=snippet,
        citation_text="Battery Recovery Evidence [paper-1]",
    )


def test_research_plan_fallback_is_domain_driven_and_preserves_evidence_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_text = "Virtual Reality is named in this source evidence."
    run = {
        "run_id": "synthetic-run",
        "topic": "Battery Recycling",
        "domain_context": _context("Battery Recycling").to_snapshot(),
        "artifacts": {},
        "counts": {"triage_items": 1},
    }
    monkeypatch.setattr(research_plan, "load_run_record", lambda run_id: run)
    monkeypatch.setattr(
        research_plan.evidence,
        "collect_run_evidence",
        lambda run_id: [_evidence_reference(snippet=evidence_text)],
    )
    monkeypatch.setattr(
        research_plan.evidence,
        "collect_research_context",
        lambda run_id: {
            "papers": [{"paper_id": "paper-1", "title": "Battery Recovery Evidence"}],
            "themes": [{"theme": "materials recovery"}],
            "evidence": [],
        },
    )
    provider_calls = {"count": 0}

    def no_provider():
        provider_calls["count"] += 1
        return None

    monkeypatch.setattr(research_plan, "get_active_provider", no_provider)
    monkeypatch.setattr(
        research_plan,
        "_artifact_paths",
        lambda run_id: (
            tmp_path / "research_plan.json",
            tmp_path / "research_plan.md",
        ),
    )
    monkeypatch.setattr(
        research_plan,
        "normalize_payload_references",
        lambda payload, **kwargs: payload,
    )
    monkeypatch.setattr(research_plan, "save_run_record", lambda payload: None)

    plan = research_plan.generate_run_research_plan("synthetic-run")
    markdown = (tmp_path / "research_plan.md").read_text(encoding="utf-8")

    assert plan.generator.mode == "heuristic"
    assert provider_calls["count"] == 1
    assert "Battery Recycling" in markdown
    assert evidence_text in markdown
    template_without_evidence = markdown.replace(evidence_text, "")
    assert not UNSUPPORTED_XR_LANGUAGE.search(template_without_evidence)
    assert "deployment-ready XR" not in markdown


def test_research_plan_zero_evidence_does_not_invent_findings() -> None:
    sections = research_plan._fallback_sections(
        {"topic": "Battery Recycling"},
        {"papers": [], "themes": []},
        [],
        _context("Battery Recycling"),
    )
    rendered = "\n".join(
        text
        for section in sections
        for text in [
            section.summary,
            *[claim.claim_text for claim in section.claims],
        ]
    )

    assert all(not section.claims for section in sections)
    assert "no citable evidence" in rendered
    assert "not evidence of a substantive research gap" in rendered
    assert not UNSUPPORTED_XR_LANGUAGE.search(rendered)


def test_weekly_journal_is_profile_aware_and_domain_neutral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_weekly_external_state(monkeypatch)
    profile_payload = {
        "title": "Maritime Decarbonization",
        "topic": "Maritime Decarbonization",
        "queries": ["green shipping fuels"],
    }
    reviews_payload = [
        _review(
            paper_id="ship-1",
            title="Green Methanol for Shipping",
            abstract="Shipping fuel pathways compare methanol and lifecycle emissions.",
        ),
        _review(
            paper_id="ship-2",
            title="Port Fuel Infrastructure",
            abstract="Port infrastructure affects shipping fuel deployment.",
            status="full_text_available",
        ),
    ]

    markdown = reporting.build_weekly_journal_markdown(
        topic_profile=profile_payload,
        reviews=reviews_payload,
        generated_at="2026-07-27T12:00:00+00:00",
    )

    assert markdown.startswith("# Maritime Decarbonization 주간 연구 저널")
    assert "제한된 근거 범위의 baseline" in markdown
    assert "full_text_available 1편" in markdown
    assert "초록만 검토" in markdown
    assert "Green Methanol for Shipping" in markdown
    assert "다음 주 모니터링 포인트" in markdown
    assert not UNSUPPORTED_XR_LANGUAGE.search(markdown)


def test_weekly_journal_preserves_evidence_owned_domain_terms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_weekly_external_state(monkeypatch)
    title = "Virtual Reality Exposure in Clinical Rehabilitation"
    markdown = reporting.build_weekly_journal_markdown(
        topic_profile={
            "title": "Clinical Rehabilitation",
            "topic": "Clinical Rehabilitation",
        },
        reviews=[
            _review(
                paper_id="clinical-1",
                title=title,
                abstract="The source reports a virtual reality intervention.",
            )
        ],
        generated_at="2026-07-27T12:00:00+00:00",
    )

    assert title in markdown
    assert markdown.startswith("# Clinical Rehabilitation")
    assert "XR system" not in markdown
    assert "headset" not in markdown


def test_weekly_journal_zero_paper_case_is_truthful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_weekly_external_state(monkeypatch)
    markdown = reporting.build_weekly_journal_markdown(
        topic_profile={},
        reviews=[],
        generated_at="2026-07-27T12:00:00+00:00",
    )

    assert markdown.startswith("# Research Topic 주간 연구 저널")
    assert "검토된 논문이 없어 trend, finding, 또는 연구 공백을 substantive result로 단정하지 않았습니다" in markdown
    assert "저장된 paper review가 없어 흐름을 산출하지 못했습니다" in markdown
    assert not UNSUPPORTED_XR_LANGUAGE.search(markdown)


def test_weekly_regeneration_prefers_the_persisted_run_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_weekly_external_state(monkeypatch)
    persisted = _context("Original Battery Context")
    monkeypatch.setattr(
        reporting,
        "load_run_record",
        lambda run_id: {
            "run_id": run_id,
            "topic": "Original Battery Context",
            "domain_context": persisted.to_snapshot(),
        },
    )
    monkeypatch.setattr(reporting.paper_reviews, "list_reviews", lambda **kwargs: [])
    monkeypatch.setattr(
        reporting,
        "_topic_relevant_review_denominator",
        lambda *args, **kwargs: 0,
    )
    monkeypatch.setattr(
        reporting,
        "_accepted_ai_drafts_for_reviews",
        lambda *args, **kwargs: {},
    )

    result = reporting.generate_weekly_journal_report_from_reviews(
        topic_profile={
            "profile_id": "mutable-profile",
            "title": "Mutated Live Profile",
            "topic": "Mutated Live Profile",
        },
        topic_id="mutable-profile",
        run_id="persisted-run",
        generated_at="2026-07-27T12:00:00+00:00",
    )

    assert result["markdown"].startswith(
        "# Original Battery Context 주간 연구 저널"
    )
    assert "Mutated Live Profile" not in result["markdown"]


def test_legacy_journal_cli_service_uses_neutral_or_explicit_context(
    tmp_path: Path,
) -> None:
    triage_path = tmp_path / "triage.jsonl"
    _write_jsonl(
        triage_path,
        [
            _triage_item(
                paper_id="battery-3",
                title="Battery Recycling Review",
                tags=["battery"],
            )
        ],
    )
    result = reviews.compile_journal(
        run_date="2026-07-27",
        triage_path=triage_path,
        shortlist=1,
        deep_dive=0,
        reviews_dir=tmp_path / "reviews",
        out_dir=tmp_path / "journal",
        require_fulltext=False,
        domain_context=_context("Battery Recycling"),
    )
    journal = json.loads(Path(result["output"]).read_text(encoding="utf-8"))

    assert journal["domain_context"]["display_name"] == "Battery Recycling"
    assert journal["editorial_ko"]["this_week_takeaway"].startswith(
        "이번 주 Battery Recycling"
    )
    assert not UNSUPPORTED_XR_LANGUAGE.search(
        journal["editorial_ko"]["this_week_takeaway"]
    )


def test_scoped_generated_text_has_no_unsupported_hardcoded_domain_literals() -> None:
    paths = [
        Path("app/services/synthesis.py"),
        Path("app/services/research_plan.py"),
        Path("app/services/reporting.py"),
        Path("app/services/notes.py"),
        Path("app/services/experiments.py"),
        Path("app/services/reviews.py"),
        Path("templates/paper_summary_template.md"),
        Path("tools/make_journal_pdf.py"),
    ]
    banned_literals = [
        "Weekly XRML Research Brief",
        "deployment-ready XR runtime constraints",
        "XR 환경에서",
        "XR, AR, VR",
        "AR/XR Integration",
        "RAG, agent, alignment, reasoning 관련 신규 논문",
        "XR/AR × ML(Image/Video) 논문 리뷰 위클리 저널",
        "XR 관점 포인트",
        "이번 주 XRML:",
    ]

    for path in paths:
        source = path.read_text(encoding="utf-8")
        for literal in banned_literals:
            assert literal not in source, f"{literal!r} remains in {path}"
