"""Explicit Antigravity child; no login, token/config reads or home replacement.

Only bounded capture/owned process cleanup is shared with Codex. Global account
configuration and hooks are not isolated by this fake-qualified adapter.
"""
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Mapping

from app.domain.journal_exchange import preserved_json, sha
from app.services.codex_cli_report_draft import CliExecResult
from app.services.providers.antigravity_prepared_review_plan import AGENT_NAME, FIXED_ENV, agent_text, argv
from app.services.providers.base import ProviderError
from app.services.providers.codex_journal_process import capture

PROFILE_KEYS = {"HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH"}
ENV_KEYS = {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "PYTHONIOENCODING"} | PROFILE_KEYS


def _workspace(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(Path(__file__).resolve().parents[3]):
        raise ValueError()
    for parent in (resolved, *resolved.parents):
        try:
            mode = parent.stat().st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(mode):
            raise ValueError()
        return resolved
    raise ValueError()


@dataclass(frozen=True)
class CapturedAttempt:
    process: CliExecResult
    attempt_cwd: str


@dataclass(frozen=True, init=False)
class AntigravityRuntime:
    launcher: tuple[str, ...] = field(repr=False)
    environment: tuple[tuple[str, str], ...] = field(repr=False)
    workspace: Path = field(repr=False)
    _identity_json: str = field(repr=False)

    def __init__(self, launcher: tuple[str, ...], environment: Mapping[str, str], workspace: Path) -> None:
        try:
            if (not isinstance(launcher, tuple) or not 1 <= len(launcher) <= 2
                    or any(not isinstance(item, str) or not Path(item).is_absolute()
                           or Path(item).suffix.lower() in {".cmd", ".bat"} for item in launcher)
                    or not isinstance(workspace, Path) or not workspace.is_absolute()
                    or any((key not in ENV_KEYS and key not in FIXED_ENV) or not isinstance(value, str) or "\x00" in value
                           or (key in FIXED_ENV and value != FIXED_ENV[key])
                           or (key in PROFILE_KEYS and value != os.environ.get(key)) for key, value in environment.items())):
                raise ValueError()
            object.__setattr__(self, "launcher", launcher)
            object.__setattr__(self, "environment", tuple(sorted((dict(environment) | FIXED_ENV).items())))
            object.__setattr__(self, "workspace", _workspace(workspace))
            object.__setattr__(self, "_identity_json", preserved_json(self._identity()))
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
            raise ValueError("paper_antigravity_runtime_invalid") from None

    def _identity(self) -> dict[str, str]:
        files = []
        for name in self.launcher:
            path = Path(name)
            if not path.is_file() or path.is_symlink():
                raise ValueError()
            with path.open("rb") as stream:
                files.append([str(path.resolve()), hashlib.file_digest(stream, "sha256").hexdigest()])
        if (_workspace(self.workspace) != self.workspace
                or any(value != os.environ.get(key) for key, value in self.environment if key in PROFILE_KEYS)):
            raise ValueError()
        return dict(launcher_sha256=sha(preserved_json(files)), environment_sha256=sha(preserved_json(self.environment)),
                    workspace_sha256=sha(str(self.workspace)))

    def identity(self) -> dict[str, str]:
        return json.loads(self._identity_json)

    def verify(self) -> None:
        try:
            if preserved_json(self._identity()) != self._identity_json:
                raise ValueError()
        except (OSError, RuntimeError, ValueError):
            raise ValueError("paper_antigravity_runtime_changed") from None

    def run(self, stdin: str, schema: str, *, model: str, timeout_seconds: float) -> CapturedAttempt:
        self.verify()
        self.workspace.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="antigravity-review-", dir=self.workspace) as attempt:
            root = Path(attempt)
            if root.resolve().parent != self.workspace:
                raise ValueError("paper_antigravity_runtime_changed")
            request = json.loads(json.loads(stdin)["message"]["content"])
            agents = root / ".agents" / "agents"
            agents.mkdir(parents=True)
            (agents / (AGENT_NAME + ".md")).write_text(agent_text(request["instructions"]), encoding="utf-8", newline="")
            schema_file = root / "output-schema.json"
            schema_file.write_text(schema, encoding="utf-8", newline="")
            try:
                result = capture(argv(self.launcher, str(schema_file), model, timeout_seconds), stdin, root, dict(self.environment), timeout_seconds)
            except ProviderError:
                raise ProviderError("paper_antigravity_process_outcome_unknown") from None
            if result.last_message:
                raise ProviderError("paper_antigravity_process_outcome_unknown")
            return CapturedAttempt(result, str(root))
