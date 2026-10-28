"""One explicit child process with bounded capture; no CLI/account discovery.

Only the caller's environment is used. The runtime does not read/copy credentials.
Timeout or capture failure never proves that a remote operation did not occur.
"""
from dataclasses import dataclass, field
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Mapping

from app.domain.journal_exchange import preserved_json
from app.services.codex_cli_report_draft import CliExecResult
from app.services.providers.base import ProviderError
from app.services.providers.codex_journal_job import WindowsJob
from app.services.providers.codex_journal_plan import (
    MAX_FINAL_BYTES, MAX_STDERR_BYTES, MAX_STREAM_BYTES, argv, sha,
)

# Auth uses the explicit CODEX_HOME through the official CLI. Proxy, API-key,
# NODE_OPTIONS, PYTHONPATH and provider-routing environment variables are absent.
ENV_KEYS = {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE",
            "APPDATA", "LOCALAPPDATA", "CODEX_HOME", "LANG", "LC_ALL", "PYTHONIOENCODING"}


def _resolve_home(value: str) -> Path:
    try:
        home = Path(value).resolve(strict=True)
        if not home.is_dir():
            raise ValueError()
        return home
    except (OSError, RuntimeError, ValueError):
        raise ValueError("journal_codex_runtime_invalid") from None


def _resolve_workspace(value: Path) -> Path:
    try:
        workspace = value.resolve()
        # Missing directories are created only at run. An existing file in the
        # path cannot become a workspace; detect it before consuming a claim.
        for directory in (workspace, *workspace.parents):
            try:
                mode = directory.stat().st_mode
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(mode):
                raise ValueError()
            return workspace
        raise ValueError()
    except (OSError, RuntimeError, ValueError):
        raise ValueError("journal_codex_runtime_invalid") from None


@dataclass(frozen=True, init=False)
class CliRuntime:
    launcher: tuple[str, ...] = field(repr=False)
    environment: tuple[tuple[str, str], ...] = field(repr=False)
    workspace: Path = field(repr=False)
    _identity_json: str = field(repr=False)

    def __init__(self, launcher: tuple[str, ...], environment: Mapping[str, str], workspace: Path) -> None:
        if (not isinstance(launcher, tuple) or not 1 <= len(launcher) <= 2
                or any(not Path(value).is_absolute() or Path(value).suffix.lower() in {".cmd", ".bat"} for value in launcher)
                or not workspace.is_absolute()
                or any(key not in ENV_KEYS or not isinstance(value, str) or "\x00" in value for key, value in environment.items())
                or not environment.get("CODEX_HOME") or not Path(environment["CODEX_HOME"]).is_absolute()):
            raise ValueError("journal_codex_runtime_invalid")
        resolved_workspace = _resolve_workspace(workspace)
        if resolved_workspace.is_relative_to(Path(__file__).resolve().parents[3]):
            raise ValueError("journal_codex_runtime_invalid")
        # Bind the path actually passed to the child, not a mutable home alias.
        # Directory metadata is checked; credential/config contents are not read.
        child_environment = dict(environment, CODEX_HOME=str(_resolve_home(environment["CODEX_HOME"])))
        object.__setattr__(self, "launcher", launcher)
        object.__setattr__(self, "environment", tuple(sorted(child_environment.items())))
        object.__setattr__(self, "workspace", resolved_workspace)
        object.__setattr__(self, "_identity_json", preserved_json(self._identity()))

    def _identity(self) -> dict[str, str]:
        try:
            files = []
            for name in self.launcher:
                file = Path(name)
                if not file.is_file() or file.is_symlink():
                    raise ValueError()
                # Hash the executable/entrypoint, not only a mutable command name.
                import hashlib
                with file.open("rb") as stream:
                    content = hashlib.file_digest(stream, "sha256").hexdigest()
                files.append([str(file.resolve()), content])
            return dict(launcher_sha256=sha(preserved_json(files)),
                        environment_sha256=sha(preserved_json(self.environment)),
                        workspace_sha256=sha(str(self.workspace)))
        except (OSError, ValueError):
            raise ValueError("journal_codex_runtime_invalid") from None

    def identity(self) -> dict[str, str]:
        import json
        return json.loads(self._identity_json)

    def verify(self) -> None:
        home = Path(dict(self.environment)["CODEX_HOME"])
        try:
            if _resolve_home(str(home)) != home or _resolve_workspace(self.workspace) != self.workspace:
                raise ValueError()
        except ValueError:
            raise ValueError("journal_codex_runtime_changed") from None
        if preserved_json(self._identity()) != self._identity_json:
            raise ValueError("journal_codex_runtime_changed")

    def run(self, stdin: str, schema: str, *, model: str, effort: str, timeout_seconds: float,
            profile: str = "CODEX_JOURNAL_CLI_V1") -> CliExecResult:
        if profile == "CODEX_JOURNAL_CLI_V1":
            command_argv = argv
        elif profile == "CODEX_JOURNAL_CLI_V2":
            from app.services.providers.codex_cli_profile_v2 import argv as command_argv
        else:
            raise ValueError("journal_codex_profile_invalid")
        self.verify()
        self.workspace.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="journal-", dir=self.workspace) as attempt:
            root = Path(attempt)
            if root.resolve().parent != self.workspace:
                raise ValueError("journal_codex_runtime_changed")
            (root / "output-schema.json").write_text(schema, encoding="utf-8", newline="")
            return capture(command_argv(self.launcher, str(root), model, effort), stdin, root, dict(self.environment), timeout_seconds)


def capture(command: list[str], stdin: str, cwd: Path, env: dict[str, str], timeout: float) -> CliExecResult:
    options: dict[str, Any] = {}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True
    try:
        proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
    except OSError:
        raise ProviderError("journal_codex_process_unavailable") from None
    exceeded = threading.Event()
    buffers = [bytearray(), bytearray()]

    def read(stream, index: int, limit: int) -> None:
        try:
            while chunk := stream.read(4096):
                room = limit - len(buffers[index])
                buffers[index].extend(chunk[:room])
                if len(chunk) > room:
                    exceeded.set()
                    return
        except (OSError, ValueError):
            exceeded.set()
        finally:
            stream.close()

    def write() -> None:
        assert proc.stdin is not None
        try:
            proc.stdin.write(stdin.encode("utf-8"))
            proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            exceeded.set()  # A zero exit cannot prove the full input arrived.
        finally:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                exceeded.set()

    readers = [threading.Thread(target=read, args=(proc.stdout, 0, MAX_STREAM_BYTES), daemon=True),
               threading.Thread(target=read, args=(proc.stderr, 1, MAX_STDERR_BYTES), daemon=True),
               threading.Thread(target=write, daemon=True)]
    deadline = time.monotonic() + timeout
    output = cwd / "last-message.json"
    interrupted = False
    job: WindowsJob | None = None
    def terminate_group() -> None:
        if job is not None:
            job.close()
        elif sys.platform != "win32":
            try:
                getattr(os, "killpg")(proc.pid, getattr(signal, "SIGKILL"))
            except ProcessLookupError:
                pass
        elif proc.poll() is None:
            proc.kill()  # Assignment failed before input was sent.
    try:
        if sys.platform == "win32":
            job = WindowsJob(int(getattr(proc, "_handle")))
        for reader in readers:
            reader.start()
        while proc.poll() is None:
            if output.exists() and (output.is_symlink() or output.stat().st_size > MAX_FINAL_BYTES):
                exceeded.set()
            if exceeded.is_set() or time.monotonic() >= deadline:
                interrupted = True
                terminate_group()
                break
            time.sleep(.01)
        proc.wait(timeout=5)
        terminate_group()  # Reap descendants even when the launcher exited first.
        for reader in readers:
            reader.join(timeout=1)
        if interrupted or exceeded.is_set() or any(reader.is_alive() for reader in readers):
            raise ProviderError("journal_codex_process_outcome_unknown")
        final = ""
        if output.exists():
            info = output.lstat()
            if not stat.S_ISREG(info.st_mode) or output.is_symlink() or info.st_nlink != 1 or info.st_size > MAX_FINAL_BYTES:
                raise ProviderError("journal_codex_output_boundary")
            with output.open("rb") as stream:
                raw = stream.read(MAX_FINAL_BYTES + 1)
            if len(raw) > MAX_FINAL_BYTES:
                raise ProviderError("journal_codex_output_boundary")
            final = raw.decode("utf-8", errors="strict")
        return CliExecResult(int(proc.returncode), buffers[0].decode("utf-8", errors="strict"), "", last_message=final)
    except (OSError, UnicodeError, subprocess.TimeoutExpired, RuntimeError):
        raise ProviderError("journal_codex_process_outcome_unknown") from None
    finally:
        terminate_group()
        if proc.poll() is None:
            proc.wait(timeout=5)
        for reader in readers:
            if reader.ident is not None:
                reader.join(timeout=1)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None and not any(reader.is_alive() for reader in readers):
                pipe.close()
