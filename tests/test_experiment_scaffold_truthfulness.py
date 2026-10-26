from __future__ import annotations

import builtins
import hashlib
import importlib
import json
import os
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.services import (
    evidence,
    experiments,
    reporting,
    research_domain,
    research_plan,
    runs,
    synthesis,
)

FIXED_NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
EXPECTED_UNRESOLVED = {
    "dataset_not_selected",
    "sample_size_not_specified",
    "evaluation_metric_not_selected",
    "baseline_not_selected",
    "execution_environment_not_defined",
    "implementation_not_available",
}
UNSUPPORTED_XR_TERMS = (
    " xr ",
    " ar ",
    " vr ",
    "headset",
    "slam",
    "immersive",
    "augmented reality",
    "virtual reality",
)


def _context(
    title: str,
    *,
    questions: list[str] | None = None,
    description: str = "",
    source: research_domain.DomainContextSource = (
        research_domain.DomainContextSource.RUN_PROFILE_SNAPSHOT
    ),
) -> research_domain.ResearchDomainContext:
    return research_domain.build_research_domain_context(
        profile={
            "title": title,
            "topic": title,
            "description": description,
            "research_questions": questions or [],
        },
        source=source,
    )


def _write_source(
    root: Path,
    *,
    paper_id: str = "paper-1",
    title: str = "Synthetic Research Paper",
    evidence_refs: list[object] | None = None,
) -> Path:
    source = root / "triage.jsonl"
    source.parent.mkdir(parents=True, exist_ok=True)
    item = {
        "paper_id": paper_id,
        "title": title,
        "published": "2026-07-26T00:00:00Z",
        "evidence_refs": evidence_refs or [],
    }
    source.write_text(json.dumps(item, ensure_ascii=False) + "\n", encoding="utf-8")
    return source


def _generate(
    root: Path,
    *,
    context: research_domain.ResearchDomainContext | None = None,
    evidence_refs: list[object] | None = None,
    title: str = "Synthetic Research Paper",
) -> tuple[dict, Path, dict, str]:
    source = _write_source(root, title=title, evidence_refs=evidence_refs)
    result = experiments.create_experiment_scaffold(
        source_path=source,
        rank=1,
        out_root=root / "experiments",
        domain_context=context,
        generated_at=FIXED_NOW,
    )
    artifact_dir = Path(result["experiment_dir"])
    config = json.loads((artifact_dir / "config.json").read_text(encoding="utf-8"))
    readme = (artifact_dir / "README.md").read_text(encoding="utf-8")
    return result, artifact_dir, config, readme


def _file_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    }


def test_new_artifact_is_explicitly_scaffold_only_and_not_executed(
    tmp_path: Path,
) -> None:
    result, artifact_dir, config, readme = _generate(
        tmp_path,
        context=_context(
            "Battery Recycling",
            questions=["Which documented process assumptions require comparison?"],
        ),
    )

    assert result["status"] == "scaffold_created"
    assert result["execution_status"] == "not_executed"
    assert result["executable"] is False
    assert result["runner_available"] is False
    assert config["schema_version"] == "research_design_scaffold_v1"
    assert config["artifact_kind"] == "research_design_scaffold"
    assert config["lifecycle_state"] == "draft_scaffold"
    assert config["generation_status"] == "scaffold_created"
    assert config["execution_status"] == "not_executed"
    assert config["executable"] is False
    assert config["runner_available"] is False
    assert config["truth_classification"] == [
        "proposal",
        "not_observation",
        "not_result",
    ]
    assert config["proposed_evaluation"]["metric_names"] == []
    assert config["proposed_evaluation"]["measured_values"] == []
    assert config["proposed_evaluation"]["success_criteria"] is None
    assert "not_executed" in readme
    assert "non-executable research design proposal" in readme
    assert "not an observation" in readme
    assert set(path.name for path in artifact_dir.iterdir()) == {
        "README.md",
        "config.json",
    }


def test_scaffold_has_no_ambiguous_placeholders_or_generated_execution_files(
    tmp_path: Path,
) -> None:
    _, artifact_dir, config, readme = _generate(
        tmp_path,
        context=_context("General Scientific Research"),
    )
    visible = readme + "\n" + json.dumps(config, ensure_ascii=False)
    lowered = visible.casefold()
    unresolved = {item["code"] for item in config["unresolved_items"]}

    assert "tbd" not in lowered
    assert "todo" not in lowered
    assert "implement runner" not in lowered
    assert "run this command" not in lowered
    assert "ready to execute" not in lowered
    assert "results pending" not in lowered
    assert "experiment results" not in lowered
    assert "measured improvement" not in lowered
    assert unresolved == EXPECTED_UNRESOLVED
    assert config["required_inputs"] == [
        item["code"] for item in config["unresolved_items"]
    ]
    assert not any(
        path.suffix.casefold() in {".py", ".ipynb", ".sh", ".ps1", ".bat", ".cmd"}
        for path in artifact_dir.rglob("*")
    )
    assert not any(
        key in config
        for key in (
            "command",
            "script",
            "notebook",
            "owner",
            "owner_user_id",
            "provider",
            "provider_id",
            "path",
        )
    )


def test_only_relevant_ethics_input_is_added(tmp_path: Path) -> None:
    _, _, generic, _ = _generate(
        tmp_path / "generic",
        context=_context("Materials Research"),
    )
    _, _, human, _ = _generate(
        tmp_path / "human",
        context=_context(
            "Clinical Interview Research",
            description="A future human participant interview design.",
        ),
    )

    generic_codes = {item["code"] for item in generic["unresolved_items"]}
    human_codes = {item["code"] for item in human["unresolved_items"]}
    assert "ethics_review_not_assessed" not in generic_codes
    assert human_codes == EXPECTED_UNRESOLVED | {"ethics_review_not_assessed"}


@pytest.mark.parametrize(
    ("name", "title", "forbidden_terms"),
    [
        ("generic", "General Scientific Research", UNSUPPORTED_XR_TERMS),
        ("llm", "Large Language Models", UNSUPPORTED_XR_TERMS),
        ("xr", "XR Interaction Research", ()),
    ],
)
def test_profile_specific_scaffolds_preserve_domain_without_fake_results(
    tmp_path: Path,
    name: str,
    title: str,
    forbidden_terms: tuple[str, ...],
) -> None:
    _, _, config, readme = _generate(
        tmp_path / name,
        context=_context(
            title,
            questions=[f"What evidence is needed for a future {title} study?"],
        ),
    )
    rendered = f" {readme.casefold()} "

    assert title in readme
    assert config["execution_status"] == "not_executed"
    assert config["proposed_evaluation"]["measured_values"] == []
    assert not any(term in rendered for term in forbidden_terms)


def test_legacy_no_profile_scaffold_is_neutral_and_does_not_migrate_input(
    tmp_path: Path,
) -> None:
    source = _write_source(tmp_path, title="Neutral Paper")
    before = source.read_bytes()
    result = experiments.create_experiment_scaffold(
        source_path=source,
        rank=1,
        out_root=tmp_path / "experiments",
        generated_at=FIXED_NOW,
    )
    config = json.loads(
        (Path(result["experiment_dir"]) / "config.json").read_text(encoding="utf-8")
    )
    rendered = json.dumps(config, ensure_ascii=False).casefold()

    assert config["domain_context"]["display_name"] == "Research Topic"
    assert config["domain_context_source"] == "legacy_neutral"
    assert not any(term in f" {rendered} " for term in UNSUPPORTED_XR_TERMS)
    assert source.read_bytes() == before


def test_evidence_refs_are_preserved_without_fabricating_or_exposing_paths(
    tmp_path: Path,
) -> None:
    refs: list[object] = [
        "evidence-1",
        {"evidence_id": "evidence:2"},
        "evidence-1",
        r"C:\private\evidence.json",
        "../escape",
        {"artifact_path": "run:reviews/private.json"},
    ]
    _, _, config, _ = _generate(
        tmp_path,
        context=_context("Evidence-Grounded Research"),
        evidence_refs=refs,
    )

    assert config["evidence_refs"] == ["evidence-1", "evidence:2"]
    assert config["proposal_basis"]["evidence_status"] == "explicit_refs_available"
    assert "artifact_path" not in json.dumps(config)


def test_zero_evidence_is_marked_insufficient_without_fake_refs(
    tmp_path: Path,
) -> None:
    _, _, config, readme = _generate(
        tmp_path,
        context=_context("Zero Evidence Research"),
    )

    assert config["evidence_refs"] == []
    assert config["proposal_basis"]["evidence_status"] == "insufficient_evidence"
    assert "No explicit evidence reference is attached" in readme


def test_same_input_and_context_produce_deterministic_semantic_content(
    tmp_path: Path,
) -> None:
    context = _context(
        "Deterministic Research",
        questions=["What condition should a future proposal test?"],
    )
    _, _, first_config, first_readme = _generate(tmp_path / "first", context=context)
    _, _, second_config, second_readme = _generate(tmp_path / "second", context=context)

    assert first_config == second_config
    assert first_readme == second_readme


def test_empty_source_writes_nothing_and_malformed_source_fails_closed(
    tmp_path: Path,
) -> None:
    empty_source = tmp_path / "empty.jsonl"
    empty_source.write_text("", encoding="utf-8")
    empty_out = tmp_path / "empty-out"

    empty_result = experiments.create_experiment_scaffold(
        source_path=empty_source,
        rank=1,
        out_root=empty_out,
        domain_context=_context("Empty Research"),
        generated_at=FIXED_NOW,
    )

    assert empty_result["status"] == "not_generated"
    assert empty_result["execution_status"] == "not_executed"
    assert not empty_out.exists()

    malformed_source = tmp_path / "malformed.jsonl"
    malformed_source.write_text('{"paper_id":', encoding="utf-8")
    with pytest.raises(experiments.ExperimentScaffoldError) as exc_info:
        experiments.create_experiment_scaffold(
            source_path=malformed_source,
            rank=1,
            out_root=tmp_path / "malformed-out",
            domain_context=_context("Malformed Research"),
            generated_at=FIXED_NOW,
        )
    assert exc_info.value.code == "experiment_scaffold_invalid"
    assert str(tmp_path) not in str(exc_info.value)
    assert not (tmp_path / "malformed-out").exists()


def test_repeat_generation_fails_closed_without_overwriting_any_file(
    tmp_path: Path,
) -> None:
    source = _write_source(tmp_path)
    out_root = tmp_path / "experiments"
    untouched = out_root / "unrelated.txt"
    untouched.parent.mkdir(parents=True)
    untouched.write_text("preserve-me", encoding="utf-8")

    experiments.create_experiment_scaffold(
        source_path=source,
        rank=1,
        out_root=out_root,
        domain_context=_context("Collision Research"),
        generated_at=FIXED_NOW,
    )
    before = _file_hashes(out_root)
    with pytest.raises(experiments.ExperimentArtifactConflict) as exc_info:
        experiments.create_experiment_scaffold(
            source_path=source,
            rank=1,
            out_root=out_root,
            domain_context=_context("Collision Research"),
            generated_at=FIXED_NOW,
        )

    assert exc_info.value.code == "experiment_artifact_conflict"
    assert _file_hashes(out_root) == before
    assert untouched.read_text(encoding="utf-8") == "preserve-me"


def test_concurrent_same_run_generation_has_one_winner_and_no_overwrite(
    tmp_path: Path,
) -> None:
    source = _write_source(tmp_path)
    out_root = tmp_path / "experiments"
    context = _context("Concurrent Research")

    def generate_once() -> str:
        try:
            return str(
                experiments.create_experiment_scaffold(
                    source_path=source,
                    rank=1,
                    out_root=out_root,
                    domain_context=context,
                    generated_at=FIXED_NOW,
                )["status"]
            )
        except experiments.ExperimentScaffoldError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = sorted(executor.map(lambda _index: generate_once(), range(2)))

    assert results == ["experiment_artifact_conflict", "scaffold_created"]
    assert set(path.name for path in (out_root / "proposal").iterdir()) == {
        "README.md",
        "config.json",
    }


def test_cross_run_namespaces_are_isolated(tmp_path: Path) -> None:
    context = _context("Cross-Run Research")
    first, _, first_config, _ = _generate(tmp_path / "run-a", context=context)
    second, _, second_config, _ = _generate(tmp_path / "run-b", context=context)

    assert first["experiment_dir"] != second["experiment_dir"]
    assert Path(first["experiment_dir"]).parent != Path(second["experiment_dir"]).parent
    assert first_config == second_config


def test_legacy_and_missing_read_statuses_are_fail_safe_and_side_effect_free(
    tmp_path: Path,
) -> None:
    legacy = {
        "run_id": "legacy-run",
        "artifacts": {"experiment_dir": "run:experiments/legacy"},
    }
    missing = {"run_id": "missing-run", "artifacts": {}}
    legacy_before = deepcopy(legacy)
    missing_before = deepcopy(missing)
    filesystem_before = list(tmp_path.rglob("*"))

    legacy_presented = experiments.present_run_experiment_status(legacy)
    missing_presented = experiments.present_run_experiment_status(missing)

    assert legacy_presented["experiment_status"]["execution_status"] == "unknown_legacy"
    assert legacy_presented["experiment_status"]["executable"] is False
    assert legacy_presented["experiment_status"]["runner_available"] is False
    assert missing_presented["experiment_status"]["lifecycle_state"] == "not_generated"
    assert missing_presented["experiment_status"]["execution_status"] == "not_executed"
    assert legacy == legacy_before
    assert missing == missing_before
    assert list(tmp_path.rglob("*")) == filesystem_before


def test_new_read_status_exposes_only_safe_additive_contract(tmp_path: Path) -> None:
    _, _, config, _ = _generate(
        tmp_path,
        context=_context("Public Contract Research"),
    )
    config["artifact_ref"] = "run:experiments/proposal"
    run = {
        "run_id": "safe-run",
        "artifacts": {
            "experiment_dir": "run:experiments/proposal",
            "experiment_scaffold": config,
        },
    }

    presented = experiments.present_run_experiment_status(run)
    status = presented["experiment_status"]
    serialized = json.dumps(status, ensure_ascii=False)

    assert status["artifact_kind"] == "research_design_scaffold"
    assert status["execution_status"] == "not_executed"
    assert status["artifact_ref"] == "run:experiments/proposal"
    assert status["executable"] is False
    assert status["runner_available"] is False
    assert "created_at_utc" not in status
    assert "hypothesis" not in status
    assert "proposed_method" not in status
    assert "C:\\" not in serialized
    assert run["artifacts"]["experiment_dir"] == "run:experiments/proposal"


def test_run_read_surfaces_never_generate_or_rewrite_experiment_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = {
        "run_id": "legacy-read-run",
        "artifacts": {"experiment_dir": "run:experiments/legacy"},
    }
    before = deepcopy(stored)
    generation_calls: list[object] = []
    monkeypatch.setattr(runs, "load_run_record", lambda _run_id: deepcopy(stored))
    monkeypatch.setattr(runs, "list_run_records", lambda limit: [deepcopy(stored)])
    monkeypatch.setattr(
        experiments,
        "create_experiment_scaffold",
        lambda **kwargs: generation_calls.append(kwargs),
    )

    detail = runs.get_run("legacy-read-run")
    listing = runs.list_runs()

    assert detail["experiment_status"]["execution_status"] == "unknown_legacy"
    assert listing[0]["experiment_status"]["execution_status"] == "unknown_legacy"
    assert generation_calls == []
    assert stored == before


def test_openapi_describes_generation_only_and_has_no_experiment_execute_route() -> None:
    from app.main import create_app

    schema = create_app().openapi()
    experiment_field = schema["components"]["schemas"]["RunCreateRequest"][
        "properties"
    ]["experiment"]
    experiment_paths = [
        path for path in schema["paths"] if "experiment" in path.casefold()
    ]

    assert experiment_field["default"] is False
    assert "non-executable research design proposal scaffold" in experiment_field[
        "description"
    ]
    assert "does not run or validate" in experiment_field["description"]
    assert experiment_paths == []


def test_legacy_cli_reports_scaffold_generation_without_absolute_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    absolute_artifact = tmp_path / "private" / "proposal"
    monkeypatch.setattr(
        experiments,
        "create_experiment_scaffold",
        lambda **_kwargs: {
            "status": "scaffold_created",
            "generation_status": "scaffold_created",
            "execution_status": "not_executed",
            "executable": False,
            "runner_available": False,
            "experiment_dir": str(absolute_artifact),
            "artifact": {"artifact_kind": "research_design_scaffold"},
        },
    )

    exit_code = experiments.main(["--from", "triage", "--rank", "1"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "scaffold_created" in output
    assert "not_executed" in output
    assert "project:experiments/proposal" in output
    assert str(tmp_path) not in output


def test_generation_calls_no_execution_provider_or_network_primitive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def blocked(name: str):
        def fail(*_args, **_kwargs):
            calls.append(name)
            raise AssertionError(f"forbidden primitive called: {name}")

        return fail

    monkeypatch.setattr(subprocess, "run", blocked("subprocess.run"))
    monkeypatch.setattr(subprocess, "Popen", blocked("subprocess.Popen"))
    monkeypatch.setattr(os, "system", blocked("os.system"))
    monkeypatch.setattr(socket, "getaddrinfo", blocked("socket.getaddrinfo"))
    monkeypatch.setattr(socket, "create_connection", blocked("socket.create_connection"))
    monkeypatch.setattr(httpx, "request", blocked("httpx.request"))
    monkeypatch.setattr(httpx.Client, "request", blocked("httpx.Client.request"))
    monkeypatch.setattr(importlib, "import_module", blocked("importlib.import_module"))
    monkeypatch.setattr(builtins, "eval", blocked("eval"))
    monkeypatch.setattr(builtins, "exec", blocked("exec"))

    _generate(tmp_path, context=_context("Offline Research"))

    assert calls == []


def test_run_integration_records_generation_without_execution_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    triage_dir = tmp_path / "run" / "triage"
    _write_source(triage_dir)
    paths = SimpleNamespace(run_dir=tmp_path / "run", triage_dir=triage_dir)
    state = {
        "run_id": "run-integration",
        "source": "scheduled_topic",
        "steps": [],
        "artifacts": {},
    }
    events: list[tuple[str, dict]] = []

    def load_record(_run_id: str) -> dict:
        return deepcopy(state)

    def save_record(record: dict) -> dict:
        state.clear()
        state.update(deepcopy(record))
        return record

    monkeypatch.setattr(runs, "load_run_record", load_record)
    monkeypatch.setattr(runs, "save_run_record", save_record)
    monkeypatch.setattr(
        runs,
        "_append_event",
        lambda _run_id, event_type, payload: events.append((event_type, payload)),
    )
    monkeypatch.setattr(
        runs,
        "normalize_path_to_ref",
        lambda _path, *, run_id: "run:experiments/proposal",
    )

    runs._generate_experiment_scaffold_for_run(
        run_id="run-integration",
        paths=paths,
        request={"rank": 1},
        domain_context=_context("Scheduled Research"),
    )

    step = next(item for item in state["steps"] if item["name"] == "experiment")
    contract = state["artifacts"]["experiment_scaffold"]
    assert step["status"] == "scaffold_created"
    assert step["execution_status"] == "not_executed"
    assert step["executable"] is False
    assert step["runner_available"] is False
    assert contract["execution_status"] == "not_executed"
    assert contract["artifact_ref"] == "run:experiments/proposal"
    assert [event_type for event_type, _payload in events] == [
        "step.started",
        "experiment.scaffold_created",
    ]
    assert not any("completed" in event_type for event_type, _payload in events)


def test_synthesis_research_plan_and_weekly_journal_label_proposals_truthfully(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(
        "Battery Recycling",
        questions=["Which evidence gap should a future study address?"],
    )
    source = _write_source(tmp_path / "synthesis", title="Battery Evidence Paper")
    synthesis_path = tmp_path / "weekly.md"
    synthesis.synthesize_weekly(
        triage_path=source,
        out_path=synthesis_path,
        top_n=3,
        domain_context=context,
        generated_at=FIXED_NOW,
    )
    weekly = synthesis_path.read_text(encoding="utf-8")
    assert "Proposed Experiment Scaffolds (Not Executed)" in weekly
    assert "execution_status=not_executed" in weekly
    assert "Unresolved inputs" in weekly
    assert "empirical finding" not in weekly.casefold()

    reference = evidence.EvidenceReference(
        evidence_id="ev-1",
        source_id="paper-1",
        paper_id="paper-1",
        title="Battery Evidence Paper",
        source_type="abstract",
        snippet="A preserved synthetic evidence snippet.",
        citation_text="Battery Evidence Paper [paper-1]",
    )
    sections = research_plan._fallback_sections(
        {"topic": "Battery Recycling"},
        {
            "papers": [{"paper_id": "paper-1", "title": "Battery Evidence Paper"}],
            "themes": [{"theme": "recycling", "count": 1}],
        },
        [reference],
        context,
    )
    plan = evidence.ResearchPlanArtifact(
        run_id="synthetic-run",
        topic="Battery Recycling",
        generated_at=FIXED_NOW.isoformat(),
        evidence_count=1,
        source_count=1,
        sections=sections,
    )
    plan_markdown = research_plan.render_plan_markdown(plan)
    assert "Proposed Experiment Scaffolds (Not Executed)" in plan_markdown
    assert "execution_status=not_executed" in plan_markdown
    assert "unresolved inputs" in plan_markdown
    assert "A preserved synthetic evidence snippet." in plan_markdown

    _, _, config, _ = _generate(
        tmp_path / "journal-scaffold",
        context=context,
    )
    status = experiments.classify_experiment_artifact(config)
    monkeypatch.setattr(
        reporting.provider_review_coverage,
        "build_provider_review_coverage_summary",
        lambda *_args, **_kwargs: {
            "providerOrchestratedAcceptedCount": 0,
            "fixtureSyntheticCount": 0,
            "realProviderAcceptedCount": 0,
            "coverageWarning": None,
        },
    )
    journal = reporting.build_weekly_journal_markdown(
        topic_profile={"title": "Battery Recycling", "topic": "Battery Recycling"},
        reviews=[],
        generated_at=FIXED_NOW.isoformat(),
        domain_context=context,
        experiment_status=status,
    )
    assert "Proposed Experiment Scaffold (Not Executed)" in journal
    assert "execution_status: `not_executed`" in journal
    assert "runner_available: `false`" in journal
    assert "Required inputs remain unresolved" in journal
    assert "not empirical evidence or an experiment result" in journal


def test_provider_next_experiment_text_cannot_claim_measured_results() -> None:
    reference = evidence.EvidenceReference(
        evidence_id="ev-1",
        source_id="paper-1",
        paper_id="paper-1",
        title="Synthetic Evidence",
        source_type="abstract",
        snippet="Preserved source evidence.",
        citation_text="Synthetic Evidence [paper-1]",
    )
    sections = research_plan._normalize_provider_sections(
        {
            "sections": [
                {
                    "section_id": "next-experiments",
                    "summary": "Experiment completed with a measured improvement.",
                    "paper_ids": ["paper-1"],
                    "claims": [
                        {
                            "claim_id": "unsafe-claim",
                            "claim_text": "Measured improvement was 99 percent.",
                            "importance": "high",
                            "citations": [{"evidence_id": "ev-1"}],
                        }
                    ],
                }
            ]
        },
        [reference],
    )

    rendered = json.dumps(
        [section.model_dump() for section in sections],
        ensure_ascii=False,
    )
    assert "99 percent" not in rendered
    assert "measured improvement" not in rendered.casefold()
    assert "not execute" in rendered.casefold()
    assert sections[0].claims[0].citations[0].evidence_id == "ev-1"


def test_experiment_source_guard_contains_no_execution_adapter_or_placeholder() -> None:
    source = Path(experiments.__file__).read_text(encoding="utf-8")
    lowered = source.casefold()

    assert "subprocess" not in lowered
    assert "os.system" not in lowered
    assert "shell=true" not in lowered
    assert "eval(" not in lowered
    assert "exec(" not in lowered
    assert "docker" not in lowered
    assert "run.py" not in lowered
    assert ".ipynb" not in lowered
    assert '"tbd"' not in lowered
    assert '"todo"' not in lowered
    assert "--out_root" not in source
    assert "--out-root" not in source
