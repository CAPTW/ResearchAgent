from __future__ import annotations

import json

from app.services.codex_cli_output_schema import (
    SCHEMA_PROFILE_VERSION,
    build_codex_cli_contract_probe_schema,
    build_codex_cli_report_draft_schema,
    schema_sha256,
)
from app.services.prototype_live_provider import (
    DraftStatement,
    PrototypeProviderDraft,
    validate_quarantined_output,
)


def _walk_objects(node: object):
    if isinstance(node, list):
        for item in node:
            yield from _walk_objects(item)
        return
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            yield node
        for value in node.values():
            yield from _walk_objects(value)


def _walk_schema(node: object, path: str = "$"):
    yield path, node
    if isinstance(node, list):
        for index, item in enumerate(node):
            yield from _walk_schema(item, f"{path}[{index}]")
        return
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk_schema(value, f"{path}.{key}")


def _assert_strict_generation_schema(schema: dict[str, object]) -> None:
    forbidden_paths: dict[str, list[str]] = {"$ref": [], "$defs": [], "const": []}
    for path, node in _walk_schema(schema):
        if not isinstance(node, dict):
            continue
        for keyword in forbidden_paths:
            if keyword in node:
                forbidden_paths[keyword].append(path)
        if node.get("type") != "object" and "properties" not in node:
            continue
        properties = node.get("properties")
        required = node.get("required")
        assert isinstance(properties, dict), f"{path}: properties must be a mapping"
        assert node.get("additionalProperties") is False, (
            f"{path}: additionalProperties must be false"
        )
        assert isinstance(required, list), f"{path}: required must be a list"
        assert len(required) == len(set(required)), f"{path}: required contains duplicates"
        assert set(required) == set(properties), (
            f"{path}: required/property mismatch; required={required!r}, "
            f"properties={list(properties)!r}"
        )
        assert required == list(properties), (
            f"{path}: required order must match property order; "
            f"required={required!r}, properties={list(properties)!r}"
        )
    assert forbidden_paths == {"$ref": [], "$defs": [], "const": []}


def _missing_required_candidate_paths(
    value: object,
    schema: dict[str, object],
    path: str = "$",
) -> list[str]:
    missing: list[str] = []
    properties = schema.get("properties")
    if (schema.get("type") == "object" or properties is not None) and isinstance(value, dict):
        assert isinstance(properties, dict)
        required = schema.get("required")
        assert isinstance(required, list)
        for name in required:
            assert isinstance(name, str)
            if name not in value:
                missing.append(f"{path}.{name}")
        for name, child_schema in properties.items():
            if name in value and isinstance(child_schema, dict):
                missing.extend(
                    _missing_required_candidate_paths(value[name], child_schema, f"{path}.{name}")
                )
    items = schema.get("items")
    if schema.get("type") == "array" and isinstance(value, list) and isinstance(items, dict):
        for index, item in enumerate(value):
            missing.extend(_missing_required_candidate_paths(item, items, f"{path}[{index}]"))
    return missing


def _nullable_paths(schema: dict[str, object]) -> list[str]:
    paths: list[str] = []
    for path, node in _walk_schema(schema):
        if not isinstance(node, dict):
            continue
        node_type = node.get("type")
        if node_type == "null" or (isinstance(node_type, list) and "null" in node_type):
            paths.append(path)
    return paths


def _representative_candidate() -> dict[str, object]:
    first = "paper-a-evidence-1"
    second = "paper-b-evidence-1"
    return {
        "schema_version": "prototype_provider_report_draft_v1",
        "run_id": "strict-schema-run",
        "provider_id": "codex",
        "execution_surface": "codex_sdk_python",
        "stage": "report_draft_assist",
        "internal_output_class": "report_draft",
        "product_output_class": "weekly_report",
        "title": "Strict schema representative Weekly Journal report",
        "executive_summary": "The bounded evidence supports a local synthetic finding.",
        "reviewed_literature_overview": [
            {
                "text": "Paper A reports the bounded observation.",
                "statement_type": "grounded",
                "evidence_ids": [first],
                "strength": "high",
            }
        ],
        "thematic_findings": [
            {
                "text": "A cross-paper interpretation remains an inference.",
                "statement_type": "inference",
                "evidence_ids": [],
                "strength": "medium",
            }
        ],
        "grounded_claims": [
            {
                "claim_text": "Paper B independently records the second observation.",
                "evidence_ids": [second],
                "strength": "high",
                "claim_type": "grounded",
            }
        ],
        "limitations_and_uncertainties": [
            {
                "text": "The synthetic corpus cannot establish field generalization.",
                "statement_type": "uncertainty",
                "evidence_ids": [],
                "strength": "high",
            }
        ],
        "next_research_questions": [
            {
                "text": "Would the finding hold for a bounded public corpus?",
                "statement_type": "proposal",
                "evidence_ids": [],
                "strength": "medium",
            }
        ],
        "referenced_evidence_ids": [first, second],
        "inference_or_proposal_sections": [],
    }


def test_compiled_schema_drops_unsupported_keywords_and_is_deterministic() -> None:
    source = PrototypeProviderDraft.model_json_schema()
    first = build_codex_cli_report_draft_schema(source)
    second = build_codex_cli_report_draft_schema(source)
    third = build_codex_cli_report_draft_schema(source)
    rendered = json.dumps(first, ensure_ascii=False, sort_keys=True)
    serialized = [
        json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        for item in (first, second, third)
    ]
    assert "$ref" not in rendered
    assert "$defs" not in rendered
    assert '"const"' not in rendered
    assert SCHEMA_PROFILE_VERSION.startswith("codex_cli_report_draft_schema_")
    assert first == second == third
    assert serialized[0] == serialized[1] == serialized[2]
    assert schema_sha256(first) == schema_sha256(second) == schema_sha256(third)
    assert first["type"] == "object"
    assert first["additionalProperties"] is False
    assert "run_id" in first["required"]
    assert "schema_version" in first["required"]
    assert first["properties"]["schema_version"]["enum"] == ["prototype_provider_report_draft_v1"]
    for obj in _walk_objects(first):
        assert obj.get("additionalProperties") is False
        if "required" in obj:
            assert isinstance(obj["required"], list)


def test_live_failure_node_requires_every_declared_statement_property() -> None:
    compiled = build_codex_cli_report_draft_schema(PrototypeProviderDraft.model_json_schema())
    statement = compiled["properties"]["reviewed_literature_overview"]["items"]
    assert set(statement["properties"]) == {
        "evidence_ids",
        "statement_type",
        "strength",
        "text",
    }
    assert set(statement["required"]) == set(statement["properties"])
    assert statement["required"].count("evidence_ids") == 1
    assert statement["additionalProperties"] is False


def test_every_compiled_object_satisfies_strict_service_contract() -> None:
    compiled = build_codex_cli_report_draft_schema(PrototypeProviderDraft.model_json_schema())
    _assert_strict_generation_schema(compiled)


def test_strict_object_rule_recurses_through_arrays_anyof_and_oneof() -> None:
    source = {
        "type": "object",
        "properties": {
            "choice": {
                "anyOf": [
                    {
                        "type": "object",
                        "properties": {
                            "selected": {"type": "string"},
                            "defaulted": {"type": "string", "default": "value"},
                        },
                        "required": ["selected"],
                    },
                    {"type": "string"},
                ]
            },
            "variants": {
                "type": "array",
                "items": {
                    "oneOf": [
                        {
                            "type": "object",
                            "properties": {"value": {"type": "integer"}},
                            "required": [],
                        }
                    ]
                },
            },
        },
        "required": ["choice"],
    }
    compiled = build_codex_cli_report_draft_schema(source)
    _assert_strict_generation_schema(compiled)
    assert compiled["required"] == ["choice", "variants"]
    assert compiled["properties"]["choice"]["anyOf"][0]["required"] == (
        ["selected", "defaulted"]
    )
    assert compiled["properties"]["variants"]["items"]["oneOf"][0]["required"] == ["value"]


def test_defaulted_non_nullable_fields_are_explicitly_required_without_null() -> None:
    source = PrototypeProviderDraft.model_json_schema()
    source_statement = source["$defs"]["DraftStatement"]
    canonical_field = DraftStatement.model_fields["evidence_ids"]
    assert canonical_field.is_required() is False
    assert canonical_field.default_factory is list
    assert "evidence_ids" not in source_statement["required"]

    compiled = build_codex_cli_report_draft_schema(source)
    statement = compiled["properties"]["reviewed_literature_overview"]["items"]
    compiled_evidence = statement["properties"]["evidence_ids"]
    assert "evidence_ids" in statement["required"]
    assert compiled_evidence["type"] == "array"
    assert "default" not in compiled_evidence
    assert "null" not in json.dumps(compiled_evidence)


def test_report_draft_candidate_model_currently_has_no_nullable_fields() -> None:
    assert _nullable_paths(PrototypeProviderDraft.model_json_schema()) == []


def test_representative_candidate_passes_canonical_and_evidence_validation() -> None:
    compiled = build_codex_cli_report_draft_schema(PrototypeProviderDraft.model_json_schema())
    _assert_strict_generation_schema(compiled)
    candidate = _representative_candidate()
    validated = PrototypeProviderDraft.model_validate(candidate)
    assert validated.model_dump() == candidate

    validation = validate_quarantined_output(
        raw_output=json.dumps(candidate, ensure_ascii=False).encode("utf-8"),
        run_id="strict-schema-run",
        evidence_ids=["paper-a-evidence-1", "paper-b-evidence-1"],
        paper_ids=["paper-a", "paper-b"],
    )
    assert validation["valid"] is True
    assert validation["schema_valid"] is True
    assert validation["evidence_valid"] is True


def test_compiled_contract_rejects_omitted_defaulted_evidence_ids() -> None:
    candidate = _representative_candidate()
    del candidate["thematic_findings"][0]["evidence_ids"]
    compiled = build_codex_cli_report_draft_schema(PrototypeProviderDraft.model_json_schema())
    assert _missing_required_candidate_paths(candidate, compiled) == (
        ["$.thematic_findings[0].evidence_ids"]
    )


def test_compiled_schema_keeps_canonical_field_names() -> None:
    compiled = build_codex_cli_report_draft_schema(PrototypeProviderDraft.model_json_schema())
    required = set(compiled["required"])
    for field in (
        "schema_version",
        "run_id",
        "provider_id",
        "execution_surface",
        "stage",
        "title",
        "grounded_claims",
        "referenced_evidence_ids",
    ):
        assert field in required
        assert field in compiled["properties"]


def test_contract_probe_schema_is_minimal() -> None:
    schema = build_codex_cli_contract_probe_schema()
    assert schema == {
        "additionalProperties": False,
        "properties": {"status": {"enum": ["ok"], "type": "string"}},
        "required": ["status"],
        "type": "object",
    }
