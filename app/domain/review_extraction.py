"""Versioned extraction observations, separate from model input/review coverage."""

from copy import deepcopy
import re
from typing import Any

VERSION = "REVIEW_EXTRACTION_V1"
ERROR = "review_extraction_provenance_invalid"


def _count(value: Any) -> bool:
    return type(value) is int and value >= 0


def validate_provenance(value: Any, *, text_sha256: str, text_chars: int) -> dict[str, Any]:
    try:
        if not isinstance(value, dict) or set(value) != {
            "schema_version", "artifact", "normalization", "decoding", "selection", "pdf_extraction"
        }:
            raise ValueError(ERROR)
        artifact, selection = value["artifact"], value["selection"]
        if (
            value["schema_version"] != VERSION
            or value["normalization"] != "XRML_WHITESPACE_V1"
            or value["decoding"] not in {"UTF8_ERRORS_IGNORE", "PDF_ENGINE"}
            or not isinstance(artifact, dict)
            or set(artifact) != {"kind", "sha256", "bytes"}
            or artifact["kind"] not in {"text", "pages", "pdf_extract"}
            or not isinstance(artifact["sha256"], str)
            or re.fullmatch(r"[a-f0-9]{64}", artifact["sha256"]) is None
            or not _count(artifact["bytes"])
            or artifact["bytes"] == 0
            or not isinstance(selection, dict)
            or set(selection) != {"sha256", "chars", "max_chars", "available_chars", "truncation"}
            or selection["sha256"] != text_sha256
            or not isinstance(text_sha256, str)
            or re.fullmatch(r"[a-f0-9]{64}", text_sha256) is None
            or not _count(selection["chars"])
            or selection["chars"] != text_chars
            or not _count(selection["max_chars"])
            or not 0 < selection["chars"] <= selection["max_chars"]
            or selection["truncation"] not in {"TRUNCATED", "NOT_OBSERVED", "UNKNOWN"}
        ):
            raise ValueError(ERROR)
        available = selection["available_chars"]
        if available is not None:
            if not _count(available) or available < text_chars:
                raise ValueError(ERROR)
            expected = "TRUNCATED" if available > text_chars else "NOT_OBSERVED"
            if selection["truncation"] != expected:
                raise ValueError(ERROR)
        elif selection["truncation"] != "UNKNOWN":
            raise ValueError(ERROR)
        if (artifact["kind"] == "pdf_extract") != (value["decoding"] == "PDF_ENGINE"):
            raise ValueError(ERROR)
        pdf = value["pdf_extraction"]
        if pdf is not None:
            # Import lazily: old records and plain text need no PDF runtime.
            from app.services.pdf_acquisition import validate_extraction_provenance

            validate_extraction_provenance(pdf)
            if artifact["kind"] == "pdf_extract":
                expected_artifact = pdf["source_pdf"]
            else:
                prefix = "text" if artifact["kind"] == "text" else "pages"
                expected_artifact = {"sha256": pdf["outputs"][f"{prefix}_sha256"], "bytes": pdf["outputs"][f"{prefix}_bytes"]}
            if artifact["sha256"] != expected_artifact["sha256"] or artifact["bytes"] != expected_artifact["bytes"]:
                raise ValueError(ERROR)
        return deepcopy(value)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError(ERROR) from None
