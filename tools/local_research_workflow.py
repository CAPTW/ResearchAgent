from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = REPO_ROOT / "web"
DEFAULT_BACKEND_HOST = "127.0.0.1"
DEFAULT_BACKEND_PORT = 8000
DEFAULT_FRONTEND_PORT = 5173
DEFAULT_CATEGORIES = ["cs.CV"]
RESULT_STATUS_SUCCESS = "success"
RESULT_STATUS_DRY_RUN = "dry_run"
RESULT_STATUS_NOT_EXECUTED = "not_executed"
RESULT_STATUS_FAILED = "failed"
RESULT_STATUSES = {
    RESULT_STATUS_SUCCESS,
    RESULT_STATUS_DRY_RUN,
    RESULT_STATUS_NOT_EXECUTED,
    RESULT_STATUS_FAILED,
}


class WorkflowError(RuntimeError):
    pass


class WorkflowNotExecuted(WorkflowError):
    pass


@dataclass
class WorkflowConfig:
    topic: str
    profile_name: str
    storage_root: Path | None = None
    dry_run: bool = False
    backend_host: str = DEFAULT_BACKEND_HOST
    backend_port: int = DEFAULT_BACKEND_PORT
    frontend_port: int = DEFAULT_FRONTEND_PORT
    categories: list[str] = field(default_factory=lambda: list(DEFAULT_CATEGORIES))
    max_results: int = 20
    top_papers: int = 3
    weekly_top: int = 10
    poll_timeout_seconds: int = 180
    poll_interval_seconds: float = 2.0
    process_start_timeout_seconds: int = 45

    @property
    def backend_base_url(self) -> str:
        return f"http://{self.backend_host}:{self.backend_port}"

    @property
    def frontend_url(self) -> str:
        return f"http://{self.backend_host}:{self.frontend_port}"

    @property
    def data_root(self) -> Path:
        if self.storage_root is not None:
            return self.storage_root
        return REPO_ROOT / "storage"

    @property
    def runs_root(self) -> Path:
        return self.data_root / "runs"


@dataclass
class EndpointResult:
    url: str
    status_code: int | None
    ok: bool
    detail: str | None = None


@dataclass
class StartedProcess:
    name: str
    pid: int
    log_path: str
    command: list[str]


class HttpApiClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def get_json(self, path: str) -> dict[str, Any]:
        status, payload, _ = self.request("GET", path)
        if status != 200 or not isinstance(payload, dict):
            raise WorkflowError(f"GET {path} returned {status}: {payload!r}")
        return payload

    def get_text(self, path: str, *, base_url: str | None = None) -> str:
        status, payload, _ = self.request("GET", path, expect_json=False, base_url=base_url)
        if status != 200:
            raise WorkflowError(f"GET {path} returned {status}: {payload!r}")
        return str(payload)

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        status, body, _ = self.request("POST", path, payload=payload)
        if status != 200 or not isinstance(body, dict):
            raise WorkflowError(f"POST {path} returned {status}: {body!r}")
        return body

    def patch_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        status, body, _ = self.request("PATCH", path, payload=payload)
        if status != 200 or not isinstance(body, dict):
            raise WorkflowError(f"PATCH {path} returned {status}: {body!r}")
        return body

    def probe(self, path: str, *, expect_json: bool = True, base_url: str | None = None) -> EndpointResult:
        try:
            status, payload, _ = self.request("GET", path, expect_json=expect_json, base_url=base_url)
            return EndpointResult(url=self._url(path, base_url=base_url), status_code=status, ok=status == 200, detail=None if status == 200 else str(payload))
        except WorkflowError as exc:
            return EndpointResult(url=self._url(path, base_url=base_url), status_code=None, ok=False, detail=str(exc))

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        expect_json: bool = True,
        base_url: str | None = None,
    ) -> tuple[int, Any, dict[str, str]]:
        target = self._url(path, base_url=base_url)
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            target,
            data=body,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urlopen(request, timeout=10) as response:
                text = response.read().decode("utf-8")
                parsed = json.loads(text) if expect_json else text
                return response.getcode(), parsed, dict(response.headers.items())
        except HTTPError as error:
            raw = error.read().decode("utf-8")
            if expect_json:
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = {"raw": raw}
            else:
                parsed = raw
            return error.code, parsed, dict(error.headers.items())
        except URLError as error:
            raise WorkflowError(f"Could not reach {target}: {error.reason}") from error

    def _url(self, path: str, *, base_url: str | None = None) -> str:
        root = (base_url or self.base_url).rstrip("/")
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{root}/{path.lstrip('/')}" if path else root


class LocalProcessController:
    def __init__(self, config: WorkflowConfig) -> None:
        self.config = config
        self.logs_dir = Path(tempfile.gettempdir()) / "xrml-local-research-workflow"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.started: list[StartedProcess] = []

    def ensure_frontend_dist(self) -> str | None:
        dist_index = WEB_ROOT / "dist" / "index.html"
        if dist_index.is_file():
            return None
        command = [self._npm_command(), "run", "build"]
        if self.config.dry_run:
            return " ".join(command)
        self._run_command(
            command,
            cwd=WEB_ROOT,
            extra_env={"WEB_APP_BASE_PATH": "/app"},
            name="frontend-build",
            wait=True,
        )
        return None

    def start_backend(self) -> str | None:
        command = [
            "uv",
            "run",
            "uvicorn",
            "app.main:app",
            "--host",
            self.config.backend_host,
            "--port",
            str(self.config.backend_port),
        ]
        if self.config.dry_run:
            return " ".join(command)
        env = {
            "AUTH_MODE": "disabled",
            "APP_BASE_URL": self.config.backend_base_url,
            "WEB_APP_BASE_PATH": "/app",
        }
        if self.config.storage_root is not None:
            env["XRML_DATA_DIR"] = str(self.config.storage_root)
        self._run_command(command, cwd=REPO_ROOT, extra_env=env, name="backend", wait=False)
        return None

    def start_frontend(self) -> str | None:
        command = [
            self._npm_command(),
            "run",
            "dev",
            "--",
            "--host",
            self.config.backend_host,
            "--port",
            str(self.config.frontend_port),
        ]
        if self.config.dry_run:
            return " ".join(command)
        env = {
            "VITE_API_BASE_URL": self.config.backend_base_url,
        }
        self._run_command(command, cwd=WEB_ROOT, extra_env=env, name="frontend", wait=False)
        return None

    def _run_command(
        self,
        command: list[str],
        *,
        cwd: Path,
        extra_env: dict[str, str],
        name: str,
        wait: bool,
    ) -> None:
        env = os.environ.copy()
        env.update(extra_env)
        log_path = self.logs_dir / f"{name}.log"
        with log_path.open("a", encoding="utf-8") as handle:
            if wait:
                completed = subprocess.run(command, cwd=str(cwd), env=env, stdout=handle, stderr=subprocess.STDOUT, text=True)
                if completed.returncode != 0:
                    raise WorkflowError(f"{name} failed with exit code {completed.returncode}. See {log_path}")
                return
            process = subprocess.Popen(command, cwd=str(cwd), env=env, stdout=handle, stderr=subprocess.STDOUT, text=True)
        self.started.append(StartedProcess(name=name, pid=process.pid, log_path=str(log_path), command=command))

    @staticmethod
    def _npm_command() -> str:
        return "npm.cmd" if os.name == "nt" else "npm"


class LocalResearchWorkflow:
    def __init__(
        self,
        config: WorkflowConfig,
        *,
        api_client: HttpApiClient | None = None,
        process_controller: LocalProcessController | None = None,
        sleep_fn=time.sleep,
    ) -> None:
        self.config = config
        self.api = api_client or HttpApiClient(config.backend_base_url)
        self.process_controller = process_controller or LocalProcessController(config)
        self.sleep_fn = sleep_fn

    def execute(self) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "status": RESULT_STATUS_NOT_EXECUTED,
            "topic": self.config.topic,
            "profile_name": self.config.profile_name,
            "dry_run": self.config.dry_run,
            "provider_mode": None,
            "run_id": None,
            "profile_id": None,
            "artifact_paths": {},
            "local_ui_urls": {
                "app": f"{self.config.backend_base_url}/app",
                "frontend_dev": self.config.frontend_url,
            },
            "endpoint_checks": {},
            "started_processes": [],
            "planned_actions": [],
            "failures": [],
        }

        backend_ready = self._ensure_backend(summary)
        self._verify_backend_endpoints(summary, require_all=not self.config.dry_run)
        self._ensure_frontend(summary)
        settings_check = summary["endpoint_checks"].get("settings_status", {})
        if settings_check.get("ok"):
            settings = self.api.get_json("/settings/status")
            provider = settings.get("provider") if isinstance(settings.get("provider"), dict) else {}
            summary["provider_mode"] = provider.get("provider_mode")
        else:
            summary["provider_mode"] = "unknown"

        if self.config.dry_run:
            summary["status"] = RESULT_STATUS_DRY_RUN
            summary["backend_ready"] = backend_ready
            summary["failures"] = [
                name
                for name, result in summary["endpoint_checks"].items()
                if isinstance(result, dict) and not result.get("ok", False)
            ]
            return summary

        profile = self._upsert_topic_profile()
        summary["profile_id"] = profile["profile_id"]

        start_response = self.api.post_json("/dashboard/start-runs", {"profile_ids": [profile["profile_id"]]})
        items = start_response.get("items") or []
        if not items:
            raise WorkflowNotExecuted("Run start returned no items.")
        run_id = str(items[0].get("run_id", "")).strip()
        if not run_id:
            raise WorkflowNotExecuted("Run start returned an empty run_id.")
        summary["run_id"] = run_id
        summary["local_ui_urls"]["run_detail"] = f"{self.config.backend_base_url}/app/runs/{run_id}"

        run_detail = self._poll_run(run_id)
        if run_detail.get("status") != "completed":
            raise WorkflowError(f"Run {run_id} ended with status {run_detail.get('status')}: {run_detail.get('error')}")

        preview = self.api.get_json(f"/runs/{run_id}/preview")
        export_payload = self.api.post_json(f"/runs/{run_id}/export", {"format": "report_markdown"})
        export_list = self.api.get_json(f"/runs/{run_id}/exports")
        summary["artifact_paths"] = self._collect_artifacts(run_id, run_detail, preview, export_payload, export_list)
        summary["started_processes"] = [self._started_process_to_dict(item) for item in self.process_controller.started]
        summary["status"] = RESULT_STATUS_SUCCESS
        return summary

    def _ensure_backend(self, summary: dict[str, Any]) -> bool:
        health = self.api.probe("/health")
        if not health.ok:
            build_command = self.process_controller.ensure_frontend_dist()
            if build_command:
                summary["planned_actions"].append({"action": "build_frontend_dist", "command": build_command})
            backend_command = self.process_controller.start_backend()
            if backend_command:
                summary["planned_actions"].append({"action": "start_backend", "command": backend_command})
                return False
            self._wait_for(lambda: self.api.probe("/health").ok, "backend /health")
        summary["started_processes"] = [self._started_process_to_dict(item) for item in self.process_controller.started]
        return True

    def _ensure_frontend(self, summary: dict[str, Any]) -> None:
        frontend = self.api.probe("", expect_json=False, base_url=self.config.frontend_url)
        if frontend.ok:
            summary["endpoint_checks"]["frontend_dev"] = self._endpoint_to_dict(frontend)
            return
        command = self.process_controller.start_frontend()
        if command:
            summary["planned_actions"].append({"action": "start_frontend", "command": command})
            summary["endpoint_checks"]["frontend_dev"] = self._endpoint_to_dict(frontend)
            return
        self._wait_for(lambda: self.api.probe("", expect_json=False, base_url=self.config.frontend_url).ok, "frontend dev server")
        summary["endpoint_checks"]["frontend_dev"] = self._endpoint_to_dict(self.api.probe("", expect_json=False, base_url=self.config.frontend_url))

    def _verify_backend_endpoints(self, summary: dict[str, Any], *, require_all: bool) -> None:
        results = {
            "health": self.api.probe("/health"),
            "app": self.api.probe("/app", expect_json=False),
            "dashboard_overview": self.api.probe("/dashboard/overview"),
            "topic_profiles": self.api.probe("/topic-profiles"),
            "settings_status": self.api.probe("/settings/status"),
        }
        summary["endpoint_checks"].update({name: self._endpoint_to_dict(result) for name, result in results.items()})
        failed = [name for name, result in results.items() if not result.ok]
        if failed and require_all:
            raise WorkflowError(f"Required local endpoints failed: {', '.join(failed)}")

    def _upsert_topic_profile(self) -> dict[str, Any]:
        listing = self.api.get_json("/topic-profiles")
        items = listing.get("items") or []
        payload = {
            "title": self.config.profile_name,
            "description": f"Local workflow profile for {self.config.topic}",
            "topic": self.config.topic,
            "queries": [self.config.topic],
            "categories": self.config.categories,
            "max_results": self.config.max_results,
            "top_papers": self.config.top_papers,
            "weekly_top": self.config.weekly_top,
        }
        matched = next((item for item in items if str(item.get("title", "")).strip() == self.config.profile_name), None)
        if matched is None:
            updated = self.api.post_json("/topic-profiles", payload)
        else:
            updated = self.api.patch_json(f"/topic-profiles/{matched['profile_id']}", payload)
        refreshed_items = updated.get("items") or []
        profile = next((item for item in refreshed_items if str(item.get("title", "")).strip() == self.config.profile_name), None)
        if profile is None:
            raise WorkflowError(f"Could not resolve topic profile after upsert: {self.config.profile_name}")
        return profile

    def _poll_run(self, run_id: str) -> dict[str, Any]:
        deadline = time.time() + self.config.poll_timeout_seconds
        while time.time() < deadline:
            payload = self.api.get_json(f"/runs/{run_id}")
            status = str(payload.get("status", "")).strip()
            if status in {"completed", "failed"}:
                return payload
            self.sleep_fn(self.config.poll_interval_seconds)
        raise WorkflowError(f"Timed out waiting for run {run_id} to finish.")

    def _collect_artifacts(
        self,
        run_id: str,
        run_detail: dict[str, Any],
        preview: dict[str, Any],
        export_payload: dict[str, Any],
        export_list: dict[str, Any],
    ) -> dict[str, str]:
        authoritative_root = (
            str((run_detail.get("legacy_compatibility") or {}).get("authoritative_root", "")).strip()
            or str((self.config.runs_root / run_id).resolve())
        )
        run_dir = Path(authoritative_root)
        artifacts = run_detail.get("artifacts") if isinstance(run_detail.get("artifacts"), dict) else {}
        preview_path = str(preview.get("path", "")).strip() or str(run_dir / "reports" / "preview.md")
        export_output = str(export_payload.get("output", "")).strip()
        report_markdown = str(artifacts.get("research_plan_markdown") or artifacts.get("report_markdown") or "").strip()
        exports_manifest = run_dir / "reports" / "exports.json"
        research_plan_json = str(artifacts.get("research_plan_json") or "").strip() or str(run_dir / "reports" / "research_plan.json")
        research_plan_md = str(artifacts.get("research_plan_markdown") or "").strip() or str(run_dir / "reports" / "research_plan.md")

        expected = {
            "run_json": str(run_dir / "run.json"),
            "research_plan_json": research_plan_json,
            "research_plan_markdown": research_plan_md,
            "report_markdown": report_markdown,
            "preview_markdown": preview_path,
            "exports_manifest": str(exports_manifest),
            "export_output": export_output,
        }
        missing = [name for name, value in expected.items() if not value or not Path(value).exists()]
        if missing:
            raise WorkflowError(f"Expected artifacts were missing after run {run_id}: {', '.join(missing)}")

        export_items = export_list.get("items") or []
        if not export_items:
            raise WorkflowError(f"Export manifest for run {run_id} is empty.")
        return expected

    def _wait_for(self, probe, label: str) -> None:
        deadline = time.time() + self.config.process_start_timeout_seconds
        while time.time() < deadline:
            if probe():
                return
            self.sleep_fn(1.0)
        raise WorkflowError(f"Timed out waiting for {label}.")

    @staticmethod
    def _endpoint_to_dict(result: EndpointResult) -> dict[str, Any]:
        return {
            "url": result.url,
            "status_code": result.status_code,
            "ok": result.ok,
            "detail": result.detail,
        }

    @staticmethod
    def _started_process_to_dict(item: StartedProcess) -> dict[str, Any]:
        return {
            "name": item.name,
            "pid": item.pid,
            "log_path": item.log_path,
            "command": item.command,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local XRML research workflow end-to-end.")
    parser.add_argument("--topic", required=True, help="Research topic to turn into a topic profile and run.")
    parser.add_argument("--profile-name", default="", help="Topic profile title. Defaults to '<topic> local profile'.")
    parser.add_argument("--storage-root", default="", help="Optional XRML_DATA_DIR override root.")
    parser.add_argument("--dry-run", action="store_true", help="Do not mutate data or start servers; only report planned actions and current checks.")
    parser.add_argument("--json-out", default="", help="Optional path to write the machine-readable result JSON.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when the workflow result is not success or dry_run.")
    parser.add_argument(
        "--fail-if-not-executed",
        action="store_true",
        help="Treat not_executed as a non-zero exit. This is implied by --strict for status evaluation.",
    )
    parser.add_argument("--backend-host", default=DEFAULT_BACKEND_HOST)
    parser.add_argument("--backend-port", type=int, default=DEFAULT_BACKEND_PORT)
    parser.add_argument("--frontend-port", type=int, default=DEFAULT_FRONTEND_PORT)
    parser.add_argument("--max-results", type=int, default=20)
    parser.add_argument("--top-papers", type=int, default=3)
    parser.add_argument("--weekly-top", type=int, default=10)
    parser.add_argument("--poll-timeout-seconds", type=int, default=180)
    parser.add_argument("--poll-interval-seconds", type=float, default=2.0)
    parser.add_argument("--process-start-timeout-seconds", type=int, default=45)
    return parser


def build_config(args: argparse.Namespace) -> WorkflowConfig:
    topic = str(args.topic).strip()
    if not topic:
        raise WorkflowError("--topic must not be empty.")
    profile_name = str(args.profile_name).strip() or f"{topic} local profile"
    storage_root = Path(args.storage_root).expanduser().resolve() if str(args.storage_root).strip() else None
    return WorkflowConfig(
        topic=topic,
        profile_name=profile_name,
        storage_root=storage_root,
        dry_run=bool(args.dry_run),
        backend_host=str(args.backend_host).strip(),
        backend_port=int(args.backend_port),
        frontend_port=int(args.frontend_port),
        max_results=int(args.max_results),
        top_papers=int(args.top_papers),
        weekly_top=int(args.weekly_top),
        poll_timeout_seconds=int(args.poll_timeout_seconds),
        poll_interval_seconds=float(args.poll_interval_seconds),
        process_start_timeout_seconds=int(args.process_start_timeout_seconds),
    )


def normalize_result_payload(
    payload: dict[str, Any] | None,
    *,
    executed_via: str,
    error: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": RESULT_STATUS_NOT_EXECUTED,
        "run_id": None,
        "provider_mode": None,
        "artifact_paths": {},
        "local_ui_urls": {},
        "failures": [],
        "executed_via": executed_via,
    }
    if payload:
        result.update(payload)

    artifact_paths = result.get("artifact_paths")
    local_ui_urls = result.get("local_ui_urls")
    failures = result.get("failures")
    result["artifact_paths"] = dict(artifact_paths) if isinstance(artifact_paths, dict) else {}
    result["local_ui_urls"] = dict(local_ui_urls) if isinstance(local_ui_urls, dict) else {}

    normalized_failures: list[str] = []
    if isinstance(failures, list):
        normalized_failures = [str(item).strip() for item in failures if str(item).strip()]
    elif failures:
        normalized_failures = [str(failures).strip()]
    if error and error not in normalized_failures:
        normalized_failures.append(error)
        result["error"] = error
    result["failures"] = normalized_failures
    result["executed_via"] = executed_via

    candidate_status = str(result.get("status") or "").strip().lower()
    if result.get("dry_run"):
        normalized_status = RESULT_STATUS_DRY_RUN
    elif candidate_status == RESULT_STATUS_FAILED:
        normalized_status = RESULT_STATUS_FAILED
    elif candidate_status == RESULT_STATUS_DRY_RUN:
        normalized_status = RESULT_STATUS_DRY_RUN
    elif candidate_status == RESULT_STATUS_SUCCESS and not result.get("run_id"):
        normalized_status = RESULT_STATUS_NOT_EXECUTED
    elif result.get("run_id"):
        normalized_status = RESULT_STATUS_SUCCESS
    elif candidate_status == RESULT_STATUS_NOT_EXECUTED:
        normalized_status = RESULT_STATUS_NOT_EXECUTED
    else:
        normalized_status = RESULT_STATUS_NOT_EXECUTED

    if candidate_status == RESULT_STATUS_SUCCESS and not str(result.get("run_id") or "").strip():
        if "Missing run_id for a supposed success result." not in result["failures"]:
            result["failures"].append("Missing run_id for a supposed success result.")

    if normalized_status == RESULT_STATUS_SUCCESS and not str(result.get("run_id") or "").strip():
        normalized_status = RESULT_STATUS_NOT_EXECUTED
        if "Missing run_id for a supposed success result." not in result["failures"]:
            result["failures"].append("Missing run_id for a supposed success result.")

    if normalized_status not in RESULT_STATUSES:
        normalized_status = RESULT_STATUS_FAILED
    result["status"] = normalized_status
    result["run_id"] = str(result.get("run_id") or "").strip() or None
    provider_mode = result.get("provider_mode")
    result["provider_mode"] = str(provider_mode).strip() if provider_mode is not None and str(provider_mode).strip() else None
    return result


def emit_result(payload: dict[str, Any], *, json_out: str) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if str(json_out).strip():
        output_path = Path(json_out).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text + "\n", encoding="utf-8")
    print(text)


def exit_code_for_result(payload: dict[str, Any], *, strict: bool, fail_if_not_executed: bool) -> int:
    status = str(payload.get("status") or "").strip()
    if status == RESULT_STATUS_SUCCESS or status == RESULT_STATUS_DRY_RUN:
        return 0
    if status == RESULT_STATUS_FAILED:
        return 1
    if status == RESULT_STATUS_NOT_EXECUTED and (strict or fail_if_not_executed):
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = build_config(args)
        workflow = LocalResearchWorkflow(config)
        summary = normalize_result_payload(workflow.execute(), executed_via="runner_cli")
        emit_result(summary, json_out=str(args.json_out))
        return exit_code_for_result(summary, strict=bool(args.strict), fail_if_not_executed=bool(args.fail_if_not_executed))
    except WorkflowNotExecuted as exc:
        payload = normalize_result_payload(
            {
                "status": RESULT_STATUS_NOT_EXECUTED,
                "topic": str(getattr(args, "topic", "") or "").strip() or None,
                "profile_name": str(getattr(args, "profile_name", "") or "").strip() or None,
                "dry_run": bool(getattr(args, "dry_run", False)),
            },
            executed_via="runner_cli",
            error=str(exc),
        )
        emit_result(payload, json_out=str(args.json_out))
        return exit_code_for_result(payload, strict=bool(args.strict), fail_if_not_executed=bool(args.fail_if_not_executed))
    except WorkflowError as exc:
        payload = normalize_result_payload(
            {
                "status": RESULT_STATUS_FAILED,
                "topic": str(getattr(args, "topic", "") or "").strip() or None,
                "profile_name": str(getattr(args, "profile_name", "") or "").strip() or None,
                "dry_run": bool(getattr(args, "dry_run", False)),
            },
            executed_via="runner_cli",
            error=str(exc),
        )
        emit_result(payload, json_out=str(args.json_out))
        return 1
    except Exception as exc:
        payload = normalize_result_payload(
            {
                "status": RESULT_STATUS_FAILED,
                "topic": str(getattr(args, "topic", "") or "").strip() or None,
                "profile_name": str(getattr(args, "profile_name", "") or "").strip() or None,
                "dry_run": bool(getattr(args, "dry_run", False)),
            },
            executed_via="runner_cli",
            error=str(exc),
        )
        emit_result(payload, json_out=str(args.json_out))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
