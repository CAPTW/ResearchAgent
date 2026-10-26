"""Small, persistence-friendly research policy primitives.

The policy is deliberately independent from UI locale and scheduler cadence.  It
is stored alongside an existing topic profile, so R2C does not need a database
migration or a second source of truth.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Mapping

LANGUAGE_TAG_PATTERN = re.compile(r"^(?:any|auto|[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*)$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DEFAULT_ACQUISITION_LANGUAGE = "any"
DEFAULT_REVIEW_REPORT_LANGUAGE = "en"


class ResearchPolicyError(ValueError):
    """Raised when a topic policy would be invalid or ambiguous."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def normalize_language(value: Any, *, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ResearchPolicyError("language_invalid", "Research language must be a language tag.")
    text = value.strip()
    if not text or len(text) > 35 or not LANGUAGE_TAG_PATTERN.fullmatch(text):
        raise ResearchPolicyError("language_invalid", "Research language must be any, auto, or a BCP-47-like tag.")
    lowered = text.casefold()
    if lowered in {"any", "auto"}:
        return lowered
    parts = lowered.split("-")
    # Canonicalise only the presentation of tags; matching remains deterministic
    # and the original semantics are not changed.
    canonical = [parts[0]]
    for part in parts[1:]:
        canonical.append(part.upper() if len(part) in {2, 3} and part.isalpha() else part.title() if len(part) == 4 else part)
    return "-".join(canonical)


def normalize_date(value: Any, *, field: str) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str) or not DATE_PATTERN.fullmatch(value.strip()):
        raise ResearchPolicyError(f"{field}_invalid", f"{field} must use YYYY-MM-DD.")
    text = value.strip()
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise ResearchPolicyError(f"{field}_invalid", f"{field} must use a real ISO date.") from exc
    return text


def normalize_research_policy(
    values: Mapping[str, Any] | None = None,
    *,
    current: Mapping[str, Any] | None = None,
) -> dict[str, str | None]:
    """Merge and validate policy values using both snake and camel aliases."""

    incoming = dict(values or {})
    existing = dict(current or {})

    def pick(snake: str, camel: str, default: Any = None) -> Any:
        if snake in incoming:
            return incoming[snake]
        if camel in incoming:
            return incoming[camel]
        if snake in existing:
            return existing[snake]
        if camel in existing:
            return existing[camel]
        return default

    acquisition_language = normalize_language(
        pick("acquisition_language", "acquisitionLanguage", DEFAULT_ACQUISITION_LANGUAGE),
        default=DEFAULT_ACQUISITION_LANGUAGE,
    )
    review_report_language = normalize_language(
        pick("review_report_language", "reviewReportLanguage", DEFAULT_REVIEW_REPORT_LANGUAGE),
        default=DEFAULT_REVIEW_REPORT_LANGUAGE,
    )
    acquisition_start_date = normalize_date(
        pick("acquisition_start_date", "acquisitionStartDate", None),
        field="acquisition_start_date",
    )
    acquisition_end_date = normalize_date(
        pick("acquisition_end_date", "acquisitionEndDate", None),
        field="acquisition_end_date",
    )
    if acquisition_start_date and acquisition_end_date and acquisition_start_date > acquisition_end_date:
        raise ResearchPolicyError(
            "acquisition_window_inverted",
            "acquisition_start_date must not be later than acquisition_end_date.",
        )
    return {
        "acquisition_language": acquisition_language,
        "review_report_language": review_report_language,
        "acquisition_start_date": acquisition_start_date,
        "acquisition_end_date": acquisition_end_date,
    }


def policy_from_profile(profile: Mapping[str, Any]) -> dict[str, str | None]:
    return normalize_research_policy(profile)
