"""User-visible report_draft_assist provider registry.

Selection is persisted under XRML_DATA_DIR when configured. The live Codex
generation path stays on the existing prototype_live_provider contract.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, Mapping

from app.domain.constants import PROJECT_ROOT
from app.repositories.files import read_json, write_json
from app.services.codex_session_probe import CommandRunner, probe_codex_session

SCHEMA_VERSION = "report_draft_assist_registry_v1"
STAGE = "report_draft_assist"
DETERMINISTIC_PROVIDER_ID = "deterministic_local"
CODEX_PROVIDER_ID = "codex"
ALLOWED_PROVIDER_IDS = (DETERMINISTIC_PROVIDER_ID, CODEX_PROVIDER_ID)
SELECTION_FILENAME = "selection.json"


class ReportDraftAssistError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 409) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


_PROBE_CACHE: dict[str, Any] | None = None


def reset_report_draft_assist_state_for_tests() -> None:
    """Clear process-local probe cache; production code never calls this."""

    global _PROBE_CACHE
    _PROBE_CACHE = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _runtime_base() -> Path:
    configured = str(os.getenv("XRML_DATA_DIR", "") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return PROJECT_ROOT / "storage"


def selection_path() -> Path:
    return _runtime_base() / "report-draft-assist" / SELECTION_FILENAME


def _read_selection() -> dict[str, Any]:
    payload = read_json(selection_path(), default=None)
    if not isinstance(payload, dict):
        return {
            "schema_version": SCHEMA_VERSION,
            "stage": STAGE,
            "selected_provider_id": DETERMINISTIC_PROVIDER_ID,
            "updated_at": None,
        }
    selected = str(payload.get("selected_provider_id") or DETERMINISTIC_PROVIDER_ID)
    if selected not in ALLOWED_PROVIDER_IDS:
        selected = DETERMINISTIC_PROVIDER_ID
    return {
        "schema_version": SCHEMA_VERSION,
        "stage": STAGE,
        "selected_provider_id": selected,
        "updated_at": payload.get("updated_at"),
    }


def _write_selection(provider_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "stage": STAGE,
        "selected_provider_id": provider_id,
        "updated_at": _iso(now or _utc_now()),
    }
    write_json(selection_path(), payload)
    return payload


def _deterministic_card(*, last_probe_at: str | None) -> dict[str, Any]:
    return {
        "provider_id": DETERMINISTIC_PROVIDER_ID,
        "display_name": "Deterministic local fallback",
        "surface": "local",
        "installed": True,
        "available": True,
        "authenticated": True,
        "activation_proof_state": "not_required",
        "supported_stages": [STAGE],
        "supported_output_classes": ["weekly_report"],
        "version": None,
        "last_probe_at": last_probe_at,
        "reason_code": "deterministic_local_ready",
        "official_login_command": None,
        "official_status_command": None,
        "official_auth_surface_available": False,
        "deterministic_fallback_available": True,
    }


def _cached_or_unprobed_codex() -> dict[str, Any]:
    if isinstance(_PROBE_CACHE, dict):
        return dict(_PROBE_CACHE)
    return {
        "provider_id": CODEX_PROVIDER_ID,
        "display_name": "Codex",
        "surface": "cli",
        "installed": False,
        "available": False,
        "authenticated": False,
        "activation_proof_state": "not_probed",
        "supported_stages": [STAGE],
        "supported_output_classes": ["report_draft", "weekly_report"],
        "version": None,
        "last_probe_at": None,
        "reason_code": "codex_session_not_probed",
        "official_login_command": "codex login",
        "official_status_command": "codex login status",
        "official_auth_surface_available": True,
        "deterministic_fallback_available": True,
    }


def _live_lane_ready() -> bool:
    return (
        os.getenv("XRML_PROTOTYPE_MODE", "").strip() == "1"
        and os.getenv("XRML_PROTOTYPE_LIVE_PROVIDER_PROOF", "").strip() == "1"
        and bool(os.getenv("XRML_PROTOTYPE_SESSION_ID", "").strip())
    )


def _activation_state(selected: str, codex: Mapping[str, Any]) -> str:
    if selected != CODEX_PROVIDER_ID:
        return "inactive"
    if not bool(codex.get("authenticated")):
        return "inactive"
    if _live_lane_ready():
        return "activated"
    return "eligible"


def describe_report_draft_assist(*, refresh: bool = False, runner: CommandRunner | None = None) -> dict[str, Any]:
    global _PROBE_CACHE
    if refresh:
        _PROBE_CACHE = probe_codex_session(runner=runner)
    selection = _read_selection()
    codex = _cached_or_unprobed_codex()
    last_probe_at = str(codex.get("last_probe_at") or "") or None
    selected = str(selection["selected_provider_id"])
    activation_state = _activation_state(selected, codex)
    return {
        "schema_version": SCHEMA_VERSION,
        "stage": STAGE,
        "selected_provider_id": selected,
        "activation_state": activation_state,
        "activation_proof_state": (
            "live_lane_ready"
            if activation_state == "activated"
            else ("session_authenticated" if bool(codex.get("authenticated")) else str(codex.get("activation_proof_state") or "not_proven"))
        ),
        "last_probe_at": last_probe_at,
        "deterministic_fallback_available": True,
        "providers": [
            _deterministic_card(last_probe_at=last_probe_at),
            {
                **codex,
                "activation_proof_state": (
                    "live_lane_ready"
                    if selected == CODEX_PROVIDER_ID and activation_state == "activated"
                    else codex.get("activation_proof_state")
                ),
            },
        ],
    }


def refresh_report_draft_assist(*, runner: CommandRunner | None = None) -> dict[str, Any]:
    return describe_report_draft_assist(refresh=True, runner=runner)


def select_report_draft_provider(
    provider_id: str,
    *,
    runner: CommandRunner | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if provider_id not in ALLOWED_PROVIDER_IDS:
        raise ReportDraftAssistError("report_draft_assist_provider_unknown", status_code=400)
    if provider_id == CODEX_PROVIDER_ID:
        snapshot = describe_report_draft_assist(refresh=_PROBE_CACHE is None or runner is not None, runner=runner)
        codex = next(item for item in snapshot["providers"] if item["provider_id"] == CODEX_PROVIDER_ID)
        if not bool(codex.get("official_auth_surface_available")) and not bool(codex.get("authenticated")):
            if str(codex.get("reason_code")) == "codex_official_auth_surface_unavailable":
                raise ReportDraftAssistError("codex_official_auth_surface_unavailable")
        if not bool(codex.get("installed")) and not bool(codex.get("authenticated")):
            raise ReportDraftAssistError(str(codex.get("reason_code") or "codex_cli_not_installed"))
        if not bool(codex.get("authenticated")):
            raise ReportDraftAssistError(str(codex.get("reason_code") or "codex_session_unauthenticated"))
    _write_selection(provider_id, now=now)
    return describe_report_draft_assist(refresh=False, runner=runner)


def require_codex_selected_for_live_generation() -> dict[str, Any]:
    """Fail closed when the owner explicitly selected deterministic fallback.

    No selection file means the existing Codex live lane remains usable so
    current tests and the established prototype proof panel keep working.
    """

    if not selection_path().is_file():
        return describe_report_draft_assist(refresh=False)
    snapshot = describe_report_draft_assist(refresh=False)
    if snapshot["selected_provider_id"] != CODEX_PROVIDER_ID:
        raise ReportDraftAssistError("report_draft_assist_codex_not_selected")
    codex = next(item for item in snapshot["providers"] if item["provider_id"] == CODEX_PROVIDER_ID)
    if snapshot["activation_state"] == "inactive" and not bool(codex.get("authenticated")):
        if str(codex.get("reason_code")) not in {"codex_session_not_probed"}:
            raise ReportDraftAssistError(str(codex.get("reason_code") or "codex_session_unauthenticated"))
    return snapshot
