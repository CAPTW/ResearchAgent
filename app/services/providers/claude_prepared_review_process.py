"""Explicit Claude Code child; official CLI owns auth, XRML never opens tokens.

The shared capture primitive supplies bounded pipes, deadline and owned process
cleanup only. No Codex argv, last-message format, account or authority is reused.
"""
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import stat
import tempfile
from typing import Mapping

from app.domain.journal_exchange import preserved_json, sha
from app.services.codex_cli_report_draft import CliExecResult
from app.services.providers.base import ProviderError
from app.services.providers.claude_prepared_review_plan import FIXED_ENV, argv
from app.services.providers.codex_journal_process import capture

ENV_KEYS = {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE",
            "APPDATA", "LOCALAPPDATA", "CLAUDE_CONFIG_DIR", "LANG", "LC_ALL", "PYTHONIOENCODING"}


def _directory(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError()
    return resolved


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


@dataclass(frozen=True, init=False)
class ClaudeRuntime:
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
                           or (key in FIXED_ENV and value != FIXED_ENV[key]) for key, value in environment.items())
                    or not environment.get("CLAUDE_CONFIG_DIR") or not Path(environment["CLAUDE_CONFIG_DIR"]).is_absolute()):
                raise ValueError()
            config = _directory(Path(environment["CLAUDE_CONFIG_DIR"]))
            work = _workspace(workspace)
            if config.is_relative_to(work) or work.is_relative_to(config):
                raise ValueError()
            object.__setattr__(self, "launcher", launcher)
            child_environment = dict(environment, CLAUDE_CONFIG_DIR=str(config)) | FIXED_ENV
            object.__setattr__(self, "environment", tuple(sorted(child_environment.items())))
            object.__setattr__(self, "workspace", work)
            object.__setattr__(self, "_identity_json", preserved_json(self._identity()))
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
            raise ValueError("paper_claude_runtime_invalid") from None

    def _identity(self) -> dict[str, str]:
        files = []
        for name in self.launcher:
            path = Path(name)
            if not path.is_file() or path.is_symlink():
                raise ValueError()
            with path.open("rb") as stream:
                files.append([str(path.resolve()), hashlib.file_digest(stream, "sha256").hexdigest()])
        config = Path(dict(self.environment)["CLAUDE_CONFIG_DIR"])
        if _directory(config) != config or _workspace(self.workspace) != self.workspace:
            raise ValueError()
        metadata = config.stat()
        # Directory identity detects replacement without reading any config/token.
        return dict(launcher_sha256=sha(preserved_json(files)), environment_sha256=sha(preserved_json(self.environment)),
                    workspace_sha256=sha(str(self.workspace)),
                    config_dir_sha256=sha(preserved_json([str(config), metadata.st_dev, metadata.st_ino])))

    def identity(self) -> dict[str, str]:
        return json.loads(self._identity_json)

    def verify(self) -> None:
        try:
            if preserved_json(self._identity()) != self._identity_json:
                raise ValueError()
        except (OSError, RuntimeError, ValueError):
            raise ValueError("paper_claude_runtime_changed") from None

    def run(self, stdin: str, schema: str, *, model: str, timeout_seconds: float) -> CliExecResult:
        self.verify()
        self.workspace.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="claude-review-", dir=self.workspace) as attempt:
            root = Path(attempt)
            if root.resolve().parent != self.workspace:
                raise ValueError("paper_claude_runtime_changed")
            instructions = json.loads(stdin)["instructions"]
            try:
                result = capture(argv(self.launcher, model, schema, instructions), stdin, root, dict(self.environment), timeout_seconds)
            except ProviderError:
                raise ProviderError("paper_claude_process_outcome_unknown") from None
            if result.last_message:
                raise ProviderError("paper_claude_process_outcome_unknown")
            return result
