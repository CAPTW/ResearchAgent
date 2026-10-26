"""Reserved in-process synthetic transport archive, never a CLI or live proof.

The fixed request uses the existing synthetic provider unchanged. These pure
functions rebuild archived bindings without running that provider, opening a
launcher, reading credentials or granting execution authority.
"""
from typing import Any

from app.domain import prepared_review
from app.domain.journal_exchange import preserved_json
from app.domain.review_offline import ADAPTER, MODEL, VERSION
from app.services.providers.base import ProviderResult

ADAPTER_ID = "xrml-offline-prepared-review"
MANIFEST_SCHEMA = "OFFLINE_PREPARED_REVIEW_TRANSPORT_V1"
TRANSPORT_SCHEMA = "PREPARED_REVIEW_OFFLINE_TRANSPORT_V1"
MODE = "OFFLINE_SYNTHETIC_TRANSPORT"
SURFACE = "offline_synthetic"


def build_plan(contract: dict[str, Any]) -> dict[str, Any]:
    """Freeze only the reserved pure synthetic target; accept no runtime options."""
    try:
        frozen = prepared_review.validate_contract(contract)
        if (frozen["provider_name"] != ADAPTER or frozen["model"] != MODEL
                or frozen["provider_request"]["max_output_tokens"] != 2200):
            raise ValueError()
        return dict(schema_version=MANIFEST_SCHEMA, adapter_id=ADAPTER_ID, adapter_version="1",
            mode=MODE, surface=SURFACE, provider_name=ADAPTER, model=MODEL, provider_adapter_version=VERSION,
            contract_sha256=prepared_review.sha(preserved_json(frozen)), request_sha256=frozen["request_sha256"],
            max_output_tokens=2200, max_review_output_bytes=prepared_review.MAX_OUTPUT_BYTES,
            input_origin="FIXTURE_SYNTHETIC", network_enabled=False, subprocess_enabled=False,
            credential_access=False, automatic_retry=False, output_token_limit_enforced=False,
            scientific_assessment="NOT_ASSESSED", full_document_review="NOT_ESTABLISHED")
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ValueError("paper_offline_configuration_invalid") from None


def _marker(contract: dict[str, Any], raw: str) -> dict[str, Any]:
    manifest = build_plan(contract)
    return dict(schema_version=TRANSPORT_SCHEMA, adapter_id=ADAPTER_ID, adapter_version="1", mode=MODE,
        manifest=manifest, manifest_sha256=prepared_review.sha(preserved_json(manifest)),
        response_sha256=prepared_review.sha(raw), authority="NOT_ESTABLISHED", live_execution="NOT_VERIFIED",
        scientific_assessment="NOT_ASSESSED")


def transport_record(contract: dict[str, Any], response: ProviderResult) -> dict[str, Any]:
    """Retain the normal prepared result with a separate unsigned offline marker."""
    build_plan(contract)
    if (not isinstance(response, ProviderResult) or response.model != MODEL
            or response.response_id is not None or response.usage != {}):
        raise ValueError("paper_offline_transport_invalid")
    record = prepared_review.result(contract, response)
    raw = record["validation"]["prepared_execution"]["response"]["raw_text"]
    record["validation"]["execution"] = _marker(contract, raw)
    return record


def validate_transport(record: dict[str, Any], packet: dict[str, Any]) -> None:
    """Validate the reserved marker on receipt write and read, without execution."""
    try:
        prepared_review.validate_record(record, packet)
        proof = record["validation"]["prepared_execution"]
        response = proof["response"]
        if (response["model"] != MODEL or response["response_id"] is not None or response["usage"] != {}
                or ("input_origin" in record and record["input_origin"] != "FIXTURE_SYNTHETIC")):
            raise ValueError()
        expected = _marker(proof["contract"], response["raw_text"])
        if preserved_json(record["validation"]["execution"]) != preserved_json(expected):
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ValueError("paper_offline_transport_invalid") from None
