"""Copy an already successful local trial into an owner-private analysis archive."""
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.repositories import saved_analyses
from app.services.local_anthropic_trial import LocalAnthropicTrial
from app.services.providers.base import ProviderError


class Save(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_binding_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    expected_response_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


def _capture(runtime: LocalAnthropicTrial, actor: str, body: Save | None = None) -> dict[str, Any]:
    try:
        return runtime.archive_capture(actor, body.expected_binding_sha256 if body else None,
                                       body.expected_response_sha256 if body else None)
    except (TypeError, KeyError, AttributeError, ProviderError):
        raise ValueError('saved_analysis_capture_invalid') from None


def preview(runtime: LocalAnthropicTrial | None, actor: str) -> dict[str, Any]:
    if runtime is None:
        return dict(available=False, reason='anthropic_trial_disabled')
    try:
        bundle = _capture(runtime, actor)
    except ValueError as error:
        if str(error) == 'anthropic_trial_analysis_unavailable':
            return dict(available=False, reason=str(error))
        raise
    grant = bundle['plan']['grant']
    source = grant['source']
    return dict(available=True, reason=None, source=dict(trial_id=grant['trial_id'],
        binding_sha256=bundle['binding_sha256'], response_sha256=bundle['response_sha256'],
        model=bundle['plan']['contract']['model'], origin=grant['origin'], captured_at=bundle['captured_at'],
        **{key: source[key] for key in ('title', 'page', 'start', 'end', 'excerpt')}))


def save(runtime: LocalAnthropicTrial | None, topic: str, actor: str, body: Save) -> dict[str, Any]:
    if runtime is None:
        raise ValueError('anthropic_trial_disabled')
    bundle = _capture(runtime, actor, body)
    return saved_analyses.save_trial_capture(topic, actor, bundle)
