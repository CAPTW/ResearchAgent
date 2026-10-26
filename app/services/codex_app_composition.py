"""Explicit server composition; no login, issuer, discovery or live activation.

Configuration is supplied by trusted server code, never an HTTP request or an
environment activation flag. Sources must independently authenticate their
immutable approval records. This module neither calls them at startup nor
creates the proof required by normal dispatch. The first live qualification
boundary remains separate.
"""
from pathlib import Path
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.auth.config import get_auth_settings, require_valid_session_secret
from app.services.journal_approval import JournalApprovalRuntime, Source as JournalSource
from app.services.journal_codex_dispatch import CodexJournalRuntime
from app.services.journal_codex_preview import JournalCodexPreview, PreviewConfigV2, _launcher_outside_profile
from app.services.providers.codex_journal_process import CliRuntime
from app.services.review_approval import PaperApprovalRuntime, Source as PaperSource
from app.services.review_codex_execution import CodexPaperRuntime
from app.services.codex_paper_trial import PaperTrialRuntime, Source as TrialSource


def _identity(value: str) -> str:
    if not value or value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("codex_app_identity_invalid")
    value.encode("utf-8", errors="strict")
    return value


class TrustedIssuer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    issuer_id: str = Field(min_length=1, max_length=500)
    policy_version: str = Field(min_length=1, max_length=100)

    @field_validator("issuer_id", "policy_version")
    @classmethod
    def exact_identity(cls, value: str) -> str:
        return _identity(value)


class CodexAppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["CODEX_V2_APP_CONFIG_V1"]
    runtime: PreviewConfigV2 = Field(repr=False)
    paper_model: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,199}$")
    paper_max_output_tokens: int = Field(ge=256, le=8000)
    approvers: list[str] = Field(min_length=1)
    trusted_issuers: list[TrustedIssuer] = Field(min_length=1)
    input_origin: Literal["PROVIDER_RESPONSE", "FIXTURE_SYNTHETIC"] = "PROVIDER_RESPONSE"

    @model_validator(mode="after")
    def exact_allowlists(self) -> "CodexAppConfig":
        for approver in self.approvers:
            _identity(approver)
        issuers = [(entry.issuer_id, entry.policy_version) for entry in self.trusted_issuers]
        if len(set(self.approvers)) != len(self.approvers) or len(set(issuers)) != len(issuers):
            raise ValueError("codex_app_duplicate_identity")
        return self


def create_codex_app(configuration: CodexAppConfig | None = None, *,
                     paper_source: PaperSource | None = None, journal_source: JournalSource | None = None,
                     paper_trial_source: TrialSource | None = None,
                     compatibility_report_path: str | None = None, web_dist: Path | None = None) -> FastAPI:
    from app.main import create_app

    if configuration is None:
        if (paper_source is not None or journal_source is not None or paper_trial_source is not None
                or compatibility_report_path is not None):
            raise ValueError("codex_app_configuration_required")
        return create_app(web_dist=web_dist)
    if not isinstance(configuration, CodexAppConfig):
        raise ValueError("codex_app_configuration_invalid")
    # Revalidate nested caller-owned collections, then keep only independent
    # runtime snapshots. Later edits cannot retarget a constructed app.
    config = CodexAppConfig.model_validate(configuration.model_dump())
    settings = get_auth_settings()
    if settings.auth_mode != "oidc":
        raise ValueError("codex_app_authentication_required")
    try:
        require_valid_session_secret(settings)
    except RuntimeError:
        raise ValueError("codex_app_authentication_required") from None
    trusted = frozenset((entry.issuer_id, entry.policy_version) for entry in config.trusted_issuers)
    approvers = frozenset(config.approvers)
    paper_approval = None if paper_source is None else PaperApprovalRuntime(
        source=paper_source, approvers=approvers, trusted_issuers=trusted, input_origin=config.input_origin)
    journal_approval = None if journal_source is None else JournalApprovalRuntime(
        source=journal_source, approvers=approvers, trusted_issuers=trusted)
    spec = config.runtime
    try:
        _launcher_outside_profile(tuple(spec.launcher), spec.environment)
        runtime = CliRuntime(tuple(spec.launcher), spec.environment, Path(spec.workspace))
        preview = JournalCodexPreview.from_runtime(runtime, reasoning_effort=spec.reasoning_effort,
            timeout_seconds=spec.timeout_seconds, compatibility_report_path=compatibility_report_path)
    except (OSError, ValueError, RuntimeError):
        raise ValueError("codex_app_runtime_invalid") from None
    paper = CodexPaperRuntime(runtime=runtime, trusted_issuers=trusted, model=config.paper_model,
        max_output_tokens=config.paper_max_output_tokens, reasoning_effort=spec.reasoning_effort,
        timeout_seconds=spec.timeout_seconds, input_origin=config.input_origin)
    journal = CodexJournalRuntime(runtime=runtime, trusted_issuers=trusted,
        reasoning_effort=spec.reasoning_effort, timeout_seconds=spec.timeout_seconds)
    trial = None if paper_trial_source is None else PaperTrialRuntime(paper=paper, source=paper_trial_source,
        approvers=approvers, trusted_issuers=trusted)
    return create_app(web_dist=web_dist, paper_codex_runtime=paper, journal_codex_runtime=journal,
        paper_approval_runtime=paper_approval, journal_approval_runtime=journal_approval,
        journal_codex_preview=preview, paper_trial_runtime=trial)
