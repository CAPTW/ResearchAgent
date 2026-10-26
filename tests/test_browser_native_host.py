"""Native bridge boundaries, with synthetic bytes and no real browser or account."""
import io
import json
from pathlib import Path
import struct
import subprocess
import sys

import pytest

from app.desktop import browser_native_host as host
from scripts.prepare_browser_native_host import prepare


def test_framing_and_size_fail_closed():
    output = io.BytesIO()
    host.write_message(output, {"ok": True})
    output.seek(0)
    assert host.read_message(output) == {"ok": True}
    for raw in (b"x", struct.pack("=I", host.MAX_MESSAGE + 1), struct.pack("=I", 5) + b"{}"):
        with pytest.raises(host.BridgeError):
            host.read_message(io.BytesIO(raw))


@pytest.mark.parametrize("base", ["https://127.0.0.1:1234", "http://localhost:1234", "http://example.org:1234",
    "http://127.0.0.1:1234/else", "http://user@127.0.0.1:1234", "http://127.0.0.1:1234?secret=x"])
def test_remote_proxy_and_path_destinations_rejected(base):
    with pytest.raises(host.BridgeError):
        host.connection({"base_url": base, "job_id": "bc_fixture", "token": "x" * 43})


def fixture_download(tmp_path):
    config = {"browser": "whale", "extension_id": "a" * 32,
        "extension_origin": "whale-extension://" + "a" * 32 + "/", "download_root": str(tmp_path / "XRML")}
    conn = {"base_url": "http://127.0.0.1:1234", "job_id": "bc_fixture", "token": "x" * 43}
    path = Path(config["download_root"]) / conn["job_id"] / "bci_fixture" / "attempt.pdf"
    path.parent.mkdir(parents=True)
    raw = b"%PDF-1.4\nsynthetic framing fixture only\n%%EOF\n"
    path.write_bytes(raw)
    download = {"id": 7, "byExtensionId": "a" * 32, "state": "complete", "exists": True,
        "danger": "safe", "filename": str(path), "url": "https://arxiv.org/pdf/2601.00001v1",
        "fileSize": len(raw), "bytesReceived": len(raw), "totalBytes": len(raw)}
    return config, conn, download, path, raw


def test_only_exact_attempt_file_is_read(tmp_path):
    config, conn, download, _, raw = fixture_download(tmp_path)
    observed, headers = host.read_download(config, conn, "bci_fixture", "attempt", download)
    assert observed == raw
    assert headers["X-XRML-Download-ID"] == "7"
    download["totalBytes"] = -1  # Downloads API uses -1 when Content-Length is unknown.
    assert host.read_download(config, conn, "bci_fixture", "attempt", download)[0] == raw
    with pytest.raises(host.BridgeError, match="download_path_rejected"):
        host.read_download(config, conn, "bci_other", "attempt", download)


@pytest.mark.parametrize("key,value", [("state", "in_progress"), ("danger", "accepted"), ("exists", False),
    ("byExtensionId", "b" * 32), ("fileSize", 1), ("finalUrl", "https://arxiv.org/login")])
def test_browser_receipt_must_be_complete_owned_and_scoped(tmp_path, key, value):
    config, conn, download, _, _ = fixture_download(tmp_path)
    download[key] = value
    with pytest.raises(host.BridgeError):
        host.read_download(config, conn, "bci_fixture", "attempt", download)


def test_html_or_partial_pdf_not_forwarded(tmp_path):
    config, conn, download, path, _ = fixture_download(tmp_path)
    for raw in (b"<html>Login required</html>", b"%PDF-1.7\npartial"):
        path.write_bytes(raw)
        download.update(fileSize=len(raw), bytesReceived=len(raw), totalBytes=len(raw))
        with pytest.raises(host.BridgeError, match="download_is_not_complete_pdf"):
            host.read_download(config, conn, "bci_fixture", "attempt", download)


def test_expired_capability_rejected_before_local_file_read(monkeypatch):
    monkeypatch.setattr(host, "request", lambda *a, **k: (_ for _ in ()).throw(host.BridgeError("connection_expired_or_denied")))
    monkeypatch.setattr(host, "read_download", lambda *a, **k: pytest.fail("expired capability read a local file"))
    with pytest.raises(host.BridgeError, match="expired"):
        host.handle({"command": "upload", "connection": {"base_url": "http://127.0.0.1:1234", "job_id": "bc_fixture", "token": "x" * 43},
            "item_id": "bci_fixture", "attempt_id": "attempt"}, {})


@pytest.mark.parametrize("browser,scheme,registry", [
    ("whale", "whale-extension", r"Software\Naver\Naver Whale\NativeMessagingHosts"),
    ("chrome", "chrome-extension", r"Software\Google\Chrome\NativeMessagingHosts"),
    ("edge", "chrome-extension", r"Software\Microsoft\Edge\NativeMessagingHosts"),
])
def test_prepare_creates_reviewable_files_without_registering(tmp_path, browser, scheme, registry):
    root = tmp_path / "prepared"
    result = prepare(root, "a" * 32, tmp_path / "Downloads" / "XRML", browser)
    assert result["state"] == "PREPARED_NOT_REGISTERED"
    manifest = json.loads((root / (host.HOST_NAME + ".json")).read_text(encoding="utf-8"))
    origin = scheme + "://" + "a" * 32 + "/"
    assert manifest["allowed_origins"] == [origin]
    registration = (root / "register-current-user.reg").read_text(encoding="utf-16")
    assert f"[HKEY_CURRENT_USER\\{registry}\\{host.HOST_NAME}]" in registration
    assert registration.count("[HKEY_CURRENT_USER") == 1
    config = json.loads((root / "host-config.json").read_text(encoding="utf-8"))
    assert config["browser"] == browser and config["extension_origin"] == origin
    host.validate_origin(config, origin)
    with pytest.raises(FileExistsError):
        prepare(root, "a" * 32, tmp_path / "Downloads" / "XRML", browser)


@pytest.mark.parametrize("browser,scheme", [("whale", "whale-extension"), ("chrome", "chrome-extension"), ("edge", "chrome-extension")])
def test_native_startup_accepts_only_prepared_browser_origin(tmp_path, browser, scheme):
    root = tmp_path / "prepared"
    prepare(root, "a" * 32, tmp_path / "Downloads" / "XRML", browser)
    command = [sys.executable, "-m", "app.desktop.browser_native_host", "--config", str(root / "host-config.json")]
    origin = scheme + "://" + "a" * 32 + "/"
    accepted = subprocess.run([*command, origin], input=b"", capture_output=True, timeout=10, check=False)
    assert accepted.returncode == 0 and accepted.stdout == b""
    other = "chrome-extension" if browser == "whale" else "whale-extension"
    for rejected in (other + "://" + "a" * 32 + "/", scheme + "://" + "b" * 32 + "/", origin + "page.html"):
        result = subprocess.run([*command, rejected], input=b"", capture_output=True, timeout=10, check=False)
        assert result.returncode == 1
        assert host.read_message(io.BytesIO(result.stdout)) == {"ok": False, "error": "extension_origin_denied"}


@pytest.mark.parametrize("change", [
    {"browser": "unknown"}, {"browser": None}, {"extension_origin": "chrome-extension://" + "a" * 32 + "/"},
    {"extension_origin": "whale-extension://*/"}, {"extension_id": None},
])
def test_origin_rejects_unbound_or_inconsistent_browser_configuration(change):
    origin = "whale-extension://" + "a" * 32 + "/"
    config = {"browser": "whale", "extension_id": "a" * 32, "extension_origin": origin}
    config.update(change)
    with pytest.raises(host.BridgeError, match="extension_origin_denied"):
        host.validate_origin(config, origin)


def test_legacy_configuration_requires_explicit_browser_repreparation():
    with pytest.raises(host.BridgeError, match="extension_origin_denied"):
        host.validate_origin({"extension_id": "a" * 32, "download_root": "unused"}, "chrome-extension://" + "a" * 32 + "/")


def test_preparation_script_runs_without_project_import_path(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "prepare_browser_native_host.py"
    output = tmp_path / "prepared"
    result = subprocess.run([sys.executable, str(script), "--browser", "whale", "--extension-id", "a" * 32,
        "--download-root", str(tmp_path / "Downloads" / "XRML"), "--output", str(output)],
        cwd=tmp_path, capture_output=True, timeout=10, check=False)
    assert result.returncode == 0
    assert json.loads(result.stdout)["state"] == "PREPARED_NOT_REGISTERED"
    config = json.loads((output / "host-config.json").read_text(encoding="utf-8"))
    host.validate_origin(config, "whale-extension://" + "a" * 32 + "/")


def test_manifest_has_no_cookie_host_or_debugger_permissions():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "extensions/whale-collector/manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["permissions"]) == {"downloads", "nativeMessaging", "storage"}
    assert "host_permissions" not in manifest
    assert "content_scripts" not in manifest
