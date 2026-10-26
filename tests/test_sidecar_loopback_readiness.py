"""A sidecar must observe its own loopback server, never a configured proxy."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import subprocess
import sys
from threading import Thread
import urllib.request

import pytest

from app.desktop.sidecar import LOOPBACK_HOST, _readiness_is_usable


READY = {"status": "ready", "runtime_stores": {"ready": True}, "runtime_bootstrap": {"status": "completed"}}


@contextmanager
def local_server(status: int, payload: object):
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer((LOOPBACK_HOST, 0), Handler) as server:
        worker = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        worker.start()
        try:
            yield server.server_port, requests
        finally:
            server.shutdown()
            worker.join(timeout=5)
            assert not worker.is_alive()


@pytest.mark.parametrize("proxy_source", ["environment", "system"])
@pytest.mark.parametrize("local_status,local_payload,proxy_status,proxy_payload,expected", [
    (200, READY, 503, {"status": "proxy_denied"}, True),
    (503, {"status": "not_ready"}, 200, READY, False),
])
def test_readiness_uses_local_response_without_proxy_or_global_opener_changes(
    monkeypatch, proxy_source, local_status, local_payload, proxy_status, proxy_payload, expected,
):
    with local_server(local_status, local_payload) as (port, local_requests):
        with local_server(proxy_status, proxy_payload) as (proxy_port, proxy_requests):
            proxy = f"http://{LOOPBACK_HOST}:{proxy_port}"
            if proxy_source == "environment":
                monkeypatch.setenv("http_proxy", proxy)
                monkeypatch.setenv("HTTP_PROXY", proxy)
                monkeypatch.setenv("no_proxy", "example.invalid")
                monkeypatch.setenv("NO_PROXY", "example.invalid")
            else:
                # Stand in for OS proxy discovery, not the network response.
                monkeypatch.setattr(urllib.request, "getproxies", lambda: {"http": proxy})
                monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
            monkeypatch.setattr(urllib.request, "_opener", None)
            before_environment = dict(os.environ)
            observed = _readiness_is_usable(LOOPBACK_HOST, port)
            assert (observed, local_requests, proxy_requests) == (expected, ["/health/ready"], [])
            assert urllib.request._opener is None
            assert dict(os.environ) == before_environment


def test_actual_sidecar_starts_and_restarts_with_a_rejecting_proxy(tmp_path):
    dist = tmp_path / "web-dist"
    dist.mkdir()
    (dist / "index.html").write_text("<main>synthetic desktop fixture</main>", encoding="utf-8")
    data = tmp_path / "runtime-data"
    with local_server(503, {"status": "proxy_denied"}) as (proxy_port, proxy_requests):
        proxy = f"http://{LOOPBACK_HOST}:{proxy_port}"
        env = {**os.environ, "http_proxy": proxy, "HTTP_PROXY": proxy,
               "no_proxy": "example.invalid", "NO_PROXY": "example.invalid",
               "AUTH_MODE": "disabled", "XRML_DATA_DIR": str(data),
               "TOPIC_SCHEDULER_ENABLED": "0", "TOPIC_SCHEDULER_MODE": "disabled",
               "XRML_RUN_EXECUTOR_MODE": "disabled", "XRML_PROVIDER": "disabled",
               "XRML_JOURNAL_OFFLINE_ENABLED": "0", "XRML_REVIEW_OFFLINE_ENABLED": "0",
               "XRML_REVIEW_EXECUTION_OFFLINE_ENABLED": "0", "XRML_PAPER_TRANSPORT_OFFLINE_ENABLED": "0"}
        for _ in range(2):
            result = subprocess.run(
                [sys.executable, "-m", "app.desktop.sidecar", "--web-dist", str(dist), "--data-dir", str(data)],
                env=env, input="shutdown\n", capture_output=True, text=True, encoding="utf-8", timeout=60,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
            assert [event["event"] for event in events] == ["ready", "shutdown"]
            assert events[0]["host"] == LOOPBACK_HOST and events[0]["port"] > 0
            assert events[-1]["reason"] == "host_requested"
        assert proxy_requests == []
