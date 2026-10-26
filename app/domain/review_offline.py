"""Request contract for a reserved synthetic adapter, never a live grant."""
from pydantic import BaseModel, ConfigDict, Field, field_validator

ADAPTER = "xrml_offline_review"
VERSION = "2"
MODEL = "synthetic-prepared-review-v2"
MODE = "OFFLINE_SYNTHETIC"


class Execute(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=100, pattern=r"\S")
    expected_preparation_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("request_id")
    @classmethod
    def valid_text(cls, value: str) -> str:
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeError:
            raise ValueError("review_offline_request_invalid") from None
        if value != value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("review_offline_request_invalid")
        return value
