"""Local conservative input budgets, not tokenizer, billing or execution proof."""
from copy import deepcopy
import json
import re
from typing import Any

ESTIMATOR = "UTF8_BYTES_CONSERVATIVE_V1"
ANALYSIS_UNIT_SCHEMA = "PAPER_ANALYSIS_UNIT_V1"
FRAMING_TOKENS = 1024
MAX_CONTEXT_BYTES = 4096
MAX_UNITS = 256
_PROVIDER = re.compile(r"[a-z0-9][a-z0-9_-]{0,99}")
_HASH = re.compile(r"[a-f0-9]{64}")
_PLAN = re.compile(r"rplan_[a-f0-9]{64}")


def _encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _identity(value: Any, limit: int) -> bool:
    return (isinstance(value, str) and 1 <= len(value) <= limit and value == value.strip()
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def validate_budget(value: Any) -> dict[str, Any]:
    """Validate an explicit caller-selected budget without selecting a model."""
    try:
        if not isinstance(value, dict) or set(value) != {
            "provider_name", "model", "context_tokens", "input_tokens", "max_output_tokens", "safety_tokens", "estimator",
        }:
            raise ValueError()
        if (not isinstance(value["provider_name"], str) or _PROVIDER.fullmatch(value["provider_name"]) is None
                or not _identity(value["model"], 200) or value["estimator"] != ESTIMATOR
                or any(type(value[key]) is not int for key in (
                    "context_tokens", "input_tokens", "max_output_tokens", "safety_tokens"))
                or not 1 <= value["context_tokens"] <= 10**9 or not 1 <= value["input_tokens"] <= 10**9
                or not 256 <= value["max_output_tokens"] <= 8000 or not 0 <= value["safety_tokens"] <= 10**9
                or value["input_tokens"] + value["max_output_tokens"] + value["safety_tokens"] > value["context_tokens"]):
            raise ValueError()
        _encoded(value).encode("utf-8", errors="strict")
        return deepcopy(value)
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        raise ValueError("review_token_budget_invalid") from None


def estimate_input_tokens(request: dict[str, Any]) -> int:
    """Count one token per UTF-8 byte plus fixed framing and two schema copies.

    This local planning bound deliberately overcounts common text. It is not a
    provider tokenizer result or a guarantee about hidden/provider-side context.
    """
    try:
        instructions, prompt, schema = request["instructions"], request["prompt"], request["schema"]
        if not isinstance(instructions, str) or not isinstance(prompt, str) or not isinstance(schema, dict):
            raise ValueError()
        return (len(instructions.encode("utf-8", errors="strict")) + len(prompt.encode("utf-8", errors="strict"))
                + 2 * len(_encoded(schema).encode("utf-8", errors="strict")) + FRAMING_TOKENS)
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        raise ValueError("review_token_budget_invalid") from None


def validate_analysis_unit(value: Any, *, document_chars: int | None = None) -> dict[str, Any]:
    """Check frozen planning metadata; outline and neighboring text are not evidence."""
    try:
        if not isinstance(value, dict) or set(value) != {
            "schema_version", "plan_id", "plan_sha256", "unit_index", "unit_count", "label", "document_chars",
            "outline", "context_before", "context_after", "budget",
        }:
            raise ValueError()
        if (value["schema_version"] != ANALYSIS_UNIT_SCHEMA or not isinstance(value["plan_id"], str)
                or _PLAN.fullmatch(value["plan_id"]) is None or not isinstance(value["plan_sha256"], str)
                or _HASH.fullmatch(value["plan_sha256"]) is None
                or type(value["unit_index"]) is not int or type(value["unit_count"]) is not int
                or not 0 <= value["unit_index"] < value["unit_count"] <= MAX_UNITS
                or not isinstance(value["label"], str) or not value["label"].strip() or len(value["label"]) > 500
                or type(value["document_chars"]) is not int or not 1 <= value["document_chars"] <= 1_000_000
                or (document_chars is not None and value["document_chars"] != document_chars)
                or not isinstance(value["outline"], list) or len(value["outline"]) > MAX_UNITS):
            raise ValueError()
        for item in value["outline"]:
            if (not isinstance(item, dict) or set(item) != {"label", "start", "end"}
                    or not isinstance(item["label"], str) or not item["label"].strip() or len(item["label"]) > 500
                    or type(item["start"]) is not int or type(item["end"]) is not int
                    or not 0 <= item["start"] < item["end"] <= value["document_chars"]):
                raise ValueError()
        for field in ("context_before", "context_after"):
            if not isinstance(value[field], str) or len(value[field].encode("utf-8", errors="strict")) > MAX_CONTEXT_BYTES:
                raise ValueError()
        validate_budget(value["budget"])
        _encoded(value).encode("utf-8", errors="strict")
        return deepcopy(value)
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        raise ValueError("review_analysis_unit_invalid") from None
