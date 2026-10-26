"""Frozen Claude Code paper plan and unsigned archive; no account or live authority.

CLI flags: https://code.claude.com/docs/en/cli-reference
Subscription auth needs non-bare mode. Managed hooks remain outside this plan's
control. One XRML child launch does not imply one remote request or a cost cap.
Historical V1 readers retain these exact flags and reconstruct without local I/O.
"""
from dataclasses import asdict
import math
import re
from typing import Any

from app.domain import prepared_review
from app.domain.journal_exchange import preserved_json
from app.services.providers.base import ProviderError, ProviderResult
from app.services.providers.claude_prepared_review_completion import read_completion
from app.services.providers.codex_journal_plan import MAX_INPUT_BYTES, MAX_STDERR_BYTES, MAX_STREAM_BYTES

ADAPTER_ID = "xrml-claude-prepared-review"
MANIFEST_SCHEMA = "CLAUDE_PREPARED_REVIEW_CLI_V1"
TRANSPORT_SCHEMA = "PREPARED_REVIEW_CLAUDE_CLI_TRANSPORT_V1"
SURFACE = "claude_code"
SETTINGS = preserved_json({"disableAllHooks": True, "disableClaudeAiConnectors": True})
# Official per-process controls, not a claim that managed hooks/auth are isolated.
# https://code.claude.com/docs/en/env-vars
FIXED_ENV = {"DISABLE_UPDATES": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}


def argv(prefix: tuple[str, ...], model: str, schema: str, instructions: str) -> list[str]:
    return [*prefix, "-p", "--safe-mode", "--output-format", "json", "--json-schema", schema,
            "--model", model, "--system-prompt", instructions, "--tools", "", "--disallowedTools", "mcp__*",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
            "--settings", SETTINGS, "--disable-slash-commands", "--no-session-persistence", "--no-chrome"]


def build_plan(contract: dict[str, Any], *, runtime_identity: dict[str, Any],
               timeout_seconds: float) -> tuple[str, str, dict[str, Any]]:
    try:
        frozen = prepared_review.validate_contract(contract)
        if (type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 120
                or set(runtime_identity) != {"launcher_sha256", "environment_sha256", "workspace_sha256", "config_dir_sha256"}
                or any(not isinstance(value, str) or not re.fullmatch("[a-f0-9]{64}", value)
                       for value in runtime_identity.values())
                or frozen["provider_name"] != "claude_code"
                or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,199}", frozen["model"])):
            raise ValueError()
        stdin = preserved_json(frozen["provider_request"])
        schema = preserved_json(frozen["provider_request"]["schema"])
        instructions = frozen["provider_request"]["instructions"]
        if len(stdin.encode("utf-8")) > MAX_INPUT_BYTES or len(schema.encode("utf-8")) > 16_000:
            raise ValueError()
        manifest = dict(schema_version=MANIFEST_SCHEMA, adapter_id=ADAPTER_ID, adapter_version="1",
            runtime_profile=MANIFEST_SCHEMA, fixed_environment=dict(FIXED_ENV),
            contract_sha256=prepared_review.sha(preserved_json(frozen)),
            request_sha256=frozen["request_sha256"], model=frozen["model"], runtime_identity=dict(runtime_identity),
            argv_shape=argv(("{explicit_launcher}",), frozen["model"], "{canonical_schema_json}", "{frozen_instructions}"),
            stdin_sha256=prepared_review.sha(stdin), stdin_bytes=len(stdin.encode("utf-8")),
            schema_sha256=prepared_review.sha(schema), system_prompt_sha256=prepared_review.sha(instructions),
            timeout_seconds=float(timeout_seconds), max_stdout_bytes=MAX_STREAM_BYTES, max_stderr_bytes=MAX_STDERR_BYTES,
            max_review_output_bytes=prepared_review.MAX_OUTPUT_BYTES,
            automatic_retry=False, xrml_process_relaunch=False, remote_call_count_enforced=False,
            internal_retries="CLI_MANAGED_NOT_LIVE_VERIFIED", managed_hooks="NOT_ISOLATED_NOT_LIVE_VERIFIED",
            output_token_limit_enforced=False, temperature_enforced=False,
            tool_isolation="REQUESTED_NOT_LIVE_VERIFIED", model_identity="REQUESTED_NOT_INDEPENDENTLY_VERIFIED",
            authentication="OFFICIAL_CLI_OWNED_NOT_VERIFIED", raw_review_encoding="EXTRACTED_CANONICAL_JSON_V1")
        return stdin, schema, manifest
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ValueError("paper_claude_configuration_invalid") from None


def _marker(contract: dict[str, Any], response: ProviderResult, manifest: dict[str, Any], raw_envelope: str) -> dict[str, Any]:
    _, _, expected = build_plan(contract, runtime_identity=manifest["runtime_identity"], timeout_seconds=manifest["timeout_seconds"])
    observed = read_completion(raw_envelope, 0)
    if preserved_json(manifest) != preserved_json(expected) or preserved_json(asdict(observed)) != preserved_json(asdict(response)):
        raise ValueError("paper_claude_transport_invalid")
    assert isinstance(response.raw_text, str)
    return dict(schema_version=TRANSPORT_SCHEMA, adapter_id=ADAPTER_ID, adapter_version="1", manifest=expected,
        manifest_sha256=prepared_review.sha(preserved_json(expected)), raw_envelope=raw_envelope,
        envelope_sha256=prepared_review.sha(raw_envelope), envelope_bytes=len(raw_envelope.encode("utf-8")),
        review_sha256=prepared_review.sha(response.raw_text), review_encoding="EXTRACTED_CANONICAL_JSON_V1",
        authority="NOT_ESTABLISHED", live_execution="NOT_VERIFIED")


def transport_record(contract: dict[str, Any], response: ProviderResult, manifest: dict[str, Any], raw_envelope: str) -> dict[str, Any]:
    record = prepared_review.result(contract, response)
    record["validation"]["execution"] = _marker(contract, response, manifest, raw_envelope)
    return record


def validate_transport(record: dict[str, Any], packet: dict[str, Any]) -> None:
    try:
        prepared_review.validate_record(record, packet)
        proof = record["validation"]["prepared_execution"]
        marker = record["validation"]["execution"]
        response = ProviderResult(**{key: proof["response"][key] for key in (
            "provider_name", "model", "response_id", "payload", "usage", "raw_text")})
        expected = _marker(proof["contract"], response, marker["manifest"], marker["raw_envelope"])
        if preserved_json(marker) != preserved_json(expected):
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, ProviderError):
        raise ValueError("paper_claude_transport_invalid") from None
