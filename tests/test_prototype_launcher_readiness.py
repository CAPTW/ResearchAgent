from __future__ import annotations

from unittest.mock import Mock

import pytest

from tools import prototype_launcher as launcher


@pytest.mark.parametrize(
    ("status", "payload", "error"),
    [
        (200, {"status": "not_ready", "runtime_bootstrap": {"status": "failed"}}, "runtime_bootstrap_failed"),
        (200, {"status": "degraded", "runtime_bootstrap": {"status": "failed"}}, "runtime_bootstrap_failed"),
        (200, {"status": "ready"}, "local_endpoint_startup_timeout"),
        (200, "not JSON", "local_endpoint_startup_timeout"),
        (404, None, "local_endpoint_startup_timeout"),
        (503, {"status": "ready"}, "local_endpoint_startup_timeout"),
        (
            200,
            {"status": "ready", "runtime_stores": {"ready": False}, "runtime_bootstrap": {"status": "completed"}},
            "local_endpoint_startup_timeout",
        ),
        (
            200,
            {"status": "ready", "runtime_stores": {"ready": True}, "runtime_bootstrap": {"status": "not_observed"}},
            "local_endpoint_startup_timeout",
        ),
    ],
)
def test_launcher_rejects_unready_backend_before_research_requests(
    monkeypatch: pytest.MonkeyPatch, tmp_path, status: int, payload: object, error: str,
) -> None:
    """Exercise the real launcher wiring; no process, scheduler or research is started."""
    data_root = tmp_path / "launcher-data"
    data_root.mkdir()
    monkeypatch.setattr(launcher.tempfile, "mkdtemp", lambda **_kwargs: str(data_root))
    monkeypatch.setattr(launcher, "_require_free_port", lambda _port: None)
    monkeypatch.setattr(launcher, "_runtime_environment", lambda *_args, **_kwargs: {})
    backend, frontend = Mock(), Mock()
    backend.poll.return_value = frontend.poll.return_value = None
    start = Mock(side_effect=[backend, frontend])
    stop = Mock()
    monkeypatch.setattr(launcher, "_start_owned_process", start)
    monkeypatch.setattr(launcher, "_stop_owned_processes", stop)
    clock = [0.0]
    monkeypatch.setattr(launcher.time, "monotonic", lambda: clock[0])

    def advance(_seconds):
        clock[0] += 31.0

    sleep = Mock(side_effect=advance)
    monkeypatch.setattr(launcher.time, "sleep", sleep)
    requests = []

    def request(method, url, payload=None):
        requests.append((method, url))
        assert method == "GET" and url.endswith("/health/ready"), "unready backend advanced into app flow"
        return status, response_payload

    response_payload = payload
    monkeypatch.setattr(launcher, "_json_request", request)
    with pytest.raises(launcher.LauncherError, match=f"^{error}$"):
        launcher.run(launcher.build_parser().parse_args([]))
    assert len(requests) == 1
    stop.assert_called_once_with([backend, frontend], [8000, 5173])
    if error == "runtime_bootstrap_failed":
        sleep.assert_not_called()


@pytest.mark.parametrize("status", ["ready", "degraded"])
def test_backend_wait_accepts_completed_bootstrap_with_usable_stores(monkeypatch, status) -> None:
    process = Mock()
    process.poll.return_value = None
    request = Mock(return_value=(200, {
        "status": status,
        "runtime_bootstrap": {"status": "completed"},
        "runtime_stores": {"ready": True},
    }))
    monkeypatch.setattr(launcher, "_json_request", request)
    launcher._wait_for_http("http://127.0.0.1:8000/health/ready", process, require_runtime_ready=True)
    request.assert_called_once()


def test_backend_wait_recovers_after_unreachable_and_unready_responses(monkeypatch) -> None:
    process = Mock()
    process.poll.return_value = None
    request = Mock(side_effect=[
        launcher.LauncherError("local_endpoint_unreachable"),
        (200, {"status": "not_ready", "runtime_bootstrap": {"status": "not_observed"}}),
        (200, {
            "status": "degraded",
            "runtime_bootstrap": {"status": "completed"},
            "runtime_stores": {"ready": True},
        }),
    ])
    monkeypatch.setattr(launcher, "_json_request", request)
    sleep = Mock()
    monkeypatch.setattr(launcher.time, "sleep", sleep)
    launcher._wait_for_http("http://127.0.0.1:8000/health/ready", process, require_runtime_ready=True)
    assert request.call_count == 3
    assert sleep.call_count == 2


def test_backend_wait_rejects_exited_process_before_http(monkeypatch) -> None:
    process = Mock()
    process.poll.return_value = 1
    request = Mock()
    monkeypatch.setattr(launcher, "_json_request", request)
    with pytest.raises(launcher.LauncherError, match="^owned_process_exited_during_startup$"):
        launcher._wait_for_http("http://127.0.0.1:8000/health/ready", process, require_runtime_ready=True)
    request.assert_not_called()


def test_frontend_wait_still_accepts_html(monkeypatch) -> None:
    process = Mock()
    process.poll.return_value = None
    request = Mock(return_value=(200, "<html>app</html>"))
    monkeypatch.setattr(launcher, "_json_request", request)
    launcher._wait_for_http("http://127.0.0.1:5173/app/", process)
    request.assert_called_once()
