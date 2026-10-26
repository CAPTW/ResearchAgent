from __future__ import annotations

import hashlib
import re
import mimetypes
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from app.repositories.artifact_refs import normalize_payload_references, resolve_payload_references
from app.repositories import metadata_index
from app.repositories.files import copy_file, ensure_dir, iso_now_utc, read_json, write_json, write_text
from app.repositories.runs import build_run_paths, load_run_record
from app.services import report_versions, reporting


CANONICAL_FORMATS = {
    "report_markdown": "markdown",
    "report_pdf": "pdf",
    "journal_pdf": "journal_pdf",
}

RENDERERS = {
    "report_markdown": "report_preview_markdown_copy",
    "report_pdf": "completed_report_markdown_reportlab",
    "journal_pdf": "completed_report_markdown_journal_reportlab",
}

def _report_markdown_candidates(run_id: str) -> list[Path]:
    reports_dir = build_run_paths(run_id).reports_dir
    return sorted(
        [
            path
            for path in reports_dir.glob("*.md")
            if path.name != "preview.md" and not path.name.startswith("exported_")
        ]
    )


def _exports_manifest_path(run_id: str) -> Path:
    return build_run_paths(run_id).reports_dir / "exports.json"


def _resolve_cjk_font() -> tuple[str, str | None]:
    candidates = [
        Path(r"C:\Windows\Fonts\malgun.ttf"),
        Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    ]
    for path in candidates:
        if path.is_file():
            return "XRMLCJK", str(path)
    return "Helvetica", None


def _pdf_export_availability() -> dict[str, Any]:
    try:
        import reportlab  # noqa: F401
    except Exception as exc:
        return {
            "available": False,
            "status": "unavailable",
            "reason": f"ReportLab PDF renderer is not installed: {exc}",
        }
    font_name, font_path = _resolve_cjk_font()
    return {
        "available": True,
        "status": "available",
        "reason": None,
        "renderer": RENDERERS["report_pdf"],
        "font": font_name,
        "font_path": font_path,
    }


def _export_capabilities() -> dict[str, dict[str, Any]]:
    pdf = _pdf_export_availability()
    return {
        "report_markdown": {
            "format": "report_markdown",
            "available": True,
            "status": "available",
            "reason": None,
            "renderer": RENDERERS["report_markdown"],
        },
        "report_pdf": {
            "format": "report_pdf",
            "available": bool(pdf.get("available")),
            "status": "available" if pdf.get("available") else "unavailable",
            "reason": pdf.get("reason"),
            "renderer": RENDERERS["report_pdf"],
        },
        "journal_pdf": {
            "format": "journal_pdf",
            "available": bool(pdf.get("available")),
            "status": "available" if pdf.get("available") else "unavailable",
            "reason": pdf.get("reason"),
            "renderer": RENDERERS["journal_pdf"],
            "template_id": "journal_style_report_v1",
            "meaning": "journal_style_report_pdf",
            "source_pdf_handling": "links_only_no_redistribution",
        },
    }


def _plain_markdown_lines(content: str) -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = []
    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            lines.append(("space", ""))
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            level = len(heading.group(1))
            lines.append((f"h{min(level, 3)}", heading.group(2).strip()))
            continue
        bullet = re.match(r"^\s*[-*+]\s+(.*)$", line)
        if bullet:
            lines.append(("bullet", f"• {bullet.group(1).strip()}"))
            continue
        numbered = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        if numbered:
            lines.append(("bullet", f"• {numbered.group(1).strip()}"))
            continue
        cleaned = re.sub(r"`([^`]+)`", r"\1", line)
        cleaned = re.sub(r"\*\*([^*]+)\*\*", r"\1", cleaned)
        cleaned = re.sub(r"\*([^*]+)\*", r"\1", cleaned)
        cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cleaned)
        lines.append(("p", cleaned.strip()))
    return lines


def _render_completed_markdown_pdf(*, markdown_content: str, output_path: Path, title: str) -> None:
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    except Exception as exc:
        raise RuntimeError(f"PDF export unavailable: ReportLab renderer is not installed ({exc})") from exc

    font_name, font_path = _resolve_cjk_font()
    if font_path:
        try:
            pdfmetrics.registerFont(TTFont(font_name, font_path))
        except Exception as exc:
            raise RuntimeError(f"PDF export unavailable: failed to register CJK font {font_path} ({exc})") from exc

    ensure_dir(output_path.parent)
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=title,
        author="AI Research Agent",
        creator="AI Research Agent completed markdown renderer",
        invariant=1,
        pageCompression=0,
    )
    styles = {
        "h1": ParagraphStyle("h1", fontName=font_name, fontSize=18, leading=24, spaceAfter=8),
        "h2": ParagraphStyle("h2", fontName=font_name, fontSize=15, leading=21, spaceAfter=7),
        "h3": ParagraphStyle("h3", fontName=font_name, fontSize=13, leading=18, spaceAfter=6),
        "p": ParagraphStyle("p", fontName=font_name, fontSize=9.5, leading=14, spaceAfter=4),
        "bullet": ParagraphStyle("bullet", fontName=font_name, fontSize=9.5, leading=14, leftIndent=10, spaceAfter=3),
    }
    story: list[Any] = [
        Paragraph(xml_escape(title), styles["h1"]),
        Paragraph("Completed report PDF rendered from the immutable markdown snapshot.", styles["p"]),
        Spacer(1, 6),
    ]
    for kind, text in _plain_markdown_lines(markdown_content):
        if kind == "space":
            story.append(Spacer(1, 5))
            continue
        story.append(Paragraph(xml_escape(text), styles.get(kind, styles["p"])))
    doc.build(story)


def _read_manifest(run_id: str) -> list[dict[str, Any]]:
    manifest = resolve_payload_references(read_json(_exports_manifest_path(run_id), default=[]) or [], run_id=run_id)
    return [item for item in manifest if isinstance(item, dict)]


def _write_manifest(run_id: str, manifest: list[dict[str, Any]]) -> None:
    write_json(_exports_manifest_path(run_id), normalize_payload_references(manifest, run_id=run_id))
    metadata_index.refresh_run(run_id, reason="export_write")


def _next_export_id(run_id: str, export_format: str) -> str:
    return f"{run_id}-{export_format}-{len(_read_manifest(run_id)) + 1}"


def _export_path(run_id: str, export_id: str, file_name: str) -> Path:
    path = build_run_paths(run_id).reports_dir / "exports" / export_id / file_name
    ensure_dir(path.parent)
    return path


def _canonical_format(export_format: str) -> str:
    return CANONICAL_FORMATS.get(export_format, export_format)


def _mime_type(path: Path, export_format: str) -> str:
    if _canonical_format(export_format) == "markdown":
        return "text/markdown"
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _latest_markdown_version(run_id: str) -> dict[str, Any] | None:
    versions = report_versions.list_report_versions(run_id).get("items") or []
    for version in reversed(versions):
        if version.get("status") == "completed" and version.get("content_hash"):
            return version
    return None


def _artifact_metadata(
    run_id: str,
    *,
    export_id: str,
    export_format: str,
    output_path: Path,
    created_at: str,
    status: str = "completed",
    renderer: str | None = None,
    render_options: dict[str, Any] | None = None,
    version: dict[str, Any] | None = None,
    source_markdown_version: dict[str, Any] | None = None,
    immutable: bool = True,
    metadata_source: str = "export_manifest",
    error_message: str | None = None,
) -> dict[str, Any]:
    run = load_run_record(run_id)
    file_exists = output_path.is_file()
    effective_status = status
    if status in {"ready", "completed"}:
        effective_status = "completed" if file_exists else "unavailable"
    source_version = source_markdown_version or version
    workspace = run.get("workspace") if isinstance(run.get("workspace"), dict) else {}
    topic_id = workspace.get("topic_id") or run.get("topic_profile_id")
    record = {
        "export_id": export_id,
        "artifact_id": export_id,
        "report_id": run_id,
        "version_id": (version or source_version or {}).get("version_id"),
        "topic_id": topic_id,
        "corpus_snapshot_id": workspace.get("snapshot_id"),
        "settings_version": workspace.get("settings_version"),
        "settings_digest": workspace.get("settings_digest"),
        "source_manifest_digest": workspace.get("source_manifest_digest"),
        "evidence_set_digest": workspace.get("evidence_set_digest"),
        "run_id": run_id,
        "format": _canonical_format(export_format),
        "export_format": export_format,
        "mime_type": _mime_type(output_path, export_format),
        "file_name": output_path.name,
        "output": str(output_path),
        "storage_path": str(output_path),
        "created_at": created_at,
        "size_bytes": output_path.stat().st_size if file_exists else None,
        "sha256": _sha256(output_path) if file_exists else None,
        "source_markdown_version_id": (source_version or {}).get("version_id"),
        "source_markdown_hash": (source_version or {}).get("content_hash"),
        "renderer": renderer or RENDERERS.get(export_format),
        "render_options": render_options or {},
        "status": effective_status,
        "error_message": error_message,
        "immutable": immutable,
        "provenance_summary": {
            "source_run_id": run_id,
            "source_report_id": run_id,
            "source_topic_id": topic_id,
            "source_corpus_snapshot_id": workspace.get("snapshot_id"),
            "source_evidence_set_digest": workspace.get("evidence_set_digest"),
            "source_export_id": export_id,
            "source_markdown_version_id": (source_version or {}).get("version_id"),
            "metadata_source": metadata_source,
        },
    }
    if source_version:
        for field in (
            "generation_mode",
            "provider_id",
            "execution_record_id",
            "accepted_output_digest",
            "evidence_references",
            "baseline_version_id",
        ):
            if field in source_version:
                record[field] = source_version.get(field)
                record["provenance_summary"][field] = source_version.get(field)
    return record


def _decorate_export_record(run_id: str, record: dict[str, Any]) -> dict[str, Any]:
    output = Path(str(record.get("storage_path") or record.get("output") or ""))
    export_format = str(record.get("export_format") or record.get("format") or "other")
    if record.get("artifact_id"):
        decorated = dict(record)
        decorated.setdefault("storage_path", decorated.get("output"))
        decorated.setdefault("mime_type", _mime_type(output, export_format))
        decorated.setdefault("file_name", output.name if str(output) else None)
        if output.is_file():
            decorated.setdefault("size_bytes", output.stat().st_size)
            decorated.setdefault("sha256", _sha256(output))
        elif decorated.get("status") in {"ready", "completed"}:
            decorated["status"] = "unavailable"
            decorated["error_message"] = decorated.get("error_message") or "Export artifact file is missing."
        return decorated

    return _artifact_metadata(
        run_id,
        export_id=str(record.get("export_id") or ""),
        export_format=export_format,
        output_path=output,
        created_at=str(record.get("created_at") or ""),
        status=str(record.get("status") or "unavailable"),
        renderer=record.get("renderer"),
        render_options=record.get("render_options") if isinstance(record.get("render_options"), dict) else {},
        version=None,
        source_markdown_version=None,
        immutable=False,
        metadata_source="legacy_manifest_fallback",
        error_message=record.get("error_message"),
    )


def _append_export_record(
    run_id: str,
    *,
    export_id: str,
    export_format: str,
    output_path: Path,
    renderer: str | None = None,
    render_options: dict[str, Any] | None = None,
    version: dict[str, Any] | None = None,
    source_markdown_version: dict[str, Any] | None = None,
    status: str = "completed",
) -> dict[str, Any]:
    manifest_path = _exports_manifest_path(run_id)
    manifest = resolve_payload_references(read_json(manifest_path, default=[]) or [], run_id=run_id)
    record = _artifact_metadata(
        run_id,
        export_id=export_id,
        export_format=export_format,
        output_path=output_path,
        created_at=iso_now_utc(),
        status=status,
        renderer=renderer,
        render_options=render_options,
        version=version,
        source_markdown_version=source_markdown_version,
    )
    manifest.append(record)
    _write_manifest(run_id, manifest)
    return record


def _update_export_record(run_id: str, export_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    manifest = _read_manifest(run_id)
    updated: dict[str, Any] | None = None
    for index, item in enumerate(manifest):
        if item.get("export_id") != export_id:
            continue
        updated = dict(item)
        updated.update(updates)
        provenance = dict(updated.get("provenance_summary") or {})
        if updates.get("source_markdown_version_id") is not None:
            provenance["source_markdown_version_id"] = updates["source_markdown_version_id"]
        updated["provenance_summary"] = provenance
        manifest[index] = updated
        break
    if updated is None:
        raise FileNotFoundError(f"Export not found for run {run_id}: {export_id}")
    _write_manifest(run_id, manifest)
    return updated


def list_exports(run_id: str) -> dict[str, Any]:
    manifest = [_decorate_export_record(run_id, item) for item in _read_manifest(run_id)]
    return {"run_id": run_id, "items": manifest, "capabilities": _export_capabilities()}


def export_report_markdown(run_id: str) -> dict[str, Any]:
    preview = reporting.build_preview(run_id)
    source = Path(preview["path"])
    if not source.is_file():
        raise FileNotFoundError(f"No report preview markdown found for run {run_id}")
    export_id = _next_export_id(run_id, "report_markdown")
    export_path = _export_path(run_id, export_id, "exported_preview.md")
    copy_file(source, export_path)
    record = _append_export_record(
        run_id,
        export_id=export_id,
        export_format="report_markdown",
        output_path=export_path,
        renderer=RENDERERS["report_markdown"],
        render_options={"source": "preview.md"},
    )
    version = report_versions.record_completed_report_version(
        run_id,
        source="exported_report_markdown",
        source_export_id=record["export_id"],
        source_artifact_path=str(export_path),
        content=export_path.read_text(encoding="utf-8", errors="ignore"),
    )
    if version is not None:
        record = _update_export_record(
            run_id,
            export_id,
            {
                "version_id": version.get("version_id"),
                "source_markdown_version_id": version.get("version_id"),
                "source_markdown_hash": version.get("content_hash"),
            },
        )
    metadata_index.refresh_run(run_id, reason="export_write")
    return {"status": "ok", "format": "report_markdown", "output": str(export_path), "artifact": record, "version": version}


def export_report_version_markdown(run_id: str, version_id: str) -> dict[str, Any]:
    """Export one explicitly selected immutable report version."""

    detail = report_versions.get_report_version_detail(run_id, version_id)
    version = detail["version"]
    content = str(detail.get("content") or "")
    if not content.strip():
        raise FileNotFoundError(f"No report-version content found for run {run_id}")
    export_id = _next_export_id(run_id, "report_markdown")
    output_path = _export_path(run_id, export_id, "selected-report-version.md")
    write_text(output_path, content)
    record = _append_export_record(
        run_id,
        export_id=export_id,
        export_format="report_markdown",
        output_path=output_path,
        renderer="selected_report_version_markdown_copy",
        render_options={"source": "immutable_report_version"},
        version=version,
        source_markdown_version=version,
    )
    metadata_index.refresh_run(run_id, reason="report_version_export_write")
    return {
        "status": "ok",
        "format": "report_markdown",
        "output": str(output_path),
        "artifact": record,
        "version": version,
    }


def resolve_export_download(run_id: str, export_id: str) -> tuple[Path, str]:
    manifest = _read_manifest(run_id)
    record = next((item for item in manifest if item.get("export_id") == export_id), None)
    if record is None:
        raise FileNotFoundError(f"Export not found for run {run_id}: {export_id}")

    output = Path(str(record.get("storage_path") or record.get("output") or ""))
    run_reports_dir = build_run_paths(run_id).reports_dir.resolve()
    output_path = output.resolve()
    try:
        output_path.relative_to(run_reports_dir)
    except ValueError:
        raise FileNotFoundError(f"Export output is outside the run report directory: {export_id}")
    if not output_path.is_file():
        raise FileNotFoundError(f"Export artifact file is missing: {export_id}")

    media_type = str(record.get("mime_type") or _mime_type(output_path, str(record.get("export_format") or record.get("format") or "")))
    return output_path, media_type


def export_report_pdf(run_id: str) -> dict[str, Any]:
    run = load_run_record(run_id)
    if run.get("status") != "completed":
        raise RuntimeError("PDF export requires a completed report run.")
    availability = _pdf_export_availability()
    if not availability.get("available"):
        raise RuntimeError(str(availability.get("reason") or "PDF export renderer is unavailable."))

    preview = reporting.build_preview(run_id)
    source = Path(preview["path"])
    if not source.is_file():
        raise FileNotFoundError(f"No report preview markdown found for run {run_id}")
    markdown_content = source.read_text(encoding="utf-8", errors="ignore")
    if not markdown_content.strip():
        raise FileNotFoundError(f"No completed report markdown content found for run {run_id}")

    run_date = (run.get("created_at") or datetime.now().isoformat())[:10]
    export_id = _next_export_id(run_id, "report_pdf")
    output_path = _export_path(run_id, export_id, f"XRML_LabReport_{run_date}.pdf")
    title = f"{run.get('topic') or run_id} completed report ({run_date})"
    try:
        _render_completed_markdown_pdf(markdown_content=markdown_content, output_path=output_path, title=title)
    except Exception:
        output_path.unlink(missing_ok=True)
        raise
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        output_path.unlink(missing_ok=True)
        raise RuntimeError("PDF export failed before a completed artifact file was created.")

    version = report_versions.record_completed_report_version(
        run_id,
        source="exported_report_pdf_source_markdown",
        source_export_id=export_id,
        source_artifact_path=str(source),
        content=markdown_content,
    )
    if version is None:
        output_path.unlink(missing_ok=True)
        raise RuntimeError("PDF export requires a completed report version.")
    record = _append_export_record(
        run_id,
        export_id=export_id,
        export_format="report_pdf",
        output_path=output_path,
        renderer=RENDERERS["report_pdf"],
        render_options={
            "source": "completed_report_markdown_snapshot",
            "run_date": run_date,
            "deterministic": True,
            "page_compression": False,
            "renderer_dependency": "reportlab",
        },
        version=version,
        source_markdown_version=version,
    )
    return {"status": "ok", "format": "report_pdf", "output": str(output_path), "artifact": record, "version": version}


def export_journal_pdf(run_id: str) -> dict[str, Any]:
    run = load_run_record(run_id)
    if run.get("status") != "completed":
        raise RuntimeError("Journal-style PDF export requires a completed report run.")
    availability = _pdf_export_availability()
    if not availability.get("available"):
        raise RuntimeError(str(availability.get("reason") or "Journal-style PDF export renderer is unavailable."))

    preview = reporting.build_preview(run_id)
    source = Path(preview["path"])
    if not source.is_file():
        raise FileNotFoundError(f"No report preview markdown found for run {run_id}")
    markdown_content = source.read_text(encoding="utf-8", errors="ignore")
    if not markdown_content.strip():
        raise FileNotFoundError(f"No completed report markdown content found for run {run_id}")

    run_date = (run.get("created_at") or datetime.now().isoformat())[:10]
    export_id = _next_export_id(run_id, "journal_pdf")
    output_path = _export_path(run_id, export_id, f"XRML_JournalStyleReport_{run_date}.pdf")
    title = f"{run.get('topic') or run_id} journal-style report ({run_date})"
    try:
        _render_completed_markdown_pdf(markdown_content=markdown_content, output_path=output_path, title=title)
    except Exception:
        output_path.unlink(missing_ok=True)
        raise
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        output_path.unlink(missing_ok=True)
        raise RuntimeError("Journal-style PDF export failed before a completed artifact file was created.")

    version = report_versions.record_completed_report_version(
        run_id,
        source="exported_journal_style_pdf_source_markdown",
        source_export_id=export_id,
        source_artifact_path=str(source),
        content=markdown_content,
    )
    if version is None:
        output_path.unlink(missing_ok=True)
        raise RuntimeError("Journal-style PDF export requires a completed report version.")
    record = _append_export_record(
        run_id,
        export_id=export_id,
        export_format="journal_pdf",
        output_path=output_path,
        renderer=RENDERERS["journal_pdf"],
        render_options={
            "source": "completed_report_markdown_snapshot",
            "run_date": run_date,
            "template_id": "journal_style_report_v1",
            "template_name": "Journal-style completed report PDF",
            "meaning": "journal_style_report_pdf",
            "source_pdf_handling": "links_only_no_redistribution",
            "deterministic": True,
            "page_compression": False,
            "renderer_dependency": "reportlab",
        },
        version=version,
        source_markdown_version=version,
    )
    return {"status": "ok", "format": "journal_pdf", "output": str(output_path), "artifact": record, "version": version}
