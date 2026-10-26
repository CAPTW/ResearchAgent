import pytest

from app.repositories.files import read_json, write_json, write_text
from app.repositories.runs import build_run_paths
from app.services import report_versions
from tests.conftest import PRIMARY_RUN_ID


@pytest.fixture
def saved_versions(client):
    paths = build_run_paths(PRIMARY_RUN_ID)
    versions = [report_versions.record_completed_report_version(
        PRIMARY_RUN_ID, source="pytest_integrity", source_artifact_path=str(paths.reports_dir / "fixture.md"),
        content=f"# Local report\n\n## Evidence\nSaved observation {number}.\n") for number in (1, 2)]
    assert all(versions)
    return versions, paths.reports_dir


@pytest.mark.parametrize("replacement", [b"Changed content under the same version", b"# Local report\n\xff"])
def test_changed_or_invalid_snapshot_is_not_served_as_original(saved_versions, replacement):
    versions, root = saved_versions
    target = root / versions[0]["snapshot_path"]
    target.write_bytes(replacement)
    detail = report_versions.get_report_version_detail(PRIMARY_RUN_ID, versions[0]["version_id"])
    assert detail["content"] is None
    assert detail["warnings"] and not detail["version"]["download_available"]
    diff = report_versions.diff_report_versions(PRIMARY_RUN_ID, from_version_id=versions[0]["version_id"], to_version_id=versions[1]["version_id"])
    assert not diff["ready"] and diff["section_diffs"] == []
    assert diff["summary"]["hash_changed"] is None


def test_verified_legacy_missing_and_unknown_integrity_are_distinct(saved_versions):
    versions, root = saved_versions
    first = versions[0]
    detail = report_versions.get_report_version_detail(PRIMARY_RUN_ID, first["version_id"])
    assert detail["content_integrity"] == "SHA256_VERIFIED"
    manifest_path = root / "report_versions.json"
    manifest = read_json(manifest_path)
    record = next(v for v in manifest if v["version_id"] == first["version_id"])
    record.pop("content_sha256")
    write_json(manifest_path, manifest)
    assert report_versions.get_report_version_detail(PRIMARY_RUN_ID, first["version_id"])["content_integrity"] == "LEGACY_HASH_VERIFIED"
    record.pop("content_hash")
    write_json(manifest_path, manifest)
    unknown = report_versions.get_report_version_detail(PRIMARY_RUN_ID, first["version_id"])
    assert unknown["content"] is None and unknown["content_integrity"] == "DIGEST_UNAVAILABLE"
    # Missing files are never replaced by the current source artifact.
    (root / first["snapshot_path"]).unlink()
    write_text(root / "fixture.md", "New unrelated current report")
    missing = report_versions.get_report_version_detail(PRIMARY_RUN_ID, first["version_id"])
    assert missing["content"] is None and missing["content_integrity"] == "SNAPSHOT_MISSING"


def test_windows_newline_expansion_preserves_original_content(client):
    original = "# Report\r\n\r\nWindows input\r\n"
    version = report_versions.record_completed_report_version(PRIMARY_RUN_ID, source="pytest_integrity", source_artifact_path="unused", content=original)
    detail = report_versions.get_report_version_detail(PRIMARY_RUN_ID, version["version_id"])
    assert detail["content"] == original
    assert detail["content_integrity"] == "SHA256_VERIFIED"
