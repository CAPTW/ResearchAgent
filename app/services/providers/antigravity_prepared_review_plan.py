"""Frozen Antigravity paper plan and unsigned replayable output archive.

Official headless/subagent/changelog controls request a restricted main agent;
global hooks, startup MCP/config, Windows sandbox and internal retries are not
live-qualified. One XRML child does not establish one remote model request.
"""
from dataclasses import asdict
import math
from pathlib import Path
import re
from typing import Any

from app.domain import prepared_review
from app.domain.journal_exchange import preserved_json
from app.services.providers.antigravity_prepared_review_completion import AGENT_NAME, read_completion
from app.services.providers.base import ProviderError, ProviderResult
from app.services.providers.codex_journal_plan import MAX_INPUT_BYTES, MAX_STDERR_BYTES, MAX_STREAM_BYTES

ADAPTER_ID = "xrml-antigravity-prepared-review"
MANIFEST_SCHEMA = "ANTIGRAVITY_PREPARED_REVIEW_CLI_V1"
TRANSPORT_SCHEMA = "PREPARED_REVIEW_ANTIGRAVITY_CLI_TRANSPORT_V1"
SURFACE = "antigravity_cli"
FIXED_ENV = {"AGY_CLI_DISABLE_AUTO_UPDATE": "true"}


def agent_text(instructions: str) -> str:
    # Official subagents + changelog v1.1.14/v1.2.1. Hooks remain inherited.
    return ("---\nname: " + AGENT_NAME + "\ndescription: Review only the supplied paper request.\n"
            "tools: []\nmainAgent: true\nsubagent: false\ncommandExecutionPolicy: off\n"
            "mcpServers: []\nskills: []\nplugins: []\ninheritCustomizations: false\n"
            "excludeDefaultComponents: true\n---\n" + instructions + "\n")


def argv(prefix: tuple[str, ...], schema_file: str, model: str, timeout_seconds: float) -> list[str]:
    return [*prefix, "--input-format", "stream-json", "--output-format", "stream-json",
            "--json-schema", schema_file, "--model", model, "--agent", AGENT_NAME,
            "--effort", "low", "--print-timeout", f"{timeout_seconds:g}s", "--sandbox"]


def build_plan(contract: dict[str, Any], *, runtime_identity: dict[str, Any],
               timeout_seconds: float) -> tuple[str, str, dict[str, Any]]:
    try:
        frozen = prepared_review.validate_contract(contract)
        if (type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 120
                or set(runtime_identity) != {"launcher_sha256", "environment_sha256", "workspace_sha256"}
                or any(not isinstance(value, str) or not re.fullmatch("[a-f0-9]{64}", value) for value in runtime_identity.values())
                or frozen["provider_name"] != "antigravity_cli"
                or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,199}", frozen["model"])):
            raise ValueError()
        stdin = preserved_json(dict(event="user", message=dict(content=preserved_json(frozen["provider_request"])))) + "\n"
        schema = preserved_json(frozen["provider_request"]["schema"])
        agent = agent_text(frozen["provider_request"]["instructions"])
        if len(stdin.encode("utf-8")) > MAX_INPUT_BYTES:
            raise ValueError()
        manifest = dict(schema_version=MANIFEST_SCHEMA, adapter_id=ADAPTER_ID, adapter_version="1",
            runtime_profile=MANIFEST_SCHEMA, fixed_environment=dict(FIXED_ENV),
            contract_sha256=prepared_review.sha(preserved_json(frozen)), request_sha256=frozen["request_sha256"],
            model=frozen["model"], reasoning_effort="low", runtime_identity=dict(runtime_identity),
            argv_shape=argv(("{explicit_launcher}",), "{attempt}/output-schema.json", frozen["model"], float(timeout_seconds)),
            stdin_sha256=prepared_review.sha(stdin), stdin_bytes=len(stdin.encode("utf-8")), input_turns=1,
            schema_sha256=prepared_review.sha(schema), agent_sha256=prepared_review.sha(agent), agent_name=AGENT_NAME,
            timeout_seconds=float(timeout_seconds), max_stdout_bytes=MAX_STREAM_BYTES, max_stderr_bytes=MAX_STDERR_BYTES,
            max_review_output_bytes=prepared_review.MAX_OUTPUT_BYTES,
            automatic_retry=False, xrml_process_relaunch=False, remote_call_count_enforced=False,
            internal_retries="CLI_MANAGED_NOT_LIVE_VERIFIED", managed_hooks="NOT_ISOLATED_NOT_LIVE_VERIFIED",
            global_config_and_mcp="NOT_ISOLATED_NOT_LIVE_VERIFIED", windows_sandbox="REQUESTED_NOT_LIVE_VERIFIED",
            output_token_limit_enforced=False, temperature_enforced=False,
            tool_isolation="REQUESTED_AND_OUTPUT_CHECKED_NOT_LIVE_VERIFIED", output_rejection_prevents_side_effects=False,
            model_identity="REQUESTED_NOT_INDEPENDENTLY_VERIFIED", authentication="OFFICIAL_CLI_OWNED_NOT_VERIFIED",
            raw_review_encoding="RESULT_RESPONSE_JSON_UTF8_V1", step_text="NONCANONICAL_STREAM_TELEMETRY")
        return stdin, schema, manifest
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        raise ValueError("paper_antigravity_configuration_invalid") from None


def _marker(contract: dict[str, Any], response: ProviderResult, manifest: dict[str, Any], raw_stdout: str,
            attempt_cwd: str) -> dict[str, Any]:
    _, schema, expected = build_plan(contract, runtime_identity=manifest["runtime_identity"], timeout_seconds=manifest["timeout_seconds"])
    import json
    path = Path(attempt_cwd)
    if (not path.is_absolute() or not path.name.startswith("antigravity-review-")
            or prepared_review.sha(str(path.parent)) != manifest["runtime_identity"]["workspace_sha256"]):
        raise ValueError("paper_antigravity_transport_invalid")
    observed = read_completion(raw_stdout, 0, model=manifest["model"], schema=json.loads(schema), attempt_cwd=attempt_cwd)
    if preserved_json(manifest) != preserved_json(expected) or preserved_json(asdict(observed)) != preserved_json(asdict(response)):
        raise ValueError("paper_antigravity_transport_invalid")
    assert isinstance(response.raw_text, str)
    return dict(schema_version=TRANSPORT_SCHEMA, adapter_id=ADAPTER_ID, adapter_version="1", manifest=expected,
        manifest_sha256=prepared_review.sha(preserved_json(expected)), raw_stdout=raw_stdout,
        stdout_sha256=prepared_review.sha(raw_stdout), stdout_bytes=len(raw_stdout.encode("utf-8")),
        attempt_cwd=attempt_cwd, attempt_cwd_sha256=prepared_review.sha(attempt_cwd),
        review_sha256=prepared_review.sha(response.raw_text), review_encoding="RESULT_RESPONSE_JSON_UTF8_V1",
        structured_output_sha256=prepared_review.sha(preserved_json(response.payload)),
        authority="NOT_ESTABLISHED", live_execution="NOT_VERIFIED")


def transport_record(contract: dict[str, Any], response: ProviderResult, manifest: dict[str, Any],
                     raw_stdout: str, attempt_cwd: str) -> dict[str, Any]:
    record = prepared_review.result(contract, response)
    record["validation"]["execution"] = _marker(contract, response, manifest, raw_stdout, attempt_cwd)
    return record


def validate_transport(record: dict[str, Any], packet: dict[str, Any]) -> None:
    try:
        prepared_review.validate_record(record, packet)
        proof = record["validation"]["prepared_execution"]
        marker = record["validation"]["execution"]
        response = ProviderResult(**{key: proof["response"][key] for key in (
            "provider_name", "model", "response_id", "payload", "usage", "raw_text")})
        expected = _marker(proof["contract"], response, marker["manifest"], marker["raw_stdout"], marker["attempt_cwd"])
        if preserved_json(marker) != preserved_json(expected):
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, ProviderError):
        raise ValueError("paper_antigravity_transport_invalid") from None
