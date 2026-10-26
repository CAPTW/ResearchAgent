from __future__ import annotations

import json
import shutil

from app.domain import constants
from app.repositories import runs as run_repository
from app.services import exports, research_plan


def test_load_run_record_remaps_legacy_absolute_run_paths_to_current_storage_root(isolated_run_repo) -> None:
    run = run_repository.load_run_record(isolated_run_repo["run_id"])

    assert str(isolated_run_repo["storage_root"]) in run["artifacts"]["report_markdown"]
    assert str(isolated_run_repo["storage_root"]) in run["artifacts"]["outline_json"]
    assert str(isolated_run_repo["storage_root"]) in run["legacy_compatibility"]["authoritative_root"]


def test_new_research_plan_and_export_writes_store_relocatable_references(
    isolated_run_repo,
    monkeypatch,
) -> None:
    monkeypatch.setattr(research_plan, "get_active_provider", lambda: None)

    plan = research_plan.generate_run_research_plan(isolated_run_repo["run_id"])
    export_result = exports.export_report_markdown(isolated_run_repo["run_id"])

    raw_run = json.loads((isolated_run_repo["primary_run_dir"] / "run.json").read_text(encoding="utf-8"))
    raw_plan = json.loads((isolated_run_repo["primary_run_dir"] / "reports" / "research_plan.json").read_text(encoding="utf-8"))
    raw_exports = json.loads((isolated_run_repo["primary_run_dir"] / "reports" / "exports.json").read_text(encoding="utf-8"))

    assert raw_run["artifacts"]["report_markdown"].startswith("run:")
    assert raw_run["artifacts"]["research_plan_json"].startswith("run:")
    assert raw_run["artifacts"]["research_plan_markdown"].startswith("run:")
    assert raw_exports[0]["output"].startswith("run:")
    assert export_result["artifact"]["output"].endswith("exported_preview.md")

    first_citation = next(
        citation
        for section in raw_plan["sections"]
        for claim in section.get("claims", [])
        for citation in claim.get("citations", [])
    )
    assert first_citation["artifact_path"].startswith(("run:", "storage:", "project:", "http://", "https://"))
    assert plan.sections


def test_relocatable_run_artifacts_resolve_after_copy_to_new_storage_root(
    isolated_run_repo,
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(research_plan, "get_active_provider", lambda: None)
    research_plan.generate_run_research_plan(isolated_run_repo["run_id"])
    exports.export_report_markdown(isolated_run_repo["run_id"])

    copied_runs_root = tmp_path / "relocated" / "runs"
    copied_runs_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(isolated_run_repo["primary_run_dir"], copied_runs_root / isolated_run_repo["run_id"])

    monkeypatch.setattr(constants, "STORAGE_ROOT", copied_runs_root)
    monkeypatch.setattr(run_repository, "STORAGE_ROOT", copied_runs_root)

    relocated_run = run_repository.load_run_record(isolated_run_repo["run_id"])
    relocated_exports = exports.list_exports(isolated_run_repo["run_id"])

    assert str(copied_runs_root) in relocated_run["artifacts"]["research_plan_markdown"]
    assert str(copied_runs_root) in relocated_run["artifacts"]["research_plan_json"]
    assert str(copied_runs_root) in relocated_exports["items"][0]["output"]
