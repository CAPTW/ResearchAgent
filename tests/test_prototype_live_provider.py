from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Mapping

from fastapi.testclient import TestClient

from app.repositories import paper_reviews
from app.repositories.runs import build_run_paths
from app.services import prototype_live_provider
from app.services.codex_cli_report_draft import CliExecResult


ROUTE = {
    "provider_id": "codex",
    "execution_surface": "codex_sdk_python",
    "stage": "report_draft_assist",
    "internal_output_class": "report_draft",
    "product_output_class": "weekly_report",
}


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


def _start_prototype_run(client: TestClient) -> tuple[str, dict[str, Any]]:
    response = client.post("/prototype/runs")
    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]
    run = _wait_for_terminal_run(client, run_id)
    assert run["status"] == "completed", run
    return run_id, run


def _evidence_ids(run_id: str, topic_id: str) -> list[str]:
    reviews = paper_reviews.list_reviews(topic_id=topic_id, run_id=run_id, limit=8, read_only=True)
    return [
        f"{review['paper_id']}-evidence-{index}"
        for review in reviews
        for index, _span in enumerate(review.get("evidence_spans") or [], start=1)
    ]


def _valid_candidate(run_id: str, evidence_ids: list[str]) -> dict[str, Any]:
    first, second = evidence_ids[:2]
    return {
        "schema_version": "prototype_provider_report_draft_v1",
        "run_id": run_id,
        **ROUTE,
        "title": "Provider-assisted synthetic evidence report",
        "executive_summary": "The supplied synthetic evidence supports bounded local Prototype findings.",
        "reviewed_literature_overview": [
            {
                "text": "The synthetic literature links latency and provenance findings.",
                "statement_type": "grounded",
                "evidence_ids": [first],
                "strength": "high",
            }
        ],
        "thematic_findings": [
            {
                "text": "A broader deployment effect remains an inference.",
                "statement_type": "inference",
                "evidence_ids": [],
                "strength": "low",
            }
        ],
        "grounded_claims": [
            {
                "claim_text": "The local fixture records a bounded latency improvement.",
                "evidence_ids": [first],
                "strength": "high",
                "claim_type": "grounded",
            },
            {
                "claim_text": "The local fixture preserves provenance for a spatial claim.",
                "evidence_ids": [second],
                "strength": "high",
                "claim_type": "grounded",
            },
        ],
        "limitations_and_uncertainties": [
            {
                "text": "The generated corpus cannot establish field generalization.",
                "statement_type": "uncertainty",
                "evidence_ids": [],
                "strength": "high",
            }
        ],
        "next_research_questions": [
            {
                "text": "Would the bounded findings hold for a small open-access corpus?",
                "statement_type": "proposal",
                "evidence_ids": [],
                "strength": "medium",
            }
        ],
        "referenced_evidence_ids": [first, second],
        "inference_or_proposal_sections": [],
    }


async def _accepted_runner(prompt: str, output_schema: Mapping[str, Any], _cwd: str) -> dict[str, Any]:
    request = json.loads(prompt)
    assert request["synthetic_corpus"] is True
    assert request["provider_id"] == "codex"
    assert output_schema["properties"]["run_id"]
    evidence_ids = [item["evidence_id"] for item in request["evidence"]]
    return {
        "raw_output": json.dumps(_valid_candidate(request["run_id"], evidence_ids), sort_keys=True),
        "duration_ms": 25,
        "model_backend_identifier": "recorded-test-backend",
    }


def _expect_code(call, code: str) -> bool:
    try:
        call()
    except prototype_live_provider.PrototypeLiveProviderError as exc:
        return exc.code == code
    return False


def _stub_known_available_dependency(monkeypatch: Any) -> None:
    monkeypatch.setattr(prototype_live_provider, "_dependency_available", lambda: True)


def test_required_offline_activation_controls_01_through_11(tmp_path: Path, app_factory, monkeypatch) -> None:
    data_root = (tmp_path / "prototype-live-activation").resolve()
    _stub_known_available_dependency(monkeypatch)
    prototype_live_provider.reset_ephemeral_activation_state_for_tests()
    with TestClient(
        app_factory(
            AUTH_MODE="disabled",
            XRML_PROTOTYPE_MODE="1",
            XRML_PROTOTYPE_LIVE_PROVIDER_PROOF="0",
            XRML_PROTOTYPE_SESSION_ID="offline-activation-session",
            XRML_DATA_DIR=str(data_root),
            XRML_RUN_EXECUTOR_POLL_SECONDS="0.05",
        ),
        client=("127.0.0.1", 50000),
    ) as client:
        run_id, run = _start_prototype_run(client)
        controls: dict[str, bool] = {}
        status = client.get(f"/prototype/runs/{run_id}/provider-proof").json()
        controls["01_default_unavailable"] = status["endpoint_available"] is False

        monkeypatch.setenv("XRML_PROTOTYPE_LIVE_PROVIDER_PROOF", "1")
        controls["02_flag_alone_no_authority"] = _expect_code(
            lambda: asyncio.run(
                prototype_live_provider.generate_live_candidate(
                    run_id=run_id,
                    route=ROUTE,
                    activation_token="missing-activation-token-value",
                    confirmed=True,
                    client_host="127.0.0.1",
                    sdk_runner=_accepted_runner,
                )
            ),
            "prototype_activation_token_invalid",
        )
        monkeypatch.setenv("CODEX_API_KEY", "presence-only-not-a-real-secret")
        controls["03_auth_presence_no_authority"] = _expect_code(
            lambda: asyncio.run(
                prototype_live_provider.generate_live_candidate(
                    run_id=run_id,
                    route=ROUTE,
                    activation_token="still-missing-activation-token",
                    confirmed=True,
                    client_host="127.0.0.1",
                    sdk_runner=_accepted_runner,
                )
            ),
            "prototype_activation_token_invalid",
        )
        monkeypatch.delenv("CODEX_API_KEY", raising=False)
        controls["04_wrong_run_id"] = _expect_code(
            lambda: prototype_live_provider.issue_activation_token(
                run_id="not-a-prototype-run",
                route=ROUTE,
                client_host="127.0.0.1",
            ),
            "prototype_run_not_found",
        )
        for number, field, value, code in (
            ("05", "provider_id", "other", "prototype_live_route_mismatch"),
            ("06", "execution_surface", "other_surface", "prototype_live_route_mismatch"),
            ("07", "stage", "triage_assist", "prototype_live_route_mismatch"),
            ("08", "internal_output_class", "research_output", "prototype_live_route_mismatch"),
        ):
            wrong = {**ROUTE, field: value}
            controls[f"{number}_{field}"] = _expect_code(
                lambda wrong=wrong: prototype_live_provider.issue_activation_token(
                    run_id=run_id,
                    route=wrong,
                    client_host="127.0.0.1",
                ),
                code,
            )

        issued_at = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
        expired = prototype_live_provider.issue_activation_token(
            run_id=run_id,
            route=ROUTE,
            client_host="127.0.0.1",
            now=issued_at,
        )
        controls["09_expired_token"] = _expect_code(
            lambda: asyncio.run(
                prototype_live_provider.generate_live_candidate(
                    run_id=run_id,
                    route=ROUTE,
                    activation_token=expired["activation_token"],
                    confirmed=True,
                    client_host="127.0.0.1",
                    sdk_runner=_accepted_runner,
                    now=issued_at + timedelta(minutes=4),
                )
            ),
            "prototype_activation_token_expired",
        )

        prototype_live_provider.reset_ephemeral_activation_state_for_tests()
        monkeypatch.setenv("XRML_PROTOTYPE_SESSION_ID", "offline-replay-session")
        replay = prototype_live_provider.issue_activation_token(
            run_id=run_id,
            route=ROUTE,
            client_host="127.0.0.1",
        )
        pending = asyncio.run(
            prototype_live_provider.generate_live_candidate(
                run_id=run_id,
                route=ROUTE,
                activation_token=replay["activation_token"],
                confirmed=True,
                client_host="127.0.0.1",
                sdk_runner=_accepted_runner,
            )
        )
        assert pending["state"] == "VALIDATED_PENDING_USER_ACCEPTANCE"
        controls["10_replayed_token"] = _expect_code(
            lambda: asyncio.run(
                prototype_live_provider.generate_live_candidate(
                    run_id=run_id,
                    route=ROUTE,
                    activation_token=replay["activation_token"],
                    confirmed=True,
                    client_host="127.0.0.1",
                    sdk_runner=_accepted_runner,
                )
            ),
            "prototype_activation_token_replayed",
        )

        prototype_live_provider.reset_ephemeral_activation_state_for_tests()
        monkeypatch.setenv("XRML_PROTOTYPE_SESSION_ID", "offline-other-run-session")
        other_run_token = prototype_live_provider.issue_activation_token(
            run_id=run_id,
            route=ROUTE,
            client_host="127.0.0.1",
        )
        controls["11_token_for_another_run"] = _expect_code(
            lambda: asyncio.run(
                prototype_live_provider.generate_live_candidate(
                    run_id="another-run-id",
                    route=ROUTE,
                    activation_token=other_run_token["activation_token"],
                    confirmed=True,
                    client_host="127.0.0.1",
                    sdk_runner=_accepted_runner,
                )
            ),
            "prototype_activation_token_run_mismatch",
        )

        assert len(controls) == 11
        assert all(controls.values()), controls
        assert run["prototype"]["provider"]["live_provider_invoked"] is False


def test_required_offline_output_controls_12_through_16_and_22() -> None:
    run_id = "synthetic-run"
    evidence_ids = ["paper-a-evidence-1", "paper-b-evidence-1"]
    valid = _valid_candidate(run_id, evidence_ids)
    controls: dict[str, bool] = {}

    malformed = prototype_live_provider.validate_quarantined_output(
        raw_output=b"not-json",
        run_id=run_id,
        evidence_ids=evidence_ids,
    )
    controls["12_malformed_output"] = not malformed["valid"] and not malformed["schema_valid"]

    unknown = json.loads(json.dumps(valid))
    unknown["grounded_claims"][0]["evidence_ids"] = ["unknown-evidence"]
    unknown["referenced_evidence_ids"] = ["unknown-evidence", evidence_ids[1]]
    unknown_result = prototype_live_provider.validate_quarantined_output(
        raw_output=json.dumps(unknown).encode(),
        run_id=run_id,
        evidence_ids=evidence_ids,
    )
    controls["13_unknown_evidence"] = not unknown_result["valid"] and unknown_result["unknown_evidence_count"] == 1

    cross_run = json.loads(json.dumps(valid))
    cross_run["grounded_claims"][0]["evidence_ids"] = ["other-run-evidence-1"]
    cross_run["referenced_evidence_ids"] = ["other-run-evidence-1", evidence_ids[1]]
    cross_result = prototype_live_provider.validate_quarantined_output(
        raw_output=json.dumps(cross_run).encode(),
        run_id=run_id,
        evidence_ids=evidence_ids,
    )
    controls["14_cross_run_evidence"] = not cross_result["valid"] and cross_result["unknown_evidence_count"] == 1

    no_evidence = json.loads(json.dumps(valid))
    no_evidence["grounded_claims"][0]["evidence_ids"] = []
    no_evidence_result = prototype_live_provider.validate_quarantined_output(
        raw_output=json.dumps(no_evidence).encode(),
        run_id=run_id,
        evidence_ids=evidence_ids,
    )
    controls["15_grounded_without_evidence"] = not no_evidence_result["valid"] and not no_evidence_result["schema_valid"]

    untyped = json.loads(json.dumps(valid))
    del untyped["thematic_findings"][0]["statement_type"]
    untyped_result = prototype_live_provider.validate_quarantined_output(
        raw_output=json.dumps(untyped).encode(),
        run_id=run_id,
        evidence_ids=evidence_ids,
    )
    controls["16_untyped_unsupported_claim"] = not untyped_result["valid"] and not untyped_result["schema_valid"]

    leaked = json.loads(json.dumps(valid))
    leaked["executive_summary"] = "Traceback (most recent call last) at C:\\owner\\private\\draft.json"
    leaked_result = prototype_live_provider.validate_quarantined_output(
        raw_output=json.dumps(leaked).encode(),
        run_id=run_id,
        evidence_ids=evidence_ids,
    )
    controls["22_no_traceback_or_local_path"] = (
        not leaked_result["valid"]
        and "absolute_local_path_detected" in leaked_result["reasons"]
        and "traceback_or_raw_error_detected" in leaked_result["reasons"]
    )

    assert len(controls) == 6
    assert all(controls.values()), controls


def test_required_offline_lifecycle_controls_17_through_21(tmp_path: Path, app_factory, monkeypatch) -> None:
    data_root = (tmp_path / "prototype-live-lifecycle").resolve()
    _stub_known_available_dependency(monkeypatch)
    prototype_live_provider.reset_ephemeral_activation_state_for_tests()
    with TestClient(
        app_factory(
            AUTH_MODE="disabled",
            XRML_PROTOTYPE_MODE="1",
            XRML_PROTOTYPE_LIVE_PROVIDER_PROOF="1",
            XRML_PROTOTYPE_SESSION_ID="offline-lifecycle-accepted",
            XRML_DATA_DIR=str(data_root),
            XRML_RUN_EXECUTOR_POLL_SECONDS="0.05",
        ),
        client=("127.0.0.1", 50000),
    ) as client:
        run_id, run = _start_prototype_run(client)
        paths = build_run_paths(run_id)
        baseline_path = paths.reports_dir / "prototype-weekly-journal.md"
        baseline_digest = hashlib.sha256(baseline_path.read_bytes()).hexdigest()
        evidence_ids = _evidence_ids(run_id, str(run["topic_profile_id"]))

        def accepted_api_runner(**_kwargs):
            return {
                "status": "completed",
                "result_category": "provider_output_received",
                "provider_call_count": 1,
                "raw_output": json.dumps(_valid_candidate(run_id, evidence_ids), sort_keys=True),
                "duration_ms": 30,
                "model_backend_identifier": "recorded-test-backend",
                "reason_category": None,
            }

        monkeypatch.setattr("app.services.codex_cli_report_draft.run_codex_cli_report_draft_once", accepted_api_runner)
        activation = client.post(f"/prototype/runs/{run_id}/provider-proof/activation", json=ROUTE)
        assert activation.status_code == 200, activation.text
        token = activation.json()["activation_token"]
        generated = client.post(
            f"/prototype/runs/{run_id}/provider-proof/generate",
            json={**ROUTE, "activation_token": token, "confirmed": True},
        )
        assert generated.status_code == 200, generated.text
        execution = generated.json()
        assert execution["state"] == "VALIDATED_PENDING_USER_ACCEPTANCE"
        assert execution["trusted_persistence_count"] == 0

        preview = client.get(f"/prototype/runs/{run_id}/provider-proof/{execution['execution_id']}/candidate")
        assert preview.status_code == 200, preview.text
        assert "Provider-assisted synthetic evidence report" in preview.json()["markdown"]

        accepted = client.post(
            f"/prototype/runs/{run_id}/provider-proof/{execution['execution_id']}/accept",
            json={"confirmed": True},
        )
        assert accepted.status_code == 200, accepted.text
        accepted_payload = accepted.json()
        assert accepted_payload["state"] == "ACCEPTED"
        assert accepted_payload["trusted_persistence_count"] == 1
        provider_version_id = accepted_payload["resulting_report_version_id"]

        version_preview = client.get(f"/prototype/runs/{run_id}/report-versions/{provider_version_id}/preview")
        assert version_preview.status_code == 200, version_preview.text
        assert version_preview.json()["generation_mode"] == "provider_assisted"
        selected_export = client.post(f"/prototype/runs/{run_id}/report-versions/{provider_version_id}/export")
        assert selected_export.status_code == 200, selected_export.text
        assert selected_export.json()["artifact"]["generation_mode"] == "provider_assisted"

        comparison = client.get(f"/prototype/runs/{run_id}/provider-proof/{execution['execution_id']}/comparison")
        assert comparison.status_code == 200, comparison.text
        assert comparison.json()["ready"] is True
        reopened = client.get(f"/prototype/runs/{run_id}/provider-proof")
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["latest_execution"]["state"] == "ACCEPTED"
        assert hashlib.sha256(baseline_path.read_bytes()).hexdigest() == baseline_digest

        controls = {
            "19_accepted_fake_creates_separate_version": provider_version_id != accepted_payload["baseline_version_id"],
            "20_deterministic_baseline_unchanged": hashlib.sha256(baseline_path.read_bytes()).hexdigest() == baseline_digest,
        }

        prototype_live_provider.reset_ephemeral_activation_state_for_tests()
        monkeypatch.setenv("XRML_PROTOTYPE_SESSION_ID", "offline-lifecycle-rejected")

        def failed_runner(**_kwargs):
            return {
                "status": "failed",
                "result_category": "provider_failed",
                "provider_call_count": 1,
                "reason_category": "CODEX_CLI_EXIT_NONZERO",
            }

        monkeypatch.setattr("app.services.codex_cli_report_draft.run_codex_cli_report_draft_once", failed_runner)
        failure_activation = client.post(f"/prototype/runs/{run_id}/provider-proof/activation", json=ROUTE)
        assert failure_activation.status_code == 200, failure_activation.text
        failure = client.post(
            f"/prototype/runs/{run_id}/provider-proof/generate",
            json={**ROUTE, "activation_token": failure_activation.json()["activation_token"], "confirmed": True},
        )
        assert failure.status_code == 200, failure.text
        failure_payload = failure.json()
        controls["17_provider_failure_fallback"] = failure_payload["state"] == "FAILED" and failure_payload["fallback_used"] is True
        controls["18_rejected_trusted_persistence_zero"] = failure_payload["trusted_persistence_count"] == 0

        normal_export = client.post(f"/runs/{run_id}/export", json={"format": "report_markdown"})
        assert normal_export.status_code == 200, normal_export.text
        normal_export_id = normal_export.json()["artifact"]["export_id"]
        normal_export_path, _media_type = __import__("app.services.exports", fromlist=["resolve_export_download"]).resolve_export_download(
            run_id,
            normal_export_id,
        )
        controls["21_export_excludes_quarantined"] = "CODEX_CLI_EXIT_NONZERO" not in normal_export_path.read_text(encoding="utf-8")

        assert len(controls) == 5
        assert all(controls.values()), controls
        all_runtime_text = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in paths.reports_dir.rglob("*")
            if path.is_file() and path.name != "quarantine.raw"
        )
        assert token not in all_runtime_text
        assert data_root.name.lower() not in json.dumps(reopened.json()).lower()


def test_cli_schema_evidence_and_user_reject_do_not_persist_versions(
    tmp_path: Path, app_factory, monkeypatch: Any
) -> None:
    data_root = (tmp_path / "prototype-live-reject-paths").resolve()
    _stub_known_available_dependency(monkeypatch)
    prototype_live_provider.reset_ephemeral_activation_state_for_tests()

    def _client(session_id: str) -> TestClient:
        return TestClient(
            app_factory(
                AUTH_MODE="disabled",
                XRML_PROTOTYPE_MODE="1",
                XRML_PROTOTYPE_LIVE_PROVIDER_PROOF="1",
                XRML_PROTOTYPE_SESSION_ID=session_id,
                XRML_DATA_DIR=str(data_root),
                XRML_RUN_EXECUTOR_POLL_SECONDS="0.05",
            ),
            client=("127.0.0.1", 50000),
        )

    def _version_ids(client: TestClient, run_id: str) -> list[str]:
        return [
            str(item["version_id"])
            for item in client.get(f"/prototype/runs/{run_id}/provider-proof").json()["report_versions"]
        ]

    def _generate(client: TestClient, run_id: str) -> tuple[dict[str, Any], list[str]]:
        activation = client.post(f"/prototype/runs/{run_id}/provider-proof/activation", json=ROUTE)
        assert activation.status_code == 200, activation.text
        versions_after_activation = _version_ids(client, run_id)
        generated = client.post(
            f"/prototype/runs/{run_id}/provider-proof/generate",
            json={**ROUTE, "activation_token": activation.json()["activation_token"], "confirmed": True},
        )
        assert generated.status_code == 200, generated.text
        return generated.json(), versions_after_activation

    with _client("cli-schema-invalid") as client:
        run_id, _run = _start_prototype_run(client)

        def schema_invalid_runner(**_kwargs):
            return {
                "status": "completed",
                "result_category": "provider_output_received",
                "provider_call_count": 1,
                "raw_output": json.dumps({"schema_version": "wrong", "title": "x"}),
                "reason_category": None,
            }

        monkeypatch.setattr(
            "app.services.codex_cli_report_draft.run_codex_cli_report_draft_once",
            schema_invalid_runner,
        )
        payload, before = _generate(client, run_id)
        assert payload["state"] == "REJECTED"
        assert payload["schema_valid"] is False
        assert payload["trusted_persistence_count"] == 0
        assert "CODEX_CANDIDATE_SCHEMA_INVALID" in payload["rejection_reasons"]
        assert _version_ids(client, run_id) == before

    prototype_live_provider.reset_ephemeral_activation_state_for_tests()
    with _client("cli-unknown-evidence") as client:
        run_id, run = _start_prototype_run(client)
        evidence_ids = _evidence_ids(run_id, str(run["topic_profile_id"]))
        bad = json.loads(
            json.dumps(_valid_candidate(run_id, evidence_ids)).replace(evidence_ids[0], "unknown-paper-evidence-9")
        )

        def evidence_invalid_runner(**_kwargs):
            return {
                "status": "completed",
                "result_category": "provider_output_received",
                "provider_call_count": 1,
                "raw_output": json.dumps(bad, sort_keys=True),
                "reason_category": None,
            }

        monkeypatch.setattr(
            "app.services.codex_cli_report_draft.run_codex_cli_report_draft_once",
            evidence_invalid_runner,
        )
        payload, before = _generate(client, run_id)
        assert payload["state"] == "REJECTED"
        assert payload["evidence_valid"] is False
        assert payload["trusted_persistence_count"] == 0
        assert "CODEX_EVIDENCE_REFERENCE_INVALID" in payload["rejection_reasons"]
        assert _version_ids(client, run_id) == before

    prototype_live_provider.reset_ephemeral_activation_state_for_tests()
    with _client("cli-user-reject") as client:
        run_id, run = _start_prototype_run(client)
        evidence_ids = _evidence_ids(run_id, str(run["topic_profile_id"]))

        def accepted_runner(**_kwargs):
            return {
                "status": "completed",
                "result_category": "provider_output_received",
                "provider_call_count": 1,
                "raw_output": json.dumps(_valid_candidate(run_id, evidence_ids), sort_keys=True),
                "reason_category": None,
            }

        monkeypatch.setattr(
            "app.services.codex_cli_report_draft.run_codex_cli_report_draft_once",
            accepted_runner,
        )
        payload, before = _generate(client, run_id)
        rejected = client.post(
            f"/prototype/runs/{run_id}/provider-proof/{payload['execution_id']}/reject",
            json={"confirmed": True},
        )
        assert rejected.status_code == 200, rejected.text
        rejected_payload = rejected.json()
        assert rejected_payload["state"] == "REJECTED"
        assert rejected_payload["trusted_persistence_count"] == 0
        assert rejected_payload["rejection_reasons"] == ["CODEX_CANDIDATE_REJECTED"]
        assert _version_ids(client, run_id) == before


def test_usage_limit_keeps_acceptance_unattempted_and_versions_unchanged(
    tmp_path: Path, app_factory, monkeypatch: Any
) -> None:
    data_root = (tmp_path / "prototype-live-usage-limit").resolve()
    _stub_known_available_dependency(monkeypatch)
    prototype_live_provider.reset_ephemeral_activation_state_for_tests()
    original = prototype_live_provider.codex_cli_report_draft.run_codex_cli_report_draft_once

    def usage_limit_runner(**kwargs: Any) -> dict[str, Any]:
        def injected(_argv: Any, _prompt: str, _cwd: str, _env: Any) -> CliExecResult:
            stdout = "\n".join(
                [
                    json.dumps({"type": "thread.started"}),
                    json.dumps({"type": "turn.started"}),
                    json.dumps(
                        {
                            "type": "error",
                            "message": (
                                "You've hit your usage limit. Retry after Aug 27, 2026 12:47 PM "
                                "or purchase more credits. Bearer leaked-token-value-123456"
                            ),
                        }
                    ),
                    json.dumps(
                        {
                            "type": "turn.failed",
                            "error": {
                                "message": "You've hit your usage limit. Retry after Aug 27, 2026 12:47 PM",
                            },
                        }
                    ),
                ]
            )
            return CliExecResult(1, stdout, r"cookie=abc123 C:\Users\EXAMPLE\.codex\auth.json")

        kwargs = dict(kwargs)
        kwargs["runner"] = injected
        kwargs.setdefault("executable", "codex")
        return original(**kwargs)

    monkeypatch.setattr(
        "app.services.codex_cli_report_draft.run_codex_cli_report_draft_once",
        usage_limit_runner,
    )
    with TestClient(
        app_factory(
            AUTH_MODE="disabled",
            XRML_PROTOTYPE_MODE="1",
            XRML_PROTOTYPE_LIVE_PROVIDER_PROOF="1",
            XRML_PROTOTYPE_SESSION_ID="offline-usage-limit",
            XRML_DATA_DIR=str(data_root),
            XRML_RUN_EXECUTOR_POLL_SECONDS="0.05",
        ),
        client=("127.0.0.1", 50000),
    ) as client:
        run_id, _run = _start_prototype_run(client)
        before_versions = [
            item["version_id"]
            for item in client.get(f"/prototype/runs/{run_id}/provider-proof").json()["report_versions"]
        ]
        activation = client.post(f"/prototype/runs/{run_id}/provider-proof/activation", json=ROUTE)
        assert activation.status_code == 200, activation.text
        versions_after_activation = [
            item["version_id"]
            for item in client.get(f"/prototype/runs/{run_id}/provider-proof").json()["report_versions"]
        ]
        generated = client.post(
            f"/prototype/runs/{run_id}/provider-proof/generate",
            json={**ROUTE, "activation_token": activation.json()["activation_token"], "confirmed": True},
        )
        assert generated.status_code == 200, generated.text
        payload = generated.json()
        status = client.get(f"/prototype/runs/{run_id}/provider-proof").json()
        diagnostics = payload["safe_diagnostics"]
        rendered = json.dumps(status)

        assert payload["state"] == "FAILED"
        assert payload["reason_category"] == "CODEX_CLI_USAGE_LIMIT"
        assert payload["rejection_reasons"] == ["CODEX_CLI_USAGE_LIMIT"]
        assert payload["candidate_available"] is False
        assert payload["schema_valid"] is False
        assert payload["evidence_valid"] is False
        assert payload["accepted"] is False
        assert payload["acceptance_state"] == "not_attempted"
        assert payload["trusted_persistence_count"] == 0
        assert payload["fallback_used"] is True
        assert status["deterministic_fallback_available"] is True
        assert [item["version_id"] for item in status["report_versions"]] == versions_after_activation
        assert diagnostics["usage_limit"]["source"] == "codex_cli"
        assert diagnostics["usage_limit"]["reset_time_text"] == "Aug 27, 2026 12:47 PM"
        assert diagnostics["usage_limit"]["reset_timezone_claimed"] is False
        assert "leaked-token-value-123456" not in rendered
        assert "abc123" not in rendered
        assert before_versions or versions_after_activation
