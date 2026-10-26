"""Read-only inspection of one frozen Journal request and explicit local runtime.

File configuration is captured without inspection and initialized lazily. An
explicit server composition may instead supply its already-pinned V2 runtime.
Observed drift is latched until application restart. A preview is never an
approval or execution receipt.
"""
import json
import os
from pathlib import Path
import stat
from threading import Lock
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from app.domain.journal_dispatch import AdapterBinding
from app.domain.temporal_memory import digest
from app.repositories import journal_executions
from app.services.providers.codex_journal_plan import ADAPTER_ID, build_plan
from app.services.providers.codex_journal_process import CliRuntime

CONFIG_ENV = "XRML_CODEX_JOURNAL_PREVIEW_CONFIG"
COMPATIBILITY_ENV = "XRML_CODEX_JOURNAL_COMPATIBILITY_REPORT"
MAX_CONFIG_BYTES = 65_536


class _PreviewConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    launcher: list[str] = Field(min_length=1, max_length=2)
    environment: dict[str, str]
    workspace: str
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
    timeout_seconds: float = Field(gt=0, le=120, allow_inf_nan=False)


class PreviewConfig(_PreviewConfig):
    schema_version: Literal["CODEX_JOURNAL_PREVIEW_CONFIG_V1"]


class PreviewConfigV2(_PreviewConfig):
    schema_version: Literal["CODEX_JOURNAL_PREVIEW_CONFIG_V2"]


Profile = Literal["CODEX_JOURNAL_CLI_V1", "CODEX_JOURNAL_CLI_V2"]
Configuration = PreviewConfig | PreviewConfigV2


def read_configuration(value: Any) -> Configuration:
    model = PreviewConfigV2 if isinstance(value, dict) and value.get("schema_version") == "CODEX_JOURNAL_PREVIEW_CONFIG_V2" else PreviewConfig
    return model.model_validate(value)


def configuration_profile(configuration: Configuration) -> Profile:
    return "CODEX_JOURNAL_CLI_V2" if isinstance(configuration, PreviewConfigV2) else "CODEX_JOURNAL_CLI_V1"


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError()
        value[key] = item
    return value


def _invalid_constant(_value: str) -> None:
    raise ValueError()


def _launcher_outside_profile(launcher: tuple[str, ...], environment: Mapping[str, str]) -> None:
    # An explicitly configured launcher is hashed by CliRuntime. Check resolved
    # targets using metadata first, including on each later verification, so a
    # changed parent alias cannot turn that hash into a profile-content read.
    home_path = Path(environment.get("CODEX_HOME", ""))
    if not home_path.is_absolute():
        raise ValueError()
    home = home_path.resolve(strict=True)
    if not home.is_dir():
        raise ValueError()
    for value in launcher:
        path = Path(value)
        if not path.is_absolute() or path.resolve(strict=True).is_relative_to(home):
            raise ValueError()


class JournalCodexPreview:
    def __init__(self, configuration_path: str | None, compatibility_report_path: str | None = None) -> None:
        # Keep the caller's path as text: construction does not resolve, stat,
        # read a configuration/profile, or discover/execute an installed CLI.
        self._configuration_path = configuration_path
        self._compatibility_report_path = compatibility_report_path
        self._lock = Lock()
        self._runtime: CliRuntime | None = None
        self._configuration: Configuration | None = None
        self._pinned_path: Path | None = None
        self._pinned_bytes: bytes | None = None
        self._changed: str | None = None
        self._explicit_runtime = False

    @classmethod
    def from_environment(cls) -> "JournalCodexPreview":
        return cls(os.environ.get(CONFIG_ENV), os.environ.get(COMPATIBILITY_ENV))

    @classmethod
    def from_runtime(cls, runtime: CliRuntime, *, reasoning_effort: str, timeout_seconds: float,
                     compatibility_report_path: str | None = None) -> "JournalCodexPreview":
        """Use one explicit V2 execution runtime without inheriting process settings."""
        if not isinstance(runtime, CliRuntime):
            raise ValueError("codex_preview_runtime_invalid")
        configuration = PreviewConfigV2.model_validate(dict(schema_version="CODEX_JOURNAL_PREVIEW_CONFIG_V2",
            launcher=list(runtime.launcher), environment=dict(runtime.environment), workspace=str(runtime.workspace),
            reasoning_effort=reasoning_effort, timeout_seconds=timeout_seconds))
        preview = cls(None, compatibility_report_path)
        preview._runtime, preview._configuration = runtime, configuration
        preview._explicit_runtime = True
        changed = preview._verify()
        if changed:
            raise ValueError(changed)
        return preview

    def compatibility(self, topic: str, job_id: str, execution_id: str, viewing_actor_id: str) -> dict[str, Any]:
        # This route only reads a separately generated operator observation.
        # It never runs the checker, even if the observation is missing/stale.
        from app.services.codex_cli_compatibility import CompatibilityReport, requirements_digest
        preview = self.inspect(topic, job_id, execution_id, viewing_actor_id)
        result = dict(schema_version="CODEX_JOURNAL_COMPATIBILITY_V1", execution_id=execution_id,
            contract_digest=preview["contract_digest"], actor_id=preview["actor_id"],
            observed_sequence=preview["observed_sequence"], execution_allowed=False, model_invoked=False,
            status="NOT_CHECKED", reason="report_not_configured", report=None)
        if not preview["available"]:
            return result | dict(status="UNAVAILABLE", reason=preview["reason"])
        if not self._compatibility_report_path:
            return result
        with self._lock:
            changed = self._verify()
            if changed:
                return result | dict(status="UNAVAILABLE", reason=changed)
            assert self._runtime is not None
            try:
                path = Path(self._compatibility_report_path)
                if not path.is_absolute():
                    raise ValueError()
                path = path.resolve(strict=True)
                home = Path(dict(self._runtime.environment)["CODEX_HOME"]).resolve(strict=True)
                if path.is_relative_to(home):
                    raise ValueError()
                info = path.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONFIG_BYTES:
                    raise ValueError()
                with path.open("rb") as stream:
                    raw = stream.read(MAX_CONFIG_BYTES + 1)
                if len(raw) > MAX_CONFIG_BYTES:
                    raise ValueError()
                json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs, parse_constant=_invalid_constant)
                report = CompatibilityReport.model_validate_json(raw)
            except (OSError, ValueError, RuntimeError):
                return result | dict(status="INVALID", reason="report_unreadable")
            changed = self._verify()
            if changed:
                return result | dict(status="UNAVAILABLE", reason=changed)
            if (report.profile != preview["manifest"]["schema_version"]
                    or report.runtime_identity.model_dump() != preview["manifest"]["runtime_identity"]
                    or report.requirements_sha256 != requirements_digest(report.profile)):
                return result | dict(status="STALE", reason="report_binding_changed")
            return result | dict(status=report.status, reason=report.reason, report=report.model_dump(mode="json"))

    def _read_configuration(self) -> tuple[Path, bytes]:
        path = Path(self._configuration_path or "")
        if not path.is_absolute():
            raise ValueError()
        resolved = path.resolve(strict=True)
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONFIG_BYTES:
            raise ValueError()
        with resolved.open("rb") as stream:
            raw = stream.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise ValueError()
        return resolved, raw

    def _initialize(self, path: Path, raw: bytes) -> None:
        configuration = read_configuration(json.loads(raw.decode("utf-8"),
            object_pairs_hook=_unique_pairs, parse_constant=_invalid_constant))
        _launcher_outside_profile(tuple(configuration.launcher), configuration.environment)
        runtime = CliRuntime(tuple(configuration.launcher), configuration.environment, Path(configuration.workspace))
        self._runtime = runtime
        self._configuration = configuration
        self._pinned_path, self._pinned_bytes = path, raw

    def _verify(self) -> str | None:
        assert self._runtime is not None
        if not self._explicit_runtime:
            try:
                current_path, current_bytes = self._read_configuration()
                if current_path != self._pinned_path or current_bytes != self._pinned_bytes:
                    raise ValueError()
            except (OSError, ValueError, RuntimeError):
                self._changed = "codex_preview_configuration_changed"
                return self._changed
        try:
            _launcher_outside_profile(self._runtime.launcher, dict(self._runtime.environment))
            self._runtime.verify()
        except (OSError, ValueError, RuntimeError):
            self._changed = "codex_preview_runtime_changed"
        return self._changed

    def inspect(self, topic: str, job_id: str, execution_id: str, viewing_actor_id: str) -> dict[str, Any]:
        execution = journal_executions.detail(topic, job_id, execution_id)
        contract = execution["contract"]
        envelope = dict(schema_version="CODEX_JOURNAL_PREVIEW_V1", preview_only=True, execution_allowed=False,
            execution_id=execution_id, contract_digest=digest(contract), actor_id=contract["actor_id"],
            observed_sequence=execution["current"]["sequence"], available=False, reason=None,
            manifest=None, binding=None, preview_digest=None)
        if contract["specification"]["provider_name"] != "codex_cli":
            return envelope | dict(reason="codex_preview_provider_unsupported")
        if not self._configuration_path and not self._explicit_runtime:
            return envelope | dict(reason="codex_preview_not_configured")
        with self._lock:
            if self._changed:
                return envelope | dict(reason=self._changed)
            if self._runtime is None:
                try:
                    self._initialize(*self._read_configuration())
                except (OSError, ValueError, TypeError, RuntimeError):
                    # A bad initial configuration can be corrected and retried.
                    return envelope | dict(reason="codex_preview_configuration_invalid")
            changed = self._verify()
            if changed:
                return envelope | dict(reason=changed)
            assert self._runtime is not None and self._configuration is not None
            builder = build_plan
            version = "1"
            if configuration_profile(self._configuration) == "CODEX_JOURNAL_CLI_V2":
                from app.services.providers.codex_journal_plan_v2 import build_plan as build_v2
                builder, version = build_v2, "2"
            try:
                _, _, manifest = builder(contract, runtime_identity=self._runtime.identity(),
                    reasoning_effort=self._configuration.reasoning_effort,
                    timeout_seconds=self._configuration.timeout_seconds)
            except ValueError:
                return envelope | dict(reason=self._verify() or "codex_preview_contract_unsupported")
            changed = self._verify()
            if changed:
                return envelope | dict(reason=changed)
            binding = AdapterBinding(surface_id="codex_cli", adapter_id=ADAPTER_ID, adapter_version=version,
                command_shape_sha256=digest(manifest)).model_dump()
            preview_digest = digest(dict(schema_version="CODEX_JOURNAL_PREVIEW_V1", topic_id=topic, job_id=job_id,
                execution_id=execution_id, contract_digest=envelope["contract_digest"], actor_id=contract["actor_id"],
                viewing_actor_id=viewing_actor_id, manifest=manifest, binding=binding))
            return envelope | dict(available=True, manifest=manifest, binding=binding, preview_digest=preview_digest)
