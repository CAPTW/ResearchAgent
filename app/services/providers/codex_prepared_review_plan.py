"""Frozen paper CLI plan and unsigned transport archive, without execution authority.

V1 deliberately reuses the frozen Journal V1 argv/capture/compiler profile, not
its output schema or grant. Future profile changes require a new version. Reads
rebuild from archived fingerprints without opening a launcher or account files.
An archive binds local bytes; it cannot authenticate a caller or prove live use.
"""
import math
import re
from typing import Any

from app.domain import prepared_review
from app.domain.journal_exchange import preserved_json
from app.services.codex_cli_output_schema import build_codex_cli_report_draft_schema
from app.services.providers.base import ProviderRequest, ProviderResult
from app.services.providers.codex_journal_plan import (
    MAX_FINAL_BYTES, MAX_INPUT_BYTES, MAX_STDERR_BYTES, MAX_STREAM_BYTES, argv,
)

ADAPTER_ID = "xrml-codex-prepared-review"
MANIFEST_SCHEMA = "CODEX_PREPARED_REVIEW_CLI_V1"
TRANSPORT_SCHEMA = "PREPARED_REVIEW_CLI_TRANSPORT_V1"


def build_plan(contract: dict[str, Any], *, runtime_identity: dict[str, Any], reasoning_effort: str,
               timeout_seconds: float) -> tuple[str, str, dict[str, Any]]:
    try:
        frozen = prepared_review.validate_contract(contract)
        if (type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 120
                or reasoning_effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
                or set(runtime_identity) != {"launcher_sha256", "environment_sha256", "workspace_sha256"}
                or any(not isinstance(value, str) or not re.fullmatch("[a-f0-9]{64}", value)
                       for value in runtime_identity.values())
                or frozen["provider_name"] != "codex_cli"
                or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,199}", frozen["model"])):
            raise ValueError()
        request = ProviderRequest(**frozen["provider_request"])
        stdin = preserved_json(frozen["provider_request"])
        schema = preserved_json(build_codex_cli_report_draft_schema(request.schema))
        if len(stdin.encode("utf-8")) > MAX_INPUT_BYTES or len(schema.encode("utf-8")) > MAX_INPUT_BYTES:
            raise ValueError()
        manifest = dict(schema_version=MANIFEST_SCHEMA, adapter_id=ADAPTER_ID, adapter_version="1",
            runtime_profile="CODEX_JOURNAL_CLI_V1", contract_sha256=prepared_review.sha(preserved_json(frozen)),
            request_sha256=frozen["request_sha256"], model=frozen["model"], reasoning_effort=reasoning_effort,
            runtime_identity=dict(runtime_identity),
            argv_shape=argv(("{explicit_launcher}",), "{attempt_dir}", frozen["model"], reasoning_effort),
            stdin_sha256=prepared_review.sha(stdin), stdin_bytes=len(stdin.encode("utf-8")),
            schema_sha256=prepared_review.sha(schema), timeout_seconds=float(timeout_seconds),
            max_stdout_bytes=MAX_STREAM_BYTES, max_stderr_bytes=MAX_STDERR_BYTES, max_final_bytes=MAX_FINAL_BYTES,
            max_review_output_bytes=prepared_review.MAX_OUTPUT_BYTES,
            automatic_retry=False, output_token_limit_enforced=False, temperature_enforced=False,
            tool_isolation="REQUESTED_NOT_LIVE_VERIFIED", model_identity="REQUESTED_NOT_INDEPENDENTLY_VERIFIED")
        return stdin, schema, manifest
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ValueError("paper_codex_configuration_invalid") from None


def _marker(contract: dict[str, Any], raw: str, manifest: dict[str, Any]) -> dict[str, Any]:
    _, _, expected = build_plan(contract, runtime_identity=manifest["runtime_identity"],
        reasoning_effort=manifest["reasoning_effort"], timeout_seconds=manifest["timeout_seconds"])
    if preserved_json(manifest) != preserved_json(expected):
        raise ValueError("paper_codex_transport_invalid")
    return dict(schema_version=TRANSPORT_SCHEMA, adapter_id=ADAPTER_ID, adapter_version="1", manifest=expected,
                manifest_sha256=prepared_review.sha(preserved_json(expected)), response_sha256=prepared_review.sha(raw),
                authority="NOT_ESTABLISHED", live_execution="NOT_VERIFIED")


def transport_record(contract: dict[str, Any], response: ProviderResult, manifest: dict[str, Any]) -> dict[str, Any]:
    record = prepared_review.result(contract, response)
    raw = record["validation"]["prepared_execution"]["response"]["raw_text"]
    record["validation"]["execution"] = _marker(contract, raw, manifest)
    return record


def validate_transport(record: dict[str, Any], packet: dict[str, Any]) -> None:
    """Validate the reserved transport marker on both receipt write and read."""
    try:
        prepared_review.validate_record(record, packet)
        validation = record["validation"]
        proof = validation["prepared_execution"]
        marker = validation["execution"]
        expected = _marker(proof["contract"], proof["response"]["raw_text"], marker["manifest"])
        if preserved_json(marker) != preserved_json(expected):
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ValueError("paper_codex_transport_invalid") from None
