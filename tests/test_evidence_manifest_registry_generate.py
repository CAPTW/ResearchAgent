from __future__ import annotations

import json
from pathlib import Path

from app.cli import evidence_manifest
from app.services import evidence_manifest_registry, evidence_manifest_registry_generate


KNOWN_MANIFESTS = [
    "data/evidence_manifests/m19.3-large-language-models-2604.26498v1.json",
    "data/evidence_manifests/m21-large-language-models-2605.01143v1.json",
    "data/evidence_manifests/m23-large-language-models-2604.14475v1.json",
]


def _reason_codes(result: dict) -> set[str]:
    return {reason["code"] for reason in result.get("reasons", [])}


def _write_json(tmp_path: Path, payload: dict, *, name: str = "proposed-registry.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def test_current_three_manifests_generate_registry_dry_run_and_verify_with_checker(tmp_path: Path) -> None:
    result = evidence_manifest_registry_generate.generate_registry_dry_run(
        KNOWN_MANIFESTS,
        topic_id="large-language-models",
        generated_by_phase="M26_P1_REGISTRY_GENERATOR_DRY_RUN",
        registry_id="large-language-models-m26-p1",
    )

    assert result["status"] == "registry_generate_dry_run_planned"
    assert result["manifest_count"] == 3
    assert result["generated_entry_count"] == 3
    assert result["registry_written"] is False
    assert result["write_attempted"] is False
    assert result["network_attempted"] is False
    assert result["safe_to_write_tracked_registry"] is False
    assert result["safe_to_claim_registry_lifecycle_complete"] is False
    assert result["safe_to_claim_clean_clone_reproducible"] is False
    assert result["safe_to_claim_arbitrary_manifest_automation"] is False

    registry = result["proposed_registry"]
    assert registry["registry_schema_version"] == 1
    assert registry["registry_id"] == "large-language-models-m26-p1"
    assert registry["topic_id"] == "large-language-models"
    assert registry["generated_by_phase"] == "M26_P1_REGISTRY_GENERATOR_DRY_RUN"
    assert registry["source_manifest_count"] == 3
    assert registry["policy_version"] == "m26.p1"
    assert registry["hashing_policy"] == {"manifest_hash": "raw_sha256", "canonical_json": False}
    assert len(registry["manifest_entries"]) == 3
    assert [entry["paper_id"] for entry in registry["manifest_entries"]] == [
        "2604.26498v1",
        "2605.01143v1",
        "2604.14475v1",
    ]

    for entry, manifest_path in zip(registry["manifest_entries"], KNOWN_MANIFESTS):
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        assert entry["manifest_path"] == Path(manifest_path).as_posix()
        assert len(entry["manifest_sha256"]) == 64
        assert entry["manifest_kind"] == manifest["manifest_kind"]
        assert entry["manifest_schema_version"] == manifest["schema_version"]
        assert entry["title"] == manifest["title"]
        assert entry["pdf_url"] == manifest["pdf_url"]
        assert entry["pdf_sha256"] == manifest["pdf_sha256"]
        assert entry["source_text_sha256"] == manifest["source_text_sha256"]
        assert entry["artifact_ref"] == manifest["artifact_ref"]
        assert entry["evidence_span_count"] == 1
        assert entry["admitted_status"] == "admitted"
        assert entry["last_verified_status"] == "verified"
        assert entry["portability_flags"]["do_not_commit_pdf"] is True
        assert entry["portability_flags"]["do_not_commit_full_text"] is True
        assert entry["portability_flags"]["replay_requires_bounded_network"] is True
        assert entry["lifecycle_status"] == "registry_proposed"
        assert entry["created_by_phase"] == "M26_P1_REGISTRY_GENERATOR_DRY_RUN"
        assert entry["updated_by_phase"] is None

    assert registry["aggregate_hashes"]["manifest_count"] == 3
    assert len(registry["aggregate_hashes"]["entries_sha256"]) == 64

    registry_path = _write_json(tmp_path, registry)
    check = evidence_manifest_registry.check_manifest_registry(registry_path, topic_id="large-language-models")
    assert check["status"] == "registry_verified"
    assert check["entry_count"] == 3
    assert check["verified_count"] == 3
    assert check["drift_count"] == 0


def test_empty_manifest_set_returns_stable_failure() -> None:
    result = evidence_manifest_registry_generate.generate_registry_dry_run(
        [],
        topic_id="large-language-models",
    )

    assert result["status"] == "failed"
    assert result["manifest_count"] == 0
    assert result["generated_entry_count"] == 0
    assert result["proposed_registry"] is None
    assert result["registry_written"] is False
    assert "registry_generation_empty_manifest_set" in _reason_codes(result)


def test_admission_rejection_stops_generation_before_entries() -> None:
    result = evidence_manifest_registry_generate.generate_registry_dry_run(
        [KNOWN_MANIFESTS[2], KNOWN_MANIFESTS[2]],
        topic_id="large-language-models",
    )

    assert result["status"] == "registry_generation_admission_failed"
    assert result["manifest_count"] == 2
    assert result["generated_entry_count"] == 0
    assert result["proposed_registry"] is None
    assert result["registry_written"] is False
    assert "registry_generation_admission_failed" in _reason_codes(result)
    assert "duplicate_paper_in_batch" in result["admission_report"]["reasons"]


def test_missing_manifest_returns_admission_failure(tmp_path: Path) -> None:
    result = evidence_manifest_registry_generate.generate_registry_dry_run(
        [tmp_path / "missing.json"],
        topic_id="large-language-models",
        allow_untracked_manifest=True,
        require_local_verified=False,
    )

    assert result["status"] == "registry_generation_admission_failed"
    assert result["generated_entry_count"] == 0
    assert "manifest_missing" in result["admission_report"]["reasons"]


def test_invalid_manifest_parse_maps_to_admission_failure(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema_version": 1,', encoding="utf-8")

    result = evidence_manifest_registry_generate.generate_registry_dry_run(
        [bad],
        topic_id="large-language-models",
        allow_untracked_manifest=True,
        require_local_verified=False,
    )

    assert result["status"] == "registry_generation_admission_failed"
    assert result["generated_entry_count"] == 0
    assert "manifest_invalid_json" in result["admission_report"]["reasons"]


def test_cli_registry_generate_dry_run_json_happy_path(capsys) -> None:
    exit_code = evidence_manifest.main(
        [
            "--registry-generate-dry-run",
            "--topic",
            "large-language-models",
            *sum((["--manifest", manifest] for manifest in KNOWN_MANIFESTS), []),
            "--json",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["status"] == "registry_generate_dry_run_planned"
    assert output["manifest_count"] == 3
    assert output["generated_entry_count"] == 3
    assert output["proposed_registry"]["registry_schema_version"] == 1
    assert output["registry_written"] is False
    assert output["write_attempted"] is False
    assert output["network_attempted"] is False


def test_cli_registry_generate_duplicate_exits_nonzero(capsys) -> None:
    exit_code = evidence_manifest.main(
        [
            "--registry-generate-dry-run",
            "--topic",
            "large-language-models",
            "--manifest",
            KNOWN_MANIFESTS[2],
            "--manifest",
            KNOWN_MANIFESTS[2],
            "--json",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert output["status"] == "registry_generation_admission_failed"
    assert output["generated_entry_count"] == 0
    assert output["registry_written"] is False
    assert "duplicate_paper_in_batch" in output["admission_report"]["reasons"]


def test_generator_does_not_write_tracked_registry_or_call_live_paths() -> None:
    service_text = Path("app/services/evidence_manifest_registry_generate.py").read_text(encoding="utf-8")
    cli_text = Path("app/cli/evidence_manifest.py").read_text(encoding="utf-8")

    assert not Path("data/evidence_manifests/registry.json").exists()
    assert "openai" not in service_text.lower()
    assert "oauth" not in service_text.lower()
    assert "requests." not in service_text
    assert "httpx" not in service_text
    assert "replay_manifest(" not in service_text
    assert "seed_manifest_metadata(" not in service_text
    assert "ALLOW_BOUNDED_PDF_FETCH" not in service_text
    assert 'parser.add_argument("--registry-write",' in cli_text
    assert "registry_write_apply_not_implemented" in Path("app/services/evidence_manifest_registry_write.py").read_text(encoding="utf-8")
    assert "--registry-update" not in cli_text
    assert "--allow-overwrite-existing-registry" not in cli_text
