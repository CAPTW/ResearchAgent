"""Local API keys encrypted by Windows DPAPI, separate from public preferences.

Only trusted server composition can resolve plaintext. No read-key HTTP API,
credential validation call, environment mutation, or execution approval here.
"""
import base64
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import sys
from typing import Any

from app.repositories.shared_state import SharedStateError, load_json_state, mutate_json_state

SCHEMA = "MODEL_CREDENTIALS_DPAPI_V1"
ENVIRONMENTS = {"anthropic_api": "ANTHROPIC_API_KEY", "openai_api": "OPENAI_API_KEY", "xai_api": "XAI_API_KEY"}


class CredentialUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("model_credentials_unavailable")


def available() -> bool:
    return sys.platform == "win32"


def path() -> Path:
    from app.services.model_connections import STATE_PATH
    return STATE_PATH.parent / "private" / "provider-credentials.json"


def _connector(identity: str) -> str:
    if identity not in ENVIRONMENTS:
        raise ValueError("model_credentials_connector_invalid")
    return ENVIRONMENTS[identity]


def _key(value: str) -> str:
    if type(value) is not str:
        raise ValueError("model_credentials_invalid")
    value = value.strip()
    if not 1 <= len(value) <= 4096 or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise ValueError("model_credentials_invalid")
    return value


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(raw: bytes, identity: str, *, decrypt: bool = False) -> bytes:
    if sys.platform != "win32" or not available():
        raise CredentialUnavailable()
    buffer = ctypes.create_string_buffer(raw)
    entropy_buffer = ctypes.create_string_buffer((SCHEMA + ":" + identity).encode("ascii"))
    source = _Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    entropy = _Blob(len(entropy_buffer.value), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = _Blob()
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
        function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob),
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        function.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        # USER scope (never LOCAL_MACHINE), non-interactive. Entropy binds connector.
        if not function(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 1, ctypes.byref(output)):
            raise CredentialUnavailable()
        try:
            if output.cbData > 16384:
                raise CredentialUnavailable()
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            ctypes.memset(output.pbData, 0, output.cbData)
            kernel32.LocalFree(output.pbData)
    except (OSError, ValueError):
        raise CredentialUnavailable() from None
    finally:
        ctypes.memset(buffer, 0, len(buffer))


def _default() -> dict[str, Any]:
    return {"schema_version": SCHEMA, "entries": {}}


def _validate(value: dict[str, Any]) -> dict[str, Any]:
    try:
        if set(value) != {"schema_version", "entries"} or value["schema_version"] != SCHEMA or type(value["entries"]) is not dict:
            raise ValueError()
        for identity, item in value["entries"].items():
            _connector(identity)
            if (type(item) is not dict or set(item) != {"revision", "ciphertext"}
                    or type(item["revision"]) is not int or item["revision"] < 1):
                raise ValueError()
            cipher = item["ciphertext"]
            if cipher is not None:
                if type(cipher) is not str or not 1 <= len(cipher) <= 24000:
                    raise ValueError()
                base64.b64decode(cipher, validate=True)
        return value
    except (ValueError, TypeError, KeyError):
        raise CredentialUnavailable() from None


def _read(location: Path | None = None) -> dict[str, Any]:
    try:
        location = path() if location is None else location
        if location.exists() and location.stat().st_size > 100_000:
            raise CredentialUnavailable()
        return _validate(load_json_state(location, state_name="model_credentials", expected_type=dict, default=_default))
    except (SharedStateError, OSError):
        raise CredentialUnavailable() from None


def _status(identity: str, state: dict[str, Any]) -> dict[str, Any]:
    env = _connector(identity)
    entry = state["entries"].get(identity, {"revision": 0, "ciphertext": None})
    stored = entry["ciphertext"] is not None
    environment = bool(os.getenv(env, "").strip())
    return dict(connector_id=identity, revision=entry["revision"], storage_available=available(), stored=stored,
                environment_present=environment, source="stored" if stored else "environment" if environment else "none",
                execution_allowed=False)


def status(identity: str, *, credential_path: Path | None = None) -> dict[str, Any]:
    _connector(identity)
    return _status(identity, _read(credential_path))


def _decode(identity: str, cipher: str) -> str:
    try:
        return _key(_crypt(base64.b64decode(cipher, validate=True), identity, decrypt=True).decode("ascii"))
    except (ValueError, UnicodeError):
        raise CredentialUnavailable() from None


def resolve(identity: str) -> str:
    env = _connector(identity)
    entry = _read()["entries"].get(identity)
    if entry and entry["ciphertext"] is not None:
        return _decode(identity, entry["ciphertext"])
    value = os.getenv(env, "").strip()
    if not value:
        raise ValueError("model_connection_key_required")
    return _key(value)


def resolve_stored(identity: str, expected_revision: int, *, credential_path: Path | None = None) -> str:
    """Resolve one exact encrypted snapshot selected by trusted server code.

    The optional path keeps a trial ledger separate from its credential vault.
    No default-vault or environment fallback, mutation, or execution authority.
    """
    _connector(identity)
    if type(expected_revision) is not int or expected_revision < 1:
        raise ValueError("model_credentials_invalid")
    entry = _read(credential_path)["entries"].get(identity)
    if entry is None or entry["revision"] != expected_revision or entry["ciphertext"] is None:
        raise ValueError("model_credentials_changed")
    return _decode(identity, entry["ciphertext"])


def _mutate(identity: str, expected_revision: int, key: str | None) -> dict[str, Any]:
    _connector(identity)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("model_credentials_invalid")
    if not available():
        raise CredentialUnavailable()
    # Detect corruption/oversize before acquiring the ordinary shared-state lock.
    _read()
    def change(raw: dict[str, Any]):
        state = _validate(raw)
        old = state["entries"].get(identity, {"revision": 0, "ciphertext": None})
        same = old["ciphertext"] is None and key is None
        if key is not None and old["ciphertext"] is not None:
            # No stored plaintext digest; exact lost-response replay is readback.
            same = _decode(identity, old["ciphertext"]) == key
        if same:
            return state, _status(identity, state)
        if old["revision"] != expected_revision:
            raise ValueError("model_credentials_changed")
        cipher = None if key is None else base64.b64encode(_crypt(key.encode("ascii"), identity)).decode("ascii")
        value = {"schema_version": SCHEMA, "entries": {**state["entries"], identity:
                 {"revision": old["revision"] + 1, "ciphertext": cipher}}}
        return value, _status(identity, value)
    try:
        return mutate_json_state(path(), state_name="model_credentials", expected_type=dict, default=_default, mutate=change)
    except (SharedStateError, OSError):
        raise CredentialUnavailable() from None


def save(identity: str, expected_revision: int, key: str) -> dict[str, Any]:
    return _mutate(identity, expected_revision, _key(key))


def delete(identity: str, expected_revision: int) -> dict[str, Any]:
    return _mutate(identity, expected_revision, None)
