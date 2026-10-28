"""Local document planning requests; never execution authority."""
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.review_preparation import Prepare

SCHEMA = "PAPER_DOCUMENT_PLAN_V1"
LOCAL_SCHEMA = "PAPER_DOCUMENT_PLAN_V2"
LOCAL_CONNECTOR = "ollama_local"
LOCAL_CONTEXT_REQUEST_LIMIT = 1_000_000
ESTIMATOR = "UTF8_BYTES_CONSERVATIVE_V1"


def local_model_valid(value: str) -> bool:
    """V1 local-selection grammar, not model availability or locality proof."""
    return (re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", value) is not None
            and not value.lower().startswith(("sk-", "xai-", "ya29.")) and "://" not in value
            and "cloud" not in re.split(r"[:/._-]", value.lower()))


def local_capability_v1() -> dict[str, Any]:
    """Frozen snapshot for V2 plans; future capabilities need another version."""
    return dict(schema_version="OLLAMA_LOCAL_CAPABILITY_V1", connector_id=LOCAL_CONNECTOR, provider_name="ollama",
        protocol_schema="OLLAMA_PREPARED_REVIEW_WIRE_V1", synthetic_protocol_available=True,
        document_planning_available=True, execution_available=False, locality="LOCALITY_NOT_VERIFIED",
        runtime_presence="NOT_CHECKED", model_identity="NOT_VERIFIED", context_capacity="NOT_VERIFIED",
        context_request_limit_tokens=LOCAL_CONTEXT_REQUEST_LIMIT)


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=100)
    run_id: str = Field(min_length=1, max_length=200)
    paper_id: str = Field(min_length=1, max_length=500)
    expected_source_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    connector_id: str = Field(min_length=1, max_length=100)
    model: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
    context_tokens: int = Field(ge=8192, le=2_000_000)
    input_tokens: int = Field(ge=4096, le=2_000_000)
    max_output_tokens: int = Field(ge=256, le=8000)
    safety_tokens: int = Field(ge=1024, le=100_000)

    @field_validator("request_id", "run_id", "paper_id")
    @classmethod
    def text(cls, value: str) -> str:
        return Prepare.valid_text(value)

    @model_validator(mode="after")
    def bounds(self) -> "Plan":
        if self.input_tokens + self.max_output_tokens + self.safety_tokens > self.context_tokens:
            raise ValueError("document_plan_budget_invalid")
        if self.model.lower().startswith(("sk-", "xai-", "ya29.")):
            raise ValueError("document_plan_model_invalid")
        if self.connector_id == LOCAL_CONNECTOR:
            if not local_model_valid(self.model):
                raise ValueError("document_plan_model_invalid")
            if self.context_tokens > LOCAL_CONTEXT_REQUEST_LIMIT:
                raise ValueError("document_plan_budget_invalid")
        return self


def budget(body: Plan, provider: str) -> dict[str, Any]:
    return dict(provider_name=provider, model=body.model, context_tokens=body.context_tokens,
        input_tokens=body.input_tokens, max_output_tokens=body.max_output_tokens,
        safety_tokens=body.safety_tokens, estimator=ESTIMATOR)
