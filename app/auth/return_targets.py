from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote_to_bytes, urlsplit, urlunsplit

from app.auth.config import AuthSettings

MAX_RETURN_TARGET_CHARACTERS = 2048
MAX_PERCENT_DECODE_PASSES = 3
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_ENCODED_PATH_SEPARATOR = re.compile(r"%(?:2f|5c)", re.IGNORECASE)


@dataclass(frozen=True)
class ReturnTargetValidation:
    target: str
    accepted: bool
    reason_code: str | None = None


class _UnsafeReturnTarget(ValueError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _contains_control(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _strict_unquote(value: str) -> str:
    index = 0
    while index < len(value):
        if value[index] != "%":
            index += 1
            continue
        if index + 2 >= len(value) or value[index + 1] not in _HEX_DIGITS or value[index + 2] not in _HEX_DIGITS:
            raise _UnsafeReturnTarget("return_target_malformed_encoding")
        index += 3
    try:
        return unquote_to_bytes(value).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _UnsafeReturnTarget("return_target_malformed_encoding") from exc


def _configured_base_path(settings: AuthSettings) -> str:
    candidate = settings.normalized_web_app_base_path
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return "/app"
    if (
        not candidate.startswith("/")
        or candidate.startswith("//")
        or "\\" in candidate
        or _contains_control(candidate)
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        return "/app"
    return parsed.path.rstrip("/") or "/"


def default_return_target(settings: AuthSettings) -> str:
    return _configured_base_path(settings)


def _path_matches_base(path: str, base_path: str) -> bool:
    if base_path == "/":
        return path.startswith("/") and not path.startswith("//")
    return path == base_path or path.startswith(f"{base_path}/")


def _query_contains_external_target(query: str) -> bool:
    for _, value in parse_qsl(query, keep_blank_values=True):
        candidate = value.strip()
        if not candidate:
            continue
        if candidate.startswith(("//", "\\\\")) or "\\" in candidate or _contains_control(candidate):
            return True
        try:
            parsed = urlsplit(candidate)
            if parsed.scheme or parsed.netloc or parsed.username or parsed.password or parsed.hostname:
                return True
            if parsed.port is not None:
                return True
        except ValueError:
            return True
    return False


def _validate_representation(value: str, *, base_path: str) -> None:
    if "\\" in value:
        raise _UnsafeReturnTarget("return_target_backslash")
    if _contains_control(value):
        raise _UnsafeReturnTarget("return_target_control_character")
    if _ENCODED_PATH_SEPARATOR.search(value):
        raise _UnsafeReturnTarget("return_target_encoded_separator")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise _UnsafeReturnTarget("return_target_invalid_url") from exc
    if parsed.scheme or parsed.netloc or parsed.username or parsed.password or parsed.hostname or port is not None:
        raise _UnsafeReturnTarget("return_target_external")
    if not parsed.path.startswith("/") or parsed.path.startswith("//"):
        raise _UnsafeReturnTarget("return_target_not_local")
    if not _path_matches_base(parsed.path, base_path):
        raise _UnsafeReturnTarget("return_target_outside_app")
    if any(segment in {".", ".."} for segment in parsed.path.split("/")):
        raise _UnsafeReturnTarget("return_target_path_traversal")
    if _query_contains_external_target(parsed.query):
        raise _UnsafeReturnTarget("return_target_nested_external")


def validate_return_target(value: str | None, *, settings: AuthSettings) -> ReturnTargetValidation:
    fallback = default_return_target(settings)
    if value is None or not isinstance(value, str) or not value:
        return ReturnTargetValidation(target=fallback, accepted=False, reason_code="return_target_missing")
    if len(value) > MAX_RETURN_TARGET_CHARACTERS:
        return ReturnTargetValidation(target=fallback, accepted=False, reason_code="return_target_too_long")

    try:
        base_path = _configured_base_path(settings)
        current = value
        for _ in range(MAX_PERCENT_DECODE_PASSES):
            _validate_representation(current, base_path=base_path)
            decoded = _strict_unquote(current)
            if decoded == current:
                parsed = urlsplit(value)
                canonical = urlunsplit(("", "", parsed.path, parsed.query, parsed.fragment))
                return ReturnTargetValidation(target=canonical, accepted=True)
            current = decoded
        _validate_representation(current, base_path=base_path)
        if _strict_unquote(current) != current:
            raise _UnsafeReturnTarget("return_target_excessive_encoding")
        parsed = urlsplit(value)
        canonical = urlunsplit(("", "", parsed.path, parsed.query, parsed.fragment))
        return ReturnTargetValidation(target=canonical, accepted=True)
    except _UnsafeReturnTarget as exc:
        return ReturnTargetValidation(target=fallback, accepted=False, reason_code=exc.reason_code)
    except ValueError:
        return ReturnTargetValidation(target=fallback, accepted=False, reason_code="return_target_invalid_url")


def canonical_return_target(value: str | None, *, settings: AuthSettings) -> str:
    return validate_return_target(value, settings=settings).target
