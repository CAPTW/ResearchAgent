from __future__ import annotations

from pathlib import Path

from app.services import exports, reporting, research_plan


def test_markdown_export_uses_current_preview_and_preserves_citation_fields(
    isolated_run_repo,
    monkeypatch,
) -> None:
    monkeypatch.setattr(research_plan, "get_active_provider", lambda: None)

    research_plan.generate_run_research_plan(isolated_run_repo["run_id"])
    outline = reporting.load_outline(isolated_run_repo["run_id"])
    preview = reporting.build_preview(isolated_run_repo["run_id"], outline=outline)
    export_result = exports.export_report_markdown(isolated_run_repo["run_id"])

    exported = Path(export_result["output"]).read_text(encoding="utf-8")

    assert Path(export_result["output"]).name == "exported_preview.md"
    assert exported == preview["content"]
    assert "evidence_id=" in preview["content"]
    assert "source_id=" in preview["content"]
    assert "artifact_path=" in preview["content"]
    assert "citation:" in preview["content"]
    assert "evidence_id=" in exported
    assert "source_id=" in exported
    assert "artifact_path=" in exported
    assert "citation:" in exported
