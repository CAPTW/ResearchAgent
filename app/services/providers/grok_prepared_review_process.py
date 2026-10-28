"""One bounded interactive ACP child, with owned cleanup and no account reads.

The host ends a persistent ACP child after the correlated terminal response.
No quiet-period heuristic, generic CLI completion grammar or process relaunch.
WindowsJob is process cleanup, not a security sandbox or remote cancellation.
"""
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import queue
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Mapping

from app.domain.journal_exchange import preserved_json, sha
from app.services.providers.base import ProviderError
from app.services.providers.codex_journal_job import WindowsJob
from app.services.providers.codex_journal_plan import MAX_STDERR_BYTES, MAX_STREAM_BYTES
from app.services.providers.grok_prepared_review_plan import FIXED_ENV, agent_text, argv
from app.services.providers.grok_prepared_review_protocol import Conversation, DeniedRequest, line

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
class CapturedConversation:
    transcript: list[dict[str, str]]
    attempt_cwd: str


def capture(command: list[str], request_text: str, model: str, cwd: Path,
            environment: dict[str, str], timeout_seconds: float) -> CapturedConversation:
    options: dict[str, Any] = {}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True
    try:
        proc = subprocess.Popen(command, cwd=cwd, env=environment, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
    except OSError:
        raise ProviderError("paper_grok_process_outcome_unknown") from None
    failed = threading.Event()
    stdout_done = threading.Event()
    incoming: queue.Queue[bytes] = queue.Queue(maxsize=256)
    outgoing: queue.Queue[tuple[bytes, threading.Event] | None] = queue.Queue(maxsize=1)
    job: WindowsJob | None = None
    deadline = time.monotonic() + timeout_seconds
    conversation = Conversation(request_text, model, str(cwd))
    pending = bytearray()

    def terminate() -> None:
        if job is not None:
            job.close()
        elif sys.platform != "win32":
            try:
                getattr(os, "killpg")(proc.pid, getattr(signal, "SIGKILL"))
            except ProcessLookupError:
                pass
        elif proc.poll() is None:
            proc.kill()

    def reader(stream, output: bool, limit: int) -> None:
        count = 0
        try:
            while data := os.read(stream.fileno(), 4096):
                count += len(data)
                if count > limit:
                    failed.set()
                    return
                if output:
                    incoming.put_nowait(data)
        except (OSError, ValueError, queue.Full):
            failed.set()
        finally:
            stream.close()
            if output:
                stdout_done.set()

    def writer() -> None:
        assert proc.stdin is not None
        try:
            while True:
                item = outgoing.get()
                if item is None:
                    return
                data, done = item
                proc.stdin.write(data)
                proc.stdin.flush()
                done.set()
        except (OSError, ValueError):
            failed.set()
        finally:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                failed.set()

    threads = [threading.Thread(target=reader, args=(proc.stdout, True, MAX_STREAM_BYTES), daemon=True),
               threading.Thread(target=reader, args=(proc.stderr, False, MAX_STDERR_BYTES), daemon=True),
               threading.Thread(target=writer, daemon=True)]

    def send(raw: str, *, record: bool = True) -> None:
        done = threading.Event()
        outgoing.put_nowait((raw.encode("utf-8"), done))
        while not done.wait(.01):
            if failed.is_set() or time.monotonic() >= deadline:
                raise ValueError()
        if failed.is_set():
            raise ValueError()
        if record:
            conversation.sent(raw)

    def consume(data: bytes) -> None:
        pending.extend(data)
        while b"\n" in pending:
            split = pending.index(b"\n") + 1
            raw = bytes(pending[:split]).decode("utf-8", errors="strict")
            del pending[:split]
            try:
                conversation.received(raw)
            except DeniedRequest as denial:
                send(line(denial.reply), record=False)
                raise
            if conversation.next_request is not None:
                send(line(conversation.next_request))

    try:
        if sys.platform == "win32":
            job = WindowsJob(int(getattr(proc, "_handle")))
        for thread in threads:
            thread.start()
        assert conversation.next_request is not None
        send(line(conversation.next_request))
        while conversation.terminal is None:
            if failed.is_set() or time.monotonic() >= deadline:
                raise ValueError()
            try:
                consume(incoming.get(timeout=min(.02, max(.001, deadline - time.monotonic()))))
            except queue.Empty:
                if stdout_done.is_set():
                    raise ValueError()
        # A correlated terminal reply, not EOF or an idle delay, established the
        # turn outcome. ACP is persistent: close only this host-owned child now.
        outgoing.put_nowait(None)
        terminate()
        proc.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=1)
        if failed.is_set() or any(thread.is_alive() for thread in threads):
            raise ValueError()
        while not incoming.empty():
            consume(incoming.get_nowait())  # Reject observed post-terminal conflicts.
        if pending:
            raise ValueError()
        return CapturedConversation(conversation.transcript, str(cwd))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError,
            RuntimeError, queue.Full, subprocess.TimeoutExpired):
        raise ProviderError("paper_grok_process_outcome_unknown") from None
    finally:
        try:
            outgoing.put_nowait(None)
        except queue.Full:
            pass
        terminate()
        if proc.poll() is None:
            proc.wait(timeout=5)
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=1)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None and not any(thread.is_alive() for thread in threads):
                pipe.close()


@dataclass(frozen=True, init=False)
class GrokRuntime:
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
            raise ValueError("paper_grok_runtime_invalid") from None

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
            raise ValueError("paper_grok_runtime_changed") from None

    def run(self, request_text: str, *, model: str, timeout_seconds: float) -> CapturedConversation:
        self.verify()
        self.workspace.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="grok-review-", dir=self.workspace) as attempt:
            root = Path(attempt)
            if root.resolve().parent != self.workspace:
                raise ValueError("paper_grok_runtime_changed")
            profile = root / "paper-agent.md"
            profile.write_text(agent_text(json.loads(request_text)["instructions"]), encoding="utf-8", newline="")
            return capture(argv(self.launcher, str(profile), model), request_text, model, root, dict(self.environment), timeout_seconds)
