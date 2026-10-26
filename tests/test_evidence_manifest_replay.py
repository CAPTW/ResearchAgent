from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.cli import evidence_manifest
from app.db import bootstrap
from app.repositories import artifact_refs, paper_reviews, runs as run_repository
from app.repositories.files import write_jsonl
from app.services import evidence_manifest_replay, pdf_acquisition, topic_analytics, topic_profiles
from tests.research_transport_fakes import install_pdf_urlopen_mock


class _FakePdfResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.headers = {"Content-Type": "application/pdf", "Content-Length": str(len(body))}

    def __enter__(self) -> "_FakePdfResponse":
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        return self._body


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _long_text(label: str = "bounded replay") -> str:
    return (
        f"Full text evidence for {label} with enough characters to pass deterministic extraction quality checks. "
        "The mocked paper discusses methods, evaluation, limitations, traceability, and evidence spans for a local replay test. "
        "No external AI provider, OAuth connector, or live network dependency is required for this fixture."
    )


def _patch_storage(tmp_path: Path, monkeypatch) -> Path:
    storage_root = tmp_path / "storage" / "runs"
    profile_dir = storage_root.parent / "topic_profiles"
    analytics_dir = storage_root.parent / "topic_analytics"
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(run_repository, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(topic_profiles, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", profile_dir)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", profile_dir / "profiles.json")
    monkeypatch.setattr(topic_analytics, "ANALYTICS_LIBRARY_DIR", analytics_dir)
    monkeypatch.setattr(artifact_refs.constants, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(artifact_refs.constants, "STORAGE_ROOT", storage_root)
    return storage_root


def _create_replay_run(tmp_path: Path, monkeypatch, *, paper_id: str = "paper-1", pdf_url: str = "https://arxiv.org/pdf/paper-1") -> dict:
    _patch_storage(tmp_path, monkeypatch)
    profile = topic_profiles.get_topic_profile("large-language-models")
    run = run_repository.create_run_record(
        topic=profile["topic"],
        request_payload={
            "topic": profile["topic"],
            "queries": profile["queries"],
            "categories": profile["categories"],
            "max_results": 10,
            "top_papers": 3,
            "weekly_top": 10,
        },
        source="test:evidence-replay",
        write_legacy=False,
    )
    paths = run_repository.build_run_paths(run["run_id"])
    write_jsonl(
        paths.triage_dir / "triage.jsonl",
        [
            {
                "paper_id": "oversized-1",
                "title": "Known oversized replay exclusion",
                "abstract": "Large language model benchmark paper that is excluded from this replay.",
                "score": 100,
                "source_provider": "arxiv",
                "published": "2026-05-08",
                "pdf_url": "https://arxiv.org/pdf/oversized-1",
            },
            {
                "paper_id": paper_id,
                "title": "Manifest Replay Target Paper",
                "abstract": "Large language model replay target with benchmark and evidence signals.",
                "score": 90,
                "source_provider": "arxiv",
                "published": "2026-05-07",
                "pdf_url": pdf_url,
            },
        ],
    )
    run["status"] = "completed"
    run["topic_profile_id"] = profile["profile_id"]
    run["steps"] = [{"name": "summary", "status": "completed"}]
    return run_repository.save_run_record(run)


def _manifest_payload(
    *,
    paper_id: str = "paper-1",
    pdf_url: str = "https://arxiv.org/pdf/paper-1",
    pdf_domain: str = "arxiv.org",
    pdf_sha256: str | None = None,
    text: str | None = None,
    source_text_sha256: str | None = None,
    download_limit: int = 1,
    max_pdf_bytes: int = 25000000,
    timeout_seconds: int = 30,
    known_exclusions: list[dict] | None = None,
) -> dict:
    text_value = text if text is not None else _long_text()
    return {
        "schema_version": 1,
        "manifest_kind": "paper_full_text_evidence_replay",
        "topic_id": "large-language-models",
        "paper_id": paper_id,
        "title": "Manifest Replay Target Paper",
        "pdf_url": pdf_url,
        "pdf_domain": pdf_domain,
        "pdf_sha256": pdf_sha256 or _sha256_bytes(b"%PDF-1.4 replay fixture\n%%EOF"),
        "source_text_sha256": source_text_sha256 or _sha256_text(text_value),
        "artifact_ref": f"run:raw/{paper_id}.txt",
        "pages_artifact_ref": f"run:raw/{paper_id}.pages.json",
        "pdf_artifact_ref": f"run:raw/{paper_id}.pdf",
        "extraction": {
            "page_count": 1,
            "extracted_chars": len(text_value),
            "extractor": "mock",
        },
        "review": {
            "expected_status": "reviewed",
            "evidence_span_count": 1,
            "evidence_span_metadata": [{"index": 1, "label": "full_text_excerpt_1", "char_start": 0, "char_end": 120}],
        },
        "acquisition": {
            "command_provenance": "python -m app.cli.review_refresh --topic large-language-models --fetch-missing-pdf",
            "max_pdf_bytes": max_pdf_bytes,
            "download_limit": download_limit,
            "allow_domain": "arxiv.org",
            "timeout_seconds": timeout_seconds,
            "operator_gate_required": True,
        },
        "known_exclusions": known_exclusions if known_exclusions is not None else [{"paper_id": "oversized-1", "reason": "file_too_large"}],
        "coverage": {
            "before_full_text_reviewed_unique_count": 27,
            "after_full_text_reviewed_unique_count": 28,
            "topic_relevant_unique_count": 524,
        },
        "portability": {
            "do_not_commit_pdf": True,
            "do_not_commit_full_text": True,
            "replay_requires_bounded_network": True,
        },
        "created_from_commit": "test",
        "notes": ["test replay manifest"],
    }


def _write_manifest(tmp_path: Path, payload: dict | None = None, *, name: str = "manifest.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload or _manifest_payload(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _mock_successful_pdf(monkeypatch, *, body: bytes, text: str) -> None:
    def fake_urlopen(_request, timeout=30):
        return _FakePdfResponse(body)

    def fake_extract(_path, max_text_chars=24000):
        return [{"page_number": 1, "text": text, "extraction_method": "mock"}]

    install_pdf_urlopen_mock(monkeypatch, fake_urlopen)
    monkeypatch.setattr(pdf_acquisition, "_extract_pdf_pages", fake_extract)


def test_replay_bounded_missing_local_state_dry_run_plans_without_network(tmp_path: Path, monkeypatch) -> None:
    _create_replay_run(tmp_path, monkeypatch)
    path = _write_manifest(tmp_path)

    result = evidence_manifest_replay.replay_manifest(path, dry_run=True, env={})

    assert result["status"] == "replay_dry_run_planned"
    assert result["network_attempted"] is False
    assert result["write_attempted"] is False
    assert result["bounded_parameters"]["paper_id"] == "paper-1"
    assert result["bounded_parameters"]["download_limit"] == 1
    assert "replay_verified" in result["supported_statuses"]
    assert "replay_blocked_env_gate" in result["supported_statuses"]
    assert result["safe_to_claim_reviewed"] is False


def test_replay_bounded_missing_local_state_blocks_without_env(tmp_path: Path, monkeypatch) -> None:
    _create_replay_run(tmp_path, monkeypatch)
    path = _write_manifest(tmp_path)

    result = evidence_manifest_replay.replay_manifest(path, env={})

    assert result["status"] == "replay_blocked_env_gate"
    assert result["network_attempted"] is False
    assert result["write_attempted"] is False


def test_replay_bounded_invalid_manifest_returns_manifest_invalid(tmp_path: Path, monkeypatch) -> None:
    _patch_storage(tmp_path, monkeypatch)
    path = _write_manifest(tmp_path, {"schema_version": 1, "manifest_kind": "wrong"})

    result = evidence_manifest_replay.replay_manifest(path, dry_run=True, env={})

    assert result["status"] == "manifest_invalid"
    assert result["safe_to_claim_reviewed"] is False


def test_replay_rejects_domain_and_target_mismatch(tmp_path: Path, monkeypatch) -> None:
    _create_replay_run(tmp_path, monkeypatch)
    domain_path = _write_manifest(
        tmp_path,
        _manifest_payload(pdf_url="https://example.com/pdf/paper-1", pdf_domain="example.com"),
        name="domain-manifest.json",
    )
    target_path = _write_manifest(tmp_path, _manifest_payload(paper_id="oversized-1"), name="target-manifest.json")

    domain_result = evidence_manifest_replay.replay_manifest(domain_path, dry_run=True, env={})
    target_result = evidence_manifest_replay.replay_manifest(target_path, dry_run=True, env={})

    assert domain_result["status"] == "manifest_invalid"
    assert "domain_not_allowlisted" in domain_result["reasons"]
    assert target_result["status"] == "manifest_invalid"
    assert "target_is_known_excluded" in target_result["reasons"]


def test_replay_rejects_looser_bounds(tmp_path: Path, monkeypatch) -> None:
    _create_replay_run(tmp_path, monkeypatch)
    path = _write_manifest(tmp_path)

    max_result = evidence_manifest_replay.replay_manifest(path, dry_run=True, env={}, max_pdf_bytes=25000001)
    timeout_result = evidence_manifest_replay.replay_manifest(path, dry_run=True, env={}, timeout_seconds=31)
    download_result = evidence_manifest_replay.replay_manifest(path, dry_run=True, env={}, download_limit=2)

    assert max_result["status"] == "manifest_invalid"
    assert "max_pdf_bytes_looser_than_manifest" in max_result["reasons"]
    assert timeout_result["status"] == "manifest_invalid"
    assert "download_timeout_looser_than_manifest" in timeout_result["reasons"]
    assert download_result["status"] == "manifest_invalid"
    assert "download_limit_must_be_1" in download_result["reasons"]


def test_replay_local_already_verified_does_not_fetch(tmp_path: Path, monkeypatch) -> None:
    from tests.test_evidence_manifest_verifier import _seed_review

    _seed_review(tmp_path, monkeypatch)
    path = _write_manifest(tmp_path, _manifest_payload(paper_id="paper-1", pdf_sha256=None, text="Full text evidence for manifest verifier."))

    def fail_refresh(*_args, **_kwargs):
        raise AssertionError("refresh should not run for already verified evidence")

    result = evidence_manifest_replay.replay_manifest(path, env={"ALLOW_BOUNDED_PDF_FETCH": "1"}, refresh_func=fail_refresh)

    assert result["status"] == "local_already_verified"
    assert result["safe_to_claim_reviewed"] is True
    assert result["network_attempted"] is False
    assert result["write_attempted"] is False


def test_mocked_temp_state_replay_reaches_verified(tmp_path: Path, monkeypatch) -> None:
    body = b"%PDF-1.4 replay fixture\n%%EOF"
    text = _long_text("successful replay")
    _create_replay_run(tmp_path, monkeypatch)
    _mock_successful_pdf(monkeypatch, body=body, text=text)
    path = _write_manifest(tmp_path, _manifest_payload(pdf_sha256=_sha256_bytes(body), text=text))

    result = evidence_manifest_replay.replay_manifest(path, env={"ALLOW_BOUNDED_PDF_FETCH": "1"})
    stored = paper_reviews.list_reviews(topic_id="large-language-models", limit=10)

    assert result["status"] == "replay_verified"
    assert result["local_verified_after"] is True
    assert result["safe_to_claim_reviewed"] is True
    assert result["network_attempted"] is True
    assert result["write_attempted"] is True
    assert any(row["paper_id"] == "paper-1" and row["full_text_status"] == "reviewed" for row in stored)


def test_mocked_pdf_hash_mismatch_fails_without_reviewing(tmp_path: Path, monkeypatch) -> None:
    body = b"%PDF-1.4 replay fixture\n%%EOF"
    text = _long_text("pdf hash mismatch")
    _create_replay_run(tmp_path, monkeypatch)
    _mock_successful_pdf(monkeypatch, body=body, text=text)
    path = _write_manifest(tmp_path, _manifest_payload(pdf_sha256="1" * 64, text=text))

    result = evidence_manifest_replay.replay_manifest(path, env={"ALLOW_BOUNDED_PDF_FETCH": "1"})
    stored = paper_reviews.list_reviews(topic_id="large-language-models", limit=10)

    assert result["status"] == "replay_pdf_hash_mismatch"
    assert result["safe_to_claim_reviewed"] is False
    assert not any(row["paper_id"] == "paper-1" and row["full_text_status"] == "reviewed" for row in stored)


def test_mocked_text_hash_mismatch_fails_without_reviewing(tmp_path: Path, monkeypatch) -> None:
    body = b"%PDF-1.4 replay fixture\n%%EOF"
    text = _long_text("text hash mismatch")
    _create_replay_run(tmp_path, monkeypatch)
    _mock_successful_pdf(monkeypatch, body=body, text=text)
    path = _write_manifest(tmp_path, _manifest_payload(pdf_sha256=_sha256_bytes(body), source_text_sha256="2" * 64))

    result = evidence_manifest_replay.replay_manifest(path, env={"ALLOW_BOUNDED_PDF_FETCH": "1"})
    stored = paper_reviews.list_reviews(topic_id="large-language-models", limit=10)

    assert result["status"] in {"replay_review_validation_failed", "replay_extraction_failed"}
    assert "source_text_sha256_mismatch" in result["reasons"]
    assert result["safe_to_claim_reviewed"] is False
    assert not any(row["paper_id"] == "paper-1" and row["full_text_status"] == "reviewed" for row in stored)


def test_mocked_extraction_failure_does_not_mark_reviewed(tmp_path: Path, monkeypatch) -> None:
    body = b"%PDF-1.4 replay fixture\n%%EOF"
    _create_replay_run(tmp_path, monkeypatch)

    def fake_urlopen(_request, timeout=30):
        return _FakePdfResponse(body)

    def fail_extract(_path, max_text_chars=24000):
        raise RuntimeError("mock extraction failed")

    install_pdf_urlopen_mock(monkeypatch, fake_urlopen)
    monkeypatch.setattr(pdf_acquisition, "_extract_pdf_pages", fail_extract)
    path = _write_manifest(tmp_path, _manifest_payload(pdf_sha256=_sha256_bytes(body), text=_long_text("extract fail")))

    result = evidence_manifest_replay.replay_manifest(path, env={"ALLOW_BOUNDED_PDF_FETCH": "1"})
    stored = paper_reviews.list_reviews(topic_id="large-language-models", limit=10)

    assert result["status"] == "replay_extraction_failed"
    assert result["safe_to_claim_reviewed"] is False
    assert not any(row["paper_id"] == "paper-1" and row["full_text_status"] == "reviewed" for row in stored)


def test_cli_replay_bounded_json_is_stable_and_plan_replay_stays_print_only(tmp_path: Path, monkeypatch, capsys) -> None:
    _create_replay_run(tmp_path, monkeypatch)
    path = _write_manifest(tmp_path)

    replay_exit = evidence_manifest.main(["--manifest", str(path), "--replay-bounded", "--dry-run", "--json"])
    replay_output = json.loads(capsys.readouterr().out)
    plan_exit = evidence_manifest.main(["--manifest", str(path), "--plan-replay"])
    plan_output = capsys.readouterr().out

    assert replay_exit == 0
    assert replay_output["status"] == "replay_dry_run_planned"
    assert replay_output["network_attempted"] is False
    assert plan_exit == 0
    assert "status: plan_only" in plan_output
    assert "does not run network" in plan_output


def test_cli_env_var_alone_does_not_start_replay(tmp_path: Path, monkeypatch, capsys) -> None:
    _create_replay_run(tmp_path, monkeypatch)
    path = _write_manifest(tmp_path)
    monkeypatch.setenv("ALLOW_BOUNDED_PDF_FETCH", "1")

    def fail_urlopen(*_args, **_kwargs):
        raise AssertionError("network must not run without --replay-bounded")

    install_pdf_urlopen_mock(monkeypatch, fail_urlopen)

    exit_code = evidence_manifest.main(["--manifest", str(path), "--json"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert output["status"] == "missing_review_row"
    assert output["replay_required"] is True
