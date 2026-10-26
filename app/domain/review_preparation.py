"""Frozen local text ranges. Preparation is neither execution nor a review receipt."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA = "PAPER_REVIEW_PREPARATION_V1"
CONTEXT_SCHEMA = "PAPER_REVIEW_PREPARATION_V2"
DOCUMENT_SCHEMA = "PAPER_REVIEW_PREPARATION_V3"
NORMALIZATION = "XRML_WHITESPACE_V1"
MAX_SELECTED_CHARS = 12_000
MAX_PREVIEW_CHARS = 4_000
MAX_SOURCE_CHARS = 1_000_000
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
_HASH = re.compile(r"^[a-f0-9]{64}$")


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="strict")).hexdigest()


class Prepare(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=100, pattern=r"\S")
    run_id: str = Field(min_length=1, max_length=200, pattern=r"\S")
    paper_id: str = Field(min_length=1, max_length=500, pattern=r"\S")
    expected_source_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    start: int = Field(ge=0, le=MAX_SOURCE_CHARS)
    end: int = Field(ge=1, le=MAX_SOURCE_CHARS)

    @field_validator("request_id", "run_id", "paper_id")
    @classmethod
    def valid_text(cls, value: str) -> str:
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeError:
            raise ValueError("review_preparation_request_invalid") from None
        if value != value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("review_preparation_request_invalid")
        return value

    @model_validator(mode="after")
    def valid_range(self) -> "Prepare":
        if not 1 <= self.end - self.start <= MAX_SELECTED_CHARS:
            raise ValueError("review_preparation_range_invalid")
        return self


class DocumentPrepare(Prepare):
    """Versioned document planning input; the legacy range contract stays fixed."""
    analysis_unit: dict[str, Any]

    @field_validator("analysis_unit")
    @classmethod
    def valid_analysis_unit(cls, value: dict[str, Any]) -> dict[str, Any]:
        from app.domain.review_token_budget import validate_analysis_unit
        return validate_analysis_unit(value)

    @model_validator(mode="after")
    def valid_range(self) -> "DocumentPrepare":
        if not 1 <= self.end - self.start <= MAX_SOURCE_CHARS or self.end > self.analysis_unit["document_chars"]:
            raise ValueError("review_preparation_range_invalid")
        return self


class Preview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    run_id: str = Field(min_length=1, max_length=200, pattern=r"\S")
    paper_id: str = Field(min_length=1, max_length=500, pattern=r"\S")
    expected_source_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    start: int = Field(ge=0, le=MAX_SOURCE_CHARS)
    length: int = Field(ge=1, le=MAX_PREVIEW_CHARS)

    @field_validator("run_id", "paper_id")
    @classmethod
    def valid_text(cls, value: str) -> str:
        return Prepare.valid_text(value)


def source_digest(source: dict[str, Any]) -> str:
    return sha(encoded({"normalization": NORMALIZATION,
                        "decoding": "UTF8_SIG_STRICT" if source.get("source_kind") == "pages" else "UTF8_STRICT",
                        **{key: value for key, value in source.items() if key != "source_digest"}}))


def identity(topic: str, version: str, actor: str, request: str) -> str:
    # Keep the original namespace: a replay after upgrade returns its first row.
    return "rprep_" + sha(encoded([SCHEMA, topic, version, actor, request]))


def build(topic: str, version: str, actor: str, body: Prepare, source: dict[str, Any], text: str, at: str,
          *, context: dict[str, Any] | None = None) -> dict[str, Any]:
    if not 0 <= body.start < body.end <= len(text):
        raise ValueError("review_preparation_range_invalid")
    selected = text[body.start:body.end]
    record = dict(schema_version=SCHEMA, preparation_id=identity(topic, version, actor, body.request_id),
                  topic_id=topic, paper_version_id=version, actor_id=actor, created_at=at,
                  state="PREPARED_NOT_EXECUTED", source=source,
                  selection=dict(start=body.start, end=body.end, text=selected, chars=len(selected), sha256=sha(selected)),
                  execution=dict(started=False, full_document_review="NOT_ESTABLISHED"))
    if context is not None:
        record.update(schema_version=CONTEXT_SCHEMA, context=context)
    if isinstance(body, DocumentPrepare):
        record.update(schema_version=DOCUMENT_SCHEMA, analysis_unit=deepcopy(body.analysis_unit))
    return validate(record, body)


def validate(record: Any, body: Prepare) -> dict[str, Any]:
    try:
        if not isinstance(record, dict):
            raise ValueError()
        document = record.get("schema_version") == DOCUMENT_SCHEMA
        contextual = record.get("schema_version") in {CONTEXT_SCHEMA, DOCUMENT_SCHEMA}
        if set(record) != {
            "schema_version", "preparation_id", "topic_id", "paper_version_id", "actor_id", "created_at",
            "state", "source", "selection", "execution",
        } | ({"context"} if contextual else set()) | ({"analysis_unit"} if document else set()):
            raise ValueError()
        if contextual:
            from app.domain.review_context import validate as validate_context
            validate_context(record["context"])
        if any(not isinstance(record[k], str) or not record[k].strip()
               for k in ("topic_id", "paper_version_id", "actor_id", "created_at")):
            raise ValueError()
        if datetime.fromisoformat(record["created_at"]).tzinfo is None:
            raise ValueError()
        if (record["schema_version"] not in {SCHEMA, CONTEXT_SCHEMA, DOCUMENT_SCHEMA} or record["state"] != "PREPARED_NOT_EXECUTED"
                or document != isinstance(body, DocumentPrepare)
                or record["preparation_id"] != identity(record["topic_id"], record["paper_version_id"], record["actor_id"], body.request_id)
                or record["execution"] != {"started": False, "full_document_review": "NOT_ESTABLISHED"}
                or record["execution"]["started"] is not False):
            raise ValueError()
        source, selection = record["source"], record["selection"]
        if not isinstance(source, dict) or set(source) != {
            "run_id", "paper_id", "source_digest", "source_chars", "source_kind", "artifact_sha256",
            "artifact_bytes", "artifact_ref", "extraction_truncation",
        }:
            raise ValueError()
        if (source["run_id"] != body.run_id or source["paper_id"] != body.paper_id
                or source["source_digest"] != body.expected_source_digest or source_digest(source) != source["source_digest"]
                or source["source_kind"] not in {"text", "pages"}
                or source["extraction_truncation"] not in {"UNKNOWN", "TRUNCATED", "NOT_OBSERVED"}
                or not isinstance(source["artifact_ref"], str) or not source["artifact_ref"].startswith("run:")
                or not isinstance(source["artifact_sha256"], str) or not _HASH.fullmatch(source["artifact_sha256"])
                or type(source["source_chars"]) is not int or not 1 <= source["source_chars"] <= MAX_SOURCE_CHARS
                or type(source["artifact_bytes"]) is not int or not 1 <= source["artifact_bytes"] <= MAX_ARTIFACT_BYTES):
            raise ValueError()
        if document:
            from app.domain.review_token_budget import validate_analysis_unit
            validate_analysis_unit(record["analysis_unit"], document_chars=source["source_chars"])
            if not isinstance(body, DocumentPrepare) or encoded(record["analysis_unit"]) != encoded(body.analysis_unit):
                raise ValueError()
        if not isinstance(selection, dict) or set(selection) != {"start", "end", "text", "chars", "sha256"}:
            raise ValueError()
        if (type(selection["start"]) is not int or type(selection["end"]) is not int or type(selection["chars"]) is not int
                or selection["start"] != body.start or selection["end"] != body.end
                or not 0 <= body.start < body.end <= source["source_chars"]
                or not isinstance(selection["text"], str) or selection["chars"] != len(selection["text"])
                or not 1 <= selection["chars"] == body.end - body.start <= (MAX_SOURCE_CHARS if document else MAX_SELECTED_CHARS)
                or selection["sha256"] != sha(selection["text"])):
            raise ValueError()
        encoded(record).encode("utf-8", errors="strict")
        return deepcopy(record)
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        raise ValueError("review_preparation_integrity_invalid") from None


def summary(record: dict[str, Any]) -> dict[str, Any]:
    return {**record, "selection": {key: value for key, value in record["selection"].items() if key != "text"}}
