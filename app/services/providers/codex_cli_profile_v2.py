"""Explicit ChatGPT CLI profile; configuration intent is not live qualification.

V1 remains a historical contract. This version uses a custom provider name,
avoiding unsupported overrides of Codex's reserved built-in OpenAI provider.
The official CLI resolves its authentication and endpoint; XRML reads neither.
"""
from typing import Any

from app.services.codex_cli_report_draft import build_codex_cli_exec_argv
from app.services.providers.codex_journal_plan import CONFIG as V1_CONFIG

RUNTIME_PROFILE = "CODEX_JOURNAL_CLI_V2"
CONFIG = tuple(setting for setting in V1_CONFIG
               if not setting.startswith(("model_provider=", "model_providers.openai."))) + (
    'model_provider="xrml_openai"',
    'model_providers.xrml_openai.name="OpenAI"',
    'model_providers.xrml_openai.requires_openai_auth=true',
    'model_providers.xrml_openai.wire_api="responses"',
    'model_providers.xrml_openai.request_max_retries=0',
    'model_providers.xrml_openai.stream_max_retries=0',
    'model_providers.xrml_openai.supports_websockets=false',
    'forced_login_method="chatgpt"',
)


def profile_metadata() -> dict[str, Any]:
    return dict(runtime_profile=RUNTIME_PROFILE, model_provider="xrml_openai", requires_openai_auth=True,
        wire_api="responses", forced_login_method="chatgpt", request_max_retries=0, stream_max_retries=0,
        supports_websockets=False, authentication="OFFICIAL_CLI_CHATGPT_REQUIRED_NOT_VERIFIED",
        endpoint_resolution="OFFICIAL_CLI_OWNED_NOT_LIVE_VERIFIED", retry_policy="REQUESTED_ZERO_NOT_LIVE_VERIFIED",
        xrml_process_relaunch=False, remote_call_count_enforced=False, cost_limit_enforced=False,
        live_execution="NOT_VERIFIED", billing_route="CHATGPT_ACCOUNT_NOT_VERIFIED",
        raw_output_encoding="LAST_MESSAGE_JSON_UTF8_V1", raw_envelope_retained=False)


def argv(prefix: tuple[str, ...], cwd: str, model: str, effort: str) -> list[str]:
    command = build_codex_cli_exec_argv(launcher_argv=prefix, cwd=cwd,
        schema_path=cwd + "/output-schema.json", last_message_path=cwd + "/last-message.json")
    command[-1:-1] = ["--ignore-user-config", "--ignore-rules", "--strict-config", "--model", model,
                     "-c", 'model_reasoning_effort="' + effort + '"',
                     *[part for setting in CONFIG for part in ("-c", setting)]]
    return command
