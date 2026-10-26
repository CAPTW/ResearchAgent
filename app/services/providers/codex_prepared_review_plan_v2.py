"""Unsigned V2 paper transport archive; V1 readers and manifests stay unchanged."""
from typing import Any

from app.domain import prepared_review
from app.domain.journal_exchange import preserved_json
from app.services.providers.base import ProviderResult
from app.services.providers.codex_cli_profile_v2 import argv, profile_metadata
from app.services.providers.codex_prepared_review_plan import ADAPTER_ID, build_plan as build_v1

MANIFEST_SCHEMA = "CODEX_PREPARED_REVIEW_CLI_V2"
TRANSPORT_SCHEMA = "PREPARED_REVIEW_CLI_TRANSPORT_V2"
ADAPTER_VERSION = "2"


def build_plan(contract: dict[str, Any], *, runtime_identity: dict[str, Any], reasoning_effort: str,
               timeout_seconds: float) -> tuple[str, str, dict[str, Any]]:
    stdin, schema, manifest = build_v1(contract, runtime_identity=runtime_identity,
        reasoning_effort=reasoning_effort, timeout_seconds=timeout_seconds)
    manifest.update(profile_metadata())
    manifest.update(schema_version=MANIFEST_SCHEMA, adapter_version=ADAPTER_VERSION,
        argv_shape=argv(("{explicit_launcher}",), "{attempt_dir}", manifest["model"], reasoning_effort))
    return stdin, schema, manifest


def _marker(contract: dict[str, Any], raw: str, manifest: dict[str, Any]) -> dict[str, Any]:
    _, _, expected = build_plan(contract, runtime_identity=manifest["runtime_identity"],
        reasoning_effort=manifest["reasoning_effort"], timeout_seconds=manifest["timeout_seconds"])
    if preserved_json(manifest) != preserved_json(expected):
        raise ValueError("paper_codex_transport_invalid")
    return dict(schema_version=TRANSPORT_SCHEMA, adapter_id=ADAPTER_ID, adapter_version=ADAPTER_VERSION, manifest=expected,
                manifest_sha256=prepared_review.sha(preserved_json(expected)), response_sha256=prepared_review.sha(raw),
                authority="NOT_ESTABLISHED", live_execution="NOT_VERIFIED")


def transport_record(contract: dict[str, Any], response: ProviderResult, manifest: dict[str, Any]) -> dict[str, Any]:
    record = prepared_review.result(contract, response)
    raw = record["validation"]["prepared_execution"]["response"]["raw_text"]
    record["validation"]["execution"] = _marker(contract, raw, manifest)
    return record


def validate_transport(record: dict[str, Any], packet: dict[str, Any]) -> None:
    try:
        prepared_review.validate_record(record, packet)
        validation = record["validation"]
        proof = validation["prepared_execution"]
        marker = validation["execution"]
        expected = _marker(proof["contract"], proof["response"]["raw_text"], marker["manifest"])
        if preserved_json(marker) != preserved_json(expected):
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ValueError("paper_codex_transport_invalid") from None
