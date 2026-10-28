from __future__ import annotations

import hashlib
import json
import struct
import zlib
from pathlib import Path

import pytest

from tools.stitch_manual_handoff_intake import inspect_manual_handoff


# Synthetic parser inputs, not historical screenshots or real Stitch evidence.
# No private design directory is consulted.
EXPECTED_MANUAL_FILES = [
    "stitch-wide-1920.png", "stitch-wide-1440.png", "stitch-narrow.png",
    "design-dna.md", "component-hierarchy.json", "component-hierarchy.md",
    "tokens-extended.json", "implementation-notes.md",
]
REQUIRED_HIERARCHY_COMPONENTS = [
    "screen/root", "sidebar/nav", "command bar", "status strip", "main workbench",
    "topic portfolio", "recent runs table/list", "schedule status panel",
    "right inspector", "profile drawer/modal", "settings/status compact area",
]
REQUIRED_DNA_SECTIONS = {
    "overall_ia": "overall IA", "menu_placement_rationale": "menu placement rationale",
    "wide_desktop_grid": "wide desktop grid", "behavior_1920": "1920px behavior",
    "behavior_1440": "1440px behavior", "behavior_narrow": "narrow responsive behavior",
    "action_hierarchy": "action hierarchy", "density_guidance": "card/table density guidance",
    "drawer_guidance": "drawer/modal guidance", "accessibility_notes": "accessibility notes",
}
SYNTHETIC_MARKER = "synthetic-only-key-never-real"


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _png(width: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    # A deterministic two-row RGB swatch, not a screenshot or UI benchmark.
    header = struct.pack(">IIBBBBB", width, 2, 8, 2, 0, 0, 0)
    pixels = (b"\0" + b"\x20\x40\x60" * width) * 2
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")


@pytest.fixture
def intake(tmp_path: Path):
    root = tmp_path / "synthetic-manual-intake"
    root.mkdir()
    for name, width in (("stitch-wide-1920.png", 1920), ("stitch-wide-1440.png", 1440), ("stitch-narrow.png", 360)):
        (root / name).write_bytes(_png(width))
    for name in ("component-hierarchy.md", "implementation-notes.md"):
        (root / name).write_text("# Synthetic manual intake\nFixture content, not a real export.\n", encoding="utf-8")
    (root / "design-dna.md").write_text("# Synthetic design DNA\n" + "\n".join(
        f"## {heading}\nSynthetic description.\n" for heading in REQUIRED_DNA_SECTIONS.values()
    ), encoding="utf-8")

    def node(name: str) -> dict:
        return {"name": name, "role": "Synthetic role", "layout_region": "synthetic-region",
                "children": [], "primary_actions": ["synthetic-action"],
                "data_dependencies": ["synthetic-record"], "responsive_notes": "Synthetic wrapping rule."}

    screen = node("Synthetic screen")
    screen["children"] = [node(name) for name in REQUIRED_HIERARCHY_COMPONENTS[1:]]
    _write_json(root / "component-hierarchy.json", {"screen": screen})
    _write_json(root / "tokens-extended.json", {"project": "synthetic-fixture", "theme": "synthetic",
                                               "tokens": {"colors": {"surface": "#204060"}, "spacing": {"small": 4}}})
    metadata = {"project_id": "projects/synthetic-intake", "project_name": "Synthetic manual intake fixture"}
    return root, metadata


def _inspect(intake):
    root, metadata = intake
    return inspect_manual_handoff(root, metadata, redaction_markers=(SYNTHETIC_MARKER,))


def _incomplete(report: dict, field: str) -> None:
    assert report["completeness"]["enough_for_implementation"] is False
    assert report["completeness"]["label"] == "Manual handoff incomplete"
    assert field in report["completeness"]["missing_fields"]


def test_manual_inventory_is_complete_and_parseable(intake):
    inventory = _inspect(intake)["inventory"]
    assert inventory["expected_files"] == EXPECTED_MANUAL_FILES
    assert len(inventory["files"]) == len(EXPECTED_MANUAL_FILES)
    assert inventory["summary"] == {"total_expected": 8, "present": 8, "missing": 0}
    for entry in inventory["files"]:
        raw = (intake[0] / entry["name"]).read_bytes()
        assert entry["exists"] is True
        assert entry["parseable"] is True
        assert entry["bytes"] == len(raw)
        assert entry["sha256"] == hashlib.sha256(raw).hexdigest()


def test_manual_pngs_and_manual_docs_openable(intake):
    files = {row["name"]: row for row in _inspect(intake)["inventory"]["files"]}
    for name, width in (("stitch-wide-1920.png", 1920), ("stitch-wide-1440.png", 1440), ("stitch-narrow.png", 360)):
        assert files[name]["width"] == width
        assert files[name]["height"] == 2
        assert files[name]["validation"] == "PNG_STRUCTURE_ONLY"
    assert all(row["parseable"] and row["bytes"] > 0 for row in files.values())


def test_component_hierarchy_validation_complete(intake):
    validation = _inspect(intake)["component_hierarchy"]["validation"]
    assert validation["present"] is True
    assert validation["complete"] is True
    assert validation["has_all_fields"] is True
    assert set(validation["present_components"]) == set(REQUIRED_HIERARCHY_COMPONENTS)
    assert validation["missing_components"] == validation["missing_fields"] == validation["duplicate_components"] == []


def test_design_dna_sections_present(intake):
    dna = _inspect(intake)["design_dna"]
    assert dna["contains_layout_notes"] is True
    assert set(dna["sections_present"]) == set(REQUIRED_DNA_SECTIONS)
    assert all(dna["sections_present"].values())


def test_final_merge_artifacts_are_parseable_and_ready(intake):
    report = _inspect(intake)
    assert json.loads(json.dumps(report)) == report
    assert report["source"] == ["supplied_project_metadata", "local_manual_files"]
    assert report["external_stitch_provenance"] == "NOT_ESTABLISHED"
    assert report["project_metadata_sha256"] == hashlib.sha256(
        json.dumps(intake[1], ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    assert report["completeness"]["label"] == "Manual handoff ready for implementation"
    assert report["completeness"]["enough_for_implementation"] is True
    assert report["completeness"]["missing_fields"] == []


def test_final_completeness_gate_conditions(intake):
    complete = _inspect(intake)["completeness"]
    for field in ("has_project_metadata", "has_tokens_or_theme", "has_screen_1920", "has_screen_1440",
                  "has_screen_narrow", "has_component_hierarchy", "has_layout_structure",
                  "has_design_dna_layout_notes", "has_implementation_notes", "all_inputs_parseable", "supplied_markers_absent"):
        assert complete[field] is True


def test_provenance_and_no_secret_in_outputs(intake, monkeypatch):
    # Only the supplied marker is scanned; no stored key or success becomes
    # external provenance, visual approval, or a general privacy clearance.
    monkeypatch.setenv("STITCH_API_KEY", "unrelated-synthetic-environment-value")
    report = _inspect(intake)
    serialized = json.dumps(report)
    assert SYNTHETIC_MARKER not in serialized
    assert "unrelated-synthetic-environment-value" not in serialized
    assert str(intake[0]) not in serialized
    assert report["secret_check"] == {"scope": "SUPPLIED_MARKERS_ONLY", "marker_found": False}
    assert report["privacy_review"] == "NOT_ESTABLISHED"
    assert report["visual_quality"] == "NOT_ASSESSED"


@pytest.mark.parametrize("name,field", [
    ("stitch-wide-1920.png", "has_screen_1920"), ("stitch-wide-1440.png", "has_screen_1440"),
    ("stitch-narrow.png", "has_screen_narrow"), ("implementation-notes.md", "has_implementation_notes"),
])
def test_missing_required_file_is_incomplete(intake, name, field):
    (intake[0] / name).unlink()
    report = _inspect(intake)
    _incomplete(report, field)
    assert report["inventory"]["summary"]["missing"] == 1
    row = next(row for row in report["inventory"]["files"] if row["name"] == name)
    assert row["parseable"] is False
    assert row["error"] == "missing_or_nonregular_file"


@pytest.mark.parametrize("name,data", [
    ("stitch-narrow.png", b"not a PNG"), ("stitch-narrow.png", _png(360)[:-1]),
    ("component-hierarchy.json", b"{broken json"), ("component-hierarchy.json", b"[]"),
    ("tokens-extended.json", b'{"tokens":{}}'), ("design-dna.md", b"   "),
])
def test_malformed_or_empty_input_never_becomes_ready(intake, name, data):
    (intake[0] / name).write_bytes(data)
    assert _inspect(intake)["completeness"]["enough_for_implementation"] is False


def test_missing_component_and_field_are_reported_without_copied_ready_flags(intake):
    path = intake[0] / "component-hierarchy.json"
    hierarchy = json.loads(path.read_text())
    hierarchy["screen"]["children"].pop()
    del hierarchy["screen"]["children"][0]["responsive_notes"]
    _write_json(path, hierarchy)
    report = _inspect(intake)
    _incomplete(report, "has_component_hierarchy")
    validation = report["component_hierarchy"]["validation"]
    assert validation["missing_components"] == ["settings/status compact area"]
    assert validation["missing_fields"] == ["sidebar/nav.responsive_notes"]


def test_missing_dna_section_and_project_metadata_are_incomplete(intake):
    path = intake[0] / "design-dna.md"
    path.write_text(path.read_text().replace("accessibility notes", "Synthetic omitted section"))
    intake[1]["project_id"] = ""
    report = _inspect(intake)
    _incomplete(report, "has_design_dna_layout_notes")
    _incomplete(report, "has_project_metadata")
    assert report["design_dna"]["sections_present"]["accessibility_notes"] is False


@pytest.mark.parametrize("source", ["markdown", "escaped_json", "metadata"])
def test_sensitive_marker_rejects_readiness_without_echoing_input(intake, source):
    if source == "metadata":
        intake[1]["project_name"] = SYNTHETIC_MARKER
    elif source == "markdown":
        (intake[0] / "implementation-notes.md").write_text(SYNTHETIC_MARKER)
    else:
        path = intake[0] / "tokens-extended.json"
        content = path.read_text().replace("synthetic-fixture", SYNTHETIC_MARKER)
        path.write_text(content.replace("synthetic-only", "\\u0073ynthetic-only"))
    report = _inspect(intake)
    _incomplete(report, "supplied_markers_absent")
    assert report["secret_check"]["marker_found"] is True
    assert SYNTHETIC_MARKER not in json.dumps(report)
