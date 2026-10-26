"""Central trust boundary for turning external strings into filesystem paths.

Every place that lets an external identifier or a persisted artifact reference
select a directory/file must go through the validators and the containment
resolver in this module. The validators are pure: they never create
directories, open files, touch a database, or hit the network. Unsafe input
fails closed by raising a typed :class:`PathSafetyError` (or, for the pure
artifact-reference resolver that must never raise, by returning ``None`` at its
own call site).

Design notes
------------
* ``run_id`` selects exactly one path segment under the trusted runs root, so
  the accepted alphabet is the whitelist ``[A-Za-z0-9._-]`` plus structural
  checks. The whitelist alone rejects ``/`` ``\\`` ``:`` ``%`` and every
  control character, which collapses the "absolute / drive / UNC / device /
  ADS / encoded-traversal" families into one rule; the explicit checks below
  add precise, stable reason codes and the Windows-specific guards
  (``.``/``..`` segments, trailing dot, reserved device names, length).
* Artifact references are validated per segment with the same alphabet and the
  canonical separator ``/`` only. Normalization is never used to *repair*
  input: ``a/../b``, ``a//b`` and ``a/./b`` are rejected rather than folded.
* Containment is checked with resolved-path identity (``Path.resolve`` +
  ``Path.relative_to``), never string prefixing, so a sibling root such as
  ``.../runs-evil`` can never collide with ``.../runs``. Existing components
  below the root are rejected if they are symlinks, junctions or other reparse
  points, blocking escape through a pre-existing link. Read-time reparse-swap
  TOCTOU is a documented residual under the local-trusted-host model.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path, PurePosixPath

# Windows reparse-point attribute bit, used as a fallback when a stat result
# exposes ``st_file_attributes`` (present on Windows CPython).
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400

# Filesystem-safe identifier alphabet. Deliberately excludes path separators,
# colon, percent and every non-printable byte.
_ID_ALLOWED = re.compile(r"\A[A-Za-z0-9._-]+\Z")

# Windows reserved device names (case-insensitive, with or without extension).
_RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

MAX_RUN_ID_LENGTH = 128
MAX_ARTIFACT_PATH_LENGTH = 1024
MAX_ARTIFACT_SEGMENT_LENGTH = 255


class PathSafetyError(Exception):
    """Base class for every rejected path-derived value.

    ``code`` is a stable, machine-readable reason and ``field`` is the input
    field name. The raw offending value is intentionally *not* embedded in the
    message so it can never be reflected into an API response or a log line.
    """

    field = "path"
    code = "unsafe_path"

    def __init__(self, reason: str, *, field: str | None = None) -> None:
        self.reason = reason
        if field is not None:
            self.field = field
        super().__init__(f"{self.code} ({self.field}): {reason}")


class InvalidRunId(PathSafetyError):
    field = "run_id"
    code = "invalid_run_id"


class InvalidArtifactPath(PathSafetyError):
    field = "artifact_path"
    code = "invalid_artifact_path"


class PathContainmentError(PathSafetyError):
    field = "path"
    code = "path_not_contained"


def _has_control_char(text: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text)


def _reserved_device_stem(segment: str) -> bool:
    stem = segment.split(".", 1)[0].upper()
    return stem in _RESERVED_DEVICE_NAMES or segment.upper() in _RESERVED_DEVICE_NAMES


def _check_segment(segment: str, *, exc: type[PathSafetyError]) -> None:
    """Validate a single already-split path segment; raise on any defect."""
    if segment == "":
        raise exc("empty_segment")
    if segment in {".", ".."}:
        raise exc("dot_segment")
    if len(segment) > MAX_ARTIFACT_SEGMENT_LENGTH:
        raise exc("segment_too_long")
    if _has_control_char(segment):
        raise exc("control_char")
    if ":" in segment:
        raise exc("colon")
    if "%" in segment:
        raise exc("percent_encoding")
    if segment.endswith("."):
        raise exc("trailing_dot")
    if segment.endswith(" ") or segment.startswith(" "):
        raise exc("whitespace_padding")
    if not _ID_ALLOWED.match(segment):
        raise exc("illegal_char")
    if _reserved_device_stem(segment):
        raise exc("reserved_device_name")


def validate_run_id(value: object) -> str:
    """Return ``value`` unchanged if it is a safe single run-directory segment.

    Raises :class:`InvalidRunId` (never normalises silently) otherwise.
    """
    if not isinstance(value, str):
        raise InvalidRunId("not_a_string")
    if value == "":
        raise InvalidRunId("empty")
    if value.strip() != value:
        # Leading/trailing whitespace is rejected, not stripped.
        raise InvalidRunId("whitespace_padding")
    if len(value) > MAX_RUN_ID_LENGTH:
        raise InvalidRunId("too_long")
    if value in {".", ".."}:
        raise InvalidRunId("dot_segment")
    if "/" in value or "\\" in value:
        raise InvalidRunId("path_separator")
    if ":" in value:
        # Drive letters, drive-relative paths and NTFS alternate data streams.
        raise InvalidRunId("colon")
    if _has_control_char(value):
        raise InvalidRunId("control_char")
    if "%" in value:
        # Percent-encoded separator/traversal aliases (single or double).
        raise InvalidRunId("percent_encoding")
    if value.endswith("."):
        raise InvalidRunId("trailing_dot")
    if not _ID_ALLOWED.match(value):
        raise InvalidRunId("illegal_char")
    if _reserved_device_stem(value):
        raise InvalidRunId("reserved_device_name")
    return value


def is_safe_run_id(value: object) -> bool:
    try:
        validate_run_id(value)
    except InvalidRunId:
        return False
    return True


def validate_artifact_relative_path(value: object) -> str:
    """Return a canonical ``/``-joined relative artifact path or raise.

    Accepts only forward-slash relative references inside a trusted root.
    Absolute, drive, UNC, device, backslash, empty/``.``/``..`` segment,
    encoded, colon/ADS, control-char, trailing dot/space, reserved-name and
    over-length inputs are rejected. Normalisation is never used to succeed.
    """
    if not isinstance(value, str):
        raise InvalidArtifactPath("not_a_string")
    text = value
    if text == "":
        raise InvalidArtifactPath("empty")
    if text.strip() != text:
        raise InvalidArtifactPath("whitespace_padding")
    if len(text) > MAX_ARTIFACT_PATH_LENGTH:
        raise InvalidArtifactPath("too_long")
    if _has_control_char(text):
        raise InvalidArtifactPath("control_char")
    if "\\" in text:
        raise InvalidArtifactPath("backslash")
    if "?" in text or "#" in text:
        # A filesystem reference must not carry a URL query/fragment.
        raise InvalidArtifactPath("query_or_fragment")
    if text.startswith("/"):
        raise InvalidArtifactPath("leading_slash")
    if "//" in text:
        raise InvalidArtifactPath("empty_segment")
    segments = text.split("/")
    for segment in segments:
        _check_segment(segment, exc=InvalidArtifactPath)
    return "/".join(segments)


def is_safe_artifact_relative_path(value: object) -> bool:
    try:
        validate_artifact_relative_path(value)
    except InvalidArtifactPath:
        return False
    return True


def is_reparse_point(path: str | os.PathLike[str]) -> bool:
    """True if ``path`` is a symlink, junction or other reparse point.

    Uses ``os.lstat`` (no link following). Returns ``False`` if the path does
    not exist. Detection is intentionally broad: symlink, junction and the
    Windows reparse attribute are all treated as reparse points.
    """
    p = Path(path)
    try:
        result = os.lstat(p)
    except OSError:
        return False
    if stat.S_ISLNK(result.st_mode):
        return True
    attributes = getattr(result, "st_file_attributes", 0)
    if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        return True
    isjunction = getattr(os.path, "isjunction", None)
    if isjunction is not None:
        try:
            if isjunction(os.fspath(p)):
                return True
        except OSError:
            return False
    return False


def _existing_components_below_root(root: Path, parts: tuple[str, ...]) -> None:
    """Reject if any existing component strictly below ``root`` is a reparse point."""
    current = root
    for part in parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if is_reparse_point(current):
                raise PathContainmentError("reparse_point_component")


def resolve_within_root(
    root: str | os.PathLike[str],
    relative: str,
    *,
    must_exist: bool = False,
) -> Path:
    """Validate ``relative`` and resolve it strictly inside ``root``.

    Containment uses resolved-path identity (never string prefixing), so a
    sibling root cannot collide. Existing components below the root are
    rejected if they are reparse points. For a not-yet-existing write target,
    the nearest existing ancestor is what gets reparse-checked. This function
    performs no directory/file creation.
    """
    canonical = validate_artifact_relative_path(relative)
    parts = tuple(PurePosixPath(canonical).parts)
    root_path = Path(root)
    try:
        root_resolved = root_path.resolve()
    except OSError as exc:
        raise PathContainmentError("root_unresolvable") from exc

    _existing_components_below_root(root_resolved, parts)

    candidate = root_resolved.joinpath(*parts)
    try:
        candidate_resolved = candidate.resolve()
    except OSError as exc:
        raise PathContainmentError("candidate_unresolvable") from exc

    try:
        candidate_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise PathContainmentError("outside_root") from exc

    if is_reparse_point(candidate):
        raise PathContainmentError("reparse_point_target")

    if must_exist and not candidate.exists():
        raise PathContainmentError("missing_target")

    return candidate
