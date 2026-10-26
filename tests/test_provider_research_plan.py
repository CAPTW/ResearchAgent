from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from app.services import evidence, reporting, research_plan
from app.services.providers import describe_provider_status, get_active_provider
from app.services.providers.base import ProviderRequest
from app.services.providers.openai_provider import OpenAIProvider
from tools.provider_live_smoke import main as provider_live_smoke_main


def test_openai_provider_smoke_parses_structured_output() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/responses"
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["text"]["format"]["type"] == "json_schema"
        return httpx.Response(
            200,
            json={
                "id": "resp_test",
                "model": "gpt-4o-mini",
                "status": "completed",
                "usage": {"input_tokens": 10, "output_tokens": 20},
                "output": [
                    {
                        "type": "message",
                        "role": "assistant", "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps({"sections": [{"section_id": "executive-summary", "summary": "ok", "paper_ids": [], "claims": []}]}),
                            }
                        ],
                    }
                ],
            },
        )

    provider = OpenAIProvider(api_key="test-key", transport=httpx.MockTransport(handler))
    result = provider.generate_json(
        ProviderRequest(
            schema_name="test_schema",
            schema={"type": "object", "properties": {"sections": {"type": "array"}}, "required": ["sections"]},
            instructions="Return structured JSON.",
            prompt="{}",
        )
    )

    assert result.provider_name == "openai"
    assert result.response_id == "resp_test"
    assert result.payload["sections"][0]["section_id"] == "executive-summary"


def test_collect_run_evidence_produces_citation_fields(isolated_run_repo: dict[str, Any]) -> None:
    rows = evidence.collect_run_evidence(isolated_run_repo["run_id"], max_papers=2, max_evidence_per_paper=2)

    assert rows
    assert rows[0].evidence_id
    assert rows[0].source_id == rows[0].paper_id
    assert rows[0].citation_text


def test_research_plan_fallback_keeps_evidence_links_without_provider(
    isolated_run_repo: dict[str, Any],
    monkeypatch,
) -> None:
    monkeypatch.setattr(research_plan, "get_active_provider", lambda: None)

    plan = research_plan.generate_run_research_plan(isolated_run_repo["run_id"])
    outline = reporting.load_outline(isolated_run_repo["run_id"])
    preview = reporting.build_preview(isolated_run_repo["run_id"], outline=outline)

    assert plan.generator.mode == "heuristic"
    assert any(section.claims for section in plan.sections)
    assert "evidence_id=" in preview["content"]
    assert "source_id=" in preview["content"]
    assert "artifact_path=" in preview["content"]
    assert "citation:" in preview["content"]


def test_research_plan_generation_and_preview_include_evidence_links(
    isolated_run_repo: dict[str, Any],
    monkeypatch,
) -> None:
    class FakeProvider:
        provider_name = "fake"

        def is_configured(self) -> bool:
            return True

        def describe_status(self) -> dict[str, object]:
            return {"provider": "fake", "configured": True}

        def generate_json(self, request: ProviderRequest):
            evidence_context = json.loads(request.prompt)
            evidence_id = evidence_context["evidence"][0]["evidence_id"]
            source_id = evidence_context["evidence"][0]["source_id"]
            return type(
                "FakeResult",
                (),
                {
                    "provider_name": "fake",
                    "model": "fake-model",
                    "response_id": "resp_fake",
                    "payload": {
                        "sections": [
                            {
                                "section_id": "executive-summary",
                                "summary": "Provider-backed summary",
                                "paper_ids": [source_id],
                                "claims": [
                                    {
                                        "claim_id": "claim-1",
                                        "claim_text": "Evidence-backed claim",
                                        "importance": "high",
                                        "citations": [{"evidence_id": evidence_id}],
                                    }
                                ],
                            }
                        ]
                    },
                },
            )()

    monkeypatch.setattr(research_plan, "get_active_provider", lambda: FakeProvider())

    plan = research_plan.generate_run_research_plan(isolated_run_repo["run_id"])
    outline = reporting.load_outline(isolated_run_repo["run_id"])
    preview = reporting.build_preview(isolated_run_repo["run_id"], outline=outline)

    assert plan.generator.mode == "provider"
    assert any(section.claims for section in plan.sections)
    assert any(section.get("claims") for section in outline["sections"])
    assert "evidence_id=" in preview["content"]
    assert "citation:" in preview["content"]
    assert Path(preview["path"]).exists()


def test_provider_status_does_not_report_live_ready_when_key_exists_without_project_gate(monkeypatch) -> None:
    monkeypatch.setenv("XRML_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-live-key")
    monkeypatch.delenv("XRML_ALLOW_PROVIDER_RESEARCH_OUTPUT", raising=False)
    monkeypatch.delenv("XRML_PROVIDER_LIVE_PROOF_STATUS", raising=False)
    monkeypatch.delenv("XRML_PROVIDER_LIVE_PROOF_ARTIFACT", raising=False)

    status = describe_provider_status()

    assert status["provider"] == "openai"
    assert status["provider_mode"] == "configured_no_project_live_proof"
    assert status["configured"] is True
    assert status["live_call_enabled"] is False
    assert status["canonicalStatus"] == "configured_not_verified"
    assert status["routingLabel"] == "live_proof_required"
    assert status["researchOutputAllowed"] is False
    assert get_active_provider() is None


def test_provider_status_does_not_report_live_ready_when_gate_exists_without_proof(monkeypatch) -> None:
    monkeypatch.setenv("XRML_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-live-key")
    monkeypatch.setenv("XRML_ALLOW_PROVIDER_RESEARCH_OUTPUT", "1")
    monkeypatch.setenv("XRML_PROVIDER_LIVE_PROOF_STATUS", "live_verified_tiny")
    monkeypatch.setenv("XRML_PROVIDER_LIVE_PROOF_ARTIFACT", "missing-provider-proof.json")

    status = describe_provider_status()

    assert status["provider"] == "openai"
    assert status["provider_mode"] == "configured_no_project_live_proof"
    assert status["configured"] is True
    assert status["live_call_enabled"] is False
    assert status["canonicalStatus"] == "configured_not_verified"
    assert status["routingLabel"] == "live_proof_required"
    assert status["researchOutputAllowed"] is False
    assert status["reportGenerationAllowed"] is False
    assert get_active_provider() is None


def test_provider_live_smoke_skips_cleanly_without_api_key(monkeypatch, capsys) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("XRML_PROVIDER", "openai")

    exit_code = provider_live_smoke_main([])
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert exit_code == 0
    assert payload["status"] == "skipped"
    assert payload["reason"] == "OPENAI_API_KEY is not configured."
