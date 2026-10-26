from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class PrototypeProviderSummary(BaseModel):
    provider_id: str | None = None
    surface_id: str | None = None
    stage: str = "report_draft_assist"
    output_class: str = "report_draft"
    route_status: str
    fallback_used: bool = True
    fallback_reason: str | None = None
    live_provider_invoked: bool = False
    accepted_control: str
    rejection_controls: str
    control_total: int | None = None
    control_passed: int | None = None
    record_digest: str | None = None


class PrototypeRunSummary(BaseModel):
    run_id: str
    topic_profile_id: str | None = None
    topic: str
    status: str
    created_at: str | None = None
    finished_at: str | None = None
    lineage: dict[str, Any] = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)
    report_status: str
    provider: PrototypeProviderSummary
    report_quality: dict[str, Any] = Field(default_factory=dict)


class PrototypeStatusResponse(BaseModel):
    enabled: bool
    label: str
    isolation_status: str
    reasons: list[str] = Field(default_factory=list)
    data_root_configured: bool
    data_root_under_system_temp: bool
    profile_id: str | None = None
    topic: str
    latest_run: PrototypeRunSummary | None = None
    can_start: bool
    live_provider_proof: dict[str, Any] = Field(default_factory=dict)


class PrototypeStartResponse(PrototypeRunSummary):
    pass


class PrototypeProviderRouteRequest(BaseModel):
    provider_id: str
    execution_surface: str
    stage: str
    internal_output_class: str
    product_output_class: str


class PrototypeProviderInvocationRequest(PrototypeProviderRouteRequest):
    activation_token: str = Field(min_length=20, max_length=256)
    confirmed: bool


class PrototypeProviderDecisionRequest(BaseModel):
    confirmed: bool


class PrototypeRealCorpusStartRequest(BaseModel):
    confirmed: bool
    network_enabled: bool
