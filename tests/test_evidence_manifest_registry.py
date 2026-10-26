from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.cli import evidence_manifest
from app.services import evidence_manifest_registry


KNOWN_MANIFESTS = [
    "data/evidence_manifests/m19.3-large-language-models-2604.26498v1.json",
    "data/evidence_manifests/m21-large-language-models-2605.01143v1.json",
    "data/evidence_manifests/m23-large-language-models-2604.14475v1.json",
]


def _sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _manifest(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _registry_entry(source_manifest_path: str | Path, **overrides) -> dict:
    payload = _manifest(source_manifest_path)
    entry = {
        "paper_id": payload["paper_id"],
        "manifest_path": Path(source_manifest_path).as_posix(),
        "manifest_sha256": _sha256(source_manifest_path),
        "manifest_kind": payload["manifest_kind"],
        "manifest_schema_version": payload["schema_version"],
        "pdf_sha256": payload["pdf_sha256"],
        "source_text_sha256": payload["source_text_sha256"],
        "artifact_ref": payload["artifact_ref"],
        "admitted_status": "admitted",
        "last_verified_status": "verified",
        "portability_flags": payload.get("portability", {}),
        "notes": "",
    }
    entry.update(overrides)
    return entry


def _registry(entries: list[dict], **overrides) -> dict:
    payload = {
        "registry_schema_version": 1,
        "topic_id": "large-language-models",
        "manifest_entries": entries,
    }
    payload.update(overrides)
    return payload


def _write_registry(tmp_path: Path, payload: dict | list | str | None, *, name: str = "registry.json") -> Path:
    path = tmp_path / name
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _known_registry_path(tmp_path: Path, **overrides) -> Path:
    entries = [_registry_entry(path) for path in KNOWN_MANIFESTS]
    return _write_registry(tmp_path, _registry(entries, **overrides))


def _check(path: str | Path, **kwargs) -> dict:
    return evidence_manifest_registry.check_manifest_registry(
        path,
        topic_id="large-language-models",
        **kwargs,
    )


def _reason_codes(result: dict) -> set[str]:
    codes = {reason["code"] for reason in result.get("reasons", [])}
    for entry in result.get("entries", []):
        codes.update(reason["code"] for reason in entry.get("reasons", []))
    return codes


def test_temp_registry_with_current_three_manifests_is_verified(tmp_path: Path) -> None:
    registry_path = _known_registry_path(tmp_path)

    result = _check(registry_path)

    assert result["status"] == "registry_verified"
    assert result["entry_count"] == 3
    assert result["verified_count"] == 3
    assert result["rejected_count"] == 0
    assert result["drift_count"] == 0
    assert result["duplicate_count"] == 0
    assert result["safe_to_use_registry_for_admission"] is True
    assert result["safe_to_claim_registry_integrity"] is True
    assert result["safe_to_claim_clean_clone_reproducible"] is False
    assert result["safe_to_claim_arbitrary_manifest_automation"] is False
    assert result["network_attempted"] is False
    assert result["write_attempted"] is False
    assert [entry["paper_id"] for entry in result["entries"]] == ["2604.26498v1", "2605.01143v1", "2604.14475v1"]


def test_registry_report_contract_includes_stable_fields(tmp_path: Path) -> None:
    registry_path = _known_registry_path(tmp_path)

    result = _check(registry_path)

    assert {
        "status",
        "registry_path",
        "registry_schema_version",
        "topic_id",
        "entry_count",
        "verified_count",
        "rejected_count",
        "warning_count",
        "drift_count",
        "duplicate_count",
        "missing_manifest_count",
        "safe_to_use_registry_for_admission",
        "safe_to_claim_registry_integrity",
        "safe_to_claim_clean_clone_reproducible",
        "safe_to_claim_arbitrary_manifest_automation",
        "network_attempted",
        "write_attempted",
        "entries",
        "reasons",
    } <= set(result)
    entry = result["entries"][0]
    assert {
        "paper_id",
        "manifest_path",
        "manifest_exists",
        "manifest_sha256_expected",
        "manifest_sha256_actual",
        "manifest_hash_matched",
        "manifest_kind_matched",
        "schema_version_matched",
        "topic_matched",
        "pdf_hash_matched",
        "source_text_hash_matched",
        "artifact_ref_matched",
        "status",
        "reasons",
    } <= set(entry)


def test_missing_registry_returns_manifest_registry_missing(tmp_path: Path) -> None:
    result = _check(tmp_path / "missing-registry.json")

    assert result["status"] == "registry_missing"
    assert "manifest_registry_missing" in _reason_codes(result)


def test_invalid_registry_json_returns_stable_reason(tmp_path: Path) -> None:
    registry_path = _write_registry(tmp_path, '{"registry_schema_version": 1,')

    result = _check(registry_path)

    assert result["status"] == "registry_rejected"
    assert "registry_invalid_json" in _reason_codes(result)


@pytest.mark.parametrize("payload", [["not-object"], '"not-object"', None])
def test_registry_root_must_be_object(tmp_path: Path, payload) -> None:
    registry_path = _write_registry(tmp_path, payload)

    result = _check(registry_path)

    assert result["status"] == "registry_rejected"
    assert "registry_not_object" in _reason_codes(result)


def test_unsupported_registry_schema_rejected(tmp_path: Path) -> None:
    registry_path = _known_registry_path(tmp_path, registry_schema_version=999)

    result = _check(registry_path)

    assert result["status"] == "registry_rejected"
    assert "registry_schema_unsupported" in _reason_codes(result)


def test_registry_topic_mismatch_rejected(tmp_path: Path) -> None:
    registry_path = _known_registry_path(tmp_path, topic_id="other-topic")

    result = _check(registry_path)

    assert result["status"] == "registry_rejected"
    assert "registry_topic_mismatch" in _reason_codes(result)


def test_duplicate_registry_paper_id_rejected(tmp_path: Path) -> None:
    entry = _registry_entry(KNOWN_MANIFESTS[0])
    registry_path = _write_registry(tmp_path, _registry([entry, dict(entry)]))

    result = _check(registry_path)

    assert result["status"] == "registry_rejected"
    assert result["duplicate_count"] == 2
    assert "manifest_registry_duplicate_paper" in _reason_codes(result)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"full_text": "embedded paper text"}, "registry_contains_forbidden_content"),
        ({"manifest_entries": [{"api_key": "sk-live-abc1234567890secret"}]}, "registry_contains_secret_like_value"),
    ],
)
def test_registry_content_safety_rejections(tmp_path: Path, payload: dict, expected: str) -> None:
    base = _registry([_registry_entry(KNOWN_MANIFESTS[0])])
    base.update(payload)
    registry_path = _write_registry(tmp_path, base)

    result = _check(registry_path)

    assert result["status"] == "registry_rejected"
    assert expected in _reason_codes(result)


def test_missing_manifest_file_rejected(tmp_path: Path) -> None:
    entry = _registry_entry(KNOWN_MANIFESTS[0], manifest_path="data/evidence_manifests/missing-registry-test.json")
    registry_path = _write_registry(tmp_path, _registry([entry]))

    result = _check(registry_path)

    assert result["status"] == "registry_rejected"
    assert result["missing_manifest_count"] == 1
    assert "registry_manifest_missing" in _reason_codes(result)


def test_manifest_path_outside_project_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    entry = _registry_entry(KNOWN_MANIFESTS[0], manifest_path=str(outside))
    registry_path = _write_registry(tmp_path, _registry([entry]))

    result = _check(registry_path)

    assert result["status"] == "registry_rejected"
    assert "registry_manifest_path_outside_project" in _reason_codes(result)


def test_symlink_manifest_rejected_by_default(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    target = project_root / "target.json"
    target.write_text(json.dumps(_manifest(KNOWN_MANIFESTS[0])), encoding="utf-8")
    link = project_root / "link.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    entry = _registry_entry(KNOWN_MANIFESTS[0], manifest_path="link.json", manifest_sha256=_sha256(target))
    registry_path = _write_registry(tmp_path, _registry([entry]))

    result = _check(registry_path, project_root=project_root)

    assert result["status"] == "registry_rejected"
    assert "registry_manifest_symlink_disallowed" in _reason_codes(result)


def test_manifest_invalid_json_rejected(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    bad = project_root / "bad.json"
    bad.write_text('{"paper_id": ', encoding="utf-8")
    entry = _registry_entry(KNOWN_MANIFESTS[0], manifest_path="bad.json", manifest_sha256=_sha256(bad))
    registry_path = _write_registry(tmp_path, _registry([entry]))

    result = _check(registry_path, project_root=project_root)

    assert result["status"] == "registry_rejected"
    assert "registry_manifest_invalid_json" in _reason_codes(result)


def test_manifest_hash_mismatch_rejected_as_drift(tmp_path: Path) -> None:
    registry_path = _write_registry(
        tmp_path,
        _registry([_registry_entry(KNOWN_MANIFESTS[0], manifest_sha256="0" * 64)]),
    )

    result = _check(registry_path)

    assert result["status"] == "registry_rejected"
    assert result["drift_count"] == 1
    assert result["entries"][0]["status"] == "drifted"
    assert "manifest_hash_mismatch" in _reason_codes(result)


def test_manifest_field_mismatch_rejected(tmp_path: Path) -> None:
    registry_path = _write_registry(
        tmp_path,
        _registry([_registry_entry(KNOWN_MANIFESTS[0], pdf_sha256="f" * 64)]),
    )

    result = _check(registry_path)

    assert result["status"] == "registry_rejected"
    assert result["drift_count"] == 1
    assert "registry_manifest_field_mismatch" in _reason_codes(result)


def test_missing_manifest_sha256_rejected(tmp_path: Path) -> None:
    entry = _registry_entry(KNOWN_MANIFESTS[0])
    entry.pop("manifest_sha256")
    registry_path = _write_registry(tmp_path, _registry([entry]))

    result = _check(registry_path)

    assert result["status"] == "registry_rejected"
    assert "manifest_registry_entry_missing_hash" in _reason_codes(result)


def test_stale_registry_entry_rejected(tmp_path: Path) -> None:
    registry_path = _write_registry(
        tmp_path,
        _registry([_registry_entry(KNOWN_MANIFESTS[0], admitted_status="stale", last_verified_status="missing")]),
    )

    result = _check(registry_path)

    assert result["status"] == "registry_rejected"
    assert "manifest_registry_entry_stale" in _reason_codes(result)


def test_cli_registry_check_json_happy_path(capsys, tmp_path: Path) -> None:
    registry_path = _known_registry_path(tmp_path)

    exit_code = evidence_manifest.main(
        [
            "--registry-check",
            "--registry",
            str(registry_path),
            "--topic",
            "large-language-models",
            "--json",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["status"] == "registry_verified"
    assert output["entry_count"] == 3
    assert output["verified_count"] == 3


def test_cli_registry_check_rejected_exits_nonzero(capsys, tmp_path: Path) -> None:
    registry_path = _write_registry(
        tmp_path,
        _registry([_registry_entry(KNOWN_MANIFESTS[0], manifest_sha256="0" * 64)]),
    )

    exit_code = evidence_manifest.main(
        [
            "--registry-check",
            "--registry",
            str(registry_path),
            "--topic",
            "large-language-models",
            "--json",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert output["status"] == "registry_rejected"
    assert "manifest_hash_mismatch" in _reason_codes(output)


def test_registry_checker_does_not_call_seed_replay_network_or_dashboard() -> None:
    api_text = "\n".join(path.read_text(encoding="utf-8") for path in Path("app/api").glob("*.py"))
    service_text = Path("app/services/evidence_manifest_registry.py").read_text(encoding="utf-8")

    assert "registry-check" not in api_text
    assert "ALLOW_BOUNDED_PDF_FETCH" not in service_text
    assert "replay_manifest(" not in service_text
    assert "seed_manifest_metadata(" not in service_text
    assert "requests." not in service_text
    assert "httpx" not in service_text
    assert "provider" not in service_text
    assert "oauth" not in service_text
