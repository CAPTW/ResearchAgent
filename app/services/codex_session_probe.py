"""Safe Codex CLI session probe.

Talks only to official installed-executable help, version, and status
surfaces. Never reads credential files, never copies token values, and
never persists raw command output.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import re
import shutil
import subprocess
from typing import Any, Callable, Mapping

PROVIDER_ID = "codex"
DISPLAY_NAME = "Codex"
STAGE = "report_draft_assist"
EXECUTABLE_LABEL = "codex"
PROBE_TIMEOUT_SECONDS = 8
SECRET_MARKERS: tuple[str, ...] = (
    "Bearer ",
    "sk-",
    "ya29.",
    "access_token=",
    "refresh_token=",
    "client_secret=",
    "api_key=",
    "CODEX_API_KEY=",
    "CODEX_ACCESS_TOKEN=",
    "Authorization:",
    "super-secret-value",
)
UNAUTHENTICATED_MARKERS: tuple[str, ...] = (
    "not logged in",
    "logged out",
    "unauthenticated",
    "login required",
    "please log in",
    "not authenticated",
)
AUTHENTICATED_MARKERS: tuple[str, ...] = (
    "logged in",
    "authenticated",
    "signed in",
)


@dataclass(frozen=True)
class CommandResult:
    argv_tail: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


CommandRunner = Callable[[tuple[str, ...]], CommandResult]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _contains_secret_shape(text: str) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in SECRET_MARKERS)


def assert_no_secret_leakage(payload: Mapping[str, Any] | str) -> list[str]:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    hits = [marker for marker in SECRET_MARKERS if marker.lower() in text.lower()]
    if re.search(r"(?i)[a-z]:\\users\\", text):
        hits.append("absolute_user_path")
    if "auth.json" in text or ".codex/" in text:
        hits.append("credential_path")
    return hits


def _safe_version(text: str) -> str | None:
    match = re.search(r"\b(\d+\.\d+(?:\.\d+)?)\b", text)
    return match.group(1) if match else None


def _help_has_subcommand(help_text: str, name: str) -> bool:
    return re.search(rf"(?im)^\s+{re.escape(name)}\b", help_text) is not None


def _classify_status_text(text: str) -> tuple[bool, str]:
    lowered = text.lower()
    if _contains_secret_shape(text):
        return False, "codex_session_status_redacted"
    if any(marker in lowered for marker in UNAUTHENTICATED_MARKERS):
        return False, "codex_session_unauthenticated"
    if any(marker in lowered for marker in AUTHENTICATED_MARKERS):
        return True, "codex_session_authenticated"
    return False, "codex_session_status_unclassified"


def _resolve_executable() -> str | None:
    names = ("codex.cmd", "codex.exe", "codex") if os.name == "nt" else ("codex",)
    for name in names:
        resolved = shutil.which(name)
        if resolved:
            return resolved
    return None


def codex_cli_is_installed() -> bool:
    return _resolve_executable() is not None


def _probe_env() -> dict[str, str]:
    env = os.environ.copy()
    if env.get("TERM", "").strip().lower() == "dumb":
        env.pop("TERM", None)
    return env


def _default_runner(argv_tail: tuple[str, ...]) -> CommandResult:
    executable = _resolve_executable()
    if executable is None:
        return CommandResult(argv_tail, 127, "", "")
    try:
        completed = subprocess.run(
            [executable, *argv_tail],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=float(PROBE_TIMEOUT_SECONDS),
            env=_probe_env(),
        )
    except subprocess.TimeoutExpired:
        return CommandResult(argv_tail, 124, "", "", timed_out=True)
    except OSError:
        return CommandResult(argv_tail, 127, "", "")
    return CommandResult(argv_tail, int(completed.returncode), completed.stdout or "", completed.stderr or "")


def _card(
    *,
    installed: bool,
    available: bool,
    authenticated: bool,
    reason_code: str,
    version: str | None,
    last_probe_at: str,
    official_auth_surface_available: bool,
    activation_proof_state: str,
) -> dict[str, object]:
    return {
        "provider_id": PROVIDER_ID,
        "display_name": DISPLAY_NAME,
        "surface": "cli",
        "installed": installed,
        "available": available,
        "authenticated": authenticated,
        "activation_proof_state": activation_proof_state,
        "supported_stages": [STAGE],
        "supported_output_classes": ["report_draft", "weekly_report"],
        "version": version,
        "last_probe_at": last_probe_at,
        "reason_code": reason_code,
        "official_login_command": "codex login",
        "official_status_command": "codex login status",
        "official_auth_surface_available": official_auth_surface_available,
        "deterministic_fallback_available": True,
    }


def probe_codex_session(
    *,
    runner: CommandRunner | None = None,
    executable: str | None = None,
) -> dict[str, object]:
    """Probe official Codex CLI help/version/status without reading secrets."""

    probed_at = _iso(_utc_now())
    if executable == "":
        return _card(
            installed=False,
            available=False,
            authenticated=False,
            reason_code="codex_cli_not_installed",
            version=None,
            last_probe_at=probed_at,
            official_auth_surface_available=False,
            activation_proof_state="not_probed",
        )
    command_runner = runner
    if command_runner is None:
        if _resolve_executable() is None:
            return _card(
                installed=False,
                available=False,
                authenticated=False,
                reason_code="codex_cli_not_installed",
                version=None,
                last_probe_at=probed_at,
                official_auth_surface_available=False,
                activation_proof_state="not_probed",
            )
        command_runner = _default_runner

    version_result = command_runner(("--version",))
    help_result = command_runner(("--help",))
    login_help_result = command_runner(("login", "--help"))
    version = _safe_version(version_result.stdout)
    installed = version_result.exit_code != 127
    login_available = _help_has_subcommand(help_result.stdout, "login") or _help_has_subcommand(
        login_help_result.stdout, "status"
    )
    status_available = _help_has_subcommand(login_help_result.stdout, "status")
    if not installed:
        return _card(
            installed=False,
            available=False,
            authenticated=False,
            reason_code="codex_cli_not_installed",
            version=None,
            last_probe_at=probed_at,
            official_auth_surface_available=False,
            activation_proof_state="not_proven",
        )
    if not login_available or not status_available:
        return _card(
            installed=installed,
            available=installed,
            authenticated=False,
            reason_code="codex_official_auth_surface_unavailable",
            version=version,
            last_probe_at=probed_at,
            official_auth_surface_available=False,
            activation_proof_state="not_proven",
        )

    status_result = command_runner(("login", "status"))
    status_text = f"{status_result.stdout}\n{status_result.stderr}"
    if _contains_secret_shape(status_text):
        return _card(
            installed=True,
            available=True,
            authenticated=False,
            reason_code="codex_session_status_redacted",
            version=version,
            last_probe_at=probed_at,
            official_auth_surface_available=True,
            activation_proof_state="not_proven",
        )
    authenticated, reason_code = _classify_status_text(status_text)
    return _card(
        installed=True,
        available=True,
        authenticated=authenticated,
        reason_code=reason_code,
        version=version,
        last_probe_at=probed_at,
        official_auth_surface_available=True,
        activation_proof_state="session_authenticated" if authenticated else "not_proven",
    )
