"""Inspect an explicit local manual handoff without network access or writes.

This checks artifact structure and declared design fields, not visual quality,
Stitch account provenance, redistribution rights, or general secret absence.
"""

from __future__ import annotations

import hashlib
import json
import struct
import zlib
from pathlib import Path
from typing import Any, Mapping, Sequence


MANUAL_FILES = (
    "stitch-wide-1920.png", "stitch-wide-1440.png", "stitch-narrow.png",
    "design-dna.md", "component-hierarchy.json", "component-hierarchy.md",
    "tokens-extended.json", "implementation-notes.md",
)
COMPONENTS = (
    "screen/root", "sidebar/nav", "command bar", "status strip", "main workbench",
    "topic portfolio", "recent runs table/list", "schedule status panel",
    "right inspector", "profile drawer/modal", "settings/status compact area",
)
COMPONENT_FIELDS = (
    "name", "role", "layout_region", "children", "primary_actions",
    "data_dependencies", "responsive_notes",
)
DNA_SECTIONS = {
    "overall_ia": "overall IA",
    "menu_placement_rationale": "menu placement rationale",
    "wide_desktop_grid": "wide desktop grid",
    "behavior_1920": "1920px behavior",
    "behavior_1440": "1440px behavior",
    "behavior_narrow": "narrow responsive behavior",
    "action_hierarchy": "action hierarchy",
    "density_guidance": "card/table density guidance",
    "drawer_guidance": "drawer/modal guidance",
    "accessibility_notes": "accessibility notes",
}


def _png_dimensions(raw: bytes) -> tuple[int, int]:
    """Check PNG framing, required chunks and CRCs; do not claim pixel decoding."""
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("png_structure_invalid")
    offset, dimensions, has_data = 8, None, False
    while offset + 12 <= len(raw):
        size = struct.unpack(">I", raw[offset:offset + 4])[0]
        kind = raw[offset + 4:offset + 8]
        end = offset + 12 + size
        if end > len(raw):
            break
        data = raw[offset + 8:end - 4]
        crc = struct.unpack(">I", raw[end - 4:end])[0]
        if zlib.crc32(kind + data) & 0xFFFFFFFF != crc:
            break
        if dimensions is None:
            if kind != b"IHDR" or size != 13:
                break
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", data)
            depths = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
            if not width or not height or depth not in depths.get(color, set()) or compression or filtering or interlace not in {0, 1}:
                break
            dimensions = (width, height)
        elif kind == b"IHDR":
            break
        elif kind == b"IDAT":
            has_data = has_data or bool(size)
        elif kind == b"IEND":
            if not size and end == len(raw) and has_data:
                return dimensions
            break
        offset = end
    raise ValueError("png_structure_invalid")


def _component_validation(value: Any) -> dict[str, Any]:
    nodes: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()

    def visit(node: Any, *, root: bool = False) -> None:
        if not isinstance(node, dict):
            return
        name = "screen/root" if root else node.get("name")
        if isinstance(name, str) and name in COMPONENTS:
            if name in nodes:
                duplicates.add(name)
            nodes[name] = node
        children = node.get("children")
        if isinstance(children, list):
            for child in children:
                visit(child)

    if isinstance(value, dict):
        visit(value.get("screen"), root=True)
    missing = [name for name in COMPONENTS if name not in nodes]
    missing_fields = []
    for name, node in nodes.items():
        for field in COMPONENT_FIELDS:
            item = node.get(field)
            valid = isinstance(item, list) if field in {"children", "primary_actions", "data_dependencies"} else isinstance(item, str) and bool(item.strip())
            if not valid:
                missing_fields.append(f"{name}.{field}")
    complete = not missing and not missing_fields and not duplicates
    return {"present": bool(nodes), "complete": complete, "has_all_fields": not missing_fields,
            "present_components": sorted(nodes), "missing_components": missing,
            "missing_fields": sorted(missing_fields), "duplicate_components": sorted(duplicates)}


def inspect_manual_handoff(
    manual_root: Path, project_metadata: Mapping[str, Any], *, redaction_markers: Sequence[str] = (),
) -> dict[str, Any]:
    """Return a content-free report for the eight named inputs under manual_root.

    Metadata needs nonempty project_id/project_name strings. Explicit markers
    test only supplied values; they are never read from the process environment.
    PNG validation is structural, not a rendered-image or design-quality check.
    """
    markers = tuple(marker for marker in redaction_markers if isinstance(marker, str) and marker)
    metadata_ok = all(isinstance(project_metadata.get(key), str) and bool(project_metadata[key].strip()) for key in ("project_id", "project_name"))
    metadata_text = json.dumps(dict(project_metadata), ensure_ascii=False, sort_keys=True)
    marker_found = any(marker in metadata_text for marker in markers)
    parsed: dict[str, Any] = {}
    files = []
    for name in MANUAL_FILES:
        path = manual_root / name
        row: dict[str, Any] = {"name": name, "exists": path.is_file() and not path.is_symlink(), "parseable": False}
        try:
            if not row["exists"]:
                raise ValueError("missing_or_nonregular_file")
            raw = path.read_bytes()
            row.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            # Scan exact bytes as well as decoded JSON (which can escape a marker).
            marker_found = marker_found or any(marker.encode("utf-8") in raw for marker in markers)
            if name.endswith(".png"):
                width, height = _png_dimensions(raw)
                row.update(width=width, height=height, validation="PNG_STRUCTURE_ONLY")
                value: Any = True
            else:
                text = raw.decode("utf-8")
                if not text.strip():
                    raise ValueError("empty_file")
                value = json.loads(text) if name.endswith(".json") else text
                if name.endswith(".json") and not isinstance(value, dict):
                    raise ValueError("json_object_required")
                normalized = json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value
                marker_found = marker_found or any(marker in normalized for marker in markers)
            parsed[name] = value
            row["parseable"] = True
        except (OSError, UnicodeError, ValueError, struct.error):
            # Exception text can contain private paths or invalid JSON input.
            row["error"] = "missing_or_nonregular_file" if not row["exists"] else "unreadable_or_invalid_file"
        files.append(row)
    hierarchy = _component_validation(parsed.get("component-hierarchy.json"))
    dna = parsed.get("design-dna.md", "")
    sections = {key: heading.lower() in dna.lower() for key, heading in DNA_SECTIONS.items()}
    tokens = parsed.get("tokens-extended.json", {}).get("tokens", {})
    has_tokens = isinstance(tokens, dict) and bool(tokens) and any(isinstance(value, dict) and bool(value) for value in tokens.values())
    checks = {
        "has_project_metadata": metadata_ok,
        "has_tokens_or_theme": has_tokens,
        "has_screen_1920": "stitch-wide-1920.png" in parsed,
        "has_screen_1440": "stitch-wide-1440.png" in parsed,
        "has_screen_narrow": "stitch-narrow.png" in parsed,
        "has_component_hierarchy": hierarchy["complete"],
        "has_layout_structure": hierarchy["complete"],
        "has_design_dna_layout_notes": all(sections.values()),
        "has_implementation_notes": "implementation-notes.md" in parsed,
        "all_inputs_parseable": all(row["parseable"] for row in files),
        "supplied_markers_absent": not marker_found,
    }
    ready = all(checks.values())
    return {
        "schema_version": "XRML_MANUAL_HANDOFF_VALIDATION_V1",
        "source": ["supplied_project_metadata", "local_manual_files"],
        "external_stitch_provenance": "NOT_ESTABLISHED",
        "visual_quality": "NOT_ASSESSED", "privacy_review": "NOT_ESTABLISHED",
        "project_metadata_sha256": hashlib.sha256(metadata_text.encode("utf-8")).hexdigest(),
        "inventory": {"expected_files": list(MANUAL_FILES), "files": files,
                      "summary": {"total_expected": len(MANUAL_FILES), "present": sum(row["exists"] for row in files),
                                  "missing": sum(not row["exists"] for row in files)}},
        "component_hierarchy": {"validation": hierarchy},
        "design_dna": {"contains_layout_notes": all(sections.values()), "sections_present": sections},
        "secret_check": {"scope": "SUPPLIED_MARKERS_ONLY" if markers else "NOT_REQUESTED", "marker_found": marker_found},
        "completeness": {**checks, "enough_for_implementation": ready,
                         "label": "Manual handoff ready for implementation" if ready else "Manual handoff incomplete",
                         "missing_fields": [key for key, present in checks.items() if not present]},
    }
