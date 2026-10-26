from __future__ import annotations

import json
from pathlib import Path

from tools import local_research_workflow


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_missing_run_id_never_normalizes_to_success() -> None:
    payload = local_research_workflow.normalize_result_payload(
        {"status": "success", "run_id": None, "failures": []},
        executed_via="runner_cli",
    )

    assert payload["status"] == "not_executed"
    assert payload["run_id"] is None
    assert "Missing run_id for a supposed success result." in payload["failures"]


def test_strict_non_executed_returns_nonzero(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        local_research_workflow.LocalResearchWorkflow,
        "execute",
        lambda self: {
            "status": "not_executed",
            "topic": self.config.topic,
            "profile_name": self.config.profile_name,
            "run_id": None,
            "provider_mode": "fallback_only",
            "artifact_paths": {},
            "local_ui_urls": {},
            "failures": ["Execution capability unavailable."],
        },
    )

    exit_code = local_research_workflow.main(
        [
            "--topic",
            "Truthfulness test",
            "--profile-name",
            "Truthfulness profile",
            "--strict",
        ]
    )
    stdout = capsys.readouterr().out
    payload = json.loads(stdout)

    assert exit_code == 2
    assert payload["status"] == "not_executed"
    assert payload["executed_via"] == "runner_cli"


def test_agent_docs_are_mcp_first_and_manual_fallback_is_documented() -> None:
    gemini_text = (REPO_ROOT / "GEMINI.md").read_text(encoding="utf-8")
    claude_text = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    adapters_doc = (REPO_ROOT / "docs/AGENT_ADAPTERS.md").read_text(encoding="utf-8")
    workflow_doc = (REPO_ROOT / "docs/LOCAL_RESEARCH_WORKFLOW.md").read_text(encoding="utf-8")

    assert "If `run_local_research` MCP tool is available, use it first." in gemini_text
    assert "Do not assume `run_shell_command` exists." in gemini_text
    assert "If `run_local_research` MCP tool is available, use it before any shell path." in claude_text
    assert "Never report success without a real `run_id`." in claude_text
    assert "pwsh scripts/run_local_research.ps1 --topic \"Gaussian avatars in XR\" --profile-name \"Gaussian XR\"" in adapters_doc
    assert "run_id`가 없으면 workflow는 success가 아닙니다." in workflow_doc
