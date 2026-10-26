from __future__ import annotations

import hashlib
import io
import json
import socket
import stat
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.services import prototype_living_topics as living
from app.services import prototype_project_bundle as bundles
from app.services import prototype_real_corpus as real
from app.services import prototype_workspace as workspace
from tests.test_prototype_living_topics import _current_observation, _enable_weekly
from tests.test_prototype_real_corpus import _fixture_network
from tests.test_prototype_workspace import (
    _acquire_real,
    _established_real_candidates,
    _wait_for_run,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _export_bundle(client: TestClient, topic_ids: list[str] | None = None) -> tuple[dict, bytes]:
    preview_response = client.post(
        "/prototype/workspace/bundles/export/preview",
        json={"selected_topic_ids": topic_ids},
    )
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    confirm_response = client.post(
        "/prototype/workspace/bundles/export",
        json={
            "selected_topic_ids": topic_ids,
            "preview_digest": preview["preview_digest"],
            "confirmed": True,
        },
    )
    assert confirm_response.status_code == 200, confirm_response.text
    exported = confirm_response.json()
    download = client.get(exported["download_url"])
    assert download.status_code == 200, download.text
    return exported, download.content


def _receive_bundle(client: TestClient, data: bytes, filename: str = "research.xrmlbundle") -> dict:
    response = client.post(
        "/prototype/workspace/bundles/imports",
        content=data,
        headers={
            "content-type": "application/vnd.xrml.research-project+zip",
            "x-xrml-bundle-filename": filename,
        },
    )
    assert response.status_code == 202, response.text
    return response.json()


def _confirm_import(client: TestClient, preview: dict) -> dict:
    response = client.post(
        f"/prototype/workspace/bundles/imports/{preview['import_id']}/confirm",
        json={"preview_digest": preview["preview_digest"], "confirmed": True},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _zip_bytes(entries: list[tuple[str, bytes, int | None]], *, compression: int = zipfile.ZIP_STORED) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression, allowZip64=False) as archive:
        for name, content, external_attr in entries:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = compression
            info.create_system = 3
            info.external_attr = external_attr if external_attr is not None else (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, content)
    return output.getvalue()


def _replace_entry(bundle_data: bytes, path: str, content: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(bundle_data), "r") as archive:
        entries = [(info.filename, archive.read(info), info.external_attr) for info in archive.infolist()]
    return _zip_bytes(
        [(name, content if name == path else data, mode) for name, data, mode in entries]
    )


def _entries(bundle_data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(bundle_data), "r") as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


def _reseal(entries: dict[str, bytes]) -> bytes:
    project = json.loads(entries["project.json"])
    record_files = {path: entries[path] for path in bundles.RECORD_PATHS.values()}
    semantic_digest = bundles._semantic_digest(project["selected_topic_ids"], record_files)
    project["project_semantic_digest"] = semantic_digest
    entries["project.json"] = bundles._canonical_bytes(project)
    declared = [
        {"path": path, "size_bytes": len(entries[path]), "sha256": bundles._sha256_bytes(entries[path])}
        for path in sorted(path for path in entries if path != "manifest.json")
    ]
    manifest = json.loads(entries["manifest.json"])
    manifest["project_semantic_digest"] = semantic_digest
    manifest["entries"] = declared
    manifest["bundle_content_digest"] = bundles._digest(declared)
    manifest["bundle_id"] = f"bundle-{manifest['bundle_content_digest'][:20]}"
    manifest_base = {key: value for key, value in manifest.items() if key != "manifest_payload_digest"}
    manifest["manifest_payload_digest"] = bundles._digest(manifest_base)
    entries["manifest.json"] = bundles._canonical_bytes(manifest)
    return _zip_bytes([(path, entries[path], None) for path in sorted(entries)])


def _encrypted_flag(data: bytes) -> bytes:
    output = bytearray(data)
    local = output.find(b"PK\x03\x04")
    central = output.find(b"PK\x01\x02")
    assert local >= 0 and central >= 0
    output[local + 6 : local + 8] = (int.from_bytes(output[local + 6 : local + 8], "little") | 1).to_bytes(2, "little")
    output[central + 8 : central + 10] = (int.from_bytes(output[central + 8 : central + 10], "little") | 1).to_bytes(2, "little")
    return bytes(output)


def _backslash_filename_archive() -> bytes:
    data = _zip_bytes([("records/topics.jsonl", b"", None)])
    assert data.count(b"records/topics.jsonl") == 2
    return data.replace(b"records/topics.jsonl", b"records\\topics.jsonl")


@pytest.fixture
def default_bundle_client(tmp_path: Path, app_factory):
    data_root = (tmp_path / "prototype-v0.7-default").resolve()
    with TestClient(
        app_factory(
            AUTH_MODE="disabled",
            XRML_PROTOTYPE_MODE="1",
            XRML_PROTOTYPE_REAL_CORPUS_NETWORK="0",
            XRML_DATA_DIR=str(data_root),
            XRML_RUN_EXECUTOR_POLL_SECONDS="0.05",
        )
    ) as client:
        yield client, data_root


def test_bundle_export_is_explicit_deterministic_complete_and_side_effect_free(default_bundle_client) -> None:
    client, data_root = default_bundle_client
    workspace_payload = client.get("/prototype/workspace").json()
    workspace_path = data_root / "prototype-workspace" / "workspace.json"
    before = (workspace_path.stat().st_size, workspace_path.stat().st_mtime_ns, _sha256(workspace_path))

    first_preview = client.post(
        "/prototype/workspace/bundles/export/preview",
        json={"selected_topic_ids": None},
    ).json()
    second_preview = client.post(
        "/prototype/workspace/bundles/export/preview",
        json={"selected_topic_ids": None},
    ).json()
    assert first_preview == second_preview
    assert first_preview["writes"] == 0
    assert first_preview["provider_calls"] == 0
    assert first_preview["network_requests"] == 0
    assert first_preview["source_prototype_version"] == "v0.7"
    assert first_preview["archive_size_bytes"] > 0
    assert first_preview["entry_count"] == len(bundles.REQUIRED_PATHS)
    assert first_preview["validation_status"] == {
        "archive_safety": "VERIFIED",
        "manifest": "VERIFIED",
        "schema": "VERIFIED",
        "integrity": "VERIFIED",
        "lineage": "VERIFIED",
        "rights": "PRESERVED",
    }
    assert first_preview["rights_summary"]["records"] == first_preview["record_counts"]["rights"]
    assert "full_text_bodies_and_raw_jats_xml" in first_preview["excluded_content"]
    assert first_preview["selected_topic_ids"] == sorted(
        topic["topic_id"] for topic in workspace_payload["topics"]
    )
    assert (workspace_path.stat().st_size, workspace_path.stat().st_mtime_ns, _sha256(workspace_path)) == before

    denied = client.post(
        "/prototype/workspace/bundles/export",
        json={
            "selected_topic_ids": None,
            "preview_digest": first_preview["preview_digest"],
            "confirmed": False,
        },
    )
    assert denied.status_code == 400
    exported, data = _export_bundle(client)
    repeated, repeated_data = _export_bundle(client)
    assert exported["project_semantic_digest"] == repeated["project_semantic_digest"]
    assert exported["bundle_content_digest"] == repeated["bundle_content_digest"]
    assert data == repeated_data
    assert (workspace_path.stat().st_size, workspace_path.stat().st_mtime_ns, _sha256(workspace_path)) == before

    validated = bundles._validate_bundle_bytes(data)
    assert validated["project"]["schema_version"] == bundles.BUNDLE_SCHEMA
    assert validated["project"]["full_text_embedded"] is False
    assert validated["project"]["runtime_database_embedded"] is False
    assert validated["project"]["raw_provider_output_embedded"] is False
    with zipfile.ZipFile(io.BytesIO(data), "r") as archive:
        assert bundles.REQUIRED_PATHS <= set(archive.namelist())
        assert archive.namelist() == sorted(archive.namelist())
        combined = b"\n".join(archive.read(name) for name in archive.namelist()).lower()
    assert str(data_root).lower().encode() not in combined
    assert b".sqlite3" not in combined
    assert b"<article" not in combined
    assert b"activation_token" not in combined


def test_fresh_import_is_previewed_atomic_idempotent_offline_and_restart_safe(
    default_bundle_client,
    tmp_path: Path,
    app_factory,
) -> None:
    source_client, _source_root = default_bundle_client
    source_export, data = _export_bundle(source_client)
    target_root = (tmp_path / "prototype-v0.7-target").resolve()
    target_workspace = target_root / "prototype-workspace" / "workspace.json"

    with TestClient(
        app_factory(
            AUTH_MODE="disabled",
            XRML_PROTOTYPE_MODE="1",
            XRML_PROTOTYPE_REAL_CORPUS_NETWORK="0",
            XRML_DATA_DIR=str(target_root),
        )
    ) as target:
        assert not target_workspace.exists()
        received = _receive_bundle(target, data)
        assert received["collision_status"] == "NO_CONFLICT"
        assert received["target_writes"] == 0
        assert received["activation_posture"] == bundles.IMPORTED_POSTURE
        assert received["rollback_status"] == "NOT_REQUIRED"
        assert set(received["validation_status"].values()) == {"VERIFIED", "PRESERVED"}
        assert not target_workspace.exists()
        imported = _confirm_import(target, received)
        assert imported["status"] == "IMPORTED"
        assert imported["target_writes"] == 1
        assert imported["atomic_replace"] is True
        assert target_workspace.is_file()
        workspace_payload = target.get("/prototype/workspace").json()
        assert len(workspace_payload["topics"]) == 3
        assert workspace_payload["bundle_projects"][0]["project_semantic_digest"] == source_export[
            "project_semantic_digest"
        ]
        assert workspace_payload["bundle_projects"][0]["source_prototype_version"] == "v0.7"
        assert workspace_payload["bundle_projects"][0]["imported_at"]
        assert all(topic["bundle_import"]["automatic_execution_enabled"] is False for topic in workspace_payload["topics"])
        assert all(topic["living_schedule"]["enabled"] is False for topic in workspace_payload["topics"])
        run_attempt = target.post(
            f"/prototype/workspace/topics/{workspace.SYNTHETIC_TOPIC_ID}/runs",
            json={"snapshot_id": None, "confirmed": True},
        )
        assert run_attempt.status_code == 409
        assert run_attempt.json()["detail"]["code"] == "bundle_import_execution_disabled"

        repeated = _receive_bundle(target, data)
        assert repeated["collision_status"] == "EXACT_BUNDLE_ALREADY_IMPORTED"
        before_hash = _sha256(target_workspace)
        idempotent = _confirm_import(target, repeated)
        assert idempotent["idempotent"] is True
        assert idempotent["target_writes"] == 0
        assert _sha256(target_workspace) == before_hash

        reexport, reexported_data = _export_bundle(target)
        assert reexport["project_semantic_digest"] == source_export["project_semantic_digest"]
        assert reexport["bundle_content_digest"] == source_export["bundle_content_digest"]
        assert reexported_data == data

    with TestClient(
        app_factory(
            AUTH_MODE="disabled",
            XRML_PROTOTYPE_MODE="1",
            XRML_PROTOTYPE_REAL_CORPUS_NETWORK="0",
            XRML_DATA_DIR=str(target_root),
        )
    ) as reopened:
        payload = reopened.get("/prototype/workspace").json()
        assert payload["bundle_projects"][0]["status"] == "IMPORTED"
        assert payload["bundle_projects"][0]["activation_posture"] == bundles.IMPORTED_POSTURE


def test_integrated_four_topic_project_survives_export_import_and_reexport(
    tmp_path: Path,
    app_factory,
    monkeypatch,
) -> None:
    source_root = (tmp_path / "prototype-v0.7-integrated-source").resolve()
    fixture_clients = [_fixture_network(candidates=_established_real_candidates()) for _ in range(12)]
    monkeypatch.setattr(real, "PmcNetworkClient", lambda: fixture_clients.pop(0))

    def forbidden_socket(*_args, **_kwargs):
        raise AssertionError("actual external network attempted")

    monkeypatch.setattr(socket, "create_connection", forbidden_socket)
    env = {
        "AUTH_MODE": "disabled",
        "XRML_PROTOTYPE_MODE": "1",
        "XRML_PROTOTYPE_REAL_CORPUS_NETWORK": "1",
        "XRML_PROTOTYPE_LIVING_SCHEDULER_ENABLED": "0",
        "XRML_DATA_DIR": str(source_root),
        "XRML_RUN_EXECUTOR_POLL_SECONDS": "0.05",
    }
    with TestClient(app_factory(**env)) as source:
        initial = source.get("/prototype/workspace").json()
        synthetic = next(item for item in initial["topics"] if item["topic_id"] == workspace.SYNTHETIC_TOPIC_ID)
        synthetic_run_response = source.post(
            f"/prototype/workspace/topics/{workspace.SYNTHETIC_TOPIC_ID}/runs",
            json={"snapshot_id": synthetic["active_snapshot_id"], "confirmed": True},
        )
        assert synthetic_run_response.status_code == 200, synthetic_run_response.text
        _wait_for_run(synthetic_run_response.json()["run_id"])

        acquired = _acquire_real(source)
        real_topic = source.get(f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}").json()
        four_paper_snapshot = real_topic["active_snapshot"]
        assert four_paper_snapshot["included_count"] == 4
        before_analysis_response = source.post(
            f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/gap-analyses",
            json={"snapshot_id": four_paper_snapshot["snapshot_id"]},
        )
        assert before_analysis_response.status_code == 201, before_analysis_response.text

        schedule = _enable_weekly()
        no_change = living.execute_due_schedule(
            workspace.REAL_TOPIC_ID,
            now=schedule["next_due_at"],
            source_probe=lambda: _current_observation(source),
        )
        assert no_change["refresh"]["status"] == "NO_MATERIAL_CHANGE"
        living.save_schedule(
            workspace.REAL_TOPIC_ID,
            {
                "enabled": False,
                "cadence": "weekly",
                "time_zone": "Asia/Seoul",
                "local_time": "09:00",
                "network_policy": "disabled",
                "briefing_policy": "no_change_and_material_changes",
            },
        )

        held_item = next(item for item in four_paper_snapshot["items"] if item.get("pmcid") == "PMC7465609")
        change_response = source.post(
            f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/curation/preview",
            json={
                "source_snapshot_id": four_paper_snapshot["snapshot_id"],
                "corpus_item_id": held_item["corpus_item_id"],
                "requested_state": "hold",
                "reason_code": "quality_or_relevance_hold",
                "note": "Held for bounded local relevance review.",
            },
        )
        assert change_response.status_code == 200, change_response.text
        applied_response = source.post(
            f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/changesets/{change_response.json()['changeset_id']}/apply",
            json={"confirmed": True},
        )
        assert applied_response.status_code == 200, applied_response.text
        three_paper_snapshot = applied_response.json()
        assert three_paper_snapshot["included_count"] == 3
        briefing_response = source.post(
            f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/briefings/curation-delta",
            json={
                "before_snapshot_id": four_paper_snapshot["snapshot_id"],
                "after_snapshot_id": three_paper_snapshot["snapshot_id"],
            },
        )
        assert briefing_response.status_code == 200, briefing_response.text
        curated_run_response = source.post(
            f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/runs",
            json={"snapshot_id": three_paper_snapshot["snapshot_id"], "confirmed": True},
        )
        assert curated_run_response.status_code == 200, curated_run_response.text
        curated_run = curated_run_response.json()
        after_analysis_response = source.post(
            f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/gap-analyses",
            json={"snapshot_id": three_paper_snapshot["snapshot_id"]},
        )
        assert after_analysis_response.status_code == 201, after_analysis_response.text
        after_analysis = after_analysis_response.json()
        recommendations = after_analysis["recommendations"][:3]
        choices = (
            (recommendations[0], "accepted", "clear_falsifiable_question"),
            (recommendations[1], "held", "needs_more_evidence"),
            (recommendations[2], "rejected", "feasibility_uncertain"),
        )
        for recommendation, status, reason in choices:
            decision = source.post(
                f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/recommendations/{recommendation['recommendation_id']}/decisions",
                json={"requested_status": status, "reason_code": reason, "bounded_note": "Bundle proof decision."},
            )
            assert decision.status_code == 201, decision.text
        promotion = source.post(
            f"/prototype/workspace/topics/{workspace.REAL_TOPIC_ID}/recommendations/{recommendations[0]['recommendation_id']}/promote",
            json={"confirmed": True},
        )
        assert promotion.status_code == 201, promotion.text

        final_source = source.get("/prototype/workspace").json()
        assert len(final_source["topics"]) == 4
        source_real = next(item for item in final_source["topics"] if item["topic_id"] == workspace.REAL_TOPIC_ID)
        assert len(source_real["briefings"]) >= 2
        assert source_real["living_schedule"]["enabled"] is False
        source_decisions = sum(
            len(item["decision_history"])
            for item in source_real["research_gaps_workspace"]["current_analysis"]["recommendations"]
        )
        assert source_decisions == 3
        workspace_path = source_root / "prototype-workspace" / "workspace.json"
        source_before_export = _sha256(workspace_path)
        selected_ids = sorted(item["topic_id"] for item in final_source["topics"])
        exported, data = _export_bundle(source, selected_ids)
        assert _sha256(workspace_path) == source_before_export
        assert exported["record_counts"]["topics"] == 4
        assert exported["record_counts"]["corpus_snapshots"] >= 3
        assert exported["record_counts"]["briefings"] >= 2
        assert exported["record_counts"]["recommendation_decisions"] == 3
        assert exported["record_counts"]["topic_origins"] == 1
        assert exported["record_counts"]["runs"] >= 3
        assert exported["provider_calls"] == 0
        assert exported["network_requests"] == 0

    target_root = (tmp_path / "prototype-v0.7-integrated-target").resolve()
    target_workspace_path = target_root / "prototype-workspace" / "workspace.json"
    with TestClient(
        app_factory(
            AUTH_MODE="disabled",
            XRML_PROTOTYPE_MODE="1",
            XRML_PROTOTYPE_REAL_CORPUS_NETWORK="0",
            XRML_PROTOTYPE_LIVING_SCHEDULER_ENABLED="0",
            XRML_DATA_DIR=str(target_root),
        )
    ) as target:
        received = _receive_bundle(target, data)
        assert not target_workspace_path.exists()
        imported = _confirm_import(target, received)
        assert imported["project_semantic_digest"] == exported["project_semantic_digest"]
        target_workspace = target.get("/prototype/workspace").json()
        assert len(target_workspace["topics"]) == 4
        target_real = next(item for item in target_workspace["topics"] if item["topic_id"] == workspace.REAL_TOPIC_ID)
        assert target_real["active_snapshot"]["included_count"] == 3
        assert {item["pmcid"] for item in target_real["active_snapshot"]["items"] if item["curation_state"] == "hold"} == {"PMC7465609"}
        assert len(target_real["briefings"]) >= 2
        target_decisions = sum(
            len(item["decision_history"])
            for item in target_real["research_gaps_workspace"]["current_analysis"]["recommendations"]
        )
        assert target_decisions == 3
        assert target_real["living_schedule"]["enabled"] is False
        detail = target.get(f"/prototype/workspace/bundles/projects/{imported['project_id']}")
        assert detail.status_code == 200, detail.text
        portable = detail.json()["portable_records"]
        assert portable["evidence"]
        assert portable["rights"]
        assert portable["report_versions"]
        evidence = portable["evidence"][0]
        evidence_detail = target.get(
            f"/prototype/workspace/bundles/runs/{evidence['run_id']}/evidence/{evidence['evidence_id']}"
        )
        assert evidence_detail.status_code == 200, evidence_detail.text
        assert evidence_detail.json()["evidence"]["evidence_id"] == evidence["evidence_id"]
        assert evidence_detail.json()["full_text_embedded"] is False
        version = portable["report_versions"][0]
        report = target.get(
            f"/prototype/workspace/bundles/runs/{version['run_id']}/report-versions/{version['report_version_id']}"
        )
        assert report.status_code == 200, report.text
        assert report.json()["content"]
        reexported, reexported_data = _export_bundle(target, selected_ids)
        assert reexported["project_semantic_digest"] == exported["project_semantic_digest"]
        assert reexported_data == data
        assert curated_run["run_id"] in {row["run_id"] for row in portable["runs"]}
        assert acquired["run"]["run_id"] in {row["run_id"] for row in portable["runs"]}

    with TestClient(
        app_factory(
            AUTH_MODE="disabled",
            XRML_PROTOTYPE_MODE="1",
            XRML_PROTOTYPE_REAL_CORPUS_NETWORK="0",
            XRML_PROTOTYPE_LIVING_SCHEDULER_ENABLED="0",
            XRML_DATA_DIR=str(target_root),
        )
    ) as reopened:
        after_restart = reopened.get("/prototype/workspace").json()
        assert len(after_restart["topics"]) == 4
        assert after_restart["bundle_projects"][0]["project_semantic_digest"] == exported[
            "project_semantic_digest"
        ]


def test_archive_path_type_compression_and_size_controls_fail_before_target_write(
    default_bundle_client,
    monkeypatch,
) -> None:
    client, data_root = default_bundle_client
    client.get("/prototype/workspace")
    workspace_path = data_root / "prototype-workspace" / "workspace.json"
    before = _sha256(workspace_path)
    regular = (stat.S_IFREG | 0o644) << 16
    symlink = (stat.S_IFLNK | 0o777) << 16
    attacks = [
        (_zip_bytes([("../escape.json", b"{}", regular)]), "bundle_archive_path_invalid"),
        (_zip_bytes([("/manifest.json", b"{}", regular)]), "bundle_archive_path_invalid"),
        (_zip_bytes([("//server/share/manifest.json", b"{}", regular)]), "bundle_archive_path_invalid"),
        (_zip_bytes([("C:/manifest.json", b"{}", regular)]), "bundle_archive_alternate_stream_forbidden"),
        (_backslash_filename_archive(), "bundle_archive_path_invalid"),
        (_zip_bytes([("manifest.json:stream", b"{}", regular)]), "bundle_archive_alternate_stream_forbidden"),
        (_zip_bytes([("payload.zip", b"x", regular)]), "bundle_archive_executable_or_nested_archive_forbidden"),
        (_zip_bytes([("manifest.json", b"{}", symlink)]), "bundle_archive_non_regular_entry_forbidden"),
        (_zip_bytes([("manifest.json", b"{}", 0x400)]), "bundle_archive_reparse_entry_forbidden"),
        (
            _zip_bytes(
                [("manifest.json", b"{}", regular), ("MANIFEST.JSON", b"{}", regular)]
            ),
            "bundle_archive_normalized_path_collision",
        ),
        (
            _zip_bytes([("manifest.json", b"A" * 512_000, regular)], compression=zipfile.ZIP_DEFLATED),
            "bundle_archive_compression_ratio_exceeded",
        ),
        (
            _zip_bytes([("manifest.json", b"x" * (bundles.MAX_ENTRY_BYTES + 1), regular)]),
            "bundle_archive_entry_size_exceeded",
        ),
        (_zip_bytes([("manifest.json", b"{}", regular)], compression=zipfile.ZIP_BZIP2), "bundle_archive_compression_unsupported"),
        (_zip_bytes([("tool.py", b"pass\n", regular)]), "bundle_archive_executable_or_nested_archive_forbidden"),
        (b"not-a-zip", "bundle_archive_invalid"),
    ]
    for index, (attack, code) in enumerate(attacks):
        response = client.post(
            "/prototype/workspace/bundles/imports",
            content=attack,
            headers={"x-xrml-bundle-filename": f"attack-{index}.xrmlbundle"},
        )
        assert response.status_code == 409, (code, response.text)
        assert response.json()["detail"]["code"] == code
        assert _sha256(workspace_path) == before

    too_many = _zip_bytes(
        [(f"exports/item-{index}.md", b"x", regular) for index in range(bundles.MAX_ENTRY_COUNT + 1)]
    )
    response = client.post(
        "/prototype/workspace/bundles/imports",
        content=too_many,
        headers={"x-xrml-bundle-filename": "too-many.xrmlbundle"},
    )
    assert response.json()["detail"]["code"] == "bundle_archive_entry_count_exceeded"
    oversized = client.post(
        "/prototype/workspace/bundles/imports",
        content=b"x" * (bundles.MAX_COMPRESSED_BYTES + 1),
        headers={"x-xrml-bundle-filename": "oversized.xrmlbundle"},
    )
    assert oversized.status_code == 413
    assert oversized.json()["detail"]["code"] == "bundle_archive_compressed_limit_exceeded"
    assert _sha256(workspace_path) == before
    with pytest.raises(bundles.PrototypeProjectBundleError, match="bundle_archive_path_invalid"):
        bundles._validate_archive_path("manifest.json\x00ignored", set())
    monkeypatch.setattr(bundles, "MAX_UNCOMPRESSED_BYTES", 5)
    total_oversized = client.post(
        "/prototype/workspace/bundles/imports",
        content=_zip_bytes([("manifest.json", b"123456", regular)]),
        headers={"x-xrml-bundle-filename": "total-oversized.xrmlbundle"},
    )
    assert total_oversized.json()["detail"]["code"] == "bundle_archive_uncompressed_limit_exceeded"
    assert _sha256(workspace_path) == before


def test_manifest_json_record_and_encryption_tampering_is_rejected(default_bundle_client) -> None:
    client, data_root = default_bundle_client
    client.get("/prototype/workspace")
    _exported, valid = _export_bundle(client)
    workspace_path = data_root / "prototype-workspace" / "workspace.json"
    before = _sha256(workspace_path)
    project = json.loads(_entries(valid)["project.json"])
    unsupported = {**project, "schema_version": "xrml_research_project_bundle_v999"}
    unknown_source = {**project, "source_workspace_schema": "prototype_workspace_v999"}
    variants = [
        (_replace_entry(valid, "project.json", b'{"schema_version":"x","schema_version":"y"}'), "bundle_json_duplicate_key"),
        (_replace_entry(valid, "project.json", b'{"schema_version":NaN}'), "bundle_json_non_finite_number"),
        (_replace_entry(valid, "project.json", b"\xff"), "bundle_json_utf8_required"),
        (_replace_entry(valid, "project.json", b"\xef\xbb\xbf{}"), "bundle_json_bom_forbidden"),
        (_replace_entry(valid, "project.json", b"{\r\n}"), "bundle_json_lf_required"),
        (_replace_entry(valid, "project.json", b"{"), "bundle_json_invalid"),
        (_replace_entry(valid, "project.json", bundles._canonical_bytes(unsupported)), "bundle_version_unsupported"),
        (_reseal({**_entries(valid), "project.json": bundles._canonical_bytes(unknown_source)}), "bundle_source_schema_unsupported"),
        (_replace_entry(valid, bundles.RECORD_PATHS["topics"], b"{}\n"), "bundle_manifest_entry_digest_mismatch"),
        (_encrypted_flag(valid), "bundle_archive_encryption_forbidden"),
    ]
    entries = _entries(valid)
    entries.pop("manifest.json")
    variants.append((_zip_bytes([(path, value, None) for path, value in entries.items()]), "bundle_archive_required_entry_missing"))
    entries = _entries(valid)
    manifest_digest_tamper = json.loads(entries["manifest.json"])
    manifest_digest_tamper["manifest_payload_digest"] = "0" * 64
    variants.append(
        (
            _replace_entry(
                valid,
                "manifest.json",
                bundles._canonical_bytes(manifest_digest_tamper),
            ),
            "bundle_manifest_digest_mismatch",
        )
    )
    content_digest_tamper = json.loads(entries["manifest.json"])
    content_digest_tamper["bundle_content_digest"] = "0" * 64
    content_digest_base = {
        key: value
        for key, value in content_digest_tamper.items()
        if key != "manifest_payload_digest"
    }
    content_digest_tamper["manifest_payload_digest"] = bundles._digest(content_digest_base)
    variants.append(
        (
            _replace_entry(
                valid,
                "manifest.json",
                bundles._canonical_bytes(content_digest_tamper),
            ),
            "bundle_content_digest_mismatch",
        )
    )
    with pytest.warns(UserWarning, match="Duplicate name"):
        duplicate_manifest = _zip_bytes(
            [(path, value, None) for path, value in entries.items()]
            + [("manifest.json", entries["manifest.json"], None)]
        )
    variants.append((duplicate_manifest, "bundle_archive_normalized_path_collision"))
    variants.append(
        (
            _zip_bytes(
                [(path, value, None) for path, value in entries.items()]
                + [("exports/undeclared.md", b"unexpected", None)]
            ),
            "bundle_manifest_entry_digest_mismatch",
        )
    )

    topics_path = bundles.RECORD_PATHS["topics"]
    crlf_entries = _entries(valid)
    crlf_entries[topics_path] = crlf_entries[topics_path].replace(b"\n", b"\r\n")
    variants.append((_reseal(crlf_entries), "bundle_json_lf_required"))
    bom_entries = _entries(valid)
    bom_entries[topics_path] = b"\xef\xbb\xbf" + bom_entries[topics_path]
    variants.append((_reseal(bom_entries), "bundle_json_bom_forbidden"))

    duplicate_entries = _entries(valid)
    duplicate_entries[topics_path] += duplicate_entries[topics_path].splitlines(keepends=True)[0]
    variants.append((_reseal(duplicate_entries), "bundle_record_identity_collision"))

    digest_entries = _entries(valid)
    topic_lines = digest_entries[topics_path].splitlines()
    digest_envelope = json.loads(topic_lines[0])
    digest_envelope["payload"]["description"] = "Tampered without updating the record digest."
    topic_lines[0] = bundles._canonical_bytes(digest_envelope)
    digest_entries[topics_path] = b"\n".join(topic_lines) + b"\n"
    variants.append((_reseal(digest_entries), "bundle_record_digest_mismatch"))

    snapshot_entries = _entries(valid)
    snapshot_path = bundles.RECORD_PATHS["corpus_snapshots"]
    snapshot_lines = snapshot_entries[snapshot_path].splitlines()
    snapshot_envelope = json.loads(snapshot_lines[0])
    snapshot_envelope["payload"]["corpus_item_ids"] = ["missing-item"]
    snapshot_basis = {
        key: snapshot_envelope[key] for key in ("record_type", "record_id", "payload")
    }
    snapshot_envelope["bundle_record_digest"] = bundles._digest(snapshot_basis)
    snapshot_lines[0] = bundles._canonical_bytes(snapshot_envelope)
    snapshot_entries[snapshot_path] = b"\n".join(snapshot_lines) + b"\n"
    variants.append((_reseal(snapshot_entries), "bundle_snapshot_item_reference_missing"))

    project_variants = [
        ({**project, "project_id": "project-mismatch"}, "bundle_project_identity_mismatch"),
        ({**project, "automatic_execution_enabled": True}, "bundle_project_unsafe_posture"),
        ({**project, "import_activation_posture": "ENABLED"}, "bundle_project_activation_posture_invalid"),
        ({**project, "full_text": "forbidden article body"}, "bundle_metadata_forbidden_field"),
        ({**project, "raw_provider_output": "forbidden"}, "bundle_metadata_forbidden_field"),
        (
            {**project, "debug_location": "C:\\private\\workspace"},
            "bundle_record_filesystem_path_forbidden",
        ),
        ({**project, "rights_summary": {"records": 1, "accepted": 1, "requires_review": 0}}, "bundle_project_rights_summary_mismatch"),
        ({**project, "record_counts": {**project["record_counts"], "topics": 999}}, "bundle_project_record_count_mismatch"),
    ]
    for changed_project, code in project_variants:
        changed_entries = _entries(valid)
        changed_entries["project.json"] = bundles._canonical_bytes(changed_project)
        variants.append((_reseal(changed_entries), code))

    semantic_entries = _entries(valid)
    semantic_project = json.loads(semantic_entries["project.json"])
    semantic_project["project_semantic_digest"] = "0" * 64
    semantic_entries["project.json"] = bundles._canonical_bytes(semantic_project)
    semantic_manifest = json.loads(semantic_entries["manifest.json"])
    semantic_manifest["project_semantic_digest"] = "0" * 64
    semantic_declared = [
        {"path": path, "size_bytes": len(semantic_entries[path]), "sha256": bundles._sha256_bytes(semantic_entries[path])}
        for path in sorted(path for path in semantic_entries if path != "manifest.json")
    ]
    semantic_manifest["entries"] = semantic_declared
    semantic_manifest["bundle_content_digest"] = bundles._digest(semantic_declared)
    semantic_base = {
        key: value for key, value in semantic_manifest.items() if key != "manifest_payload_digest"
    }
    semantic_manifest["manifest_payload_digest"] = bundles._digest(semantic_base)
    semantic_entries["manifest.json"] = bundles._canonical_bytes(semantic_manifest)
    variants.append(
        (
            _zip_bytes([(path, value, None) for path, value in sorted(semantic_entries.items())]),
            "bundle_project_semantic_digest_mismatch",
        )
    )
    for index, (variant, code) in enumerate(variants):
        response = client.post(
            "/prototype/workspace/bundles/imports",
            content=variant,
            headers={"x-xrml-bundle-filename": f"tampered-{index}.xrmlbundle"},
        )
        assert response.status_code == 409, (code, response.text)
        assert response.json()["detail"]["code"] == code
        assert _sha256(workspace_path) == before


def test_same_project_semantic_conflict_is_visible_and_non_mutating(
    default_bundle_client,
    tmp_path: Path,
    app_factory,
) -> None:
    source, _source_root = default_bundle_client
    _exported, valid = _export_bundle(source)
    changed_entries = _entries(valid)
    topic_path = bundles.RECORD_PATHS["topics"]
    topic_lines = changed_entries[topic_path].splitlines()
    envelope = json.loads(topic_lines[0])
    envelope["payload"]["description"] = "Semantically different but structurally valid."
    basis = {key: envelope[key] for key in ("record_type", "record_id", "payload")}
    envelope["bundle_record_digest"] = bundles._digest(basis)
    topic_lines[0] = bundles._canonical_bytes(envelope)
    changed_entries[topic_path] = b"\n".join(topic_lines) + b"\n"
    changed = _reseal(changed_entries)
    target_root = (tmp_path / "prototype-v0.7-conflict-target").resolve()
    target_workspace = target_root / "prototype-workspace" / "workspace.json"
    with TestClient(
        app_factory(
            AUTH_MODE="disabled",
            XRML_PROTOTYPE_MODE="1",
            XRML_PROTOTYPE_REAL_CORPUS_NETWORK="0",
            XRML_DATA_DIR=str(target_root),
        )
    ) as target:
        original_preview = _receive_bundle(target, valid)
        _confirm_import(target, original_preview)
        before = _sha256(target_workspace)
        conflict = _receive_bundle(target, changed)
        assert conflict["collision_status"] == "PROJECT_ID_SEMANTIC_CONFLICT"
        denied = target.post(
            f"/prototype/workspace/bundles/imports/{conflict['import_id']}/confirm",
            json={"preview_digest": conflict["preview_digest"], "confirmed": True},
        )
        assert denied.status_code == 409
        assert denied.json()["detail"]["code"] == "bundle_import_project_id_semantic_conflict"
        assert _sha256(target_workspace) == before


def test_atomic_write_failure_rolls_back_without_partial_workspace(
    default_bundle_client,
    tmp_path: Path,
    app_factory,
    monkeypatch,
) -> None:
    from app.repositories import shared_state

    source, _source_root = default_bundle_client
    _exported, valid = _export_bundle(source)
    target_root = (tmp_path / "prototype-v0.7-rollback-target").resolve()
    target_workspace = target_root / "prototype-workspace" / "workspace.json"
    with TestClient(
        app_factory(
            AUTH_MODE="disabled",
            XRML_PROTOTYPE_MODE="1",
            XRML_PROTOTYPE_REAL_CORPUS_NETWORK="0",
            XRML_DATA_DIR=str(target_root),
        )
    ) as target:
        target.get("/prototype/workspace")
        before = _sha256(target_workspace)
        preview = _receive_bundle(target, valid)

        def fail_replace(_source, _target):
            raise OSError("injected atomic replacement failure")

        original_replace = shared_state.os.replace
        monkeypatch.setattr(shared_state.os, "replace", fail_replace)
        try:
            failed = target.post(
                f"/prototype/workspace/bundles/imports/{preview['import_id']}/confirm",
                json={"preview_digest": preview["preview_digest"], "confirmed": True},
            )
        finally:
            monkeypatch.setattr(shared_state.os, "replace", original_replace)
        assert failed.status_code == 409
        assert failed.json()["detail"]["code"] == "bundle_import_atomic_write_failed"
        assert _sha256(target_workspace) == before
        status_response = target.get(f"/prototype/workspace/bundles/imports/{preview['import_id']}")
        assert status_response.status_code == 200
        assert status_response.json()["status"] == "ROLLED_BACK"
        assert not list((target_root / "prototype-workspace").glob("*.tmp"))


def test_record_level_collision_classification_precedes_workspace_conflict() -> None:
    existing_run = {
        "run_id": "run-collision",
        "topic_id": workspace.SYNTHETIC_TOPIC_ID,
        "snapshot_id": "snapshot-a",
        "status": "completed",
    }
    incoming_run = {**existing_run, "status": "failed"}
    state = workspace._default_state()
    state["portable_runs"] = [existing_run]
    state["bundle_projects"] = [
        {"project_id": "project-existing", "project_semantic_digest": "a" * 64}
    ]
    records = {record_type: [] for record_type in bundles.RECORD_PATHS}
    records["runs"] = [bundles._envelope("runs", incoming_run)]
    validated = {
        "project": {"project_id": "project-new", "project_semantic_digest": "b" * 64},
        "records": records,
    }
    assert bundles._collision_status(state, validated) == "RECORD_ID_CONTENT_CONFLICT"
