"""Bounded arXiv collection contracts. A downloaded PDF is not an AI review."""
from datetime import date
import hashlib
import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.review_pdf_extraction import MAX_PDF_BYTES as MAX_PDF_BYTES

SCHEMA = "BROWSER_COLLECTION_V1"
HOSTS = ("arxiv.org", "export.arxiv.org")
ARXIV_ID = re.compile(r"\d{4}\.\d{4,5}v[1-9]\d{0,3}")
IDENTIFIER = r"[A-Za-z0-9_-]{1,100}"
STATES = {"queued", "downloading", "auth_required", "interrupted", "awaiting_confirmation", "saved", "failed"}
QUERY_CREDENTIAL_ERROR = "browser_collection_query_contains_connection_or_credential"
REDACTED_QUERY = "[민감한 검색어 숨김]"
_CONNECTION_FIELD = re.compile(r'''(?i)(?<![\w])(?:["']?)(base_url|job_id|token)(?:["']?)\s*[:=]''')
_CREDENTIAL_QUERY = re.compile(
    r'''(?ix)
    \bBearer\s+[a-z0-9._~+/=-]{16,}
    | (?<![\w])["']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|authorization|password|secret|token)["']?\s*[:=]\s*["']?[a-z0-9._~+/=-]{16,}
    | \b(?:sk-[a-z0-9_-]{16,}|gh[pousr]_[a-z0-9_]{16,}|github_pat_[a-z0-9_]{16,})
    | \beyJ[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}
    '''
)


def query_contains_credentials(value: object) -> bool:
    """Recognize misplaced connection codes, not ordinary token research terms.

    This bounded paste guard is not a general secret scanner. Never include the
    supplied query in an error or a diagnostic produced by this check.
    """
    return isinstance(value, str) and (
        {field.lower() for field in _CONNECTION_FIELD.findall(value)} == {"base_url", "job_id", "token"}
        or _CREDENTIAL_QUERY.search(value) is not None
    )


def validate_query(value: str) -> None:
    if query_contains_credentials(value):
        raise ValueError(QUERY_CREDENTIAL_ERROR)


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Empty(Strict):
    pass


class Candidate(Strict):
    arxiv_id: str = Field(pattern=r"^\d{4}\.\d{4,5}v[1-9]\d{0,3}$")
    title: str = Field(min_length=1, max_length=1000)
    authors: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(default_factory=list, max_length=500)
    published: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    abstract: str = Field(default="", max_length=30000)
    source_url: str
    pdf_url: str

    @model_validator(mode="after")
    def validate_source(self):
        date.fromisoformat(self.published)
        if (ARXIV_ID.fullmatch(self.arxiv_id) is None or not self.title.strip()
                or any(not author.strip() for author in self.authors)):
            raise ValueError("browser_collection_candidate_invalid")
        if self.source_url != f"https://arxiv.org/abs/{self.arxiv_id}":
            raise ValueError("browser_collection_source_out_of_scope")
        if self.pdf_url != f"https://arxiv.org/pdf/{self.arxiv_id}":
            raise ValueError("browser_collection_source_out_of_scope")
        return self


class Create(Strict):
    request_id: str = Field(pattern="^" + IDENTIFIER + "$")
    query: str = Field(min_length=1, max_length=500)
    allowed_hosts: list[Literal["arxiv.org", "export.arxiv.org"]] = Field(min_length=1, max_length=2)
    candidates: list[Candidate] = Field(min_length=1, max_length=10)
    search_receipt_id: str | None = Field(default=None, pattern=r"^bcs_[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_scope(self):
        if (not self.query.strip() or "arxiv.org" not in self.allowed_hosts
                or len(set(self.allowed_hosts)) != len(self.allowed_hosts)
                or len({c.arxiv_id for c in self.candidates}) != len(self.candidates)):
            raise ValueError("browser_collection_request_invalid")
        return self


class Search(Strict):
    query: str = Field(min_length=1, max_length=500)
    max_results: int = Field(default=5, ge=1, le=10)

    @field_validator("query")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("browser_collection_query_invalid")
        return value


class Transition(Strict):
    attempt_id: str = Field(pattern="^" + IDENTIFIER + "$")
    event: Literal["start", "auth_required", "interrupted", "resume"]
    download_id: int | None = Field(default=None, ge=0)
    source_url: str | None = None


class Confirm(Strict):
    expected_pdf_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    identity_confirmed: Literal[True]

    @field_validator("identity_confirmed", mode="before")
    @classmethod
    def exact_confirmation(cls, value):
        if value is not True:
            raise ValueError("browser_collection_confirmation_required")
        return value


class Control(Strict):
    action: Literal["pause", "resume"]
