"""Immutable local PDF text observations, never a model-review assertion."""
from copy import deepcopy
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain import review_preparation as preparation

SCHEMA = "PAPER_PDF_EXTRACTION_V1"
ATTACHED_SCHEMA = "PAPER_PDF_EXTRACTION_V2"
POLICY = "LOCAL_PDF_TEXT_V1"
MAX_PDF_BYTES = 25_000_000
MAX_DOCUMENT_PAGES = 2_000
MAX_RECORDS = 200
encoded = preparation.encoded
sha = preparation.sha
ERROR = "review_pdf_extraction_integrity_invalid"


class Extract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    run_id: str = Field(min_length=1, max_length=200, pattern=r"\S")
    paper_id: str = Field(min_length=1, max_length=500, pattern=r"\S")
    expected_pdf_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    attachment_id: str | None = Field(default=None, pattern=r"^pdfa_[a-f0-9]{64}$")

    @field_validator("run_id", "paper_id")
    @classmethod
    def valid_text(cls, value: str) -> str:
        return preparation.Prepare.valid_text(value)


def identity(topic: str, version: str, body: Extract) -> str:
    # Preserve every existing V1 identity, including explicit null callers.
    schema = ATTACHED_SCHEMA if body.attachment_id else SCHEMA
    return "pdfx_" + sha(encoded([schema, POLICY, topic, version, body.model_dump(exclude_none=True)]))


def text_reference(extraction_id: str) -> str:
    return f"run:extractions/{extraction_id}/document.txt"


def validate(value: Any) -> dict[str, Any]:
    from app.services.pdf_acquisition import validate_extraction_provenance

    try:
        if not isinstance(value, dict) or set(value) != {
            "schema_version", "extraction_id", "topic_id", "paper_version_id", "created_by", "created_at",
            "policy_version", "pdf", "provenance", "source", "page_ranges", "no_text_page_numbers",
            "unvisited_page_numbers", "execution", "scientific_support",
        }:
            raise ValueError()
        if (value["schema_version"] not in {SCHEMA, ATTACHED_SCHEMA} or value["policy_version"] != POLICY
                or value["execution"] != {"started": False, "full_document_review": "NOT_ESTABLISHED"}
                or value["execution"]["started"] is not False or value["scientific_support"] != "NOT_ASSESSED"
                or any(not isinstance(value[k], str) or not value[k].strip()
                       for k in ("topic_id", "paper_version_id", "created_by", "created_at"))
                or datetime.fromisoformat(value["created_at"]).utcoffset() is None):
            raise ValueError()
        pdf, source = value["pdf"], value["source"]
        attached = value["schema_version"] == ATTACHED_SCHEMA
        if not isinstance(pdf, dict) or set(pdf) != ({"sha256", "bytes", "artifact_ref", "attachment_id"} if attached else {"sha256", "bytes", "artifact_ref"}):
            raise ValueError()
        body = Extract(run_id=source["run_id"], paper_id=source["paper_id"], expected_pdf_sha256=pdf["sha256"], attachment_id=pdf.get("attachment_id"))
        if attached:
            from app.domain import review_pdf_attachment as attachment
            if (pdf["artifact_ref"] != attachment.reference(pdf["sha256"])
                    or pdf["attachment_id"] != attachment.identity(attachment.target_id(value["topic_id"], value["paper_version_id"], source["run_id"], source["paper_id"]), pdf["sha256"])):
                raise ValueError()
        if (value["extraction_id"] != identity(value["topic_id"], value["paper_version_id"], body)
                or type(pdf["bytes"]) is not int or not 0 < pdf["bytes"] <= MAX_PDF_BYTES
                or not isinstance(pdf["artifact_ref"], str) or (not attached and not pdf["artifact_ref"].startswith("run:raw/"))
                or ".." in pdf["artifact_ref"].split("/") or "\\" in pdf["artifact_ref"]):
            raise ValueError()
        if not isinstance(source, dict) or set(source) != {
            "run_id", "paper_id", "source_digest", "source_chars", "source_kind", "artifact_sha256",
            "artifact_bytes", "artifact_ref", "extraction_truncation",
        }:
            raise ValueError()
        provenance = validate_extraction_provenance(value["provenance"])
        outputs = provenance["outputs"]
        total = provenance["document_page_count"]
        if (provenance["source_pdf"] != {"sha256": pdf["sha256"], "bytes": pdf["bytes"]}
                or provenance["max_chars"] != preparation.MAX_SOURCE_CHARS
                or (total is not None and total > MAX_DOCUMENT_PAGES)
                or any(len(provenance[key]) > MAX_DOCUMENT_PAGES for key in ("visited_page_numbers", "retained_page_numbers"))
                or source["source_kind"] != "text" or source["artifact_ref"] != text_reference(value["extraction_id"])
                or source["source_digest"] != preparation.source_digest(source)
                or source["artifact_sha256"] != outputs["text_sha256"] or source["artifact_bytes"] != outputs["text_bytes"]
                or type(source["source_chars"]) is not int or not 0 < source["source_chars"] <= preparation.MAX_SOURCE_CHARS
                or type(source["artifact_bytes"]) is not int or not 0 < source["artifact_bytes"] <= preparation.MAX_ARTIFACT_BYTES
                or outputs["pages_bytes"] > preparation.MAX_ARTIFACT_BYTES
                or source["extraction_truncation"] != provenance["truncation"]):
            raise ValueError()
        if (provenance["document_text_chars"] is not None
                and (provenance["document_text_chars"] < source["source_chars"]
                     or (provenance["truncation"] == "NOT_OBSERVED" and provenance["document_text_chars"] != source["source_chars"]))):
            raise ValueError()
        ranges = value["page_ranges"]
        if not isinstance(ranges, list) or not ranges:
            raise ValueError()
        cursor = 0
        for row in ranges:
            if (not isinstance(row, dict) or set(row) != {"page_number", "start", "end", "chars"}
                    or any(type(row[k]) is not int for k in row)
                    or row["start"] != cursor or row["chars"] != row["end"] - row["start"] or row["chars"] < 1):
                raise ValueError()
            cursor = row["end"] + 1
        if ([row["page_number"] for row in ranges] != provenance["retained_page_numbers"]
                or cursor - 1 != source["source_chars"]):
            raise ValueError()
        no_text = sorted(set(provenance["visited_page_numbers"]) - set(provenance["retained_page_numbers"]))
        unvisited = sorted(set(range(1, total + 1)) - set(provenance["visited_page_numbers"])) if total is not None else None
        if (value["no_text_page_numbers"] != no_text or value["unvisited_page_numbers"] != unvisited
                or any(type(n) is not int for n in value["no_text_page_numbers"])
                or (unvisited is not None and any(type(n) is not int for n in value["unvisited_page_numbers"]))):
            raise ValueError()
        encoded(value).encode("utf-8", errors="strict")
        return deepcopy(value)
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        raise ValueError(ERROR) from None
