from __future__ import annotations

import difflib
import hashlib
import mimetypes
import re
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from app.repositories.artifact_refs import normalize_payload_references, resolve_payload_references
from app.repositories import metadata_index
from app.repositories.file_locks import file_lock
from app.domain.temporal_memory import digest
from app.repositories.files import ensure_dir, iso_now_utc, read_json, read_text, write_json, write_text
from app.repositories.runs import build_run_paths, load_run_record
from app.services import reporting

VERSIONS_MANIFEST_FILENAME = "report_versions.json"
VERSIONS_DIRNAME = "versions"
CANONICAL_EXPORT_FORMATS = {
    "report_markdown": "markdown",
    "report_pdf": "pdf",
    "journal_pdf": "journal_pdf",
}
BINARY_EXPORT_FORMATS = {"pdf", "journal_pdf"}


def _content_hash(content: str) -> str:
    return hashlib.sha1(content.encode("utf-8")).hexdigest()[:12]


def _content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _versions_manifest_path(run_id: str) -> Path:
    return build_run_paths(run_id).reports_dir / VERSIONS_MANIFEST_FILENAME


def _versions_dir(run_id: str) -> Path:
    return build_run_paths(run_id).reports_dir / VERSIONS_DIRNAME


def _exports_manifest_path(run_id: str) -> Path:
    return build_run_paths(run_id).reports_dir / "exports.json"


def _read_manifest(run_id: str) -> list[dict[str, Any]]:
    load_run_record(run_id)
    path = _versions_manifest_path(run_id)
    raw = json.loads(path.read_bytes().decode("utf-8")) if path.exists() else []
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise ValueError("report_version_manifest_invalid")
    return resolve_payload_references(raw, run_id=run_id)


def _read_export_manifest(run_id: str) -> list[dict[str, Any]]:
    payload = resolve_payload_references(read_json(_exports_manifest_path(run_id), default=[]) or [], run_id=run_id)
    return [item for item in payload if isinstance(item, dict)]


def _write_manifest(run_id: str, items: list[dict[str, Any]]) -> None:
    write_json(_versions_manifest_path(run_id), normalize_payload_references(items, run_id=run_id))
    metadata_index.refresh_run(run_id, reason="report_version_write")


def _snapshot_relative_path(version_number: int, content_hash: str) -> str:
    return f"{VERSIONS_DIRNAME}/v{version_number:04d}-{content_hash}.md"


def _version_title(run_id: str, version_number: int, source: str) -> str:
    run = load_run_record(run_id)
    topic = str(run.get("topic") or run_id)
    return f"{topic} completed report v{version_number} ({source})"


def _source_providers(run: dict[str, Any]) -> list[str]:
    request = run.get("request") if isinstance(run.get("request"), dict) else {}
    source_settings = request.get("source_settings") if isinstance(request.get("source_settings"), dict) else {}
    enabled = source_settings.get("enabledProviders")
    if isinstance(enabled, list):
        providers = [str(provider).strip() for provider in enabled if str(provider).strip()]
        if providers:
            return providers

    fallback = str(source_settings.get("defaultProvider") or request.get("source_provider") or run.get("source_provider") or "").strip()
    if fallback:
        return [fallback]

    profile = run.get("profile") if isinstance(run.get("profile"), dict) else {}
    sources = profile.get("sources") if isinstance(profile.get("sources"), dict) else {}
    return [str(provider).strip() for provider in sources.keys() if str(provider).strip()]


def _version_metadata(
    run_id: str,
    *,
    source: str,
    source_artifact_path: str,
    source_export_id: str | None,
    snapshot_path: str,
) -> dict[str, Any]:
    run = load_run_record(run_id)
    artifacts = run.get("artifacts") if isinstance(run.get("artifacts"), dict) else {}
    schedule_context = run.get("schedule_context") if isinstance(run.get("schedule_context"), dict) else {}
    outline_path = str(artifacts.get("outline_json") or build_run_paths(run_id).reports_dir / reporting.OUTLINE_FILENAME)
    preview_path = str(artifacts.get("preview_markdown") or build_run_paths(run_id).reports_dir / reporting.PREVIEW_FILENAME)
    outline_hash = reporting.compute_outline_draft_hash(run_id=run_id) if Path(outline_path).exists() else None
    providers = _source_providers(run)
    workspace = run.get("workspace") if isinstance(run.get("workspace"), dict) else {}
    topic_id = workspace.get("topic_id") or run.get("topic_profile_id")
    return {
        "report_id": run_id,
        "topic_id": topic_id,
        "corpus_snapshot_id": workspace.get("snapshot_id"),
        "settings_version": workspace.get("settings_version"),
        "settings_digest": workspace.get("settings_digest"),
        "source_manifest_digest": workspace.get("source_manifest_digest"),
        "evidence_set_digest": workspace.get("evidence_set_digest"),
        "created_by": run.get("owner_user_id") or run.get("created_by"),
        "trigger_source": run.get("source") or schedule_context.get("trigger"),
        "artifact_references": {
            "source_artifact_path": source_artifact_path,
            "source_export_id": source_export_id,
            "markdown_snapshot": snapshot_path,
            "report_markdown": artifacts.get("report_markdown"),
            "preview_markdown": artifacts.get("preview_markdown"),
            "outline_json": artifacts.get("outline_json"),
        },
        "markdown_content_reference": snapshot_path,
        "source_outline_id": outline_path if Path(outline_path).exists() else None,
        "source_outline": {
            "path": outline_path if Path(outline_path).exists() else None,
            "version_hash": outline_hash,
        },
        "citation_provenance": {
            "source_outline_path": outline_path if Path(outline_path).exists() else None,
            "source_preview_path": preview_path if Path(preview_path).exists() else None,
            "topic_id": topic_id,
            "corpus_snapshot_id": workspace.get("snapshot_id"),
            "evidence_set_digest": workspace.get("evidence_set_digest"),
        },
        "provider_source_coverage": {
            "providers": providers,
            "provider_count": len(providers),
            "source": "run_request_or_profile",
        },
    }


def record_completed_report_version(
    run_id: str,
    *,
    source: str,
    source_artifact_path: str,
    content: str | None = None,
    source_export_id: str | None = None,
    generation_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    with version_write_lock(run_id):
        return _record_completed_report_version(
            run_id, source=source, source_artifact_path=source_artifact_path, content=content,
            source_export_id=source_export_id, generation_metadata=generation_metadata)


@contextmanager
def version_write_lock(run_id: str) -> Iterator[None]:
    load_run_record(run_id)
    root = ensure_dir(build_run_paths(run_id).reports_dir)
    with file_lock(root / ".report-versions.lock"):
        yield


def _record_completed_report_version(
    run_id: str,
    *,
    source: str,
    source_artifact_path: str,
    content: str | None = None,
    source_export_id: str | None = None,
    generation_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    run = load_run_record(run_id)
    if run.get("status") != "completed":
        return None
    source_path = Path(source_artifact_path)
    resolved_content = content if content is not None else read_text(source_path)
    if not resolved_content.strip():
        raise FileNotFoundError(f"No completed report content found for run {run_id}")

    safe_generation_metadata: dict[str, Any] = {}
    if generation_metadata:
        allowed_fields = {
            "generation_mode",
            "provider_id",
            "execution_record_id",
            "accepted_output_digest",
            "evidence_references",
            "baseline_version_id",
            "acceptance_record_digest",
            "display_label",
        }
        unexpected = set(generation_metadata) - allowed_fields
        if unexpected:
            raise ValueError("Unsupported report-version generation metadata.")
        safe_generation_metadata = {
            key: generation_metadata.get(key)
            for key in allowed_fields
            if key in generation_metadata
        }

    manifest = _read_manifest(run_id)
    version_number = max((int(item.get("version_number") or 0) for item in manifest), default=0) + 1
    content_hash = _content_hash(resolved_content)
    relative_snapshot_path = _snapshot_relative_path(version_number, content_hash)
    snapshot_path = _versions_dir(run_id) / Path(relative_snapshot_path).name
    ensure_dir(snapshot_path.parent)
    write_text(snapshot_path, resolved_content)

    now = iso_now_utc()
    record = {
        "version_id": f"{run_id}-report-v{version_number:04d}-{content_hash}",
        "version_number": version_number,
        "run_id": run_id,
        "title": _version_title(run_id, version_number, source),
        "created_at": now,
        "source": source,
        "source_export_id": source_export_id,
        "source_artifact_path": source_artifact_path,
        "snapshot_path": relative_snapshot_path,
        "content_hash": content_hash,
        "content_sha256": _content_sha256(resolved_content),
        "byte_size": len(resolved_content.encode("utf-8")),
        "status": "completed",
        "immutable": True,
        "provenance": {
            "source_run_id": run_id,
            "source_export_id": source_export_id,
            "source_artifact_path": source_artifact_path,
            "created_from": source,
        },
    }
    record.update(
        _version_metadata(
            run_id,
            source=source,
            source_artifact_path=source_artifact_path,
            source_export_id=source_export_id,
            snapshot_path=relative_snapshot_path,
        )
    )
    if safe_generation_metadata:
        record.update(safe_generation_metadata)
        record["provenance"] = {
            **record["provenance"],
            "generation_mode": safe_generation_metadata.get("generation_mode"),
            "provider_id": safe_generation_metadata.get("provider_id"),
            "execution_record_id": safe_generation_metadata.get("execution_record_id"),
            "accepted_output_digest": safe_generation_metadata.get("accepted_output_digest"),
            "evidence_references": list(safe_generation_metadata.get("evidence_references") or []),
            "baseline_version_id": safe_generation_metadata.get("baseline_version_id"),
            "acceptance_record_digest": safe_generation_metadata.get("acceptance_record_digest"),
        }
    manifest.append(record)
    # Publish body + bound citation payload together through the atomic manifest
    # replacement. An interrupted write can leave an unreferenced file, never a
    # visible half-version. The lock also prevents lost concurrent appends.
    citations = normalize_payload_references(reporting.capture_version_citations(run_id, resolved_content), run_id=run_id)
    citations.update(schema_version="REPORT_CITATIONS_V1", run_id=run_id,
                     version_id=record["version_id"], content_sha256=record["content_sha256"])
    citation_filename = str(Path(relative_snapshot_path).with_suffix(".citations.json")).replace("\\", "/")
    write_json(build_run_paths(run_id).reports_dir / citation_filename, citations)
    record["citations_snapshot"] = {"filename": citation_filename, "sha256": digest(citations)}
    _write_manifest(run_id, manifest)
    return record


def snapshot_completed_report_if_available(run_id: str) -> dict[str, Any] | None:
    run = load_run_record(run_id)
    if run.get("status") != "completed":
        return None
    report_path = Path(str(run.get("artifacts", {}).get("report_markdown") or ""))
    if not report_path.is_file():
        candidates = reporting._report_markdown_candidates(run_id)
        report_path = candidates[-1] if candidates else Path("")
    if not report_path.is_file():
        return None
    return record_completed_report_version(
        run_id,
        source="report_markdown",
        source_artifact_path=str(report_path),
        content=read_text(report_path),
    )


def list_report_versions(run_id: str) -> dict[str, Any]:
    items = [_decorate_version_for_response(run_id, item) for item in sorted(_read_manifest(run_id), key=lambda item: int(item.get("version_number") or 0))]
    warnings: list[str] = []
    for item in items:
        snapshot_path = _resolve_snapshot_path(run_id, str(item.get("snapshot_path") or ""))
        if snapshot_path is None or not snapshot_path.is_file():
            warnings.append(f"Version snapshot is missing: {item.get('version_id')}")
    return {"run_id": run_id, "report_id": run_id, "order": "version_number_ascending", "items": items, "total": len(items), "warnings": warnings}


def _decorate_version_for_response(run_id: str, item: dict[str, Any]) -> dict[str, Any]:
    version = dict(item)
    snapshot_path = _resolve_snapshot_path(run_id, str(version.get("snapshot_path") or ""))
    snapshot_available = snapshot_path is not None and snapshot_path.is_file()
    artifact_references = dict(version.get("artifact_references") or {})
    artifact_references.setdefault("markdown_snapshot", version.get("snapshot_path"))
    version["artifact_references"] = artifact_references
    version["artifact_availability"] = {
        "markdown_snapshot": snapshot_available,
        "source_artifact": Path(str(version.get("source_artifact_path") or "")).is_file(),
    }
    version["download_available"] = snapshot_available
    version["artifacts"] = _version_export_artifacts(run_id, version)
    # `_read_manifest` resolves stored references to native paths so availability
    # and artifact metadata can be computed. Those native paths must not cross the
    # API boundary, so the decorated record is serialized back to portable
    # references once every filesystem-derived field has been filled in.
    return normalize_payload_references(version, run_id=run_id)


def _canonical_export_format(export_format: str) -> str:
    return CANONICAL_EXPORT_FORMATS.get(export_format, export_format)


def _artifact_mime_type(path: Path, export_format: str) -> str:
    if _canonical_export_format(export_format) == "markdown":
        return "text/markdown"
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decorate_export_artifact_for_version(run_id: str, record: dict[str, Any]) -> dict[str, Any]:
    output = Path(str(record.get("storage_path") or record.get("output") or ""))
    export_format = str(record.get("export_format") or record.get("format") or "other")
    file_exists = output.is_file()
    status = str(record.get("status") or "unavailable")
    if status in {"ready", "completed"}:
        status = "completed" if file_exists else "unavailable"
    decorated = dict(record)
    decorated.setdefault("artifact_id", decorated.get("export_id"))
    decorated.setdefault("report_id", run_id)
    decorated.setdefault("run_id", run_id)
    decorated["format"] = _canonical_export_format(export_format)
    decorated.setdefault("mime_type", _artifact_mime_type(output, export_format))
    decorated.setdefault("file_name", output.name if str(output) else None)
    decorated.setdefault("storage_path", decorated.get("output"))
    decorated["status"] = status
    if file_exists:
        decorated["size_bytes"] = output.stat().st_size
        decorated["sha256"] = _file_sha256(output)
    else:
        decorated["size_bytes"] = None
        decorated["sha256"] = None
        if status == "unavailable":
            decorated["error_message"] = decorated.get("error_message") or "Export artifact file is missing."
    decorated["download_available"] = status == "completed" and file_exists
    return decorated


def _version_export_artifacts(run_id: str, version: dict[str, Any]) -> list[dict[str, Any]]:
    version_id = str(version.get("version_id") or "")
    source_export_id = str(version.get("source_export_id") or "")
    artifacts: list[dict[str, Any]] = []
    for record in _read_export_manifest(run_id):
        if (
            str(record.get("version_id") or "") == version_id
            or str(record.get("source_markdown_version_id") or "") == version_id
            or (source_export_id and str(record.get("export_id") or "") == source_export_id)
        ):
            artifacts.append(_decorate_export_artifact_for_version(run_id, record))
    return sorted(artifacts, key=lambda item: str(item.get("created_at") or ""))


SNAPSHOT_WARNINGS = {
    "SNAPSHOT_MISSING": "완료 보고서 버전 snapshot 파일이 없습니다.",
    "SNAPSHOT_UNREADABLE": "저장된 버전 파일을 읽을 수 없습니다. 접근 상태와 문자 인코딩을 확인하세요.",
    "DIGEST_MISMATCH": "저장된 버전 파일이 기록된 해시와 다릅니다. 원래 버전의 본문으로 표시할 수 없습니다.",
    "DIGEST_UNAVAILABLE": "이 버전에는 검증할 해시가 기록되어 있지 않습니다. 원래 본문인지 확인할 수 없습니다.",
}


def _verified_snapshot(run_id: str, version: dict[str, Any]) -> tuple[str | None, str]:
    path = _resolve_snapshot_path(run_id, str(version.get("snapshot_path") or ""))
    if path is None:
        return None, "SNAPSHOT_MISSING"
    try:
        # The historical text writer expands LF to CRLF on Windows. Accept the
        # exact stored text or that one reversible expansion, and return only the
        # form whose digest matches. Strict decoding never discards bad bytes.
        content = path.read_bytes().decode("utf-8", errors="strict")
    except FileNotFoundError:
        return None, "SNAPSHOT_MISSING"
    except (OSError, UnicodeError):
        return None, "SNAPSHOT_UNREADABLE"
    sha256 = version.get("content_sha256")
    legacy = version.get("content_hash")
    if not sha256 and not legacy:
        return None, "DIGEST_UNAVAILABLE"
    for candidate in (content, content.replace("\r\n", "\n")):
        if sha256 and _content_sha256(candidate) == sha256:
            return candidate, "SHA256_VERIFIED"
        if not sha256 and _content_hash(candidate) == legacy:
            return candidate, "LEGACY_HASH_VERIFIED"
    return None, "DIGEST_MISMATCH"


def get_report_version_detail(run_id: str, version_id: str) -> dict[str, Any]:
    version = _get_version(run_id, version_id)
    if version is None:
        raise FileNotFoundError(f"Report version not found for run {run_id}: {version_id}")
    version = _decorate_version_for_response(run_id, version)
    content, integrity = _verified_snapshot(run_id, version)
    if content is None:
        version["download_available"] = False
    citations = _verified_citations(run_id, version)
    review_binding = digest([version_id, version.get("content_sha256"), version.get("content_hash"), version.get("citations_snapshot")])
    return {
        "run_id": run_id,
        "report_id": run_id,
        "version": version,
        "content_reference": version.get("markdown_content_reference") or version.get("snapshot_path"),
        "content": content,
        "content_integrity": integrity,
        "citations": citations,
        "review_binding": review_binding,
        "review_eligible": content is not None and citations["status"] not in {"UNREADABLE", "DIGEST_MISMATCH"},
        "source_refs": {
            "run_id": version.get("run_id"),
            "report_id": version.get("report_id") or run_id,
            "topic_id": version.get("topic_id"),
            "source_export_id": version.get("source_export_id"),
            "source_artifact_path": version.get("source_artifact_path"),
        },
        "warnings": [] if content is not None else [SNAPSHOT_WARNINGS[integrity]],
    }


def _verified_citations(run_id: str, version: dict[str, Any]) -> dict[str, Any]:
    reference = version.get("citations_snapshot")
    empty: dict[str, Any] = {"items": [], "warnings": []}
    if not reference:
        return {**empty, "status": "NOT_RECORDED"}
    path = _resolve_snapshot_path(run_id, str(reference.get("filename") or ""))
    try:
        if path is None:
            return {**empty, "status": "UNREADABLE"}
        payload = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {**empty, "status": "UNREADABLE"}
    if (not isinstance(payload, dict) or digest(payload) != reference.get("sha256")
            or payload.get("schema_version") != "REPORT_CITATIONS_V1"
            or payload.get("version_id") != version["version_id"] or payload.get("run_id") != run_id
            or payload.get("content_sha256") != version.get("content_sha256")):
        return {**empty, "status": "DIGEST_MISMATCH"}
    return {**payload, "status": "VERIFIED" if payload["status"] == "CAPTURED" else payload["status"]}


def _resolve_snapshot_path(run_id: str, snapshot_path: str) -> Path | None:
    """Resolve a version snapshot reference under the run's own reports directory.

    Snapshot references are stored relative (`versions/v0001-<hash>.md`), but a
    historical record can still carry an absolute path from an older checkout,
    so both spellings are accepted — and both must then land inside this run's
    reports directory. Anything resolving outside it is refused instead of read.

    Previously an absolute reference was returned untouched and a relative one
    was joined without a containment check, so either spelling could address a
    file anywhere on disk. Nothing untrusted reaches here today (bundle import
    drops `snapshot_path`, and values are generated by `_snapshot_relative_path`),
    but this is the one reference in this module that never passes through
    `artifact_refs`, and it should fail closed like every sibling resolver.

    Returns None when the reference is empty, unreadable or out of bounds. Every
    caller already treats an absent snapshot as a truthful "unavailable" state,
    so a refused reference degrades exactly the same way.
    """
    text = str(snapshot_path or "").strip()
    if not text:
        return None
    reports_dir = build_run_paths(run_id).reports_dir
    candidate = Path(text)
    try:
        root = reports_dir.resolve()
        resolved = (candidate if candidate.is_absolute() else reports_dir / candidate).resolve()
    except OSError:
        return None
    return resolved if resolved.is_relative_to(root) else None


def _get_version(run_id: str, version_id: str) -> dict[str, Any] | None:
    if not version_id:
        return None
    return next((item for item in _read_manifest(run_id) if item.get("version_id") == version_id), None)


def _slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return text or "section"


def _sections(content: str) -> list[dict[str, Any]]:
    parsed = reporting._parse_report_sections(content)
    sections: list[dict[str, Any]] = []
    for index, (title, body) in enumerate(parsed.items(), start=1):
        display_title = "Executive Summary" if title == "_intro" else title
        body_text = body.strip()
        sections.append(
            {
                "section_id": "executive-summary" if title == "_intro" else _slug(display_title),
                "title": display_title,
                "order": index,
                "body": body_text,
                "body_hash": _content_hash(body_text) if body_text else None,
                "excerpt": _excerpt(body_text),
            }
        )
    return sections


def _excerpt(value: str, limit: int = 320) -> str | None:
    cleaned = re.sub(r"\s+", " ", value).strip()
    if not cleaned:
        return None
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1].rstrip() + "…"


def _similarity(left: str, right: str) -> float | None:
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return round(difflib.SequenceMatcher(a=left, b=right).ratio(), 3)


def _section_diffs(from_sections: list[dict[str, Any]], to_sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from_map = {item["section_id"]: item for item in from_sections}
    to_map = {item["section_id"]: item for item in to_sections}
    ordered_ids: list[str] = []
    for item in [*from_sections, *to_sections]:
        if item["section_id"] not in ordered_ids:
            ordered_ids.append(item["section_id"])

    diffs: list[dict[str, Any]] = []
    for section_id in ordered_ids:
        left = from_map.get(section_id)
        right = to_map.get(section_id)
        if left and not right:
            status = "removed"
        elif right and not left:
            status = "added"
        elif left and right and (left["body_hash"] != right["body_hash"] or left["title"] != right["title"] or left["order"] != right["order"]):
            status = "changed"
        else:
            status = "unchanged"
        diffs.append(
            {
                "section_id": section_id,
                "title": (right or left or {}).get("title") or section_id,
                "status": status,
                "from_order": left.get("order") if left else None,
                "to_order": right.get("order") if right else None,
                "from_excerpt": left.get("excerpt") if left else None,
                "to_excerpt": right.get("excerpt") if right else None,
                "body_similarity": _similarity(left.get("body", "") if left else "", right.get("body", "") if right else ""),
            }
        )
    return diffs


def _binary_artifacts_by_format(version: dict[str, Any]) -> dict[str, dict[str, Any]]:
    artifacts = version.get("artifacts") if isinstance(version.get("artifacts"), list) else []
    by_format: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        artifact_format = _canonical_export_format(str(artifact.get("format") or artifact.get("export_format") or ""))
        if artifact_format not in BINARY_EXPORT_FORMATS:
            continue
        current = by_format.get(artifact_format)
        if current is None or str(artifact.get("created_at") or "") >= str(current.get("created_at") or ""):
            by_format[artifact_format] = artifact
    return by_format


def _binary_artifact_summary(artifact: dict[str, Any] | None) -> dict[str, Any] | None:
    if artifact is None:
        return None
    fields = [
        "artifact_id",
        "export_id",
        "format",
        "mime_type",
        "file_name",
        "created_at",
        "size_bytes",
        "sha256",
        "renderer",
        "source_markdown_version_id",
        "source_markdown_hash",
        "status",
        "download_available",
        "error_message",
    ]
    return {field: artifact.get(field) for field in fields}


def _binary_availability(left: dict[str, Any] | None, right: dict[str, Any] | None) -> str:
    left_completed = bool(left and left.get("status") == "completed" and left.get("sha256"))
    right_completed = bool(right and right.get("status") == "completed" and right.get("sha256"))
    if left_completed and right_completed:
        return "both_available"
    if left is None and right is not None:
        return "to_only" if right_completed else "to_unavailable"
    if left is not None and right is None:
        return "from_only" if left_completed else "from_unavailable"
    if left is not None and right is not None:
        if not left_completed and right_completed:
            return "from_unavailable"
        if left_completed and not right_completed:
            return "to_unavailable"
        return "both_unavailable"
    return "none"


def _binary_metadata_changed(left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:
    metadata_fields = [
        "format",
        "size_bytes",
        "sha256",
        "renderer",
        "source_markdown_version_id",
        "source_markdown_hash",
        "created_at",
        "status",
    ]
    return any((left or {}).get(field) != (right or {}).get(field) for field in metadata_fields)


def _binary_hash_equal(left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool | None:
    left_hash = (left or {}).get("sha256")
    right_hash = (right or {}).get("sha256")
    if not left_hash or not right_hash:
        return None
    return left_hash == right_hash


def _binary_artifact_comparison(from_version: dict[str, Any], to_version: dict[str, Any]) -> dict[str, Any]:
    from_artifacts = _binary_artifacts_by_format(from_version)
    to_artifacts = _binary_artifacts_by_format(to_version)
    formats = sorted(set(from_artifacts) | set(to_artifacts))
    items: list[dict[str, Any]] = []
    for artifact_format in formats:
        left = _binary_artifact_summary(from_artifacts.get(artifact_format))
        right = _binary_artifact_summary(to_artifacts.get(artifact_format))
        items.append(
            {
                "format": artifact_format,
                "from": left,
                "to": right,
                "availability": _binary_availability(left, right),
                "hash_equal": _binary_hash_equal(left, right),
                "metadata_changed": _binary_metadata_changed(left, right),
                "comparison_basis": "sha256_metadata",
                "semantic_claim": "none",
                "warning": "Hash difference does not imply semantic difference; no reliable binary text extraction is configured.",
            }
        )
    return {
        "mode": "metadata_hash_only",
        "items": items,
        "text_extraction": {
            "available": False,
            "status": "unavailable",
            "reason": "Reliable binary text extraction is not configured; binary artifacts are compared by metadata and sha256 only.",
        },
        "warnings": [
            "Binary artifact comparison is metadata/hash based only.",
            "Hash difference does not imply semantic difference.",
        ],
    }


def diff_report_versions(run_id: str, *, from_version_id: str | None, to_version_id: str | None) -> dict[str, Any]:
    versions = _read_manifest(run_id)
    if len(versions) < 2 or not from_version_id or not to_version_id:
        return {
            "run_id": run_id,
            "ready": False,
            "mode": "completed_versions",
            "from_version": None,
            "to_version": None,
            "summary": {
                "hash_changed": None,
                "added_sections": 0,
                "removed_sections": 0,
                "changed_sections": 0,
                "unchanged_sections": 0,
            },
            "section_diffs": [],
            "empty_reason": "완료된 보고서 버전이 두 개 이상 있어야 diff를 생성할 수 있습니다.",
            "warnings": [],
        }

    from_version = _get_version(run_id, from_version_id)
    to_version = _get_version(run_id, to_version_id)
    if from_version is None or to_version is None:
        return {
            "run_id": run_id,
            "ready": False,
            "mode": "completed_versions",
            "from_version": from_version,
            "to_version": to_version,
            "summary": {
                "hash_changed": None,
                "added_sections": 0,
                "removed_sections": 0,
                "changed_sections": 0,
                "unchanged_sections": 0,
            },
            "section_diffs": [],
            "empty_reason": "선택한 완료 보고서 버전을 찾을 수 없습니다.",
            "warnings": [],
        }

    # Both records are returned verbatim on every branch below, and `_get_version`
    # reads them through `_read_manifest`, which resolves stored references to
    # native paths. Serialize them back to portable references once, here: past
    # this point nothing needs a native path except the snapshot, and
    # `snapshot_path` is not a `_PATH_KEYS` field so it is left untouched.
    from_version = normalize_payload_references(from_version, run_id=run_id)
    to_version = normalize_payload_references(to_version, run_id=run_id)

    from_content, from_integrity = _verified_snapshot(run_id, from_version)
    to_content, to_integrity = _verified_snapshot(run_id, to_version)
    unavailable = [
        (version, integrity) for version, content, integrity in
        ((from_version, from_content, from_integrity), (to_version, to_content, to_integrity)) if content is None
    ]
    if unavailable:
        # These warnings are returned to the API, so they name the version
        # identity rather than the resolved native path.
        missing = [
            f"{version.get('version_id')}: {SNAPSHOT_WARNINGS[integrity]}" for version, integrity in unavailable
        ]
        return {
            "run_id": run_id,
            "ready": False,
            "mode": "completed_versions",
            "from_version": from_version,
            "to_version": to_version,
            "summary": {
                "hash_changed": None,
                "added_sections": 0,
                "removed_sections": 0,
                "changed_sections": 0,
                "unchanged_sections": 0,
            },
            "section_diffs": [],
            "empty_reason": "선택한 버전의 저장 본문을 검증할 수 없어 비교하지 않았습니다.",
            "warnings": missing,
        }

    from_version = _decorate_version_for_response(run_id, from_version)
    to_version = _decorate_version_for_response(run_id, to_version)
    assert from_content is not None and to_content is not None
    section_diffs = _section_diffs(_sections(from_content), _sections(to_content))
    metadata_changes = _metadata_changes(from_version, to_version)
    citation_source_changed = (from_version.get("citation_provenance") or {}) != (to_version.get("citation_provenance") or {})
    provider_source_changed = (from_version.get("provider_source_coverage") or {}) != (to_version.get("provider_source_coverage") or {})
    summary = {
        "hash_changed": from_version.get("content_hash") != to_version.get("content_hash"),
        "added_sections": sum(1 for item in section_diffs if item["status"] == "added"),
        "removed_sections": sum(1 for item in section_diffs if item["status"] == "removed"),
        "changed_sections": sum(1 for item in section_diffs if item["status"] == "changed"),
        "unchanged_sections": sum(1 for item in section_diffs if item["status"] == "unchanged"),
    }
    return {
        "run_id": run_id,
        "ready": True,
        "mode": "completed_versions",
        "from_version": from_version,
        "to_version": to_version,
        "summary": summary,
        "metadata_changes": metadata_changes,
        "citation_provenance_changes": {
            "changed": citation_source_changed,
            "status": "metadata_reference_only",
            "from": from_version.get("citation_provenance") or {},
            "to": to_version.get("citation_provenance") or {},
        },
        "source_provenance_changes": {
            "changed": provider_source_changed,
            "status": "metadata_reference_only",
            "from": from_version.get("provider_source_coverage") or {},
            "to": to_version.get("provider_source_coverage") or {},
        },
        "semantic_diff_status": {
            "text_diff": "section_level",
            "citation_diff": "metadata_reference_only",
            "source_diff": "metadata_reference_only",
            "binary_artifact_diff": "metadata_hash_only",
        },
        "binary_artifact_comparison": _binary_artifact_comparison(from_version, to_version),
        "section_diffs": section_diffs,
        "empty_reason": None,
        "warnings": [],
    }


def _metadata_changes(from_version: dict[str, Any], to_version: dict[str, Any]) -> dict[str, Any]:
    fields = ["content_hash", "byte_size", "source", "source_export_id", "source_artifact_path", "source_outline_id"]
    changed_fields = [
        field
        for field in fields
        if from_version.get(field) != to_version.get(field)
    ]
    return {
        "changed_fields": changed_fields,
        "content_hash_changed": from_version.get("content_hash") != to_version.get("content_hash"),
        "byte_size_changed": from_version.get("byte_size") != to_version.get("byte_size"),
    }
