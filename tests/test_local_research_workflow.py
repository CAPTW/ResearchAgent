from __future__ import annotations

import json
from pathlib import Path

from tools.local_research_workflow import EndpointResult, LocalResearchWorkflow, StartedProcess, WorkflowConfig


class FakeApiClient:
    def __init__(self, runs_root: Path) -> None:
        self.runs_root = runs_root
        self.profiles = [
            {
                "profile_id": "default-xrml",
                "title": "Default XRML Monitor",
                "topic": "XR/AR x ML",
                "queries": ["xr"],
                "categories": ["cs.CV"],
                "max_results": 10,
                "top_papers": 3,
                "weekly_top": 10,
            }
        ]
        self.run_counter = 0
        self.calls: list[tuple[str, str]] = []

    def probe(self, path: str, *, expect_json: bool = True, base_url: str | None = None) -> EndpointResult:
        url_root = (base_url or "http://127.0.0.1:8000").rstrip("/")
        return EndpointResult(url=f"{url_root}/{path.lstrip('/')}" if path else url_root, status_code=200, ok=True)

    def get_json(self, path: str) -> dict[str, object]:
        self.calls.append(("GET", path))
        if path == "/settings/status":
            return {
                "provider": {"provider_mode": "fallback_only"},
                "stitch_status": "seam_prepared",
            }
        if path == "/topic-profiles":
            return {"items": list(self.profiles)}
        if path == "/dashboard/overview":
            return {"totals": {"runs": 1}}
        if path.startswith("/runs/") and path.endswith("/preview"):
            run_id = path.split("/")[2]
            preview_path = self.runs_root / run_id / "reports" / "preview.md"
            return {"run_id": run_id, "path": str(preview_path), "content": "# preview"}
        if path.startswith("/runs/") and path.endswith("/exports"):
            run_id = path.split("/")[2]
            export_path = self.runs_root / run_id / "reports" / "exported_research_plan.md"
            return {"run_id": run_id, "items": [{"export_id": f"{run_id}-1", "output": str(export_path)}]}
        if path.startswith("/runs/"):
            run_id = path.split("/")[2]
            run_dir = self.runs_root / run_id
            return {
                "run_id": run_id,
                "status": "completed",
                "legacy_compatibility": {"authoritative_root": str(run_dir)},
                "artifacts": {
                    "research_plan_json": str(run_dir / "reports" / "research_plan.json"),
                    "research_plan_markdown": str(run_dir / "reports" / "research_plan.md"),
                    "report_markdown": str(run_dir / "reports" / "research_plan.md"),
                },
            }
        raise AssertionError(f"Unexpected GET path: {path}")

    def post_json(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append(("POST", path))
        if path == "/topic-profiles":
            profile = {
                "profile_id": f"profile-{len(self.profiles) + 1}",
                **payload,
            }
            self.profiles.append(profile)
            return {"items": list(self.profiles)}
        if path == "/dashboard/start-runs":
            self.run_counter += 1
            run_id = f"local-run-{self.run_counter}"
            self._seed_run(run_id)
            return {"items": [{"profile_id": payload["profile_ids"][0], "run_id": run_id, "topic": "topic", "status": "queued"}]}
        if path.endswith("/export"):
            run_id = path.split("/")[2]
            export_path = self.runs_root / run_id / "reports" / "exported_research_plan.md"
            return {"status": "ok", "output": str(export_path)}
        raise AssertionError(f"Unexpected POST path: {path}")

    def patch_json(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append(("PATCH", path))
        profile_id = path.split("/")[-1]
        for index, profile in enumerate(self.profiles):
            if profile["profile_id"] == profile_id:
                updated = dict(profile)
                updated.update(payload)
                self.profiles[index] = updated
                break
        return {"items": list(self.profiles)}

    def _seed_run(self, run_id: str) -> None:
        run_dir = self.runs_root / run_id
        reports_dir = run_dir / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
        (reports_dir / "research_plan.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
        (reports_dir / "research_plan.md").write_text("# plan\n", encoding="utf-8")
        (reports_dir / "preview.md").write_text("# preview\n", encoding="utf-8")
        (reports_dir / "exported_research_plan.md").write_text("# export\n", encoding="utf-8")
        (reports_dir / "exports.json").write_text(json.dumps([{"export_id": f"{run_id}-1"}]), encoding="utf-8")


class FakeProcessController:
    def __init__(self) -> None:
        self.started = [
            StartedProcess(
                name="backend",
                pid=1234,
                log_path="C:/tmp/backend.log",
                command=["uv", "run", "uvicorn", "app.main:app"],
            )
        ]

    def ensure_frontend_dist(self) -> str | None:
        return None

    def start_backend(self) -> str | None:
        return None

    def start_frontend(self) -> str | None:
        return None


def test_local_research_workflow_creates_then_updates_profile_and_checks_artifacts(tmp_path: Path) -> None:
    runs_root = tmp_path / "storage" / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)
    api = FakeApiClient(runs_root)
    process = FakeProcessController()
    config = WorkflowConfig(
        topic="Gaussian avatars in XR",
        profile_name="Gaussian XR",
        storage_root=tmp_path / "storage",
        poll_timeout_seconds=1,
        poll_interval_seconds=0.01,
        process_start_timeout_seconds=1,
    )

    first = LocalResearchWorkflow(config, api_client=api, process_controller=process, sleep_fn=lambda _: None).execute()
    second = LocalResearchWorkflow(config, api_client=api, process_controller=process, sleep_fn=lambda _: None).execute()

    assert first["status"] == "success"
    assert first["provider_mode"] == "fallback_only"
    assert first["run_id"] == "local-run-1"
    assert first["failures"] == []
    assert Path(first["artifact_paths"]["run_json"]).exists()
    assert Path(first["artifact_paths"]["research_plan_json"]).exists()
    assert Path(first["artifact_paths"]["research_plan_markdown"]).exists()
    assert Path(first["artifact_paths"]["export_output"]).exists()
    assert second["status"] == "success"
    assert second["run_id"] == "local-run-2"
    assert ("POST", "/topic-profiles") in api.calls
    assert ("PATCH", "/topic-profiles/profile-2") in api.calls
    assert ("POST", "/dashboard/start-runs") in api.calls
