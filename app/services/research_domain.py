from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from html import escape
from typing import Any, Iterable

NEUTRAL_DOMAIN_LABEL = "Research Topic"
DOMAIN_CONTEXT_SCHEMA = "research_domain_context_v1"
MAX_LABEL_CHARS = 120
MAX_TOPIC_CHARS = 240
MAX_DESCRIPTION_CHARS = 500
MAX_TERM_CHARS = 120
MAX_QUERY_CHARS = 240
MAX_QUESTION_CHARS = 240
MAX_FOCUS_TERMS = 16
MAX_SEARCH_QUERIES = 12
MAX_RESEARCH_QUESTIONS = 8


class DomainContextError(RuntimeError):
    """Stable, redacted failure raised for an invalid persisted context."""


class DomainContextSource(str, Enum):
    RUN_PROFILE_SNAPSHOT = "run_profile_snapshot"
    RUN_TOPIC = "run_topic"
    LEGACY_NEUTRAL = "legacy_neutral"
    SYSTEM_TEMPLATE = "system_template"


@dataclass(frozen=True)
class ResearchDomainContext:
    source: DomainContextSource
    display_name: str
    topic: str
    description: str
    focus_terms: tuple[str, ...]
    search_queries: tuple[str, ...]
    research_questions: tuple[str, ...]

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "schema": DOMAIN_CONTEXT_SCHEMA,
            "source": self.source.value,
            "display_name": self.display_name,
            "topic": self.topic,
            "description": self.description,
            "focus_terms": list(self.focus_terms),
            "search_queries": list(self.search_queries),
            "research_questions": list(self.research_questions),
        }


def _normalize_text(value: Any, *, max_chars: int) -> str:
    raw = str(value or "")
    without_controls = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in raw
    )
    normalized = re.sub(r"\s+", " ", without_controls).strip()
    return normalized[:max_chars].rstrip()


def _strings(value: Any) -> Iterable[Any]:
    if isinstance(value, (list, tuple)):
        yield from value
    elif isinstance(value, dict):
        for key in sorted(value):
            nested = value[key]
            if isinstance(nested, (list, tuple)):
                yield from nested


def _bounded_unique(values: Iterable[Any], *, limit: int, max_chars: int) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _normalize_text(value, max_chars=max_chars)
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        result.append(normalized)
        if len(result) >= limit:
            break
    return tuple(result)


def _context_source(
    profile: dict[str, Any] | None,
    run_topic: str,
    explicit_source: DomainContextSource | None,
) -> DomainContextSource:
    if explicit_source is not None:
        return explicit_source
    if profile:
        if str(profile.get("resource_kind") or "").strip() == "system_template":
            return DomainContextSource.SYSTEM_TEMPLATE
        return DomainContextSource.RUN_PROFILE_SNAPSHOT
    if run_topic:
        return DomainContextSource.RUN_TOPIC
    return DomainContextSource.LEGACY_NEUTRAL


def build_research_domain_context(
    *,
    profile: dict[str, Any] | None = None,
    run_topic: Any = None,
    source: DomainContextSource | None = None,
) -> ResearchDomainContext:
    """Build a bounded domain context without filesystem, network, or global state."""

    profile = dict(profile or {})
    normalized_run_topic = _normalize_text(run_topic, max_chars=MAX_TOPIC_CHARS)
    topic = _normalize_text(
        profile.get("topic") or normalized_run_topic,
        max_chars=MAX_TOPIC_CHARS,
    )
    display_name = _normalize_text(
        profile.get("title") or profile.get("display_name") or topic,
        max_chars=MAX_LABEL_CHARS,
    )
    resolved_source = _context_source(profile or None, normalized_run_topic, source)
    if not display_name:
        display_name = NEUTRAL_DOMAIN_LABEL
    if not topic:
        topic = display_name

    focus_values = list(_strings(profile.get("keywords")))
    focus_values.extend(_strings(profile.get("focus_terms")))
    search_values = list(_strings(profile.get("queries")))
    search_values.extend(_strings(profile.get("search_queries")))
    question_values = list(_strings(profile.get("research_questions")))
    question_values.extend(_strings(profile.get("questions")))

    return ResearchDomainContext(
        source=resolved_source,
        display_name=display_name,
        topic=topic,
        description=_normalize_text(
            profile.get("description"),
            max_chars=MAX_DESCRIPTION_CHARS,
        ),
        focus_terms=_bounded_unique(
            focus_values,
            limit=MAX_FOCUS_TERMS,
            max_chars=MAX_TERM_CHARS,
        ),
        search_queries=_bounded_unique(
            search_values,
            limit=MAX_SEARCH_QUERIES,
            max_chars=MAX_QUERY_CHARS,
        ),
        research_questions=_bounded_unique(
            question_values,
            limit=MAX_RESEARCH_QUESTIONS,
            max_chars=MAX_QUESTION_CHARS,
        ),
    )


def research_domain_context_from_snapshot(payload: Any) -> ResearchDomainContext:
    if not isinstance(payload, dict):
        raise DomainContextError("domain_context_invalid")
    if payload.get("schema") != DOMAIN_CONTEXT_SCHEMA:
        raise DomainContextError("domain_context_invalid")
    try:
        source = DomainContextSource(str(payload.get("source") or ""))
    except ValueError as exc:
        raise DomainContextError("domain_context_invalid") from exc
    context = build_research_domain_context(
        profile={
            "title": payload.get("display_name"),
            "topic": payload.get("topic"),
            "description": payload.get("description"),
            "focus_terms": payload.get("focus_terms"),
            "search_queries": payload.get("search_queries"),
            "research_questions": payload.get("research_questions"),
        },
        source=source,
    )
    if source != DomainContextSource.LEGACY_NEUTRAL and not _normalize_text(
        payload.get("display_name") or payload.get("topic"),
        max_chars=MAX_TOPIC_CHARS,
    ):
        raise DomainContextError("domain_context_invalid")
    return context


def research_domain_context_for_run(run: dict[str, Any]) -> ResearchDomainContext:
    """Resolve only persisted run state; never consult a mutable live profile."""

    if "domain_context" in run:
        return research_domain_context_from_snapshot(run.get("domain_context"))

    profile = run.get("profile")
    if isinstance(profile, dict):
        if "domain_context" in profile:
            return research_domain_context_from_snapshot(profile.get("domain_context"))
        if any(
            _normalize_text(profile.get(field), max_chars=MAX_TOPIC_CHARS)
            for field in ("title", "display_name", "topic")
        ):
            return build_research_domain_context(
                profile=profile,
                source=DomainContextSource.RUN_PROFILE_SNAPSHOT,
            )

    run_topic = _normalize_text(run.get("topic"), max_chars=MAX_TOPIC_CHARS)
    if run_topic:
        return build_research_domain_context(
            run_topic=run_topic,
            source=DomainContextSource.RUN_TOPIC,
        )
    return build_research_domain_context(source=DomainContextSource.LEGACY_NEUTRAL)


def markdown_inline(value: Any) -> str:
    """Render profile-owned inline text without allowing Markdown or HTML markup."""

    normalized = _normalize_text(value, max_chars=MAX_TOPIC_CHARS)
    escaped = escape(normalized, quote=False).replace("\\", "\\\\")
    for character in ("`", "*", "_", "{", "}", "[", "]", "<", ">", "#"):
        escaped = escaped.replace(character, f"\\{character}")
    return escaped
