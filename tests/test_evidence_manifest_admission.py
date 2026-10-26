from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.cli import evidence_manifest
from app.services import evidence_manifest_admission


KNOWN_MANIFESTS = [
    "data/evidence_manifests/m19.3-large-language-models-2604.26498v1.json",
    "data/evidence_manifests/m21-large-language-models-2605.01143v1.json",
    "data/evidence_manifests/m23-large-language-models-2604.14475v1.json",
]


def _manifest(**overrides) -> dict:
    payload = {
        "schema_version": 1,
        "manifest_kind": "paper_full_text_evidence_replay",
        "topic_id": "large-language-models",
        "paper_id": "paper-1",
        "title": "Admission Test Paper",
        "pdf_url": "https://arxiv.org/pdf/paper-1",
        "pdf_domain": "arxiv.org",
        "pdf_sha256": "a" * 64,
        "source_text_sha256": "b" * 64,
        "artifact_ref": "run:raw/paper-1.txt",
        "pages_artifact_ref": "run:raw/paper-1.pages.json",
        "pdf_artifact_ref": "run:raw/paper-1.pdf",
        "extraction": {
            "page_count": 1,
            "extracted_chars": 1200,
            "extractor": "test",
        },
        "review": {
            "expected_status": "reviewed",
            "evidence_span_count": 1,
            "evidence_span_metadata": [{"index": 1, "label": "full_text_excerpt_1"}],
        },
        "acquisition": {
            "command_provenance": "python -m app.cli.review_refresh --topic large-language-models --fetch-missing-pdf",
            "max_pdf_bytes": 25000000,
            "download_limit": 1,
            "allow_domain": "arxiv.org",
            "timeout_seconds": 30,
            "operator_gate_required": True,
        },
        "known_exclusions": [{"paper_id": "2605.03205v1", "reason": "file_too_large"}],
        "coverage": {
            "before_full_text_reviewed_unique_count": 30,
            "after_full_text_reviewed_unique_count": 31,
            "topic_relevant_unique_count": 524,
        },
        "portability": {
            "do_not_commit_pdf": True,
            "do_not_commit_full_text": True,
            "do_not_commit_pages_json": True,
            "replay_requires_bounded_network": True,
        },
    }
    payload.update(overrides)
    return payload


def _write_manifest(tmp_path: Path, payload: dict | None = None, *, name: str = "manifest.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload or _manifest(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _check(paths: list[str | Path], **kwargs) -> dict:
    return evidence_manifest_admission.check_batch_admission(
        paths,
        topic_id="large-language-models",
        require_tracked=False,
        require_local_verified=False,
        **kwargs,
    )


def _reason_codes(result: dict) -> set[str]:
    return {reason["code"] for reason in result.get("rejections", [])}


def _warning_codes(result: dict) -> set[str]:
    warnings = []
    for item in result.get("manifests", []):
        warnings.extend(item.get("warnings", []))
    return {warning["code"] for warning in warnings}


def test_accepts_current_three_known_manifests() -> None:
    result = evidence_manifest_admission.check_batch_admission(
        KNOWN_MANIFESTS,
        topic_id="large-language-models",
    )

    assert result["status"] == "admitted"
    assert result["batch_size"] == 3
    assert result["admitted_count"] == 3
    assert result["rejected_count"] == 0
    assert result["safe_to_run_dry_run_batch"] is True
    assert result["safe_to_run_live_batch"] is False
    assert result["safe_to_claim_arbitrary_manifest_automation"] is False
    assert result["read_only"] is True
    assert result["network_attempted"] is False
    assert result["write_attempted"] is False
    assert [item["paper_id"] for item in result["manifests"]] == ["2604.26498v1", "2605.01143v1", "2604.14475v1"]


def test_cli_batch_policy_check_json_is_parseable_and_stable(capsys) -> None:
    exit_code = evidence_manifest.main(
        [
            "--batch-policy-check",
            "--topic",
            "large-language-models",
            *sum((["--manifest", manifest] for manifest in KNOWN_MANIFESTS), []),
            "--json",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["status"] == "admitted"
    assert output["batch_size"] == 3
    assert output["admitted_count"] == 3
    assert output["rejected_count"] == 0
    assert output["safe_to_run_dry_run_batch"] is True
    assert output["safe_to_run_live_batch"] is False
    assert output["safe_to_claim_arbitrary_manifest_automation"] is False
    assert output["read_only"] is True
    assert output["network_attempted"] is False
    assert output["write_attempted"] is False


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"schema_version": 999}, "schema_version_unsupported"),
        ({"manifest_kind": "wrong"}, "manifest_kind_mismatch"),
        ({"topic_id": "other-topic"}, "topic_mismatch"),
        ({"paper_id": ""}, "paper_id_missing"),
        (
            {
                "pdf_url": "https://example.com/pdf/paper-1",
                "pdf_domain": "example.com",
                "acquisition": {
                    "command_provenance": "python -m app.cli.review_refresh --fetch-missing-pdf",
                    "max_pdf_bytes": 25000000,
                    "download_limit": 1,
                    "allow_domain": "example.com",
                    "timeout_seconds": 30,
                    "operator_gate_required": True,
                },
            },
            "non_arxiv_domain",
        ),
        ({"pdf_url": "http://arxiv.org/pdf/paper-1"}, "non_https_pdf_url"),
        ({"pdf_sha256": ""}, "missing_pdf_sha256"),
        ({"source_text_sha256": ""}, "missing_source_text_sha256"),
        ({"review": {"expected_status": "reviewed", "evidence_span_count": 0}}, "missing_evidence_spans"),
        ({"artifact_ref": ""}, "missing_artifact_ref"),
        ({"artifact_ref": "../paper-1.txt"}, "unsafe_artifact_ref"),
        ({"full_text": "embedded full text"}, "contains_full_text"),
        ({"pages_content": [{"page": 1, "text": "page text"}]}, "contains_pages_content"),
        ({"pdf_bytes": "JVBERi0xLjQ="}, "contains_pdf_bytes"),
        ({"api_key": "sk-test-secret-like"}, "contains_secret_like_value"),
        (
            {
                "acquisition": {
                    "command_provenance": "python -m app.cli.review_refresh --fetch-missing-pdf",
                    "max_pdf_bytes": 25000001,
                    "download_limit": 1,
                    "allow_domain": "arxiv.org",
                    "timeout_seconds": 30,
                    "operator_gate_required": True,
                }
            },
            "max_pdf_bytes_too_large",
        ),
        (
            {
                "acquisition": {
                    "command_provenance": "python -m app.cli.review_refresh --fetch-missing-pdf",
                    "max_pdf_bytes": 25000000,
                    "download_limit": 2,
                    "allow_domain": "arxiv.org",
                    "timeout_seconds": 30,
                    "operator_gate_required": True,
                }
            },
            "download_limit_not_one",
        ),
        (
            {
                "acquisition": {
                    "command_provenance": "python -m app.cli.review_refresh --fetch-missing-pdf",
                    "max_pdf_bytes": 25000000,
                    "download_limit": 1,
                    "allow_domain": "arxiv.org",
                    "timeout_seconds": 30,
                    "operator_gate_required": False,
                }
            },
            "operator_gate_missing",
        ),
        ({"acquisition": {}}, "missing_bounded_provenance"),
    ],
)
def test_rejects_invalid_manifest_fields(tmp_path: Path, payload: dict, expected: str) -> None:
    path = _write_manifest(tmp_path, _manifest(**payload))

    result = _check([path])

    assert result["status"] == "rejected"
    assert expected in _reason_codes(result)
    assert result["safe_to_run_dry_run_batch"] is False


def test_missing_manifest_returns_manifest_missing(tmp_path: Path) -> None:
    result = _check([tmp_path / "missing.json"])

    assert result["status"] == "rejected"
    assert "manifest_missing" in _reason_codes(result)


def test_malformed_json_returns_stable_reason(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text('{"schema_version": 1,', encoding="utf-8")

    result = _check([path])

    assert result["status"] == "rejected"
    assert "manifest_invalid_json" in _reason_codes(result)


@pytest.mark.parametrize("payload", ['["not", "object"]', '"not-object"', "null"])
def test_json_root_not_object_returns_stable_reason(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "not-object.json"
    path.write_text(payload, encoding="utf-8")

    result = _check([path])

    assert result["status"] == "rejected"
    assert "manifest_not_object" in _reason_codes(result)


def test_path_outside_project_returns_stable_reason(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    outside = tmp_path / "outside"
    project_root.mkdir()
    outside.mkdir()
    path = _write_manifest(outside)

    result = evidence_manifest_admission.check_batch_admission(
        [path],
        topic_id="large-language-models",
        require_tracked=True,
        require_local_verified=False,
        project_root=project_root,
    )

    assert result["status"] == "rejected"
    assert "manifest_path_outside_project" in _reason_codes(result)


def test_symlink_manifest_rejected_by_default(tmp_path: Path) -> None:
    target = _write_manifest(tmp_path, name="target.json")
    link = tmp_path / "manifest-link.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    result = _check([link])

    assert result["status"] == "rejected"
    assert "manifest_symlink_disallowed" in _reason_codes(result)


def test_oversized_manifest_file_returns_stable_reason(tmp_path: Path) -> None:
    path = tmp_path / "oversized.json"
    path.write_text(" " * (evidence_manifest_admission.MAX_MANIFEST_BYTES + 1), encoding="utf-8")

    result = _check([path])

    assert result["status"] == "rejected"
    assert "manifest_file_too_large" in _reason_codes(result)


def test_unknown_top_level_field_reports_non_blocking_warning(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, _manifest(operator_note="safe metadata"))

    result = _check([path])

    assert result["status"] == "admitted"
    assert result["warning_count"] == 1
    assert result["safe_to_run_dry_run_batch"] is True
    assert "unknown_field" in _warning_codes(result)


def test_secret_like_value_returns_contains_secret_like_value(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, _manifest(notes=["temporary token sk-live-abc1234567890secret"]))

    result = _check([path])

    assert result["status"] == "rejected"
    assert "contains_secret_like_value" in _reason_codes(result)


def test_duplicate_paper_id_rejects_batch(tmp_path: Path) -> None:
    first = _write_manifest(tmp_path, _manifest(paper_id="same-paper"), name="a.json")
    second = _write_manifest(tmp_path, _manifest(paper_id="same-paper"), name="b.json")

    result = _check([first, second])

    assert result["status"] == "rejected"
    assert "duplicate_paper_in_batch" in _reason_codes(result)


def test_untracked_manifest_rejected_when_tracking_required(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path)

    result = evidence_manifest_admission.check_batch_admission(
        [path],
        topic_id="large-language-models",
        require_tracked=True,
        require_local_verified=False,
    )

    assert result["status"] == "rejected"
    assert "manifest_untracked_or_unapproved" in _reason_codes(result)


def test_local_verifier_failure_maps_to_local_verification_failed(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path)

    def fake_verify(_path, **_kwargs):
        return {"status": "missing_review_row", "local_status": None, "safe_to_claim_local_verified": False, "reasons": ["review_row_not_found"]}

    result = evidence_manifest_admission.check_batch_admission(
        [path],
        topic_id="large-language-models",
        require_tracked=False,
        require_local_verified=True,
        verify_func=fake_verify,
    )

    assert result["status"] == "rejected"
    assert "local_verification_failed" in _reason_codes(result)


def test_plan_replay_not_print_only_rejects(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path)

    def fake_plan(_path):
        return {"status": "replay_dry_run_planned", "runs_network": True, "writes_storage_or_db": True}

    result = evidence_manifest_admission.check_batch_admission(
        [path],
        topic_id="large-language-models",
        require_tracked=False,
        require_local_verified=False,
        plan_func=fake_plan,
    )

    assert result["status"] == "rejected"
    assert "plan_replay_not_print_only" in _reason_codes(result)


def test_admission_report_contract_includes_stable_fields(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, _manifest(schema_version=999))

    result = _check([path])

    assert {
        "status",
        "topic_id",
        "batch_size",
        "admitted_count",
        "rejected_count",
        "warning_count",
        "safe_to_run_dry_run_batch",
        "safe_to_run_live_batch",
        "safe_to_claim_arbitrary_manifest_automation",
        "manifests",
        "rejections",
        "reasons",
        "read_only",
        "network_attempted",
        "write_attempted",
    } <= set(result)
    manifest = result["manifests"][0]
    assert {
        "index",
        "manifest_path",
        "status",
        "paper_id",
        "topic_id",
        "schema_version",
        "manifest_kind",
        "tracked",
        "reasons",
        "warnings",
        "local_verification",
        "plan_replay",
    } <= set(manifest)
    reason = result["rejections"][0]
    assert {"code", "message", "blocks_batch", "operator_fixable", "safe_next_action"} <= set(reason)


def test_checker_does_not_expose_dashboard_route_or_call_replay_paths() -> None:
    api_text = "\n".join(path.read_text(encoding="utf-8") for path in Path("app/api").glob("*.py"))
    service_text = Path("app/services/evidence_manifest_admission.py").read_text(encoding="utf-8")

    assert "batch-policy-check" not in api_text
    assert "ALLOW_BOUNDED_PDF_FETCH" not in service_text
    assert "replay_manifest(" not in service_text
    assert "seed_manifest_metadata(" not in service_text
    assert "requests." not in service_text
    assert "httpx" not in service_text
    assert "provider" not in service_text
    assert "oauth" not in service_text
