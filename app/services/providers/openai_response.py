"""Non-streaming Responses JSON protocol, independent of authority or activation.

Only an explicit completed response can yield output. Provider-reported terminal
failure is distinct from an unknown/nonterminal response. Never echo remote error
text into local diagnostics. Validating this envelope is not live model proof.
"""
import json
import math
from typing import Any

import httpx

from app.services.providers.base import ProviderError, ProviderOutputError, ProviderRefusalError, ProviderReportedFailure, ProviderResult

# Bounds the decoded HTTP body, including the envelope/JSON escaping. Journal
# acceptance additionally retains its existing 512,000-byte proposal limit.
MAX_RESPONSE_BYTES = 4_000_000


def read_response(response: httpx.Response) -> ProviderResult:
    if response.status_code != 200:
        raise ProviderError(f"OpenAI provider HTTP status {response.status_code}.")
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes(chunk_size=65536):
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise ProviderError("OpenAI response exceeds the local size limit.")
        chunks.append(chunk)
    return parse_response(b"".join(chunks))


def _object(raw: str) -> dict[str, Any]:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    def finite(text):
        value = float(text)
        if not math.isfinite(value):
            raise ValueError("nonfinite_json")
        return value

    value = json.loads(raw, object_pairs_hook=unique, parse_constant=finite, parse_float=finite)
    if not isinstance(value, dict):
        raise ValueError("object_required")
    # Reject escaped lone surrogates as well as malformed literal UTF-8.
    json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return value


def parse_response(raw: bytes) -> ProviderResult:
    try:
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("response_too_large")
        body = _object(raw.decode("utf-8"))
    except (ValueError, TypeError, RecursionError):
        raise ProviderError("OpenAI response envelope is invalid or exceeds the local size limit.") from None
    # A terminal word alone cannot identify the remote outcome. Validate the
    # existing reported identity contract before any known-failure classification.
    # Do not trim or repair identifiers: an unidentifiable response must retain
    # unknown-outcome recovery even when its terminal status looks plausible.
    model, identity = body.get("model"), body.get("id")
    if (not isinstance(model, str) or not isinstance(identity, str)
            or any(not value or len(value) > 200 or value != value.strip()
                   or any(ord(char) < 32 or ord(char) == 127 for char in value)
                   for value in (model, identity))):
        raise ProviderError("OpenAI response identity could not be confirmed.")
    status = body.get("status")
    if status in ("incomplete", "failed", "cancelled"):
        raise ProviderReportedFailure(status)
    if status != "completed" or body.get("error") is not None or body.get("incomplete_details") is not None:
        raise ProviderError("OpenAI response completion could not be confirmed.")
    try:
        usage = body.get("usage")
        if usage is not None and not isinstance(usage, dict):
            raise ValueError("usage_invalid")
        output = body["output"]
        if not isinstance(output, list):
            raise ValueError("output_invalid")
        parts: list[str] = []
        refused = False
        for item in output:
            if not isinstance(item, dict):
                raise ValueError("output_invalid")
            if item.get("type") == "reasoning":
                continue
            if (item.get("type") != "message" or item.get("status") != "completed" or item.get("role") != "assistant"
                    or not isinstance(item.get("content"), list)):
                raise ValueError("message_invalid")
            for content in item["content"]:
                if not isinstance(content, dict):
                    raise ValueError("content_invalid")
                if content.get("type") == "refusal" and isinstance(content.get("refusal"), str):
                    refused = True
                elif content.get("type") == "output_text" and isinstance(content.get("text"), str):
                    parts.append(content["text"])
                else:
                    raise ValueError("content_invalid")
        if refused:
            raise ProviderRefusalError("OpenAI refused the structured-output request.")
        text = "".join(parts)
        payload = _object(text)
    except (ValueError, KeyError, TypeError, RecursionError):
        raise ProviderOutputError("OpenAI completed response contains invalid structured output.") from None
    return ProviderResult(provider_name="openai", model=model, response_id=identity, payload=payload, usage=usage or {}, raw_text=text)
