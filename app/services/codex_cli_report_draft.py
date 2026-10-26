"""Official Codex CLI runner for Prototype report_draft_assist.

Uses only the installed `codex exec` contract. The removed Python SDK packages
are never imported. Automated tests inject a runner and must not call the live
service.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time
from typing import Any, Callable, Mapping, Sequence

from app.services.codex_cli_output_schema import SCHEMA_PROFILE_VERSION, schema_sha256
from app.services.codex_session_probe import _probe_env, _resolve_executable, assert_no_secret_leakage

STAGE = "report_draft_assist"
SCHEMA_FILENAME = "output-schema.json"
LAST_MESSAGE_FILENAME = "last-message.json"
REQUEST_FILENAME = "request.json"
DIAGNOSTICS_FILENAME = "cli-diagnostics.json"
DEFAULT_TIMEOUT_SECONDS = 120
MAX_OUTPUT_BYTES = 65_536
MAX_EXCERPT_BYTES = 8_192
LAUNCHER_CMD = "codex_cmd"
LAUNCHER_NODE = "node_official_codex_entry"

REASON_CLI_UNAVAILABLE = "CODEX_CLI_UNAVAILABLE"
REASON_EXIT_NONZERO = "CODEX_CLI_EXIT_NONZERO"
REASON_USAGE_LIMIT = "CODEX_CLI_USAGE_LIMIT"
REASON_TIMEOUT = "CODEX_CLI_TIMEOUT"
REASON_FINAL_OUTPUT_MISSING = "CODEX_CLI_FINAL_OUTPUT_MISSING"
REASON_OUTPUT_PARSE_FAILED = "CODEX_CLI_OUTPUT_PARSE_FAILED"
REASON_REQUEST_INVALID = "PROTOTYPE_REPORT_DRAFT_REQUEST_INVALID"
REASON_TIMEOUT_INVALID = "PROTOTYPE_REPORT_DRAFT_TIMEOUT_INVALID"

SAFE_REASON_MESSAGES = {
    REASON_CLI_UNAVAILABLE: "Official Codex CLI is not available.",
    REASON_EXIT_NONZERO: "Codex CLI exited without a usable candidate.",
    REASON_USAGE_LIMIT: "Codex CLI is authenticated but the account usage limit is exhausted.",
    REASON_TIMEOUT: "Codex CLI timed out before returning a candidate.",
    REASON_FINAL_OUTPUT_MISSING: "Codex CLI did not emit a final candidate.",
    REASON_OUTPUT_PARSE_FAILED: "Codex CLI output could not be parsed as candidate JSON.",
    "CODEX_CANDIDATE_SCHEMA_INVALID": "The candidate did not match the report-draft schema.",
    "CODEX_EVIDENCE_REFERENCE_INVALID": "The candidate cited evidence that is not in the current run.",
    "CODEX_CANDIDATE_REJECTED": "The candidate was rejected by acceptance policy.",
    "CODEX_SESSION_UNAUTHENTICATED": "Codex CLI is installed but not authenticated.",
}

TERMINAL_EVENT_TYPES = {
    "item.completed",
    "agent_message",
    "message",
    "turn.completed",
    "thread.completed",
}

USAGE_LIMIT_MARKERS = ("usage limit", "purchase more credits")
RESET_TIME_RE = re.compile(
    r"(?i)(?:retry after|try again after|try again on|not before|until)\s+"
    r"([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}(?:\s+\d{1,2}:\d{2}(?:\s*[APMapm]{2})?)?"
    r"|\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?)"
)

REDACTION_RULES: tuple[tuple[str, str], ...] = (
    (r"(?i)bearer\s+[a-z0-9._\-+=/]{8,}", "bearer [REDACTED]"),
    (r"(?i)sk-[a-z0-9_-]{8,}", "sk-[REDACTED]"),
    (r"(?i)(cookie\s*[:=]\s*)\S+", r"\1[REDACTED]"),
    (r"(?i)[a-z]:\\users\\[^\s\"']+", "[USER_PATH]"),
    (r"(?i)session[_-]?id[\"']?\s*[:=]\s*[\"']?[\w.-]+", "session_id=[REDACTED]"),
    (r"(?i)access_token\s*[:=]\s*\S+", "access_token=[REDACTED]"),
    (r"(?i)refresh_token\s*[:=]\s*\S+", "refresh_token=[REDACTED]"),
    (r"(?i)auth\.json", "[CREDENTIAL_PATH]"),
    (r"(?i)\.codex[/\\]", "[CREDENTIAL_PATH]"),
)


@dataclass(frozen=True)
class CliExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    last_message: str = ""


@dataclass(frozen=True)
class StreamInspection:
    jsonl_event_count: int
    event_type_counts: dict[str, int]
    last_terminal_event_type: str | None
    terminal_messages: tuple[str, ...]
    malformed_jsonl_line_count: int


@dataclass(frozen=True)
class OfficialLauncher:
    argv_prefix: tuple[str, ...]
    kind: str


CliCommandRunner = Callable[[Sequence[str], str, str, Mapping[str, str]], CliExecResult]


def human_reason_message(code: str) -> str:
    return SAFE_REASON_MESSAGES.get(code, "The Provider turn ended without an accepted candidate.")


def is_usage_limit_text(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in USAGE_LIMIT_MARKERS)


def extract_usage_limit_reset_time_text(text: str) -> str | None:
    match = RESET_TIME_RE.search(text)
    if match is None:
        return None
    return match.group(1).strip()


def classify_nonzero_cli_reason(stdout: str, stderr: str) -> str:
    if is_usage_limit_text(f"{stdout}\n{stderr}"):
        return REASON_USAGE_LIMIT
    return REASON_EXIT_NONZERO


def build_usage_limit_metadata(
    *,
    stdout: str,
    stderr: str,
    exit_code: int,
    timed_out: bool,
) -> dict[str, Any]:
    reset_time_text = extract_usage_limit_reset_time_text(f"{stdout}\n{stderr}")
    return {
        "source": "codex_cli",
        "exit_code": exit_code,
        "timed_out": timed_out,
        "reset_time_text": reset_time_text,
        "reset_timezone_claimed": False,
    }


def redact_cli_text(text: str) -> str:
    redacted = text
    for pattern, replacement in REDACTION_RULES:
        redacted = re.sub(pattern, replacement, redacted)
    return redacted


def bounded_excerpt(text: str, *, limit: int = MAX_EXCERPT_BYTES) -> str:
    payload = redact_cli_text(text)
    encoded = payload.encode("utf-8")
    if len(encoded) <= limit:
        return payload
    return encoded[-limit:].decode("utf-8", errors="replace")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sanitize_argv_shape(argv: Sequence[str], *, cwd: str, schema_path: str, last_message_path: str) -> list[str]:
    replacements = {
        cwd: "{cwd}",
        schema_path: "{schema_path}",
        last_message_path: "{last_message_path}",
    }
    shaped: list[str] = []
    for item in argv:
        next_item = item
        for raw, token in replacements.items():
            if raw and raw in next_item:
                next_item = next_item.replace(raw, token)
        next_item = redact_cli_text(next_item)
        if re.search(r"(?i)[a-z]:\\users\\", next_item):
            next_item = "[USER_PATH]"
        shaped.append(next_item)
    return shaped


def _official_js_beside(cmd_path: str) -> str | None:
    candidate = Path(cmd_path).resolve().parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    return str(candidate) if candidate.is_file() else None


def resolve_official_launcher(
    *,
    node_path: str | None = None,
    official_js_path: str | None = None,
    cmd_path: str | None = None,
) -> OfficialLauncher:
    resolved_cmd = cmd_path if cmd_path is not None else shutil.which("codex.cmd")
    resolved_node = node_path if node_path is not None else (shutil.which("node.exe") or shutil.which("node"))
    resolved_js = official_js_path
    if resolved_js is None and resolved_cmd:
        resolved_js = _official_js_beside(resolved_cmd)
    if resolved_node and resolved_js and Path(resolved_js).is_file():
        return OfficialLauncher((resolved_node, resolved_js), LAUNCHER_NODE)
    if resolved_cmd:
        return OfficialLauncher((resolved_cmd,), LAUNCHER_CMD)
    fallback = _resolve_executable()
    if fallback:
        kind = LAUNCHER_CMD if fallback.lower().endswith(".cmd") else LAUNCHER_NODE
        return OfficialLauncher((fallback,), kind)
    return OfficialLauncher((), "")


def build_codex_cli_exec_argv(
    *,
    executable: str | None = None,
    launcher_argv: Sequence[str] | None = None,
    cwd: str,
    schema_path: str,
    last_message_path: str,
) -> list[str]:
    prefix = list(launcher_argv) if launcher_argv is not None else [executable or "codex"]
    return [
        *prefix,
        "exec",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--ephemeral",
        "--color",
        "never",
        "--json",
        "--output-schema",
        schema_path,
        "--output-last-message",
        last_message_path,
        "--cd",
        cwd,
        "-",
    ]


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            check=False,
            capture_output=True,
            text=True,
        )
        return
    killpg = getattr(os, "killpg", None)
    sigkill = getattr(signal, "SIGKILL", None)
    try:
        if callable(killpg) and sigkill is not None:
            killpg(proc.pid, sigkill)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


def _default_cli_runner(
    argv: Sequence[str],
    prompt: str,
    cwd: str,
    env: Mapping[str, str],
    *,
    timeout_seconds: int,
) -> CliExecResult:
    creationflags = 0
    extra: dict[str, Any] = {}
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )
    else:
        extra["start_new_session"] = True
    proc = subprocess.Popen(
        list(argv),
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=dict(env),
        creationflags=creationflags,
        **extra,
    )
    try:
        stdout, stderr = proc.communicate(input=prompt, timeout=float(timeout_seconds))
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        return CliExecResult(124, stdout or "", stderr or "", timed_out=True)
    last_message = ""
    last_path = Path(cwd) / LAST_MESSAGE_FILENAME
    if last_path.is_file():
        last_message = last_path.read_text(encoding="utf-8", errors="replace")
    return CliExecResult(int(proc.returncode), stdout or "", stderr or "", last_message=last_message)


def _event_type(event: Mapping[str, Any]) -> str:
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    return str(event.get("type") or (item.get("type") if isinstance(item, dict) else "") or "")


def _event_text(event: Mapping[str, Any]) -> str | None:
    item = event.get("item") if isinstance(event.get("item"), dict) else event
    if not isinstance(item, dict):
        return None
    typ = _event_type(event)
    if typ not in TERMINAL_EVENT_TYPES:
        return None
    if typ in {"item.started", "item.delta"}:
        return None
    sources: list[Mapping[str, Any]] = [item]
    if event is not item:
        sources.append(event)
    for source in sources:
        for key in ("text", "message", "content", "last_agent_message"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value
            if isinstance(value, dict):
                inner = value.get("text") or value.get("message")
                if isinstance(inner, str) and inner.strip():
                    return inner
    return None


def inspect_cli_stdout(stdout: str) -> StreamInspection:
    counts: dict[str, int] = {}
    messages: list[str] = []
    last_terminal: str | None = None
    event_count = 0
    malformed = 0
    for line in stdout.splitlines():
        row = line.strip()
        if not row:
            continue
        if not row.startswith("{"):
            continue
        try:
            event = json.loads(row)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(event, dict):
            malformed += 1
            continue
        event_count += 1
        typ = _event_type(event) or "unknown"
        counts[typ] = counts.get(typ, 0) + 1
        if typ in TERMINAL_EVENT_TYPES:
            last_terminal = typ
            text = _event_text(event)
            if text:
                messages.append(text)
    return StreamInspection(event_count, counts, last_terminal, tuple(messages), malformed)


def _unwrap_json_object(text: str) -> str | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    if not stripped:
        return None
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        if isinstance(payload, dict):
            return json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return None
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        return None
    candidate = stripped[start : end + 1]
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def extract_final_candidate_json(*, last_message: str, stdout: str) -> tuple[str | None, str | None]:
    extracted = _unwrap_json_object(last_message)
    if extracted:
        return extracted, None
    inspection = inspect_cli_stdout(stdout)
    if inspection.terminal_messages:
        extracted = _unwrap_json_object(inspection.terminal_messages[-1])
        if extracted:
            return extracted, None
        return None, REASON_OUTPUT_PARSE_FAILED
    if inspection.jsonl_event_count:
        return None, REASON_FINAL_OUTPUT_MISSING
    extracted = _unwrap_json_object(stdout)
    if extracted:
        return extracted, None
    if inspection.malformed_jsonl_line_count or last_message.strip() or stdout.strip():
        return None, REASON_OUTPUT_PARSE_FAILED
    return None, REASON_FINAL_OUTPUT_MISSING


def _write_task_files(cwd: Path, *, prompt: str, output_schema: Mapping[str, Any]) -> tuple[Path, Path]:
    cwd.mkdir(parents=True, exist_ok=True)
    schema_path = cwd / SCHEMA_FILENAME
    last_message_path = cwd / LAST_MESSAGE_FILENAME
    request_path = cwd / REQUEST_FILENAME
    schema_path.write_text(json.dumps(dict(output_schema), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    request_path.write_text(prompt, encoding="utf-8")
    return schema_path, last_message_path


def _maybe_write_safe_stream(path: Path, text: str) -> bool:
    if assert_no_secret_leakage(text):
        return False
    path.write_text(text, encoding="utf-8", newline="\n")
    return True


def build_safe_cli_diagnostics(
    *,
    exit_code: int,
    timed_out: bool,
    duration_ms: int,
    launcher_kind: str,
    cli_version: str | None,
    argv: Sequence[str],
    cwd: str,
    schema_path: str,
    last_message_path: str,
    stdout: str,
    stderr: str,
    last_message: str,
    output_schema: Mapping[str, Any],
    extraction_source: str | None,
    last_message_expected: bool,
) -> dict[str, Any]:
    last_path = Path(last_message_path)
    last_exists = last_path.is_file()
    last_text = last_message if last_message else (last_path.read_text(encoding="utf-8", errors="replace") if last_exists else "")
    inspection = inspect_cli_stdout(stdout)
    stdout_excerpt = bounded_excerpt(stdout)
    stderr_excerpt = bounded_excerpt(stderr)
    summary = (
        f"exit {exit_code} · launcher {launcher_kind or 'unknown'} · "
        f"timeout {str(timed_out).lower()} · last-message {'yes' if last_exists else 'no'} · "
        f"events {inspection.jsonl_event_count} · last event {inspection.last_terminal_event_type or 'none'}"
    )
    diagnostics = {
        "source": "codex_cli",
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
        "launcher_kind": launcher_kind,
        "cli_version": cli_version,
        "argv_shape": sanitize_argv_shape(
            argv, cwd=cwd, schema_path=schema_path, last_message_path=last_message_path
        ),
        "stdout_byte_count": len(stdout.encode("utf-8")),
        "stderr_byte_count": len(stderr.encode("utf-8")),
        "stdout_sha256": _sha256_text(stdout),
        "stderr_sha256": _sha256_text(stderr),
        "stdout_excerpt": stdout_excerpt,
        "stderr_excerpt": stderr_excerpt,
        "output_last_message_expected": last_message_expected,
        "output_last_message_exists": last_exists,
        "output_last_message_byte_count": len(last_text.encode("utf-8")),
        "output_last_message_sha256": _sha256_text(last_text) if last_text else None,
        "jsonl_event_count": inspection.jsonl_event_count,
        "event_type_counts": inspection.event_type_counts,
        "last_terminal_event_type": inspection.last_terminal_event_type,
        "candidate_extraction_source": extraction_source,
        "schema_profile_version": SCHEMA_PROFILE_VERSION,
        "schema_sha256": schema_sha256(output_schema),
        "diagnostic_summary": summary,
        "secret_leakage": assert_no_secret_leakage(
            {
                "stdout_excerpt": stdout_excerpt,
                "stderr_excerpt": stderr_excerpt,
                "argv_shape": sanitize_argv_shape(
                    argv, cwd=cwd, schema_path=schema_path, last_message_path=last_message_path
                ),
                "summary": summary,
            }
        ),
    }
    return diagnostics


def _attach_diagnostics(payload: dict[str, Any], diagnostics: dict[str, Any]) -> dict[str, Any]:
    payload["safe_diagnostics"] = diagnostics
    payload["exit_code"] = diagnostics["exit_code"]
    payload["timed_out"] = diagnostics["timed_out"]
    payload["duration_ms"] = diagnostics["duration_ms"]
    payload["model_backend_identifier"] = payload.get("model_backend_identifier") or "codex-cli"
    return payload


def _persist_diagnostics(cwd: Path, diagnostics: Mapping[str, Any], *, stdout: str, stderr: str) -> None:
    path = cwd / DIAGNOSTICS_FILENAME
    path.write_text(json.dumps(dict(diagnostics), indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    if not diagnostics.get("secret_leakage"):
        _maybe_write_safe_stream(cwd / "cli-stdout.safe.txt", stdout)
        _maybe_write_safe_stream(cwd / "cli-stderr.safe.txt", stderr)


def _empty_diagnostics(*, reason: str, duration_ms: int = 0) -> dict[str, Any]:
    return {
        "source": "codex_cli",
        "exit_code": None,
        "timed_out": False,
        "duration_ms": duration_ms,
        "launcher_kind": None,
        "cli_version": None,
        "argv_shape": [],
        "stdout_byte_count": 0,
        "stderr_byte_count": 0,
        "stdout_sha256": _sha256_text(""),
        "stderr_sha256": _sha256_text(""),
        "stdout_excerpt": "",
        "stderr_excerpt": "",
        "output_last_message_expected": False,
        "output_last_message_exists": False,
        "output_last_message_byte_count": 0,
        "output_last_message_sha256": None,
        "jsonl_event_count": 0,
        "event_type_counts": {},
        "last_terminal_event_type": None,
        "candidate_extraction_source": None,
        "schema_profile_version": SCHEMA_PROFILE_VERSION,
        "schema_sha256": None,
        "diagnostic_summary": reason,
        "secret_leakage": [],
    }


def run_codex_cli_report_draft_once(
    *,
    prompt: str,
    output_schema: Mapping[str, Any],
    cwd: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    runner: CliCommandRunner | None = None,
    executable: str | None = None,
    launcher_argv: Sequence[str] | None = None,
    launcher_kind: str | None = None,
    cli_version: str | None = None,
) -> dict[str, Any]:
    """Run one non-interactive Codex CLI report-draft turn. No retry."""

    if not prompt.strip() or len(prompt.encode("utf-8")) > 65_536:
        return _attach_diagnostics(
            {
                "status": "stopped",
                "result_category": "request_bounds_rejected",
                "provider_call_count": 0,
                "reason_category": REASON_REQUEST_INVALID,
            },
            _empty_diagnostics(reason=REASON_REQUEST_INVALID),
        )
    if timeout_seconds < 1 or timeout_seconds > 300:
        return _attach_diagnostics(
            {
                "status": "stopped",
                "result_category": "request_bounds_rejected",
                "provider_call_count": 0,
                "reason_category": REASON_TIMEOUT_INVALID,
            },
            _empty_diagnostics(reason=REASON_TIMEOUT_INVALID),
        )

    resolved_kind = launcher_kind or ""
    prefix: list[str]
    if launcher_argv is not None:
        prefix = list(launcher_argv)
        resolved_kind = resolved_kind or (
            LAUNCHER_NODE if len(prefix) >= 2 and prefix[1].replace("\\", "/").endswith("codex.js") else LAUNCHER_CMD
        )
    elif executable is not None:
        if executable == "":
            return _attach_diagnostics(
                {
                    "status": "unavailable",
                    "result_category": "provider_unavailable",
                    "provider_call_count": 0,
                    "reason_category": REASON_CLI_UNAVAILABLE,
                },
                _empty_diagnostics(reason=REASON_CLI_UNAVAILABLE),
            )
        prefix = [executable]
        resolved_kind = resolved_kind or (LAUNCHER_CMD if executable.lower().endswith(".cmd") else LAUNCHER_CMD)
    else:
        launcher = resolve_official_launcher()
        if not launcher.argv_prefix:
            return _attach_diagnostics(
                {
                    "status": "unavailable",
                    "result_category": "provider_unavailable",
                    "provider_call_count": 0,
                    "reason_category": REASON_CLI_UNAVAILABLE,
                },
                _empty_diagnostics(reason=REASON_CLI_UNAVAILABLE),
            )
        prefix = list(launcher.argv_prefix)
        resolved_kind = launcher.kind

    workdir = Path(cwd)
    schema_path, last_message_path = _write_task_files(workdir, prompt=prompt, output_schema=output_schema)
    argv = build_codex_cli_exec_argv(
        launcher_argv=prefix,
        cwd=str(workdir),
        schema_path=str(schema_path),
        last_message_path=str(last_message_path),
    )
    env = _probe_env()
    started = time.monotonic()
    if runner is not None:
        result = runner(argv, prompt, str(workdir), env)
    else:
        result = _default_cli_runner(argv, prompt, str(workdir), env, timeout_seconds=timeout_seconds)
    duration_ms = round((time.monotonic() - started) * 1000)
    last_message = result.last_message
    if not last_message and last_message_path.is_file():
        last_message = last_message_path.read_text(encoding="utf-8", errors="replace")

    extraction_source: str | None = None
    raw_output: str | None = None
    parse_reason: str | None = None
    if result.exit_code == 0 and not result.timed_out:
        raw_output, parse_reason = extract_final_candidate_json(last_message=last_message, stdout=result.stdout)
        if raw_output is not None:
            extraction_source = "output_last_message" if _unwrap_json_object(last_message) else "jsonl_terminal_message"

    diagnostics = build_safe_cli_diagnostics(
        exit_code=int(result.exit_code),
        timed_out=bool(result.timed_out),
        duration_ms=duration_ms,
        launcher_kind=resolved_kind,
        cli_version=cli_version,
        argv=argv,
        cwd=str(workdir),
        schema_path=str(schema_path),
        last_message_path=str(last_message_path),
        stdout=result.stdout,
        stderr=result.stderr,
        last_message=last_message,
        output_schema=output_schema,
        extraction_source=extraction_source,
        last_message_expected=True,
    )
    nonzero_reason: str | None = None
    if result.exit_code != 0 and not result.timed_out:
        nonzero_reason = classify_nonzero_cli_reason(result.stdout, result.stderr)
        if nonzero_reason == REASON_USAGE_LIMIT:
            diagnostics["usage_limit"] = build_usage_limit_metadata(
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=int(result.exit_code),
                timed_out=bool(result.timed_out),
            )
    _persist_diagnostics(workdir, diagnostics, stdout=result.stdout, stderr=result.stderr)

    if result.timed_out:
        return _attach_diagnostics(
            {
                "status": "timeout",
                "result_category": "provider_timeout",
                "provider_call_count": 1,
                "reason_category": REASON_TIMEOUT,
            },
            diagnostics,
        )
    if nonzero_reason is not None:
        reason = nonzero_reason
        return _attach_diagnostics(
            {
                "status": "failed",
                "result_category": "provider_failed",
                "provider_call_count": 1,
                "reason_category": reason,
            },
            diagnostics,
        )
    if raw_output is None:
        return _attach_diagnostics(
            {
                "status": "failed",
                "result_category": "provider_failed",
                "provider_call_count": 1,
                "reason_category": parse_reason or REASON_FINAL_OUTPUT_MISSING,
            },
            diagnostics,
        )
    if len(raw_output.encode("utf-8")) > MAX_OUTPUT_BYTES:
        diagnostics = dict(diagnostics)
        diagnostics["candidate_extraction_source"] = extraction_source
        return _attach_diagnostics(
            {
                "status": "failed",
                "result_category": "provider_failed",
                "provider_call_count": 1,
                "reason_category": REASON_OUTPUT_PARSE_FAILED,
            },
            diagnostics,
        )
    return _attach_diagnostics(
        {
            "status": "completed",
            "result_category": "provider_output_received",
            "provider_call_count": 1,
            "raw_output": raw_output,
            "reason_category": None,
        },
        diagnostics,
    )
