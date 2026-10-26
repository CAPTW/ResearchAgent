"""Generation-only JSONL completion validation; no domain parsing or authority."""
import json
from typing import Any

from app.services.providers.base import ProviderError, ProviderReportedFailure


def _event(raw: str) -> dict[str, Any]:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    def invalid(_):
        raise ValueError()
    value = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)
    if not isinstance(value, dict):
        raise ValueError()
    return value


def read_completion(stdout: str, final: str, code: int, *, error_prefix: str) -> tuple[str, dict[str, int]]:
    if code != 0:
        raise ProviderError(f"{error_prefix}_exit_outcome_unknown")
    try:
        # JSONL uses LF (optionally preceded by CR), not Unicode text boundaries.
        # Keep payload characters intact; only ignore existing ASCII blank lines.
        events = [_event(line) for line in stdout.split("\n") if line.strip(" \t\r")]
        if not events or any(row.get("type") not in {"thread.started", "turn.started", "item.started", "item.updated", "item.completed", "turn.completed", "turn.failed", "error"} for row in events):
            raise ValueError()
        kinds = [row.get("type") for row in events]
        if (kinds[:2] != ["thread.started", "turn.started"]
                or kinds[-1] not in {"turn.completed", "turn.failed"}
                or any(kind not in {"item.started", "item.updated", "item.completed"} for kind in kinds[2:-1])):
            raise ValueError()
        items = [row["item"] for row in events if row.get("type", "").startswith("item.")]
        if any(item.get("type") not in {"agent_message", "reasoning"} for item in items):
            raise ValueError()  # A tool event is outside this generation-only plan.
        thread_ids = [events[0].get("thread_id")]
        if not isinstance(thread_ids[0], str) or not 1 <= len(thread_ids[0]) <= 200:
            raise ValueError()
        if kinds[-1] == "turn.failed":
            error = events[-1].get("error")
            if not isinstance(error, dict) or not isinstance(error.get("message"), str):
                raise ValueError()
            raise ProviderReportedFailure("failed")
        messages = [row["item"]["text"] for row in events if row.get("type") == "item.completed" and row["item"].get("type") == "agent_message"]
        if not messages or not final or messages[-1].rstrip("\r\n") != final.rstrip("\r\n"):
            raise ValueError()
        usage = events[-1].get("usage")
        usage = {key: value for key, value in usage.items() if key in {"input_tokens", "output_tokens", "total_tokens"}
                 and type(value) is int and 0 <= value <= 10**9} if isinstance(usage, dict) else {}
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        raise ProviderError(f"{error_prefix}_completion_unconfirmed") from None
    return thread_ids[0], usage
