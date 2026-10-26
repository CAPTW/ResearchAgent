"""Versioned CLI plan, independent of credentials, processes and the filesystem.

Requested tool/config restrictions need live CLI qualification before activation.
The CLI exposes no verified hard output-token/temperature limit for this profile.
Historical readers rebuild V1 from saved fingerprints without reading local files.
V1 argv/config/compiler semantics are historical contracts: future changes require
a new manifest/adapter version and retention of this reader, not silent rebuilding.
"""
from dataclasses import asdict
import hashlib
import json
import math
import re
from typing import Any

from app.domain.journal_execution import Prepare, SCHEMA
from app.domain.journal_exchange import preserved_json
from app.domain.temporal_memory import digest
from app.services.codex_cli_output_schema import build_codex_cli_report_draft_schema
from app.services.codex_cli_report_draft import build_codex_cli_exec_argv
from app.services.providers.base import ProviderRequest

ADAPTER_ID = "xrml-codex-frozen-journal"
MAX_INPUT_BYTES = 4_000_000
MAX_STREAM_BYTES = 1_048_576
MAX_STDERR_BYTES = 32_768
MAX_FINAL_BYTES = 512_000
CONFIG = (
    'model_provider="openai"', 'approval_policy="never"', 'web_search="disabled"',
    'features.shell_tool=false', 'features.unified_exec=false', 'features.apps=false',
    'apps._default.enabled=false', 'agents.enabled=false', 'features.multi_agent=false',
    'features.hooks=false', 'features.memories=false', 'features.remote_plugin=false',
    'features.skill_mcp_dependency_install=false', 'tools.view_image=false',
    'mcp_servers={}', 'project_doc_max_bytes=0', 'model_providers.openai.request_max_retries=0',
    'model_providers.openai.stream_max_retries=0',
)


def sha(raw: str | bytes) -> str:
    return hashlib.sha256(raw.encode("utf-8") if isinstance(raw, str) else raw).hexdigest()


def argv(prefix: tuple[str, ...], cwd: str, model: str, effort: str) -> list[str]:
    command = build_codex_cli_exec_argv(launcher_argv=prefix, cwd=cwd,
        schema_path=cwd + "/output-schema.json", last_message_path=cwd + "/last-message.json")
    # Keep the old Prototype argv and schema contract unchanged.
    command[-1:-1] = ["--ignore-user-config", "--ignore-rules", "--strict-config", "--model", model,
                      "-c", 'model_reasoning_effort="' + effort + '"',
                      *[part for setting in CONFIG for part in ("-c", setting)]]
    return command


def build_plan(contract: dict[str, Any], *, runtime_identity: dict[str, Any], reasoning_effort: str,
               timeout_seconds: float) -> tuple[str, str, dict[str, Any]]:
    try:
        contract = json.loads(preserved_json(contract))
        if (type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 120
                or reasoning_effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
                or set(runtime_identity) != {"launcher_sha256", "environment_sha256", "workspace_sha256"}
                or any(not isinstance(value, str) or not re.fullmatch("[a-f0-9]{64}", value)
                       for value in runtime_identity.values())):
            raise ValueError()
        spec = Prepare.model_validate(contract["specification"])
        request = ProviderRequest(**contract["provider_request"])
        if (contract["schema_version"] != SCHEMA or spec.provider_name != "codex_cli"
                or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,199}", spec.model)
                or request.schema_name != "JOURNAL_PROPOSAL_V1"
                or request.max_output_tokens != spec.max_output_tokens
                or request.schema != contract["packet"]["output_schema"]):
            raise ValueError()
        stdin = preserved_json(asdict(request))
        # Reuse the existing structural compiler, not the Prototype proposal model.
        schema = preserved_json(build_codex_cli_report_draft_schema(request.schema))
        if len(stdin.encode("utf-8")) > MAX_INPUT_BYTES or len(schema.encode("utf-8")) > MAX_INPUT_BYTES:
            raise ValueError()
        manifest = dict(schema_version="CODEX_JOURNAL_CLI_V1", contract_digest=digest(contract),
            model=spec.model, reasoning_effort=reasoning_effort, runtime_identity=dict(runtime_identity),
            argv_shape=argv(("{explicit_launcher}",), "{attempt_dir}", spec.model, reasoning_effort),
            stdin_sha256=sha(stdin), stdin_bytes=len(stdin.encode("utf-8")), schema_sha256=sha(schema),
            timeout_seconds=float(timeout_seconds), max_stdout_bytes=MAX_STREAM_BYTES,
            max_stderr_bytes=MAX_STDERR_BYTES, max_final_bytes=MAX_FINAL_BYTES,
            automatic_retry=False, output_token_limit_enforced=False, temperature_enforced=False,
            tool_isolation="REQUESTED_NOT_LIVE_VERIFIED", model_identity="REQUESTED_NOT_INDEPENDENTLY_VERIFIED")
        return stdin, schema, manifest
    except (ValueError, TypeError, KeyError, RecursionError):
        raise ValueError("journal_codex_configuration_invalid") from None
