"""Bounded native Messages wire; no SDK, account discovery or retry.

ANTHROPIC_JSON_SCHEMA_V1 compiles only our explicit schema subset. Unsupported
bounds become descriptions; original local schema validation stays mandatory.
https://platform.claude.com/docs/en/build-with-claude/structured-outputs
"""
from copy import deepcopy
from dataclasses import dataclass
import json
import math
from typing import Any, Callable

import httpx

from app.domain.journal_exchange import preserved_json
from app.services.providers.base import ProviderError, ProviderOutputError, ProviderRefusalError, ProviderReportedFailure, ProviderRequest, ProviderResult

ENDPOINT = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
SCHEMA_COMPILER = "ANTHROPIC_JSON_SCHEMA_V1"
SCHEMA_COMPILER_V2 = "ANTHROPIC_JSON_SCHEMA_V2"
MAX_REQUEST_BYTES = 4_000_000
MAX_RESPONSE_BYTES = 4_000_000
USAGE_KEYS = {"input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"}


def compile_schema(schema: dict[str, Any], *, compiler: str = SCHEMA_COMPILER) -> dict[str, Any]:
    """Versioned, context-aware compiler, not a recursive key deletion pass."""
    bounds = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength", "maxLength", "maxItems"}
    allowed = bounds | {"type", "title", "description", "default", "enum", "const", "pattern", "minItems", "properties",
                        "required", "additionalProperties", "items", "$defs", "$ref", "anyOf", "allOf"}
    if compiler not in {SCHEMA_COMPILER, SCHEMA_COMPILER_V2}:
        raise ValueError("anthropic_schema_invalid")
    if compiler == SCHEMA_COMPILER_V2:
        # Regexes can expand the remote grammar even in flat, required-only
        # schemas. Describe them on the wire; enforce the unchanged local schema.
        bounds = bounds | {"pattern"}
    try:
        original = json.loads(preserved_json(schema))
        if not isinstance(original, dict):
            raise ValueError()

        def node(value: Any, active: tuple[str, ...] = ()) -> dict[str, Any]:
            if not isinstance(value, dict) or set(value) - allowed:
                raise ValueError()
            result = deepcopy(value)
            removed = {key: result.pop(key) for key in sorted(bounds & set(result))}
            if "minItems" in result and (type(result["minItems"]) is not int or result["minItems"] not in {0, 1}):
                if type(result["minItems"]) is not int or result["minItems"] < 0:
                    raise ValueError()
                removed["minItems"] = result.pop("minItems")
            if removed:
                if not isinstance(result.get("description", ""), str):
                    raise ValueError()
                result["description"] = (result.get("description", "") + "\nLocal validation constraints: " + preserved_json(removed)).lstrip("\n")
            if "$ref" in result:
                reference = result["$ref"]
                if (not isinstance(reference, str) or not reference.startswith("#/$defs/")
                        or reference in active or reference.count("/") != 2):
                    raise ValueError()
                node(original["$defs"][reference.removeprefix("#/$defs/")], (*active, reference))
            if result.get("type") == "object" or "properties" in result:
                if result.get("additionalProperties", False) is not False:
                    raise ValueError()
                result["additionalProperties"] = False
            for key in ("properties", "$defs"):
                if key in result:
                    if not isinstance(result[key], dict):
                        raise ValueError()
                    result[key] = {name: node(child, active) for name, child in result[key].items()}
            if "items" in result:
                result["items"] = node(result["items"], active)
            for key in ("anyOf", "allOf"):
                if key in result:
                    if not isinstance(result[key], list) or not result[key]:
                        raise ValueError()
                    if key == "allOf" and any("$ref" in child for child in result[key]):
                        raise ValueError()
                    result[key] = [node(child, active) for child in result[key]]
            if "enum" in result and (not isinstance(result["enum"], list) or not result["enum"]
                                      or any(isinstance(item, (list, dict)) for item in result["enum"])):
                raise ValueError()
            return result

        return node(original)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        raise ValueError("anthropic_schema_invalid") from None


def message_payload(request: ProviderRequest, model: str) -> dict[str, Any]:
    # Newer Messages models reject non-default sampling temperatures. Preserve
    # the frozen local request, but omit this deprecated wire parameter.
    return dict(model=model, max_tokens=request.max_output_tokens,
        system=request.instructions, messages=[dict(role="user", content=request.prompt)],
        output_config=dict(format=dict(type="json_schema", schema=compile_schema(request.schema))), stream=False, tools=[])


def rejection_diagnostic(status: int | None, raw: bytes) -> str | None:
    """Advisory classification of a saved rejection, never arbitrary error text."""
    try:
        if len(raw) > MAX_RESPONSE_BYTES:
            return None
        body = _object(raw.decode("utf-8", errors="strict"))
        error = body["error"]
        if body.get("type") != "error" or not isinstance(error, dict):
            return None
        kind, message = error.get("type"), error.get("message")
        if status == 400 and kind == "invalid_request_error" and isinstance(message, str):
            if message in {"Schema is too complex.", "Schema is too complex for compilation."}:
                return "SCHEMA_COMPLEXITY"
            if message.startswith("Your credit balance is too low to access the Anthropic API."):
                return "INSUFFICIENT_CREDITS"
        if status == 402 and kind == "billing_error":
            return "BILLING_ERROR"
        if status == 401 and kind == "authentication_error":
            return "AUTHENTICATION_ERROR"
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        pass
    return None


def _object(raw: str) -> dict[str, Any]:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    def finite(text):
        value = float(text)
        if not math.isfinite(value):
            raise ValueError()
        return value

    value = json.loads(raw, object_pairs_hook=unique, parse_constant=finite, parse_float=finite)
    if not isinstance(value, dict):
        raise ValueError()
    preserved_json(value).encode("utf-8", errors="strict")
    return value


@dataclass(frozen=True)
class FinalTextResponse:
    """Envelope inspection only; deliberately not a structured ProviderResult."""
    model: str
    response_id: str
    text: str
    usage: dict[str, int]


def parse_text_response(raw: bytes, *, max_output_bytes: int = 512_000) -> FinalTextResponse:
    try:
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError()
        body = _object(raw.decode("utf-8", errors="strict"))
    except (ValueError, TypeError, AttributeError, RecursionError, OverflowError):
        raise ProviderError("anthropic_response_outcome_unknown") from None
    model, identity = body.get("model"), body.get("id")
    if (not isinstance(model, str) or not isinstance(identity, str)
            or any(not value or len(value) > 200 or value != value.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in value) for value in (model, identity))
            or body.get("type") != "message" or body.get("role") != "assistant"
            or body.get("error") is not None or body.get("stop_sequence") is not None):
        raise ProviderError("anthropic_response_outcome_unknown")
    stop = body.get("stop_reason")
    if not isinstance(stop, str) or stop not in {"end_turn", "max_tokens", "refusal", "model_context_window_exceeded"}:
        raise ProviderError("anthropic_response_outcome_unknown")
    if stop == "refusal":
        raise ProviderRefusalError("anthropic_provider_refused")
    if stop in {"max_tokens", "model_context_window_exceeded"}:
        raise ProviderReportedFailure("incomplete")
    # A contradictory terminal envelope is not a known successful completion.
    if body.get("stop_details") is not None:
        raise ProviderError("anthropic_response_outcome_unknown")
    try:
        content = body["content"]
        if not isinstance(content, list) or not content:
            raise ValueError()
        parts = []
        for block in content:
            if not isinstance(block, dict):
                raise ValueError()
            # Opus 5.5 always uses thinking; its display text can be empty.
            # This one-turn, tool-free adapter archives final JSON only, not
            # opaque signatures or thinking as evidence. The full envelope
            # remains bounded above even though these blocks are discarded.
            if block.get("type") == "thinking":
                if (set(block) != {"type", "thinking", "signature"}
                        or not isinstance(block["thinking"], str) or not isinstance(block["signature"], str)):
                    raise ValueError()
                continue
            if block.get("type") == "redacted_thinking":
                if set(block) != {"type", "data"} or not isinstance(block["data"], str):
                    raise ValueError()
                continue
            if (set(block) - {"type", "text", "citations"}
                    or block.get("type") != "text" or not isinstance(block.get("text"), str)
                    or block.get("citations") not in (None, [])):
                raise ValueError()
            parts.append(block["text"])
        text = "".join(parts)
        if not 1 <= len(text.encode("utf-8")) <= max_output_bytes:
            raise ValueError()
        source_usage = body.get("usage", {})
        if not isinstance(source_usage, dict):
            raise ValueError()
        usage = {}
        for key in USAGE_KEYS & set(source_usage):
            count = source_usage[key]
            if type(count) is not int or not 0 <= count <= 10**9:
                raise ValueError()
            usage[key] = count
        # Input is the API's uncached count. Cache counters remain separate;
        # neither input total nor total_tokens is fabricated by the adapter.
        if source_usage.get("server_tool_use") not in (None, {}):
            activity = source_usage["server_tool_use"]
            if (not isinstance(activity, dict) or set(activity) - {"web_fetch_requests", "web_search_requests"}
                    or any(type(value) is not int or value != 0 for value in activity.values())):
                raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError, OverflowError):
        raise ProviderOutputError("anthropic_output_invalid") from None
    return FinalTextResponse(model, identity, text, usage)


def parse_response(raw: bytes, *, max_output_bytes: int = 512_000) -> ProviderResult:
    captured = parse_text_response(raw, max_output_bytes=max_output_bytes)
    try:
        payload = _object(captured.text)
    except (ValueError, TypeError, AttributeError, RecursionError, OverflowError):
        raise ProviderOutputError("anthropic_output_invalid") from None
    return ProviderResult("anthropic", captured.model, captured.response_id, payload,
        usage=captured.usage, raw_text=captured.text)


def read_response(response: httpx.Response, *, max_output_bytes: int = 512_000) -> ProviderResult:
    if response.status_code != 200:
        raise ProviderError("anthropic_http_outcome_unknown")
    size = 0
    chunks = []
    for chunk in response.iter_bytes(chunk_size=65536):
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise ProviderError("anthropic_response_outcome_unknown")
        chunks.append(chunk)
    return parse_response(b"".join(chunks), max_output_bytes=max_output_bytes)


def send(body: bytes, *, api_key: str, timeout_seconds: float, transport: httpx.BaseTransport | None,
         max_output_bytes: int, before_send: Callable[[], None] | None = None) -> ProviderResult:
    headers = {"x-api-key": api_key, "anthropic-version": API_VERSION, "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=timeout_seconds, transport=transport, follow_redirects=False, trust_env=False) as client:
            if before_send is not None:
                before_send()
            with client.stream("POST", ENDPOINT, headers=headers, content=body) as response:
                return read_response(response, max_output_bytes=max_output_bytes)
    except httpx.HTTPError:
        raise ProviderError("anthropic_transport_outcome_unknown") from None
