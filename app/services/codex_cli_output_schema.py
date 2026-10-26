"""Generation-only JSON Schema compiler for Codex CLI `--output-schema`.

The canonical Pydantic candidate model remains the application validator.
This compiler only produces a simpler, deterministic constraint for `codex exec`.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

SCHEMA_PROFILE_VERSION = "codex_cli_report_draft_schema_v1"
DROP_KEYS = {
    "$schema",
    "$defs",
    "const",
    "default",
    "description",
    "examples",
    "format",
    "maxItems",
    "maxLength",
    "minItems",
    "minLength",
    "pattern",
    "title",
}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def schema_sha256(schema: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(dict(schema))).hexdigest()


def build_codex_cli_contract_probe_schema() -> dict[str, Any]:
    return {
        "additionalProperties": False,
        "properties": {
            "status": {
                "enum": ["ok"],
                "type": "string",
            }
        },
        "required": ["status"],
        "type": "object",
    }


def _defs(source: Mapping[str, Any]) -> dict[str, Any]:
    raw = source.get("$defs")
    return dict(raw) if isinstance(raw, dict) else {}


def _compile_node(node: Any, defs: Mapping[str, Any], stack: tuple[str, ...]) -> Any:
    if isinstance(node, list):
        return [_compile_node(item, defs, stack) for item in node]
    if not isinstance(node, dict):
        return node

    ref = node.get("$ref")
    if isinstance(ref, str):
        if not ref.startswith("#/$defs/"):
            raise ValueError(f"unsupported_ref:{ref}")
        name = ref[len("#/$defs/") :]
        if name in stack:
            raise ValueError("recursive_schema_definition")
        target = defs.get(name)
        if not isinstance(target, dict):
            raise ValueError(f"missing_def:{name}")
        return _compile_node(target, defs, (*stack, name))

    compiled: dict[str, Any] = {}
    if "const" in node:
        compiled["enum"] = [node["const"]]
        if "type" in node:
            compiled["type"] = node["type"]
    else:
        for key, value in node.items():
            if key in DROP_KEYS:
                continue
            if key == "properties" and isinstance(value, dict):
                compiled[key] = {
                    str(name): _compile_node(item, defs, stack) for name, item in value.items()
                }
                continue
            compiled[key] = _compile_node(value, defs, stack)

    if compiled.get("type") == "object" or "properties" in compiled:
        compiled["type"] = "object"
        compiled["additionalProperties"] = False
        properties = compiled.get("properties")
        if properties is None:
            compiled_properties: dict[str, Any] = {}
        elif isinstance(properties, dict):
            compiled_properties = {
                str(name): _compile_node(value, defs, stack) for name, value in properties.items()
            }
        else:
            raise ValueError("compiled_object_properties_must_be_mapping")
        compiled["properties"] = compiled_properties
        compiled["required"] = list(compiled_properties)

    if compiled.get("type") == "array" and "items" in compiled:
        compiled["items"] = _compile_node(compiled["items"], defs, stack)
    return compiled


def _assert_compiled_shape(schema: Mapping[str, Any]) -> None:
    rendered = json.dumps(schema, ensure_ascii=False)
    if "$ref" in rendered or "$defs" in rendered or '"const"' in rendered:
        raise ValueError("compiled_schema_contains_unsupported_keyword")
    if schema.get("type") != "object":
        raise ValueError("compiled_schema_root_must_be_object")

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        if node.get("type") == "object" or "properties" in node:
            if node.get("additionalProperties") is not False:
                raise ValueError("compiled_object_missing_additionalProperties_false")
            properties = node.get("properties")
            if not isinstance(properties, dict):
                raise ValueError("compiled_object_properties_must_be_mapping")
            required = node.get("required")
            if not isinstance(required, list):
                raise ValueError("compiled_required_must_be_array")
            if len(required) != len(set(required)):
                raise ValueError("compiled_required_contains_duplicates")
            if required != list(properties):
                raise ValueError("compiled_required_must_match_properties")
        for value in node.values():
            walk(value)

    walk(schema)


def build_codex_cli_report_draft_schema(source_schema: Mapping[str, Any]) -> dict[str, Any]:
    compiled = _compile_node(dict(source_schema), _defs(source_schema), ())
    if not isinstance(compiled, dict):
        raise ValueError("compiled_schema_invalid")
    _assert_compiled_shape(compiled)
    return compiled
