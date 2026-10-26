"""Explicit, isolated CLI syntax observations; never execution permission.

The checker runs only version/help/features commands in fresh disposable homes.
It cannot qualify effective exec isolation, authentication, model support, or
internal retries. V1 plans and historical readers never consult this module.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.journal_exchange import preserved_json
from app.services.journal_codex_preview import (
    MAX_CONFIG_BYTES, Configuration, Profile, configuration_profile, read_configuration,
    _invalid_constant, _launcher_outside_profile, _unique_pairs,
)
from app.services.providers.base import ProviderError
from app.services.providers.codex_journal_plan import CONFIG, argv, sha
from app.services.providers.codex_journal_process import CliRuntime, capture

MAX_REPORT_BYTES = 65_536
PROBE_TIMEOUT_SECONDS = 10.0
_REPO = Path(__file__).resolve().parents[2]
_VERSION = r"codex-cli [0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9][A-Za-z0-9.+-]*)?"
CheckName = Literal["version", "exec_help", "configuration"]
Status = Literal["CONFIGURATION_REJECTED", "INCOMPLETE", "CONFIGURATION_OBSERVED"]
Reason = Literal["config_rejected", "required_flags_missing", "probe_failed", "invalid_version"]
_CHECK_NAMES: tuple[CheckName, ...] = ("version", "exec_help", "configuration")
Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$", min_length=64, max_length=64)]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class RuntimeIdentity(_Frozen):
    launcher_sha256: Hash
    environment_sha256: Hash
    workspace_sha256: Hash


class CompatibilityCheck(_Frozen):
    name: CheckName
    exit_code: int
    stdout_sha256: Hash


class CompatibilityReport(_Frozen):
    schema_version: Literal["CODEX_CLI_COMPATIBILITY_V1", "CODEX_CLI_COMPATIBILITY_V2"] = "CODEX_CLI_COMPATIBILITY_V1"
    profile: Profile = "CODEX_JOURNAL_CLI_V1"
    requirements_sha256: Hash
    runtime_identity: RuntimeIdentity
    checked_at: AwareDatetime
    cli_version: Annotated[str, Field(pattern="^" + _VERSION + "$", max_length=100)] | None
    status: Status
    reason: Reason | None
    checks: tuple[CompatibilityCheck, ...] = Field(max_length=3)
    execution_allowed: Literal[False] = False
    model_invoked: Literal[False] = False

    @field_validator("execution_allowed", "model_invoked", mode="before")
    @classmethod
    def exact_false(cls, value: object) -> Literal[False]:
        if value is not False:
            raise ValueError("compatibility_boolean_invalid")
        return False

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.schema_version != _report_schema(self.profile):
            raise ValueError("compatibility_profile_invalid")
        if self.cli_version is not None and re.fullmatch(_VERSION, self.cli_version) is None:
            raise ValueError("compatibility_version_invalid")
        names = tuple(check.name for check in self.checks)
        if names != _CHECK_NAMES[:len(names)]:
            raise ValueError("compatibility_checks_invalid")
        if self.cli_version is not None and (not self.checks or self.checks[0].exit_code != 0):
            raise ValueError("compatibility_version_invalid")
        if len(self.checks) > 1 and (self.cli_version is None or self.checks[0].exit_code != 0):
            raise ValueError("compatibility_checks_invalid")
        if len(self.checks) == 3 and self.checks[1].exit_code != 0:
            raise ValueError("compatibility_checks_invalid")
        if self.status == "CONFIGURATION_OBSERVED":
            valid = self.reason is None and len(self.checks) == 3 and self.cli_version is not None and all(
                check.exit_code == 0 for check in self.checks)
        elif self.status == "CONFIGURATION_REJECTED":
            valid = (self.reason == "config_rejected" and len(self.checks) == 3
                     and self.cli_version is not None and self.checks[-1].exit_code != 0)
        elif self.reason == "invalid_version":
            valid = len(self.checks) == 1 and self.checks[0].exit_code == 0 and self.cli_version is None
        elif self.reason == "required_flags_missing":
            valid = len(self.checks) == 2 and self.checks[-1].exit_code == 0 and self.cli_version is not None
        elif self.reason == "probe_failed":
            valid = len(self.checks) < 3 and (not self.checks or self.cli_version is not None
                                            or self.checks[0].exit_code != 0)
        else:
            valid = False
        if not valid:
            raise ValueError("compatibility_status_invalid")
        return self


def _report_schema(profile: Profile) -> Literal["CODEX_CLI_COMPATIBILITY_V1", "CODEX_CLI_COMPATIBILITY_V2"]:
    return "CODEX_CLI_COMPATIBILITY_V2" if profile == "CODEX_JOURNAL_CLI_V2" else "CODEX_CLI_COMPATIBILITY_V1"


def _profile_settings(profile: Profile):
    if profile == "CODEX_JOURNAL_CLI_V1":
        return CONFIG, argv
    if profile == "CODEX_JOURNAL_CLI_V2":
        from app.services.providers.codex_cli_profile_v2 import CONFIG as config_v2, argv as argv_v2
        return config_v2, argv_v2
    raise ValueError("codex_compatibility_profile_invalid")


def _required_flags(profile: Profile = "CODEX_JOURNAL_CLI_V1") -> tuple[str, ...]:
    _, command = _profile_settings(profile)
    return tuple(sorted({value for value in command(("{launcher}",), "{cwd}", "{model}", "{effort}")
                         if value.startswith("-") and value != "-"}))


def requirements_digest(profile: Profile = "CODEX_JOURNAL_CLI_V1") -> str:
    settings, command = _profile_settings(profile)
    return sha(preserved_json(dict(profile=profile, config=list(settings),
        argv_shape=command(("{launcher}",), "{cwd}", "{model}", "{effort}"), required_flags=_required_flags(profile))))


def _absolute(value: Path | str, *, exists: bool = False) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("codex_compatibility_path_invalid")
    for component in (path, *path.parents):
        if component.is_symlink() or getattr(component, "is_junction", lambda: False)():
            raise ValueError("codex_compatibility_path_invalid")
    resolved = path.resolve(strict=exists)
    if resolved != path:
        raise ValueError("codex_compatibility_path_invalid")
    return resolved


def _configuration(path: Path | str) -> tuple[Path, bytes, Configuration]:
    try:
        resolved = _absolute(path, exists=True)
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONFIG_BYTES:
            raise ValueError()
        with resolved.open("rb") as stream:
            raw = stream.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise ValueError()
        config = read_configuration(json.loads(raw.decode("utf-8"),
            object_pairs_hook=_unique_pairs, parse_constant=_invalid_constant))
        _launcher_outside_profile(tuple(config.launcher), config.environment)
        for launcher in config.launcher:
            if _absolute(launcher, exists=True).is_relative_to(_REPO):
                raise ValueError()
        return resolved, raw, config
    except (OSError, ValueError, TypeError, RuntimeError):
        raise ValueError("codex_compatibility_configuration_invalid") from None


def _runtime(config: Configuration) -> CliRuntime:
    return CliRuntime(tuple(config.launcher), config.environment, Path(config.workspace))


def _outside(path: Path, protected: tuple[Path, ...]) -> None:
    if any(path.is_relative_to(root) or root.is_relative_to(path) for root in protected):
        raise ValueError("codex_compatibility_path_invalid")


def _scratch(value: Path | str, config: Configuration, config_path: Path) -> Path:
    root = _absolute(value)
    protected = (_REPO, Path(config.environment["CODEX_HOME"]).resolve(strict=True),
                 Path(config.workspace).resolve(), config_path, *(Path(name).resolve() for name in config.launcher))
    _outside(root, protected)
    if not root.parent.is_dir():
        raise ValueError("codex_compatibility_path_invalid")
    return root


def validate_output_path(output: Path | str, *, config_path: Path | str, scratch_root: Path | str) -> Path:
    """Reject an unsafe/existing output before any probe or exclusive write."""
    path, _, config = _configuration(config_path)
    scratch = _scratch(scratch_root, config, path)
    target = _absolute(output)
    _outside(target, (_REPO, Path(config.environment["CODEX_HOME"]).resolve(strict=True),
                     Path(config.workspace).resolve(), scratch, path,
                     *(Path(name).resolve() for name in config.launcher)))
    if target.exists() or not target.parent.is_dir():
        raise ValueError("codex_compatibility_output_invalid")
    return target


def inspect(config_path: Path | str, scratch_root: Path | str) -> CompatibilityReport:
    """Run three bounded non-agent commands only when explicitly called.

    scratch_root must be a new task-owned absolute directory. It remains empty
    after probes; each command gets its own fresh child home and working folder.
    """
    path, raw, config = _configuration(config_path)
    profile = configuration_profile(config)
    settings, _ = _profile_settings(profile)
    runtime = _runtime(config)
    scratch = _scratch(scratch_root, config, path)
    try:
        scratch.mkdir()  # Exclusive ownership: never reuse an existing profile.
    except OSError:
        raise ValueError("codex_compatibility_scratch_invalid") from None
    checks: list[CompatibilityCheck] = []
    version: str | None = None

    def verify() -> None:
        current_path, current_raw, _ = _configuration(config_path)
        if current_path != path or current_raw != raw:
            raise ValueError("codex_compatibility_configuration_changed")
        _absolute(scratch, exists=True)
        _launcher_outside_profile(runtime.launcher, dict(runtime.environment))
        runtime.verify()

    def report(status: Status, reason: Reason | None) -> CompatibilityReport:
        verify()
        return CompatibilityReport(schema_version=_report_schema(profile), profile=profile,
            requirements_sha256=requirements_digest(profile),
            runtime_identity=RuntimeIdentity(**runtime.identity()), checked_at=datetime.now(timezone.utc),
            cli_version=version, status=status, reason=reason, checks=tuple(checks))

    commands = (("--version",), ("exec", "--help"),
                ("features", "list", *[part for setting in settings for part in ("-c", setting)]))
    for name, arguments in zip(_CHECK_NAMES, commands, strict=True):
        verify()
        attempt = Path(tempfile.mkdtemp(prefix=name + "-", dir=scratch))
        try:
            if _absolute(attempt, exists=True).parent != scratch:
                raise ValueError("codex_compatibility_path_invalid")
            environment = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
            for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "CODEX_HOME", "TEMP", "TMP"):
                directory = attempt / key.lower()
                directory.mkdir()
                environment[key] = str(directory)
            cwd = attempt / "cwd"
            cwd.mkdir()
            try:
                result = capture([*runtime.launcher, *arguments], "", cwd, environment, PROBE_TIMEOUT_SECONDS)
            except ProviderError:
                return report("INCOMPLETE", "probe_failed")
            verify()
            if result.timed_out:
                return report("INCOMPLETE", "probe_failed")
            checks.append(CompatibilityCheck(name=name, exit_code=result.exit_code, stdout_sha256=sha(result.stdout)))
            if name == "version":
                if result.exit_code != 0:
                    return report("INCOMPLETE", "probe_failed")
                candidate = result.stdout.strip()
                if not re.fullmatch(_VERSION, candidate) or len(candidate) > 100:
                    return report("INCOMPLETE", "invalid_version")
                version = candidate
            elif name == "exec_help":
                if result.exit_code != 0:
                    return report("INCOMPLETE", "probe_failed")
                observed = set(re.findall(r"(?<![\w-])--?[A-Za-z][A-Za-z0-9-]*(?![\w-])", result.stdout))
                if not set(_required_flags(profile)).issubset(observed):
                    return report("INCOMPLETE", "required_flags_missing")
            elif result.exit_code != 0:
                return report("CONFIGURATION_REJECTED", "config_rejected")
        finally:
            # Verify every ancestor and the resolved target before owned cleanup.
            # A changed alias is left for inspection, never followed by rmtree.
            if _absolute(attempt, exists=True).parent != _absolute(scratch, exists=True):
                raise ValueError("codex_compatibility_path_invalid")
            shutil.rmtree(attempt)
    return report("CONFIGURATION_OBSERVED", None)


def write_report(output: Path | str, report: CompatibilityReport, *, config_path: Path | str,
                 scratch_root: Path | str) -> None:
    target = validate_output_path(output, config_path=config_path, scratch_root=scratch_root)
    _, _, config = _configuration(config_path)
    runtime = _runtime(config)
    runtime.verify()
    checked = CompatibilityReport.model_validate_json(report.model_dump_json())
    if (checked.profile != configuration_profile(config) or checked.runtime_identity.model_dump() != runtime.identity()
            or checked.requirements_sha256 != requirements_digest(checked.profile)):
        raise ValueError("codex_compatibility_runtime_changed")
    raw = (checked.model_dump_json(indent=2) + "\n").encode("utf-8")
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("codex_compatibility_report_invalid")
    with target.open("xb") as stream:
        stream.write(raw)
