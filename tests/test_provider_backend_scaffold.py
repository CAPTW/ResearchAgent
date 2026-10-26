from __future__ import annotations

import importlib
import json
import socket
import subprocess
from pathlib import Path

from app.services import provider_backend_scaffold as scaffold


def _families_by_id() -> dict[str, dict]:
    summary = scaffold.describe_provider_backend_families()
    return {family["providerFamilyId"]: family for family in summary["families"]}


def _surfaces_by_id() -> dict[str, dict]:
    summary = scaffold.describe_provider_sdk_oauth_surfaces()
    return {surface["surfaceId"]: surface for surface in summary["surfaces"]}


def _strategies_by_id() -> dict[str, dict]:
    summary = scaffold.describe_provider_auth_strategies()
    return {strategy["authStrategyId"]: strategy for strategy in summary["strategies"]}


def test_backend_families_make_codex_claude_antigravity_primary_candidates() -> None:
    summary = scaffold.describe_provider_backend_families()
    families = _families_by_id()

    assert summary["preferredProviderFamilyOrder"] == ["antigravity", "claude", "codex"]
    for family_id in ("antigravity", "claude", "codex"):
        family = families[family_id]
        assert family["backendRole"] == "primary_ai_backend"
        assert family["preferredByDefault"] is True
        assert family["supportsMainAiSelectionCandidate"] is True
        assert family["supportsWeightedRoutingCandidate"] is True
        assert family["defaultBlocked"] is True
        assert family["provenOutputClasses"] == []
    openai = families["openai_api_pipeline"]
    assert openai["backendRole"] == "api_pipeline_skeleton"
    assert openai["preferredByDefault"] is False
    assert openai["supportsMainAiSelectionCandidate"] is False
    assert openai["supportsWeightedRoutingCandidate"] is False


def test_sdk_cli_surfaces_are_represented_as_non_executable_contracts() -> None:
    surfaces = _surfaces_by_id()
    expected = {
        "codex_sdk_python": "sdk_adapter_scaffold",
        "codex_sdk_typescript": "sdk_contract",
        "codex_cli": "cli_contract",
        "claude_agent_sdk_python": "sdk_contract",
        "claude_agent_sdk_typescript": "sdk_contract",
        "claude_code_cli": "cli_contract",
        "antigravity_sdk_python": "sdk_contract",
        "antigravity_cli": "cli_contract",
        "openai_api_pipeline": "api_pipeline_skeleton",
    }

    for surface_id, adapter_kind in expected.items():
        surface = surfaces[surface_id]
        assert surface["adapterKind"] == adapter_kind
        assert surface["defaultBlocked"] is True
        assert surface["wouldExecute"] is False
        assert surface["mayImportSdk"] is False
        assert surface["mayRunProviderCommand"] is False
        assert surface["mayCallNetwork"] is False
        assert surface["mayStartBrowserLogin"] is False
        assert surface["mayStartDeviceFlow"] is False
        assert surface["mayInspectCredentialCache"] is False
        assert surface["mayInspectSessionFile"] is False


def test_auth_strategies_expose_env_names_only_without_env_values(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret-value")
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "codex-token-value")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "C:/Users/operator/provider-auth.json")

    summary = scaffold.describe_provider_auth_strategies()
    serialized = json.dumps(summary)

    assert "OPENAI_API_KEY" in serialized
    assert "ANTHROPIC_API_KEY" in serialized
    assert "CODEX_ACCESS_TOKEN" in serialized
    assert "GOOGLE_APPLICATION_CREDENTIALS" in serialized
    for forbidden_value in (
        "sk-test-secret-value",
        "anthropic-secret-value",
        "codex-token-value",
        "provider-auth.json",
        "C:/Users/operator",
        ".mcp.json",
        "auth.json",
        "browser/cookies",
        "keychain",
    ):
        assert forbidden_value not in serialized


def test_provider_auth_oauth_is_not_app_oauth_oidc_or_public_mcp() -> None:
    families = scaffold.describe_provider_backend_families()
    surfaces = scaffold.describe_provider_sdk_oauth_surfaces()
    strategies = scaffold.describe_provider_auth_strategies()

    assert families["appOauthOidcInScope"] is False
    assert families["publicMcpConnectorInScope"] is False
    assert "provider_auth_is_not_app_oauth_oidc" in families["statusNotes"]
    assert "local_mcp_is_not_public_mcp_connector_proof" in families["statusNotes"]
    for family in families["families"]:
        assert family["providerAuthScope"] == "provider_auth_ai_backend"
        assert family["appOauthOidcInScope"] is False
        assert family["publicMcpConnectorInScope"] is False
    for surface in surfaces["surfaces"]:
        assert surface["providerAuthScope"] == "provider_auth_ai_backend"
        assert surface["appOauthOidcInScope"] is False
        assert surface["publicMcpConnectorInScope"] is False
    for strategy in strategies["strategies"]:
        assert strategy["providerAuthScope"] == "provider_auth_ai_backend"
        assert strategy["appOauthOidcEquivalent"] is False
        assert strategy["publicMcpEquivalent"] is False


def test_main_ai_selection_selects_candidate_but_refuses_output_without_proof() -> None:
    selected = scaffold.select_main_ai_backend_candidate()
    codex = scaffold.select_main_ai_backend_candidate("codex", output_class="paper_review")

    assert selected["providerFamilyId"] == "antigravity"
    assert selected["selected"] is True
    assert selected["selectionAllowed"] is False
    assert selected["outputAllowed"] is False
    assert selected["wouldExecute"] is False
    assert codex["providerFamilyId"] == "codex"
    assert codex["internalOutputClass"] == "research_output"
    assert codex["gateStatus"]["requiredProofLevel"] == "research_output_verified_tiny"
    assert codex["blockedReason"] == "output_class_proof_missing"


def test_weighted_routing_plan_is_deterministic_and_non_executable() -> None:
    weights = {"codex": 3, "claude": 1, "antigravity": 2}
    first = scaffold.plan_weighted_backend_routing(weights, request_fingerprint="req-1")
    second = scaffold.plan_weighted_backend_routing(weights, request_fingerprint="req-1")

    assert first == second
    assert first["selectedProviderFamilyId"] in {"codex", "claude", "antigravity"}
    assert first["wouldExecute"] is False
    assert first["allowed"] is False
    assert first["allOutputBlocked"] is True
    for row in first["providerDecisionMatrix"]:
        assert row["wouldExecute"] is False
        assert row["outputAllowed"] is False


def test_all_output_classes_remain_blocked_without_exact_proof() -> None:
    for product_output_class, internal_output_class in (
        ("paper_review", "research_output"),
        ("weekly_report", "report_draft"),
    ):
        candidate = scaffold.select_main_ai_backend_candidate(
            "codex",
            output_class=product_output_class,
        )
        assert candidate["internalOutputClass"] == internal_output_class
        assert candidate["outputAllowed"] is False
        assert candidate["gateStatus"]["blockedReason"] == "output_class_proof_missing"


def test_paper_review_and_weekly_report_do_not_cross_unlock() -> None:
    accepted_research = {"codex": {"research_output": "research_output_verified_tiny"}}
    accepted_wrong_report = {"codex": {"report_draft": "research_output_verified_tiny"}}
    paper = scaffold.select_main_ai_backend_candidate(
        "codex",
        output_class="paper_review",
        accepted_output_proofs=accepted_research,
    )
    weekly_missing = scaffold.select_main_ai_backend_candidate(
        "codex",
        output_class="weekly_report",
        accepted_output_proofs=accepted_research,
    )
    weekly_wrong = scaffold.select_main_ai_backend_candidate(
        "codex",
        output_class="weekly_report",
        accepted_output_proofs=accepted_wrong_report,
    )

    assert paper["outputAllowed"] is True
    assert paper["gateStatus"]["requiredProofLevel"] == "research_output_verified_tiny"
    assert weekly_missing["outputAllowed"] is False
    assert weekly_missing["gateStatus"]["requiredProofLevel"] == "report_draft_verified_tiny"
    assert weekly_missing["gateStatus"]["blockedReason"] == "output_class_proof_missing"
    assert weekly_wrong["outputAllowed"] is False
    assert weekly_wrong["gateStatus"]["blockedReason"] == "insufficient_proof_level_for_output_class"


def test_bare_live_verified_tiny_remains_insufficient() -> None:
    plan = scaffold.plan_weighted_backend_routing(
        {"codex": 1},
        output_class="paper_review",
        accepted_output_proofs={"codex": {"research_output": "live_verified_tiny"}},
    )

    assert plan["selectedProviderFamilyId"] == "codex"
    assert plan["allowed"] is False
    assert plan["gateStatus"]["blockedReason"] == "insufficient_proof_level_for_output_class"
    assert plan["gateStatus"]["requiredProofLevel"] == "research_output_verified_tiny"
    assert plan["wouldExecute"] is False


def test_openai_api_pipeline_is_skeleton_only_and_cannot_bypass_proof() -> None:
    families = _families_by_id()
    surfaces = _surfaces_by_id()
    candidate = scaffold.select_main_ai_backend_candidate(
        "openai_api_pipeline",
        output_class="paper_review",
    )
    pipeline = scaffold.plan_provider_backend_pipeline(
        main_ai_provider_family="codex",
        output_class="paper_review",
    )

    assert families["openai_api_pipeline"]["backendRole"] == "api_pipeline_skeleton"
    assert surfaces["openai_api_pipeline"]["adapterKind"] == "api_pipeline_skeleton"
    assert candidate["selectionAllowed"] is False
    assert candidate["blockedReason"] == "provider_family_not_main_ai_candidate"
    assert candidate["wouldExecute"] is False
    assert pipeline["fallbackProviderFamily"] == "openai_api_pipeline"
    assert pipeline["apiPipelineSkeletonOnly"] is True
    assert pipeline["apiSkeletonCanBypassProof"] is False
    assert pipeline["wouldExecute"] is False
    assert all(assignment["wouldExecute"] is False for assignment in pipeline["assignments"])


def test_pipeline_plan_lists_stages_and_keeps_assignments_fail_closed() -> None:
    pipeline = scaffold.plan_provider_backend_pipeline(
        main_ai_provider_family="antigravity",
        weights={"antigravity": 2, "claude": 1, "codex": 1},
        output_class="paper_review",
    )
    stage_ids = [stage["stageId"] for stage in pipeline["stages"]]

    assert stage_ids == [
        "paper_collection",
        "full_text_or_evidence_reading",
        "paper_review",
        "weekly_report",
        "topic_recommendation",
    ]
    assert pipeline["mainAiProviderFamily"] == "antigravity"
    assert pipeline["internalOutputClass"] == "research_output"
    assert pipeline["dryRunOnly"] is True
    assert pipeline["wouldExecute"] is False
    for assignment in pipeline["assignments"]:
        assert assignment["fallbackProviderFamily"] == "openai_api_pipeline"
        assert assignment["wouldExecute"] is False
        assert assignment["proofRequired"] is True
        assert assignment["gateStatus"]["allowed"] is False


def test_no_subprocess_sdk_import_or_network_call_occurs(monkeypatch) -> None:
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("non-live scaffold must not run provider commands, SDK imports, or network calls")

    monkeypatch.setattr(subprocess, "run", fail_if_called)
    monkeypatch.setattr(subprocess, "Popen", fail_if_called)
    monkeypatch.setattr(importlib, "import_module", fail_if_called)
    monkeypatch.setattr(socket, "create_connection", fail_if_called)

    scaffold.describe_provider_backend_families()
    scaffold.describe_provider_sdk_oauth_surfaces()
    scaffold.describe_provider_auth_strategies()
    scaffold.select_main_ai_backend_candidate("claude")
    scaffold.plan_weighted_backend_routing({"antigravity": 1, "claude": 1, "codex": 1})
    scaffold.plan_provider_backend_pipeline(main_ai_provider_family="antigravity")


def test_status_payloads_are_stable_and_do_not_include_auth_paths_or_secret_values() -> None:
    first = {
        "families": scaffold.describe_provider_backend_families(),
        "surfaces": scaffold.describe_provider_sdk_oauth_surfaces(),
        "strategies": scaffold.describe_provider_auth_strategies(),
        "pipeline": scaffold.plan_provider_backend_pipeline(main_ai_provider_family="codex"),
    }
    second = {
        "families": scaffold.describe_provider_backend_families(),
        "surfaces": scaffold.describe_provider_sdk_oauth_surfaces(),
        "strategies": scaffold.describe_provider_auth_strategies(),
        "pipeline": scaffold.plan_provider_backend_pipeline(main_ai_provider_family="codex"),
    }
    serialized = json.dumps(first)

    assert first == second
    for forbidden in (
        "sk-",
        "Bearer ",
        "C:\\",
        "C:/Users/",
        ".codex/",
        ".claude/",
        ".config/",
        ".mcp.json",
        "auth.json",
        "browser/cookies",
        "keychain",
        "secret-value",
    ):
        assert forbidden not in serialized


def _tiny_candidates_by_surface() -> dict[str, dict]:
    summary = scaffold.describe_provider_tiny_proof_preconditions()
    return {candidate["surfaceId"]: candidate for candidate in summary["candidates"]}


def test_tiny_proof_precondition_matrix_includes_primary_provider_families() -> None:
    summary = scaffold.describe_provider_tiny_proof_preconditions()
    candidates = _tiny_candidates_by_surface()

    assert summary["candidateProviderFamilyIds"] == ["codex", "claude", "antigravity"]
    assert {candidate["providerFamilyId"] for candidate in summary["candidates"]} == {
        "codex",
        "claude",
        "antigravity",
    }
    assert "openai_api_pipeline" not in {candidate["providerFamilyId"] for candidate in summary["candidates"]}
    assert summary["excludedSurfaces"] == [
        {
            "providerFamilyId": "openai_api_pipeline",
            "surfaceId": "openai_api_pipeline",
            "reasonCode": "api_pipeline_skeleton_not_tiny_proof_candidate",
        }
    ]
    assert {"codex_sdk_python", "codex_cli"}.issubset(candidates)
    assert {"claude_agent_sdk_python", "claude_code_cli"}.issubset(candidates)
    assert {"antigravity_sdk_python", "antigravity_cli"}.issubset(candidates)


def test_tiny_proof_candidates_are_visible_but_never_executable_now() -> None:
    for candidate in scaffold.describe_provider_tiny_proof_preconditions()["candidates"]:
        assert candidate["providerAuthScope"] == "provider_auth_ai_backend"
        assert candidate["appOauthOidcInScope"] is False
        assert candidate["publicMcpConnectorInScope"] is False
        assert candidate["outputClassProofInScope"] is False
        assert candidate["mayRunNow"] is False
        assert candidate["wouldExecuteProvider"] is False
        assert candidate["maxLiveCallsForFutureProof"] == 1
        assert candidate["requiresOwnerApproval"] is True
        assert candidate["requiresProcessEnvOnly"] is True
        assert candidate["noSecretInspection"] is True
        assert candidate["noTokenCacheInspection"] is True
        assert candidate["noCredentialPathDisclosure"] is True
        assert candidate["noBrowserLoginInThisTask"] is True
        assert candidate["noDeviceFlowInThisTask"] is True
        assert candidate["noProviderCommandInThisTask"] is True
        assert candidate["noSdkImportInThisTask"] is True
        assert candidate["defaultBlocked"] is True


def test_tiny_proof_auth_requirements_have_official_support_state_and_names_only(monkeypatch) -> None:
    env = {
        "OPENAI_API_KEY": "sk-secret-value",
        "CODEX_ACCESS_TOKEN": "codex-secret-token",
        "ANTHROPIC_API_KEY": "anthropic-secret",
        "ANTHROPIC_AUTH_TOKEN": "anthropic-auth-token-secret",
        "GOOGLE_APPLICATION_CREDENTIALS": "C:/Users/operator/google-auth.json",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    payload = scaffold.describe_provider_tiny_proof_preconditions(env=env)
    serialized = json.dumps(payload)
    support_states = {
        requirement["officialSupportState"]
        for requirement in payload["authRequirements"]
    }

    assert support_states.issubset(
        {
            "official_supported",
            "official_supported_but_live_unverified",
            "contract_only_pending_doc_confirmation",
            "planned_or_external_only",
            "unsupported",
            "unknown_do_not_run",
        }
    )
    assert "OPENAI_API_KEY" in serialized
    assert "CODEX_ACCESS_TOKEN" in serialized
    assert "ANTHROPIC_API_KEY" in serialized
    assert "ANTHROPIC_AUTH_TOKEN" in serialized
    for forbidden in (
        "sk-secret-value",
        "codex-secret-token",
        "anthropic-secret",
        "anthropic-auth-token-secret",
        "google-auth.json",
        "C:/Users/operator",
        ".mcp.json",
        "browser/cookies",
        "keychain",
    ):
        assert forbidden not in serialized


def test_tiny_proof_future_approval_env_contract_is_represented_but_not_required_now() -> None:
    summary = scaffold.describe_provider_tiny_proof_preconditions()
    env_names = {item["envName"] for item in summary["futureApprovalEnvContract"]}
    plan = scaffold.plan_provider_tiny_proof("codex", "codex_cli", env={})

    assert env_names == {
        "XRML_PROVIDER_TINY_PROOF_APPROVED",
        "XRML_PROVIDER_TINY_PROOF_PROVIDER",
        "XRML_PROVIDER_TINY_PROOF_SURFACE",
        "XRML_PROVIDER_TINY_PROOF_MODE",
        "XRML_PROVIDER_TINY_PROOF_MAX_CALLS",
        "XRML_PROVIDER_TINY_PROOF_ARTIFACT",
        "XRML_PROVIDER_TINY_PROOF_PROMPT_CLASS",
    }
    assert all(observed is False for observed in plan["futureLiveApprovalEnvObserved"].values())
    assert plan["mayRunNow"] is False
    assert plan["wouldExecuteProvider"] is False
    assert "tiny_proof_not_executed" in plan["reasonCodes"]


def test_tiny_proof_plan_reports_env_presence_booleans_without_values() -> None:
    env = {
        "XRML_PROVIDER_TINY_PROOF_APPROVED": "1",
        "XRML_PROVIDER_TINY_PROOF_PROVIDER": "codex",
        "XRML_PROVIDER_TINY_PROOF_SURFACE": "codex_cli",
        "XRML_PROVIDER_TINY_PROOF_MODE": "tiny_live",
        "XRML_PROVIDER_TINY_PROOF_MAX_CALLS": "1",
        "XRML_PROVIDER_TINY_PROOF_ARTIFACT": "output/provider-live-proof/future-proof.json",
        "XRML_PROVIDER_TINY_PROOF_PROMPT_CLASS": "sentinel_only",
        "CODEX_ACCESS_TOKEN": "codex-secret-token",
        "OPENAI_API_KEY": "sk-secret-value",
    }
    plan = scaffold.plan_provider_tiny_proof("codex", "codex_cli", env=env)
    serialized = json.dumps(plan)

    assert all(plan["futureLiveApprovalEnvObserved"].values())
    assert plan["futureProviderAuthEnvObserved"]["CODEX_ACCESS_TOKEN"] is True
    assert plan["futureProviderAuthEnvObserved"]["OPENAI_API_KEY"] is True
    assert "codex-secret-token" not in serialized
    assert "sk-secret-value" not in serialized
    assert "future-proof.json" not in serialized
    assert plan["mayRunNow"] is False
    assert plan["wouldExecuteProvider"] is False


def test_tiny_proof_artifact_contract_is_separate_from_output_class_proof() -> None:
    contract = scaffold.describe_provider_tiny_proof_artifact_contract()
    gates = contract["fixedValues"]
    plan = scaffold.plan_provider_tiny_proof("claude", "claude_agent_sdk_python")

    assert contract["artifactKind"] == "provider_tiny_proof"
    assert gates["outputClassProof"] is False
    assert gates["researchOutputAllowed"] is False
    assert gates["reportDraftAllowed"] is False
    assert gates["cannotUnlockOutputClass"] is True
    assert gates["proofStatusCandidate"] == "live_verified_tiny"
    assert plan["postTinyProofOutputGates"]["tinyProofCanUnlockResearchOutput"] is False
    assert plan["postTinyProofOutputGates"]["tinyProofCanUnlockReportDraft"] is False
    assert plan["postTinyProofOutputGates"]["researchOutputGate"]["blockedReason"] == "insufficient_proof_level_for_output_class"
    assert plan["postTinyProofOutputGates"]["reportDraftGate"]["blockedReason"] == "insufficient_proof_level_for_output_class"


def test_tiny_proof_prompt_contract_is_sentinel_only_and_has_no_paper_or_cot_request() -> None:
    prompt = scaffold.describe_provider_tiny_proof_prompt_contract()

    assert prompt["promptClass"] == "sentinel_only"
    assert prompt["oneSentenceOnly"] is True
    assert prompt["fixedJsonObjectOnly"] is True
    assert prompt["containsUserSecrets"] is False
    assert prompt["containsRepoSource"] is False
    assert prompt["containsPaperData"] is False
    assert prompt["containsStorageData"] is False
    assert prompt["requestsChainOfThought"] is False
    assert prompt["expectedSentinelJsonShape"]["sentinel"] == "xrml-provider-tiny-proof"
    assert prompt["expectedSentinelJsonShape"]["no_chain_of_thought"] is True


def test_tiny_proof_does_not_cross_unlock_paper_review_or_weekly_report_gates() -> None:
    plan = scaffold.plan_provider_tiny_proof("antigravity", "antigravity_cli")
    gates = plan["postTinyProofOutputGates"]

    assert gates["proofStatusCandidate"] == "live_verified_tiny"
    assert gates["paperReviewAndWeeklyReportRemainSeparate"] is True
    assert gates["researchOutputGate"]["requiredProofLevel"] == "research_output_verified_tiny"
    assert gates["reportDraftGate"]["requiredProofLevel"] == "report_draft_verified_tiny"
    assert gates["researchOutputGate"]["allowed"] is False
    assert gates["reportDraftGate"]["allowed"] is False


def test_tiny_proof_api_pipeline_skeleton_cannot_be_selected() -> None:
    plan = scaffold.plan_provider_tiny_proof("openai_api_pipeline", "openai_api_pipeline")

    assert plan["candidate"] is None
    assert plan["recommendationStatus"] == "NO_TINY_PROOF_CANDIDATE_READY"
    assert plan["mayRunNow"] is False
    assert plan["wouldExecuteProvider"] is False
    assert "NO_TINY_PROOF_CANDIDATE_READY" in plan["reasonCodes"]


def test_first_tiny_proof_candidate_recommendation_is_deterministic() -> None:
    first = scaffold.recommend_provider_tiny_proof_candidates()
    second = scaffold.recommend_provider_tiny_proof_candidates()

    assert first == second
    assert first["recommendationStatus"] == "blocked_pending_future_approval_and_provider_auth"
    assert first["recommendedFirstCandidate"]["surfaceId"] == "codex_cli"
    assert first["recommendedFirstCandidate"]["providerFamilyId"] == "codex"
    assert first["recommendedFirstCandidate"]["wouldExecuteProvider"] is False
    assert first["orderedCandidates"][0]["surfaceId"] == "codex_cli"


def test_no_tiny_proof_candidate_ready_when_all_candidates_are_unsafe() -> None:
    all_surface_ids = [candidate["surfaceId"] for candidate in scaffold.describe_provider_tiny_proof_preconditions()["candidates"]]
    recommendation = scaffold.recommend_provider_tiny_proof_candidates(unsafe_surface_ids=all_surface_ids)
    summary = scaffold.describe_provider_tiny_proof_preconditions(unsafe_surface_ids=all_surface_ids)

    assert recommendation["recommendationStatus"] == "NO_TINY_PROOF_CANDIDATE_READY"
    assert recommendation["recommendedFirstCandidate"] is None
    assert recommendation["orderedCandidates"] == []
    assert summary["recommendation"]["recommendationStatus"] == "NO_TINY_PROOF_CANDIDATE_READY"


def test_tiny_precondition_status_is_stable_redacted_and_non_live() -> None:
    first = scaffold.describe_provider_tiny_proof_preconditions()
    second = scaffold.describe_provider_tiny_proof_preconditions()
    serialized = json.dumps(first)

    assert first == second
    assert first["nonLive"] is True
    assert first["mayRunNow"] is False
    assert first["wouldExecuteProvider"] is False
    assert first["mayRunProviderCommand"] is False
    assert first["mayImportSdk"] is False
    assert first["mayCallNetwork"] is False
    for forbidden in (
        "sk-",
        "Bearer ",
        "C:\\",
        "C:/Users/",
        ".codex/",
        ".claude/",
        ".config/",
        ".mcp.json",
        "auth.json",
        "browser/cookies",
        "keychain",
        "secret-value",
    ):
        assert forbidden not in serialized


def test_tiny_precondition_functions_do_not_run_commands_import_sdks_read_files_or_network(monkeypatch) -> None:
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("tiny-proof preconditions must not execute, import, read credential files, or call network")

    monkeypatch.setattr(subprocess, "run", fail_if_called)
    monkeypatch.setattr(subprocess, "Popen", fail_if_called)
    monkeypatch.setattr(importlib, "import_module", fail_if_called)
    monkeypatch.setattr(socket, "create_connection", fail_if_called)
    monkeypatch.setattr(Path, "read_text", fail_if_called)
    monkeypatch.setattr(Path, "open", fail_if_called)

    scaffold.describe_provider_tiny_proof_preconditions()
    scaffold.plan_provider_tiny_proof("codex", "codex_cli")
    scaffold.recommend_provider_tiny_proof_candidates()
    scaffold.describe_provider_tiny_proof_artifact_contract()
    scaffold.describe_provider_tiny_proof_prompt_contract()
