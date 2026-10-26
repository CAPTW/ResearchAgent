"""Small stdio bridge for the explicitly connected collector extension.

No site requests, browser profiles, cookie access, shell execution or AI calls.
The browser owns Downloads API observations; this process validates their scope
and reads only the exact attempt file beneath its configured download root.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import sys
from typing import Any, BinaryIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_MESSAGE = 64 * 1024
MAX_RESPONSE = 900 * 1024
MAX_PDF = 25_000_000
HOST_NAME = "org.xrml.paper_collector"
BROWSER_ORIGIN_SCHEMES = {"whale": "whale-extension", "chrome": "chrome-extension", "edge": "chrome-extension"}


class BridgeError(ValueError):
    """Fixed public code, never a raw exception or local path."""


def extension_origin(browser: Any, extension_id: Any) -> str:
    if (not isinstance(browser, str) or browser not in BROWSER_ORIGIN_SCHEMES
            or not isinstance(extension_id, str) or re.fullmatch(r"[a-p]{32}", extension_id) is None):
        raise BridgeError("extension_origin_denied")
    return f"{BROWSER_ORIGIN_SCHEMES[browser]}://{extension_id}/"


def validate_origin(config: Any, origin: str) -> None:
    if not isinstance(config, dict):
        raise BridgeError("extension_origin_denied")
    expected = extension_origin(config.get("browser"), config.get("extension_id"))
    if config.get("extension_origin") != expected or origin != expected:
        raise BridgeError("extension_origin_denied")


def identifier(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}", value) is None:
        raise BridgeError("invalid_identity")
    return value


def connection(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"base_url", "job_id", "token"}:
        raise BridgeError("invalid_connection")
    url = urlsplit(value.get("base_url", ""))
    try:
        valid = (url.scheme == "http" and url.hostname == "127.0.0.1" and url.port is not None
                 and 1 <= url.port <= 65535 and not url.username and not url.password
                 and url.path in {"", "/"} and not url.query and not url.fragment)
    except ValueError:
        valid = False
    token = value.get("token")
    if not valid or not isinstance(token, str) or re.fullmatch(r"[a-zA-Z0-9_-]{32,128}", token) is None:
        raise BridgeError("invalid_connection")
    return {"base_url": value["base_url"].rstrip("/"), "job_id": identifier(value["job_id"]), "token": token}


def read_message(stream: BinaryIO) -> dict[str, Any] | None:
    header = stream.read(4)
    if not header:
        return None
    if len(header) != 4:
        raise BridgeError("incomplete_message")
    length = struct.unpack("=I", header)[0]
    if not 0 < length <= MAX_MESSAGE:
        raise BridgeError("message_too_large")
    raw = stream.read(length)
    if len(raw) != length:
        raise BridgeError("incomplete_message")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise BridgeError("invalid_message") from None
    if not isinstance(value, dict):
        raise BridgeError("invalid_message")
    return value


def write_message(stream: BinaryIO, value: dict[str, Any]) -> None:
    raw = json.dumps(value, ensure_ascii=True, allow_nan=False).encode("utf-8")
    if len(raw) > MAX_RESPONSE:
        raw = b'{"ok":false,"error":"response_too_large"}'
    stream.write(struct.pack("=I", len(raw)) + raw)
    stream.flush()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BridgeError("redirect_refused")


def request(conn: dict[str, str], suffix: str = "", *, payload: Any = None,
            raw: bytes | None = None, headers: dict[str, str] | None = None) -> Any:
    body = raw if raw is not None else json.dumps(payload).encode("utf-8") if payload is not None else None
    request_headers = {"Authorization": "Bearer " + conn["token"], "Accept": "application/json",
                       "Content-Type": "application/pdf" if raw is not None else "application/json"}
    request_headers.update(headers or {})
    req = Request(conn["base_url"] + "/api/browser-collection-bridge/" + conn["job_id"] + suffix,
                  data=body, headers=request_headers, method="POST" if body is not None else "GET")
    try:
        # Do not send capabilities to system proxies or redirected servers.
        with build_opener(ProxyHandler({}), _NoRedirect()).open(req, timeout=30) as response:
            data = response.read(MAX_RESPONSE + 1)
        if len(data) > MAX_RESPONSE:
            raise BridgeError("response_too_large")
        return json.loads(data)
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise BridgeError("connection_expired_or_denied") from None
        if exc.code == 422:
            raise BridgeError("file_or_scope_rejected") from None
        if exc.code == 409:
            raise BridgeError("state_changed_refresh_first") from None
        raise BridgeError("app_request_failed") from None
    except (URLError, TimeoutError, OSError):
        raise BridgeError("app_unavailable_resume_after_reopen") from None
    except (ValueError, UnicodeError):
        raise BridgeError("invalid_app_response") from None


def _file_signature(path: Path) -> tuple[int, int, int, int, int]:
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise BridgeError("download_path_rejected")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= MAX_PDF:
        raise BridgeError("download_file_rejected")
    return info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_dev, info.st_ino


def read_download(config: dict[str, str], conn: dict[str, str], item: str, attempt: str,
                  download: Any) -> tuple[bytes, dict[str, str]]:
    if not isinstance(download, dict):
        raise BridgeError("invalid_download")
    if (download.get("state") != "complete" or download.get("exists") is not True
            or download.get("danger") not in {"safe", "deepScannedSafe"}
            or download.get("byExtensionId") != config["extension_id"]
            or type(download.get("id")) is not int or download["id"] < 0):
        raise BridgeError("download_not_confirmed_complete")
    root = Path(config["download_root"])
    expected = root / conn["job_id"] / identifier(item) / (identifier(attempt) + ".pdf")
    actual = Path(download.get("filename", ""))
    if not root.is_absolute() or not actual.is_absolute() or os.path.normcase(str(actual)) != os.path.normcase(str(expected)):
        raise BridgeError("download_path_rejected")
    before = _file_signature(expected)
    if (any(type(download.get(key)) is not int or download[key] != before[0]
            for key in ("fileSize", "bytesReceived"))
            or type(download.get("totalBytes")) is not int or download["totalBytes"] not in {-1, before[0]}):
        raise BridgeError("download_bytes_mismatch")
    source = download.get("finalUrl") or download.get("url")
    # The backend additionally binds this URL to the exact approved paper version.
    if (not isinstance(source, str) or re.fullmatch(
            r"https://(?:export\.)?arxiv\.org/pdf/(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})v[1-9]\d*(?:\.pdf)?", source) is None):
        raise BridgeError("download_scope_rejected")
    with expected.open("rb") as stream:
        raw = stream.read(MAX_PDF + 1)
    if _file_signature(expected) != before or len(raw) != before[0]:
        raise BridgeError("download_changed")
    if not raw.startswith(b"%PDF-") or b"%%EOF" not in raw[-1024:]:
        raise BridgeError("download_is_not_complete_pdf")
    return raw, {"X-XRML-Attempt-ID": attempt, "X-XRML-Download-ID": str(download["id"]),
                 "X-XRML-Source-URL": source, "X-XRML-PDF-SHA256": hashlib.sha256(raw).hexdigest(),
                 "X-XRML-Download-State": "complete", "X-XRML-Download-Bytes": str(len(raw))}


def handle(message: dict[str, Any], config: dict[str, str]) -> dict[str, Any]:
    conn = connection(message.get("connection"))
    command = message.get("command")
    if command == "inspect":
        data = request(conn)
    elif command == "transition":
        item = identifier(message.get("item_id"))
        payload = message.get("payload")
        if not isinstance(payload, dict):
            raise BridgeError("invalid_transition")
        data = request(conn, "/items/" + item + "/transition", payload=payload)
    elif command == "upload":
        item, attempt = identifier(message.get("item_id")), identifier(message.get("attempt_id"))
        # Authorize before touching a file; expired/revoked connections do not read.
        request(conn)
        raw, headers = read_download(config, conn, item, attempt, message.get("download"))
        data = request(conn, "/items/" + item + "/pdf", raw=raw, headers=headers)
    else:
        raise BridgeError("command_not_supported")
    return {"ok": True, "data": data}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("origin")
    parser.add_argument("--parent-window")
    args = parser.parse_args(argv)
    if os.name == "nt":
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        validate_origin(config, args.origin)
        message = read_message(sys.stdin.buffer)
        if message is not None:
            write_message(sys.stdout.buffer, handle(message, config))
        return 0
    except BridgeError as exc:
        write_message(sys.stdout.buffer, {"ok": False, "error": str(exc)})
    except Exception:
        write_message(sys.stdout.buffer, {"ok": False, "error": "bridge_failed"})
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
