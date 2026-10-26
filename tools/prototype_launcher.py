from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = REPO_ROOT / "web"
HOST = "127.0.0.1"
DATA_PREFIX = "xrml-prototype-v0.5-"
PROTOTYPE_LABEL = "Prototype v0.5 - Living Topic refresh delta and recurring briefing"
TERMINAL_RUN_STATES = {"completed", "failed"}


class LauncherError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _npm_command() -> str:
    return "npm.cmd" if os.name == "nt" else "npm"


def _port_is_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(0.25)
        return client.connect_ex((HOST, port)) == 0


def _require_free_port(port: int) -> None:
    if _port_is_open(port):
        raise LauncherError(f"loopback_port_in_use:{port}")


def _json_request(method: str, url: str, payload: dict[str, Any] | None = None) -> tuple[int, Any]:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=body, headers={"Content-Type": "application/json"}, method=method)
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            raw = response.read().decode("utf-8")
            if not raw:
                return response.getcode(), None
            try:
                return response.getcode(), json.loads(raw)
            except json.JSONDecodeError:
                return response.getcode(), raw
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, None
    except URLError as exc:
        raise LauncherError("local_endpoint_unreachable") from exc


def _wait_for_http(
    url: str,
    process: subprocess.Popen[bytes],
    *,
    timeout_seconds: float = 30.0,
    require_runtime_ready: bool = False,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise LauncherError("owned_process_exited_during_startup")
        try:
            status, payload = _json_request("GET", url)
        except LauncherError:
            status, payload = 0, None
        if require_runtime_ready:
            # /health/ready keeps HTTP 200 for diagnostics, including startup failure.
            if status == 200 and isinstance(payload, dict):
                bootstrap = payload.get("runtime_bootstrap")
                stores = payload.get("runtime_stores")
                if isinstance(bootstrap, dict) and bootstrap.get("status") == "failed":
                    raise LauncherError("runtime_bootstrap_failed")
                if (
                    payload.get("status") in ("ready", "degraded")
                    and isinstance(bootstrap, dict)
                    and bootstrap.get("status") == "completed"
                    and isinstance(stores, dict)
                    and stores.get("ready") is True
                ):
                    return
        elif 200 <= status < 500:
            return
        time.sleep(0.2)
    raise LauncherError("local_endpoint_startup_timeout")


def _wait_for_run(api_url: str, run_id: str, *, timeout_seconds: float = 60.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status, payload = _json_request("GET", f"{api_url}/runs/{run_id}")
        if status == 200 and isinstance(payload, dict) and payload.get("status") in TERMINAL_RUN_STATES:
            return payload
        time.sleep(0.2)
    raise LauncherError("prototype_run_timeout")


def _require_ok(status: int, payload: Any, code: str) -> dict[str, Any]:
    if status != 200 or not isinstance(payload, dict):
        raise LauncherError(code)
    return payload


def _require_portable(payload: Any, *, data_label: str, code: str) -> None:
    if data_label.lower() in json.dumps(payload, ensure_ascii=False).lower():
        raise LauncherError(code)


def _start_prototype_run(api_url: str) -> dict[str, Any]:
    status, payload = _json_request("POST", f"{api_url}/prototype/runs", {})
    started = _require_ok(status, payload, "prototype_run_start_failed")
    run_id = str(started.get("run_id") or "")
    if not run_id:
        raise LauncherError("prototype_run_identifier_missing")
    completed = _wait_for_run(api_url, run_id)
    if completed.get("status") != "completed":
        raise LauncherError("prototype_run_failed")
    return completed


def _validate_multi_topic_workspace(api_url: str, *, data_label: str) -> dict[str, Any]:
    status, payload = _json_request("GET", f"{api_url}/prototype/workspace")
    workspace = _require_ok(status, payload, "prototype_workspace_status_failed")
    _require_portable(workspace, data_label=data_label, code="prototype_workspace_path_leak")
    topics = workspace.get("topics") if isinstance(workspace.get("topics"), list) else []
    kinds = {str(topic.get("topic_kind") or "") for topic in topics if isinstance(topic, dict)}
    if len(topics) != 3 or kinds != {"synthetic_demo", "real_open_access", "local_empty"}:
        raise LauncherError("prototype_workspace_initial_topics_invalid")
    synthetic = next(topic for topic in topics if topic.get("topic_kind") == "synthetic_demo")
    real = next(topic for topic in topics if topic.get("topic_kind") == "real_open_access")
    if not synthetic.get("active_snapshot_id") or real.get("active_snapshot_id") is not None:
        raise LauncherError("prototype_workspace_initial_snapshots_invalid")
    if int(workspace.get("provider_calls") or 0) != 0:
        raise LauncherError("prototype_workspace_provider_boundary_failed")
    return {"topic_count": len(topics), "provider_calls": 0}


def _validate_parent_journey(api_url: str, run: dict[str, Any], *, data_label: str) -> dict[str, Any]:
    run_id = str(run["run_id"])
    _require_portable(run, data_label=data_label, code="prototype_run_path_leak")
    prototype = run.get("prototype") if isinstance(run.get("prototype"), dict) else {}
    counts = run.get("counts") if isinstance(run.get("counts"), dict) else {}
    if prototype.get("report_status") != "ready":
        raise LauncherError("prototype_report_not_ready")
    if int(counts.get("reviewed_items") or 0) < 1 or int(counts.get("evidence_spans") or 0) < 2:
        raise LauncherError("prototype_review_evidence_incomplete")

    status, papers_payload = _json_request("GET", f"{api_url}/runs/{run_id}/papers")
    papers_payload = _require_ok(status, papers_payload, "prototype_paper_list_failed")
    _require_portable(papers_payload, data_label=data_label, code="prototype_paper_path_leak")
    papers = papers_payload.get("items") if isinstance(papers_payload.get("items"), list) else []
    if len(papers) < 3:
        raise LauncherError("prototype_corpus_incomplete")
    for paper in papers:
        paper_id = str(paper.get("paper_id") or "")
        review_status, review_payload = _json_request("GET", f"{api_url}/runs/{run_id}/papers/{paper_id}/review")
        review = _require_ok(review_status, review_payload, "prototype_review_failed")
        _require_portable(review, data_label=data_label, code="prototype_review_path_leak")
        if review.get("status") != "complete" or len(review.get("evidence") or []) < 2:
            raise LauncherError("prototype_review_evidence_incomplete")

    report_status, report_payload = _json_request("GET", f"{api_url}/runs/{run_id}/report")
    report = _require_ok(report_status, report_payload, "prototype_report_read_failed")
    _require_portable(report, data_label=data_label, code="prototype_report_path_leak")
    report_content = str(report.get("content") or "")
    if "Provider execution posture" not in report_content or "Live Provider invoked: no" not in report_content:
        raise LauncherError("prototype_provider_posture_missing")

    preview_status, preview_payload = _json_request("GET", f"{api_url}/runs/{run_id}/preview")
    preview = _require_ok(preview_status, preview_payload, "prototype_preview_failed")
    _require_portable(preview, data_label=data_label, code="prototype_preview_path_leak")
    preview_content = str(preview.get("content") or "")
    if not preview_content.strip():
        raise LauncherError("prototype_preview_empty")
    if "Provider execution posture" not in preview_content or "Live Provider invoked: no" not in preview_content:
        raise LauncherError("prototype_preview_report_mismatch")

    export_status, export_payload = _json_request(
        "POST", f"{api_url}/runs/{run_id}/export", {"format": "report_markdown"}
    )
    export_result = _require_ok(export_status, export_payload, "prototype_export_failed")
    _require_portable(export_result, data_label=data_label, code="prototype_export_path_leak")
    return {"run_id": run_id, "papers": papers, "evidence_spans": int(counts.get("evidence_spans") or 0)}


def _validate_child_rerun(api_url: str, parent: dict[str, Any]) -> dict[str, Any]:
    parent_id = str(parent["run_id"])
    selected = [str(item.get("paper_id") or "") for item in parent["papers"][:2]]
    status, payload = _json_request(
        "POST",
        f"{api_url}/runs/{parent_id}/rerun",
        {
            "mode": "branch_from_selected_papers",
            "selected_paper_ids": selected,
            "skip_retrieval_when_branching": True,
            "selection_source": "manual",
            "note": "Prototype launcher lineage smoke",
        },
    )
    rerun = _require_ok(status, payload, "prototype_rerun_start_failed")
    child_id = str(rerun.get("child_run_id") or "")
    child = _wait_for_run(api_url, child_id)
    lineage = child.get("lineage") if isinstance(child.get("lineage"), dict) else {}
    prototype = child.get("prototype") if isinstance(child.get("prototype"), dict) else {}
    if child.get("status") != "completed" or lineage.get("parent_run_id") != parent_id:
        raise LauncherError("prototype_rerun_failed")
    if prototype.get("report_status") != "ready":
        raise LauncherError("prototype_rerun_report_not_ready")
    return {"run_id": child_id, "parent_run_id": parent_id}


@contextlib.contextmanager
def _temporary_environment(values: dict[str, str]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in values}
    try:
        os.environ.update(values)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _start_owned_process(command: list[str], *, cwd: Path, env: dict[str, str], log_path: Path) -> subprocess.Popen[bytes]:
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    log_handle = log_path.open("wb")
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
    except Exception:
        log_handle.close()
        raise
    process._prototype_log_handle = log_handle  # type: ignore[attr-defined]
    return process


def _signal_owned_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        os.killpg(process.pid, signal.SIGINT)


def _close_process_log(process: subprocess.Popen[bytes]) -> None:
    handle = getattr(process, "_prototype_log_handle", None)
    if handle is not None:
        handle.close()


def _stop_owned_processes(processes: list[subprocess.Popen[bytes]], ports: list[int]) -> None:
    for process in reversed(processes):
        _signal_owned_process(process)
    deadline = time.monotonic() + 15.0
    for process in reversed(processes):
        remaining = max(0.1, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise LauncherError("owned_process_graceful_shutdown_timeout") from exc
        finally:
            _close_process_log(process)
    while time.monotonic() < deadline and any(_port_is_open(port) for port in ports):
        time.sleep(0.1)
    if any(_port_is_open(port) for port in ports):
        raise LauncherError("owned_listener_cleanup_failed")


def _remove_disposable_data(data_root: Path) -> None:
    resolved = data_root.resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if resolved.parent != temp_root or not resolved.name.startswith(DATA_PREFIX):
        raise LauncherError("disposable_data_cleanup_boundary_failed")
    shutil.rmtree(resolved)


def _runtime_environment(
    data_root: Path,
    backend_port: int,
    frontend_port: int,
    *,
    enable_live_provider_proof: bool,
    enable_real_corpus_proof: bool,
) -> dict[str, str]:
    return {
        "ALLOWED_ORIGINS": f"http://{HOST}:{frontend_port}",
        "AUTH_MODE": "disabled",
        "APP_BASE_URL": f"http://{HOST}:{backend_port}",
        "WEB_APP_BASE_PATH": "/app",
        "TOPIC_SCHEDULER_ENABLED": "1",
        "TOPIC_SCHEDULER_TICK_SECONDS": "1",
        "XRML_DATA_DIR": str(data_root),
        "XRML_ENVIRONMENT": "prototype",
        "XRML_PROVIDER": "disabled",
        "XRML_PROTOTYPE_MODE": "1",
        "XRML_PROTOTYPE_LIVING_SCHEDULER_ENABLED": "1",
        "XRML_PROTOTYPE_LIVE_PROVIDER_PROOF": "1" if enable_live_provider_proof else "0",
        "XRML_PROTOTYPE_PROVIDER_ENABLED": "0",
        "XRML_PROTOTYPE_REAL_CORPUS_NETWORK": "1" if enable_real_corpus_proof else "0",
        "XRML_PROTOTYPE_SESSION_ID": secrets.token_urlsafe(24),
        "XRML_RUN_EXECUTOR_POLL_SECONDS": "0.05",
        "VITE_API_BASE_URL": f"http://{HOST}:{backend_port}",
    }


def run(args: argparse.Namespace) -> int:
    _require_free_port(args.backend_port)
    _require_free_port(args.frontend_port)
    data_root = Path(tempfile.mkdtemp(prefix=DATA_PREFIX)).resolve()
    log_dir = data_root / "launcher-logs"
    log_dir.mkdir()
    processes: list[subprocess.Popen[bytes]] = []
    cleanup_error: LauncherError | None = None
    env_values = _runtime_environment(
        data_root,
        args.backend_port,
        args.frontend_port,
        enable_live_provider_proof=args.enable_live_provider_proof,
        enable_real_corpus_proof=args.enable_real_corpus_proof,
    )
    api_url = f"http://{HOST}:{args.backend_port}"
    web_url = f"http://{HOST}:{args.frontend_port}/app/"
    runtime_env = os.environ.copy()
    runtime_env.update(env_values)

    try:
        with _temporary_environment(env_values):
            backend = _start_owned_process(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "app.main:create_app",
                    "--factory",
                    "--host",
                    HOST,
                    "--port",
                    str(args.backend_port),
                ],
                cwd=REPO_ROOT,
                env=runtime_env,
                log_path=log_dir / "backend.log",
            )
            processes.append(backend)
            frontend = _start_owned_process(
                [
                    _npm_command(),
                    "run",
                    "dev",
                    "--",
                    "--host",
                    HOST,
                    "--port",
                    str(args.frontend_port),
                    "--strictPort",
                ],
                cwd=WEB_ROOT,
                env=runtime_env,
                log_path=log_dir / "frontend.log",
            )
            processes.append(frontend)

            _wait_for_http(f"{api_url}/health/ready", backend, require_runtime_ready=True)
            _wait_for_http(web_url, frontend)
            mode_status, mode_payload = _json_request("GET", f"{api_url}/prototype/status")
            mode = _require_ok(mode_status, mode_payload, "prototype_mode_status_failed")
            if not mode.get("enabled") or mode.get("isolation_status") != "isolated":
                raise LauncherError("prototype_mode_isolation_failed")
            live_proof = mode.get("live_provider_proof") if isinstance(mode.get("live_provider_proof"), dict) else {}
            real_status_code, real_payload = _json_request("GET", f"{api_url}/prototype/real-corpus/status")
            real_status = _require_ok(real_status_code, real_payload, "prototype_real_corpus_status_failed")
            if bool(real_status.get("network_enabled")) != bool(args.enable_real_corpus_proof):
                raise LauncherError("prototype_real_corpus_network_boundary_failed")

            workspace = _validate_multi_topic_workspace(api_url, data_label=data_root.name)
            parent_run = _start_prototype_run(api_url)
            parent = _validate_parent_journey(api_url, parent_run, data_label=data_root.name)
            print(PROTOTYPE_LABEL)
            print(f"Web: {web_url}")
            print(f"API: {api_url}")
            print(f"Data label: {data_root.name}")
            print(f"Topics: {workspace['topic_count']}; Provider calls: {workspace['provider_calls']}")
            print(f"Run: {parent['run_id']}")
            print(f"Papers: {len(parent['papers'])}; evidence spans: {parent['evidence_spans']}")
            print(
                "Provider route: deterministic local fallback; live proof availability: "
                + ("enabled" if live_proof.get("endpoint_available") else "disabled")
            )
            print(
                "Real corpus route: official NCBI only; network availability: "
                + ("enabled" if real_status.get("network_enabled") else "disabled")
            )

            if args.smoke:
                child = _validate_child_rerun(api_url, parent)
                print(f"Child run: {child['run_id']}; lineage: verified")
                print("Prototype smoke: PASS")
            else:
                print("Stop: Ctrl+C in this terminal")
                deadline = time.monotonic() + args.lifetime_seconds if args.lifetime_seconds > 0 else None
                while all(process.poll() is None for process in processes):
                    if deadline is not None and time.monotonic() >= deadline:
                        print("Prototype lifetime completed.")
                        break
                    time.sleep(0.5)
                else:
                    raise LauncherError("owned_process_exited_unexpectedly")
    except KeyboardInterrupt:
        print("Prototype stop requested.")
    finally:
        if processes:
            try:
                _stop_owned_processes(processes, [args.backend_port, args.frontend_port])
            except LauncherError as exc:
                cleanup_error = exc
        if args.cleanup_data and cleanup_error is None:
            _remove_disposable_data(data_root)
            print("Disposable data: removed")
        elif not args.cleanup_data:
            print(f"Disposable data: retained as {data_root.name}")
    if cleanup_error is not None:
        raise cleanup_error
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the isolated XRML Prototype v0.5 Living Topic stack.")
    parser.add_argument("--backend-port", type=int, default=8000)
    parser.add_argument("--frontend-port", type=int, default=5173)
    parser.add_argument("--smoke", action="store_true", help="Run the real-backend journey and exit.")
    parser.add_argument(
        "--enable-live-provider-proof",
        action="store_true",
        help="Expose the Prototype-only activation endpoint; this does not authorize or invoke a Provider.",
    )
    parser.add_argument(
        "--enable-real-corpus-proof",
        action="store_true",
        help="Enable only the fixed official-NCBI real-corpus UI path; disabled by default.",
    )
    parser.add_argument("--cleanup-data", action="store_true", help="Remove only this launcher's disposable temp root.")
    parser.add_argument("--lifetime-seconds", type=float, default=0, help="Stop normally after a bounded demo window.")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        return run(build_parser().parse_args(argv))
    except LauncherError as exc:
        print(f"Prototype launcher: FAILED ({exc.code})", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Prototype launcher: FAILED (unexpected_{type(exc).__name__.lower()})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
