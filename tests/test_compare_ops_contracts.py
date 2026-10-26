from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import COMPARE_RUN_ID, PRIMARY_RUN_ID


def test_compare_contract_exposes_research_ops_metrics_and_run_aliases(client: TestClient) -> None:
    response = client.get("/compare", params={"runA": PRIMARY_RUN_ID, "runB": COMPARE_RUN_ID})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["left"]["run_id"] == PRIMARY_RUN_ID
    assert payload["right"]["run_id"] == COMPARE_RUN_ID
    assert payload["summary"]["common_papers"] >= 1
    assert {"added", "removed", "common"} <= set(payload["papers"])
    assert payload["score_distribution"]
    assert {item["band"] for item in payload["score_distribution"]} == {"high", "medium", "low", "unscored"}
    assert payload["keyword_movement"], "fixture runs should expose keyword movement from triage keyword_hits"
    assert payload["provider_contribution"], "fixture runs should expose provider contribution from source metadata"
    assert payload["report_status"]["left_status"] in {"completed", "missing"}
    assert payload["report_status"]["right_status"] in {"completed", "missing"}
    assert payload["provenance"]["source_run_ids"] == [PRIMARY_RUN_ID, COMPARE_RUN_ID]
    assert any("score distribution" in definition for definition in payload["provenance"]["definitions"])


def test_compare_report_contract_accepts_run_aliases(client: TestClient) -> None:
    response = client.get("/compare/report", params={"runA": PRIMARY_RUN_ID, "runB": COMPARE_RUN_ID})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["left_run_id"] == PRIMARY_RUN_ID
    assert payload["right_run_id"] == COMPARE_RUN_ID
    assert "report_summary_diff" in payload


def test_selective_rerun_rejects_missing_selected_papers_before_child_creation(client: TestClient) -> None:
    runs_before_response = client.get("/runs")
    assert runs_before_response.status_code == 200
    runs_before = {run["run_id"] for run in runs_before_response.json()}

    response = client.post(
        f"/runs/{PRIMARY_RUN_ID}/rerun",
        json={
            "mode": "branch_from_selected_papers",
            "selected_paper_ids": ["missing-paper-id"],
            "skip_retrieval_when_branching": True,
            "selection_source": "manual",
        },
    )

    assert response.status_code == 400
    assert "Selected papers were not found" in response.json()["detail"]
    runs_after_response = client.get("/runs")
    assert runs_after_response.status_code == 200
    runs_after = {run["run_id"] for run in runs_after_response.json()}
    assert runs_after == runs_before
