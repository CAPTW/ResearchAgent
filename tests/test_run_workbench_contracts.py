from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from app.repositories import artifact_refs
from app.repositories.files import read_json, write_json
from app.repositories.runs import build_run_paths
from app.services import exports
from tests.conftest import PRIMARY_RUN_ID


def resolve_portable_artifact_path(locator: str, *, run_id: str = PRIMARY_RUN_ID) -> Path:
    """Resolve an API artifact locator the way production does.

    `storage_path` is an opaque portable product reference (`run:reports/...`),
    never a native filesystem path. `Path("run:reports/x.pdf")` is not a path to
    that artifact on any platform, and on Windows it names an alternate data
    stream that cannot be opened, so only the canonical resolver may turn a
    locator into a filesystem path.
    """
    assert artifact_refs.is_portable_ref(locator), f"artifact locator must stay portable: {locator!r}"
    resolution = artifact_refs.resolve_artifact_reference(locator, run_id=run_id)
    assert resolution.is_safe, f"artifact locator must resolve safely: {locator!r}"
    assert resolution.resolved_path, f"artifact locator must resolve to a native path: {locator!r}"
    return Path(resolution.resolved_path)


def test_run_detail_contract_exposes_topic_scope_and_stage_state(client: TestClient) -> None:
    response = client.get(f"/runs/{PRIMARY_RUN_ID}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == PRIMARY_RUN_ID
    assert "topic_profile_id" in payload
    assert payload["status"] in {"queued", "running", "completed", "failed"}
    assert isinstance(payload.get("steps"), list)
    assert isinstance(payload.get("counts"), dict)


def test_run_papers_contract_exposes_ranked_review_and_provenance_shape(client: TestClient) -> None:
    response = client.get(f"/runs/{PRIMARY_RUN_ID}/papers", params={"page": 1, "page_size": 5})

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == PRIMARY_RUN_ID
    assert payload["page"] == 1
    assert payload["page_size"] == 5
    assert "facets" in payload
    assert {"themes", "statuses", "score_bands", "providers", "duplicate_clusters"} <= set(payload["facets"])
    assert payload["items"], "fixture run should expose ranked papers for the workbench"
    first = payload["items"][0]
    assert first["paper_id"]
    assert "source_provider" in first
    assert "score_breakdown" in first
    assert "artifacts" in first
    assert "dedup" in first
    assert {"cluster_id", "cluster_size", "is_primary", "primary_paper_id", "members"} <= set(first["dedup"])
    assert "review_action" in first
    assert first["review_action"]["decision"] in {"unreviewed", "keep", "dismiss", "save"}


def test_run_papers_contract_supports_provider_facet_and_filter(client: TestClient) -> None:
    all_response = client.get(f"/runs/{PRIMARY_RUN_ID}/papers", params={"page": 1, "page_size": 5})

    assert all_response.status_code == 200
    all_payload = all_response.json()
    providers = [facet["value"] for facet in all_payload["facets"]["providers"]]
    assert providers, "fixture run should expose provider provenance facets"

    provider = providers[0]
    filtered_response = client.get(f"/runs/{PRIMARY_RUN_ID}/papers", params={"provider": provider})

    assert filtered_response.status_code == 200
    filtered_payload = filtered_response.json()
    assert filtered_payload["provider"] == provider
    assert filtered_payload["items"]
    assert {item["source_provider"] or "unknown" for item in filtered_payload["items"]} == {provider}


def test_run_paper_review_action_persists_in_list_and_detail(client: TestClient) -> None:
    actions_path = build_run_paths(PRIMARY_RUN_ID).run_dir / "paper_review_actions.json"
    original_actions = read_json(actions_path, default=None)
    papers_response = client.get(f"/runs/{PRIMARY_RUN_ID}/papers", params={"page": 1, "page_size": 1})
    assert papers_response.status_code == 200
    paper_id = papers_response.json()["items"][0]["paper_id"]
    note = "pytest review action persistence check"

    try:
        patch_response = client.patch(
            f"/runs/{PRIMARY_RUN_ID}/papers/{paper_id}",
            json={"decision": "keep", "pinned": True, "note": note},
        )

        assert patch_response.status_code == 200
        patched = patch_response.json()
        assert patched["decision"] == "keep"
        assert patched["pinned"] is True
        assert patched["note"] == note
        assert patched["updated_at"]

        list_response = client.get(f"/runs/{PRIMARY_RUN_ID}/papers", params={"page": 1, "page_size": 5})
        assert list_response.status_code == 200
        persisted_item = next(item for item in list_response.json()["items"] if item["paper_id"] == paper_id)
        assert persisted_item["review_action"]["decision"] == "keep"
        assert persisted_item["review_action"]["pinned"] is True
        assert persisted_item["review_action"]["note"] == note

        detail_response = client.get(f"/runs/{PRIMARY_RUN_ID}/papers/{paper_id}")
        assert detail_response.status_code == 200
        assert detail_response.json()["paper"]["review_action"]["note"] == note
    finally:
        if original_actions is None:
            actions_path.unlink(missing_ok=True)
        else:
            write_json(actions_path, original_actions)


def test_run_paper_detail_exposes_evidence_safe_empty_and_dedup_contract(client: TestClient) -> None:
    papers_response = client.get(f"/runs/{PRIMARY_RUN_ID}/papers", params={"page": 1, "page_size": 1})
    assert papers_response.status_code == 200
    paper_id = papers_response.json()["items"][0]["paper_id"]

    detail_response = client.get(f"/runs/{PRIMARY_RUN_ID}/papers/{paper_id}")
    review_response = client.get(f"/runs/{PRIMARY_RUN_ID}/papers/{paper_id}/review")

    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["paper"]["dedup"]["members"]
    assert isinstance(detail["related_papers"], list)

    assert review_response.status_code == 200
    review = review_response.json()
    assert "evidence" in review
    assert isinstance(review["evidence"], list)
    for item in review["evidence"]:
        assert {"evidence_id", "source_type", "snippet", "artifact_path", "citation_text"} <= set(item)


def test_report_authoring_contract_updates_outline_preview_and_citation_trace(client: TestClient) -> None:
    outline_response = client.get(f"/runs/{PRIMARY_RUN_ID}/outline")
    assert outline_response.status_code == 200
    outline = outline_response.json()
    assert outline["sections"], "fixture run should expose editable report outline sections"

    unique_text = "pytest authoring preview trace"
    patched_sections = outline["sections"]
    patched_sections[0]["body_markdown"] = f"{patched_sections[0].get('body_markdown', '')}\n\n{unique_text}".strip()

    patch_response = client.patch(f"/runs/{PRIMARY_RUN_ID}/outline", json={"sections": patched_sections})
    assert patch_response.status_code == 200
    assert patch_response.json()["sections"][0]["body_markdown"].endswith(unique_text)

    preview_response = client.get(f"/runs/{PRIMARY_RUN_ID}/preview")
    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["run_id"] == PRIMARY_RUN_ID
    assert preview["path"].endswith("preview.md")
    assert unique_text in preview["content"]

    citations_response = client.get(f"/runs/{PRIMARY_RUN_ID}/citations")
    assert citations_response.status_code == 200
    citations = citations_response.json()
    assert citations["run_id"] == PRIMARY_RUN_ID
    assert citations["source_outline_path"]
    assert citations["source_preview_path"].endswith("preview.md")
    assert isinstance(citations["items"], list)
    if citations["items"]:
        first = citations["items"][0]
        assert {"citation_id", "section_id", "section_title", "provenance"} <= set(first)
        assert first["provenance"]["source_run_id"] == PRIMARY_RUN_ID


def test_report_alias_contracts_match_run_authoring_endpoints(client: TestClient) -> None:
    for path in ("outline", "preview", "citations"):
        response = client.get(f"/reports/{PRIMARY_RUN_ID}/{path}")
        assert response.status_code == 200
        assert response.json()["run_id"] == PRIMARY_RUN_ID


def test_markdown_export_download_uses_current_preview_artifact(client: TestClient) -> None:
    outline_response = client.get(f"/runs/{PRIMARY_RUN_ID}/outline")
    assert outline_response.status_code == 200
    outline = outline_response.json()
    unique_text = "pytest exported preview trace"
    patched_sections = outline["sections"]
    patched_sections[0]["body_markdown"] = f"{patched_sections[0].get('body_markdown', '')}\n\n{unique_text}".strip()

    patch_response = client.patch(f"/runs/{PRIMARY_RUN_ID}/outline", json={"sections": patched_sections})
    assert patch_response.status_code == 200

    export_response = client.post(f"/runs/{PRIMARY_RUN_ID}/export", json={"format": "report_markdown"})
    assert export_response.status_code == 200
    export_payload = export_response.json()
    assert export_payload["format"] == "report_markdown"
    export_id = export_payload["artifact"]["export_id"]

    download_response = client.get(f"/runs/{PRIMARY_RUN_ID}/exports/{export_id}/download")
    assert download_response.status_code == 200
    assert "text/markdown" in download_response.headers["content-type"]
    assert unique_text in download_response.text

    report_alias_response = client.get(f"/reports/{PRIMARY_RUN_ID}/exports/{export_id}/download")
    assert report_alias_response.status_code == 200
    assert unique_text in report_alias_response.text


def test_completed_report_versions_are_created_from_real_exports_and_diffed(client: TestClient) -> None:
    outline_response = client.get(f"/runs/{PRIMARY_RUN_ID}/outline")
    assert outline_response.status_code == 200
    outline = outline_response.json()

    first_marker = "pytest completed report version one"
    first_sections = [dict(section) for section in outline["sections"]]
    first_sections[0]["body_markdown"] = f"{first_sections[0].get('body_markdown', '')}\n\n{first_marker}".strip()
    assert client.patch(f"/runs/{PRIMARY_RUN_ID}/outline", json={"sections": first_sections}).status_code == 200
    first_export = client.post(f"/runs/{PRIMARY_RUN_ID}/export", json={"format": "report_markdown"})
    assert first_export.status_code == 200

    second_marker = "pytest completed report version two"
    second_sections = [dict(section) for section in first_sections]
    second_sections[0]["body_markdown"] = second_sections[0]["body_markdown"].replace(first_marker, second_marker)
    assert client.patch(f"/runs/{PRIMARY_RUN_ID}/outline", json={"sections": second_sections}).status_code == 200
    second_export = client.post(f"/runs/{PRIMARY_RUN_ID}/export", json={"format": "report_markdown"})
    assert second_export.status_code == 200

    versions_response = client.get(f"/runs/{PRIMARY_RUN_ID}/report/versions")
    assert versions_response.status_code == 200
    versions = versions_response.json()
    assert versions["run_id"] == PRIMARY_RUN_ID
    assert versions["total"] >= 2
    latest_versions = versions["items"][-2:]
    assert latest_versions[0]["source_export_id"] == first_export.json()["artifact"]["export_id"]
    assert latest_versions[1]["source_export_id"] == second_export.json()["artifact"]["export_id"]
    assert latest_versions[0]["content_hash"] != latest_versions[1]["content_hash"]
    assert latest_versions[0]["immutable"] is True
    assert latest_versions[1]["immutable"] is True
    for version in latest_versions:
        assert version["report_id"] == PRIMARY_RUN_ID
        assert version["run_id"] == PRIMARY_RUN_ID
        assert "topic_id" in version
        assert "created_by" in version
        assert version["trigger_source"] in {"api", "api_topic_action:report", "legacy_cli", "rerun_api", "pytest_synthetic_fixture"}
        assert version["status"] == "completed"
        assert version["artifact_references"]["markdown_snapshot"].endswith(".md")
        assert version["markdown_content_reference"] == version["snapshot_path"]
        assert version["source_outline"]["path"].endswith("outline.json")
        assert version["source_outline"]["version_hash"]
        assert version["citation_provenance"]["source_preview_path"].endswith("preview.md")
        assert isinstance(version["provider_source_coverage"]["providers"], list)

    first_snapshot_content = (build_run_paths(PRIMARY_RUN_ID).reports_dir / latest_versions[0]["snapshot_path"].split("reports/")[-1]).read_text(encoding="utf-8")
    assert first_marker in first_snapshot_content
    assert second_marker not in first_snapshot_content

    diff_response = client.get(
        f"/runs/{PRIMARY_RUN_ID}/report/versions/diff",
        params={
            "from_version_id": latest_versions[0]["version_id"],
            "to_version_id": latest_versions[1]["version_id"],
        },
    )
    assert diff_response.status_code == 200
    diff = diff_response.json()
    assert diff["ready"] is True
    assert diff["mode"] == "completed_versions"
    assert diff["summary"]["hash_changed"] is True
    assert diff["summary"]["changed_sections"] >= 1
    assert any(item["status"] == "changed" and second_marker in (item.get("to_excerpt") or "") for item in diff["section_diffs"])


def test_repeated_completed_markdown_exports_append_immutable_versions(client: TestClient) -> None:
    versions_before = client.get(f"/runs/{PRIMARY_RUN_ID}/report/versions")
    assert versions_before.status_code == 200
    original_total = versions_before.json()["total"]

    outline_response = client.get(f"/runs/{PRIMARY_RUN_ID}/outline")
    assert outline_response.status_code == 200
    outline = outline_response.json()

    marker = "pytest repeated completed report export"
    patched_sections = [dict(section) for section in outline["sections"]]
    patched_sections[0]["body_markdown"] = f"{patched_sections[0].get('body_markdown', '')}\n\n{marker}".strip()
    assert client.patch(f"/runs/{PRIMARY_RUN_ID}/outline", json={"sections": patched_sections}).status_code == 200

    first_export = client.post(f"/runs/{PRIMARY_RUN_ID}/export", json={"format": "report_markdown"})
    second_export = client.post(f"/runs/{PRIMARY_RUN_ID}/export", json={"format": "report_markdown"})
    assert first_export.status_code == 200
    assert second_export.status_code == 200

    versions_response = client.get(f"/runs/{PRIMARY_RUN_ID}/report/versions")
    assert versions_response.status_code == 200
    versions_payload = versions_response.json()
    assert versions_payload["total"] == original_total + 2
    latest_versions = versions_payload["items"][-2:]

    assert latest_versions[0]["content_hash"] == latest_versions[1]["content_hash"]
    assert latest_versions[0]["version_id"] != latest_versions[1]["version_id"]
    assert latest_versions[0]["version_number"] + 1 == latest_versions[1]["version_number"]
    assert latest_versions[0]["snapshot_path"] != latest_versions[1]["snapshot_path"]
    assert latest_versions[0]["source_export_id"] == first_export.json()["artifact"]["export_id"]
    assert latest_versions[1]["source_export_id"] == second_export.json()["artifact"]["export_id"]
    assert latest_versions[0]["immutable"] is True
    assert latest_versions[1]["immutable"] is True

    for export_payload in (first_export.json(), second_export.json()):
        download_response = client.get(f"/runs/{PRIMARY_RUN_ID}/exports/{export_payload['artifact']['export_id']}/download")
        assert download_response.status_code == 200
        assert marker in download_response.text


def test_markdown_export_artifact_metadata_is_immutable_and_version_linked(client: TestClient) -> None:
    outline_response = client.get(f"/runs/{PRIMARY_RUN_ID}/outline")
    assert outline_response.status_code == 200
    outline = outline_response.json()
    marker = "pytest export artifact metadata trace"
    patched_sections = [dict(section) for section in outline["sections"]]
    patched_sections[0]["body_markdown"] = f"{patched_sections[0].get('body_markdown', '')}\n\n{marker}".strip()
    assert client.patch(f"/runs/{PRIMARY_RUN_ID}/outline", json={"sections": patched_sections}).status_code == 200

    first_export = client.post(f"/runs/{PRIMARY_RUN_ID}/export", json={"format": "report_markdown"})
    second_export = client.post(f"/runs/{PRIMARY_RUN_ID}/export", json={"format": "report_markdown"})

    assert first_export.status_code == 200
    assert second_export.status_code == 200
    first_payload = first_export.json()
    second_payload = second_export.json()
    first_artifact = first_payload["artifact"]
    second_artifact = second_payload["artifact"]

    assert first_artifact["artifact_id"] == first_artifact["export_id"]
    assert first_artifact["report_id"] == PRIMARY_RUN_ID
    assert first_artifact["run_id"] == PRIMARY_RUN_ID
    assert first_artifact["version_id"] == first_payload["version"]["version_id"]
    assert first_artifact["source_markdown_version_id"] == first_payload["version"]["version_id"]
    assert first_artifact["source_markdown_hash"] == first_payload["version"]["content_hash"]
    assert first_artifact["format"] == "markdown"
    assert first_artifact["mime_type"] == "text/markdown"
    assert first_artifact["file_name"] == "exported_preview.md"
    assert first_artifact["storage_path"].endswith("exported_preview.md")
    assert first_artifact["output"] == first_artifact["storage_path"]
    assert first_artifact["created_at"]
    assert first_artifact["size_bytes"] > 0
    assert len(first_artifact["sha256"]) == 64
    assert first_artifact["renderer"] == "report_preview_markdown_copy"
    assert first_artifact["status"] == "completed"
    assert first_artifact["immutable"] is True
    assert first_artifact["provenance_summary"]["source_run_id"] == PRIMARY_RUN_ID

    first_path = resolve_portable_artifact_path(first_artifact["storage_path"])
    assert first_path.is_file()
    assert first_artifact["size_bytes"] == first_path.stat().st_size
    assert first_artifact["sha256"] == hashlib.sha256(first_path.read_bytes()).hexdigest()
    assert marker in first_path.read_text(encoding="utf-8")

    assert second_artifact["artifact_id"] != first_artifact["artifact_id"]
    assert second_artifact["storage_path"] != first_artifact["storage_path"]
    assert first_path.is_file(), "older completed export artifact must not be overwritten"


def test_completed_report_versions_include_linked_export_artifacts(client: TestClient, monkeypatch) -> None:
    markdown_response = client.post(f"/runs/{PRIMARY_RUN_ID}/export", json={"format": "report_markdown"})
    assert markdown_response.status_code == 200
    markdown_payload = markdown_response.json()

    def fake_pdf_renderer(*, markdown_content: str, output_path: Path, title: str) -> None:
        output_path.write_bytes(b"%PDF-1.4\npytest linked artifact pdf\n%%EOF\n")

    monkeypatch.setattr(exports, "_render_completed_markdown_pdf", fake_pdf_renderer)
    pdf_response = client.post(f"/runs/{PRIMARY_RUN_ID}/export", json={"format": "report_pdf"})
    assert pdf_response.status_code == 200
    pdf_payload = pdf_response.json()

    versions_response = client.get(f"/runs/{PRIMARY_RUN_ID}/report/versions")
    assert versions_response.status_code == 200
    versions = versions_response.json()["items"]

    markdown_version = next(item for item in versions if item["version_id"] == markdown_payload["version"]["version_id"])
    pdf_version = next(item for item in versions if item["version_id"] == pdf_payload["version"]["version_id"])

    assert markdown_version["artifacts"][0]["export_id"] == markdown_payload["artifact"]["export_id"]
    assert markdown_version["artifacts"][0]["format"] == "markdown"
    assert markdown_version["artifacts"][0]["status"] == "completed"
    assert markdown_version["artifacts"][0]["download_available"] is True
    assert markdown_version["artifacts"][0]["sha256"]

    pdf_artifacts = [item for item in pdf_version["artifacts"] if item["format"] == "pdf"]
    assert pdf_artifacts
    assert pdf_artifacts[0]["export_id"] == pdf_payload["artifact"]["export_id"]
    assert pdf_artifacts[0]["mime_type"] == "application/pdf"
    assert pdf_artifacts[0]["source_markdown_version_id"] == pdf_version["version_id"]
    assert pdf_artifacts[0]["download_available"] is True


def test_report_version_diff_compares_binary_artifacts_by_metadata_hash(client: TestClient, monkeypatch) -> None:
    pdf_payloads = []
    pdf_bodies = [
        b"%PDF-1.4\npytest binary diff left\n%%EOF\n",
        b"%PDF-1.4\npytest binary diff right with more bytes\n%%EOF\n",
    ]

    def fake_pdf_renderer(*, markdown_content: str, output_path: Path, title: str) -> None:
        assert markdown_content.strip()
        assert title
        output_path.write_bytes(pdf_bodies[len(pdf_payloads)])

    monkeypatch.setattr(exports, "_render_completed_markdown_pdf", fake_pdf_renderer)
    for _ in range(2):
        response = client.post(f"/runs/{PRIMARY_RUN_ID}/export", json={"format": "report_pdf"})
        assert response.status_code == 200
        pdf_payloads.append(response.json())

    diff_response = client.get(
        f"/runs/{PRIMARY_RUN_ID}/report/versions/diff",
        params={
            "from_version_id": pdf_payloads[0]["version"]["version_id"],
            "to_version_id": pdf_payloads[1]["version"]["version_id"],
        },
    )

    assert diff_response.status_code == 200
    comparison = diff_response.json()["binary_artifact_comparison"]
    assert comparison["mode"] == "metadata_hash_only"
    assert comparison["text_extraction"]["available"] is False
    assert comparison["text_extraction"]["status"] == "unavailable"
    pdf_item = next(item for item in comparison["items"] if item["format"] == "pdf")
    assert pdf_item["availability"] == "both_available"
    assert pdf_item["hash_equal"] is False
    assert pdf_item["metadata_changed"] is True
    assert pdf_item["comparison_basis"] == "sha256_metadata"
    assert pdf_item["semantic_claim"] == "none"
    assert "semantic difference" in pdf_item["warning"]
    assert pdf_item["from"]["sha256"] != pdf_item["to"]["sha256"]
    assert pdf_item["from"]["size_bytes"] != pdf_item["to"]["size_bytes"]
    assert pdf_item["from"]["renderer"] == "completed_report_markdown_reportlab"
    assert pdf_item["to"]["source_markdown_version_id"] == pdf_payloads[1]["version"]["version_id"]


def test_report_version_diff_marks_identical_binary_hash_without_semantic_claim(client: TestClient, monkeypatch) -> None:
    pdf_payloads = []

    def fake_pdf_renderer(*, markdown_content: str, output_path: Path, title: str) -> None:
        assert markdown_content.strip()
        assert title
        output_path.write_bytes(b"%PDF-1.4\npytest identical binary bytes\n%%EOF\n")

    monkeypatch.setattr(exports, "_render_completed_markdown_pdf", fake_pdf_renderer)
    for _ in range(2):
        response = client.post(f"/runs/{PRIMARY_RUN_ID}/export", json={"format": "report_pdf"})
        assert response.status_code == 200
        pdf_payloads.append(response.json())

    diff_response = client.get(
        f"/runs/{PRIMARY_RUN_ID}/report/versions/diff",
        params={
            "from_version_id": pdf_payloads[0]["version"]["version_id"],
            "to_version_id": pdf_payloads[1]["version"]["version_id"],
        },
    )

    assert diff_response.status_code == 200
    comparison = diff_response.json()["binary_artifact_comparison"]
    pdf_item = next(item for item in comparison["items"] if item["format"] == "pdf")
    assert pdf_item["availability"] == "both_available"
    assert pdf_item["hash_equal"] is True
    assert pdf_item["metadata_changed"] is True
    assert pdf_item["semantic_claim"] == "none"


def test_report_version_diff_reports_mixed_binary_artifact_availability(client: TestClient, monkeypatch) -> None:
    markdown_response = client.post(f"/runs/{PRIMARY_RUN_ID}/export", json={"format": "report_markdown"})
    assert markdown_response.status_code == 200

    def fake_pdf_renderer(*, markdown_content: str, output_path: Path, title: str) -> None:
        assert markdown_content.strip()
        assert title
        output_path.write_bytes(b"%PDF-1.4\npytest mixed availability pdf\n%%EOF\n")

    monkeypatch.setattr(exports, "_render_completed_markdown_pdf", fake_pdf_renderer)
    pdf_response = client.post(f"/runs/{PRIMARY_RUN_ID}/export", json={"format": "report_pdf"})
    assert pdf_response.status_code == 200

    diff_response = client.get(
        f"/runs/{PRIMARY_RUN_ID}/report/versions/diff",
        params={
            "from_version_id": markdown_response.json()["version"]["version_id"],
            "to_version_id": pdf_response.json()["version"]["version_id"],
        },
    )

    assert diff_response.status_code == 200
    comparison = diff_response.json()["binary_artifact_comparison"]
    pdf_item = next(item for item in comparison["items"] if item["format"] == "pdf")
    assert pdf_item["availability"] == "to_only"
    assert pdf_item["hash_equal"] is None
    assert pdf_item["from"] is None
    assert pdf_item["to"]["status"] == "completed"


def test_export_and_preview_payloads_expose_portable_locators_not_native_paths(client: TestClient) -> None:
    from app.domain import constants

    export_response = client.post(f"/runs/{PRIMARY_RUN_ID}/export", json={"format": "report_markdown"})
    assert export_response.status_code == 200
    locator = export_response.json()["artifact"]["storage_path"]

    assert locator.startswith("run:"), "export artifacts must be addressed by a portable locator"
    assert artifact_refs.is_portable_ref(locator)
    assert "\\" not in locator, "portable locators never carry a native separator"
    assert resolve_portable_artifact_path(locator).read_text(encoding="utf-8")

    preview_response = client.get(f"/runs/{PRIMARY_RUN_ID}/preview")
    exports_response = client.get(f"/runs/{PRIMARY_RUN_ID}/exports")
    versions_response = client.get(f"/runs/{PRIMARY_RUN_ID}/report/versions")
    assert preview_response.status_code == 200
    assert exports_response.status_code == 200
    assert versions_response.status_code == 200

    versions = versions_response.json()["items"]
    assert versions, "fixture run should expose at least one report version"
    diff_response = client.get(
        f"/runs/{PRIMARY_RUN_ID}/report/versions/diff",
        params={"from_version_id": versions[0]["version_id"], "to_version_id": versions[-1]["version_id"]},
    )
    assert diff_response.status_code == 200

    storage_root = str(constants.STORAGE_ROOT.resolve())
    leaked_forms = {storage_root, storage_root.replace("\\", "/"), storage_root.replace("\\", "\\\\")}
    for label, body in (
        ("export", export_response.text),
        ("preview", preview_response.text),
        ("exports", exports_response.text),
        ("report versions", versions_response.text),
        ("report version diff", diff_response.text),
    ):
        for leaked in leaked_forms:
            assert leaked not in body, f"{label} payload leaked the native storage root"


def test_version_snapshot_reference_cannot_escape_the_run_reports_directory(tmp_path) -> None:
    from app.services import report_versions

    reports_dir = build_run_paths(PRIMARY_RUN_ID).reports_dir.resolve()
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("must never be reachable through a snapshot reference", encoding="utf-8")

    legitimate = report_versions._resolve_snapshot_path(PRIMARY_RUN_ID, "versions/v0001-deadbeef.md")
    assert legitimate is not None
    assert legitimate.is_relative_to(reports_dir)

    # Whatever the resolver accepts must land inside the run's reports directory.
    # `snapshot_path` is the one reference in this module that never passes
    # through artifact_refs, so its containment is asserted directly.
    hostile = [
        str(outside),
        "/etc/passwd",
        "C:/Windows/System32/config/SAM",
        "C:\\Windows\\System32\\config\\SAM",
        "//attacker/share/secret.md",
        "../" * 12 + "outside_secret.txt",
        "..\\..\\..\\..\\outside_secret.txt",
        "versions/../../../../outside_secret.txt",
        "",
    ]
    for ref in hostile:
        resolved = report_versions._resolve_snapshot_path(PRIMARY_RUN_ID, ref)
        assert resolved is None or resolved.is_relative_to(reports_dir), f"escaped the reports dir: {ref!r}"

    # These point outside on every platform, so they must be refused outright
    # rather than merely contained.
    for ref in (str(outside), "../" * 12 + "outside_secret.txt"):
        assert report_versions._resolve_snapshot_path(PRIMARY_RUN_ID, ref) is None, ref
        assert outside.read_text(encoding="utf-8").startswith("must never")


def test_missing_version_snapshot_is_reported_by_identity_not_by_native_path(client: TestClient) -> None:
    from app.domain import constants
    from app.services import report_versions

    first = client.post(f"/runs/{PRIMARY_RUN_ID}/export", json={"format": "report_markdown"})
    assert first.status_code == 200
    from_version_id = first.json()["version"]["version_id"]

    # A diff needs two distinct versions, so change the report body before the
    # second export rather than re-exporting identical content.
    outline = client.get(f"/runs/{PRIMARY_RUN_ID}/outline").json()
    sections = [dict(section) for section in outline["sections"]]
    sections[0]["body_markdown"] = f"{sections[0].get('body_markdown', '')}\n\npytest snapshot availability marker".strip()
    assert client.patch(f"/runs/{PRIMARY_RUN_ID}/outline", json={"sections": sections}).status_code == 200

    second = client.post(f"/runs/{PRIMARY_RUN_ID}/export", json={"format": "report_markdown"})
    assert second.status_code == 200
    to_version_id = second.json()["version"]["version_id"]
    assert to_version_id != from_version_id

    versions = client.get(f"/runs/{PRIMARY_RUN_ID}/report/versions").json()["items"]
    target = next(item for item in versions if item["version_id"] == to_version_id)
    snapshot = report_versions._resolve_snapshot_path(PRIMARY_RUN_ID, str(target["snapshot_path"]))
    assert snapshot is not None and snapshot.is_file()
    snapshot.unlink()

    diff_response = client.get(
        f"/runs/{PRIMARY_RUN_ID}/report/versions/diff",
        params={"from_version_id": from_version_id, "to_version_id": to_version_id},
    )

    assert diff_response.status_code == 200
    payload = diff_response.json()
    assert payload["ready"] is False
    assert payload["warnings"], "an unavailable snapshot must be reported"
    assert all(to_version_id in warning for warning in payload["warnings"])

    storage_root = str(constants.STORAGE_ROOT.resolve())
    for leaked in {storage_root, storage_root.replace("\\", "/"), storage_root.replace("\\", "\\\\")}:
        assert leaked not in diff_response.text, "diff warnings leaked the native storage root"


def test_report_version_diff_reports_missing_binary_artifact_file(client: TestClient, monkeypatch) -> None:
    pdf_payloads = []

    def fake_pdf_renderer(*, markdown_content: str, output_path: Path, title: str) -> None:
        assert markdown_content.strip()
        assert title
        output_path.write_bytes(f"%PDF-1.4\npytest missing binary {len(pdf_payloads)}\n%%EOF\n".encode("utf-8"))

    monkeypatch.setattr(exports, "_render_completed_markdown_pdf", fake_pdf_renderer)
    for _ in range(2):
        response = client.post(f"/runs/{PRIMARY_RUN_ID}/export", json={"format": "report_pdf"})
        assert response.status_code == 200
        pdf_payloads.append(response.json())

    missing_path = resolve_portable_artifact_path(pdf_payloads[1]["artifact"]["storage_path"])
    missing_path.unlink()

    diff_response = client.get(
        f"/runs/{PRIMARY_RUN_ID}/report/versions/diff",
        params={
            "from_version_id": pdf_payloads[0]["version"]["version_id"],
            "to_version_id": pdf_payloads[1]["version"]["version_id"],
        },
    )

    assert diff_response.status_code == 200
    comparison = diff_response.json()["binary_artifact_comparison"]
    pdf_item = next(item for item in comparison["items"] if item["format"] == "pdf")
    assert pdf_item["availability"] == "to_unavailable"
    assert pdf_item["hash_equal"] is None
    assert pdf_item["to"]["status"] == "unavailable"
    assert pdf_item["to"]["sha256"] is None
    assert pdf_item["to"]["download_available"] is False


def test_old_export_manifest_rows_are_backward_compatible_with_metadata_fallback(client: TestClient) -> None:
    paths = build_run_paths(PRIMARY_RUN_ID)
    legacy_path = paths.reports_dir / "legacy_export.md"
    legacy_content = "# Legacy export\n\npytest legacy export metadata fallback"
    legacy_path.write_text(legacy_content, encoding="utf-8")
    exports_path = paths.reports_dir / "exports.json"
    original_exports = read_json(exports_path, default=[]) or []
    legacy_record = {
        "export_id": "legacy-export-without-metadata",
        "format": "report_markdown",
        "output": str(legacy_path),
        "created_at": "2026-04-01T00:00:00Z",
        "status": "ready",
    }

    try:
        write_json(exports_path, [legacy_record])
        response = client.get(f"/runs/{PRIMARY_RUN_ID}/exports")
        assert response.status_code == 200
        artifact = response.json()["items"][0]

        assert artifact["artifact_id"] == legacy_record["export_id"]
        assert artifact["report_id"] == PRIMARY_RUN_ID
        assert artifact["version_id"] is None
        assert artifact["format"] == "markdown"
        assert artifact["mime_type"] == "text/markdown"
        assert artifact["file_name"] == legacy_path.name
        legacy_bytes = legacy_path.read_bytes()
        assert artifact["size_bytes"] == len(legacy_bytes)
        assert artifact["sha256"] == hashlib.sha256(legacy_bytes).hexdigest()
        assert artifact["status"] == "completed"
        assert artifact["immutable"] is False
        assert artifact["provenance_summary"]["metadata_source"] == "legacy_manifest_fallback"
    finally:
        write_json(exports_path, original_exports)
        legacy_path.unlink(missing_ok=True)


def test_missing_completed_export_file_is_reported_unavailable(client: TestClient) -> None:
    export_response = client.post(f"/runs/{PRIMARY_RUN_ID}/export", json={"format": "report_markdown"})
    assert export_response.status_code == 200
    artifact = export_response.json()["artifact"]
    artifact_path = resolve_portable_artifact_path(artifact["storage_path"])
    artifact_path.unlink()

    exports_response = client.get(f"/runs/{PRIMARY_RUN_ID}/exports")

    assert exports_response.status_code == 200
    missing_artifact = next(item for item in exports_response.json()["items"] if item["export_id"] == artifact["export_id"])
    assert missing_artifact["status"] == "unavailable"
    assert missing_artifact["error_message"] == "Export artifact file is missing."


def test_pdf_export_artifact_metadata_tracks_binary_file_and_source_markdown_version(client: TestClient, monkeypatch) -> None:
    markdown_export = client.post(f"/runs/{PRIMARY_RUN_ID}/export", json={"format": "report_markdown"})
    assert markdown_export.status_code == 200
    _source_version = markdown_export.json()["version"]

    def fake_pdf_renderer(*, markdown_content: str, output_path: Path, title: str) -> None:
        output_path.write_bytes(b"%PDF-1.4\npytest binary export artifact\n%%EOF\n")
        assert markdown_content.strip()
        assert title

    monkeypatch.setattr(exports, "_render_completed_markdown_pdf", fake_pdf_renderer)

    export_response = client.post(f"/runs/{PRIMARY_RUN_ID}/export", json={"format": "report_pdf"})

    assert export_response.status_code == 200
    payload = export_response.json()
    artifact = payload["artifact"]
    artifact_path = resolve_portable_artifact_path(artifact["storage_path"])

    assert payload["format"] == "report_pdf"
    assert artifact["format"] == "pdf"
    assert artifact["mime_type"] == "application/pdf"
    assert artifact["file_name"].endswith(".pdf")
    assert artifact["status"] == "completed"
    assert artifact["renderer"] == "completed_report_markdown_reportlab"
    assert artifact["version_id"]
    assert artifact["source_markdown_version_id"] == artifact["version_id"]
    assert artifact["source_markdown_hash"]
    assert artifact["size_bytes"] == artifact_path.stat().st_size
    assert artifact["sha256"] == hashlib.sha256(artifact_path.read_bytes()).hexdigest()


def test_pdf_export_renders_current_completed_markdown_version(client: TestClient) -> None:
    outline_response = client.get(f"/runs/{PRIMARY_RUN_ID}/outline")
    assert outline_response.status_code == 200
    outline = outline_response.json()
    marker = "pytest deterministic report pdf marker"
    patched_sections = [dict(section) for section in outline["sections"]]
    patched_sections[0]["body_markdown"] = f"{patched_sections[0].get('body_markdown', '')}\n\n{marker}".strip()
    assert client.patch(f"/runs/{PRIMARY_RUN_ID}/outline", json={"sections": patched_sections}).status_code == 200

    export_response = client.post(f"/runs/{PRIMARY_RUN_ID}/export", json={"format": "report_pdf"})

    assert export_response.status_code == 200
    payload = export_response.json()
    artifact = payload["artifact"]
    assert artifact["format"] == "pdf"
    assert artifact["mime_type"] == "application/pdf"
    assert artifact["renderer"] == "completed_report_markdown_reportlab"
    assert artifact["version_id"] == artifact["source_markdown_version_id"]
    assert artifact["source_markdown_hash"]

    artifact_path = resolve_portable_artifact_path(artifact["storage_path"])
    assert artifact_path.read_bytes().startswith(b"%PDF")
    assert marker.encode("ascii") in artifact_path.read_bytes()

    download_response = client.get(f"/runs/{PRIMARY_RUN_ID}/exports/{artifact['export_id']}/download")
    assert download_response.status_code == 200
    assert download_response.headers["content-type"].startswith("application/pdf")


def test_pdf_export_capability_reports_unavailable_renderer(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        exports,
        "_pdf_export_availability",
        lambda: {"available": False, "reason": "pytest reportlab unavailable"},
        raising=False,
    )

    response = client.get(f"/runs/{PRIMARY_RUN_ID}/exports")

    assert response.status_code == 200
    capability = response.json()["capabilities"]["report_pdf"]
    assert capability["available"] is False
    assert capability["status"] == "unavailable"
    assert capability["reason"] == "pytest reportlab unavailable"


def test_pdf_export_unavailable_does_not_create_artifact(client: TestClient, monkeypatch) -> None:
    exports_path = build_run_paths(PRIMARY_RUN_ID).reports_dir / "exports.json"
    original_exports = read_json(exports_path, default=[]) or []
    monkeypatch.setattr(
        exports,
        "_pdf_export_availability",
        lambda: {"available": False, "reason": "pytest reportlab unavailable"},
        raising=False,
    )

    export_response = client.post(f"/runs/{PRIMARY_RUN_ID}/export", json={"format": "report_pdf"})

    assert export_response.status_code == 400
    assert "pytest reportlab unavailable" in export_response.text
    assert (read_json(exports_path, default=[]) or []) == original_exports


def test_failed_pdf_render_does_not_create_artifact(client: TestClient, monkeypatch) -> None:
    exports_path = build_run_paths(PRIMARY_RUN_ID).reports_dir / "exports.json"
    original_exports = read_json(exports_path, default=[]) or []

    def fail_render(*_args, **_kwargs):
        raise RuntimeError("pytest deterministic pdf render failed")

    monkeypatch.setattr(exports, "_render_completed_markdown_pdf", fail_render, raising=False)

    export_response = client.post(f"/runs/{PRIMARY_RUN_ID}/export", json={"format": "report_pdf"})

    assert export_response.status_code == 400
    assert "pytest deterministic pdf render failed" in export_response.text
    assert (read_json(exports_path, default=[]) or []) == original_exports


def test_journal_pdf_export_is_journal_style_report_template_not_source_pdf_bundle(client: TestClient) -> None:
    outline_response = client.get(f"/runs/{PRIMARY_RUN_ID}/outline")
    assert outline_response.status_code == 200
    outline = outline_response.json()
    marker = "pytest journal style report pdf marker"
    patched_sections = [dict(section) for section in outline["sections"]]
    patched_sections[0]["body_markdown"] = f"{patched_sections[0].get('body_markdown', '')}\n\n{marker}".strip()
    assert client.patch(f"/runs/{PRIMARY_RUN_ID}/outline", json={"sections": patched_sections}).status_code == 200

    export_response = client.post(f"/runs/{PRIMARY_RUN_ID}/export", json={"format": "journal_pdf"})

    assert export_response.status_code == 200
    payload = export_response.json()
    artifact = payload["artifact"]
    artifact_path = resolve_portable_artifact_path(artifact["storage_path"])
    assert artifact["format"] == "journal_pdf"
    assert artifact["mime_type"] == "application/pdf"
    assert artifact["renderer"] == "completed_report_markdown_journal_reportlab"
    assert artifact["version_id"] == payload["version"]["version_id"]
    assert artifact["source_markdown_version_id"] == payload["version"]["version_id"]
    assert artifact["render_options"]["template_id"] == "journal_style_report_v1"
    assert artifact["render_options"]["source_pdf_handling"] == "links_only_no_redistribution"
    assert artifact_path.read_bytes().startswith(b"%PDF")
    assert marker.encode("ascii") in artifact_path.read_bytes()


def test_journal_pdf_capability_exposes_template_metadata(client: TestClient) -> None:
    response = client.get(f"/runs/{PRIMARY_RUN_ID}/exports")

    assert response.status_code == 200
    capability = response.json()["capabilities"]["journal_pdf"]
    assert capability["available"] is True
    assert capability["renderer"] == "completed_report_markdown_journal_reportlab"
    assert capability["template_id"] == "journal_style_report_v1"
    assert capability["meaning"] == "journal_style_report_pdf"
    assert capability["source_pdf_handling"] == "links_only_no_redistribution"


def test_journal_pdf_unavailable_does_not_create_artifact(client: TestClient, monkeypatch) -> None:
    exports_path = build_run_paths(PRIMARY_RUN_ID).reports_dir / "exports.json"
    original_exports = read_json(exports_path, default=[]) or []
    monkeypatch.setattr(
        exports,
        "_pdf_export_availability",
        lambda: {"available": False, "reason": "pytest journal renderer unavailable"},
        raising=False,
    )

    export_response = client.post(f"/runs/{PRIMARY_RUN_ID}/export", json={"format": "journal_pdf"})

    assert export_response.status_code == 400
    assert "pytest journal renderer unavailable" in export_response.text
    assert (read_json(exports_path, default=[]) or []) == original_exports


def test_failed_markdown_export_does_not_create_version_or_export_record(client: TestClient, monkeypatch) -> None:
    versions_path = build_run_paths(PRIMARY_RUN_ID).reports_dir / "report_versions.json"
    exports_path = build_run_paths(PRIMARY_RUN_ID).reports_dir / "exports.json"
    original_versions = read_json(versions_path, default=[]) or []
    original_exports = read_json(exports_path, default=[]) or []

    def fail_preview(_run_id: str):
        raise RuntimeError("pytest export boundary failed before artifact")

    monkeypatch.setattr(exports.reporting, "build_preview", fail_preview)

    export_response = client.post(f"/runs/{PRIMARY_RUN_ID}/export", json={"format": "report_markdown"})
    assert export_response.status_code == 400
    assert "pytest export boundary failed before artifact" in export_response.text
    assert (read_json(versions_path, default=[]) or []) == original_versions
    assert (read_json(exports_path, default=[]) or []) == original_exports


def test_report_hub_exposes_completed_version_summary_after_export(client: TestClient) -> None:
    export_response = client.post(f"/runs/{PRIMARY_RUN_ID}/export", json={"format": "report_markdown"})
    assert export_response.status_code == 200
    created_version = export_response.json()["version"]
    assert created_version is not None

    reports_response = client.get("/api/reports")
    assert reports_response.status_code == 200
    report_item = next(item for item in reports_response.json()["items"] if item["runId"] == PRIMARY_RUN_ID)

    assert report_item["status"] == "completed"
    assert report_item["completedVersionCount"] >= 1
    assert report_item["latestCompletedVersionId"] == created_version["version_id"]
    assert report_item["latestCompletedVersionAt"] == created_version["created_at"]
    assert report_item["latestCompletedVersionHash"] == created_version["content_hash"]
    assert report_item["exportArtifactSummary"]["total"] >= 1
    assert report_item["exportArtifactSummary"]["downloadable"] >= 1
    assert "markdown" in report_item["exportArtifactSummary"]["formats"]
    assert any(
        artifact["format"] == "markdown"
        and artifact["status"] in {"completed", "ready"}
        and artifact["download_available"] is True
        for artifact in report_item["exportArtifacts"]
    )


def test_completed_report_version_diff_empty_state_requires_two_versions(client: TestClient) -> None:
    versions_response = client.get(f"/runs/{PRIMARY_RUN_ID}/report/versions")
    assert versions_response.status_code == 200
    version_ids = [item["version_id"] for item in versions_response.json()["items"]]
    version_ids = version_ids[:1]

    diff_response = client.get(
        f"/runs/{PRIMARY_RUN_ID}/report/versions/diff",
        params={
            "from_version_id": version_ids[0] if version_ids else "",
            "to_version_id": "",
        },
    )

    assert diff_response.status_code == 200
    payload = diff_response.json()
    assert payload["ready"] is False
    assert payload["empty_reason"]


def test_report_version_api_contract_lists_details_and_path_diff(client: TestClient) -> None:
    outline_response = client.get(f"/runs/{PRIMARY_RUN_ID}/outline")
    assert outline_response.status_code == 200
    outline = outline_response.json()

    first_marker = "pytest report version api one"
    first_sections = [dict(section) for section in outline["sections"]]
    first_sections[0]["body_markdown"] = f"{first_sections[0].get('body_markdown', '')}\n\n{first_marker}".strip()
    assert client.patch(f"/runs/{PRIMARY_RUN_ID}/outline", json={"sections": first_sections}).status_code == 200
    assert client.post(f"/runs/{PRIMARY_RUN_ID}/export", json={"format": "report_markdown"}).status_code == 200

    second_marker = "pytest report version api two"
    second_sections = [dict(section) for section in first_sections]
    second_sections[0]["body_markdown"] = second_sections[0]["body_markdown"].replace(first_marker, second_marker)
    assert client.patch(f"/runs/{PRIMARY_RUN_ID}/outline", json={"sections": second_sections}).status_code == 200
    assert client.post(f"/runs/{PRIMARY_RUN_ID}/export", json={"format": "report_markdown"}).status_code == 200

    list_response = client.get(f"/api/reports/{PRIMARY_RUN_ID}/versions")
    assert list_response.status_code == 200
    version_list = list_response.json()
    assert version_list["report_id"] == PRIMARY_RUN_ID
    latest_versions = version_list["items"][-2:]
    assert [item["version_number"] for item in latest_versions] == sorted(item["version_number"] for item in latest_versions)
    assert latest_versions[0]["artifact_availability"]["markdown_snapshot"] is True
    assert latest_versions[0]["download_available"] is True
    assert latest_versions[0]["provider_source_coverage"]["provider_count"] >= 0

    detail_response = client.get(f"/api/reports/{PRIMARY_RUN_ID}/versions/{latest_versions[0]['version_id']}")
    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["version"]["version_id"] == latest_versions[0]["version_id"]
    assert detail["content_reference"] == latest_versions[0]["snapshot_path"]
    assert first_marker in detail["content"]
    assert detail["source_refs"]["run_id"] == PRIMARY_RUN_ID
    assert "citation_provenance" in detail["version"]

    diff_response = client.get(
        f"/api/reports/{PRIMARY_RUN_ID}/versions/{latest_versions[0]['version_id']}/diff/{latest_versions[1]['version_id']}"
    )
    assert diff_response.status_code == 200
    diff = diff_response.json()
    assert diff["ready"] is True
    assert diff["mode"] == "completed_versions"
    assert diff["metadata_changes"]["content_hash_changed"] is True
    assert diff["semantic_diff_status"]["citation_diff"] == "metadata_reference_only"
    assert any(item["status"] == "changed" and second_marker in (item.get("to_excerpt") or "") for item in diff["section_diffs"])


def test_report_version_api_contract_handles_missing_version_ids(client: TestClient) -> None:
    response = client.get(f"/api/reports/{PRIMARY_RUN_ID}/versions/not-a-version")
    assert response.status_code == 404

    diff_response = client.get(f"/api/reports/{PRIMARY_RUN_ID}/versions/not-a-version/diff/also-missing")
    assert diff_response.status_code == 200
    payload = diff_response.json()
    assert payload["ready"] is False
    assert payload["empty_reason"]


def test_completed_report_version_not_created_for_failed_or_pending_report(client: TestClient) -> None:
    run_path = build_run_paths(PRIMARY_RUN_ID).run_dir / "run.json"
    run_record = read_json(run_path)
    versions_path = build_run_paths(PRIMARY_RUN_ID).reports_dir / "report_versions.json"
    original_versions = read_json(versions_path, default=[]) or []
    failed_run = dict(run_record)
    failed_run["status"] = "failed"
    failed_run["error"] = "pytest failed report guard"
    write_json(run_path, failed_run)

    export_response = client.post(f"/runs/{PRIMARY_RUN_ID}/export", json={"format": "report_markdown"})
    assert export_response.status_code == 200
    assert export_response.json().get("version") is None

    versions_response = client.get(f"/runs/{PRIMARY_RUN_ID}/report/versions")
    assert versions_response.status_code == 200
    assert versions_response.json()["items"] == original_versions


def test_old_report_without_versions_lists_empty_state(client: TestClient) -> None:
    versions_path = build_run_paths(PRIMARY_RUN_ID).reports_dir / "report_versions.json"
    versions_path.unlink(missing_ok=True)

    versions_response = client.get(f"/runs/{PRIMARY_RUN_ID}/report/versions")
    assert versions_response.status_code == 200
    payload = versions_response.json()
    assert payload["run_id"] == PRIMARY_RUN_ID
    assert payload["items"] == []
    assert payload["total"] == 0


def test_run_events_contract_streams_sse_without_mock_events(client: TestClient) -> None:
    with client.stream("GET", f"/runs/{PRIMARY_RUN_ID}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        first_chunk = next(response.iter_text())

    assert first_chunk.startswith(("data:", ": keepalive", "event: end"))
