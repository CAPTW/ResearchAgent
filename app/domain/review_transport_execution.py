"""Versioned paper transport execution with exact CLI, HTTP and offline variants."""
from typing import Any, Literal

from pydantic import Field

from app.domain import prepared_review
from app.domain.review_execution import Prepare as OfflinePrepare, Scope
from app.domain.review_preparation import encoded, sha
from app.domain.review_range_input import build_packet
from app.services.providers import codex_prepared_review_plan as cli, offline_prepared_review_plan as offline, openai_prepared_review_plan as http
from app.services.providers import codex_prepared_review_plan_v2 as cli_v2
from app.services.providers import xai_prepared_review as xai
from app.services.providers import anthropic_prepared_review as anthropic
from app.services.providers import claude_prepared_review_plan as claude
from app.services.providers import antigravity_prepared_review_plan as antigravity
from app.services.providers import grok_prepared_review_plan as grok

SCHEMA = "PAPER_REVIEW_TRANSPORT_EXECUTION_V1"
EVENT_SCHEMA = "PAPER_REVIEW_TRANSPORT_EVENT_V1"
# Terminal reasons shared by event validation and persistence. Unknown transport
# errors still require an explicit acknowledgement before another attempt.
KNOWN_FAILURE_REASONS = frozenset({"provider_failed", "provider_incomplete", "provider_cancelled", "provider_refused", "output_invalid"})


class Prepare(OfflinePrepare):
    provider_name: Literal["codex_cli", "claude_code", "antigravity_cli", "grok_cli", "xrml_offline_review", "openai", "xai", "anthropic"] = "codex_cli"
    model: str = Field(min_length=1, max_length=200)
    max_output_tokens: int = Field(default=2200, ge=256, le=8000)


def identity(scope: Scope, request: str) -> str:
    return "rtransport_" + sha(encoded([SCHEMA, scope.topic, scope.version, scope.actor, request]))


def build_manifest(prepared: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    """Rebuild an explicit known variant; no discovery or caller-selected adapter."""
    try:
        if manifest["schema_version"] == cli.MANIFEST_SCHEMA:
            _, _, expected = cli.build_plan(prepared, runtime_identity=manifest["runtime_identity"],
                reasoning_effort=manifest["reasoning_effort"], timeout_seconds=manifest["timeout_seconds"])
        elif manifest["schema_version"] == cli_v2.MANIFEST_SCHEMA:
            _, _, expected = cli_v2.build_plan(prepared, runtime_identity=manifest["runtime_identity"],
                reasoning_effort=manifest["reasoning_effort"], timeout_seconds=manifest["timeout_seconds"])
        elif manifest["schema_version"] == offline.MANIFEST_SCHEMA:
            expected = offline.build_plan(prepared)
        elif manifest["schema_version"] == http.MANIFEST_SCHEMA:
            _, _, expected = http.build_plan(prepared, endpoint=manifest["endpoint"], timeout_seconds=manifest["timeout_seconds"])
        elif manifest["schema_version"] == xai.MANIFEST_SCHEMA:
            _, _, expected = xai.build_plan(prepared, timeout_seconds=manifest["timeout_seconds"])
        elif manifest["schema_version"] == anthropic.MANIFEST_SCHEMA:
            _, _, expected = anthropic.build_plan(prepared, timeout_seconds=manifest["timeout_seconds"])
        elif manifest["schema_version"] == anthropic.MANIFEST_SCHEMA_V2:
            options = {"credential_revision": manifest["credential_binding"]["revision"]} if "credential_binding" in manifest else {}
            _, _, expected = anthropic.build_plan(prepared, timeout_seconds=manifest["timeout_seconds"],
                profile=anthropic.MANIFEST_SCHEMA_V2, execution_surface=manifest["execution_surface"], **options)
        elif manifest["schema_version"] == claude.MANIFEST_SCHEMA:
            _, _, expected = claude.build_plan(prepared, runtime_identity=manifest["runtime_identity"],
                timeout_seconds=manifest["timeout_seconds"])
        elif manifest["schema_version"] == antigravity.MANIFEST_SCHEMA:
            _, _, expected = antigravity.build_plan(prepared, runtime_identity=manifest["runtime_identity"],
                timeout_seconds=manifest["timeout_seconds"])
        elif manifest["schema_version"] == grok.MANIFEST_SCHEMA:
            _, expected = grok.build_plan(prepared, runtime_identity=manifest["runtime_identity"],
                timeout_seconds=manifest["timeout_seconds"])
        else:
            raise ValueError()
        if encoded(manifest) != encoded(expected):
            raise ValueError()
        return expected
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ValueError("review_execution_transport_mismatch") from None


def contract(scope: Scope, body: Prepare, preparation: dict[str, Any], at: str,
             manifest: dict[str, Any], origin: str, *, prepared_schema: str | None = None) -> dict[str, Any]:
    if origin not in {"FIXTURE_SYNTHETIC", "PROVIDER_RESPONSE"}:
        raise ValueError("review_execution_origin_invalid")
    if body.expected_preparation_digest != sha(encoded(preparation)):
        raise ValueError("review_execution_preparation_changed")
    frozen = prepared_review.prepare(build_packet(preparation), provider_name=body.provider_name,
        model=body.model, max_output_tokens=body.max_output_tokens, schema_version=prepared_schema)
    expected = build_manifest(frozen, manifest)
    anthropic.validate_origin(expected, origin)
    is_offline = expected["schema_version"] == offline.MANIFEST_SCHEMA
    if is_offline and origin != "FIXTURE_SYNTHETIC":
        raise ValueError("review_execution_origin_invalid")
    mode = offline.MODE if is_offline else http.MODE if expected["schema_version"] in {http.MANIFEST_SCHEMA, xai.MANIFEST_SCHEMA, anthropic.MANIFEST_SCHEMA, anthropic.MANIFEST_SCHEMA_V2} else "TRUSTED_PAPER_CLI"
    return dict(schema_version=SCHEMA, execution_id=identity(scope, body.request_id), topic_id=scope.topic,
        paper_version_id=scope.version, preparation_id=scope.preparation, actor_id=scope.actor,
        created_at=at, mode=mode, specification=body.model_dump(), prepared=frozen,
        transport=expected, input_origin=origin)


def validate_record(value: dict[str, Any], record: dict[str, Any]) -> None:
    if set(record) != {"topic_id", "run_id", "paper_id", "source_text_sha256", "review_level", "status", "draft", "validation"}:
        raise ValueError("review_execution_integrity_invalid")
    prepared_review.validate_record(record, value["prepared"]["packet"])
    manifest = build_manifest(value["prepared"], value["transport"])
    if manifest["schema_version"] == anthropic.MANIFEST_SCHEMA_V2:
        anthropic.validate_origin(manifest, value["input_origin"])
    if manifest["schema_version"] == offline.MANIFEST_SCHEMA:
        offline.validate_transport(record, value["prepared"]["packet"])
    elif manifest["schema_version"] == http.MANIFEST_SCHEMA:
        http.validate_transport(record, value["prepared"]["packet"])
    elif manifest["schema_version"] == xai.MANIFEST_SCHEMA:
        xai.validate_transport(record, value["prepared"]["packet"])
    elif manifest["schema_version"] in {anthropic.MANIFEST_SCHEMA, anthropic.MANIFEST_SCHEMA_V2}:
        anthropic.validate_transport(record, value["prepared"]["packet"])
    elif manifest["schema_version"] == claude.MANIFEST_SCHEMA:
        claude.validate_transport(record, value["prepared"]["packet"])
    elif manifest["schema_version"] == antigravity.MANIFEST_SCHEMA:
        antigravity.validate_transport(record, value["prepared"]["packet"])
    elif manifest["schema_version"] == grok.MANIFEST_SCHEMA:
        grok.validate_transport(record, value["prepared"]["packet"])
    elif manifest["schema_version"] == cli_v2.MANIFEST_SCHEMA:
        cli_v2.validate_transport(record, value["prepared"]["packet"])
    else:
        cli.validate_transport(record, value["prepared"]["packet"])
    if (encoded(record["validation"]["prepared_execution"]["contract"]) != encoded(value["prepared"])
            or encoded(record["validation"]["execution"]["manifest"]) != encoded(value["transport"])):
        raise ValueError("review_execution_integrity_invalid")
