from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.repositories.runs import build_run_paths
from app.services import prototype_provider


def _wait_for_terminal_run(client: TestClient, run_id: str, *, timeout_seconds: float = 20.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = client.get(f"/runs/{run_id}")
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload["status"] in {"completed", "failed"}:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"run did not reach a terminal state: {run_id}")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_prototype_vertical_slice_is_grounded_portable_and_non_overwriting(tmp_path: Path, app_factory) -> None:
    data_root = (tmp_path / "prototype-data").resolve()
    with TestClient(
        app_factory(
            AUTH_MODE="disabled",
            XRML_PROTOTYPE_MODE="1",
            XRML_DATA_DIR=str(data_root),
            XRML_RUN_EXECUTOR_POLL_SECONDS="0.05",
        )
    ) as client:
        status_response = client.get("/prototype/status")
        assert status_response.status_code == 200, status_response.text
        assert status_response.json()["isolation_status"] == "isolated"

        start_response = client.post("/prototype/runs")
        assert start_response.status_code == 200, start_response.text
        parent_id = start_response.json()["run_id"]
        parent = _wait_for_terminal_run(client, parent_id)
        assert parent["status"] == "completed", parent
        assert parent["prototype"]["pipeline_status"] == "completed"
        assert parent["prototype"]["report_status"] == "ready"
        assert parent["counts"]["reviewed_items"] == 4
        assert parent["counts"]["evidence_spans"] >= 8

        # Public API contracts must expose only portable run references, never
        # the disposable machine-local data root.
        assert data_root.name.lower() not in json.dumps(parent).lower()
        assert "run:reports/" in json.dumps(parent)

        papers_response = client.get(f"/runs/{parent_id}/papers")
        assert papers_response.status_code == 200, papers_response.text
        papers = papers_response.json()["items"]
        assert len(papers) == 4
        assert data_root.name.lower() not in json.dumps(papers).lower()
        for paper in papers:
            review_response = client.get(f"/runs/{parent_id}/papers/{paper['paper_id']}/review")
            assert review_response.status_code == 200, review_response.text
            review = review_response.json()
            assert review["status"] == "complete"
            assert len(review["evidence"]) >= 2
            assert all(item["source_type"] == "full_text" for item in review["evidence"])
            assert data_root.name.lower() not in json.dumps(review).lower()

        report_response = client.get(f"/runs/{parent_id}/report")
        assert report_response.status_code == 200, report_response.text
        report = report_response.json()
        assert "Provider execution posture" in report["content"]
        assert "deterministic local fallback" in report["content"]
        assert "Live Provider invoked: no" in report["content"]
        assert data_root.name.lower() not in json.dumps(report).lower()

        preview_response = client.get(f"/runs/{parent_id}/preview")
        assert preview_response.status_code == 200, preview_response.text
        assert preview_response.json()["content"].strip()
        assert data_root.name.lower() not in json.dumps(preview_response.json()).lower()

        export_response = client.post(f"/runs/{parent_id}/export", json={"format": "report_markdown"})
        assert export_response.status_code == 200, export_response.text
        assert data_root.name.lower() not in json.dumps(export_response.json()).lower()
        assert data_root.name.lower() not in (
            build_run_paths(parent_id).reports_dir / "report_versions.json"
        ).read_text(encoding="utf-8").lower()

        parent_paths = build_run_paths(parent_id)
        parent_report = parent_paths.reports_dir / "prototype-weekly-journal.md"
        provider_record = parent_paths.reports_dir / "prototype-provider-executions.json"
        assert parent_report.is_file()
        parent_digest_before = _sha256(parent_report)
        provider_bundle = json.loads(provider_record.read_text(encoding="utf-8"))
        assert provider_bundle["control_total"] == 5
        assert provider_bundle["control_passed"] == 5
        assert provider_bundle["accepted_control_passed"] is True
        assert provider_bundle["rejection_controls_passed"] is True
        assert provider_bundle["trusted_provider_output_persisted"] is False
        assert provider_bundle["live_provider_invoked"] is False
        assert [item["accepted"] for item in provider_bundle["controls"]] == [True, False, False, False, False]

        rerun_response = client.post(
            f"/runs/{parent_id}/rerun",
            json={
                "mode": "branch_from_selected_papers",
                "selected_paper_ids": [paper["paper_id"] for paper in papers[:2]],
                "skip_retrieval_when_branching": True,
                "selection_source": "manual",
                "note": "Prototype immutable child rerun",
            },
        )
        assert rerun_response.status_code == 200, rerun_response.text
        child_id = rerun_response.json()["child_run_id"]
        child = _wait_for_terminal_run(client, child_id)
        assert child["status"] == "completed", child
        assert child["lineage"]["parent_run_id"] == parent_id
        assert child["lineage"]["root_run_id"] == parent_id
        assert child["prototype"]["report_status"] == "ready"
        assert child["counts"]["reviewed_items"] == 2
        assert child["counts"]["evidence_spans"] >= 4
        assert (build_run_paths(child_id).reports_dir / "prototype-weekly-journal.md").is_file()
        assert _sha256(parent_report) == parent_digest_before
        assert build_run_paths(child_id).run_dir != parent_paths.run_dir

        reopen_response = client.get("/prototype/status")
        assert reopen_response.status_code == 200, reopen_response.text
        assert reopen_response.json()["latest_run"]["run_id"] == child_id
        assert reopen_response.json()["latest_run"]["provider"]["route_status"] == "deterministic_local_fallback"


def test_prototype_mode_and_provider_activation_fail_closed(tmp_path: Path, app_factory, monkeypatch) -> None:
    data_root = (tmp_path / "prototype-disabled-data").resolve()
    with TestClient(
        app_factory(
            AUTH_MODE="disabled",
            XRML_PROTOTYPE_MODE="0",
            XRML_DATA_DIR=str(data_root),
        )
    ) as client:
        status = client.get("/prototype/status")
        assert status.status_code == 200
        assert status.json()["can_start"] is False
        blocked = client.post("/prototype/runs")
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["code"] == "prototype_mode_unavailable"

    for key in (
        "XRML_PROTOTYPE_PROVIDER_ENABLED",
        "XRML_PROTOTYPE_PROVIDER_ID",
        "XRML_PROTOTYPE_PROVIDER_SURFACE",
        "XRML_PROTOTYPE_PROVIDER_STAGE",
        "XRML_PROTOTYPE_PROVIDER_OUTPUT_CLASS",
        "XRML_PROTOTYPE_PROVIDER_ACCEPTANCE_POLICY",
    ):
        monkeypatch.delenv(key, raising=False)
    disabled = prototype_provider.evaluate_development_activation(proof_eligible=True)
    assert disabled["eligible"] is False
    assert disabled["live_invocation_authorized"] is False

    monkeypatch.setenv("XRML_PROTOTYPE_MODE", "1")
    monkeypatch.setenv("XRML_PROTOTYPE_PROVIDER_ENABLED", "1")
    monkeypatch.setenv("XRML_PROTOTYPE_PROVIDER_ID", prototype_provider.PROVIDER_ID)
    monkeypatch.setenv("XRML_PROTOTYPE_PROVIDER_SURFACE", prototype_provider.SURFACE_ID)
    monkeypatch.setenv("XRML_PROTOTYPE_PROVIDER_STAGE", prototype_provider.STAGE)
    monkeypatch.setenv("XRML_PROTOTYPE_PROVIDER_OUTPUT_CLASS", prototype_provider.OUTPUT_CLASS)
    monkeypatch.setenv("XRML_PROTOTYPE_PROVIDER_ACCEPTANCE_POLICY", prototype_provider.ACCEPTANCE_POLICY)
    exact = prototype_provider.evaluate_development_activation(proof_eligible=True)
    assert exact["eligible"] is True
    assert exact["activation_state"] == "eligible"
    assert exact["live_invocation_authorized"] is False


def test_pipeline_failure_exposes_only_stable_error_codes(tmp_path: Path, app_factory, monkeypatch) -> None:
    from app.services import prototype

    data_root = (tmp_path / "prototype-failure-data").resolve()

    def fail_with_private_detail(_run_id: str) -> dict[str, Any]:
        raise RuntimeError(r"private failure at C:\owner\secret\artifact.json")

    monkeypatch.setattr(prototype, "finalize_prototype_outputs", fail_with_private_detail)
    with TestClient(
        app_factory(
            AUTH_MODE="disabled",
            XRML_PROTOTYPE_MODE="1",
            XRML_DATA_DIR=str(data_root),
            XRML_RUN_EXECUTOR_POLL_SECONDS="0.05",
        )
    ) as client:
        started = client.post("/prototype/runs")
        assert started.status_code == 200, started.text
        run_id = started.json()["run_id"]
        failed = _wait_for_terminal_run(client, run_id)
        assert failed["status"] == "failed"
        assert failed["error"] == "pipeline_execution_failed"
        public_text = json.dumps(failed).lower()
        assert "private failure" not in public_text
        assert r"c:\owner\secret" not in public_text
        events_text = build_run_paths(run_id).events_path.read_text(encoding="utf-8").lower()
        assert "pipeline_execution_failed" in events_text
        assert "private failure" not in events_text
        assert r"c:\owner\secret" not in events_text
