from __future__ import annotations

from app.desktop.startup_trace import startup_phase

startup_phase("python_entry")

import argparse
import asyncio
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from typing import IO, TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    import uvicorn
    from fastapi import FastAPI

startup_phase("sidecar_imports_complete")

LOOPBACK_HOST = "127.0.0.1"
APP_PATH = "/app"
SOURCE_ROOT = Path(__file__).resolve().parents[2]


class SidecarFailureCode(str, Enum):
    CONFIGURATION_FAILURE = "configuration_failure"
    PORT_BIND_FAILURE = "port_bind_failure"
    FRONTEND_DIST_MISSING = "frontend_dist_missing"
    APP_STARTUP_FAILURE = "app_startup_failure"
    READINESS_TIMEOUT = "readiness_timeout"
    UNEXPECTED_CRASH = "unexpected_crash"


class SidecarFailure(RuntimeError):
    def __init__(self, code: SidecarFailureCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def runtime_source_root() -> Path:
    """Return the source boundary without mistaking a frozen executable tree for source."""
    frozen_bundle_root = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and frozen_bundle_root:
        return _resolved(Path(frozen_bundle_root))
    return _resolved(SOURCE_ROOT)


def default_frontend_dist() -> Path:
    bundle_root = Path(getattr(sys, "_MEIPASS", SOURCE_ROOT))
    return _resolved(bundle_root / "web" / "dist")


@dataclass(frozen=True)
class SidecarConfig:
    web_dist: Path
    data_dir: Path
    host: str = LOOPBACK_HOST
    port: int = 0
    startup_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        web_dist = _resolved(Path(self.web_dist))
        data_dir = _resolved(Path(self.data_dir))
        object.__setattr__(self, "web_dist", web_dist)
        object.__setattr__(self, "data_dir", data_dir)

        if self.host != LOOPBACK_HOST or self.port != 0:
            raise SidecarFailure(
                SidecarFailureCode.CONFIGURATION_FAILURE,
                "The desktop sidecar must use loopback and an OS-assigned port.",
            )
        if not 1.0 <= self.startup_timeout_seconds <= 120.0:
            raise SidecarFailure(
                SidecarFailureCode.CONFIGURATION_FAILURE,
                "The readiness timeout must be between 1 and 120 seconds.",
            )
        if _is_relative_to(data_dir, runtime_source_root()):
            raise SidecarFailure(
                SidecarFailureCode.CONFIGURATION_FAILURE,
                "Desktop runtime data must stay outside the source worktree.",
            )
        if not web_dist.is_dir() or not (web_dist / "index.html").is_file():
            raise SidecarFailure(
                SidecarFailureCode.FRONTEND_DIST_MISSING,
                "The packaged frontend distribution is unavailable.",
            )


def bind_ephemeral_socket(host: str = LOOPBACK_HOST) -> socket.socket:
    if host != LOOPBACK_HOST:
        raise SidecarFailure(
            SidecarFailureCode.CONFIGURATION_FAILURE,
            "Only the IPv4 loopback host is permitted.",
        )
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind((host, 0))
        listener.listen(2048)
        listener.set_inheritable(False)
        return listener
    except OSError as exc:
        listener.close()
        raise SidecarFailure(
            SidecarFailureCode.PORT_BIND_FAILURE,
            "The loopback listener could not be created.",
        ) from exc


def build_ready_event(*, port: int, pid: int | None = None) -> dict[str, object]:
    return {
        "event": "ready",
        "host": LOOPBACK_HOST,
        "port": port,
        "app_path": APP_PATH,
        "pid": os.getpid() if pid is None else pid,
    }


def redact_lifecycle_detail(detail: str) -> str:
    return "[redacted]" if detail else ""


def emit_lifecycle_event(payload: dict[str, object], stream: IO[str] | None = None) -> None:
    target = stream or sys.stdout
    target.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")
    target.flush()


def _configure_runtime(config: SidecarConfig, port: int) -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["XRML_DATA_DIR"] = str(config.data_dir)
    os.environ.setdefault("AUTH_MODE", "disabled")
    os.environ["APP_BASE_URL"] = f"http://{config.host}:{port}"
    os.environ["WEB_APP_BASE_PATH"] = APP_PATH
    os.environ.setdefault("TOPIC_SCHEDULER_MODE", "embedded")
    os.environ.setdefault("TOPIC_SCHEDULER_ENABLED", "true")
    os.environ.setdefault("SCHEDULER_MODE", "single_node_file_backed")
    os.environ.setdefault("XRML_ENVIRONMENT", "local")
    os.environ.setdefault("XRML_DEPLOYMENT_PROFILE", "single-service")


def create_desktop_app(web_dist: Path) -> FastAPI:
    startup_phase("application_import_started")
    from app.local import create_local_app
    # create_local_app imports main inside its factory. Include that import in
    # the import interval so it is not mistaken for application construction.
    from app.main import create_app as _create_app  # noqa: F401

    startup_phase("application_import_complete")
    startup_phase("application_factory_started")
    application = create_local_app(web_dist=_resolved(web_dist))
    startup_phase("application_factory_complete")
    return application


def _readiness_is_usable(host: str, port: int) -> bool:
    try:
        # This is our own listener: a user/system proxy must neither veto a
        # healthy sidecar nor substitute another service's readiness response.
        # Keep this opener local so research transports retain their settings.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://{host}:{port}/health/ready", timeout=1.0) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                return False
            stores = payload.get("runtime_stores")
            bootstrap = payload.get("runtime_bootstrap")
            return (
                payload.get("status") in ("ready", "degraded")
                and isinstance(stores, dict)
                and stores.get("ready") is True
                and isinstance(bootstrap, dict)
                and bootstrap.get("status") == "completed"
            )
    except (OSError, ValueError, urllib.error.URLError):
        return False


def _watch_host_input(server: uvicorn.Server, reason: dict[str, str]) -> None:
    try:
        for line in sys.stdin:
            if line.strip().lower() == "shutdown":
                reason["value"] = "host_requested"
                server.should_exit = True
                return
    except (OSError, ValueError):
        return


async def _serve(config: SidecarConfig, listener: socket.socket) -> int:
    port = int(listener.getsockname()[1])
    _configure_runtime(config, port)
    startup_phase("runtime_configured")
    try:
        application = create_desktop_app(config.web_dist)
    except Exception as exc:
        raise SidecarFailure(
            SidecarFailureCode.APP_STARTUP_FAILURE,
            "The FastAPI application could not be constructed.",
        ) from exc

    startup_phase("server_import_started")
    import uvicorn
    startup_phase("server_import_complete")

    original_lifespan = application.router.lifespan_context

    @asynccontextmanager
    async def traced_lifespan(app: FastAPI):
        startup_phase("lifespan_started")
        entered = False
        try:
            async with original_lifespan(app) as state:
                # Entry completion is not a successful-bootstrap assertion:
                # the existing readiness checks below retain that authority.
                startup_phase("lifespan_enter_complete")
                entered = True
                yield state
        except Exception:
            if not entered:
                startup_phase("lifespan_failed")
            raise

    application.router.lifespan_context = traced_lifespan
    server = uvicorn.Server(
        uvicorn.Config(
            application,
            host=config.host,
            port=port,
            access_log=False,
            log_level="warning",
            lifespan="on",
        )
    )
    startup_phase("server_starting")
    serve_task = asyncio.create_task(server.serve(sockets=[listener]))
    deadline = time.monotonic() + config.startup_timeout_seconds
    started_observed = False
    while time.monotonic() < deadline:
        if serve_task.done():
            try:
                await serve_task
            except Exception as exc:
                raise SidecarFailure(
                    SidecarFailureCode.APP_STARTUP_FAILURE,
                    "The FastAPI application failed during startup.",
                ) from exc
            raise SidecarFailure(
                SidecarFailureCode.APP_STARTUP_FAILURE,
                "The FastAPI application stopped before readiness.",
            )
        if server.started:
            if not started_observed:
                startup_phase("server_started")
                started_observed = True
            if getattr(application.state, "runtime_bootstrap_ok", None) is False:
                server.should_exit = True
                await serve_task
                raise SidecarFailure(
                    SidecarFailureCode.APP_STARTUP_FAILURE,
                    "The local runtime stores could not be initialized.",
                )
            if await asyncio.to_thread(_readiness_is_usable, config.host, port):
                break
        await asyncio.sleep(0.05)
    else:
        server.should_exit = True
        await serve_task
        # Blocking bootstrap can finish after the polling deadline. Preserve its
        # confirmed failure instead of replacing the cause with a timeout.
        if getattr(application.state, "runtime_bootstrap_ok", None) is False:
            raise SidecarFailure(
                SidecarFailureCode.APP_STARTUP_FAILURE,
                "The local runtime stores could not be initialized.",
            )
        raise SidecarFailure(
            SidecarFailureCode.READINESS_TIMEOUT,
            "The desktop sidecar did not become ready in time.",
        )

    startup_phase("readiness_confirmed")
    emit_lifecycle_event(build_ready_event(port=port))
    shutdown_reason = {"value": "normal"}
    threading.Thread(
        target=_watch_host_input,
        args=(server, shutdown_reason),
        name="xrml-desktop-host-input",
        daemon=True,
    ).start()
    try:
        await serve_task
    except Exception as exc:
        raise SidecarFailure(
            SidecarFailureCode.UNEXPECTED_CRASH,
            "The desktop sidecar stopped unexpectedly.",
        ) from exc
    emit_lifecycle_event({"event": "shutdown", "reason": shutdown_reason["value"], "pid": os.getpid()})
    return 0


def run_sidecar(config: SidecarConfig) -> int:
    startup_phase("listener_bind_started")
    listener = bind_ephemeral_socket(config.host)
    startup_phase("listener_bound")
    try:
        return asyncio.run(_serve(config, listener))
    finally:
        listener.close()


def parse_args(argv: Sequence[str] | None = None) -> SidecarConfig:
    parser = argparse.ArgumentParser(description="XRML packaged FastAPI desktop sidecar")
    parser.add_argument("--web-dist", type=Path, default=default_frontend_dist())
    # Validate the raw value first: Path("") becomes the current directory and
    # would silently turn a missing required data root into a writable path.
    parser.add_argument("--data-dir", default=os.getenv("XRML_DATA_DIR"))
    parser.add_argument("--startup-timeout-seconds", type=float, default=30.0)
    args = parser.parse_args(argv)
    if args.data_dir is None or not args.data_dir.strip():
        raise SidecarFailure(
            SidecarFailureCode.CONFIGURATION_FAILURE,
            "XRML_DATA_DIR or --data-dir is required for the desktop sidecar.",
        )
    return SidecarConfig(
        web_dist=args.web_dist,
        data_dir=Path(args.data_dir),
        startup_timeout_seconds=args.startup_timeout_seconds,
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        startup_phase("configuration_started")
        config = parse_args(argv)
        startup_phase("configuration_complete")
        return run_sidecar(config)
    except SidecarFailure as exc:
        emit_lifecycle_event(
            {
                "event": "fatal",
                "code": exc.code.value,
                "detail": redact_lifecycle_detail(str(exc)),
                "pid": os.getpid(),
            },
            stream=sys.stderr,
        )
        return 1
    except Exception:
        emit_lifecycle_event(
            {
                "event": "fatal",
                "code": SidecarFailureCode.UNEXPECTED_CRASH.value,
                "detail": "[redacted]",
                "pid": os.getpid(),
            },
            stream=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
