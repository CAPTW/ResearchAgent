from __future__ import annotations

import argparse
import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.domain.constants import LEGACY_EXPERIMENTS_ROOT
from app.repositories.artifact_refs import normalize_path_to_ref
from app.repositories.files import write_json, write_text
from app.repositories.jsonl import JsonlIntegrityError, read_jsonl_strict
from app.services.research_domain import (
    DomainContextSource,
    ResearchDomainContext,
    build_research_domain_context,
    markdown_inline,
)

SCAFFOLD_SCHEMA_VERSION = "research_design_scaffold_v1"
SCAFFOLD_ARTIFACT_KIND = "research_design_scaffold"
SCAFFOLD_LIFECYCLE_STATE = "draft_scaffold"
SCAFFOLD_EXECUTION_STATUS = "not_executed"
SCAFFOLD_GENERATION_STATUS = "scaffold_created"
SCAFFOLD_DIRECTORY_NAME = "proposal"

_REQUIRED_INPUTS: tuple[tuple[str, str], ...] = (
    (
        "dataset_not_selected",
        "Select the dataset or study materials before any separately authorized future execution.",
    ),
    (
        "sample_size_not_specified",
        "Specify and justify the sample size before any separately authorized future execution.",
    ),
    (
        "evaluation_metric_not_selected",
        "Select evidence-supported evaluation metrics before any separately authorized future execution.",
    ),
    (
        "baseline_not_selected",
        "Select an evidence-supported comparison baseline before any separately authorized future execution.",
    ),
    (
        "execution_environment_not_defined",
        "Define an approved execution environment outside this scaffold.",
    ),
    (
        "implementation_not_available",
        "No implementation or experiment runner is included in this scaffold.",
    ),
)
_ETHICS_TERMS = re.compile(
    r"(?i)\b(?:human|participant|patient|clinical|interview|survey|user study)\b|"
    r"인간\s*대상|참가자|환자|임상|인터뷰|설문|사용자\s*연구"
)
_SAFE_EVIDENCE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")


class ExperimentScaffoldError(RuntimeError):
    """Stable, redacted scaffold-generation failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ExperimentArtifactConflict(ExperimentScaffoldError):
    def __init__(self) -> None:
        super().__init__("experiment_artifact_conflict")


def _bounded_text(value: Any, *, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit].rstrip()


def _evidence_refs(item: dict[str, Any]) -> list[str]:
    raw_refs = item.get("evidence_refs")
    if not isinstance(raw_refs, list):
        return []
    refs: list[str] = []
    for raw_ref in raw_refs:
        if isinstance(raw_ref, dict):
            raw_ref = raw_ref.get("evidence_id")
        ref = _bounded_text(raw_ref, limit=160)
        if not ref or not _SAFE_EVIDENCE_REF.fullmatch(ref) or ref in refs:
            continue
        refs.append(ref)
        if len(refs) >= 20:
            break
    return refs


def _read_source_items(source_path: Path) -> list[dict[str, Any]]:
    try:
        return read_jsonl_strict(source_path)
    except JsonlIntegrityError as exc:
        # Preserve the public scaffold contract without exposing source bytes,
        # paths, or parser details.
        raise ExperimentScaffoldError("experiment_scaffold_invalid") from exc


def _validate_context(context: Any) -> ResearchDomainContext:
    if not isinstance(context, ResearchDomainContext):
        raise ExperimentScaffoldError("experiment_context_invalid")
    if not isinstance(context.source, DomainContextSource):
        raise ExperimentScaffoldError("experiment_context_invalid")
    if not _bounded_text(context.display_name or context.topic, limit=240):
        raise ExperimentScaffoldError("experiment_context_invalid")
    return context


def _unresolved_items(context: ResearchDomainContext) -> list[dict[str, str]]:
    items = [{"code": code, "label": label} for code, label in _REQUIRED_INPUTS]
    context_text = " ".join(
        (
            context.display_name,
            context.topic,
            context.description,
            *context.focus_terms,
            *context.research_questions,
        )
    )
    if _ETHICS_TERMS.search(context_text):
        items.append(
            {
                "code": "ethics_review_not_assessed",
                "label": (
                    "Assess ethics and human-subject requirements before any separately "
                    "authorized future execution."
                ),
            }
        )
    return items


def _proposal_statement(
    *,
    context: ResearchDomainContext,
    title: str,
) -> str:
    domain_label = _bounded_text(context.display_name, limit=120)
    if context.research_questions:
        question = _bounded_text(context.research_questions[0], limit=240)
        return (
            f"If a future study operationalizes the documented question “{question}”, "
            f"it could evaluate that question within {domain_label} after all required "
            "design inputs are selected."
        )
    if title:
        return (
            f"A future study could derive a testable question from the selected paper "
            f"“{title}” within {domain_label} after all required design inputs are selected."
        )
    return (
        f"A future study could define a testable question within {domain_label} after "
        "evidence and all required design inputs are selected."
    )


def _build_scaffold(
    *,
    item: dict[str, Any],
    context: ResearchDomainContext,
    generated_at: datetime,
) -> dict[str, Any]:
    title = _bounded_text(item.get("title"), limit=300)
    paper_id = _bounded_text(item.get("paper_id"), limit=160)
    if not title and not paper_id:
        raise ExperimentScaffoldError("experiment_scaffold_invalid")

    refs = _evidence_refs(item)
    unresolved = _unresolved_items(context)
    evidence_status = "explicit_refs_available" if refs else "insufficient_evidence"
    return {
        "schema_version": SCAFFOLD_SCHEMA_VERSION,
        "artifact_id": "primary",
        "artifact_kind": SCAFFOLD_ARTIFACT_KIND,
        "lifecycle_state": SCAFFOLD_LIFECYCLE_STATE,
        "generation_status": SCAFFOLD_GENERATION_STATUS,
        "execution_status": SCAFFOLD_EXECUTION_STATUS,
        "executable": False,
        "runner_available": False,
        "truth_classification": ["proposal", "not_observation", "not_result"],
        "created_at_utc": generated_at.isoformat(),
        "domain_context": {
            "display_name": _bounded_text(context.display_name, limit=120),
            "topic": _bounded_text(context.topic, limit=240),
        },
        "domain_context_source": context.source.value,
        "proposal_basis": {
            "basis_kind": "selected_paper_metadata",
            "paper_id": paper_id or None,
            "paper_title": title or None,
            "evidence_status": evidence_status,
        },
        "hypothesis": {
            "classification": "proposal",
            "statement": _proposal_statement(context=context, title=title),
        },
        "proposed_method": {
            "classification": "planning_text_only",
            "steps": [
                "Review the selected paper and linked evidence before fixing a study design.",
                "Resolve the required inputs and record the rationale for each selection.",
                "Seek separate authorization and an independently reviewed implementation before execution.",
            ],
        },
        "proposed_evaluation": {
            "classification": "proposal",
            "metric_names": [],
            "measured_values": [],
            "success_criteria": None,
        },
        "hypothesized_outcome": {
            "classification": "hypothesized_only",
            "statement": (
                "No directional or quantitative outcome is asserted before the design "
                "inputs and evidence basis are resolved."
            ),
        },
        "required_inputs": [item["code"] for item in unresolved],
        "unresolved_items": unresolved,
        "evidence_refs": refs,
    }


def _render_scaffold_markdown(scaffold: dict[str, Any]) -> str:
    basis = scaffold["proposal_basis"]
    context = scaffold["domain_context"]
    evidence_refs = scaffold["evidence_refs"]
    lines = [
        f"# Proposed Experiment Scaffold: {markdown_inline(basis.get('paper_title') or basis.get('paper_id') or 'Untitled')}",
        "",
        f"- Artifact kind: `{scaffold['artifact_kind']}`",
        f"- Lifecycle: `{scaffold['lifecycle_state']}`",
        f"- Generation status: `{scaffold['generation_status']}`",
        f"- Execution status: `{scaffold['execution_status']}`",
        "- Executable: `false`",
        "- Runner available: `false`",
        f"- Domain: {markdown_inline(context['display_name'])}",
        f"- Domain context source: `{scaffold['domain_context_source']}`",
        f"- Evidence basis: `{basis['evidence_status']}`",
        "",
        "This artifact is a non-executable research design proposal. It is not an observation, "
        "an empirical result, or evidence that an experiment was run.",
        "",
        "## Proposed Hypothesis",
        "",
        markdown_inline(scaffold["hypothesis"]["statement"]),
        "",
        "## Proposed Method (Planning Text Only)",
        "",
    ]
    lines.extend(
        f"{index}. {markdown_inline(step)}"
        for index, step in enumerate(scaffold["proposed_method"]["steps"], start=1)
    )
    lines.extend(
        [
            "",
            "## Proposed Evaluation",
            "",
            "- No evaluation metric, measured value, baseline, or success threshold has been selected.",
            "- Those choices require evidence review and separate authorization before any future execution.",
            "",
            "## Hypothesized Outcome (Not Observed)",
            "",
            markdown_inline(scaffold["hypothesized_outcome"]["statement"]),
            "",
            "## Required Inputs / Unresolved Items",
            "",
        ]
    )
    lines.extend(
        f"- `{item['code']}`: {markdown_inline(item['label'])}"
        for item in scaffold["unresolved_items"]
    )
    lines.extend(["", "## Evidence References", ""])
    if evidence_refs:
        lines.extend(f"- `{markdown_inline(ref)}`" for ref in evidence_refs)
    else:
        lines.append(
            "- No explicit evidence reference is attached; the proposal basis is insufficient for an empirical claim."
        )
    lines.extend(
        [
            "",
            "## Non-Execution Boundary",
            "",
            "This scaffold contains no runner, executable code, shell instruction, notebook, "
            "dataset, credentials, environment definition, or measured result.",
            "",
        ]
    )
    return "\n".join(lines)


def classify_experiment_artifact(payload: Any) -> dict[str, Any]:
    """Return a safe read-only classification without rewriting a stored artifact."""

    if not isinstance(payload, dict) or payload.get("schema_version") != SCAFFOLD_SCHEMA_VERSION:
        return {
            "schema_version": "legacy_unversioned",
            "artifact_kind": "legacy_experiment_artifact",
            "lifecycle_state": "unknown_legacy",
            "generation_status": "unknown_legacy",
            "execution_status": "unknown_legacy",
            "executable": False,
            "runner_available": False,
            "truth_classification": ["not_verified_as_result"],
            "required_inputs": [],
            "unresolved_items": [],
            "evidence_refs": [],
        }

    valid_contract = (
        payload.get("artifact_kind") == SCAFFOLD_ARTIFACT_KIND
        and payload.get("lifecycle_state") == SCAFFOLD_LIFECYCLE_STATE
        and payload.get("execution_status") == SCAFFOLD_EXECUTION_STATUS
        and payload.get("executable") is False
        and payload.get("runner_available") is False
    )
    if not valid_contract:
        return {
            "schema_version": SCAFFOLD_SCHEMA_VERSION,
            "artifact_kind": SCAFFOLD_ARTIFACT_KIND,
            "lifecycle_state": "invalid_scaffold",
            "generation_status": "invalid_scaffold",
            "execution_status": "unknown",
            "executable": False,
            "runner_available": False,
            "truth_classification": ["not_verified_as_result"],
            "required_inputs": [],
            "unresolved_items": [],
            "evidence_refs": [],
        }

    public_fields = (
        "schema_version",
        "artifact_id",
        "artifact_kind",
        "lifecycle_state",
        "generation_status",
        "execution_status",
        "executable",
        "runner_available",
        "truth_classification",
        "proposal_basis",
        "required_inputs",
        "unresolved_items",
        "evidence_refs",
        "domain_context_source",
        "artifact_ref",
    )
    return {key: deepcopy(payload[key]) for key in public_fields if key in payload}


def present_run_experiment_status(run: dict[str, Any]) -> dict[str, Any]:
    """Add a fail-safe experiment status to a copy of a run for read surfaces."""

    presented = deepcopy(run)
    artifacts = presented.get("artifacts")
    if not isinstance(artifacts, dict):
        artifacts = {}
        presented["artifacts"] = artifacts
    payload = artifacts.get("experiment_scaffold")
    if isinstance(payload, dict):
        status = classify_experiment_artifact(payload)
    elif artifacts.get("experiment_dir"):
        status = classify_experiment_artifact(None)
    else:
        status = {
            "schema_version": SCAFFOLD_SCHEMA_VERSION,
            "artifact_kind": SCAFFOLD_ARTIFACT_KIND,
            "lifecycle_state": "not_generated",
            "generation_status": "not_generated",
            "execution_status": SCAFFOLD_EXECUTION_STATUS,
            "executable": False,
            "runner_available": False,
            "truth_classification": ["proposal_not_generated", "not_result"],
            "required_inputs": [],
            "unresolved_items": [],
            "evidence_refs": [],
        }
    experiment_dir = artifacts.get("experiment_dir")
    if isinstance(experiment_dir, str):
        run_id = _bounded_text(presented.get("run_id"), limit=200) or None
        artifacts["experiment_dir"] = (
            normalize_path_to_ref(experiment_dir, run_id=run_id) or "artifact_reference_unavailable"
        )
    presented["experiment_status"] = status
    return presented


def create_experiment_scaffold(
    *,
    source_path: Path,
    rank: int,
    out_root: Path = LEGACY_EXPERIMENTS_ROOT,
    domain_context: ResearchDomainContext | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    items = _read_source_items(source_path)
    if not items:
        return {
            "status": "not_generated",
            "generation_status": "not_generated",
            "execution_status": SCAFFOLD_EXECUTION_STATUS,
            "executable": False,
            "runner_available": False,
            "message": "experiment_source_empty",
        }
    if not isinstance(rank, int) or rank < 1 or rank > len(items):
        raise ExperimentScaffoldError("experiment_scaffold_invalid")

    context = _validate_context(
        domain_context
        or build_research_domain_context(source=DomainContextSource.LEGACY_NEUTRAL)
    )
    now = generated_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ExperimentScaffoldError("experiment_context_invalid")
    now = now.astimezone(timezone.utc)
    scaffold = _build_scaffold(item=items[rank - 1], context=context, generated_at=now)

    out_root.mkdir(parents=True, exist_ok=True)
    experiment_dir = out_root / SCAFFOLD_DIRECTORY_NAME
    try:
        experiment_dir.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise ExperimentArtifactConflict() from exc

    write_text(experiment_dir / "README.md", _render_scaffold_markdown(scaffold))
    # The machine-readable contract is written last and acts as the completeness marker.
    write_json(experiment_dir / "config.json", scaffold)
    return {
        "status": SCAFFOLD_GENERATION_STATUS,
        "generation_status": SCAFFOLD_GENERATION_STATUS,
        "execution_status": SCAFFOLD_EXECUTION_STATUS,
        "executable": False,
        "runner_available": False,
        "experiment_dir": str(experiment_dir),
        "artifact": scaffold,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a non-executable experiment design proposal scaffold."
    )
    parser.add_argument("--from", dest="source", choices=["triage", "inbox"], default="triage")
    parser.add_argument("--rank", type=int, default=1)
    parser.add_argument("--triage", default="data/queues/triage.jsonl")
    parser.add_argument("--inbox", default="data/queues/inbox.jsonl")
    args = parser.parse_args(argv)
    source_path = Path(args.triage if args.source == "triage" else args.inbox)
    try:
        result = create_experiment_scaffold(source_path=source_path, rank=args.rank)
    except ExperimentScaffoldError as exc:
        print(json.dumps({"status": "failed", "error": exc.code}, ensure_ascii=False))
        return 2
    public_result = {
        key: value
        for key, value in result.items()
        if key not in {"experiment_dir", "artifact"}
    }
    if result.get("status") == SCAFFOLD_GENERATION_STATUS:
        public_result["artifact_ref"] = "project:experiments/proposal"
    print(json.dumps(public_result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
