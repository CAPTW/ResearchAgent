"""Immutable Grok ACP paper plan; unsigned history does not grant live authority.

Official ACP agent flags differ from headless-only tool filtering flags. Empty
profile tools inherit all tools, so this plan does not claim no-tool isolation.
"""
from dataclasses import asdict
import math
from pathlib import Path
import re
from typing import Any

from app.domain import prepared_review
from app.domain.journal_exchange import preserved_json
from app.services.providers.base import ProviderError, ProviderResult
from app.services.providers.codex_journal_plan import MAX_INPUT_BYTES, MAX_STDERR_BYTES, MAX_STREAM_BYTES
from app.services.providers.grok_prepared_review_protocol import MAX_EVENTS, replay

ADAPTER_ID = "xrml-grok-prepared-review"
MANIFEST_SCHEMA = "GROK_PREPARED_REVIEW_ACP_V1"
TRANSPORT_SCHEMA = "PREPARED_REVIEW_GROK_ACP_TRANSPORT_V1"
SURFACE = "grok_cli"
FIXED_ENV = {"GROK_DISABLE_AUTOUPDATER": "1", "GROK_SUBAGENTS": "0", "GROK_MEMORY": "0", "GROK_WEB_FETCH": "0", "GROK_TOOL_SEARCH": "0"} | {
    f"GROK_{origin}_{kind}_ENABLED": "0" for origin in ("CLAUDE", "CURSOR")
    for kind in ("SKILLS", "RULES", "AGENTS", "MCPS", "HOOKS")}


def agent_text(instructions: str) -> str:
    # Do not set tools:[]: Grok documents that as inheriting all tools.
    return ("---\nname: xrml-paper-review-v1\ndescription: Review only the supplied paper request.\n"
            "discoverSkills: false\nagentsMd: false\n---\n" + instructions + "\n")


def argv(prefix: tuple[str, ...], agent_path: str, model: str) -> list[str]:
    return [*prefix, "--no-auto-update", "agent", "--model", model,
            "--agent-profile", agent_path, "--no-leader", "stdio"]


def build_plan(contract: dict[str, Any], *, runtime_identity: dict[str, Any],
               timeout_seconds: float) -> tuple[str, dict[str, Any]]:
    try:
        frozen = prepared_review.validate_contract(contract)
        if (type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 120
                or set(runtime_identity) != {"launcher_sha256", "environment_sha256", "workspace_sha256"}
                or any(not isinstance(value, str) or not re.fullmatch("[a-f0-9]{64}", value) for value in runtime_identity.values())
                or frozen["provider_name"] != "grok_cli"
                or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,199}", frozen["model"])):
            raise ValueError()
        request_text = preserved_json(frozen["provider_request"])
        if len(request_text.encode("utf-8")) > MAX_INPUT_BYTES:
            raise ValueError()
        manifest = dict(schema_version=MANIFEST_SCHEMA, adapter_id=ADAPTER_ID, adapter_version="1",
            runtime_profile=MANIFEST_SCHEMA, fixed_environment=dict(FIXED_ENV), protocol_version=1,
            contract_sha256=prepared_review.sha(preserved_json(frozen)), request_sha256=frozen["request_sha256"],
            model=frozen["model"], runtime_identity=dict(runtime_identity),
            argv_shape=argv(("{explicit_launcher}",), "{attempt}/paper-agent.md", frozen["model"]),
            prompt_sha256=prepared_review.sha(request_text), prompt_bytes=len(request_text.encode("utf-8")),
            schema_sha256=prepared_review.sha(preserved_json(frozen["provider_request"]["schema"])),
            agent_sha256=prepared_review.sha(agent_text(frozen["provider_request"]["instructions"])),
            timeout_seconds=float(timeout_seconds), max_stdout_bytes=MAX_STREAM_BYTES, max_stderr_bytes=MAX_STDERR_BYTES,
            max_review_output_bytes=prepared_review.MAX_OUTPUT_BYTES, max_events=MAX_EVENTS,
            prompt_count=1, client_capabilities={}, authentication="OFFICIAL_CLI_CACHED_TOKEN_ONLY_NOT_VERIFIED",
            billing_route="NOT_VERIFIED", session_retention="CLI_MANAGED_PROFILE_STORAGE_NOT_ISOLATED",
            model_selection="ACP_CONFIG_OPTION_MODEL_EXACT", structured_output="PROMPT_REQUESTED_LOCAL_VALIDATION_ONLY",
            completion="CORRELATED_PROMPT_STOP_REASON", archive_extent="CAPTURED_ACP_PREFIX_THROUGH_TERMINAL",
            process_cleanup="HOST_TERMINATES_OWNED_ACP_CHILD_AFTER_TERMINAL",
            automatic_retry=False, xrml_process_relaunch=False, remote_call_count_enforced=False,
            internal_retries="CLI_MANAGED_NOT_LIVE_VERIFIED", managed_hooks="NOT_ISOLATED_NOT_LIVE_VERIFIED",
            global_config_and_mcp="NOT_ISOLATED_NOT_LIVE_VERIFIED", tool_isolation="NOT_ESTABLISHED",
            output_rejection_prevents_side_effects=False, physical_remote_cancellation="NOT_ESTABLISHED",
            output_token_limit_enforced=False, temperature_enforced=False, model_identity="REQUESTED_NOT_INDEPENDENTLY_VERIFIED",
            raw_review_encoding="ACP_AGENT_MESSAGE_TEXT_UTF8_V1", token_usage="NOT_REPORTED_BY_SUPPORTED_ACP_PROFILE")
        return request_text, manifest
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        raise ValueError("paper_grok_configuration_invalid") from None


def _marker(contract: dict[str, Any], response: ProviderResult, manifest: dict[str, Any],
            transcript: list[dict[str, str]], attempt_cwd: str) -> dict[str, Any]:
    request, expected = build_plan(contract, runtime_identity=manifest["runtime_identity"], timeout_seconds=manifest["timeout_seconds"])
    path = Path(attempt_cwd)
    if (not path.is_absolute() or not path.name.startswith("grok-review-")
            or prepared_review.sha(str(path.parent)) != manifest["runtime_identity"]["workspace_sha256"]):
        raise ValueError("paper_grok_transport_invalid")
    observed = replay(transcript, request_text=request, model=manifest["model"], cwd=attempt_cwd)
    server = "".join(entry["raw_line"] for entry in transcript if entry["direction"] == "server")
    if (len(server.encode("utf-8")) > MAX_STREAM_BYTES or preserved_json(manifest) != preserved_json(expected)
            or preserved_json(asdict(observed)) != preserved_json(asdict(response))):
        raise ValueError("paper_grok_transport_invalid")
    assert isinstance(response.raw_text, str)
    return dict(schema_version=TRANSPORT_SCHEMA, adapter_id=ADAPTER_ID, adapter_version="1", manifest=expected,
        manifest_sha256=prepared_review.sha(preserved_json(expected)), transcript=transcript,
        transcript_sha256=prepared_review.sha(preserved_json(transcript)), captured_stdout_sha256=prepared_review.sha(server),
        captured_stdout_bytes=len(server.encode("utf-8")), archive_extent="CAPTURED_ACP_PREFIX_THROUGH_TERMINAL",
        attempt_cwd=attempt_cwd, attempt_cwd_sha256=prepared_review.sha(attempt_cwd),
        review_sha256=prepared_review.sha(response.raw_text), review_encoding="ACP_AGENT_MESSAGE_TEXT_UTF8_V1",
        structured_output_sha256=prepared_review.sha(preserved_json(response.payload)),
        authority="NOT_ESTABLISHED", live_execution="NOT_VERIFIED")


def transport_record(contract: dict[str, Any], response: ProviderResult, manifest: dict[str, Any],
                     transcript: list[dict[str, str]], attempt_cwd: str) -> dict[str, Any]:
    record = prepared_review.result(contract, response)
    record["validation"]["execution"] = _marker(contract, response, manifest, transcript, attempt_cwd)
    return record


def validate_transport(record: dict[str, Any], packet: dict[str, Any]) -> None:
    try:
        prepared_review.validate_record(record, packet)
        proof, marker = record["validation"]["prepared_execution"], record["validation"]["execution"]
        response = ProviderResult(**{key: proof["response"][key] for key in (
            "provider_name", "model", "response_id", "payload", "usage", "raw_text")})
        expected = _marker(proof["contract"], response, marker["manifest"], marker["transcript"], marker["attempt_cwd"])
        if preserved_json(marker) != preserved_json(expected):
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, ProviderError):
        raise ValueError("paper_grok_transport_invalid") from None
