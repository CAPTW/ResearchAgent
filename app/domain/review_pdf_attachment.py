"""User binding is provenance, not proof that a PDF matches a paper."""
from copy import deepcopy
from datetime import datetime
import re
from typing import Any

from app.domain.review_pdf_extraction import MAX_PDF_BYTES, encoded, sha

SCHEMA = "PAPER_PDF_ATTACHMENT_V1"
ERROR = "review_pdf_attachment_integrity_invalid"
MAX_RECORDS = 200


def target_id(topic: str, version: str, run: str, paper: str) -> str:
    return "pdft_" + sha(encoded([SCHEMA, topic, version, run, paper]))


def identity(target: str, digest: str) -> str:
    return "pdfa_" + sha(encoded([SCHEMA, target, digest]))


def reference(digest: str) -> str:
    if not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None:
        raise ValueError("review_pdf_attachment_request_invalid")
    return f"run:attachments/pdf/{digest}.pdf"


def validate(value: Any) -> dict[str, Any]:
    try:
        if not isinstance(value, dict) or set(value) != {
            "schema_version", "attachment_id", "topic_id", "paper_version_id", "target_id", "run_id", "paper_id",
            "pdf_sha256", "pdf_bytes", "artifact_ref", "created_at", "created_by", "binding", "state",
        }:
            raise ValueError()
        if (value["schema_version"] != SCHEMA or value["binding"] != "USER_SELECTED_NOT_IDENTITY_VERIFIED"
                or value["state"] != "ATTACHED_NOT_EXTRACTED_OR_REVIEWED"
                or any(not isinstance(value[k], str) or not value[k].strip() for k in (
                    "topic_id", "paper_version_id", "run_id", "paper_id", "created_at", "created_by"))
                or datetime.fromisoformat(value["created_at"]).utcoffset() is None
                or type(value["pdf_bytes"]) is not int or not 0 < value["pdf_bytes"] <= MAX_PDF_BYTES
                or value["artifact_ref"] != reference(value["pdf_sha256"])
                or value["target_id"] != target_id(value["topic_id"], value["paper_version_id"], value["run_id"], value["paper_id"])
                or value["attachment_id"] != identity(value["target_id"], value["pdf_sha256"])):
            raise ValueError()
        return deepcopy(value)
    except (KeyError, ValueError, TypeError, AttributeError):
        raise ValueError(ERROR) from None
