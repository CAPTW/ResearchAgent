"""Startup instrumentation must stay passive, early and free of private values."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import queue
import sys
import textwrap
import threading

import pytest

from app.desktop import startup_trace
from scripts import diagnose_desktop_startup as diagnostic


@pytest.mark.parametrize("enabled", [False, True])
def test_help_does_not_load_application_or_server_dependencies(enabled: bool, tmp_path: Path) -> None:
    script = textwrap.dedent("""
        import builtins, runpy, sys
        original_import = builtins.__import__
        def no_server_import(name, *args, **kwargs):
            if name.split('.')[0] in {'fastapi', 'uvicorn'} or name in {'app.main', 'app.local'}:
                raise AssertionError('Help must not import server or application code')
            return original_import(name, *args, **kwargs)
        builtins.__import__ = no_server_import
        sys.argv = ['sidecar', '--help']
        try:
            runpy.run_module('app.desktop.sidecar', run_name='__main__')
        except SystemExit as exit:
            assert exit.code == 0
    """)
    env = {**os.environ, "XRML_STARTUP_TRACE": "1" if enabled else "0", "XRML_DATA_DIR": str(tmp_path / "data")}
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "XRML packaged FastAPI desktop sidecar" in result.stdout
    assert not (tmp_path / "data").exists()
    if enabled:
        events = [json.loads(line) for line in result.stderr.splitlines()]
        assert [row["phase"] for row in events] == ["python_entry", "sidecar_imports_complete", "configuration_started"]
        assert all(set(row) == {"event", "schema_version", "phase", "monotonic_ns", "elapsed_ms", "pid"} for row in events)
        assert [row["monotonic_ns"] for row in events] == sorted(row["monotonic_ns"] for row in events)
    else:
        assert result.stderr == ""


def test_trace_rejects_untrusted_values_and_cannot_break_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(startup_trace, "_ENABLED", True)
    captured = io.StringIO()
    monkeypatch.setattr(startup_trace.sys, "stderr", captured)
    startup_trace.startup_phase('D:/private/key secret-token')
    startup_trace.startup_phase("listener_bound")
    assert json.loads(captured.getvalue())["phase"] == "listener_bound"
    assert "secret" not in captured.getvalue() and "private" not in captured.getvalue()
    captured.close()
    startup_trace.startup_phase("listener_bound")
    monkeypatch.setattr(startup_trace.sys, "stderr", None)
    startup_trace.startup_phase("listener_bound")


def test_diagnostic_only_inherits_os_runtime_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_UNLISTED_PROVIDER_KEY", "synthetic-sensitive-value")
    monkeypatch.setenv("XRML_PROVIDER", "live")
    monkeypatch.setenv("TOPIC_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("PYTHONPATH", "private-runtime")
    env = diagnostic.isolated_environment(tmp_path)
    assert "synthetic-sensitive-value" not in json.dumps(env)
    assert "SECRET_UNLISTED_PROVIDER_KEY" not in env and "PYTHONPATH" not in env
    assert env["XRML_PROVIDER"] == "disabled" and env["TOPIC_SCHEDULER_ENABLED"] == "0"
    assert Path(env["HOME"]).is_relative_to(tmp_path)
    assert Path(env["XRML_OPERATOR_AUDIT_PATH"]).is_relative_to(tmp_path)
    assert not Path(env["XRML_DATA_DIR"]).exists()


def test_diagnostic_refuses_existing_or_source_runtime(tmp_path: Path) -> None:
    for output in (tmp_path, diagnostic.REPOSITORY_ROOT / "new-diagnostic-data"):
        with pytest.raises(ValueError, match="new directory outside"):
            diagnostic.observe(output=output, web_dist=tmp_path, python=Path(sys.executable))


def test_diagnostic_recognizes_windows_venv_child_without_owning_its_parent() -> None:
    process = subprocess.Popen([sys.executable, "-c", "import os,sys; print(os.getpid(),flush=True); sys.stdin.readline()"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    child_pids: queue.Queue = queue.Queue()
    reader = threading.Thread(target=lambda: child_pids.put(process.stdout.readline()), daemon=True)
    reader.start()
    try:
        child_pid = int(child_pids.get(timeout=15))
        assert diagnostic._owns_pid(process.pid, child_pid)
        assert not diagnostic._owns_pid(process.pid, os.getpid())
        process.communicate(input="close\n", timeout=10)
    finally:
        diagnostic._kill_owned_tree(process)
        process.wait(timeout=5)
        reader.join(timeout=2)


def test_diagnostic_deadline_miss_stays_failed_after_late_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bounded synthetic child reaches readiness only during cleanup."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("synthetic", encoding="utf-8")
    original_popen = diagnostic.subprocess.Popen
    script = textwrap.dedent("""
        import json, os, sys, time
        time.sleep(0.3)
        print(json.dumps({'event': 'ready', 'host': '127.0.0.1', 'port': 1, 'app_path': '/app', 'pid': os.getpid()}), flush=True)
        for line in sys.stdin:
            if line.strip() == 'shutdown':
                break
    """)

    def synthetic_popen(_command, **kwargs):
        return original_popen([sys.executable, "-c", script], **kwargs)

    monkeypatch.setattr(diagnostic.subprocess, "Popen", synthetic_popen)
    # Test harness only; shipped CLI has no deadline override.
    monkeypatch.setattr(diagnostic, "DEADLINE_SECONDS", 0.1)
    result = diagnostic.observe(output=tmp_path / "run", web_dist=dist, python=Path(sys.executable))
    assert result["verdict"] == "deadline_missed"
    assert result["confirmed_ready_ms"] is None
    assert result["cleanup"] == "graceful" and result["exit_code"] == 0
    assert result["installed_startup_qualified"] is False and result["native_host_tested"] is False
    assert json.loads((tmp_path / "run/result.json").read_text()) == result
