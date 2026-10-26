from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


class ProviderError(RuntimeError):
    pass


class ProviderUnavailableError(ProviderError):
    pass


class ProviderRefusalError(ProviderError):
    pass


class ProviderReportedFailure(ProviderError):
    """The provider reported a terminal non-success, not a transport timeout."""

    def __init__(self, status: Literal["incomplete", "failed", "cancelled"]) -> None:
        if status not in {"incomplete", "failed", "cancelled"}:
            raise ValueError("invalid_provider_terminal_status")
        self.status = status
        super().__init__(f"Provider reported {status}; no completed structured output was accepted.")


class ProviderOutputError(ProviderError):
    """A completed response was received, but its output could not be accepted."""


@dataclass(slots=True)
class ProviderRequest:
    schema_name: str
    schema: dict[str, Any]
    instructions: str
    prompt: str
    metadata: dict[str, Any] = field(default_factory=dict)
    temperature: float = 0.2
    max_output_tokens: int = 2200


@dataclass(slots=True)
class ProviderResult:
    provider_name: str
    model: str
    response_id: str | None
    payload: dict[str, Any]
    usage: dict[str, Any] = field(default_factory=dict)
    raw_text: str | None = None


class StructuredOutputProvider(Protocol):
    @property
    def provider_name(self) -> str:
        ...

    def is_configured(self) -> bool:
        ...

    def describe_status(self) -> dict[str, Any]:
        ...

    def generate_json(self, request: ProviderRequest) -> ProviderResult:
        ...
