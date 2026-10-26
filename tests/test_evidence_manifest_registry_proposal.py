from __future__ import annotations

import json
import shutil
from importlib import import_module
from pathlib import Path

import pytest

from app.cli import evidence_manifest


KNOWN_MANIFESTS = [
    "data/evidence_manifests/m19.3-large-language-models-2604.26498v1.json",
    "data/evidence_manifests/m21-large-language-models-2605.01143v1.json",
    "data/evidence_manifests/m23-large-language-models-2604.14475v1.json",
]


def _reason_codes(result: dict) -> set[str]:
    return {reason["code"] for reason in result.get("reasons", [])}


def _load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _forbidden_payload_hits(value) -> list[str]:
    hits: list[str] = []
    forbidden_keys = {"full_text", "full_text_content", "extracted_text", "pdf_bytes", "pdf_content", "raw_pdf", "pages_content", "page_text", "page_texts", "pages_json"}
    secret_keys = {"secret", "token", "api_key", "apikey", "password", "private_key", "credential"}
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in forbidden_keys or lowered in secret_keys or any(secret in lowered for secret in secret_keys):
                hits.append(str(key))
            hits.extend(_forbidden_payload_hits(item))
    elif isinstance(value, list):
        for item in value:
            hits.extend(_forbidden_payload_hits(item))
    elif isinstance(value, str):
        for marker in ("sk-live-", "github_pat_", "AKIA", "-----BEGIN PRIVATE KEY-----"):
            if marker in value:
                hits.append(marker)
    return hits


def _proposal_service():
    return import_module("app.services.evidence_manifest_registry_proposal")


def test_temp_output_writes_review_only_proposal_and_verifies_registry() -> None:
    result = _proposal_service().write_registry_proposal(
        KNOWN_MANIFESTS,
        topic_id="large-language-models",
        temp_output=True,
        proposal_id="proposal-temp-test",
    )
    proposal_path = Path(result["proposal_path"])
    try:
        assert result["status"] == "proposal_written"
        assert result["proposal_status"] == "proposal_generated"
        assert result["output_path_type"] == "temp"
        assert result["proposal_written"] is True
        assert result["tracked_registry_written"] is False
        assert result["target_registry_write_allowed"] is False
        assert result["registry_check_status"] == "registry_verified"
        assert result["network_attempted"] is False
        assert result["write_attempted"] is True
        assert result["db_storage_write_attempted"] is False
        assert result["live_replay_executed"] is False
        assert result["safe_to_claim_registry_lifecycle_complete"] is False
        assert result["safe_to_claim_tracked_registry_exists"] is False
        assert result["safe_to_claim_clean_clone_reproducible"] is False
        assert result["safe_to_claim_arbitrary_manifest_automation"] is False
        assert proposal_path.exists()
        assert "data/evidence_manifests" not in proposal_path.as_posix()

        proposal = _load_json(proposal_path)
        assert proposal["proposal_schema_version"] == 1
        assert proposal["proposal_id"] == "proposal-temp-test"
        assert proposal["proposal_kind"] == "evidence_manifest_registry_proposal"
        assert proposal["proposal_status"] == "proposal_generated"
        assert proposal["target_registry_path"] == "data/evidence_manifests/registry.json"
        assert proposal["target_registry_write_allowed"] is False
        assert proposal["requires_operator_review"] is True
        assert proposal["proposed_registry"]["registry_schema_version"] == 1
        assert proposal["proposed_registry"]["topic_id"] == "large-language-models"
        assert len(proposal["proposed_registry"]["manifest_entries"]) == 3
        assert proposal["admission_report"]["status"] == "admitted"
        assert proposal["generator_report"]["status"] == "registry_generate_dry_run_planned"
        assert proposal["registry_check_report"]["status"] == "registry_verified"
        assert proposal["safety_flags"] == {
            "proposal_only": True,
            "tracked_registry_written": False,
            "live_replay_executed": False,
            "provider_oauth_external_ai_used": False,
            "dashboard_trigger_used": False,
            "contains_runtime_artifacts": False,
        }
        assert _forbidden_payload_hits(proposal) == []
    finally:
        shutil.rmtree(proposal_path.parent, ignore_errors=True)


def test_explicit_allowed_path_writes_proposal(tmp_path: Path) -> None:
    output_path = tmp_path / "review" / "proposal.json"

    result = _proposal_service().write_registry_proposal(
        KNOWN_MANIFESTS,
        topic_id="large-language-models",
        output_path=output_path,
    )

    assert result["status"] == "proposal_written"
    assert result["output_path_type"] == "explicit_non_registry"
    assert Path(result["proposal_path"]) == output_path
    assert output_path.exists()
    assert _load_json(output_path)["registry_check_report"]["status"] == "registry_verified"


def test_missing_output_mode_is_rejected() -> None:
    result = _proposal_service().write_registry_proposal(
        KNOWN_MANIFESTS,
        topic_id="large-language-models",
    )

    assert result["status"] == "proposal_rejected"
    assert result["proposal_written"] is False
    assert "proposal_output_path_required" in _reason_codes(result)


@pytest.mark.parametrize(
    ("output_path", "expected_reason"),
    [
        ("data/evidence_manifests/registry.json", "proposal_output_path_inside_registry_location"),
        ("data/evidence_manifests/proposal.json", "proposal_output_path_inside_registry_location"),
        ("storage/proposal.json", "proposal_output_path_forbidden"),
        ("output/proposal.json", "proposal_output_path_forbidden"),
        (".git/proposal.json", "proposal_output_path_forbidden"),
        ("tmp/../proposal.json", "proposal_output_path_traversal"),
    ],
)
def test_forbidden_output_paths_are_rejected(output_path: str, expected_reason: str) -> None:
    result = _proposal_service().write_registry_proposal(
        [KNOWN_MANIFESTS[2]],
        topic_id="large-language-models",
        output_path=output_path,
    )

    assert result["status"] == "proposal_rejected"
    assert result["proposal_written"] is False
    assert expected_reason in _reason_codes(result)
    assert not Path("data/evidence_manifests/registry.json").exists()


def test_symlink_output_path_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    link = tmp_path / "link.json"
    target.write_text("{}", encoding="utf-8")
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    result = _proposal_service().write_registry_proposal(
        [KNOWN_MANIFESTS[2]],
        topic_id="large-language-models",
        output_path=link,
    )

    assert result["status"] == "proposal_rejected"
    assert "proposal_output_path_symlink_disallowed" in _reason_codes(result)


def test_existing_output_requires_overwrite_flag(tmp_path: Path) -> None:
    output_path = tmp_path / "proposal.json"
    output_path.write_text("existing", encoding="utf-8")

    rejected = _proposal_service().write_registry_proposal(
        [KNOWN_MANIFESTS[2]],
        topic_id="large-language-models",
        output_path=output_path,
    )
    allowed = _proposal_service().write_registry_proposal(
        [KNOWN_MANIFESTS[2]],
        topic_id="large-language-models",
        output_path=output_path,
        allow_output_overwrite=True,
    )

    assert rejected["status"] == "proposal_rejected"
    assert "proposal_output_already_exists" in _reason_codes(rejected)
    assert allowed["status"] == "proposal_written"
    assert _load_json(output_path)["proposal_kind"] == "evidence_manifest_registry_proposal"


def test_duplicate_manifest_stops_before_proposal_write() -> None:
    result = _proposal_service().write_registry_proposal(
        [KNOWN_MANIFESTS[2], KNOWN_MANIFESTS[2]],
        topic_id="large-language-models",
        temp_output=True,
    )

    assert result["status"] == "proposal_generation_failed"
    assert result["proposal_written"] is False
    assert "proposal_generation_admission_failed" in _reason_codes(result)
    assert result["generator_report"]["status"] == "registry_generation_admission_failed"
    assert "duplicate_paper_in_batch" in result["generator_report"]["admission_report"]["reasons"]
    assert not Path("data/evidence_manifests/registry.json").exists()


def test_registry_check_failure_rejects_proposal(monkeypatch, tmp_path: Path) -> None:
    proposal_service = _proposal_service()

    def fake_registry_check(*args, **kwargs):
        return {"status": "registry_rejected", "reasons": [{"code": "manifest_hash_mismatch"}]}

    monkeypatch.setattr(proposal_service, "_check_proposed_registry", fake_registry_check)
    result = proposal_service.write_registry_proposal(
        [KNOWN_MANIFESTS[2]],
        topic_id="large-language-models",
        output_path=tmp_path / "proposal.json",
    )

    assert result["status"] == "proposal_rejected"
    assert result["proposal_written"] is False
    assert result["registry_check_status"] == "registry_rejected"
    assert "proposal_registry_check_failed" in _reason_codes(result)
    assert not (tmp_path / "proposal.json").exists()


def test_cli_temp_output_writes_parseable_json_and_proposal_file(capsys) -> None:
    exit_code = evidence_manifest.main(
        [
            "--registry-write-proposal",
            "--temp-output",
            "--topic",
            "large-language-models",
            *sum((["--manifest", manifest] for manifest in KNOWN_MANIFESTS), []),
            "--json",
        ]
    )
    output = json.loads(capsys.readouterr().out)
    proposal_path = Path(output["proposal_path"])
    try:
        assert exit_code == 0
        assert output["status"] == "proposal_written"
        assert output["output_path_type"] == "temp"
        assert output["registry_check_status"] == "registry_verified"
        assert proposal_path.exists()
    finally:
        shutil.rmtree(proposal_path.parent, ignore_errors=True)


def test_cli_forbidden_registry_output_exits_nonzero(capsys) -> None:
    exit_code = evidence_manifest.main(
        [
            "--registry-write-proposal",
            "--output",
            "data/evidence_manifests/registry.json",
            "--topic",
            "large-language-models",
            "--manifest",
            KNOWN_MANIFESTS[2],
            "--json",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert output["status"] == "proposal_rejected"
    assert "proposal_output_path_inside_registry_location" in _reason_codes(output)
    assert not Path("data/evidence_manifests/registry.json").exists()


def test_cli_duplicate_manifest_exits_nonzero(capsys) -> None:
    exit_code = evidence_manifest.main(
        [
            "--registry-write-proposal",
            "--temp-output",
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
    assert output["status"] == "proposal_generation_failed"
    assert output["proposal_written"] is False
    assert "proposal_generation_admission_failed" in _reason_codes(output)


def test_proposal_writer_does_not_add_live_or_dashboard_paths() -> None:
    service_text = Path("app/services/evidence_manifest_registry_proposal.py").read_text(encoding="utf-8")
    api_text = "\n".join(path.read_text(encoding="utf-8") for path in Path("app/api").glob("*.py"))

    assert not Path("data/evidence_manifests/registry.json").exists()
    assert "registry-write-proposal" not in api_text
    assert "requests." not in service_text
    assert "httpx" not in service_text
    assert "replay_manifest(" not in service_text
    assert "seed_manifest_metadata(" not in service_text
    assert "ALLOW_BOUNDED_PDF_FETCH" not in service_text
