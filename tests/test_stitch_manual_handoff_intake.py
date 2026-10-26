from __future__ import annotations

import json
import os
from pathlib import Path


STITCH_DIR = Path("design/stitch")
EXPECTED_MANUAL_FILES = [
    "stitch-wide-1920.png",
    "stitch-wide-1440.png",
    "stitch-narrow.png",
    "design-dna.md",
    "component-hierarchy.json",
    "component-hierarchy.md",
    "tokens-extended.json",
    "implementation-notes.md",
]
REQUIRED_HIERARCHY_COMPONENTS = [
    "screen/root",
    "sidebar/nav",
    "command bar",
    "status strip",
    "main workbench",
    "topic portfolio",
    "recent runs table/list",
    "schedule status panel",
    "right inspector",
    "profile drawer/modal",
    "settings/status compact area",
]
REQUIRED_DNA_SECTIONS = {
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


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _read_outputs():
    final_json = _load_json(STITCH_DIR / "final-stitch-handoff.json")
    final_layout = _load_json(STITCH_DIR / "final-layout-map.json")
    final_comp = _load_json(STITCH_DIR / "final-handoff-completeness.json")
    inventory = _load_json(STITCH_DIR / "manual-export-inventory.json")
    return final_json, final_layout, final_comp, inventory


def test_manual_inventory_is_complete_and_parseable():
    _, _, _, inventory = _read_outputs()
    assert inventory["expected_files"] == EXPECTED_MANUAL_FILES
    assert len(inventory["files"]) == len(inventory["expected_files"])
    assert inventory["summary"]["total_expected"] == len(EXPECTED_MANUAL_FILES)
    assert inventory["summary"]["present"] == len(EXPECTED_MANUAL_FILES)
    assert inventory["summary"]["missing"] == 0

    for entry in inventory["files"]:
        assert entry["exists"] is True
        assert entry["parseable"] is True


def test_manual_pngs_and_manual_docs_openable():
    for name in [
        ("design/stitch/manual/stitch-wide-1920.png", "binary"),
        ("design/stitch/manual/stitch-wide-1440.png", "binary"),
        ("design/stitch/manual/stitch-narrow.png", "binary"),
        "design/stitch/manual/design-dna.md",
        "design/stitch/manual/component-hierarchy.md",
        "design/stitch/manual/implementation-notes.md",
        "design/stitch/manual/component-hierarchy.json",
        "design/stitch/manual/tokens-extended.json",
    ]:
        if isinstance(name, tuple):
            name, mode = name
        else:
            mode = "text"
            name = name
        path = Path(name)
        assert path.exists()
        if mode == "binary":
            # binary output exists and can be opened by downstream tooling
            assert path.stat().st_size > 0
        else:
            # parseable is validated in inventory; we still assert non-empty payload for text/json files.
            text = path.read_text(encoding="utf-8")
            assert text.strip()


def _component_validation_ok(validation: dict) -> tuple[bool, list[str], list[str]]:
    required = set(REQUIRED_HIERARCHY_COMPONENTS)
    present = set(validation.get("components", {}).keys())
    missing_components = sorted(required - present)

    missing_fields = []
    for key in required & present:
        comp = validation["components"][key]
        for field in [
            "name",
            "role",
            "layout_region",
            "children",
            "primary_actions",
            "data_dependencies",
            "responsive_notes",
        ]:
            if field not in comp:
                missing_fields.append(f"{key}.{field}")
                continue
            if field in {"children", "primary_actions", "data_dependencies"}:
                if not isinstance(comp[field], list):
                    missing_fields.append(f"{key}.{field}")
            elif comp[field] is None:
                if not isinstance(comp[field], list):
                    missing_fields.append(f"{key}.{field}")

    has_all_fields = len(missing_fields) == 0
    complete = (
        validation.get("present", False)
        and len(missing_components) == 0
        and has_all_fields
    )
    return complete, missing_components, missing_fields


def test_component_hierarchy_validation_complete():
    final_json, final_layout, _, _ = _read_outputs()

    for payload in [final_json.get("component_hierarchy", {}), final_layout.get("component_hierarchy", {})]:
        validation = payload.get("validation", {})
        assert validation.get("present") is True
        complete, missing_components, missing_fields = _component_validation_ok(validation)
        assert not missing_components
        assert not missing_fields
        assert validation.get("complete") is True
        assert validation.get("has_all_fields") is True
        assert payload["validation"]["complete"] is True

        # Required components must be present and complete in the canonical output.
        assert payload["validation"]["missing_components"] == []

    assert complete is True


def test_design_dna_sections_present():
    final_json, _, _, _ = _read_outputs()
    dna = final_json.get("design_dna", {})
    extracted = dna.get("extracted_sections", {})
    assert dna.get("contains_layout_notes") is True
    missing = [key for key, value in REQUIRED_DNA_SECTIONS.items() if not extracted.get(key)]
    assert missing == []

    md = _load_text(STITCH_DIR / "manual/design-dna.md")
    lowered = md.lower()
    for value in REQUIRED_DNA_SECTIONS.values():
        assert value.lower() in lowered


def test_final_merge_artifacts_are_parseable_and_ready():
    final_json, final_layout, final_comp, inventory = _read_outputs()

    assert final_json["selected_project_id"] == "projects/672383315129384900"
    assert final_json["selected_project_name"] == "XRML Research Dashboard Redesign"
    assert final_json["source"] == ["Stitch MCP direct HTTP", "Stitch manual export"]
    assert final_json.get("completeness", {}).get("label") == "Stitch handoff ready for implementation"
    assert final_json.get("completeness", {}).get("enough_for_implementation") is True
    assert final_comp["label"] == final_json["completeness"]["label"]
    assert final_comp["enough_for_implementation"] is True
    assert final_comp["missing_fields"] == []
    assert final_layout["source"] == final_json["source"]


def test_final_completeness_gate_conditions():
    _, final_layout, final_comp, _ = _read_outputs()
    assert final_comp["has_project_metadata"] is True
    assert final_comp["has_tokens_or_theme"] is True
    assert final_comp["has_screen_1920"] is True
    assert final_comp["has_screen_1440"] is True
    assert final_comp["has_screen_narrow"] is True
    assert final_comp["has_component_hierarchy"] is True
    assert final_comp["has_layout_structure"] is True
    assert final_comp["has_design_dna_layout_notes"] is True
    assert final_comp["has_implementation_notes"] is True
    assert final_comp["no_secret_leakage"] is True
    assert final_comp["has_structure"] is True
    assert final_comp["label"] == "Stitch handoff ready for implementation"

    layout = final_layout.get("provenance", {})
    assert layout["layout_structure"] == "from_stitch_component_hierarchy"
    assert layout["component_hierarchy"] == "from_stitch_component_hierarchy"
    assert layout["design_dna"] == "from_stitch_manual_design_dna"


def test_provenance_and_no_secret_in_outputs():
    final_json, final_layout, final_comp, _ = _read_outputs()

    # Provenance allowed values should be present for layout decisions.
    allowed = {
        "from_stitch_manual_image",
        "from_stitch_manual_design_dna",
        "from_stitch_component_hierarchy",
        "from_stitch_direct_mcp",
        "from_stitch_mcp_manual",
        "inferred_for_implementation",
    }
    assert final_layout["layout_structure"]["provenance"] in allowed
    assert final_layout["component_hierarchy"]["provenance"] in allowed
    for node in [
        "1920",
        "1440",
        "narrow",
    ]:
        assert final_layout["screen_images"][node]["provenance"] in allowed
        assert final_layout["screen_images"][node]["present"] is True

    secret = os.getenv("STITCH_API_KEY", "")
    if secret:
        paths = list(STITCH_DIR.glob("**/*.json")) + list(STITCH_DIR.glob("**/*.md"))
        for target in paths:
            content = target.read_text(encoding="utf-8")
            assert secret not in content

    if not secret:
        # placeholder must not appear; this also catches accidental hard-coded examples.
        for target in (STITCH_DIR / "final-stitch-handoff.json", STITCH_DIR / "final-layout-map.json"):
            assert "REPLACE_WITH_STITCH_API_KEY" not in target.read_text(encoding="utf-8")

    assert isinstance(final_comp, dict)
