from __future__ import annotations

import importlib
import json
import socket
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest


def _paper_review_evidence() -> dict[str, object]:
    return {
        "topic_id": "xr-experiences",
        "topic_name": "XR experiences",
        "paper_id": "2601.07571v1",
        "paper_title": "GPU accelerated surface-based gaze mapping for XR experiences",
        "evidence_source_ref": "data/papers/text/2601.07571v1.txt",
        "evidence_spans": [
            {
                "ref": "2601.07571v1:full_text_excerpt_1",
                "text": (
                    "This paper describes GPU accelerated surface-based gaze mapping for XR "
                    "experiences and evaluates the approach against conventional gaze mapping."
                ),
            }
        ],
    }


def _paper_review_output() -> dict[str, object]:
    return {
        "output_class": "paper_review",
        "internal_output_class": "research_output",
        "provider_family_id": "codex",
        "surface_id": "codex_sdk_python",
        "topic_id": "xr-experiences",
        "topic_name": "XR experiences",
        "paper_id": "2601.07571v1",
        "paper_title": "GPU accelerated surface-based gaze mapping for XR experiences",
        "evidence_span_refs_used": ["2601.07571v1:full_text_excerpt_1"],
        "concise_review_summary": (
            "The paper is relevant to XR experiences because it reviews a GPU accelerated "
            "surface-based gaze mapping method for interactive environments."
        ),
        "methodology_or_core_approach": (
            "It focuses on surface-based gaze mapping and compares the accelerated approach "
            "against conventional gaze mapping behavior."
        ),
        "key_findings": [
            "The supplied excerpt supports that the paper targets gaze mapping in XR experiences.",
            "The method emphasizes GPU acceleration for surface-based mapping.",
        ],
        "limitations": [
            "The single supplied excerpt is not enough to judge dataset coverage or user-study scale."
        ],
        "relevance_to_topic": (
            "The work is directly tied to XR experience quality because gaze mapping is a core "
            "interaction signal."
        ),
        "follow_up_topic_suggestions": [
            "Compare gaze mapping accuracy across XR hardware displays.",
            "Review user comfort tradeoffs in gaze-driven XR interfaces.",
        ],
        "confidence_or_caveats": "Medium confidence because only one local evidence excerpt was supplied.",
        "no_chain_of_thought": True,
    }


def _weekly_report_context() -> dict[str, object]:
    return {
        "topic_id": "xr-experiences",
        "topic_name": "XR experiences",
        "report_scope": "provider_review_context_only",
        "source_provider_review_ids": ["provider-review-ce78f0c10898f231"],
        "source_paper_ids": ["2601.07571v1"],
        "evidence_span_refs_used": ["2601.07571v1:full_text_excerpt_1"],
        "proof_artifact_refs_used": [
            "output/provider-output-class-proof/2026-07-06T07-03-11-903914-00-00/codex_sdk_paper_review_output_class_live_proof.json"
        ],
        "provider_reviews": [
            {
                "provider_review_id": "provider-review-ce78f0c10898f231",
                "paper_id": "2601.07571v1",
                "paper_title": "GPU accelerated surface-based gaze mapping for XR experiences",
                "concise_review_summary": "The accepted provider review links GPU accelerated gaze mapping to XR interaction quality.",
                "key_findings": [
                    "The reviewed paper centers on surface-based gaze mapping for XR.",
                    "The evidence supports GPU acceleration as the implementation focus.",
                ],
                "limitations": ["Only one evidence span is available for this proof context."],
                "relevance_to_topic": "Gaze mapping is directly relevant to XR experiences.",
                "confidence_or_caveats": "Medium confidence because the report uses one DB-applied provider review.",
                "provider_family_id": "codex",
                "surface_id": "codex_sdk_python",
                "output_class": "paper_review",
                "internal_output_class": "research_output",
            }
        ],
    }


def _weekly_report_output() -> dict[str, object]:
    return {
        "output_class": "weekly_report",
        "internal_output_class": "report_draft",
        "provider_family_id": "codex",
        "surface_id": "codex_sdk_python",
        "topic_id": "xr-experiences",
        "topic_name": "XR experiences",
        "report_scope": "provider_review_context_only",
        "source_provider_review_ids": ["provider-review-ce78f0c10898f231"],
        "source_paper_ids": ["2601.07571v1"],
        "evidence_span_refs_used": ["2601.07571v1:full_text_excerpt_1"],
        "proof_artifact_refs_used": [
            "output/provider-output-class-proof/2026-07-06T07-03-11-903914-00-00/codex_sdk_paper_review_output_class_live_proof.json"
        ],
        "weekly_report_title": "XR Gaze Mapping Provider Review Digest",
        "executive_summary": "This draft summarizes one provider-backed review about GPU accelerated gaze mapping for XR.",
        "reviewed_papers_summary": [
            {
                "paper_id": "2601.07571v1",
                "paper_title": "GPU accelerated surface-based gaze mapping for XR experiences",
                "main_takeaway": "The reviewed paper is relevant because gaze mapping affects XR interaction quality.",
                "evidence_refs": ["2601.07571v1:full_text_excerpt_1"],
            }
        ],
        "cross_paper_synthesis": "The current scope contains one reviewed paper, so synthesis is limited to that evidence-backed review.",
        "methodology_or_scope_note": "The draft uses only the supplied DB-applied provider review context and does not store a report.",
        "limitations": ["The draft is based on one provider review and one evidence span."],
        "recommended_next_research_topics": [
            "Compare gaze mapping accuracy across XR headset classes.",
            "Study latency and comfort tradeoffs in gaze-driven XR interfaces.",
        ],
        "confidence_or_caveats": "Medium confidence because the input context is intentionally narrow.",
        "no_chain_of_thought": True,
    }


def test_codex_sdk_provider_import_does_not_import_sdk_modules() -> None:
    sys.modules.pop("app.services.providers.codex_sdk_provider", None)
    before = {name for name in ("codex", "openai_codex") if name in sys.modules}

    module = importlib.import_module("app.services.providers.codex_sdk_provider")

    assert module.CodexSDKProvider().is_configured() is False
    after = {name for name in ("codex", "openai_codex") if name in sys.modules}
    assert after == before


def test_status_paths_do_not_probe_sdk_provider_commands_network_or_files(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.providers import codex_sdk_provider as provider

    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda *_args, **_kwargs: pytest.fail("find_spec must stay out of default readiness"),
    )
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda *_args, **_kwargs: pytest.fail("SDK import must not run"),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("provider command must not run"),
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("provider command must not start"),
    )
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: pytest.fail("network must not be opened"),
    )
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda *_args, **_kwargs: pytest.fail("secret/config files must not be read"),
    )
    monkeypatch.setattr(
        Path,
        "open",
        lambda *_args, **_kwargs: pytest.fail("secret/config files must not be opened"),
    )

    status = provider.describe_codex_sdk_readiness(env={"CODEX_ACCESS_TOKEN": "secret-token"})
    plan = provider.build_codex_sdk_execution_plan("paper_review", "research_output")

    assert status["wouldImportSdk"] is False
    assert status["wouldStartAppServer"] is False
    assert status["wouldExecuteProvider"] is False
    assert plan["wouldImportSdk"] is False
    assert plan["wouldExecuteProvider"] is False


def test_codex_sdk_readiness_exposes_safe_booleans_and_reason_codes_only() -> None:
    from app.services.providers.codex_sdk_provider import describe_codex_sdk_readiness

    env = {
        "CODEX_ACCESS_TOKEN": "codex-token-value",
        "CODEX_API_KEY": "codex-api-key-value",
        "CODEX_HOME": "C:/Users/operator/.codex",
        "GOOGLE_APPLICATION_CREDENTIALS": "C:/Users/operator/provider-auth.json",
    }
    status = describe_codex_sdk_readiness(env=env)
    serialized = json.dumps(status)

    assert status["providerId"] == "codex"
    assert status["profileId"] == "codex_sdk_python"
    assert status["providerFamilyId"] == "codex"
    assert status["executionSurfaceType"] == "sdk"
    assert status["adapterKind"] == "sdk_adapter_scaffold"
    assert status["backendRole"] == "primary_ai_backend_candidate"
    assert status["providerAuthScope"] == "provider_auth_ai_backend"
    assert status["configured"] is False
    assert status["liveReady"] is False
    assert status["mayRunNow"] is False
    assert status["dependencyStatus"]["packageName"] == "openai-codex"
    assert status["dependencyStatus"]["importName"] == "openai_codex"
    assert status["dependencyStatus"]["alternateImportNames"] == ["codex"]
    assert status["dependencyStatus"]["dependencyGroup"] == "codex-sdk"
    assert status["dependencyStatus"]["dependencyDeclared"] is True
    assert status["dependencyStatus"]["dependencyInstalledKnown"] is None
    assert status["dependencyStatus"]["wouldImportSdk"] is False
    assert status["dependencyStatus"]["wouldStartAppServer"] is False
    assert status["dependencyStatus"]["wouldExecuteProvider"] is False
    assert "dependency_missing_observed_from_prior_live_attempt" in status["dependencyStatus"]["readinessStates"]
    assert status["authEnvPresence"]["CODEX_ACCESS_TOKEN"] is True
    assert status["authEnvPresence"]["CODEX_API_KEY"] is True
    assert status["selectedAuthStrategyLabel"] == "codex_sdk_access_token_external"
    assert "codex_sdk_adapter_scaffolded" in status["reasonCodes"]
    assert "codex-token-value" not in serialized
    assert "codex-api-key-value" not in serialized
    assert "C:/Users/operator" not in serialized
    assert "provider-auth.json" not in serialized
    assert ".codex" not in serialized


def test_codex_sdk_dependency_metadata_does_not_import_probe_or_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.providers import codex_sdk_provider as provider

    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda *_args, **_kwargs: pytest.fail("dependency status must not probe specs by default"),
    )
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda *_args, **_kwargs: pytest.fail("dependency status must not import SDK modules"),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("dependency status must not run commands"),
    )
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: pytest.fail("dependency status must not open network"),
    )
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda *_args, **_kwargs: pytest.fail("dependency status must not read config or credential files"),
    )

    metadata = provider.describe_codex_sdk_dependency_status()

    assert metadata["packageName"] == "openai-codex"
    assert metadata["importName"] == "openai_codex"
    assert metadata["alternateImportNames"] == ["codex"]
    assert metadata["importNameCandidates"] == ["openai_codex", "codex"]
    assert metadata["dependencyGroup"] == "codex-sdk"
    assert metadata["dependencyDeclared"] is True
    assert metadata["dependencyDeclaredInProjectOptionalDependencies"] is True
    assert metadata["dependencyInstalledKnown"] is None
    assert metadata["dependencyInstalledStatus"] == "dependency_installed_unknown_not_checked"
    assert metadata["dependencyMissingObservedFromPriorLiveAttempt"] is True
    assert metadata["dependencyInstallRequired"] is True
    assert metadata["dependencyLockUpdateRequired"] is True
    assert metadata["importCheckAllowedNow"] is False
    assert metadata["wouldImportSdk"] is False
    assert metadata["wouldStartAppServer"] is False
    assert metadata["wouldExecuteProvider"] is False
    assert metadata["defaultBlocked"] is True
    assert "codex_sdk_dependency_declared" in metadata["reasonCodes"]
    assert "blocked_pending_dependency_install" in metadata["reasonCodes"]


def test_pyproject_declares_codex_sdk_as_optional_dependency_only() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data["project"]
    core_dependencies = set(project["dependencies"])
    optional_dependencies = project["optional-dependencies"]

    assert "codex-sdk" in optional_dependencies
    assert optional_dependencies["codex-sdk"] == ["openai-codex"]
    assert "openai-codex" not in core_dependencies


def test_codex_sdk_dependency_runbook_is_non_live_redacted_and_does_not_unlock_output() -> None:
    from app.services.providers import codex_sdk_provider as provider

    runbook = provider.describe_codex_sdk_dependency_install_runbook()
    rendered = provider.render_codex_sdk_dependency_install_runbook()
    serialized = json.dumps(runbook, sort_keys=True) + rendered

    assert runbook["ownerRunOnly"] is True
    assert runbook["nonLiveInstallOnly"] is True
    assert runbook["packageName"] == "openai-codex"
    assert runbook["importName"] == "openai_codex"
    assert runbook["dependencyGroup"] == "codex-sdk"
    assert 'python -m pip install -e ".[codex-sdk]"' in runbook["installCommandCandidates"]
    assert 'uv pip install "openai-codex"' in runbook["installCommandCandidates"]
    assert runbook["lockUpdateRequired"] is True
    assert runbook["lockUpdateCommandCandidate"] == "uv lock"
    assert "pytest -q tests/test_codex_sdk_provider_scaffold.py" in runbook["postInstallNonLiveTests"]
    assert runbook["futureLivePrompt"] == "CODEX_SDK_OWNER_APPROVED_ONE_CALL_TINY_PROOF_LIVE"
    assert set(runbook["doesNotProve"]) == {
        "provider_auth",
        "provider_tiny_proof",
        "output_class_proof",
        "app_oauth_oidc",
        "public_mcp_connector",
        "production_readiness",
    }
    assert runbook["wouldImportSdk"] is False
    assert runbook["wouldStartAppServer"] is False
    assert runbook["wouldExecuteProvider"] is False
    assert runbook["defaultBlocked"] is True
    assert "Installing the SDK does not prove provider auth" in rendered
    for forbidden in (
        "sk-",
        "Bearer ",
        "C:\\",
        "C:/Users/",
        ".codex/",
        "auth.json",
        "session.json",
        "browser/cookies",
        "keychain",
        "credential-store",
    ):
        assert forbidden not in serialized


def test_provider_managed_auth_is_represented_but_unverified_and_not_app_oauth_or_public_mcp() -> None:
    from app.services.providers.codex_sdk_provider import describe_codex_sdk_auth_strategies

    status = describe_codex_sdk_auth_strategies(
        env={"XRML_CODEX_SDK_PROVIDER_MANAGED_AUTH_APPROVED": "1"}
    )

    assert status["providerManagedAuthRepresented"] is True
    assert status["providerManagedAuthVerified"] is False
    assert status["selectedAuthStrategyLabel"] == "codex_sdk_provider_managed_auth_external"
    assert status["appOauthOidcSatisfied"] is False
    assert status["publicMcpConnectorProofSatisfied"] is False
    assert status["tinyProofSatisfied"] is False
    assert status["outputClassProofSatisfied"] is False


def test_codex_sdk_execution_plan_is_fail_closed_by_default_and_tiny_cannot_unlock_output() -> None:
    from app.services.providers.codex_sdk_provider import build_codex_sdk_execution_plan

    tiny = build_codex_sdk_execution_plan("tiny_sentinel")
    review = build_codex_sdk_execution_plan("paper_review", "research_output")
    live_future = build_codex_sdk_execution_plan("paper_review", "research_output", live=True)

    for plan in (tiny, review):
        assert plan["wouldImportSdk"] is False
        assert plan["wouldStartAppServer"] is False
        assert plan["wouldExecuteProvider"] is False
        assert plan["mayRunNow"] is False
        assert plan["providerCallCountPlanned"] == 0
        assert plan["outputClassProof"] is False
        assert plan["researchOutputAllowed"] is False
        assert plan["reportDraftAllowed"] is False
        assert plan["cannotUnlockOutputClass"] is True
        assert plan["requiredProofLevel"] == "research_output_verified_tiny"
    assert "tiny_proof_cannot_unlock_output_class" in tiny["reasonCodes"]
    assert live_future["providerCallCountPlanned"] == 1
    assert live_future["wouldExecuteProvider"] is False


def test_codex_sdk_sentinel_validator_rejects_prose_markdown_secret_and_wrong_surface() -> None:
    from app.services.providers import codex_sdk_provider as provider

    exact = provider.validate_codex_sdk_sentinel_output(
        '{"provider_family_id":"codex","surface_id":"codex_sdk_python",'
        '"sentinel":"xrml-provider-tiny-proof","no_chain_of_thought":true}'
    )
    prose = provider.validate_codex_sdk_sentinel_output(
        'Here is JSON: {"provider_family_id":"codex","surface_id":"codex_sdk_python",'
        '"sentinel":"xrml-provider-tiny-proof","no_chain_of_thought":true}'
    )
    fenced = provider.validate_codex_sdk_sentinel_output(
        '```json\n{"provider_family_id":"codex","surface_id":"codex_sdk_python",'
        '"sentinel":"xrml-provider-tiny-proof","no_chain_of_thought":true}\n```'
    )
    secret = provider.validate_codex_sdk_sentinel_output(
        '{"provider_family_id":"codex","surface_id":"codex_sdk_python",'
        '"sentinel":"sk-secret-value","no_chain_of_thought":true}'
    )
    wrong_surface = provider.validate_codex_sdk_sentinel_output(
        '{"provider_family_id":"codex","surface_id":"codex_cli",'
        '"sentinel":"xrml-provider-tiny-proof","no_chain_of_thought":true}'
    )

    assert exact["valid"] is True
    assert prose["valid"] is False
    assert "extra_prose_or_non_json_output" in prose["reasonCodes"]
    assert fenced["valid"] is False
    assert "markdown_fence_not_allowed" in fenced["reasonCodes"]
    assert secret["valid"] is False
    assert "secret_like_content_detected" in secret["reasonCodes"]
    assert wrong_surface["valid"] is False
    assert "field_mismatch:surface_id" in wrong_surface["reasonCodes"]


def test_codex_sdk_live_helper_with_injected_runner_creates_valid_tiny_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.providers import codex_sdk_provider as provider

    monkeypatch.chdir(tmp_path)
    calls: list[str] = []

    def runner(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps(provider.SENTINEL_OBJECT, separators=(",", ":"))

    result = provider.run_codex_sdk_tiny_sentinel_proof(
        env={},
        sdk_runner=runner,
        artifact_path="output/provider-tiny-proof/test/codex_sdk_python_provider_tiny_proof.json",
    )
    artifact_path = tmp_path / "output/provider-tiny-proof/test/codex_sdk_python_provider_tiny_proof.json"
    serialized = json.dumps(result)

    assert len(calls) == 1
    assert calls[0] == provider.SENTINEL_PROMPT
    assert result["status"] == "accepted_tiny_proof"
    assert result["providerSdkRunCount"] == 1
    assert result["providerCommandCount"] == 0
    assert result["providerCallCount"] == 1
    assert result["authStrategyUsed"] == "codex_sdk_provider_managed_auth_external"
    assert artifact_path.is_file()
    assert provider.validate_codex_sdk_tiny_proof_artifact_contract(result["artifact"]) == {
        "valid": True,
        "reasonCodes": [],
    }
    assert result["artifact"]["outputClassProof"] is False
    assert result["artifact"]["researchOutputAllowed"] is False
    assert result["artifact"]["reportDraftAllowed"] is False
    assert result["outputClassGateSummary"]["researchOutputAllowed"] is False
    assert result["outputClassGateSummary"]["reportDraftAllowed"] is False
    assert "C:/Users/operator" not in serialized
    assert "sk-secret-value" not in serialized


def test_codex_sdk_paper_review_output_class_helper_with_injected_runner_creates_valid_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.providers import codex_sdk_provider as provider

    monkeypatch.chdir(tmp_path)
    evidence = _paper_review_evidence()
    calls: list[str] = []

    def runner(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps(_paper_review_output(), separators=(",", ":"))

    result = provider.run_codex_sdk_paper_review_output_class_proof(
        evidence=evidence,
        env={},
        sdk_runner=runner,
        artifact_path="output/provider-output-class-proof/test/codex_sdk_paper_review_output_class_live_proof.json",
    )
    artifact_path = tmp_path / "output/provider-output-class-proof/test/codex_sdk_paper_review_output_class_live_proof.json"
    serialized = json.dumps(result, sort_keys=True)

    assert len(calls) == 1
    assert "2601.07571v1:full_text_excerpt_1" in calls[0]
    assert "GPU accelerated surface-based gaze mapping" in calls[0]
    assert result["status"] == "accepted_output_class_proof"
    assert result["providerSdkRunCount"] == 1
    assert result["providerCommandCount"] == 0
    assert result["providerCallCount"] == 1
    assert result["authStrategyUsed"] == "codex_sdk_provider_managed_auth_external"
    assert artifact_path.is_file()
    assert provider.validate_codex_sdk_paper_review_output_class_proof_artifact_contract(result["artifact"]) == {
        "valid": True,
        "reasonCodes": [],
    }
    assert result["artifact"]["artifactKind"] == "provider_output_class_proof"
    assert result["artifact"]["proofMode"] == "output_class_live"
    assert result["artifact"]["productOutputClass"] == "paper_review"
    assert result["artifact"]["internalOutputClass"] == "research_output"
    assert result["artifact"]["acceptedOutputClassProof"] is True
    assert result["artifact"]["outputClassProof"] is True
    assert result["artifact"]["researchOutputAllowed"] is True
    assert result["artifact"]["reportDraftAllowed"] is False
    assert result["artifact"]["currentProofStatus"] == "research_output_verified_tiny"
    assert result["outputClassGateSummary"]["researchOutputAllowed"] is True
    assert result["outputClassGateSummary"]["reportDraftAllowed"] is False
    assert "weekly_report" not in json.dumps(result["artifact"]["redactedProviderOutput"], sort_keys=True)
    assert "report_draft" not in json.dumps(result["artifact"]["redactedProviderOutput"], sort_keys=True)
    assert "C:/Users/operator" not in serialized
    assert "sk-secret-value" not in serialized
    assert "CODEX_HOME" not in serialized


def test_codex_sdk_weekly_report_output_class_helper_with_injected_runner_creates_valid_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.providers import codex_sdk_provider as provider

    monkeypatch.chdir(tmp_path)
    context = _weekly_report_context()
    calls: list[str] = []

    def runner(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps(_weekly_report_output(), separators=(",", ":"))

    result = provider.run_codex_sdk_weekly_report_output_class_proof(
        report_context=context,
        env={},
        sdk_runner=runner,
        artifact_path="output/provider-output-class-proof/test/codex_sdk_weekly_report_output_class_live_proof.json",
    )
    artifact_path = tmp_path / "output/provider-output-class-proof/test/codex_sdk_weekly_report_output_class_live_proof.json"
    serialized = json.dumps(result, sort_keys=True)

    assert len(calls) == 1
    assert "provider-review-ce78f0c10898f231" in calls[0]
    assert "2601.07571v1:full_text_excerpt_1" in calls[0]
    assert result["status"] == "accepted_output_class_proof"
    assert result["providerSdkRunCount"] == 1
    assert result["providerCommandCount"] == 0
    assert result["providerCallCount"] == 1
    assert result["authStrategyUsed"] == "codex_sdk_provider_managed_auth_external"
    assert artifact_path.is_file()
    assert provider.validate_codex_sdk_weekly_report_output_class_proof_artifact_contract(result["artifact"]) == {
        "valid": True,
        "reasonCodes": [],
    }
    assert result["artifact"]["artifactKind"] == "provider_output_class_proof"
    assert result["artifact"]["proofMode"] == "output_class_live"
    assert result["artifact"]["productOutputClass"] == "weekly_report"
    assert result["artifact"]["internalOutputClass"] == "report_draft"
    assert result["artifact"]["acceptedOutputClassProof"] is True
    assert result["artifact"]["outputClassProof"] is True
    assert result["artifact"]["researchOutputAllowed"] is False
    assert result["artifact"]["reportDraftAllowed"] is True
    assert result["artifact"]["currentProofStatus"] == "report_draft_verified_tiny"
    assert result["artifact"]["reportPersistenceInScope"] is False
    assert result["artifact"]["dbWriteInScope"] is False
    assert result["outputClassGateSummary"]["researchOutputAllowed"] is False
    assert result["outputClassGateSummary"]["reportDraftAllowed"] is True
    assert "C:/Users/operator" not in serialized
    assert "sk-secret-value" not in serialized
    assert "CODEX_HOME" not in serialized


def test_codex_sdk_weekly_report_prompt_contract_names_nested_required_fields() -> None:
    from app.services.providers import codex_sdk_provider as provider

    prompt = provider.build_codex_sdk_weekly_report_prompt(_weekly_report_context())
    payload = json.loads(prompt)
    serialized = json.dumps(payload, sort_keys=True)

    assert payload["task"] == "weekly_report_json_only"
    assert "Return exactly one strict JSON object and nothing else." in payload["instructions"]
    assert "Do not include markdown or prose outside JSON." in payload["instructions"]
    assert "This is a draft proof only, not a persisted report." in payload["instructions"]
    assert "Do not claim DB writes, report persistence, staging readiness, production readiness, external beta readiness, deployment, release, or public access." in payload["instructions"]
    assert payload["reviewed_papers_summary_item_required_fields"] == [
        "paper_id",
        "paper_title",
        "main_takeaway",
        "evidence_refs",
    ]
    assert "provider-review-ce78f0c10898f231" in serialized
    assert "2601.07571v1" in serialized
    assert "2601.07571v1:full_text_excerpt_1" in serialized


def test_codex_sdk_weekly_report_validator_accepts_valid_fixture_and_safe_scope_disclaimers() -> None:
    from app.services.providers import codex_sdk_provider as provider

    output = _weekly_report_output()
    output["methodology_or_scope_note"] = (
        "This is a draft proof only generated for proof validation. It is not a persisted report, "
        "not production-ready, not external beta-ready, not released, not deployed, and not publicly available. "
        "DB persistence is not in scope."
    )

    validation = provider.validate_codex_sdk_weekly_report_output(output, _weekly_report_context())

    assert validation["valid"] is True
    assert validation["reasonCodes"] == []


def test_codex_sdk_weekly_report_validator_rejects_sanitized_failed_shape() -> None:
    from app.services.providers import codex_sdk_provider as provider

    output = _weekly_report_output()
    del output["reviewed_papers_summary"][0]["main_takeaway"]
    del output["reviewed_papers_summary"][0]["evidence_refs"]
    output["methodology_or_scope_note"] = "The persisted report is production-ready and publicly available."

    validation = provider.validate_codex_sdk_weekly_report_output(output, _weekly_report_context())

    assert validation["valid"] is False
    assert "missing_reviewed_paper_takeaway:0" in validation["reasonCodes"]
    assert "missing_reviewed_paper_evidence_refs:0" in validation["reasonCodes"]
    assert "forbidden_report_persistence_or_readiness_claim" in validation["reasonCodes"]


@pytest.mark.parametrize(
    "claim",
    [
        "This is a persisted report.",
        "This is a saved report.",
        "The report has been stored.",
        "The DB has been updated.",
        "This report is ready for production.",
        "This report is production-ready.",
        "This report is external beta-ready.",
        "This report was released.",
        "This report was deployed.",
        "This report is publicly available.",
    ],
)
def test_codex_sdk_weekly_report_validator_rejects_positive_persistence_or_readiness_claims(
    claim: str,
) -> None:
    from app.services.providers import codex_sdk_provider as provider

    output = _weekly_report_output()
    output["confidence_or_caveats"] = claim

    validation = provider.validate_codex_sdk_weekly_report_output(output, _weekly_report_context())

    assert validation["valid"] is False
    assert "forbidden_report_persistence_or_readiness_claim" in validation["reasonCodes"]


def test_codex_sdk_weekly_report_validator_rejects_markdown_prose_unknown_refs_and_unsafe_content() -> None:
    from app.services.providers import codex_sdk_provider as provider

    context = _weekly_report_context()
    fenced = "```json\n" + json.dumps(_weekly_report_output()) + "\n```"
    prose = "Here is the report: " + json.dumps(_weekly_report_output())
    unknown = _weekly_report_output()
    unknown["source_paper_ids"] = ["unknown-paper"]
    unknown["evidence_span_refs_used"] = ["unknown-ref"]
    unknown["reviewed_papers_summary"][0]["evidence_refs"] = ["unknown-ref"]
    chain = _weekly_report_output()
    chain["confidence_or_caveats"] = "Hidden reasoning: first I thought through the answer."
    secret = _weekly_report_output()
    secret["executive_summary"] = "sk-secret-value"

    fenced_validation = provider.validate_codex_sdk_weekly_report_output(fenced, context)
    prose_validation = provider.validate_codex_sdk_weekly_report_output(prose, context)
    unknown_validation = provider.validate_codex_sdk_weekly_report_output(unknown, context)
    chain_validation = provider.validate_codex_sdk_weekly_report_output(chain, context)
    secret_validation = provider.validate_codex_sdk_weekly_report_output(secret, context)

    assert "markdown_fence_not_allowed" in fenced_validation["reasonCodes"]
    assert "extra_prose_or_non_json_output" in fenced_validation["reasonCodes"]
    assert "extra_prose_or_non_json_output" in prose_validation["reasonCodes"]
    assert "field_mismatch:source_paper_ids" in unknown_validation["reasonCodes"]
    assert "unknown_evidence_span_ref_used" in unknown_validation["reasonCodes"]
    assert "unknown_reviewed_paper_evidence_ref:0" in unknown_validation["reasonCodes"]
    assert "chain_of_thought_content_not_allowed" in chain_validation["reasonCodes"]
    assert "secret_like_content_detected" in secret_validation["reasonCodes"]


def test_codex_sdk_weekly_report_artifact_builder_refuses_invalid_weekly_output(tmp_path: Path) -> None:
    from app.services.providers import codex_sdk_provider as provider

    output = _weekly_report_output()
    output["reviewed_papers_summary"][0]["evidence_refs"] = []

    with pytest.raises(ValueError) as exc:
        provider.build_codex_sdk_weekly_report_output_class_proof_artifact(
            weekly_report_output=output,
            report_context=_weekly_report_context(),
            auth_strategy_used="codex_sdk_provider_managed_auth_external",
            provider_auth_observed={
                "CODEX_ACCESS_TOKEN": False,
                "CODEX_API_KEY": False,
                "XRML_CODEX_SDK_PROVIDER_MANAGED_AUTH_APPROVED": False,
            },
            artifact_ref="output/provider-output-class-proof/test/codex_sdk_weekly_report_output_class_live_proof.json",
            timestamp="2026-07-06T09-00-00-000000-00-00",
        )

    assert "missing_reviewed_paper_evidence_refs:0" in str(exc.value)
    assert not (tmp_path / "output/provider-output-class-proof/test/codex_sdk_weekly_report_output_class_live_proof.json").exists()


def test_codex_sdk_weekly_report_validator_rejects_unknown_refs_and_paper_review_output_class() -> None:
    from app.services.providers import codex_sdk_provider as provider

    output = _weekly_report_output()
    output["source_provider_review_ids"] = ["unknown-review"]
    output["output_class"] = "paper_review"

    validation = provider.validate_codex_sdk_weekly_report_output(output, _weekly_report_context())

    assert validation["valid"] is False
    assert "field_mismatch:source_provider_review_ids" in validation["reasonCodes"]
    assert "field_mismatch:output_class" in validation["reasonCodes"]


def test_codex_sdk_paper_review_validator_rejects_weekly_report_and_unknown_refs() -> None:
    from app.services.providers import codex_sdk_provider as provider

    output = _paper_review_output()
    output["evidence_span_refs_used"] = ["unknown-ref"]
    output["confidence_or_caveats"] = "This is not a weekly_report or report_draft proof."

    validation = provider.validate_codex_sdk_paper_review_output(output, _paper_review_evidence())

    assert validation["valid"] is False
    assert "unknown_evidence_span_ref_used" in validation["reasonCodes"]
    assert "weekly_report_or_report_draft_output_not_allowed" in validation["reasonCodes"]


def test_codex_sdk_paper_review_helper_stops_cleanly_on_invalid_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.providers import codex_sdk_provider as provider

    monkeypatch.chdir(tmp_path)

    result = provider.run_codex_sdk_paper_review_output_class_proof(
        evidence=_paper_review_evidence(),
        env={},
        sdk_runner=lambda _prompt: "not-json",
        artifact_path="output/provider-output-class-proof/test/codex_sdk_paper_review_output_class_live_proof.json",
    )

    assert result["status"] == "stopped"
    assert result["stopReason"] == "CODEX_SDK_OUTPUT_NOT_VALID_PAPER_REVIEW_JSON"
    assert result["providerSdkRunCount"] == 1
    assert result["providerCommandCount"] == 0
    assert result["providerCallCount"] == 1
    assert result["artifactPath"] is None
    assert not (tmp_path / "output/provider-output-class-proof/test/codex_sdk_paper_review_output_class_live_proof.json").exists()


def test_codex_sdk_live_helper_stops_cleanly_when_sdk_import_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.providers import codex_sdk_provider as provider

    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name: (_ for _ in ()).throw(ImportError(name)),
    )

    result = provider.run_codex_sdk_tiny_sentinel_proof(write_artifact=False)

    assert result["status"] == "stopped"
    assert result["stopReason"] == "CODEX_SDK_IMPORT_NOT_AVAILABLE"
    assert result["providerSdkRunCount"] == 0
    assert result["providerCommandCount"] == 0
    assert result["artifactPath"] is None
    assert result["artifact"] is None


def test_backend_scaffold_lists_codex_sdk_as_primary_blocked_adapter_candidate() -> None:
    from app.services import provider_backend_scaffold as scaffold

    surfaces = {
        item["surfaceId"]: item
        for item in scaffold.describe_provider_sdk_oauth_surfaces()["surfaces"]
    }
    families = {
        item["providerFamilyId"]: item
        for item in scaffold.describe_provider_backend_families()["families"]
    }
    strategies = {
        item["authStrategyId"]: item
        for item in scaffold.describe_provider_auth_strategies()["strategies"]
    }

    codex_sdk = surfaces["codex_sdk_python"]
    assert codex_sdk["adapterKind"] == "sdk_adapter_scaffold"
    assert codex_sdk["backendRole"] == "primary_ai_backend"
    assert codex_sdk["defaultBlocked"] is True
    assert codex_sdk["wouldExecute"] is False
    assert codex_sdk["mayImportSdk"] is False
    assert "codex_sdk_adapter_scaffolded" in codex_sdk["reasonCodes"]
    assert "blocked_pending_dependency_install" in codex_sdk["reasonCodes"]
    assert "blocked_pending_owner_live_approval" in codex_sdk["reasonCodes"]
    assert "blocked_pending_provider_auth_surface_proof" in codex_sdk["reasonCodes"]
    assert "output_class_proof_required" in codex_sdk["reasonCodes"]
    assert families["codex"]["supportsMainAiSelectionCandidate"] is True
    assert families["codex"]["supportsWeightedRoutingCandidate"] is True
    assert strategies["codex_sdk_provider_managed_auth_external"]["surfaceId"] == "codex_sdk_python"
    assert strategies["codex_sdk_api_key_external"]["requiredEnvNames"] == ["CODEX_API_KEY"]


def test_main_ai_and_weighted_routing_can_include_codex_but_cannot_execute_or_unlock() -> None:
    from app.services import provider_backend_scaffold as scaffold

    main = scaffold.select_main_ai_backend_candidate("codex", output_class="paper_review")
    route = scaffold.plan_weighted_backend_routing({"codex": 1}, output_class="paper_review")
    tiny = scaffold.plan_provider_tiny_proof("codex", "codex_sdk_python")

    assert main["selected"] is True
    assert main["selectionAllowed"] is False
    assert main["outputAllowed"] is False
    assert main["wouldExecute"] is False
    assert main["gateStatus"]["requiredProofLevel"] == "research_output_verified_tiny"
    assert route["selectedProviderFamilyId"] == "codex"
    assert route["allowed"] is False
    assert route["wouldExecute"] is False
    assert tiny["mayRunNow"] is False
    assert tiny["wouldExecuteProvider"] is False
    assert tiny["postTinyProofOutputGates"]["tinyProofCanUnlockResearchOutput"] is False
    assert tiny["postTinyProofOutputGates"]["tinyProofCanUnlockReportDraft"] is False


def test_execution_surface_bridge_exposes_codex_sdk_but_blocks_execution() -> None:
    from app.services import provider_execution_surface_bridge as bridge

    surfaces = {
        item["profileId"]: item
        for item in bridge.describe_provider_execution_surfaces(
            env={"CODEX_API_KEY": "codex-api-key-value"}
        )["surfaces"]
    }
    codex_sdk = surfaces["codex_sdk_python"]
    plan = bridge.plan_provider_output_class_execution(
        "codex_sdk_python",
        "paper_review",
        env={"CODEX_API_KEY": "codex-api-key-value"},
    ).to_dict()
    serialized = json.dumps(codex_sdk)

    assert codex_sdk["providerId"] == "codex"
    assert codex_sdk["adapterKind"] == "sdk_adapter_scaffold"
    assert codex_sdk["executionSurfaceType"] == "sdk"
    assert codex_sdk["configured"] is False
    assert codex_sdk["blocked"] is True
    assert codex_sdk["envPresence"]["CODEX_API_KEY"] is True
    assert "blocked_pending_dependency_install" in codex_sdk["blockedReasonCodes"]
    assert "codex-api-key-value" not in serialized
    assert plan["liveExecutionAllowed"] is False
    assert plan["wouldExecuteProvider"] is False
    assert "sdk_adapter_scaffold_cannot_execute" in plan["reasonCodes"]


def test_bare_live_verified_tiny_remains_insufficient_for_codex_sdk_output_class() -> None:
    from app.services import provider_execution_surface_bridge as bridge

    plan = bridge.plan_provider_output_class_execution(
        "codex_sdk_python",
        "paper_review",
        accepted_output_proofs={"codex": {"research_output": "live_verified_tiny"}},
    ).to_dict()

    assert plan["outputAllowed"] is False
    assert plan["blockedReason"] == "insufficient_proof_level_for_output_class"
    assert plan["requiredProofLevel"] == "research_output_verified_tiny"
    assert plan["wouldExecuteProvider"] is False


def test_runtime_provider_registry_does_not_select_codex_sdk_without_exact_output_class_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import providers

    monkeypatch.setenv("XRML_PROVIDER", "codex_sdk_python")
    monkeypatch.setenv("CODEX_API_KEY", "codex-api-key-value")

    status = providers.describe_provider_status()
    serialized = json.dumps(status)

    assert providers.get_active_provider() is None
    assert status["provider"] == "codex"
    assert status["provider_mode"] == "codex_sdk_scaffold_blocked"
    assert status["live_call_enabled"] is False
    assert status["researchOutputAllowed"] is False
    assert status["reportGenerationAllowed"] is False
    assert status["orchestrationAllowed"] is False
    assert "codex-api-key-value" not in serialized


def test_openai_remains_pipeline_skeleton_and_paper_review_weekly_report_stay_separate() -> None:
    from app.services import provider_backend_scaffold as scaffold

    surfaces = {
        item["surfaceId"]: item
        for item in scaffold.describe_provider_sdk_oauth_surfaces()["surfaces"]
    }
    paper = scaffold.build_codex_sdk_provider_execution_plan("paper_review", "research_output")
    report = scaffold.build_codex_sdk_provider_execution_plan("weekly_report", "report_draft")

    assert surfaces["openai_api_pipeline"]["adapterKind"] == "api_pipeline_skeleton"
    assert surfaces["openai_api_pipeline"]["backendRole"] == "api_pipeline_skeleton"
    assert paper["outputClass"] == "research_output"
    assert paper["requiredProofLevel"] == "research_output_verified_tiny"
    assert report["outputClass"] == "report_draft"
    assert report["requiredProofLevel"] == "report_draft_verified_tiny"
    assert paper["researchOutputAllowed"] is False
    assert report["reportDraftAllowed"] is False
