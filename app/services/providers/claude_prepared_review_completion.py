"""Strict Claude Code result envelope, independent of credentials and authority.

Wire fields follow the official claude-agent-sdk-python ResultMessage and
_internal/message_parser.py result branch. Unknown extensions fail closed in V1.
https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/types.py
https://code.claude.com/docs/en/agent-sdk/structured-outputs
"""
import json
import math
from typing import Any

from app.domain.journal_exchange import preserved_json
from app.services.providers.base import ProviderError, ProviderOutputError, ProviderReportedFailure, ProviderResult
from app.services.providers.codex_journal_plan import MAX_STREAM_BYTES

_REQUIRED = {"type", "subtype", "duration_ms", "duration_api_ms", "is_error", "num_turns", "session_id"}
_OPTIONAL = {"stop_reason", "total_cost_usd", "usage", "result", "structured_output", "modelUsage",
             "permission_denials", "deferred_tool_use", "errors", "api_error_status", "uuid", "terminal_reason", "origin"}
_SUBTYPES = {"success", "error_during_execution", "error_max_turns", "error_max_structured_output_retries"}


def _object(raw: str) -> dict[str, Any]:
    if not isinstance(raw, str) or not 0 < len(raw.encode("utf-8")) <= MAX_STREAM_BYTES:
        raise ValueError()

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError()
        return number

    value = json.loads(raw, object_pairs_hook=unique, parse_float=finite, parse_constant=finite)
    if not isinstance(value, dict):
        raise ValueError()
    preserved_json(value).encode("utf-8", errors="strict")
    return value


def _identifier(value: Any) -> bool:
    return (isinstance(value, str) and 1 <= len(value) <= 200 and value == value.strip()
            and all(ord(char) >= 32 and ord(char) != 127 for char in value))


def _count(value: Any) -> bool:
    return type(value) is int and 0 <= value <= 10**9


def _metadata(value: dict[str, Any]) -> dict[str, int]:
    for key in ("duration_ms", "duration_api_ms", "num_turns"):
        if not _count(value[key]):
            raise ValueError()
    if not _identifier(value["session_id"]):
        raise ValueError()
    for key in ("result", "uuid"):
        if value.get(key) is not None and not isinstance(value[key], str):
            raise ValueError()
    if value.get("uuid") is not None and not _identifier(value["uuid"]):
        raise ValueError()
    if value.get("total_cost_usd") is not None:
        amount = value["total_cost_usd"]
        if type(amount) not in {int, float} or not math.isfinite(amount) or amount < 0:
            raise ValueError()
    if value.get("api_error_status") is not None:
        status = value["api_error_status"]
        if type(status) is not int or not 400 <= status <= 599:
            raise ValueError()
    errors = value.get("errors")
    if errors is not None and (not isinstance(errors, list) or any(not isinstance(item, str) for item in errors)):
        raise ValueError()
    # This generation-only plan cannot confirm a run involving tool permissions,
    # deferred work or another session's injected prompt.
    if (value.get("permission_denials") not in (None, [])
            or value.get("deferred_tool_use") is not None
            or value.get("origin") not in (None, {"kind": "human"})):
        raise ValueError()
    usage = value.get("usage")
    if usage is not None and not isinstance(usage, dict):
        raise ValueError()
    usage = usage or {}
    for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        if key in usage and not _count(usage[key]):
            raise ValueError()
    server_tools = usage.get("server_tool_use")
    if server_tools is not None and (not isinstance(server_tools, dict)
            or any(key not in {"web_search_requests", "web_fetch_requests"} or type(number) is not int or number != 0
                   for key, number in server_tools.items())):
        raise ValueError()
    models = value.get("modelUsage")
    if models is not None:
        if not isinstance(models, dict):
            raise ValueError()
        for model, measured in models.items():
            if not _identifier(model) or not isinstance(measured, dict):
                raise ValueError()
            if measured.get("webSearchRequests", 0) != 0:
                raise ValueError()
            for key in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens",
                        "webSearchRequests", "contextWindow", "maxOutputTokens"):
                if key in measured and not _count(measured[key]):
                    raise ValueError()
    return {key: usage[key] for key in ("input_tokens", "output_tokens") if key in usage}


def read_completion(raw: str, exit_code: int) -> ProviderResult:
    """Never parse stderr/error prose as authority, a review, or a retry instruction."""
    try:
        value = _object(raw)
        if (not _REQUIRED <= set(value) or set(value) - _REQUIRED - _OPTIONAL
                or value["type"] != "result" or value["subtype"] not in _SUBTYPES
                or type(value["is_error"]) is not bool or type(exit_code) is not int or exit_code not in {0, 1}):
            raise ValueError()
        usage = _metadata(value)
        reason, subtype = value.get("terminal_reason"), value["subtype"]
        if reason not in {None, "completed", "api_error", "max_turns", "max_structured_output_retries",
                          "aborted_streaming", "aborted_tools"}:
            raise ValueError()
        if value.get("stop_reason") not in {None, "end_turn", "stop_sequence", "max_tokens", "refusal"}:
            raise ValueError()
        if value["is_error"]:
            # Only ordinary CLI error exit 1 can confirm ResultError. A crash
            # or other abnormal exit remains unknown even with plausible prose.
            # A contradictory result remains uncertain; never authorize retry
            # from one claimed cancellation field in an otherwise invalid frame.
            if exit_code == 0 or value.get("structured_output") is not None:
                raise ValueError()
            if reason in {"aborted_streaming", "aborted_tools"} and subtype == "error_during_execution":
                raise ProviderReportedFailure("cancelled")
            if subtype == "error_max_turns" and reason in {None, "max_turns"}:
                raise ProviderReportedFailure("incomplete")
            if subtype == "error_max_structured_output_retries" and reason in {None, "max_structured_output_retries"}:
                raise ProviderReportedFailure("incomplete")
            if subtype in {"success", "error_during_execution"} and reason in {None, "api_error"}:
                raise ProviderReportedFailure("failed")
            raise ValueError()
        if (exit_code != 0 or subtype != "success" or reason not in {None, "completed"}
                or value.get("errors") not in (None, []) or value.get("api_error_status") is not None):
            raise ValueError()
        if value.get("stop_reason") in {"max_tokens", "refusal"}:
            if value["stop_reason"] == "refusal":
                from app.services.providers.base import ProviderRefusalError
                raise ProviderRefusalError("paper_claude_provider_refused")
            raise ProviderReportedFailure("incomplete")
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        raise ProviderError("paper_claude_completion_outcome_unknown") from None
    payload = value.get("structured_output")
    if not isinstance(payload, dict):
        raise ProviderOutputError("paper_claude_output_invalid")
    return ProviderResult("claude_code", "not_reported", value["session_id"], payload,
                          usage=usage, raw_text=preserved_json(payload))
