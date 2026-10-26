"""One isolated source/standalone-sidecar observation under a fixed 35 s gate.

This does not run the Tauri host, qualify an installation or simulate an OS cold
start. Results keep the pre-Python interval separate from imports, factory and
lifespan. Raw child output and inherited application settings are never saved.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import urllib.request


DEADLINE_SECONDS = 35.0
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FATAL_CODES = {
    "configuration_failure", "port_bind_failure", "frontend_dist_missing",
    "app_startup_failure", "readiness_timeout", "unexpected_crash",
}
# The child vocabulary is imported before measuring. Its parent-process timer
# is not part of the reported child launch measurement.


def isolated_environment(output: Path) -> dict[str, str]:
    # Explicit OS/runtime essentials, not a denylist of credential names.
    keep = {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "SYSTEMDRIVE"}
    env = {key: value for key, value in os.environ.items() if key.upper() in keep}
    for directory in ("temp", "home", "cache", "logs"):
        (output / directory).mkdir()
    env.update({
        "TEMP": str(output / "temp"), "TMP": str(output / "temp"), "TMPDIR": str(output / "temp"),
        "HOME": str(output / "home"), "USERPROFILE": str(output / "home"),
        "APPDATA": str(output / "home"), "LOCALAPPDATA": str(output / "home"),
        "PYTHONPYCACHEPREFIX": str(output / "cache"), "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
        "XRML_DATA_DIR": str(output / "data"), "XRML_STARTUP_TRACE": "1", "AUTH_MODE": "disabled",
        "XRML_OPERATOR_AUDIT_PATH": str(output / "logs" / "operator-audit.jsonl"),
        "TRUST_PROXY_HEADERS": "0", "XRML_ENVIRONMENT": "test", "XRML_DEPLOYMENT_PROFILE": "single-service",
        "TOPIC_SCHEDULER_ENABLED": "0", "TOPIC_SCHEDULER_MODE": "disabled",
        "SCHEDULER_MODE": "single_node_file_backed", "XRML_RUN_EXECUTOR_MODE": "disabled",
        "XRML_PROVIDER": "disabled", "XRML_ALLOW_PROVIDER_RESEARCH_OUTPUT": "0",
        "XRML_PROTOTYPE_MODE": "0", "XRML_PROTOTYPE_PROVIDER_ENABLED": "0",
        "XRML_PROTOTYPE_LIVE_PROVIDER_PROOF": "0", "XRML_PROTOTYPE_REAL_CORPUS_NETWORK": "0",
        "XRML_PROTOTYPE_LIVING_SCHEDULER_ENABLED": "0", "XRML_JOURNAL_OFFLINE_ENABLED": "0",
        "XRML_REVIEW_OFFLINE_ENABLED": "0", "XRML_REVIEW_EXECUTION_OFFLINE_ENABLED": "0",
        "XRML_PAPER_TRANSPORT_OFFLINE_ENABLED": "0",
    })
    return env


def _read_pipe(stream, channel: str, messages: queue.Queue) -> None:
    for line in stream:
        received = time.perf_counter_ns()
        try:
            item = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(item, dict):
            messages.put((received, channel, item))


def _owns_pid(launcher_pid: int, child_pid: int) -> bool:
    if child_pid == launcher_pid:
        return True
    if os.name != "nt":
        return False

    # Windows venv python.exe is a redirector; its interpreter has another PID.
    # Verify ancestry from the OS instead of trusting a PID from child output.
    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return False
    parents = {}
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        available = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while available:
            parents[entry.th32ProcessID] = entry.th32ParentProcessID
            available = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    seen = set()
    while child_pid not in seen:
        seen.add(child_pid)
        child_pid = parents.get(child_pid, 0)
        if child_pid == launcher_pid:
            return True
        if child_pid == 0:
            break
    return False


def _kill_owned_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        taskkill = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "taskkill.exe"
        subprocess.run([str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                       creationflags=subprocess.CREATE_NO_WINDOW, check=False)
    else:
        process.kill()


def _confirmed_ready(item: dict, pid: int, remaining: float) -> bool:
    port = item.get("port")
    if (item.get("event") != "ready" or item.get("pid") != pid
            or item.get("host") != "127.0.0.1" or item.get("app_path") != "/app"
            or type(port) is not int or not 0 < port < 65536 or remaining <= 0):
        return False
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/health/ready", timeout=min(remaining, 1.0)) as response:
            if response.status != 200:
                return False
            data = json.loads(response.read())
        return (isinstance(data, dict) and data.get("status") in {"ready", "degraded"}
                and data.get("runtime_stores", {}).get("ready") is True
                and data.get("runtime_bootstrap", {}).get("status") == "completed")
    except (OSError, ValueError, AttributeError, TypeError):
        return False


def observe(*, output: Path, web_dist: Path, python: Path, sidecar: Path | None = None) -> dict:
    # Refuse overwrite and all source-tree output. This command creates its own
    # fresh runtime and never accepts a real data/profile directory to reopen.
    output = output.resolve()
    if output.is_relative_to(REPOSITORY_ROOT) or output.exists():
        raise ValueError("Output must be a new directory outside the source worktree.")
    if not (web_dist / "index.html").is_file():
        raise ValueError("Frontend distribution is missing.")
    output.mkdir(parents=True)
    env = isolated_environment(output)
    command = [str(sidecar)] if sidecar is not None else [str(python), "-m", "app.desktop.sidecar"]
    command.extend(["--data-dir", str(output / "data"), "--web-dist", str(web_dist.resolve())])

    # Import only the fixed phase vocabulary. The parent module's timer is never
    # used for the child measurement and importing it performs no writes.
    from app.desktop.startup_trace import PHASES

    records: list[dict] = []
    messages: queue.Queue = queue.Queue()
    started = time.perf_counter_ns()
    deadline = started + int(DEADLINE_SECONDS * 1_000_000_000)
    process = subprocess.Popen(command, cwd=REPOSITORY_ROOT, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                               errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    spawn_ms = (time.perf_counter_ns() - started) / 1_000_000
    readers = [threading.Thread(target=_read_pipe, args=(stream, channel, messages), daemon=True)
               for stream, channel in ((process.stdout, "stdout"), (process.stderr, "stderr"))]
    for reader in readers:
        reader.start()
    verdict = "deadline_missed"
    failure = None
    ready_ms = None
    cleanup = "graceful"
    trace_pid = None

    def retain(received: int, channel: str, item: dict) -> None:
        nonlocal trace_pid
        if (channel == "stderr" and item.get("event") == "startup_phase" and item.get("schema_version") == 1
                and type(item.get("pid")) is int and item["pid"] > 0 and isinstance(item.get("phase"), str)
                and item["phase"] in PHASES
                and type(item.get("monotonic_ns")) is int and started <= item["monotonic_ns"] <= received):
            if trace_pid is None and _owns_pid(process.pid, item["pid"]):
                trace_pid = item["pid"]
            if item["pid"] == trace_pid:
                records.append({"phase": item["phase"], "emitted_ms": (item["monotonic_ns"] - started) / 1_000_000,
                                "observed_ms": (received - started) / 1_000_000})

    try:
        while time.perf_counter_ns() < deadline:
            try:
                received, channel, item = messages.get(timeout=max(0, min((deadline - time.perf_counter_ns()) / 1e9, 0.1)))
            except queue.Empty:
                if process.poll() is not None:
                    verdict = "exited_before_ready"
                    break
                continue
            retain(received, channel, item)
            if channel == "stderr" and item.get("event") == "fatal":
                verdict = "startup_failed"
                failure = item.get("code") if item.get("code") in FATAL_CODES else "unexpected_crash"
                break
            remaining = (deadline - time.perf_counter_ns()) / 1e9
            child_pid = item.get("pid")
            if (channel == "stdout" and received < deadline
                    and type(child_pid) is int and child_pid > 0 and _owns_pid(process.pid, child_pid)
                    and _confirmed_ready(item, child_pid, remaining) and time.perf_counter_ns() < deadline):
                verdict = "ready_within_deadline"
                ready_ms = (time.perf_counter_ns() - started) / 1_000_000
                break
    finally:
        if process.poll() is None:
            try:
                assert process.stdin is not None
                process.stdin.write("shutdown\n")
                process.stdin.flush()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                cleanup = "terminated_owned_child_after_grace"
                try:
                    _kill_owned_tree(process)
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    cleanup = "owned_child_cleanup_failed"
        else:
            cleanup = "already_exited"
        for reader in readers:
            reader.join(timeout=2)
        while not messages.empty():
            retain(*messages.get_nowait())
        if any(reader.is_alive() for reader in readers):
            # Do not block closing a pipe held by a live descendant. Preserve
            # the missed deadline and cleanup evidence for the operator.
            cleanup = "owned_output_pipe_still_open"
        else:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
    records.sort(key=lambda item: item["emitted_ms"])
    result = {
        "schema_version": "xrml-startup-observation-v1",
        "mode": "standalone_packaged_sidecar" if sidecar is not None else "source_sidecar",
        "verdict": verdict, "failure_code": failure, "deadline_seconds": DEADLINE_SECONDS,
        "spawn_returned_ms": spawn_ms, "confirmed_ready_ms": ready_ms, "phases": records,
        "last_phase_within_deadline": next((row["phase"] for row in reversed(records)
                                           if row["emitted_ms"] < DEADLINE_SECONDS * 1000), None),
        "cleanup": cleanup, "exit_code": process.returncode,
        "owned_launcher_pid": process.pid, "phase_trace_observed": bool(records),
        "native_host_tested": False, "installed_startup_qualified": False, "cold_os_start": False,
        "data_condition": "fresh_disposable", "python_bytecode_cache": "fresh_disposable",
        "scheduler_enabled": False, "provider_enabled": False,
        "limits": ["Time before python_entry includes spawn, loader, interpreter and packaged runtime hooks; not a unique cause.",
                   "Lifespan includes bootstrap and worker setup; no SQLite-only attribution.",
                   "Concurrent host load and OS file caches are uncontrolled."],
    }
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--web-dist", type=Path, default=REPOSITORY_ROOT / "web" / "dist")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--sidecar", type=Path, help="Optional already-built standalone sidecar executable")
    args = parser.parse_args()
    try:
        result = observe(output=args.output, web_dist=args.web_dist, python=args.python, sidecar=args.sidecar)
    except (OSError, ValueError):
        print(json.dumps({"event": "diagnostic_failed", "reason": "invalid_setup_or_io"}))
        return 2
    print(json.dumps(result))
    return 0 if result["verdict"] == "ready_within_deadline" and result["cleanup"] == "graceful" else 1


if __name__ == "__main__":
    if not __package__:
        sys.path.insert(0, str(REPOSITORY_ROOT))
    raise SystemExit(main())
