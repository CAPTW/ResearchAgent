from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.cli import evidence_manifest
from app.services import evidence_manifest_registry_proposal, evidence_manifest_registry_write


KNOWN_MANIFESTS = [
    "data/evidence_manifests/m19.3-large-language-models-2604.26498v1.json",
    "data/evidence_manifests/m21-large-language-models-2605.01143v1.json",
    "data/evidence_manifests/m23-large-language-models-2604.14475v1.json",
]
TARGET_REGISTRY = "data/evidence_manifests/registry.json"


def _reason_codes(result: dict) -> set[str]:
    return {reason["code"] for reason in result.get("reasons", [])}


def _write_json(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _proposal_path(tmp_path: Path, *, proposal_id: str = "registry-write-proposal") -> Path:
    proposal_path = tmp_path / f"{proposal_id}.json"
    result = evidence_manifest_registry_proposal.write_registry_proposal(
        KNOWN_MANIFESTS,
        topic_id="large-language-models",
        output_path=proposal_path,
        proposal_id=proposal_id,
    )
    assert result["status"] == "proposal_written"
    assert result["registry_check_status"] == "registry_verified"
    return proposal_path


def _ready(proposal_path: Path, **overrides) -> dict:
    kwargs = {
        "proposal_path": proposal_path,
        "registry_path": TARGET_REGISTRY,
        "topic_id": "large-language-models",
        "dry_run": True,
        "operator_approved": True,
        "env_gate_value": "1",
        "confirm_registry_path": TARGET_REGISTRY,
    }
    kwargs.update(overrides)
    return evidence_manifest_registry_write.validate_registry_write_dry_run(**kwargs)


def test_dry_run_with_valid_proposal_gates_and_confirm_is_ready_without_writing(tmp_path: Path) -> None:
    proposal_path = _proposal_path(tmp_path)

    result = _ready(proposal_path)

    assert result["status"] == "registry_write_dry_run_ready"
    assert result["would_write"] is True
    assert result["dry_run"] is True
    assert result["registry_written"] is False
    assert result["write_attempted"] is False
    assert result["db_storage_write_attempted"] is False
    assert result["network_attempted"] is False
    assert result["live_replay_executed"] is False
    assert result["provider_oauth_external_ai_used"] is False
    assert result["dashboard_trigger_used"] is False
    assert result["operator_flag_present"] is True
    assert result["env_gate_present"] is True
    assert result["confirm_path_matched"] is True
    assert result["registry_check_status"] == "registry_verified"
    assert result["safe_to_write_tracked_registry"] is False
    assert result["safe_to_claim_tracked_registry_exists"] is False
    assert result["safe_to_claim_registry_lifecycle_complete"] is False
    assert result["safe_to_claim_clean_clone_reproducible"] is False
    assert result["reasons"] == []
    assert not Path(TARGET_REGISTRY).exists()


def test_missing_operator_and_env_gates_block_readiness(tmp_path: Path) -> None:
    result = _ready(
        _proposal_path(tmp_path),
        operator_approved=False,
        env_gate_value=None,
    )

    assert result["status"] == "registry_write_blocked"
    assert result["would_write"] is False
    assert result["registry_written"] is False
    assert "registry_write_operator_flag_required" in _reason_codes(result)
    assert "registry_write_env_gate_required" in _reason_codes(result)


@pytest.mark.parametrize(
    ("operator_approved", "env_gate_value", "expected_reason"),
    [
        (False, "1", "registry_write_operator_flag_required"),
        (True, None, "registry_write_env_gate_required"),
    ],
)
def test_flag_and_env_gate_are_each_required(
    tmp_path: Path,
    operator_approved: bool,
    env_gate_value: str | None,
    expected_reason: str,
) -> None:
    result = _ready(
        _proposal_path(tmp_path),
        operator_approved=operator_approved,
        env_gate_value=env_gate_value,
    )

    assert result["status"] == "registry_write_blocked"
    assert result["would_write"] is False
    assert expected_reason in _reason_codes(result)


@pytest.mark.parametrize("confirm_path", [None, "data/evidence_manifests/not-registry.json"])
def test_confirm_path_must_match_registry_path(tmp_path: Path, confirm_path: str | None) -> None:
    result = _ready(_proposal_path(tmp_path), confirm_registry_path=confirm_path)

    assert result["status"] == "registry_write_blocked"
    assert result["confirm_path_matched"] is False
    assert "registry_write_confirmation_mismatch" in _reason_codes(result)


def test_missing_proposal_is_rejected(tmp_path: Path) -> None:
    result = _ready(tmp_path / "missing-proposal.json")

    assert result["status"] == "registry_write_proposal_invalid"
    assert "registry_write_proposal_missing" in _reason_codes(result)


def test_invalid_json_proposal_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"proposal_schema_version": 1,', encoding="utf-8")

    result = _ready(bad)

    assert result["status"] == "registry_write_proposal_invalid"
    assert "registry_write_proposal_invalid_json" in _reason_codes(result)


def test_non_object_proposal_is_rejected(tmp_path: Path) -> None:
    result = _ready(_write_json(tmp_path / "list.json", ["not-object"]))

    assert result["status"] == "registry_write_proposal_invalid"
    assert "registry_write_proposal_not_object" in _reason_codes(result)


@pytest.mark.parametrize(
    ("field", "value", "expected_reason"),
    [
        ("proposal_kind", "wrong-kind", "registry_write_proposal_kind_mismatch"),
        ("proposal_status", "proposal_rejected", "registry_write_proposal_status_invalid"),
    ],
)
def test_proposal_identity_fields_are_validated(
    tmp_path: Path,
    field: str,
    value: str,
    expected_reason: str,
) -> None:
    proposal_path = _proposal_path(tmp_path)
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    proposal[field] = value
    _write_json(proposal_path, proposal)

    result = _ready(proposal_path)

    assert result["status"] == "registry_write_proposal_invalid"
    assert expected_reason in _reason_codes(result)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda proposal: proposal.pop("proposed_registry"),
        lambda proposal: proposal["registry_check_report"].update({"status": "registry_rejected"}),
    ],
)
def test_missing_or_unverified_proposal_registry_report_is_rejected(tmp_path: Path, mutation) -> None:
    proposal_path = _proposal_path(tmp_path)
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    mutation(proposal)
    _write_json(proposal_path, proposal)

    result = _ready(proposal_path)

    assert result["status"] == "registry_write_proposal_invalid"
    assert "registry_write_proposal_not_verified" in _reason_codes(result)


def test_invalid_safety_flags_are_rejected(tmp_path: Path) -> None:
    proposal_path = _proposal_path(tmp_path)
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    proposal["safety_flags"]["tracked_registry_written"] = True
    _write_json(proposal_path, proposal)

    result = _ready(proposal_path)

    assert result["status"] == "registry_write_proposal_invalid"
    assert "registry_write_proposal_safety_flag_invalid" in _reason_codes(result)


def test_immediate_proposed_registry_check_failure_blocks_write(tmp_path: Path) -> None:
    proposal_path = _proposal_path(tmp_path)
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    proposal["proposed_registry"]["manifest_entries"][0]["manifest_sha256"] = "0" * 64
    _write_json(proposal_path, proposal)

    result = _ready(proposal_path)

    assert result["status"] == "registry_write_registry_check_failed"
    assert result["registry_check_status"] == "registry_rejected"
    assert result["would_write"] is False
    assert "registry_write_post_check_failed" in _reason_codes(result)
    assert result["registry_written"] is False
    assert not Path(TARGET_REGISTRY).exists()


@pytest.mark.parametrize(
    ("registry_path", "expected_reason"),
    [
        ("data/evidence_manifests/other.json", "registry_write_target_path_mismatch"),
        ("data/../data/evidence_manifests/registry.json", "registry_write_forbidden_output_path"),
    ],
)
def test_registry_path_must_be_exact_target(tmp_path: Path, registry_path: str, expected_reason: str) -> None:
    result = _ready(_proposal_path(tmp_path), registry_path=registry_path)

    assert result["status"] == "registry_write_blocked"
    assert expected_reason in _reason_codes(result)


def test_existing_registry_file_blocks_dry_run_readiness(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    registry_path = project_root / TARGET_REGISTRY
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text("{}", encoding="utf-8")

    result = _ready(_proposal_path(tmp_path), project_root=project_root)

    assert result["status"] == "registry_write_blocked"
    assert "registry_write_existing_registry_requires_overwrite_flag" in _reason_codes(result)
    assert registry_path.exists()


def test_apply_mode_is_not_implemented_and_writes_nothing(tmp_path: Path) -> None:
    result = _ready(_proposal_path(tmp_path), dry_run=False)

    assert result["status"] == "registry_write_apply_not_implemented"
    assert result["would_write"] is False
    assert result["registry_written"] is False
    assert result["write_attempted"] is False
    assert "registry_write_apply_not_implemented" in _reason_codes(result)
    assert not Path(TARGET_REGISTRY).exists()


def test_cli_happy_path_outputs_parseable_ready_json(tmp_path: Path, capsys, monkeypatch) -> None:
    proposal_path = _proposal_path(tmp_path, proposal_id="cli-ready")
    monkeypatch.setenv("ALLOW_TRACKED_REGISTRY_WRITE", "1")

    exit_code = evidence_manifest.main(
        [
            "--registry-write",
            "--dry-run",
            "--proposal",
            str(proposal_path),
            "--registry",
            TARGET_REGISTRY,
            "--operator-approve-tracked-registry-write",
            "--confirm-registry-path",
            TARGET_REGISTRY,
            "--json",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["status"] == "registry_write_dry_run_ready"
    assert output["would_write"] is True
    assert output["registry_written"] is False
    assert output["write_attempted"] is False
    assert output["registry_check_status"] == "registry_verified"
    assert not Path(TARGET_REGISTRY).exists()


def test_cli_missing_gate_exits_nonzero(tmp_path: Path, capsys, monkeypatch) -> None:
    proposal_path = _proposal_path(tmp_path, proposal_id="cli-missing-gates")
    monkeypatch.delenv("ALLOW_TRACKED_REGISTRY_WRITE", raising=False)

    exit_code = evidence_manifest.main(
        [
            "--registry-write",
            "--dry-run",
            "--proposal",
            str(proposal_path),
            "--registry",
            TARGET_REGISTRY,
            "--confirm-registry-path",
            TARGET_REGISTRY,
            "--json",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert output["status"] == "registry_write_blocked"
    assert "registry_write_operator_flag_required" in _reason_codes(output)
    assert "registry_write_env_gate_required" in _reason_codes(output)


def test_cli_bad_proposal_exits_nonzero(tmp_path: Path, capsys, monkeypatch) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("not-json", encoding="utf-8")
    monkeypatch.setenv("ALLOW_TRACKED_REGISTRY_WRITE", "1")

    exit_code = evidence_manifest.main(
        [
            "--registry-write",
            "--dry-run",
            "--proposal",
            str(bad),
            "--registry",
            TARGET_REGISTRY,
            "--operator-approve-tracked-registry-write",
            "--confirm-registry-path",
            TARGET_REGISTRY,
            "--json",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert output["status"] == "registry_write_proposal_invalid"
    assert "registry_write_proposal_invalid_json" in _reason_codes(output)


def test_cli_without_dry_run_rejects_apply_mode(tmp_path: Path, capsys, monkeypatch) -> None:
    proposal_path = _proposal_path(tmp_path, proposal_id="cli-apply-blocked")
    monkeypatch.setenv("ALLOW_TRACKED_REGISTRY_WRITE", "1")

    exit_code = evidence_manifest.main(
        [
            "--registry-write",
            "--proposal",
            str(proposal_path),
            "--registry",
            TARGET_REGISTRY,
            "--operator-approve-tracked-registry-write",
            "--confirm-registry-path",
            TARGET_REGISTRY,
            "--json",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert output["status"] == "registry_write_apply_not_implemented"
    assert "registry_write_apply_not_implemented" in _reason_codes(output)
    assert output["registry_written"] is False
    assert not Path(TARGET_REGISTRY).exists()


def test_registry_write_dry_run_adds_no_api_or_live_provider_paths() -> None:
    service_text = Path("app/services/evidence_manifest_registry_write.py").read_text(encoding="utf-8")
    api_text = "\n".join(path.read_text(encoding="utf-8") for path in Path("app/api").glob("*.py"))

    assert "registry-write" not in api_text
    assert "ALLOW_BOUNDED_PDF_FETCH" not in service_text
    assert "replay_manifest(" not in service_text
    assert "seed_manifest_metadata(" not in service_text
    assert "requests." not in service_text
    assert "httpx" not in service_text
    assert "openai" not in service_text.lower()
    assert "oauth_token_exchange" not in service_text
    assert "perform_real_provider_smoke" not in service_text
