"""Strict single-turn Antigravity NDJSON reader; never authorizes execution.

Wire contract: https://www.antigravity.google/docs/cli/headless/
Step text is incremental telemetry, not the canonical structured response.
Rejecting unexpected activity after capture does not undo or prevent side effects.
"""
import json
import math
from pathlib import Path
import re
from typing import Any

from app.domain import prepared_review
from app.domain.journal_exchange import preserved_json
from app.services.providers.base import ProviderError, ProviderOutputError, ProviderReportedFailure, ProviderResult
from app.services.providers.codex_journal_plan import MAX_STREAM_BYTES

AGENT_NAME = "xrml-paper-review-v1"
USAGE_FIELDS = {"input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens"}


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in items:
        if key in value:
            raise ValueError()
        value[key] = item
    return value


def strict_json(raw: str) -> Any:
    value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    preserved_json(value).encode("utf-8", errors="strict")
    return value


def _number(value: Any) -> None:
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        raise ValueError()


def _usage(value: Any) -> dict[str, int]:
    if (not isinstance(value, dict) or set(value) != USAGE_FIELDS
            or any(type(item) is not int or item < 0 for item in value.values())):
        raise ValueError()
    return {key: value[key] for key in ("input_tokens", "output_tokens", "total_tokens")}


def read_completion(raw: str, exit_code: int, *, model: str, schema: dict[str, Any], attempt_cwd: str) -> ProviderResult:
    try:
        if (not isinstance(raw, str) or not 0 < len(raw.encode("utf-8")) <= MAX_STREAM_BYTES
                or type(exit_code) is not int or not 0 <= exit_code <= 255 or not isinstance(attempt_cwd, str)
                or not Path(attempt_cwd).is_absolute()):
            raise ValueError()
        # splitlines() would split legitimate Unicode NEL/LS/PS within JSON strings.
        lines = raw.split("\n")
        if lines[-1] == "":
            lines.pop()
        if not 2 <= len(lines) <= 4096 or any(not line.strip() for line in lines):
            raise ValueError()
        events = [strict_json(line) for line in lines]
        first = events[0]
        if not isinstance(first, dict) or set(first) != {"event", "conversation_id", "init"} or first["event"] != "init":
            raise ValueError()
        identity = first["conversation_id"]
        if not isinstance(identity, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,199}", identity):
            raise ValueError()
        init = first["init"]
        if (not isinstance(init, dict) or set(init) != {"cwd", "tools", "permission_mode", "model", "agent", "json_schema"}
                or not isinstance(init["cwd"], str) or Path(init["cwd"]) != Path(attempt_cwd)
                or init["tools"] != [] or init["permission_mode"] != "request-review"
                or init["model"] != model or init["agent"] != AGENT_NAME
                or preserved_json(init["json_schema"]) != preserved_json(schema)):
            raise ValueError()
        steps: dict[int, tuple[str, str]] = {}
        user_count = 0
        for event in events[1:-1]:
            if not isinstance(event, dict) or set(event) != {"event", "step_update"} or event["event"] != "step_update":
                raise ValueError()
            step = event["step_update"]
            required = {"conversation_id", "step_index", "state", "step_type"}
            if (not isinstance(step, dict) or not required <= set(step)
                    or set(step) - required - {"text_delta", "duration_seconds", "usage"}
                    or step["conversation_id"] != identity or type(step["step_index"]) is not int
                    or not 0 <= step["step_index"] <= 1_000_000
                    or step["state"] not in {"ACTIVE", "DONE"}
                    or step["step_type"] not in {"user_input", "agent_response", "checkpoint"}):
                raise ValueError()
            index, kind, state = step["step_index"], step["step_type"], step["state"]
            previous = steps.get(index)
            if previous is not None:
                if previous != (kind, "ACTIVE"):
                    raise ValueError()
            elif steps and index <= max(steps):
                raise ValueError()
            if kind == "user_input":
                if steps or state != "DONE":
                    raise ValueError()
                user_count += 1
            if "text_delta" in step and (kind != "agent_response" or not isinstance(step["text_delta"], str)):
                raise ValueError()
            if "duration_seconds" in step:
                _number(step["duration_seconds"])
            if "usage" in step:
                _usage(step["usage"])
            steps[index] = (kind, state)
        final = events[-1]
        if not isinstance(final, dict) or set(final) != {"event", "result"} or final["event"] != "result":
            raise ValueError()
        result = final["result"]
        required = {"conversation_id", "status", "response", "duration_seconds", "num_turns", "usage"}
        if (not isinstance(result, dict) or not required <= set(result)
                or set(result) - required - {"structured_output", "json_schema", "error"}
                or result["conversation_id"] != identity or type(result["num_turns"]) is not int
                or result["num_turns"] != 1 or user_count > 1 or not isinstance(result["response"], str)
                or not isinstance(result["status"], str)):
            raise ValueError()
        _number(result["duration_seconds"])
        usage = _usage(result["usage"])
        if "json_schema" in result and preserved_json(result["json_schema"]) != preserved_json(schema):
            raise ValueError()
        if result["status"] in {"ERROR", "CANCELED", "INTERRUPTED"}:
            if (exit_code == 0 or result["response"] != "" or "structured_output" in result
                    or not isinstance(result.get("error"), str) or not result["error"].strip()):
                raise ValueError()
            raise ProviderReportedFailure("failed" if result["status"] == "ERROR" else "cancelled")
        if (result["status"] != "SUCCESS" or exit_code != 0 or "error" in result or "json_schema" not in result
                or any(state != "DONE" for _, state in steps.values())):
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        raise ProviderError("paper_antigravity_completion_outcome_unknown") from None
    payload = result.get("structured_output")
    try:
        if not isinstance(payload, dict) or len(result["response"].encode("utf-8")) > prepared_review.MAX_OUTPUT_BYTES:
            raise ValueError()
        parsed = strict_json(result["response"])
        if not isinstance(parsed, dict) or preserved_json(parsed) != preserved_json(payload):
            raise ValueError()
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise ProviderOutputError("paper_antigravity_output_invalid") from None
    return ProviderResult("antigravity_cli", "not_reported", identity, payload, usage=usage, raw_text=result["response"])
