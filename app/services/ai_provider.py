from __future__ import annotations

from copy import deepcopy
import json
import hashlib
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from app.domain.journal_exchange import preserved_json
from app.domain.review_input import PACKET_VERSION, validate_packet
from app.repositories import ai_review_drafts
from app.repositories.files import iso_now_utc
from app.repositories.jsonl import (
    JsonlIntegrityError,
    parse_jsonl_bytes_strict,
    read_jsonl_strict,
)
from app.services import ai_review, operator_audit, provider_auth_bridge, provider_configurations

DEFAULT_PROVIDER_OUTPUT_ROOT = Path("output") / "ai-provider-runs"
DEFAULT_MANUAL_PROVIDER_RUN_ROOT = Path("output") / "provider-manual-runs"
DEFAULT_MANUAL_COMMAND_TEMPLATE = [
    "provider-reviewer",
    "--packet",
    "{packet_path}",
    "--output",
    "{output_path}",
    "--run-id",
    "{provider_run_id}",
    "--topic",
    "{topic_id}",
]
FIXTURE_COMMAND_PROVIDER_CONFIG_ID = "fixture-command"
ProviderInvocationMode = Literal["dry_run", "stub", "live_command"]


@dataclass(frozen=True)
class ProviderRequest:
    provider_name: str
    packet: dict[str, Any]
    mode: ProviderInvocationMode = "dry_run"
    model: str | None = None
    run_id: str = "local-provider-run"
    max_output_chars: int = 20000
    timeout_seconds: int = 30
    explicit_live: bool = False
    command: list[str] = field(default_factory=list)
    scenario: str = "valid"


@dataclass
class ProviderResponse:
    status: str
    provider_name: str
    run_id: str
    paper_id: str
    raw_output: str = ""
    raw_output_path: str | None = None
    parsed_draft: dict[str, Any] | None = None
    validation_result: dict[str, Any] | None = None
    reason_codes: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    exit_code: int | None = None
    packet_path: str | None = None
    provider_output_path: str | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    output_truncated: bool = False
    provider_config_id: str | None = None
    profile_role: str | None = None
    operator_accepted: bool = False
    operator_note_present: bool = False
    operator_note_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        item = {
            "status": self.status,
            "provider": self.provider_name,
            "run_id": self.run_id,
            "paper_id": self.paper_id,
            "raw_output_path": self.raw_output_path,
            "parsed_draft": self.parsed_draft,
            "validation_result": self.validation_result,
            "reason_codes": self.reason_codes,
            "duration_seconds": self.duration_seconds,
        }
        for key, value in {
            "exit_code": self.exit_code,
            "packet_path": self.packet_path,
            "provider_output_path": self.provider_output_path,
            "stdout_path": self.stdout_path,
            "stderr_path": self.stderr_path,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "output_truncated": self.output_truncated,
            "provider_config_id": self.provider_config_id,
            "profile_role": self.profile_role,
            "operator_accepted": self.operator_accepted,
            "operator_note_present": self.operator_note_present,
            "operator_note_sha256": self.operator_note_sha256,
        }.items():
            if value is not None and value is not False:
                item[key] = value
        return item


class AIReviewProvider:
    provider_name = "base"

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        raise NotImplementedError


class StubAIReviewProvider(AIReviewProvider):
    provider_name = "stub"

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        start = time.perf_counter()
        packet = request.packet
        scenario = request.scenario or "valid"
        if scenario == "failed":
            return ProviderResponse(
                status="failed",
                provider_name=self.provider_name,
                run_id=request.run_id,
                paper_id=str(packet.get("paper_id") or ""),
                reason_codes=["stub_failure"],
                duration_seconds=round(time.perf_counter() - start, 4),
            )
        if scenario == "timed_out":
            return ProviderResponse(
                status="timed_out",
                provider_name=self.provider_name,
                run_id=request.run_id,
                paper_id=str(packet.get("paper_id") or ""),
                reason_codes=["stub_timeout"],
                duration_seconds=round(time.perf_counter() - start, 4),
            )
        if scenario == "malformed_json":
            raw = "{malformed provider output"
            return ProviderResponse(
                status="rejected",
                provider_name=self.provider_name,
                run_id=request.run_id,
                paper_id=str(packet.get("paper_id") or ""),
                raw_output=raw,
                reason_codes=["malformed_json"],
                duration_seconds=round(time.perf_counter() - start, 4),
            )
        draft = ai_review.generate_fixture_ai_review_draft(packet, unsupported_claim=scenario == "unsupported_claim")
        if scenario == "wrong_sha256":
            draft["source_text_sha256"] = "wrong-provider-sha256"
        if scenario == "missing_evidence_span":
            draft["cited_evidence_span_ids"] = []
        raw = json.dumps(draft, ensure_ascii=False)
        return ProviderResponse(
            status="generated",
            provider_name=self.provider_name,
            run_id=request.run_id,
            paper_id=str(packet.get("paper_id") or ""),
            raw_output=raw,
            parsed_draft=draft,
            duration_seconds=round(time.perf_counter() - start, 4),
        )


class CommandAIReviewProvider(AIReviewProvider):
    provider_name = "command"

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        start = time.perf_counter()
        packet = request.packet
        paper_id = str(packet.get("paper_id") or "")
        if not request.explicit_live:
            return ProviderResponse(
                status="failed",
                provider_name=self.provider_name,
                run_id=request.run_id,
                paper_id=paper_id,
                reason_codes=["live_provider_not_allowed"],
                duration_seconds=round(time.perf_counter() - start, 4),
            )
        if not request.command:
            return ProviderResponse(
                status="failed",
                provider_name=self.provider_name,
                run_id=request.run_id,
                paper_id=paper_id,
                reason_codes=["missing_provider_command"],
                duration_seconds=round(time.perf_counter() - start, 4),
            )
        try:
            completed = subprocess.run(
                request.command,
                input=json.dumps(packet, ensure_ascii=False),
                text=True,
                shell=False,
                capture_output=True,
                timeout=max(1, int(request.timeout_seconds)),
            )
        except subprocess.TimeoutExpired:
            return ProviderResponse(
                status="timed_out",
                provider_name=self.provider_name,
                run_id=request.run_id,
                paper_id=paper_id,
                reason_codes=["provider_command_timed_out"],
                duration_seconds=round(time.perf_counter() - start, 4),
            )
        except FileNotFoundError:
            return ProviderResponse(
                status="failed",
                provider_name=self.provider_name,
                run_id=request.run_id,
                paper_id=paper_id,
                reason_codes=["provider_command_not_found"],
                duration_seconds=round(time.perf_counter() - start, 4),
            )
        raw = str(completed.stdout or "")
        if len(raw) > max(1, int(request.max_output_chars)):
            return ProviderResponse(
                status="rejected",
                provider_name=self.provider_name,
                run_id=request.run_id,
                paper_id=paper_id,
                raw_output=raw[: max(1, int(request.max_output_chars))],
                reason_codes=["provider_output_too_large"],
                duration_seconds=round(time.perf_counter() - start, 4),
            )
        if completed.returncode != 0:
            return ProviderResponse(
                status="failed",
                provider_name=self.provider_name,
                run_id=request.run_id,
                paper_id=paper_id,
                raw_output=raw,
                reason_codes=["provider_command_failed"],
                duration_seconds=round(time.perf_counter() - start, 4),
            )
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return ProviderResponse(
                status="rejected",
                provider_name=self.provider_name,
                run_id=request.run_id,
                paper_id=paper_id,
                raw_output=raw,
                reason_codes=["malformed_json"],
                duration_seconds=round(time.perf_counter() - start, 4),
            )
        if not isinstance(parsed, dict):
            return ProviderResponse(
                status="rejected",
                provider_name=self.provider_name,
                run_id=request.run_id,
                paper_id=paper_id,
                raw_output=raw,
                reason_codes=["malformed_json"],
                duration_seconds=round(time.perf_counter() - start, 4),
            )
        return ProviderResponse(
            status="generated",
            provider_name=self.provider_name,
            run_id=request.run_id,
            paper_id=paper_id,
            raw_output=raw,
            parsed_draft=parsed,
            duration_seconds=round(time.perf_counter() - start, 4),
        )


def _provider_for_name(name: str) -> AIReviewProvider:
    normalized = str(name or "stub").strip().lower()
    if normalized == "stub":
        return StubAIReviewProvider()
    if normalized == "command":
        return CommandAIReviewProvider()
    raise ValueError(f"unsupported AI review provider: {name}")


def _parse_command(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return shlex.split(str(value), posix=False)


def _operator_note_digest(note: str | None) -> str | None:
    text = str(note or "").strip()
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fixture_command_template() -> list[str]:
    return [
        sys.executable,
        "-m",
        "app.cli.provider_fixture_command",
        "--packet",
        "{packet_path}",
        "--output",
        "{output_path}",
        "--run-id",
        "{provider_run_id}",
        "--topic",
        "{topic_id}",
    ]


def _command_args_for_provider_config(provider_config: str | None, command: str | list[str] | None) -> tuple[list[str], dict[str, Any] | None]:
    if provider_config == FIXTURE_COMMAND_PROVIDER_CONFIG_ID:
        return fixture_command_template(), provider_configurations.provider_profile_by_id(provider_config)
    profile = provider_configurations.provider_profile_by_id(provider_config)
    explicit_command = _parse_command(command)
    if explicit_command:
        return explicit_command, profile
    if profile and str(profile.get("providerType") or "") == "command":
        return [str(item) for item in profile.get("commandArgvTemplate") or [] if str(item).strip()], profile
    return explicit_command, profile


def _provider_requires_operator_acceptance(
    *,
    provider: str,
    invocation_mode: str,
    profile: dict[str, Any] | None,
    provider_config: str | None,
) -> bool:
    if str(provider or "").strip().lower() != "command":
        return False
    if str(invocation_mode or "").strip().lower() == "dry_run":
        return False
    if str(provider_config or "").strip().lower() == FIXTURE_COMMAND_PROVIDER_CONFIG_ID:
        return False
    profile_role = str((profile or {}).get("profileRole") or "").strip().lower()
    requires_acceptance = bool((profile or {}).get("requiresOperatorAcceptance", False))
    return profile_role == "real_example" or requires_acceptance


def _operator_acceptance_failure(
    *,
    packet: dict[str, Any],
    run_id: str,
    provider_config_id: str | None,
    profile_role: str | None,
) -> ProviderResponse:
    return ProviderResponse(
        status="failed",
        provider_name="command",
        run_id=run_id,
        paper_id=str(packet.get("paper_id") or ""),
        reason_codes=["operator_acceptance_required"],
        provider_config_id=provider_config_id,
        profile_role=profile_role,
        operator_accepted=False,
    )


def _provider_output_path(output_root: Path, run_id: str, index: int, paper_id: str) -> Path:
    safe_paper_id = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in paper_id) or f"paper-{index}"
    safe_run_id = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in run_id) or "local-provider-run"
    return output_root / f"{safe_run_id}-{index:03d}-{safe_paper_id}.json"


def _safe_fragment(value: str, fallback: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in str(value or "").strip())
    return safe or fallback


def _write_provider_output(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _command_run_dir(output_root: Path, run_id: str, run_dir: str | Path | None) -> Path:
    if run_dir is not None:
        return Path(run_dir)
    return output_root / _safe_fragment(run_id, "provider-run")


def _command_artifact_paths(run_dir: Path, index: int, packet: dict[str, Any]) -> dict[str, Path]:
    filename = _manual_packet_filename(index, packet)
    stem = filename[:-5] if filename.endswith(".json") else filename
    return {
        "packet": run_dir / "packets" / filename,
        "provider_output": run_dir / "provider-output" / f"{stem}.json",
        "stdout": run_dir / "stdout" / f"{stem}.txt",
        "stderr": run_dir / "stderr" / f"{stem}.txt",
        "audit": run_dir / "audit" / f"{stem}.json",
    }


def _write_bounded_text(path: Path, text: str, max_chars: int) -> tuple[str | None, bool]:
    if text == "":
        return None, False
    limit = max(1, int(max_chars))
    truncated = len(text) > limit
    bounded = text[:limit] if truncated else text
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(bounded, encoding="utf-8")
    return str(path), truncated


def _read_provider_output(path: Path, max_chars: int) -> tuple[str, bool, list[str]]:
    if not path.exists():
        return "", False, ["provider_output_missing"]
    text = path.read_text(encoding="utf-8")
    limit = max(1, int(max_chars))
    if len(text) > limit:
        return text[:limit], True, ["provider_output_too_large"]
    return text, False, []


def _parse_provider_output_text(text: str, *, suffix: str = ".json") -> tuple[dict[str, Any] | None, int]:
    stripped = text.strip()
    if not stripped:
        return None, 1
    if suffix.lower() == ".jsonl":
        try:
            records = parse_jsonl_bytes_strict(text.encode("utf-8"))
        except JsonlIntegrityError:
            return None, 1
        return (_extract_manual_draft(records[0]), 0) if records else (None, 1)
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None, 1
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict):
                return _extract_manual_draft(item), 0
        return None, 1
    if isinstance(parsed, dict):
        items = parsed.get("drafts") or parsed.get("outputs") or parsed.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    return _extract_manual_draft(item), 0
            return None, 1
        return _extract_manual_draft(parsed), 0
    return None, 1


def _validate_response(packet: dict[str, Any], response: ProviderResponse) -> ProviderResponse:
    if response.parsed_draft is None:
        if not response.reason_codes:
            response.reason_codes = ["malformed_json"]
        if response.status == "generated":
            response.status = "rejected"
        return response
    existing_reasons = list(response.reason_codes)
    validation = ai_review.validate_ai_review_draft(packet, response.parsed_draft)
    response.validation_result = validation
    response.reason_codes = list(dict.fromkeys([*existing_reasons, *list(validation.get("reason_codes") or [])]))
    if validation["status"] == "accepted":
        response.status = "generated"
    else:
        response.status = validation["status"]
    return response


def _provider_origin(provider: str, invocation_mode: str, explicit_origin: str | None = None) -> str:
    if explicit_origin and explicit_origin.strip():
        return explicit_origin.strip()
    normalized = str(provider or "").strip().lower()
    if normalized == "stub":
        return "stub"
    if normalized == "command" and invocation_mode == "dry_run":
        return "command_dry_run"
    if normalized == "command":
        return "live_command"
    return "unknown"


def _increment_reason(reason_counts: dict[str, int], reasons: list[str]) -> None:
    for reason in reasons:
        reason_counts[reason] = reason_counts.get(reason, 0) + 1


def _review_run_id_for_packet(packet: dict[str, Any], topic_id: str) -> str:
    return ai_review._run_id_for_packet(packet, topic_id)


def _existing_accepted_draft(packet: dict[str, Any], topic_id: str) -> dict[str, Any] | None:
    source_hash = str(packet.get("source_text_sha256") or "")
    if not source_hash:
        return None
    draft = ai_review_drafts.get_draft(
        topic_id,
        _review_run_id_for_packet(packet, topic_id),
        str(packet.get("paper_id") or ""),
        source_hash,
    )
    if draft and draft.get("status") == "accepted":
        receipt_id = (draft.get("validation") or {}).get("input_receipt_id")
        if packet.get("packet_version") == PACKET_VERSION and receipt_id:
            validate_packet(packet)
            receipt = ai_review_drafts.get_input_receipt(topic_id, receipt_id)
            if receipt is None:
                raise ValueError("review_input_receipt_missing")
            if preserved_json(receipt["packet"]) != preserved_json(packet):
                return None
        # Legacy drafts without a preserved packet retain the existing skip
        # policy. Missing provenance must never trigger an automatic live call.
        return draft
    return None


def _planned_invocation(
    *,
    provider: str,
    invocation_mode: str,
    command_args: list[str],
    allow_live_provider: bool,
    timeout_seconds: int,
    max_output_chars: int,
    packet: dict[str, Any],
) -> dict[str, Any]:
    return {
        "provider": provider,
        "mode": invocation_mode,
        "command_present": bool(command_args),
        "command_preview": provider_auth_bridge.redact_provider_command(command_args[:8]),
        "live_allowed": bool(allow_live_provider),
        "timeout_seconds": max(1, int(timeout_seconds)),
        "max_output_chars": max(1, int(max_output_chars)),
        "packet_summary": {
            "paper_id": packet.get("paper_id"),
            "source_text_sha256": packet.get("source_text_sha256"),
            "review_level": packet.get("review_level"),
            "evidence_span_count": len(packet.get("evidence_spans") or []),
            "selected_text_excerpt_count": len(packet.get("selected_text_excerpts") or []),
        },
    }


def _execute_command_provider(
    *,
    packet: dict[str, Any],
    index: int,
    run_id: str,
    topic_id: str,
    command_args: list[str],
    command_run_dir: Path,
    allow_live_provider: bool,
    timeout_seconds: int,
    max_output_chars: int,
    provider_config_id: str | None = None,
    profile_role: str | None = None,
    operator_accepted: bool = False,
    operator_note_sha256: str | None = None,
) -> ProviderResponse:
    start = time.perf_counter()
    paper_id = str(packet.get("paper_id") or "")
    if not allow_live_provider:
        return ProviderResponse(
            status="failed",
            provider_name="command",
            run_id=run_id,
            paper_id=paper_id,
            reason_codes=["live_provider_not_allowed"],
            duration_seconds=round(time.perf_counter() - start, 4),
            provider_config_id=provider_config_id,
            profile_role=profile_role,
            operator_accepted=operator_accepted,
            operator_note_present=bool(operator_note_sha256),
            operator_note_sha256=operator_note_sha256,
        )
    if not command_args:
        return ProviderResponse(
            status="failed",
            provider_name="command",
            run_id=run_id,
            paper_id=paper_id,
            reason_codes=["missing_provider_command"],
            duration_seconds=round(time.perf_counter() - start, 4),
            provider_config_id=provider_config_id,
            profile_role=profile_role,
            operator_accepted=operator_accepted,
            operator_note_present=bool(operator_note_sha256),
            operator_note_sha256=operator_note_sha256,
        )

    paths = _command_artifact_paths(command_run_dir, index, packet)
    validation = provider_configurations.validate_command_template(
        command_args,
        packet_path=str(paths["packet"]),
        output_path=str(paths["provider_output"]),
        provider_run_id=run_id,
        topic_id=topic_id,
    )
    if not validation["valid"]:
        return ProviderResponse(
            status="failed",
            provider_name="command",
            run_id=run_id,
            paper_id=paper_id,
            reason_codes=["invalid_command_template", *list(validation.get("reasonCodes") or [])],
            duration_seconds=round(time.perf_counter() - start, 4),
            provider_config_id=provider_config_id,
            profile_role=profile_role,
            operator_accepted=operator_accepted,
            operator_note_present=bool(operator_note_sha256),
            operator_note_sha256=operator_note_sha256,
        )

    paths["packet"].parent.mkdir(parents=True, exist_ok=True)
    paths["provider_output"].parent.mkdir(parents=True, exist_ok=True)
    paths["packet"].write_text(json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    rendered_command = [str(item) for item in validation["renderedPreview"]]
    stdout_path: str | None = None
    stderr_path: str | None = None
    stdout_truncated = False
    stderr_truncated = False
    try:
        completed = subprocess.run(
            rendered_command,
            text=True,
            shell=False,
            capture_output=True,
            timeout=max(1, int(timeout_seconds)),
        )
    except subprocess.TimeoutExpired:
        return ProviderResponse(
            status="timed_out",
            provider_name="command",
            run_id=run_id,
            paper_id=paper_id,
            reason_codes=["provider_command_timed_out"],
            duration_seconds=round(time.perf_counter() - start, 4),
            packet_path=str(paths["packet"]),
            provider_output_path=str(paths["provider_output"]),
            provider_config_id=provider_config_id,
            profile_role=profile_role,
            operator_accepted=operator_accepted,
            operator_note_present=bool(operator_note_sha256),
            operator_note_sha256=operator_note_sha256,
        )
    except FileNotFoundError:
        return ProviderResponse(
            status="failed",
            provider_name="command",
            run_id=run_id,
            paper_id=paper_id,
            reason_codes=["provider_command_not_found"],
            duration_seconds=round(time.perf_counter() - start, 4),
            packet_path=str(paths["packet"]),
            provider_output_path=str(paths["provider_output"]),
            provider_config_id=provider_config_id,
            profile_role=profile_role,
            operator_accepted=operator_accepted,
            operator_note_present=bool(operator_note_sha256),
            operator_note_sha256=operator_note_sha256,
        )

    stdout_path, stdout_truncated = _write_bounded_text(paths["stdout"], str(completed.stdout or ""), max_output_chars)
    stderr_path, stderr_truncated = _write_bounded_text(paths["stderr"], str(completed.stderr or ""), max_output_chars)
    reason_codes: list[str] = []
    if stdout_truncated:
        reason_codes.append("provider_stdout_truncated")
    if stderr_truncated:
        reason_codes.append("provider_stderr_truncated")
    if completed.returncode != 0:
        response = ProviderResponse(
            status="failed",
            provider_name="command",
            run_id=run_id,
            paper_id=paper_id,
            raw_output=str(completed.stdout or "")[: max(1, int(max_output_chars))],
            reason_codes=[*reason_codes, "provider_command_failed"],
            duration_seconds=round(time.perf_counter() - start, 4),
            exit_code=int(completed.returncode),
            packet_path=str(paths["packet"]),
            provider_output_path=str(paths["provider_output"]),
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            provider_config_id=provider_config_id,
            profile_role=profile_role,
            operator_accepted=operator_accepted,
            operator_note_present=bool(operator_note_sha256),
            operator_note_sha256=operator_note_sha256,
        )
        _write_provider_output(paths["audit"], {"command": validation["redactedPreview"], **response.to_dict()})
        response.raw_output_path = str(paths["audit"])
        return response

    if paths["provider_output"].exists():
        raw_output, output_truncated, output_reasons = _read_provider_output(paths["provider_output"], max_output_chars)
        reason_codes.extend(output_reasons)
        output_suffix = paths["provider_output"].suffix
    else:
        stdout_text = str(completed.stdout or "")
        if len(stdout_text) > max(1, int(max_output_chars)):
            raw_output = stdout_text[: max(1, int(max_output_chars))]
            output_truncated = True
            reason_codes.append("provider_output_too_large")
        else:
            raw_output = stdout_text
            output_truncated = False
        output_suffix = ".jsonl" if "\n" in raw_output.strip() else ".json"

    parsed: dict[str, Any] | None = None
    if "provider_output_too_large" not in reason_codes:
        parsed, malformed_count = _parse_provider_output_text(raw_output, suffix=output_suffix)
        if malformed_count or parsed is None:
            reason_codes.append("malformed_json")
    status = "generated" if parsed is not None and "malformed_json" not in reason_codes else "rejected"
    if "provider_output_too_large" in reason_codes:
        status = "rejected"
        parsed = None

    response = ProviderResponse(
        status=status,
        provider_name="command",
        run_id=run_id,
        paper_id=paper_id,
        raw_output=raw_output,
        raw_output_path=str(paths["provider_output"] if paths["provider_output"].exists() else paths["stdout"] if stdout_path else paths["audit"]),
        parsed_draft=parsed,
        reason_codes=reason_codes,
        duration_seconds=round(time.perf_counter() - start, 4),
        exit_code=int(completed.returncode),
        packet_path=str(paths["packet"]),
        provider_output_path=str(paths["provider_output"]),
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        output_truncated=output_truncated,
        provider_config_id=provider_config_id,
        profile_role=profile_role,
        operator_accepted=operator_accepted,
        operator_note_present=bool(operator_note_sha256),
        operator_note_sha256=operator_note_sha256,
    )
    _write_provider_output(
        paths["audit"],
        {
            "provider": "command",
            "mode": "live_command",
            "run_id": run_id,
            "paper_id": paper_id,
            "command_redacted": validation["redactedPreview"],
            "status": response.status,
            "exit_code": response.exit_code,
            "packet_path": response.packet_path,
            "provider_output_path": response.provider_output_path,
            "stdout_path": response.stdout_path,
            "stderr_path": response.stderr_path,
            "stdout_truncated": response.stdout_truncated,
            "stderr_truncated": response.stderr_truncated,
            "output_truncated": response.output_truncated,
            "reason_codes": response.reason_codes,
            "created_at": iso_now_utc(),
        },
    )
    return response


def _store_accepted_draft(
    packet: dict[str, Any],
    response: ProviderResponse,
    *,
    provider_mode: str,
    provider_origin: str,
) -> dict[str, Any] | None:
    if packet.get("packet_version") in {"ai-review-packet-v3", "ai-review-packet-v4", "ai-review-packet-v5"}:
        raise ValueError("review_range_receipt_only")
    validation = response.validation_result or {}
    if validation.get("status") != "accepted":
        return None
    normalized = validation.get("normalized_draft") or response.parsed_draft or {}
    return ai_review_drafts.upsert_draft(
        {
            "topic_id": packet["topic_id"],
            "run_id": _review_run_id_for_packet(packet, packet["topic_id"]),
            "paper_id": packet["paper_id"],
            "source_text_sha256": str(normalized.get("source_text_sha256") or ""),
            "review_level": str(normalized.get("review_level") or "full_text_grounded"),
            "status": "accepted",
            "draft": normalized,
            "validation": {
                **validation,
                "provider": {
                    "name": response.provider_name,
                    "mode": provider_mode,
                    "origin": provider_origin,
                    "generated": True,
                    "raw_output_path": response.raw_output_path,
                    "run_id": response.run_id,
                    "provider_run_id": response.run_id,
                    "provider_config_id": response.provider_config_id,
                    "profile_role": response.profile_role,
                    "operator_accepted": response.operator_accepted,
                    "operator_note_present": response.operator_note_present,
                    "operator_note_sha256": response.operator_note_sha256,
                    "selected_for_report": True,
                },
            },
        },
        input_packet=packet if packet.get("packet_version") == PACKET_VERSION else None,
        input_origin=(
            "MANUAL_IMPORT_UNVERIFIED" if provider_mode == "manual_import"
            else "FIXTURE_SYNTHETIC" if response.provider_name == "stub" or response.profile_role == "fixture"
            or provider_origin == "command_fixture" else "PROVIDER_RESPONSE"
        ) if packet.get("packet_version") == PACKET_VERSION else None,
    )


def _summary_result(response: ProviderResponse, *, stored: bool) -> dict[str, Any]:
    item = response.to_dict()
    item["stored"] = stored
    if item.get("parsed_draft") is not None:
        item["parsed_draft"] = {
            "paper_id": item["parsed_draft"].get("paper_id"),
            "source_text_sha256": item["parsed_draft"].get("source_text_sha256"),
            "review_level": item["parsed_draft"].get("review_level"),
        }
    return item


def _manual_packet_filename(index: int, packet: dict[str, Any]) -> str:
    paper_id = _safe_fragment(str(packet.get("paper_id") or ""), f"paper-{index}")
    return f"{index:03d}-{paper_id}.json"


def _manual_run_artifact_paths(output_dir: Path) -> dict[str, Path]:
    return {
        "packets": output_dir / "packets",
        "packets_json": output_dir / "packets.json",
        "command_preview": output_dir / "command_preview.json",
        "readme": output_dir / "README.md",
        "schema": output_dir / "expected_output_schema.json",
        "manifest": output_dir / "provider_run_manifest.json",
        "fixture_output": output_dir / "fixture-provider-output.json",
        "provider_output": output_dir / "provider-output.jsonl",
    }


def _manual_run_readme(topic_id: str, run_id: str) -> str:
    return "\n".join(
        [
            "# Manual Provider Run Package",
            "",
            "This package contains bounded AI review packets and command previews for a local manual provider run.",
            "No provider command was executed while preparing these files.",
            "",
            f"- topic_id: `{topic_id}`",
            f"- provider_run_id: `{run_id}`",
            "- expected output: JSON or JSONL AI review drafts matching `expected_output_schema.json`",
            "- import path: run `python -m app.cli.review_refresh --topic <topic> --import-provider-drafts <output.jsonl> --validate-ai-review-drafts`",
            "",
            "Imported drafts are accepted only after grounded validation against packet hashes and evidence span IDs.",
        ]
    )


def prepare_manual_provider_run(
    topic_id: str,
    *,
    output_dir: str | Path,
    limit: int = 5,
    provider_run_id: str | None = None,
    command_template: list[str] | None = None,
    dry_run: bool = False,
    max_packet_chars: int = 12000,
) -> dict[str, Any]:
    output_path = Path(output_dir)
    run_id = provider_run_id or f"manual-provider-{iso_now_utc().replace(':', '').replace('+', 'Z')}"
    packet_payload = ai_review.build_ai_review_packets(topic_id, limit=limit, max_chars=max_packet_chars)
    packets = packet_payload["packets"][: max(0, int(limit))]
    artifacts = _manual_run_artifact_paths(output_path)
    template = command_template or DEFAULT_MANUAL_COMMAND_TEMPLATE
    packet_entries: list[dict[str, Any]] = []
    previews: list[dict[str, Any]] = []
    fixture_drafts: list[dict[str, Any]] = []

    for index, packet in enumerate(packets, start=1):
        packet_path = artifacts["packets"] / _manual_packet_filename(index, packet)
        output_draft_path = output_path / "provider-output.jsonl"
        validation = provider_configurations.validate_command_template(
            template,
            packet_path=str(packet_path),
            output_path=str(output_draft_path),
            provider_run_id=run_id,
            topic_id=topic_id,
        )
        packet_entries.append(
            {
                "index": index,
                "paper_id": packet.get("paper_id"),
                "packet_path": str(packet_path),
                "source_text_sha256": packet.get("source_text_sha256"),
                "evidence_span_count": len(packet.get("evidence_spans") or []),
            }
        )
        previews.append(
            {
                "index": index,
                "paper_id": packet.get("paper_id"),
                "command_template_validation": validation,
                "rendered_preview": validation["renderedPreview"],
                "redacted_preview": validation["redactedPreview"],
            }
        )
        fixture_drafts.append(ai_review.generate_fixture_ai_review_draft(packet))

    first_validation = previews[0]["command_template_validation"] if previews else provider_configurations.validate_command_template(template)
    planned_artifacts = [
        str(artifacts["packets_json"]),
        str(artifacts["command_preview"]),
        str(artifacts["readme"]),
        str(artifacts["schema"]),
        str(artifacts["manifest"]),
        str(artifacts["fixture_output"]),
        *[entry["packet_path"] for entry in packet_entries],
    ]
    summary = {
        "mode": "provider_manual_run_prepare",
        "topic_id": topic_id,
        "provider": "command",
        "provider_run_id": run_id,
        "provider_origin": "manual_command_prepare",
        "dry_run": bool(dry_run),
        "written": False,
        "no_command_executed": True,
        "packet_count": len(packets),
        "command_preview_count": len(previews),
        "output_dir": str(output_path),
        "planned_artifacts": planned_artifacts,
        "command_template_validation": first_validation,
        "packets": packets,
        "packet_entries": packet_entries,
        "command_previews": previews,
    }
    if dry_run:
        return summary

    artifacts["packets"].mkdir(parents=True, exist_ok=True)
    output_path.mkdir(parents=True, exist_ok=True)
    for entry, packet in zip(packet_entries, packets):
        Path(entry["packet_path"]).write_text(json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    artifacts["packets_json"].write_text(
        json.dumps({"topic_id": topic_id, "provider_run_id": run_id, "packets": packets}, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    artifacts["command_preview"].write_text(json.dumps(previews, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    artifacts["readme"].write_text(_manual_run_readme(topic_id, run_id), encoding="utf-8")
    artifacts["schema"].write_text(
        json.dumps(
            {
                "description": "Provider output must be a JSON object, JSON array, or JSONL stream of AI review draft objects.",
                "required_fields": [
                    "paper_id",
                    "source_text_sha256",
                    "review_level",
                    "contribution_summary",
                    "method_summary",
                    "evidence_summary",
                    "limitations",
                    "relevance_to_topic",
                    "cited_evidence_span_ids",
                    "confidence",
                ],
                "review_level": ["abstract_only", "full_text_grounded"],
                "full_text_grounded_requires": ["matching source_text_sha256", "at least one cited evidence span ID"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    artifacts["fixture_output"].write_text(json.dumps(fixture_drafts, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    manifest = {
        "topic_id": topic_id,
        "provider": "command",
        "provider_run_id": run_id,
        "provider_origin": "manual_command_prepare",
        "packet_count": len(packets),
        "command_preview_count": len(previews),
        "no_command_executed": True,
        "created_at": iso_now_utc(),
        "artifacts": {key: str(path) for key, path in artifacts.items()},
        "command_template_validation": first_validation,
    }
    artifacts["manifest"].write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    summary["written"] = True
    summary["manifest_path"] = str(artifacts["manifest"])
    operator_audit.record_event(
        action_type="prepare_provider_manual_run",
        decision="allowed",
        operator_id="cli-operator",
        auth_mode="local_operator",
        capability_required="prepare_provider_manual_run",
        capability_present=True,
        reason_codes=[],
        target_type="topic",
        target_id=topic_id,
        risk_level="provider_prepare",
        provider_config_id="command",
        provider_origin="manual_command_prepare",
        metadata={
            "provider_run_id": run_id,
            "packet_count": len(packets),
            "command_preview_count": len(previews),
            "output_dir": str(output_path),
            "no_command_executed": True,
            "command_template_valid": bool(first_validation.get("valid")),
        },
    )
    return summary


def _manual_output_items(path: Path) -> tuple[list[dict[str, Any]], int]:
    if path.suffix.lower() == ".jsonl":
        return read_jsonl_strict(path), 0
    text = path.read_text(encoding="utf-8")
    stripped = text.strip()
    if not stripped:
        return [], 0
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return [], 1
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)], sum(1 for item in parsed if not isinstance(item, dict))
    if isinstance(parsed, dict):
        drafts = parsed.get("drafts") or parsed.get("outputs") or parsed.get("items")
        if isinstance(drafts, list):
            return [item for item in drafts if isinstance(item, dict)], sum(1 for item in drafts if not isinstance(item, dict))
        return [parsed], 0
    return [], 1


def _extract_manual_draft(item: dict[str, Any]) -> dict[str, Any]:
    draft = item.get("draft") or item.get("parsed_draft") or item.get("ai_review_draft") or item
    return draft if isinstance(draft, dict) else {}


def import_manual_provider_outputs(
    topic_id: str,
    input_path: str | Path,
    *,
    provider_run_id: str | None = None,
    dry_run: bool = False,
    force_refresh: bool = False,
    skip_existing: bool = True,
    provider_origin: str = "manual_command_import",
) -> dict[str, Any]:
    path = Path(input_path)
    run_id = provider_run_id or f"manual-provider-import-{iso_now_utc().replace(':', '').replace('+', 'Z')}"
    packet_payload = ai_review.build_ai_review_packets(topic_id, limit=100_000, max_chars=12000)
    packets_by_paper = {str(packet.get("paper_id") or ""): packet for packet in packet_payload["packets"]}
    items, malformed_count = _manual_output_items(path)
    reason_counts: dict[str, int] = {}
    summary = {
        "mode": "manual_provider_output_import",
        "topic_id": topic_id,
        "provider": "command",
        "provider_origin": provider_origin,
        "provider_run_id": run_id,
        "dry_run": bool(dry_run),
        "input_path": str(path),
        "imported_count": len(items),
        "accepted_count": 0,
        "needs_review_count": 0,
        "rejected_count": 0,
        "skipped_existing_accepted_count": 0,
        "malformed_count": malformed_count,
        "wrong_sha256_count": 0,
        "missing_evidence_span_count": 0,
        "stored_draft_count": 0,
        "reason_counts": reason_counts,
        "results": [],
    }
    if malformed_count:
        reason_counts["malformed_json"] = malformed_count

    for item in items:
        draft = _extract_manual_draft(item)
        paper_id = str(draft.get("paper_id") or item.get("paper_id") or "")
        packet = packets_by_paper.get(paper_id)
        if packet is None:
            reasons = ["paper_not_in_packet_set"]
            summary["rejected_count"] += 1
            _increment_reason(reason_counts, reasons)
            summary["results"].append({"paper_id": paper_id, "status": "rejected", "reason_codes": reasons, "stored": False})
            continue
        response = ProviderResponse(
            status="generated",
            provider_name="command",
            run_id=run_id,
            paper_id=paper_id,
            raw_output=json.dumps(draft, ensure_ascii=False),
            raw_output_path=str(path),
            parsed_draft=draft,
        )
        response = _validate_response(packet, response)
        existing = _existing_accepted_draft(packet, topic_id) if response.status == "generated" and skip_existing and not force_refresh else None
        if existing:
            reasons = ["existing_accepted_draft"]
            summary["skipped_existing_accepted_count"] += 1
            _increment_reason(reason_counts, reasons)
            summary["results"].append({"paper_id": paper_id, "status": "skipped", "reason_codes": reasons, "stored": False})
            continue
        if response.status == "generated":
            summary["accepted_count"] += 1
        elif response.status == "needs_review":
            summary["needs_review_count"] += 1
        else:
            summary["rejected_count"] += 1
        if "source_text_sha256_mismatch" in response.reason_codes:
            summary["wrong_sha256_count"] += 1
        if "missing_cited_evidence_span_ids" in response.reason_codes:
            summary["missing_evidence_span_count"] += 1
        _increment_reason(reason_counts, response.reason_codes)
        stored_record = (
            _store_accepted_draft(packet, response, provider_mode="manual_import", provider_origin=provider_origin)
            if not dry_run
            else None
        )
        summary["stored_draft_count"] += 1 if stored_record is not None else 0
        summary["results"].append(_summary_result(response, stored=stored_record is not None))
    return summary


def _provider_readiness_item(
    provider_config: str | None,
    *,
    command_exists: Any | None = None,
) -> dict[str, Any]:
    profile = provider_configurations.provider_profile_by_id(provider_config)
    configs = [profile] if profile is not None else None
    readiness = provider_configurations.build_provider_readiness_summary(configs=configs, command_exists=command_exists)
    if profile is None and provider_config:
        for item in readiness.get("items") or []:
            if str(item.get("providerId") or "") == str(provider_config):
                return item
    return (readiness.get("items") or [{}])[0]


def _reason_keys(summary: dict[str, Any]) -> list[str]:
    reasons = list((summary.get("reason_counts") or {}).keys())
    for item in summary.get("results") or []:
        reasons.extend(str(reason) for reason in item.get("reason_codes") or [])
    return list(dict.fromkeys(reason for reason in reasons if reason))


def _acceptance_smoke_summary(
    *,
    topic_id: str,
    provider_config: str | None,
    provider_role: str | None,
    readiness_item: dict[str, Any],
    run_summary: dict[str, Any],
    limit: int,
    allow_live_provider: bool,
    accept_provider_risk: bool,
    readiness_only: bool = False,
) -> dict[str, Any]:
    reason_codes = list(dict.fromkeys([*list(readiness_item.get("reasonCodes") or []), *_reason_keys(run_summary)]))
    command_missing_count = int("provider_command_missing" in reason_codes) + int((run_summary.get("reason_counts") or {}).get("provider_command_not_found", 0))
    fixture_only = provider_role == "fixture"
    invoked_count = int(run_summary.get("invoked_count") or 0)
    return {
        "mode": "provider_acceptance_smoke",
        "topic_id": topic_id,
        "provider_config_id": provider_config,
        "provider_role": provider_role,
        "readiness_status": readiness_item.get("readinessStatus") or "not_configured",
        "operator_acceptance_required": bool(readiness_item.get("operatorAcceptanceRequired") or run_summary.get("operator_acceptance_required")),
        "operator_acceptance_present": bool(accept_provider_risk),
        "live_execution_allowed": bool(allow_live_provider and (fixture_only or accept_provider_risk)),
        "packet_limit": max(0, int(limit)),
        "planned_count": int(run_summary.get("planned_count") or 0),
        "invoked_count": invoked_count if not readiness_only else 0,
        "accepted_count": int(run_summary.get("accepted_count") or 0) if not readiness_only else 0,
        "needs_review_count": int(run_summary.get("needs_review_count") or 0) if not readiness_only else 0,
        "rejected_count": int(run_summary.get("rejected_count") or 0) if not readiness_only else 0,
        "skipped_existing_accepted_count": int(run_summary.get("skipped_existing_accepted_count") or 0),
        "malformed_count": int(run_summary.get("malformed_count") or 0) if not readiness_only else 0,
        "command_missing_count": command_missing_count,
        "failed_count": 0 if readiness_only and command_missing_count else int(run_summary.get("failed_count") or 0),
        "timed_out_count": int(run_summary.get("timed_out_count") or 0) if not readiness_only else 0,
        "external_provider_executed": bool(provider_role == "real_example" and invoked_count > 0 and not readiness_only),
        "fixture_only": fixture_only,
        "output_dir": run_summary.get("provider_run_dir") or run_summary.get("output_root"),
        "validation_gate": "m4_grounded_validation",
        "reason_codes": reason_codes,
        "warnings": list(dict.fromkeys(str(warning) for warning in readiness_item.get("warnings") or [] if str(warning).strip())),
        "provider_run_summary": run_summary,
    }


def run_provider_acceptance_smoke(
    topic_id: str,
    *,
    provider_config: str,
    dry_run: bool = False,
    limit: int = 2,
    output_root: str | Path = DEFAULT_PROVIDER_OUTPUT_ROOT,
    run_dir: str | Path | None = None,
    allow_live_provider: bool = False,
    accept_provider_risk: bool = False,
    provider_operator_note: str | None = None,
    import_provider_drafts: bool = False,
    force_refresh: bool = False,
    skip_existing: bool = True,
    provider_run_id: str | None = None,
    timeout_seconds: int = 30,
    max_output_chars: int = 20000,
    command_exists: Any | None = None,
) -> dict[str, Any]:
    """Run or preview a tiny provider acceptance smoke without dashboard execution."""

    readiness_item = _provider_readiness_item(provider_config, command_exists=command_exists)
    provider_role = str(readiness_item.get("profileRole") or "").strip() or None
    readiness_status = str(readiness_item.get("readinessStatus") or "")
    run_mode = "dry_run" if dry_run else "live_command"

    dry_run_summary = run_ai_review_provider(
        topic_id,
        provider="command",
        mode="dry_run",
        limit=limit,
        output_root=output_root,
        run_dir=run_dir,
        import_provider_drafts=import_provider_drafts,
        provider_config=provider_config,
        allow_live_provider=allow_live_provider,
        timeout_seconds=timeout_seconds,
        max_output_chars=max_output_chars,
        force_refresh=force_refresh,
        skip_existing=skip_existing,
        provider_run_id=provider_run_id,
        accept_provider_risk=accept_provider_risk,
        provider_operator_note=provider_operator_note,
    )
    if dry_run:
        return _acceptance_smoke_summary(
            topic_id=topic_id,
            provider_config=provider_config,
            provider_role=provider_role,
            readiness_item=readiness_item,
            run_summary=dry_run_summary,
            limit=limit,
            allow_live_provider=allow_live_provider,
            accept_provider_risk=accept_provider_risk,
        )

    acceptance_required = bool(
        readiness_item.get("operatorAcceptanceRequired") or dry_run_summary.get("operator_acceptance_required")
    )
    if acceptance_required and accept_provider_risk:
        operator_audit.record_event(
            action_type="provider_risk_acceptance",
            decision="allowed",
            operator_id="cli-operator",
            auth_mode="local_operator",
            capability_required="accept_provider_risk",
            capability_present=True,
            reason_codes=["operator_acceptance_present"],
            target_type="provider_config",
            target_id=provider_config,
            risk_level="provider_live_cli",
            provider_config_id=provider_config,
            provider_origin="live_command",
            metadata={
                "topic_id": topic_id,
                "provider_role": provider_role,
                "readiness_status": readiness_status,
                "operator_note_present": bool(_operator_note_digest(provider_operator_note)),
                "operator_note_sha256": _operator_note_digest(provider_operator_note),
                "allow_live_provider": bool(allow_live_provider),
            },
        )
    elif acceptance_required and not accept_provider_risk:
        operator_audit.record_event(
            action_type="provider_acceptance_smoke",
            decision="denied",
            operator_id="cli-operator",
            auth_mode="local_operator",
            capability_required="run_real_provider_cli",
            capability_present=bool(allow_live_provider),
            reason_codes=["operator_acceptance_required"],
            target_type="provider_config",
            target_id=provider_config,
            risk_level="provider_live_cli",
            provider_config_id=provider_config,
            provider_origin="live_command",
            metadata={
                "topic_id": topic_id,
                "provider_role": provider_role,
                "readiness_status": readiness_status,
                "allow_live_provider": bool(allow_live_provider),
            },
        )

    if provider_role == "real_example" and readiness_status == "command_missing" and accept_provider_risk:
        return _acceptance_smoke_summary(
            topic_id=topic_id,
            provider_config=provider_config,
            provider_role=provider_role,
            readiness_item=readiness_item,
            run_summary=dry_run_summary,
            limit=limit,
            allow_live_provider=allow_live_provider,
            accept_provider_risk=accept_provider_risk,
            readiness_only=True,
        )

    run_summary = run_ai_review_provider(
        topic_id,
        provider="command",
        mode=run_mode,
        limit=limit,
        output_root=output_root,
        run_dir=run_dir,
        import_provider_drafts=import_provider_drafts,
        provider_config=provider_config,
        allow_live_provider=allow_live_provider,
        timeout_seconds=timeout_seconds,
        max_output_chars=max_output_chars,
        force_refresh=force_refresh,
        skip_existing=skip_existing,
        provider_run_id=provider_run_id,
        accept_provider_risk=accept_provider_risk,
        provider_operator_note=provider_operator_note,
    )
    return _acceptance_smoke_summary(
        topic_id=topic_id,
        provider_config=provider_config,
        provider_role=provider_role,
        readiness_item=readiness_item,
        run_summary=run_summary,
        limit=limit,
        allow_live_provider=allow_live_provider,
        accept_provider_risk=accept_provider_risk,
    )


def run_ai_review_provider(
    topic_id: str,
    *,
    provider: str = "stub",
    mode: ProviderInvocationMode = "dry_run",
    limit: int = 5,
    output_root: str | Path = DEFAULT_PROVIDER_OUTPUT_ROOT,
    run_dir: str | Path | None = None,
    import_provider_drafts: bool = False,
    command: str | list[str] | None = None,
    provider_config: str | None = None,
    allow_live_provider: bool = False,
    timeout_seconds: int = 30,
    max_output_chars: int = 20000,
    stub_scenarios: list[str] | None = None,
    force_refresh: bool = False,
    skip_existing: bool = True,
    provider_run_id: str | None = None,
    provider_origin: str | None = None,
    accept_provider_risk: bool = False,
    provider_operator_note: str | None = None,
) -> dict[str, Any]:
    packet_payload = ai_review.build_ai_review_packets(topic_id, limit=limit, max_chars=12000)
    packets = packet_payload["packets"][: max(0, int(limit))]
    invocation_mode = "dry_run" if str(mode or "dry_run") == "dry_run" else str(mode)
    run_id = provider_run_id or f"ai-provider-{iso_now_utc().replace(':', '').replace('+', 'Z')}"
    origin = _provider_origin(provider, invocation_mode, provider_origin)
    output_root_path = Path(output_root)
    command_args, provider_profile = _command_args_for_provider_config(provider_config, command)
    provider_config_id = str(provider_config or (provider_profile or {}).get("providerId") or "").strip() or None
    profile_role = str((provider_profile or {}).get("profileRole") or "").strip() or None
    acceptance_required = _provider_requires_operator_acceptance(
        provider=provider,
        invocation_mode=invocation_mode,
        profile=provider_profile,
        provider_config=provider_config,
    )
    operator_note_sha256 = _operator_note_digest(provider_operator_note)
    operator_note_present = bool(operator_note_sha256)
    operator_accepted = bool(accept_provider_risk) if acceptance_required else False
    if provider_config == FIXTURE_COMMAND_PROVIDER_CONFIG_ID and provider_origin is None:
        origin = "command_fixture"
    command_run_dir = _command_run_dir(output_root_path, run_id, run_dir) if str(provider).strip().lower() == "command" else None
    reason_counts: dict[str, int] = {}
    skipped_not_full_text = len(packet_payload.get("skipped") or [])
    summary = {
        "mode": "ai_review_provider_run",
        "topic_id": packet_payload["topic_id"],
        "provider": provider,
        "invocation_mode": invocation_mode,
        "provider_mode": invocation_mode,
        "provider_run_id": run_id,
        "provider_origin": origin,
        "provider_config_id": provider_config_id,
        "profile_role": profile_role,
        "dry_run": invocation_mode == "dry_run",
        "live_allowed": bool(allow_live_provider),
        "command_present": bool(command_args),
        "operator_acceptance_required": bool(acceptance_required),
        "operator_accepted": bool(operator_accepted),
        "operator_note_present": operator_note_present,
        "operator_note_sha256": operator_note_sha256,
        "force_refresh": bool(force_refresh),
        "skip_existing": bool(skip_existing),
        "run_id": run_id,
        "planned_count": len(packets),
        "invoked_count": 0,
        "succeeded_count": 0,
        "generated_count": 0,
        "accepted_count": 0,
        "needs_review_count": 0,
        "rejected_count": 0,
        "failed_count": 0,
        "timed_out_count": 0,
        "malformed_count": 0,
        "output_truncated_count": 0,
        "stored_draft_count": 0,
        "skipped_existing_accepted_count": 0,
        "skipped_not_full_text_grounded_count": skipped_not_full_text,
        "reason_counts": reason_counts,
        "output_root": str(output_root_path),
        "provider_run_dir": str(command_run_dir) if command_run_dir is not None else None,
        "results": [],
    }
    if invocation_mode == "dry_run":
        results: list[dict[str, Any]] = []
        for packet in packets:
            existing = _existing_accepted_draft(packet, packet["topic_id"]) if import_provider_drafts and skip_existing and not force_refresh else None
            if existing:
                reasons = ["existing_accepted_draft"]
                summary["skipped_existing_accepted_count"] += 1
                _increment_reason(reason_counts, reasons)
                results.append(
                    {
                        "status": "skipped",
                        "provider": provider,
                        "run_id": run_id,
                        "paper_id": packet["paper_id"],
                        "reason_codes": reasons,
                        "stored": False,
                        "existing_draft": {
                            "run_id": existing.get("run_id"),
                            "source_text_sha256": existing.get("source_text_sha256"),
                            "status": existing.get("status"),
                        },
                    }
                )
                continue
            results.append(
                {
                    "status": "planned",
                    "provider": provider,
                    "run_id": run_id,
                    "paper_id": packet["paper_id"],
                    "reason_codes": [],
                    "stored": False,
                    "planned_invocation": _planned_invocation(
                        provider=provider,
                        invocation_mode=invocation_mode,
                        command_args=command_args,
                        allow_live_provider=allow_live_provider,
                        timeout_seconds=timeout_seconds,
                        max_output_chars=max_output_chars,
                        packet=packet,
                    ),
                }
            )
        summary["results"] = results
        return summary

    if provider == "command" and profile_role != "fixture":
        decision = provider_configurations.build_provider_routing_decision(
            provider_config_id or provider,
            purpose="research_output",
            provider_readiness_summary=provider_configurations.build_provider_readiness_summary(
                configs=[provider_profile] if provider_profile else None
            ),
        )
        reasons = [
            "provider_research_output_blocked",
            "not_allowed_for_research_output",
            str(decision.get("canonicalStatus") or "not_supported"),
            *list(decision.get("reasonCodes") or []),
        ]
        _increment_reason(reason_counts, reasons)
        summary["failed_count"] = len(packets)
        summary["results"] = [
            {
                "status": "failed",
                "provider": provider,
                "run_id": run_id,
                "paper_id": packet["paper_id"],
                "reason_codes": list(dict.fromkeys(reasons)),
                "stored": False,
                "routing_decision": decision,
            }
            for packet in packets
        ]
        return summary

    adapter = _provider_for_name(provider)
    scenarios = stub_scenarios or ["valid"] * len(packets)
    for index, packet in enumerate(packets, start=1):
        existing = _existing_accepted_draft(packet, packet["topic_id"]) if import_provider_drafts and skip_existing and not force_refresh else None
        if existing:
            reasons = ["existing_accepted_draft"]
            summary["skipped_existing_accepted_count"] += 1
            _increment_reason(reason_counts, reasons)
            summary["results"].append(
                {
                    "status": "skipped",
                    "provider": provider,
                    "run_id": run_id,
                    "paper_id": packet["paper_id"],
                    "reason_codes": reasons,
                    "stored": False,
                    "existing_draft": {
                        "run_id": existing.get("run_id"),
                        "source_text_sha256": existing.get("source_text_sha256"),
                        "status": existing.get("status"),
                    },
                }
            )
            continue
        scenario = scenarios[index - 1] if index - 1 < len(scenarios) else "valid"
        request = ProviderRequest(
            provider_name=provider,
            packet=deepcopy(packet),
            mode="stub" if provider == "stub" else "live_command",
            run_id=run_id,
            max_output_chars=max_output_chars,
            timeout_seconds=timeout_seconds,
            explicit_live=bool(allow_live_provider),
            command=command_args,
            scenario=scenario,
        )
        if provider == "command":
            if acceptance_required and not accept_provider_risk:
                response = _operator_acceptance_failure(
                    packet=packet,
                    run_id=run_id,
                    provider_config_id=provider_config_id,
                    profile_role=profile_role,
                )
            else:
                response = _execute_command_provider(
                    packet=packet,
                    index=index,
                    run_id=run_id,
                    topic_id=topic_id,
                    command_args=command_args,
                    command_run_dir=command_run_dir or output_root_path / _safe_fragment(run_id, "provider-run"),
                    allow_live_provider=allow_live_provider,
                    timeout_seconds=timeout_seconds,
                    max_output_chars=max_output_chars,
                    provider_config_id=provider_config_id,
                    profile_role=profile_role,
                    operator_accepted=operator_accepted,
                    operator_note_sha256=operator_note_sha256,
                )
        else:
            response = adapter.generate(request)
        if response.status not in {"failed", "timed_out"}:
            response = _validate_response(packet, response)
        command_preflight_failure = provider == "command" and bool(
            {"live_provider_not_allowed", "missing_provider_command", "invalid_command_template", "operator_acceptance_required"}
            & set(response.reason_codes)
        )
        summary["invoked_count"] += 0 if command_preflight_failure else 1
        if provider == "command" and response.exit_code == 0:
            summary["succeeded_count"] += 1
        if response.parsed_draft is not None:
            summary["generated_count"] += 1
        if response.status == "generated":
            summary["accepted_count"] += 1
        elif response.status == "needs_review":
            summary["needs_review_count"] += 1
        elif response.status == "rejected":
            summary["rejected_count"] += 1
        elif response.status == "failed":
            summary["failed_count"] += 1
        elif response.status == "timed_out":
            summary["timed_out_count"] += 1
        if "malformed_json" in response.reason_codes:
            summary["malformed_count"] += 1
        if response.stdout_truncated:
            summary["output_truncated_count"] += 1
        if response.stderr_truncated:
            summary["output_truncated_count"] += 1
        if response.output_truncated:
            summary["output_truncated_count"] += 1
        _increment_reason(reason_counts, response.reason_codes)
        if provider != "command" and response.reason_codes != ["live_provider_not_allowed"]:
            raw_path = _provider_output_path(output_root_path, run_id, index, response.paper_id)
            payload = {
                "provider": provider,
                "mode": request.mode,
                "origin": origin,
                "run_id": run_id,
                "paper_id": response.paper_id,
                "packet_source_text_sha256": packet.get("source_text_sha256"),
                "status": response.status,
                "raw_output": response.raw_output,
                "parsed_draft": response.parsed_draft,
                "validation_result": response.validation_result,
                "reason_codes": response.reason_codes,
                "created_at": iso_now_utc(),
            }
            _write_provider_output(raw_path, payload)
            response.raw_output_path = str(raw_path)
        stored_record = (
            _store_accepted_draft(packet, response, provider_mode=request.mode, provider_origin=origin)
            if import_provider_drafts
            else None
        )
        summary["stored_draft_count"] += 1 if stored_record is not None else 0
        summary["results"].append(_summary_result(response, stored=stored_record is not None))
    return summary
