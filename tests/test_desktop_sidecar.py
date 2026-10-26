from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.desktop.sidecar import (
    LOOPBACK_HOST,
    SidecarConfig,
    SidecarFailure,
    SidecarFailureCode,
    _readiness_is_usable,
    bind_ephemeral_socket,
    build_ready_event,
    create_desktop_app,
    parse_args,
    redact_lifecycle_detail,
    runtime_source_root,
)
from scripts.build_desktop_sidecar import deterministic_inputs, pyinstaller_command


def _write_frontend_dist(root: Path) -> Path:
    dist = root / "web-dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text("<main>XRML desktop shell</main>", encoding="utf-8")
    (assets / "app.js").write_text("window.XRML_DESKTOP = true;", encoding="utf-8")
    return dist


def test_sidecar_config_is_loopback_ephemeral_and_runtime_isolated(tmp_path: Path) -> None:
    dist = _write_frontend_dist(tmp_path)
    data_dir = tmp_path / "runtime-data"

    config = SidecarConfig(web_dist=dist, data_dir=data_dir)

    assert config.host == LOOPBACK_HOST == "127.0.0.1"
    assert config.port == 0
    assert config.web_dist == dist.resolve()
    assert config.data_dir == data_dir.resolve()


@pytest.mark.parametrize("cli_value,environment_value", [(None, None), (None, ""), (None, "  "), ("", None), ("  ", None)])
def test_cli_requires_explicit_data_root(tmp_path: Path, monkeypatch, cli_value, environment_value) -> None:
    dist = _write_frontend_dist(tmp_path)
    monkeypatch.chdir(tmp_path)
    if environment_value is None:
        monkeypatch.delenv("XRML_DATA_DIR", raising=False)
    else:
        monkeypatch.setenv("XRML_DATA_DIR", environment_value)
    arguments = ["--web-dist", str(dist)]
    if cli_value is not None:
        arguments.extend(["--data-dir", cli_value])
    with pytest.raises(SidecarFailure, match="is required") as error:
        parse_args(arguments)
    assert error.value.code is SidecarFailureCode.CONFIGURATION_FAILURE


def test_cli_data_root_overrides_environment(tmp_path: Path, monkeypatch) -> None:
    dist = _write_frontend_dist(tmp_path)
    environment_root = tmp_path / "environment data"
    argument_root = tmp_path / "argument data"
    monkeypatch.setenv("XRML_DATA_DIR", str(environment_root))
    assert parse_args(["--web-dist", str(dist)]).data_dir == environment_root.resolve()
    assert parse_args(["--web-dist", str(dist), "--data-dir", str(argument_root)]).data_dir == argument_root.resolve()


def test_sidecar_rejects_non_loopback_and_source_runtime_paths(tmp_path: Path) -> None:
    dist = _write_frontend_dist(tmp_path)
    source_root = Path(__file__).resolve().parents[1]

    with pytest.raises(SidecarFailure) as host_error:
        SidecarConfig(web_dist=dist, data_dir=tmp_path / "runtime", host="0.0.0.0")
    assert host_error.value.code is SidecarFailureCode.CONFIGURATION_FAILURE

    with pytest.raises(SidecarFailure) as path_error:
        SidecarConfig(web_dist=dist, data_dir=source_root / "runtime-data")
    assert path_error.value.code is SidecarFailureCode.CONFIGURATION_FAILURE


def test_frozen_source_containment_uses_extraction_root_not_build_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extraction_root = tmp_path / "extract" / "_MEI-safe"
    extraction_root.mkdir(parents=True)
    build_runtime = tmp_path / "build-root" / "runtime-data"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(extraction_root), raising=False)

    assert runtime_source_root() == extraction_root.resolve()

    dist = _write_frontend_dist(tmp_path)
    config = SidecarConfig(web_dist=dist, data_dir=build_runtime)
    assert config.data_dir == build_runtime.resolve()
    with pytest.raises(SidecarFailure) as extraction_error:
        SidecarConfig(web_dist=dist, data_dir=extraction_root / "runtime-data")
    assert extraction_error.value.code is SidecarFailureCode.CONFIGURATION_FAILURE


def test_sidecar_build_inventory_and_command_include_runtime_config(tmp_path: Path) -> None:
    repository_root = tmp_path / "repo"
    (repository_root / "app" / "desktop").mkdir(parents=True)
    (repository_root / "app" / "desktop" / "sidecar.py").write_text("pass\n", encoding="utf-8")
    (repository_root / "web" / "dist").mkdir(parents=True)
    (repository_root / "web" / "dist" / "index.html").write_text("<main/>\n", encoding="utf-8")
    (repository_root / "config").mkdir()
    (repository_root / "config" / "sources.json").write_text("{}\n", encoding="utf-8")
    (repository_root / "config" / "keywords.json").write_text("{}\n", encoding="utf-8")
    (repository_root / "config" / "themes.json").write_text("{}\n", encoding="utf-8")
    (repository_root / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    (repository_root / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    inventory = deterministic_inputs(repository_root)
    paths = [item["path"] for item in inventory]
    command = pyinstaller_command(repository_root, tmp_path / "output")

    assert "config/sources.json" in paths
    assert "config/keywords.json" in paths
    assert "config/themes.json" in paths
    assert f"{repository_root / 'config'};config" in command
    assert f"{repository_root / 'web' / 'dist'};web/dist" in command


def test_frontend_dist_failure_is_typed(tmp_path: Path) -> None:
    with pytest.raises(SidecarFailure) as error:
        SidecarConfig(web_dist=tmp_path / "missing-dist", data_dir=tmp_path / "runtime-data")

    assert error.value.code is SidecarFailureCode.FRONTEND_DIST_MISSING


def test_ephemeral_socket_and_ready_event_use_actual_port() -> None:
    listener = bind_ephemeral_socket()
    try:
        host, port = listener.getsockname()
        event = build_ready_event(port=port, pid=1234)
    finally:
        listener.close()

    assert host == LOOPBACK_HOST
    assert port > 0
    assert event == {
        "event": "ready",
        "host": LOOPBACK_HOST,
        "port": port,
        "app_path": "/app",
        "pid": 1234,
    }


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"status": status, "runtime_stores": {"ready": True}, "runtime_bootstrap": {"status": "completed"}}, True)
        for status in ("ready", "degraded")
    ] + [
        ({"status": "ready"}, False),
        ({"status": "ready", "runtime_stores": {"ready": True}}, False),
        ({"status": "ready", "runtime_stores": {"ready": False}, "runtime_bootstrap": {"status": "completed"}}, False),
        ({"status": "ready", "runtime_stores": {"ready": "true"}, "runtime_bootstrap": {"status": "completed"}}, False),
        ({"status": "ready", "runtime_stores": {"ready": True}, "runtime_bootstrap": {"status": "failed"}}, False),
        ({"status": "ready", "runtime_stores": {"ready": True}, "runtime_bootstrap": {"status": "not_observed"}}, False),
        ({"status": "not_ready", "runtime_stores": {"ready": True}, "runtime_bootstrap": {"status": "completed"}}, False),
        ({"status": [], "runtime_stores": [], "runtime_bootstrap": []}, False),
        ([], False),
        (None, False),
    ],
)
def test_readiness_requires_confirmed_bootstrap_and_usable_stores(
    monkeypatch: pytest.MonkeyPatch, payload: object, expected: bool,
) -> None:
    from io import BytesIO
    from types import SimpleNamespace

    from app.desktop import sidecar

    class Response(BytesIO):
        status = 200

    monkeypatch.setattr(
        sidecar.urllib.request, "build_opener",
        lambda *handlers: SimpleNamespace(open=lambda *args, **kwargs: Response(json.dumps(payload).encode("utf-8"))),
    )
    assert _readiness_is_usable(LOOPBACK_HOST, 12345) is expected


def test_packaged_path_simulation_serves_health_ready_and_spa(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dist = _write_frontend_dist(tmp_path)
    data_dir = tmp_path / "runtime-data"
    data_dir.mkdir()
    monkeypatch.setenv("XRML_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AUTH_MODE", "disabled")
    monkeypatch.setenv("WEB_APP_BASE_PATH", "/app")

    app = create_desktop_app(dist)
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        ready = client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json()["status"] in {"ready", "degraded"}
        assert ready.json()["runtime_bootstrap"] == {"status": "completed"}
        assert ready.json()["runtime_stores"]["ready"] is True
        assert client.get("/app").text == "<main>XRML desktop shell</main>"
        assert client.get("/app/topics/test/dashboard").text == "<main>XRML desktop shell</main>"
        assert client.get("/app/assets/app.js").status_code == 200

    assert data_dir.exists()
    assert data_dir.resolve().is_relative_to(tmp_path.resolve())


def test_factory_import_skips_default_app_and_asgi_entrypoint_remains_singleton(tmp_path: Path) -> None:
    dist = _write_frontend_dist(tmp_path)
    # A fresh interpreter is required: conftest imports the factory itself.
    script = textwrap.dedent("""
        import sys
        import time
        from concurrent.futures import ThreadPoolExecutor
        from pathlib import Path
        from threading import Barrier
        from unittest.mock import patch

        started = time.monotonic()
        def phase(name):
            print(f'factory_test {name}: {time.monotonic() - started:.3f}s', file=sys.stderr, flush=True)

        phase('before_framework_imports')
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from uvicorn.importer import import_from_string
        phase('framework_imports_completed')

        created = []
        original_init = FastAPI.__init__

        def record_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            created.append(self)

        with patch.object(FastAPI, '__init__', record_init):
            phase('before_app_import')
            from app.main import create_app
            from app import main
            phase('app_import_completed')
            assert callable(create_app)
            assert created == [], 'Importing the factory constructed an unused default app'

            from app.desktop.sidecar import create_desktop_app
            default_web_dist = main.WEB_DIST_ROOT
            desktop = create_desktop_app(Path(sys.argv[1]))
            phase('desktop_factory_completed')
            assert created == [desktop]
            assert main.WEB_DIST_ROOT == default_web_dist
            with TestClient(desktop) as client:
                phase('desktop_lifespan_started')
                assert client.get('/health').json() == {'status': 'ok'}
                assert client.get('/app').text == '<main>XRML desktop shell</main>'
            phase('desktop_lifespan_stopped')

            gate = Barrier(4)

            def load_default(_):
                gate.wait(timeout=10)
                return import_from_string('app.main:app')

            with ThreadPoolExecutor(max_workers=4) as pool:
                defaults = list(pool.map(load_default, range(4)))
            phase('concurrent_default_factory_completed')
            from app.main import app
            assert all(item is app for item in defaults)
            assert app is not desktop
            assert created == [desktop, app]
            assert not hasattr(main, 'unknown_entrypoint')
            with TestClient(app) as client:
                phase('default_lifespan_started')
                assert client.get('/health').json() == {'status': 'ok'}
                assert client.get('/app').text != '<main>XRML desktop shell</main>'
            phase('default_lifespan_stopped')
    """)
    env = os.environ.copy()
    env.update({
        "AUTH_MODE": "disabled",
        "TOPIC_SCHEDULER_ENABLED": "0",
        "XRML_RUN_EXECUTOR_MODE": "disabled",
        "XRML_DATA_DIR": str(tmp_path / "factory-data"),
        "WEB_APP_BASE_PATH": "/app",
    })
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, str(dist)],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"Factory subprocess exceeded 60s; stdout={exc.stdout!r}; stderr={exc.stderr!r}")
    print(result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr


def test_lifecycle_diagnostics_are_secret_safe() -> None:
    detail = "token=secret cookie=session-value D:\\private\\owner\\runtime"

    redacted = redact_lifecycle_detail(detail)

    assert "secret" not in redacted
    assert "session-value" not in redacted
    assert "D:\\private" not in redacted
    assert redacted == "[redacted]"


@pytest.mark.parametrize("existing_store,failure_delay", [(False, 0), (True, 0), (True, 1.1)])
def test_failed_bootstrap_never_announces_ready(
    tmp_path: Path, existing_store: bool, failure_delay: float,
) -> None:
    dist = _write_frontend_dist(tmp_path)
    data_dir = tmp_path / "runtime-data"
    script = textwrap.dedent("""
        import hashlib
        import sys
        import time
        from pathlib import Path
        from app import main as application
        from app.db.bootstrap import bootstrap_runtime_stores
        from app.desktop import sidecar

        data = Path(sys.argv[2])
        if sys.argv[3] == 'existing':
            bootstrap_runtime_stores()
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in data.glob('*.sqlite3')}
        starts = []
        for builder in ('build_topic_schedule_worker', 'build_run_executor_worker'):
            original = getattr(application, builder)
            def observed_builder(original=original):
                worker = original()
                worker.start = lambda: starts.append('started')
                return worker
            setattr(application, builder, observed_builder)

        def failed_bootstrap():
            time.sleep(float(sys.argv[4]))
            raise PermissionError('secret-token at D:/private/research.sqlite3')
        application.bootstrap_runtime_stores = failed_bootstrap
        result = sidecar.main(['--web-dist', sys.argv[1], '--data-dir', str(data),
                               '--startup-timeout-seconds', '1'])
        assert not starts, 'Failed bootstrap must not start workers'
        after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in data.glob('*.sqlite3')}
        assert before == after, 'Startup failure changed existing database bytes'
        raise SystemExit(result)
    """)
    env = os.environ.copy()
    env.update({
        "AUTH_MODE": "disabled", "XRML_DATA_DIR": str(data_dir),
        "TOPIC_SCHEDULER_ENABLED": "0", "TOPIC_SCHEDULER_MODE": "disabled",
        "XRML_RUN_EXECUTOR_MODE": "disabled", "XRML_PROVIDER": "disabled",
        "XRML_JOURNAL_OFFLINE_ENABLED": "0", "XRML_REVIEW_OFFLINE_ENABLED": "0",
        "XRML_REVIEW_EXECUTION_OFFLINE_ENABLED": "0", "XRML_PAPER_TRANSPORT_OFFLINE_ENABLED": "0",
    })
    result = subprocess.run(
        [sys.executable, "-c", script, str(dist), str(data_dir), "existing" if existing_store else "new", str(failure_delay)],
        cwd=Path(__file__).resolve().parents[1], env=env, input="shutdown\n",
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert not result.stdout.strip(), "Failed initialization must not emit ready"
    fatal = [json.loads(line) for line in result.stderr.splitlines() if line.startswith('{')
             and json.loads(line).get("event") == "fatal"]
    assert len(fatal) == 1, result.stderr
    assert fatal[0]["code"] == "app_startup_failure", result.stderr
    assert fatal[0]["detail"] == "[redacted]"
    assert "secret-token" not in result.stderr and "D:/private" not in result.stderr

    # A fresh process reuses the same root without an error injection or reset.
    recovered = subprocess.run(
        [sys.executable, "-m", "app.desktop.sidecar", "--web-dist", str(dist), "--data-dir", str(data_dir)],
        cwd=Path(__file__).resolve().parents[1], env=env, input="shutdown\n",
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    events = [json.loads(line) for line in recovered.stdout.splitlines() if line.strip()]
    assert [event["event"] for event in events] == ["ready", "shutdown"]
    assert events[-1]["reason"] == "host_requested"


def test_sidecar_process_ready_health_and_graceful_shutdown(tmp_path: Path) -> None:
    dist = _write_frontend_dist(tmp_path)
    data_dir = tmp_path / "runtime-data"
    env = os.environ.copy()
    env.update(
        {
            "AUTH_MODE": "disabled",
            "WEB_APP_BASE_PATH": "/app",
            "XRML_DATA_DIR": str(data_dir),
            "PYTHONPYCACHEPREFIX": str(tmp_path / "pycache"),
            "XRML_STARTUP_TRACE": "1",
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "app.desktop.sidecar",
            "--web-dist",
            str(dist),
            "--data-dir",
            str(data_dir),
            "--startup-timeout-seconds",
            "20",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert process.stdout is not None
        event = json.loads(process.stdout.readline())
        assert event["event"] == "ready"
        assert event["host"] == LOOPBACK_HOST
        assert event["port"] > 0
        response = httpx.get(f"http://{LOOPBACK_HOST}:{event['port']}/health", timeout=5)
        assert response.json() == {"status": "ok"}

        assert process.stdin is not None
        process.stdin.write("shutdown\n")
        process.stdin.flush()
        assert process.wait(timeout=15) == 0
        remaining = [json.loads(line) for line in process.stdout.read().splitlines() if line.strip()]
        assert any(item.get("event") == "shutdown" and item.get("reason") == "host_requested" for item in remaining)
        assert process.stderr is not None
        diagnostics = [json.loads(line) for line in process.stderr.read().splitlines() if line.startswith('{')]
        phases = [item for item in diagnostics if item.get("event") == "startup_phase"]
        names = [item["phase"] for item in phases]
        assert names[0] == "python_entry" and names[-1] == "readiness_confirmed"
        assert names.index("listener_bound") < names.index("application_import_started")
        assert names.index("application_import_complete") < names.index("application_factory_started")
        assert names.index("lifespan_started") < names.index("lifespan_enter_complete") < names.index("server_started")
        assert all(item["pid"] == event["pid"] for item in phases)
        assert [item["monotonic_ns"] for item in phases] == sorted(item["monotonic_ns"] for item in phases)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
