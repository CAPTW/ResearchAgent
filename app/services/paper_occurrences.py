"""Persisted, path-independent authority for fallback paper occurrences.

DOI, arXiv and provider-native identifiers remain the primary Paper Bank
identity lanes.  This module owns only the final fallback lane: an accepted
observation gets one opaque identifier at persistence time, while legacy
JSONL records recover an identifier from their immutable logical storage
coordinate.  Descriptive metadata and physical paths are deliberately absent
from every identity input.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from app.domain.path_safety import validate_run_id
from app.repositories.jsonl import append_jsonl_records, replace_jsonl

OCCURRENCE_FIELD = "occurrence_id"
TRIAGE_LOGICAL_ARTIFACT = "run:triage/triage.jsonl"
OCCURRENCE_DOMAIN = "xrml-paper-occurrence-v1"
LEGACY_COORDINATE_DOMAIN = "xrml-paper-occurrence-legacy-coordinate-v1"
PUBLIC_COMPATIBILITY_DOMAIN = "xrml-paper-occurrence-compatibility-v1"

_PUBLIC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_GENERATED_ID = re.compile(r"^occ-[0-9a-f]{32}$")
_LEGACY_ID = re.compile(r"^occ-legacy-[0-9a-f]{32}$")
_DRIVE_PATH = re.compile(r"^[A-Za-z]:(?:[\\/]|[^:])")
_WINDOWS_RESERVED = re.compile(
    r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$",
    re.IGNORECASE,
)


class OccurrenceIdentityInvalid(ValueError):
    """Stable, redacted failure for malformed persisted occurrence authority."""

    code = "occurrence_identity_invalid"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(self.code)


class OccurrenceIdentityDuplicate(OccurrenceIdentityInvalid):
    """Stable failure for ambiguous persisted occurrence authority."""

    code = "occurrence_identity_duplicate"


def _canonical_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _path_shaped(value: str) -> bool:
    lowered = value.casefold()
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return True
    if lowered.startswith("file:") or value.startswith(("/", "\\")):
        return True
    if _DRIVE_PATH.match(value):
        return True
    if "/" in value or "\\" in value or ":" in value:
        return True
    return value in {".", ".."} or value.endswith((".", " ")) or bool(_WINDOWS_RESERVED.fullmatch(value))


def normalize_occurrence_id(value: object) -> str | None:
    """Return missing authority or one exact generated/legacy identifier."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise OccurrenceIdentityInvalid("not_string")
    normalized = unicodedata.normalize("NFC", value)
    if not normalized:
        raise OccurrenceIdentityInvalid("empty")
    if normalized != normalized.strip():
        raise OccurrenceIdentityInvalid("surrounding_whitespace")
    if len(normalized.encode("utf-8")) > 512:
        raise OccurrenceIdentityInvalid("too_long")
    if _path_shaped(normalized):
        raise OccurrenceIdentityInvalid("path_shaped")
    if _GENERATED_ID.fullmatch(normalized) or _LEGACY_ID.fullmatch(normalized):
        return normalized
    raise OccurrenceIdentityInvalid("grammar_invalid")


def assign_for_persistence(record: dict[str, Any]) -> dict[str, Any]:
    """Copy a record and assign its occurrence ID exactly once if missing."""

    occurrence_id = normalize_occurrence_id(record.get(OCCURRENCE_FIELD))
    persisted = dict(record)
    persisted[OCCURRENCE_FIELD] = occurrence_id or f"occ-{uuid.uuid4().hex}"
    return persisted


def require_unique_authorities(records: list[dict[str, Any]]) -> None:
    """Reject duplicate or malformed authority before identity projection."""

    seen: set[str] = set()
    for record in records:
        occurrence_id = require_authority(record)
        if occurrence_id in seen:
            raise OccurrenceIdentityDuplicate("duplicate")
        seen.add(occurrence_id)


def prepare_for_persistence(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy, assign once, validate, and reject ambiguous incoming batches."""

    prepared = [assign_for_persistence(record) for record in records]
    require_unique_authorities(prepared)
    return prepared


def _reject_destination_duplicates(
    current_records: list[dict[str, Any]],
    incoming_records: list[dict[str, Any]],
) -> None:
    require_unique_authorities(current_records)
    require_unique_authorities(incoming_records)
    current_ids = {require_authority(record) for record in current_records}
    if any(require_authority(record) in current_ids for record in incoming_records):
        raise OccurrenceIdentityDuplicate("destination_duplicate")


def write_triage_records(
    path: Path,
    records: list[dict[str, Any]],
    *,
    append: bool = False,
) -> list[dict[str, Any]]:
    """Persist one occurrence-authorized batch through the shared lock seam."""

    prepared = prepare_for_persistence(records)
    writer = append_jsonl_records if append else replace_jsonl
    writer(path, prepared, mutation_validator=_reject_destination_duplicates)
    return prepared


def legacy_coordinate_id(
    *,
    run_id: str,
    logical_artifact: str,
    physical_line_number: int,
) -> str:
    """Recover a legacy ID from a machine-independent persisted coordinate."""

    safe_run_id = validate_run_id(run_id)
    if logical_artifact != TRIAGE_LOGICAL_ARTIFACT:
        raise OccurrenceIdentityInvalid("logical_artifact_unsupported")
    if (
        not isinstance(physical_line_number, int)
        or isinstance(physical_line_number, bool)
        or physical_line_number < 1
    ):
        raise OccurrenceIdentityInvalid("line_number_invalid")
    digest = _canonical_digest(
        {
            "domain": LEGACY_COORDINATE_DOMAIN,
            "logicalArtifact": logical_artifact,
            "physicalLineNumber": int(physical_line_number),
            "runId": safe_run_id,
        }
    )
    return f"occ-legacy-{digest[:32]}"


def attach_loaded_authority(
    record: dict[str, Any],
    *,
    run_id: str,
    logical_artifact: str,
    physical_line_number: int,
) -> dict[str, Any]:
    """Preserve a stored ID or attach a deterministic legacy read authority."""

    occurrence_id = normalize_occurrence_id(record.get(OCCURRENCE_FIELD))
    loaded = dict(record)
    loaded[OCCURRENCE_FIELD] = occurrence_id or legacy_coordinate_id(
        run_id=run_id,
        logical_artifact=logical_artifact,
        physical_line_number=physical_line_number,
    )
    return loaded


def require_authority(record: dict[str, Any]) -> str:
    occurrence_id = normalize_occurrence_id(record.get(OCCURRENCE_FIELD))
    if occurrence_id is None:
        raise OccurrenceIdentityInvalid("missing_after_load")
    return occurrence_id


def identity_anchor(record: dict[str, Any]) -> str:
    """Return a versioned semantic anchor, never a descriptive record digest."""

    return _canonical_digest(
        {
            "domain": OCCURRENCE_DOMAIN,
            "occurrenceId": require_authority(record),
        }
    )


def public_compatibility_id(record: dict[str, Any]) -> str:
    """Return an opaque public ID bound to the persisted occurrence authority."""

    occurrence_id = require_authority(record)
    if _PUBLIC_ID.fullmatch(occurrence_id):
        return occurrence_id
    digest = _canonical_digest(
        {
            "domain": PUBLIC_COMPATIBILITY_DOMAIN,
            "occurrenceId": occurrence_id,
        }
    )
    return f"occ-compat-{digest[:32]}"


def internal_legacy_aliases(record: dict[str, Any]) -> tuple[str, ...]:
    """Return exact lookup aliases for internal one-to-one legacy review binding."""

    aliases: list[str] = []
    for key in (
        "paper_id",
        "id",
        OCCURRENCE_FIELD,
        "raw_metadata_ref",
        "source_record_id",
        "provider_record_id",
    ):
        raw_value = record.get(key)
        if not isinstance(raw_value, str):
            continue
        value = unicodedata.normalize("NFC", raw_value).strip()
        if value and value not in aliases:
            aliases.append(value)
    return tuple(aliases)
