from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any

from app.domain import constants
from app.domain.path_safety import is_safe_run_id

RUN_PREFIX = "run:"
STORAGE_PREFIX = "storage:"
PROJECT_PREFIX = "project:"
_PATH_KEYS = {
    "artifact_path",
    "authoritative_root",
    "output_hint",
    "raw_file",
    "inbox_file",
    "triage_jsonl",
    "triage_md",
    "report_markdown",
    "research_plan_json",
    "research_plan_markdown",
    "daily_report",
    "outline_json",
    "outline_path",
    "preview_markdown",
    "preview_path",
    "experiment_dir",
    "profile",
    "sources",
    "keywords",
    "themes",
    "review_json",
    "note_markdown",
    "pdf",
    "text_txt",
    "pages_json",
    "output",
    "storage_path",
    "path",
    "raw_path",
    "report_path",
    "research_plan_path",
    "prototype_provider_execution_record",
    "source_artifact_path",
    "source_outline_id",
    "source_outline_path",
    "source_preview_path",
}
_LIST_PATH_KEYS = {"paper_notes", "created"}


@dataclass(frozen=True)
class ArtifactReferenceResolution:
    original_ref: str | None
    portable_ref: str | None
    display_ref: str
    resolved_path: str | None
    exists: bool
    reason: str
    is_safe: bool
    was_legacy_absolute: bool
    resolution_strategy: str


@dataclass(frozen=True)
class ArtifactReferenceValidation:
    resolution: ArtifactReferenceResolution
    status: str
    expected_sha256: str | None
    actual_sha256: str | None
    sha256_valid: bool | None
    reason: str


def _run_root(run_id: str | None) -> Path | None:
    # Fail closed on an unsafe run_id: the pure resolver never raises, so an
    # invalid identifier simply selects no run root (treated as "no run dir").
    if not run_id or not is_safe_run_id(run_id):
        return None
    return constants.STORAGE_ROOT / run_id


def _is_url(value: str) -> bool:
    lowered = value.lower()
    return lowered.startswith("http://") or lowered.startswith("https://")


def _prefixed(value: str) -> bool:
    return value.startswith((RUN_PREFIX, STORAGE_PREFIX, PROJECT_PREFIX))


def _relative_to(path: Path, root: Path) -> Path | None:
    try:
        return path.resolve().relative_to(root.resolve())
    except ValueError:
        return None


def _trusted_roots() -> tuple[Path, ...]:
    roots = [
        constants.PROJECT_ROOT,
        constants.STORAGE_ROOT,
        constants.STORAGE_ROOT.parent,
        constants.LEGACY_DATA_ROOT,
        constants.LEGACY_NOTES_ROOT,
        constants.LEGACY_REPORTS_ROOT,
    ]
    resolved: list[Path] = []
    for root in roots:
        try:
            resolved_root = root.resolve()
        except OSError:
            continue
        if resolved_root not in resolved:
            resolved.append(resolved_root)
    return tuple(resolved)


def _is_absolute_like(value: str) -> bool:
    text = value.replace("\\", "/")
    if text.startswith("/"):
        return True
    return len(text) >= 3 and text[1] == ":" and text[2] == "/"


def _relative_parts(value: str | PurePath) -> tuple[str, ...] | None:
    text = str(value).replace("\\", "/")
    if "\x00" in text:
        return None
    parts = [part for part in text.split("/") if part not in {"", "."}]
    if not parts:
        return tuple()
    if any(part == ".." for part in parts):
        return None
    # A portable segment never carries a colon. Rejecting it here closes three
    # Windows-specific holes at once: drive-letter segments (`C:`, `C:x`),
    # NTFS alternate data stream syntax (`report.md:$DATA`) and unsupported
    # namespaces (`evil:x`), none of which can name a portable file.
    if any(":" in part for part in parts):
        return None
    return tuple(parts)


def _starts_nested_storage_namespace(parts: tuple[str, ...]) -> bool:
    lowered = tuple(part.lower() for part in parts)
    return len(lowered) >= 2 and lowered[:2] == ("runs", "runs")


def _starts_project_nested_storage_namespace(parts: tuple[str, ...]) -> bool:
    lowered = tuple(part.lower() for part in parts)
    return len(lowered) >= 3 and lowered[:3] == ("storage", "runs", "runs")


def _is_nested_storage_namespace_ref(value: str | PurePath, *, project_relative: bool = False) -> bool:
    parts = _relative_parts(value)
    if parts is None:
        return False
    if project_relative:
        return _starts_project_nested_storage_namespace(parts)
    return _starts_nested_storage_namespace(parts)


def _safe_join(root: Path, relative: str | PurePath) -> Path | None:
    parts = _relative_parts(relative)
    if parts is None:
        return None
    candidate = (root / Path(*parts)).resolve()
    return candidate if _path_is_trusted(candidate) else None


def _path_is_trusted(path: Path) -> bool:
    resolved = path.resolve()
    return any(_relative_to(resolved, root) is not None for root in _trusted_roots())


def _path_is_within_roots(path: Path, roots: tuple[Path, ...]) -> bool:
    resolved = path.resolve()
    return any(_relative_to(resolved, root) is not None for root in roots)


def _is_nested_storage_namespace_path(path: Path) -> bool:
    relative_storage = _relative_to(path.resolve(), constants.STORAGE_ROOT.parent)
    return bool(relative_storage is not None and _starts_nested_storage_namespace(tuple(relative_storage.parts)))


def _is_nested_storage_namespace_path_with_roots(
    path: Path,
    storage_roots: list[str | Path] | tuple[str | Path, ...] | None,
) -> bool:
    resolved = path.resolve()
    for root in storage_roots or ():
        storage_root = Path(root).expanduser().resolve()
        relative_storage = _relative_to(resolved, storage_root.parent)
        if relative_storage is not None and _starts_nested_storage_namespace(tuple(relative_storage.parts)):
            return True
    return False


def _custom_allowed_roots(repo_root: str | Path | None, storage_roots: list[str | Path] | tuple[str | Path, ...] | None) -> tuple[Path, ...]:
    roots: list[Path] = []
    if repo_root is not None:
        roots.append(Path(repo_root).expanduser().resolve())
    for root in storage_roots or ():
        storage_root = Path(root).expanduser().resolve()
        roots.append(storage_root)
        roots.append(storage_root.parent)
    deduped: list[Path] = []
    for root in roots:
        if root not in deduped:
            deduped.append(root)
    return tuple(deduped)


def _safe_join_custom(root: Path, relative: str | PurePath, allowed_roots: tuple[Path, ...]) -> Path | None:
    parts = _relative_parts(relative)
    if parts is None:
        return None
    candidate = (root / Path(*parts)).resolve()
    return candidate if _path_is_within_roots(candidate, allowed_roots) else None


def _nested_storage_namespace_resolution(
    *,
    original_ref: str | None,
    was_legacy_absolute: bool,
    resolution_strategy: str,
) -> ArtifactReferenceResolution:
    return _resolution(
        original_ref=original_ref,
        portable_ref=None,
        resolved_path=None,
        reason="nested_storage_namespace",
        is_safe=False,
        was_legacy_absolute=was_legacy_absolute,
        resolution_strategy=resolution_strategy,
    )


def _portable_ref_for_path(path: Path, *, run_id: str | None = None) -> str | None:
    resolved = path.resolve()
    if _is_nested_storage_namespace_path(resolved):
        return None
    run_root = _run_root(run_id)
    if run_root is not None:
        relative_run = _relative_to(resolved, run_root)
        if relative_run is not None:
            return f"{RUN_PREFIX}{relative_run.as_posix()}"
    relative_storage = _relative_to(resolved, constants.STORAGE_ROOT.parent)
    if relative_storage is not None:
        return f"{STORAGE_PREFIX}{relative_storage.as_posix()}"
    relative_project = _relative_to(resolved, constants.PROJECT_ROOT)
    if relative_project is not None:
        return f"{PROJECT_PREFIX}{relative_project.as_posix()}"
    return None


def _legacy_storage_relative(value: str) -> PurePath | None:
    parts = [part for part in value.replace("\\", "/").split("/") if part]
    lowered = [part.lower() for part in parts]
    for index in range(0, len(parts) - 1):
        if lowered[index] == "storage" and lowered[index + 1] == "runs":
            tail = parts[index + 2 :]
            safe_tail = _relative_parts("/".join(tail))
            if safe_tail is None or not safe_tail:
                return None
            return PurePath(*safe_tail)
    return None


def _portable_ref_for_path_with_roots(
    path: str | Path,
    *,
    repo_root: str | Path | None = None,
    storage_roots: list[str | Path] | tuple[str | Path, ...] | None = None,
    run_id: str | None = None,
) -> str | None:
    resolved = Path(path).expanduser().resolve()
    if _is_nested_storage_namespace_path_with_roots(resolved, storage_roots):
        return None
    for root in storage_roots or ():
        storage_root = Path(root).expanduser().resolve()
        if run_id:
            relative_run = _relative_to(resolved, storage_root / run_id)
            if relative_run is not None:
                return f"{RUN_PREFIX}{relative_run.as_posix()}"
        relative_storage = _relative_to(resolved, storage_root.parent)
        if relative_storage is not None:
            return f"{STORAGE_PREFIX}{relative_storage.as_posix()}"
    if repo_root is not None:
        relative_project = _relative_to(resolved, Path(repo_root).expanduser().resolve())
        if relative_project is not None:
            return f"{PROJECT_PREFIX}{relative_project.as_posix()}"
    return None


def _resolution(
    *,
    original_ref: str | None,
    portable_ref: str | None,
    resolved_path: Path | None,
    reason: str,
    is_safe: bool,
    was_legacy_absolute: bool,
    resolution_strategy: str,
) -> ArtifactReferenceResolution:
    exists = bool(resolved_path and resolved_path.exists())
    if not is_safe:
        display_ref = "unsafe artifact reference"
    elif portable_ref:
        display_ref = portable_ref
    elif original_ref and not _is_absolute_like(original_ref):
        display_ref = original_ref
    elif original_ref:
        display_ref = "absolute artifact reference"
    else:
        display_ref = "-"
    return ArtifactReferenceResolution(
        original_ref=original_ref,
        portable_ref=portable_ref,
        display_ref=display_ref,
        resolved_path=str(resolved_path) if resolved_path is not None else None,
        exists=exists,
        reason=reason,
        is_safe=is_safe,
        was_legacy_absolute=was_legacy_absolute,
        resolution_strategy=resolution_strategy,
    )


def resolve_artifact_reference(value: str | None, *, run_id: str | None = None) -> ArtifactReferenceResolution:
    if value is None or not str(value).strip():
        return _resolution(
            original_ref=value,
            portable_ref=None,
            resolved_path=None,
            reason="missing_artifact_reference",
            is_safe=True,
            was_legacy_absolute=False,
            resolution_strategy="empty",
        )
    text = str(value).strip()
    if _is_url(text):
        return _resolution(
            original_ref=text,
            portable_ref=text,
            resolved_path=None,
            reason="external_url_reference",
            is_safe=True,
            was_legacy_absolute=False,
            resolution_strategy="url",
        )

    if text.startswith(RUN_PREFIX):
        if run_id is None:
            return _resolution(
                original_ref=text,
                portable_ref=None,
                resolved_path=None,
                reason="missing_run_id",
                is_safe=False,
                was_legacy_absolute=False,
                resolution_strategy="run_prefix",
            )
        relative = text[len(RUN_PREFIX) :]
        candidate = _safe_join(_run_root(run_id) or constants.STORAGE_ROOT, relative)
        if candidate is None:
            return _resolution(
                original_ref=text,
                portable_ref=None,
                resolved_path=None,
                reason="unsafe_artifact_reference",
                is_safe=False,
                was_legacy_absolute=False,
                resolution_strategy="run_prefix",
            )
        if _is_nested_storage_namespace_path(candidate):
            return _nested_storage_namespace_resolution(
                original_ref=text,
                was_legacy_absolute=False,
                resolution_strategy="run_prefix",
            )
        portable = _portable_ref_for_path(candidate, run_id=run_id) or text
        return _resolution(
            original_ref=text,
            portable_ref=portable,
            resolved_path=candidate,
            reason="resolved" if candidate.exists() else "missing_artifact",
            is_safe=True,
            was_legacy_absolute=False,
            resolution_strategy="run_prefix",
        )

    if text.startswith(STORAGE_PREFIX):
        relative = text[len(STORAGE_PREFIX) :]
        if _is_nested_storage_namespace_ref(relative):
            return _nested_storage_namespace_resolution(
                original_ref=text,
                was_legacy_absolute=False,
                resolution_strategy="storage_prefix",
            )
        candidate = _safe_join(constants.STORAGE_ROOT.parent, relative)
        if candidate is None:
            return _resolution(
                original_ref=text,
                portable_ref=None,
                resolved_path=None,
                reason="unsafe_artifact_reference",
                is_safe=False,
                was_legacy_absolute=False,
                resolution_strategy="storage_prefix",
            )
        if _is_nested_storage_namespace_path(candidate):
            return _nested_storage_namespace_resolution(
                original_ref=text,
                was_legacy_absolute=False,
                resolution_strategy="storage_prefix",
            )
        portable_relative = relative.replace("\\", "/")
        portable = _portable_ref_for_path(candidate, run_id=run_id) or f"{STORAGE_PREFIX}{portable_relative}"
        return _resolution(
            original_ref=text,
            portable_ref=portable,
            resolved_path=candidate,
            reason="resolved" if candidate.exists() else "missing_artifact",
            is_safe=True,
            was_legacy_absolute=False,
            resolution_strategy="storage_prefix",
        )

    if text.startswith(PROJECT_PREFIX):
        relative = text[len(PROJECT_PREFIX) :]
        if _is_nested_storage_namespace_ref(relative, project_relative=True):
            return _nested_storage_namespace_resolution(
                original_ref=text,
                was_legacy_absolute=False,
                resolution_strategy="project_prefix",
            )
        candidate = _safe_join(constants.PROJECT_ROOT, relative)
        if candidate is None:
            return _resolution(
                original_ref=text,
                portable_ref=None,
                resolved_path=None,
                reason="unsafe_artifact_reference",
                is_safe=False,
                was_legacy_absolute=False,
                resolution_strategy="project_prefix",
            )
        if _is_nested_storage_namespace_path(candidate):
            return _nested_storage_namespace_resolution(
                original_ref=text,
                was_legacy_absolute=False,
                resolution_strategy="project_prefix",
            )
        portable_relative = relative.replace("\\", "/")
        portable = _portable_ref_for_path(candidate, run_id=run_id) or f"{PROJECT_PREFIX}{portable_relative}"
        return _resolution(
            original_ref=text,
            portable_ref=portable,
            resolved_path=candidate,
            reason="resolved" if candidate.exists() else "missing_artifact",
            is_safe=True,
            was_legacy_absolute=False,
            resolution_strategy="project_prefix",
        )

    if _is_absolute_like(text):
        path = Path(text)
        if _path_is_trusted(path):
            if _is_nested_storage_namespace_path(path):
                return _nested_storage_namespace_resolution(
                    original_ref=text,
                    was_legacy_absolute=True,
                    resolution_strategy="trusted_absolute",
                )
            portable = _portable_ref_for_path(path, run_id=run_id)
            return _resolution(
                original_ref=text,
                portable_ref=portable,
                resolved_path=path.resolve(),
                reason="resolved" if path.exists() else "missing_artifact",
                is_safe=True,
                was_legacy_absolute=True,
                resolution_strategy="trusted_absolute",
            )
        legacy_relative = _legacy_storage_relative(text)
        if legacy_relative is not None:
            candidate = _safe_join(constants.STORAGE_ROOT, legacy_relative)
            if candidate is None:
                return _resolution(
                    original_ref=text,
                    portable_ref=None,
                    resolved_path=None,
                    reason="unsafe_artifact_reference",
                    is_safe=False,
                    was_legacy_absolute=True,
                    resolution_strategy="legacy_storage_suffix",
                )
            if _is_nested_storage_namespace_path(candidate):
                return _nested_storage_namespace_resolution(
                    original_ref=text,
                    was_legacy_absolute=True,
                    resolution_strategy="legacy_storage_suffix",
                )
            portable = _portable_ref_for_path(candidate, run_id=run_id)
            return _resolution(
                original_ref=text,
                portable_ref=portable,
                resolved_path=candidate,
                reason="resolved" if candidate.exists() else "missing_artifact",
                is_safe=True,
                was_legacy_absolute=True,
                resolution_strategy="legacy_storage_suffix",
            )
        return _resolution(
            original_ref=text,
            portable_ref=None,
            resolved_path=None,
            reason="unsafe_artifact_reference",
            is_safe=False,
            was_legacy_absolute=True,
            resolution_strategy="absolute_outside_trusted_roots",
        )

    candidate = _safe_join(constants.PROJECT_ROOT, text)
    if candidate is None:
        return _resolution(
            original_ref=text,
            portable_ref=None,
            resolved_path=None,
            reason="unsafe_artifact_reference",
            is_safe=False,
            was_legacy_absolute=False,
            resolution_strategy="repo_relative",
        )
    if _is_nested_storage_namespace_path(candidate):
        return _nested_storage_namespace_resolution(
            original_ref=text,
            was_legacy_absolute=False,
            resolution_strategy="repo_relative",
        )
    portable = _portable_ref_for_path(candidate, run_id=run_id) or text.replace("\\", "/")
    return _resolution(
        original_ref=text,
        portable_ref=portable,
        resolved_path=candidate,
        reason="resolved" if candidate.exists() else "missing_artifact",
        is_safe=True,
        was_legacy_absolute=False,
        resolution_strategy="repo_relative",
    )


def is_legacy_absolute_path(value: str | None) -> bool:
    return bool(value and _is_absolute_like(str(value).strip()))


def is_portable_ref(value: str | None) -> bool:
    if value is None:
        return False
    text = str(value).strip()
    if not text or _is_url(text) or _is_absolute_like(text):
        return False
    if text.startswith(RUN_PREFIX):
        return _relative_parts(text.split(":", 1)[1]) is not None
    if text.startswith(STORAGE_PREFIX):
        relative = text.split(":", 1)[1]
        return _relative_parts(relative) is not None and not _is_nested_storage_namespace_ref(relative)
    if text.startswith(PROJECT_PREFIX):
        relative = text.split(":", 1)[1]
        return _relative_parts(relative) is not None and not _is_nested_storage_namespace_ref(relative, project_relative=True)
    if _is_nested_storage_namespace_ref(text, project_relative=True):
        return False
    return _relative_parts(text) is not None


def normalize_path_to_ref(
    path: str | Path | None,
    *,
    repo_root: str | Path | None = None,
    storage_roots: list[str | Path] | tuple[str | Path, ...] | None = None,
    run_id: str | None = None,
) -> str | None:
    if path is None:
        return None
    if repo_root is None and storage_roots is None:
        return normalize_path_reference(str(path), run_id=run_id)
    return _portable_ref_for_path_with_roots(
        path,
        repo_root=repo_root,
        storage_roots=storage_roots,
        run_id=run_id,
    )


def resolve_ref_to_path(
    ref: str | None,
    *,
    repo_root: str | Path | None = None,
    storage_roots: list[str | Path] | tuple[str | Path, ...] | None = None,
    run_id: str | None = None,
) -> Path | None:
    if ref is None or not str(ref).strip():
        return None
    text = str(ref).strip()
    if repo_root is None and storage_roots is None:
        resolved = resolve_artifact_reference(text, run_id=run_id)
        return Path(resolved.resolved_path) if resolved.resolved_path else None

    allowed_roots = _custom_allowed_roots(repo_root, storage_roots)
    if not allowed_roots:
        return None
    storage_root = Path((storage_roots or [])[0]).expanduser().resolve() if storage_roots else None
    if text.startswith(RUN_PREFIX):
        if storage_root is None or not run_id:
            return None
        candidate = _safe_join_custom(storage_root / run_id, text[len(RUN_PREFIX) :], allowed_roots)
        return None if candidate and _is_nested_storage_namespace_path_with_roots(candidate, storage_roots) else candidate
    if text.startswith(STORAGE_PREFIX):
        if storage_root is None:
            return None
        relative = text[len(STORAGE_PREFIX) :]
        if _is_nested_storage_namespace_ref(relative):
            return None
        candidate = _safe_join_custom(storage_root.parent, relative, allowed_roots)
        return None if candidate and _is_nested_storage_namespace_path_with_roots(candidate, storage_roots) else candidate
    if text.startswith(PROJECT_PREFIX):
        if repo_root is None:
            return None
        relative = text[len(PROJECT_PREFIX) :]
        if _is_nested_storage_namespace_ref(relative, project_relative=True):
            return None
        candidate = _safe_join_custom(Path(repo_root).expanduser().resolve(), relative, allowed_roots)
        return None if candidate and _is_nested_storage_namespace_path_with_roots(candidate, storage_roots) else candidate
    if _is_absolute_like(text):
        path = Path(text).expanduser().resolve()
        if _path_is_within_roots(path, allowed_roots):
            return None if _is_nested_storage_namespace_path_with_roots(path, storage_roots) else path
        legacy_relative = _legacy_storage_relative(text)
        if legacy_relative is not None and storage_root is not None:
            candidate = _safe_join_custom(storage_root, legacy_relative, allowed_roots)
            return None if candidate and _is_nested_storage_namespace_path_with_roots(candidate, storage_roots) else candidate
        return None
    if repo_root is None:
        return None
    if _is_nested_storage_namespace_ref(text, project_relative=True):
        return None
    candidate = _safe_join_custom(Path(repo_root).expanduser().resolve(), text, allowed_roots)
    return None if candidate and _is_nested_storage_namespace_path_with_roots(candidate, storage_roots) else candidate


def validate_ref_within_allowed_roots(
    ref_or_path: str | Path | None,
    *,
    repo_root: str | Path | None = None,
    storage_roots: list[str | Path] | tuple[str | Path, ...] | None = None,
    run_id: str | None = None,
) -> bool:
    if repo_root is None and storage_roots is None:
        resolved = resolve_artifact_reference(str(ref_or_path) if ref_or_path is not None else None, run_id=run_id)
        return bool(resolved.is_safe and resolved.resolved_path)
    return resolve_ref_to_path(
        str(ref_or_path) if ref_or_path is not None else None,
        repo_root=repo_root,
        storage_roots=storage_roots,
        run_id=run_id,
    ) is not None


def compute_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_sha256(path: str | Path, expected_sha256: str | None) -> bool:
    expected = str(expected_sha256 or "").strip().lower()
    if not expected:
        return True
    return compute_sha256(path).lower() == expected


def explain_resolution_failure(ref_or_path: str | None, *, run_id: str | None = None) -> str:
    resolved = resolve_artifact_reference(ref_or_path, run_id=run_id)
    if resolved.is_safe and resolved.exists:
        return "resolved"
    return resolved.reason


def validate_artifact_reference(
    value: str | None,
    *,
    run_id: str | None = None,
    expected_sha256: str | None = None,
) -> ArtifactReferenceValidation:
    resolution = resolve_artifact_reference(value, run_id=run_id)
    actual_sha256: str | None = None
    sha256_valid: bool | None = None
    expected = str(expected_sha256 or "").strip().lower() or None

    if not resolution.is_safe:
        status = "nested_storage_namespace" if resolution.reason == "nested_storage_namespace" else "outside_allowed_roots"
    elif resolution.reason in {"missing_artifact_reference", "missing_run_id"}:
        status = "unresolved"
    elif resolution.reason == "external_url_reference":
        status = "unsupported_format"
    elif resolution.reason == "missing_artifact":
        status = "missing_artifact"
    elif not resolution.resolved_path:
        status = "unresolved"
    else:
        if expected:
            actual_sha256 = compute_sha256(resolution.resolved_path)
            sha256_valid = actual_sha256.lower() == expected
        if expected and not sha256_valid:
            status = "hash_mismatch"
        elif resolution.was_legacy_absolute and resolution.portable_ref and str(value or "").strip() != resolution.portable_ref:
            status = "convertible_legacy_absolute"
        elif resolution.was_legacy_absolute:
            status = "valid_legacy_absolute"
        else:
            status = "valid_portable"

    return ArtifactReferenceValidation(
        resolution=resolution,
        status=status,
        expected_sha256=expected,
        actual_sha256=actual_sha256,
        sha256_valid=sha256_valid,
        reason=status if status != "valid_portable" else resolution.reason,
    )


def display_artifact_reference(value: str | None, *, run_id: str | None = None) -> str:
    result = resolve_artifact_reference(value, run_id=run_id)
    if not result.original_ref:
        return "-"
    if not result.is_safe:
        return "unsafe artifact reference"
    if result.reason == "missing_artifact" and result.display_ref != "-":
        return f"{result.display_ref} (missing)"
    return result.display_ref


def _legacy_run_relative(path: str, run_id: str | None) -> PurePath | None:
    if not run_id:
        return None
    token = run_id.lower()
    parts = PurePath(path).parts
    lowered = [part.lower() for part in parts]
    if token not in lowered:
        return None
    index = lowered.index(token)
    tail = parts[index + 1 :]
    return PurePath(*tail) if tail else PurePath()


def normalize_path_reference(value: str | None, *, run_id: str | None = None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or _is_url(text):
        return value

    artifact = resolve_artifact_reference(text, run_id=run_id)
    if artifact.is_safe and artifact.portable_ref and not _is_url(artifact.portable_ref):
        return artifact.portable_ref
    return value


def resolve_path_reference(value: str | None, *, run_id: str | None = None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or _is_url(text):
        return value

    artifact = resolve_artifact_reference(text, run_id=run_id)
    if artifact.is_safe and artifact.resolved_path:
        return artifact.resolved_path
    if _prefixed(text) or _is_absolute_like(text):
        return value

    run_root = _run_root(run_id)
    if text in {RUN_PREFIX.rstrip(":"), f"{RUN_PREFIX}."}:
        return str(run_root) if run_root is not None else value
    if text.startswith(RUN_PREFIX):
        if run_root is None:
            return value
        return str((run_root / text[len(RUN_PREFIX) :]).resolve())
    if text.startswith(STORAGE_PREFIX):
        return str((constants.STORAGE_ROOT.parent / text[len(STORAGE_PREFIX) :]).resolve())
    if text.startswith(PROJECT_PREFIX):
        return str((constants.PROJECT_ROOT / text[len(PROJECT_PREFIX) :]).resolve())

    path = Path(text)
    legacy_run = _legacy_run_relative(text, run_id)
    if legacy_run is not None and run_root is not None:
        return str((run_root / legacy_run).resolve())
    return str(path) if path.is_absolute() else value


def _transform_payload(payload: Any, *, run_id: str | None, resolve: bool) -> Any:
    transformer = resolve_path_reference if resolve else normalize_path_reference
    if isinstance(payload, dict):
        transformed: dict[str, Any] = {}
        for key, value in payload.items():
            if key in _PATH_KEYS and isinstance(value, str):
                transformed[key] = transformer(value, run_id=run_id)
                continue
            if key in _LIST_PATH_KEYS and isinstance(value, list):
                transformed[key] = [
                    transformer(item, run_id=run_id) if isinstance(item, str) else _transform_payload(item, run_id=run_id, resolve=resolve)
                    for item in value
                ]
                continue
            transformed[key] = _transform_payload(value, run_id=run_id, resolve=resolve)
        return transformed
    if isinstance(payload, list):
        return [_transform_payload(item, run_id=run_id, resolve=resolve) for item in payload]
    return payload


def normalize_payload_references(payload: Any, *, run_id: str | None = None) -> Any:
    return _transform_payload(payload, run_id=run_id, resolve=False)


def resolve_payload_references(payload: Any, *, run_id: str | None = None) -> Any:
    return _transform_payload(payload, run_id=run_id, resolve=True)
