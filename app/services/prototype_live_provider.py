"""Prototype-only, one-shot live Provider report drafting.

This service binds an in-memory one-use activation token to one synthetic run
and one exact Codex SDK route. Provider output is written to a run-local
quarantine before validation and can become a report version only after a
durable explicit user-acceptance record exists.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any, Awaitable, Callable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.repositories import paper_reviews
from app.repositories.files import read_json, read_text
from app.repositories.runs import build_run_paths, load_run_record
from app.services import codex_cli_report_draft, exports, report_versions
from app.services.codex_cli_output_schema import (
    SCHEMA_PROFILE_VERSION,
    build_codex_cli_report_draft_schema,
)
from app.services.codex_session_probe import codex_cli_is_installed
from app.services.providers import codex_sdk_provider
from app.services.report_draft_assist_registry import (
    ReportDraftAssistError,
    describe_report_draft_assist,
    require_codex_selected_for_live_generation,
)


SCHEMA_VERSION = "prototype_live_provider_proof_v1"
DRAFT_SCHEMA_VERSION = "prototype_provider_report_draft_v1"
PROTOTYPE_RUN_SCHEMA = "prototype_research_vertical_slice_v1"
PROVIDER_ID = "codex"
SURFACE_ID = "codex_sdk_python"
STAGE = "report_draft_assist"
INTERNAL_OUTPUT_CLASS = "report_draft"
PRODUCT_OUTPUT_CLASS = "weekly_report"
ACTIVATION_TTL_SECONDS = 180
LIVE_TIMEOUT_SECONDS = 300
MAX_REQUEST_BYTES = 65_536
MAX_OUTPUT_BYTES = 65_536
EXECUTION_DIRNAME = "prototype-provider-live"
BASELINE_SOURCE = "prototype_deterministic_baseline"
PROVIDER_SOURCE = "prototype_provider_assisted"
EVIDENCE_ID_PATTERN = re.compile(r"^(?P<paper_id>.+)-evidence-(?P<index>\d+)$")
CODEX_CLI_CWD_ENV = "XRML_CODEX_CLI_CWD"


class PrototypeLiveProviderError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 409) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class PrototypeProviderRoute(_StrictModel):
    provider_id: Literal["codex"] = PROVIDER_ID
    execution_surface: Literal["codex_sdk_python"] = SURFACE_ID
    stage: Literal["report_draft_assist"] = STAGE
    internal_output_class: Literal["report_draft"] = INTERNAL_OUTPUT_CLASS
    product_output_class: Literal["weekly_report"] = PRODUCT_OUTPUT_CLASS


class DraftStatement(_StrictModel):
    text: str = Field(min_length=1, max_length=1_600)
    statement_type: Literal["grounded", "inference", "proposal", "uncertainty"]
    evidence_ids: list[str] = Field(default_factory=list, max_length=12)
    strength: Literal["low", "medium", "high"]

    @model_validator(mode="after")
    def grounded_requires_evidence(self) -> "DraftStatement":
        if self.statement_type == "grounded" and not self.evidence_ids:
            raise ValueError("grounded_statement_requires_evidence")
        return self


class GroundedClaim(_StrictModel):
    claim_text: str = Field(min_length=1, max_length=1_600)
    evidence_ids: list[str] = Field(min_length=1, max_length=12)
    strength: Literal["low", "medium", "high"]
    claim_type: Literal["grounded"] = "grounded"


class PrototypeProviderDraft(_StrictModel):
    schema_version: Literal["prototype_provider_report_draft_v1"]
    run_id: str = Field(min_length=1, max_length=220)
    provider_id: Literal["codex"]
    execution_surface: Literal["codex_sdk_python"]
    stage: Literal["report_draft_assist"]
    internal_output_class: Literal["report_draft"]
    product_output_class: Literal["weekly_report"]
    title: str = Field(min_length=1, max_length=320)
    executive_summary: str = Field(min_length=1, max_length=3_200)
    reviewed_literature_overview: list[DraftStatement] = Field(min_length=1, max_length=12)
    thematic_findings: list[DraftStatement] = Field(min_length=1, max_length=12)
    grounded_claims: list[GroundedClaim] = Field(min_length=1, max_length=20)
    limitations_and_uncertainties: list[DraftStatement] = Field(min_length=1, max_length=12)
    next_research_questions: list[DraftStatement] = Field(min_length=1, max_length=12)
    referenced_evidence_ids: list[str] = Field(min_length=1, max_length=40)
    inference_or_proposal_sections: list[DraftStatement] = Field(default_factory=list, max_length=12)


@dataclass
class _ActivationTokenState:
    token_digest: str
    run_id: str
    route: dict[str, str]
    request_digest: str
    session_digest: str
    issued_at: datetime
    expires_at: datetime
    consumed: bool = False


_STATE_LOCK = threading.Lock()
_ACTIVE_SESSION_DIGEST: str | None = None
_TOKENS: dict[str, _ActivationTokenState] = {}
_SESSION_CONSUMED: set[str] = set()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: Any) -> str:
    payload = value if isinstance(value, bytes) else _canonical_bytes(value)
    return hashlib.sha256(payload).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _route_dict() -> dict[str, str]:
    return PrototypeProviderRoute().model_dump()


def _session_digest() -> str:
    session_id = os.getenv("XRML_PROTOTYPE_SESSION_ID", "").strip()
    if not session_id or len(session_id) > 256:
        raise PrototypeLiveProviderError("prototype_live_session_required")
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _sync_memory_session(session_digest: str) -> None:
    global _ACTIVE_SESSION_DIGEST
    if _ACTIVE_SESSION_DIGEST == session_digest:
        return
    _TOKENS.clear()
    _SESSION_CONSUMED.clear()
    _ACTIVE_SESSION_DIGEST = session_digest


def reset_ephemeral_activation_state_for_tests() -> None:
    """Clear process-local activation state; production code never calls this."""

    global _ACTIVE_SESSION_DIGEST
    with _STATE_LOCK:
        _TOKENS.clear()
        _SESSION_CONSUMED.clear()
        _ACTIVE_SESSION_DIGEST = None


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    value = host.strip().strip("[]")
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return value.lower() == "localhost"


def _live_flag_enabled() -> bool:
    return os.getenv("XRML_PROTOTYPE_LIVE_PROVIDER_PROOF", "").strip() == "1"


def _prototype_mode_enabled() -> bool:
    return os.getenv("XRML_PROTOTYPE_MODE", "").strip() == "1"


def _dependency_available() -> bool:
    return bool(codex_cli_is_installed())


def _paper_id_from_evidence_id(evidence_id: str) -> str | None:
    match = EVIDENCE_ID_PATTERN.fullmatch(evidence_id)
    return match.group("paper_id") if match else None


def _cli_workdir(execution_dir: Path) -> Path:
    configured = os.getenv(CODEX_CLI_CWD_ENV, "").strip()
    if configured:
        workdir = Path(configured)
        workdir.mkdir(parents=True, exist_ok=True)
        return workdir
    workdir = execution_dir / "cli-read-only-cwd"
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir


def _require_exact_route(route: Mapping[str, Any]) -> dict[str, str]:
    try:
        parsed = PrototypeProviderRoute.model_validate(dict(route))
    except ValidationError as exc:
        raise PrototypeLiveProviderError("prototype_live_route_mismatch", status_code=400) from exc
    return parsed.model_dump()


def _require_synthetic_run(run_id: str) -> dict[str, Any]:
    try:
        run = load_run_record(run_id)
    except FileNotFoundError as exc:
        raise PrototypeLiveProviderError("prototype_run_not_found", status_code=404) from exc
    prototype = run.get("prototype") if isinstance(run.get("prototype"), dict) else {}
    if prototype.get("schema_version") != PROTOTYPE_RUN_SCHEMA or prototype.get("corpus_kind") != "synthetic":
        raise PrototypeLiveProviderError("prototype_synthetic_run_required")
    if run.get("status") != "completed" or prototype.get("report_status") != "ready":
        raise PrototypeLiveProviderError("prototype_completed_report_required")
    return run


def _report_path_and_content(run: Mapping[str, Any]) -> tuple[Path, str]:
    artifacts = run.get("artifacts") if isinstance(run.get("artifacts"), dict) else {}
    report_path = Path(str(artifacts.get("report_markdown") or ""))
    if not report_path.is_file():
        raise PrototypeLiveProviderError("prototype_deterministic_report_missing")
    content = read_text(report_path)
    if not content.strip():
        raise PrototypeLiveProviderError("prototype_deterministic_report_empty")
    return report_path, content


def _report_outline(content: str) -> list[dict[str, str]]:
    sections: list[dict[str, str]] = []
    title = "Overview"
    body: list[str] = []
    for line in content.splitlines():
        if line.startswith("## "):
            if body:
                sections.append({"title": title, "summary": " ".join(body).strip()[:700]})
            title = line[3:].strip() or "Untitled"
            body = []
        elif not line.startswith("# ") and line.strip():
            body.append(line.strip())
    if body:
        sections.append({"title": title, "summary": " ".join(body).strip()[:700]})
    return sections[:12]


def _bounded_text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _build_request_bundle(run_id: str) -> dict[str, Any]:
    run = _require_synthetic_run(run_id)
    report_path, report_content = _report_path_and_content(run)
    topic_id = str(run.get("topic_profile_id") or "")
    reviews = paper_reviews.list_reviews(topic_id=topic_id, run_id=run_id, limit=8, read_only=True)
    if len(reviews) != 4:
        raise PrototypeLiveProviderError("prototype_four_review_context_required")

    evidence: list[dict[str, Any]] = []
    papers: list[dict[str, Any]] = []
    for review in reviews:
        paper_id = str(review.get("paper_id") or "")
        spans = review.get("evidence_spans") if isinstance(review.get("evidence_spans"), list) else []
        if len(spans) < 2:
            raise PrototypeLiveProviderError("prototype_evidence_context_incomplete")
        evidence_ids: list[str] = []
        for index, span in enumerate(spans[:4], start=1):
            evidence_id = f"{paper_id}-evidence-{index}"
            evidence_ids.append(evidence_id)
            evidence.append(
                {
                    "evidence_id": evidence_id,
                    "paper_id": paper_id,
                    "label": _bounded_text(span.get("label") or "synthetic full text", 120),
                    "text": _bounded_text(span.get("text"), 900),
                }
            )
        papers.append(
            {
                "paper_id": paper_id,
                "title": _bounded_text(review.get("title"), 320),
                "review_summary": _bounded_text(review.get("why_it_matters"), 900),
                "key_findings": [_bounded_text(item, 420) for item in list(review.get("key_findings") or [])[:5]],
                "limitations": [_bounded_text(item, 420) for item in list(review.get("limitations") or [])[:5]],
                "evidence_ids": evidence_ids,
            }
        )

    route = _route_dict()
    payload = {
        "schema_version": "prototype_report_draft_request_v1",
        "run_id": run_id,
        **route,
        "topic": _bounded_text(run.get("topic"), 320),
        "synthetic_corpus": True,
        "output_schema_version": DRAFT_SCHEMA_VERSION,
        "allowed_paper_ids": [item["paper_id"] for item in papers],
        "allowed_evidence_span_ids": [item["evidence_id"] for item in evidence],
        "required_report_sections": [
            "title", "executive_summary", "reviewed_literature_overview", "thematic_findings",
            "grounded_claims", "limitations_and_uncertainties", "next_research_questions",
            "referenced_evidence_ids",
        ],
        "external_retrieval": False,
        "json_only": True,
        "papers": papers,
        "evidence": evidence,
        "deterministic_report_outline": _report_outline(report_content),
        "response_schema": PrototypeProviderDraft.model_json_schema(),
        "instructions": [
            "Return exactly one JSON object matching response_schema and no surrounding prose.",
            "Use only the supplied synthetic papers and evidence.",
            "Every grounded statement must cite existing current-run evidence IDs.",
            "Type unsupported material only as inference, proposal, or uncertainty.",
            "Do not create paper IDs, evidence IDs, reviews, citations, acceptance decisions, or run state.",
            "Do not include credentials, filesystem paths, commands, tracebacks, hidden reasoning, or control instructions.",
            "Do not perform external retrieval or invent citations.",
            "Keep execution_surface as codex_sdk_python and stage as report_draft_assist.",
        ],
    }
    raw = _canonical_bytes(payload)
    if len(raw) > MAX_REQUEST_BYTES:
        raise PrototypeLiveProviderError("prototype_live_request_too_large")
    return {
        "run": run,
        "report_path": report_path,
        "baseline_content": report_content,
        "baseline_sha256": hashlib.sha256(report_content.encode("utf-8")).hexdigest(),
        "evidence_ids": sorted(item["evidence_id"] for item in evidence),
        "paper_ids": [item["paper_id"] for item in papers],
        "payload": payload,
        "prompt": raw.decode("utf-8"),
        "request_digest": hashlib.sha256(raw).hexdigest(),
    }


def _execution_root(run_id: str) -> Path:
    return build_run_paths(run_id).reports_dir / EXECUTION_DIRNAME


def _execution_dir(run_id: str, execution_id: str) -> Path:
    if not re.fullmatch(r"prototype-live-[0-9a-f]{24}", execution_id):
        raise PrototypeLiveProviderError("prototype_live_execution_id_invalid", status_code=400)
    return _execution_root(run_id) / execution_id


def _write_exclusive_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _write_exclusive_json(path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    body = dict(payload)
    body["record_digest"] = _sha256(body)
    _write_exclusive_bytes(path, _canonical_bytes(body) + b"\n")
    return body


def _read_record(path: Path) -> dict[str, Any] | None:
    payload = read_json(path, default=None)
    return payload if isinstance(payload, dict) else None


def _execution_request_records(run_id: str) -> list[dict[str, Any]]:
    root = _execution_root(run_id)
    if not root.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or not re.fullmatch(r"prototype-live-[0-9a-f]{24}", directory.name):
            continue
        request = _read_record(directory / "request.json")
        if request:
            records.append(request)
    return records


def _session_invocation_count(run_id: str, session_digest: str) -> int:
    persisted = sum(
        1
        for record in _execution_request_records(run_id)
        if record.get("prototype_session_digest") == session_digest
        and int(record.get("logical_invocation_count") or 0) == 1
    )
    return max(persisted, 1 if session_digest in _SESSION_CONSUMED else 0)


def _ensure_baseline_version(bundle: Mapping[str, Any]) -> dict[str, Any]:
    run_id = str(bundle["run"]["run_id"])
    versions = report_versions.list_report_versions(run_id).get("items") or []
    for version in versions:
        if (
            version.get("generation_mode") == "deterministic_fallback"
            and version.get("content_sha256") == bundle["baseline_sha256"]
        ):
            return version
    created = report_versions.record_completed_report_version(
        run_id,
        source=BASELINE_SOURCE,
        source_artifact_path=str(bundle["report_path"]),
        content=str(bundle["baseline_content"]),
        generation_metadata={
            "generation_mode": "deterministic_fallback",
            "evidence_references": list(bundle["evidence_ids"]),
            "display_label": "Deterministic fallback",
        },
    )
    if created is None:
        raise PrototypeLiveProviderError("prototype_baseline_version_unavailable")
    return created


_VALIDATION_REASON_MAP = {
    "provider_output_not_strict_json": "CODEX_CANDIDATE_JSON_PARSE_FAILED",
    "provider_output_schema_invalid": "CODEX_CANDIDATE_SCHEMA_INVALID",
    "provider_output_unknown_evidence": "CODEX_EVIDENCE_REFERENCE_INVALID",
    "provider_output_unknown_paper": "CODEX_EVIDENCE_REFERENCE_INVALID",
    "provider_output_evidence_index_mismatch": "CODEX_EVIDENCE_REFERENCE_INVALID",
}


def _acceptance_state(
    *,
    state: str,
    accepted: bool,
    decision: Mapping[str, Any] | None,
) -> str:
    if accepted or state == "ACCEPTED":
        return "accepted"
    if decision and str(decision.get("decision") or "").upper() == "REJECT":
        return "rejected"
    if state == "VALIDATED_PENDING_USER_ACCEPTANCE":
        return "pending"
    return "not_attempted"


def _precise_rejection_reasons(
    provider_result: Mapping[str, Any],
    validation: Mapping[str, Any],
) -> list[str]:
    reasons: list[str] = []
    provider_reason = str(provider_result.get("reason_category") or "").strip()
    if provider_reason:
        reasons.append(provider_reason)
    for item in validation.get("reasons") or []:
        text_item = str(item)
        reasons.append(_VALIDATION_REASON_MAP.get(text_item, text_item))
    return list(dict.fromkeys(reasons))[:12]


def describe_live_availability() -> dict[str, Any]:
    prototype_mode = _prototype_mode_enabled()
    availability_flag = _live_flag_enabled()
    dependency_available = _dependency_available()
    session_available = bool(os.getenv("XRML_PROTOTYPE_SESSION_ID", "").strip())
    endpoint_available = prototype_mode and availability_flag and dependency_available and session_available
    reasons: list[str] = []
    if not prototype_mode:
        reasons.append("prototype_mode_required")
    if not availability_flag:
        reasons.append("live_provider_proof_disabled")
    if not dependency_available:
        reasons.append("codex_cli_unavailable")
    if not session_available:
        reasons.append("prototype_live_session_required")
    registry = describe_report_draft_assist(refresh=False)
    raw_providers = registry.get("providers")
    providers: list[Any] = raw_providers if isinstance(raw_providers, list) else []
    selected_codex: Any = {}
    for item in providers:
        if isinstance(item, dict) and item.get("provider_id") == "codex":
            selected_codex = item
            break
    codex: dict[str, Any] = selected_codex if isinstance(selected_codex, dict) else {}
    provider_authenticated = bool(codex.get("authenticated"))
    if provider_authenticated and endpoint_available:
        authenticated_readiness = "ready"
    elif str(codex.get("reason_code") or "codex_session_not_probed") in {"codex_session_not_probed", "codex_session_status_unclassified"}:
        authenticated_readiness = "unknown" if endpoint_available else "unavailable"
    elif not provider_authenticated:
        authenticated_readiness = "unavailable"
    else:
        authenticated_readiness = "unknown" if endpoint_available else "unavailable"
    return {
        "schema_version": SCHEMA_VERSION,
        "provider_id": PROVIDER_ID,
        "execution_surface": SURFACE_ID,
        "stage": STAGE,
        "internal_output_class": INTERNAL_OUTPUT_CLASS,
        "product_output_class": PRODUCT_OUTPUT_CLASS,
        "default_state": "disabled",
        "availability_flag": availability_flag,
        "endpoint_available": endpoint_available,
        "adapter_available": dependency_available,
        "authenticated_execution_readiness": authenticated_readiness,
        "deterministic_fallback_available": True,
        "selected_provider_id": registry.get("selected_provider_id"),
        "provider_available": bool(codex.get("available") or dependency_available),
        "provider_authenticated": provider_authenticated,
        "provider_activated": registry.get("activation_state") == "activated",
        "official_cli_installed": bool(codex.get("installed")),
        "official_cli_version": codex.get("version"),
        "session_reason_code": codex.get("reason_code"),
        "last_probe_at": registry.get("last_probe_at"),
        "reasons": reasons,
    }


def issue_activation_token(
    *,
    run_id: str,
    route: Mapping[str, Any],
    client_host: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    availability = describe_live_availability()
    if not availability["endpoint_available"]:
        raise PrototypeLiveProviderError("prototype_live_provider_unavailable")
    if not _is_loopback(client_host):
        raise PrototypeLiveProviderError("prototype_live_loopback_required", status_code=403)
    try:
        require_codex_selected_for_live_generation()
    except ReportDraftAssistError as exc:
        raise PrototypeLiveProviderError(exc.code, status_code=exc.status_code) from exc
    exact_route = _require_exact_route(route)
    bundle = _build_request_bundle(run_id)
    baseline_version = _ensure_baseline_version(bundle)
    session_digest = _session_digest()
    issued_at = now or _utc_now()
    expires_at = issued_at + timedelta(seconds=ACTIVATION_TTL_SECONDS)
    raw_token = secrets.token_urlsafe(36)
    token_digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    with _STATE_LOCK:
        _sync_memory_session(session_digest)
        if _session_invocation_count(run_id, session_digest) > 0:
            raise PrototypeLiveProviderError("prototype_live_call_budget_consumed")
        for digest, state in list(_TOKENS.items()):
            if state.session_digest == session_digest and state.run_id == run_id and not state.consumed:
                del _TOKENS[digest]
        _TOKENS[token_digest] = _ActivationTokenState(
            token_digest=token_digest,
            run_id=run_id,
            route=exact_route,
            request_digest=str(bundle["request_digest"]),
            session_digest=session_digest,
            issued_at=issued_at,
            expires_at=expires_at,
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "activation_token": raw_token,
        "token_hint": token_digest[:10],
        "expires_at": _iso(expires_at),
        "request_digest": bundle["request_digest"],
        "route": exact_route,
        "baseline_version_id": baseline_version.get("version_id"),
        "one_live_call_budget": "unused",
        "explicit_confirmation_required": True,
    }


def _all_candidate_text(candidate: Mapping[str, Any]) -> str:
    return json.dumps(candidate, ensure_ascii=False, sort_keys=True)


def _content_safety_reasons(candidate: Mapping[str, Any]) -> list[str]:
    text = _all_candidate_text(candidate)
    patterns = {
        "credential_like_content_detected": r"(?i)(?:bearer\s+[a-z0-9._-]{10,}|sk-[a-z0-9_-]{12,}|access[_-]?token\s*[:=]|api[_-]?key\s*[:=]|client[_-]?secret\s*[:=])",
        "absolute_local_path_detected": r"(?i)(?:[a-z]:[\\/][^\s\"']+|/(?:users|home|tmp|var|etc|opt|mnt)/[^\s\"']+)",
        "traceback_or_raw_error_detected": r"(?i)(?:traceback \(most recent call last\)|stack trace|file \"[^\"]+\", line \d+)",
        "control_or_executable_instruction_detected": r"(?i)(?:ignore (?:all )?previous instructions|powershell(?:\.exe)?\b|cmd\.exe\b|bash\s+-c\b|rm\s+-rf\b|curl\s+https?://|invoke-webrequest|<script\b)",
    }
    return [code for code, pattern in patterns.items() if re.search(pattern, text)]


def validate_quarantined_output(
    *,
    raw_output: bytes,
    run_id: str,
    evidence_ids: list[str],
    paper_ids: list[str] | None = None,
) -> dict[str, Any]:
    reasons: list[str] = []
    if len(raw_output) > MAX_OUTPUT_BYTES:
        reasons.append("provider_output_too_large")
    try:
        payload = json.loads(raw_output.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {
            "valid": False,
            "candidate": None,
            "reasons": [*reasons, "provider_output_not_strict_json"],
            "schema_valid": False,
            "route_valid": False,
            "evidence_valid": False,
            "content_safety_valid": False,
            "unknown_evidence_count": 0,
        }
    try:
        candidate_model = PrototypeProviderDraft.model_validate(payload)
    except ValidationError:
        return {
            "valid": False,
            "candidate": None,
            "reasons": [*reasons, "provider_output_schema_invalid"],
            "schema_valid": False,
            "route_valid": False,
            "evidence_valid": False,
            "content_safety_valid": False,
            "unknown_evidence_count": 0,
        }
    candidate = candidate_model.model_dump()
    route_valid = all(
        candidate.get(key) == value
        for key, value in {
            "run_id": run_id,
            "provider_id": PROVIDER_ID,
            "execution_surface": SURFACE_ID,
            "stage": STAGE,
            "internal_output_class": INTERNAL_OUTPUT_CLASS,
            "product_output_class": PRODUCT_OUTPUT_CLASS,
        }.items()
    )
    if not route_valid:
        reasons.append("provider_output_route_mismatch")

    known = set(evidence_ids)
    referenced = {str(item) for item in candidate["referenced_evidence_ids"]}
    used: set[str] = set()
    for claim in candidate["grounded_claims"]:
        used.update(str(item) for item in claim["evidence_ids"])
    for section in (
        "reviewed_literature_overview",
        "thematic_findings",
        "limitations_and_uncertainties",
        "next_research_questions",
        "inference_or_proposal_sections",
    ):
        for item in candidate[section]:
            used.update(str(value) for value in item["evidence_ids"])
    unknown = sorted((referenced | used) - known)
    if unknown:
        reasons.append("provider_output_unknown_evidence")
    if not used.issubset(referenced) or not referenced.issubset(used):
        reasons.append("provider_output_evidence_index_mismatch")
    paper_ok = True
    if paper_ids is not None:
        allowed_papers = set(paper_ids)
        cited_papers: set[str] = set()
        malformed_evidence_id = False
        for evidence_id in referenced | used:
            paper_id = _paper_id_from_evidence_id(evidence_id)
            if paper_id is None:
                malformed_evidence_id = True
                continue
            cited_papers.add(paper_id)
        if malformed_evidence_id or (cited_papers - allowed_papers):
            reasons.append("provider_output_unknown_paper")
            paper_ok = False
    evidence_valid = not unknown and used == referenced and bool(used) and paper_ok

    typed_non_grounded = [
        item
        for section in (
            "reviewed_literature_overview",
            "thematic_findings",
            "limitations_and_uncertainties",
            "next_research_questions",
            "inference_or_proposal_sections",
        )
        for item in candidate[section]
        if item["statement_type"] != "grounded"
    ]
    safety_reasons = _content_safety_reasons(candidate)
    reasons.extend(safety_reasons)
    return {
        "valid": not reasons and route_valid and evidence_valid,
        "candidate": candidate if not reasons and route_valid and evidence_valid else None,
        "reasons": list(dict.fromkeys(reasons)),
        "schema_valid": True,
        "route_valid": route_valid,
        "evidence_valid": evidence_valid,
        "content_safety_valid": not safety_reasons,
        "unknown_evidence_count": len(unknown),
        "grounded_claim_count": len(candidate["grounded_claims"]),
        "unsupported_content_label_count": len(typed_non_grounded),
        "referenced_evidence_count": len(referenced),
    }


def _candidate_markdown(candidate: Mapping[str, Any]) -> str:
    def statements(items: list[dict[str, Any]]) -> str:
        return "\n".join(
            f"- **{item['statement_type']} · {item['strength']}** — {item['text']}"
            + (f" _(evidence: {', '.join(item['evidence_ids'])})_" if item["evidence_ids"] else "")
            for item in items
        )

    claims = "\n".join(
        f"- **grounded · {item['strength']}** — {item['claim_text']} _(evidence: {', '.join(item['evidence_ids'])})_"
        for item in candidate["grounded_claims"]
    )
    optional = candidate.get("inference_or_proposal_sections") or []
    blocks = [
        f"# {candidate['title']}",
        "## Executive summary\n\n" + candidate["executive_summary"],
        "## Reviewed literature overview\n\n" + statements(candidate["reviewed_literature_overview"]),
        "## Thematic findings\n\n" + statements(candidate["thematic_findings"]),
        "## Grounded claims\n\n" + claims,
        "## Limitations and uncertainties\n\n" + statements(candidate["limitations_and_uncertainties"]),
        "## Next research questions\n\n" + statements(candidate["next_research_questions"]),
    ]
    if optional:
        blocks.append("## Inferences and proposals\n\n" + statements(optional))
    blocks.append("## Evidence references\n\n" + "\n".join(f"- {item}" for item in candidate["referenced_evidence_ids"]))
    return "\n\n".join(blocks).rstrip() + "\n"


async def generate_live_candidate(
    *,
    run_id: str,
    route: Mapping[str, Any],
    activation_token: str,
    confirmed: bool,
    client_host: str | None,
    sdk_runner: Callable[[str, Mapping[str, Any], str], Awaitable[Any]] | None = None,
    cli_runner: Callable[..., Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not confirmed:
        raise PrototypeLiveProviderError("prototype_live_explicit_confirmation_required", status_code=400)
    availability = describe_live_availability()
    if not availability["endpoint_available"]:
        raise PrototypeLiveProviderError("prototype_live_provider_unavailable")
    if not _is_loopback(client_host):
        raise PrototypeLiveProviderError("prototype_live_loopback_required", status_code=403)
    try:
        require_codex_selected_for_live_generation()
    except ReportDraftAssistError as exc:
        raise PrototypeLiveProviderError(exc.code, status_code=exc.status_code) from exc
    exact_route = _require_exact_route(route)
    token_digest = hashlib.sha256(str(activation_token).encode("utf-8")).hexdigest()
    session_digest = _session_digest()
    evaluated_at = now or _utc_now()

    with _STATE_LOCK:
        _sync_memory_session(session_digest)
        state = _TOKENS.get(token_digest)
        if state is None:
            raise PrototypeLiveProviderError("prototype_activation_token_invalid", status_code=403)
        if state.consumed:
            raise PrototypeLiveProviderError("prototype_activation_token_replayed", status_code=403)
        if evaluated_at >= state.expires_at:
            state.consumed = True
            raise PrototypeLiveProviderError("prototype_activation_token_expired", status_code=403)
        if state.session_digest != session_digest:
            state.consumed = True
            raise PrototypeLiveProviderError("prototype_activation_token_session_mismatch", status_code=403)
        if state.run_id != run_id:
            state.consumed = True
            raise PrototypeLiveProviderError("prototype_activation_token_run_mismatch", status_code=403)
        if state.route != exact_route:
            state.consumed = True
            raise PrototypeLiveProviderError("prototype_activation_token_route_mismatch", status_code=403)

    bundle = _build_request_bundle(run_id)
    baseline_version = _ensure_baseline_version(bundle)

    with _STATE_LOCK:
        state = _TOKENS.get(token_digest)
        if state is None or state.consumed:
            raise PrototypeLiveProviderError("prototype_activation_token_replayed", status_code=403)
        if state.request_digest != bundle["request_digest"]:
            state.consumed = True
            raise PrototypeLiveProviderError("prototype_activation_token_request_mismatch", status_code=403)
        if _session_invocation_count(run_id, session_digest) > 0:
            state.consumed = True
            raise PrototypeLiveProviderError("prototype_live_call_budget_consumed")
        state.consumed = True
        _SESSION_CONSUMED.add(session_digest)

    execution_id = f"prototype-live-{secrets.token_hex(12)}"
    directory = _execution_dir(run_id, execution_id)
    started_at = evaluated_at
    request_record = _write_exclusive_json(
        directory / "request.json",
        {
            "schema_version": SCHEMA_VERSION,
            "execution_id": execution_id,
            "run_id": run_id,
            **exact_route,
            "state": "PENDING",
            "activation_mode": "prototype_local_one_shot",
            "activation_token_digest": token_digest,
            "request_digest": bundle["request_digest"],
            "prototype_session_digest": session_digest,
            "invocation_attempted": True,
            "logical_invocation_count": 1,
            "timeout_seconds": LIVE_TIMEOUT_SECONDS,
            "start_timestamp": _iso(started_at),
            "completion_timestamp": None,
            "result_category": None,
            "model_backend_identifier": None,
            "raw_output_digest": None,
            "stderr_error_digest": None,
            "raw_output_byte_count": 0,
            "schema_valid": False,
            "evidence_valid": False,
            "route_valid": False,
            "accepted": False,
            "rejected": False,
            "rejection_reasons": [],
            "fallback_used": True,
            "fallback_reason": "candidate_not_accepted",
            "accepted_output_digest": None,
            "resulting_report_version_id": None,
            "baseline_report_sha256": bundle["baseline_sha256"],
            "baseline_version_id": baseline_version.get("version_id"),
        },
    )

    sandbox_dir = _cli_workdir(directory)
    (sandbox_dir / "allowed-paper-ids.json").write_text(
        json.dumps(list(bundle["paper_ids"]), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (sandbox_dir / "allowed-evidence-span-ids.json").write_text(
        json.dumps(list(bundle["evidence_ids"]), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    started_clock = time.monotonic()
    if sdk_runner is not None:
        provider_result = await codex_sdk_provider.run_codex_sdk_prototype_report_draft_once(
            prompt=str(bundle["prompt"]),
            output_schema=PrototypeProviderDraft.model_json_schema(),
            cwd=str(sandbox_dir),
            timeout_seconds=LIVE_TIMEOUT_SECONDS,
            sdk_runner=sdk_runner,
        )
    else:
        cli_schema = build_codex_cli_report_draft_schema(PrototypeProviderDraft.model_json_schema())
        (sandbox_dir / "cli-schema-profile.json").write_text(
            json.dumps(
                {
                    "schema_profile_version": SCHEMA_PROFILE_VERSION,
                    "canonical_model": "PrototypeProviderDraft",
                },
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        provider_result = await asyncio.to_thread(
            codex_cli_report_draft.run_codex_cli_report_draft_once,
            prompt=str(bundle["prompt"]),
            output_schema=cli_schema,
            cwd=str(sandbox_dir),
            timeout_seconds=LIVE_TIMEOUT_SECONDS,
            runner=cli_runner,
        )
    measured_duration_ms = round((time.monotonic() - started_clock) * 1000)
    completed_at = _utc_now()
    raw_output = str(provider_result.get("raw_output") or "").encode("utf-8")
    validation: dict[str, Any] = {
        "valid": False,
        "candidate": None,
        "reasons": [str(provider_result.get("reason_category") or "provider_invocation_failed")],
        "schema_valid": False,
        "route_valid": False,
        "evidence_valid": False,
        "content_safety_valid": False,
        "unknown_evidence_count": 0,
        "grounded_claim_count": 0,
        "unsupported_content_label_count": 0,
        "referenced_evidence_count": 0,
    }
    raw_digest: str | None = None
    if provider_result.get("status") == "completed":
        _write_exclusive_bytes(directory / "quarantine.raw", raw_output)
        raw_digest = hashlib.sha256(raw_output).hexdigest()
        validation = validate_quarantined_output(
            raw_output=raw_output,
            run_id=run_id,
            evidence_ids=list(bundle["evidence_ids"]),
            paper_ids=list(bundle["paper_ids"]),
        )
        if validation["valid"]:
            _write_exclusive_json(directory / "candidate.json", validation["candidate"])

    current_baseline_sha = hashlib.sha256(read_text(bundle["report_path"]).encode("utf-8")).hexdigest()
    if current_baseline_sha != bundle["baseline_sha256"]:
        validation["valid"] = False
        validation["candidate"] = None
        validation["reasons"] = [*validation["reasons"], "deterministic_baseline_changed_during_invocation"]

    state_value = "VALIDATED_PENDING_USER_ACCEPTANCE" if validation["valid"] else (
        "REJECTED" if provider_result.get("status") == "completed" else "FAILED"
    )
    rejection_reasons = [] if validation["valid"] else _precise_rejection_reasons(provider_result, validation)
    outcome = _write_exclusive_json(
        directory / "outcome.json",
        {
            "schema_version": SCHEMA_VERSION,
            "execution_id": execution_id,
            "request_record_digest": request_record["record_digest"],
            "state": state_value,
            "completion_timestamp": _iso(completed_at),
            "logical_invocation_count": 1,
            "provider_call_count": int(provider_result.get("provider_call_count") or 0),
            "timeout_seconds": LIVE_TIMEOUT_SECONDS,
            "timed_out": provider_result.get("status") == "timeout",
            "result_category": provider_result.get("result_category"),
            "model_backend_identifier": provider_result.get("model_backend_identifier"),
            "duration_ms": provider_result.get("duration_ms") or measured_duration_ms,
            "raw_output_digest": raw_digest,
            "stderr_error_digest": _sha256(str(provider_result.get("reason_category"))) if provider_result.get("reason_category") else None,
            "raw_output_byte_count": len(raw_output),
            "schema_valid": bool(validation["schema_valid"]),
            "evidence_valid": bool(validation["evidence_valid"]),
            "route_valid": bool(validation["route_valid"]),
            "content_safety_valid": bool(validation["content_safety_valid"]),
            "unknown_evidence_count": int(validation["unknown_evidence_count"]),
            "grounded_claim_count": int(validation.get("grounded_claim_count") or 0),
            "unsupported_content_label_count": int(validation.get("unsupported_content_label_count") or 0),
            "referenced_evidence_count": int(validation.get("referenced_evidence_count") or 0),
            "accepted": False,
            "rejected": not validation["valid"],
            "acceptance_state": "pending" if validation["valid"] else "not_attempted",
            "rejection_reasons": rejection_reasons,
            "fallback_used": True,
            "fallback_reason": None if validation["valid"] else (rejection_reasons[0] if rejection_reasons else "provider_invocation_failed"),
            "accepted_output_digest": None,
            "resulting_report_version_id": None,
            "trusted_persistence_count": 0,
            "baseline_report_unchanged": current_baseline_sha == bundle["baseline_sha256"],
            "exit_code": provider_result.get("exit_code"),
            "safe_diagnostics": provider_result.get("safe_diagnostics"),
        },
    )
    return _aggregate_execution(run_id, execution_id, request=request_record, outcome=outcome)


def _aggregate_execution(
    run_id: str,
    execution_id: str,
    *,
    request: dict[str, Any] | None = None,
    outcome: dict[str, Any] | None = None,
) -> dict[str, Any]:
    directory = _execution_dir(run_id, execution_id)
    request = request or _read_record(directory / "request.json") or {}
    outcome = outcome or _read_record(directory / "outcome.json") or {}
    decision = _read_record(directory / "user-decision.json")
    accepted_version = _read_record(directory / "accepted-version.json")
    state = str(outcome.get("state") or request.get("state") or "UNKNOWN")
    if decision and decision.get("decision") == "REJECT":
        state = "REJECTED"
    if accepted_version:
        state = "ACCEPTED"
    accepted = state == "ACCEPTED"
    rejected = state == "REJECTED"
    return {
        "schema_version": SCHEMA_VERSION,
        "execution_id": execution_id,
        "run_id": run_id,
        "provider_id": request.get("provider_id"),
        "execution_surface": request.get("execution_surface"),
        "stage": request.get("stage"),
        "internal_output_class": request.get("internal_output_class"),
        "product_output_class": request.get("product_output_class"),
        "state": state,
        "invocation_attempted": bool(request.get("invocation_attempted")),
        "logical_invocation_count": int(outcome.get("logical_invocation_count") or request.get("logical_invocation_count") or 0),
        "provider_call_count": int(outcome.get("provider_call_count") or 0),
        "start_timestamp": request.get("start_timestamp"),
        "completion_timestamp": outcome.get("completion_timestamp"),
        "timeout_seconds": request.get("timeout_seconds"),
        "timed_out": bool(outcome.get("timed_out")),
        "duration_ms": outcome.get("duration_ms"),
        "model_backend_identifier": outcome.get("model_backend_identifier"),
        "result_category": outcome.get("result_category"),
        "schema_valid": bool(outcome.get("schema_valid")),
        "route_valid": bool(outcome.get("route_valid")),
        "evidence_valid": bool(outcome.get("evidence_valid")),
        "content_safety_valid": bool(outcome.get("content_safety_valid")),
        "unknown_evidence_count": int(outcome.get("unknown_evidence_count") or 0),
        "grounded_claim_count": int(outcome.get("grounded_claim_count") or 0),
        "unsupported_content_label_count": int(outcome.get("unsupported_content_label_count") or 0),
        "referenced_evidence_count": int(outcome.get("referenced_evidence_count") or 0),
        "candidate_available": (directory / "candidate.json").is_file(),
        "accepted": accepted,
        "rejected": rejected,
        "acceptance_state": _acceptance_state(state=state, accepted=accepted, decision=decision),
        "rejection_reasons": list(outcome.get("rejection_reasons") or []) if not decision else list(decision.get("reasons") or outcome.get("rejection_reasons") or []),
        "fallback_used": not accepted,
        "fallback_reason": None if accepted else (outcome.get("fallback_reason") or "candidate_not_accepted"),
        "reason_category": None if accepted else (
            (list(outcome.get("rejection_reasons") or [])[:1] or [outcome.get("fallback_reason")])[0]
        ),
        "accepted_output_digest": accepted_version.get("accepted_output_digest") if accepted_version else None,
        "resulting_report_version_id": accepted_version.get("report_version_id") if accepted_version else None,
        "baseline_version_id": request.get("baseline_version_id"),
        "baseline_report_sha256": request.get("baseline_report_sha256"),
        "baseline_report_unchanged": outcome.get("baseline_report_unchanged"),
        "trusted_persistence_count": 1 if accepted else 0,
        "request_digest": request.get("request_digest"),
        "raw_output_digest": outcome.get("raw_output_digest"),
        "raw_output_byte_count": int(outcome.get("raw_output_byte_count") or 0),
        "exit_code": outcome.get("exit_code"),
        "safe_diagnostics": outcome.get("safe_diagnostics") if isinstance(outcome.get("safe_diagnostics"), dict) else None,
        "record_digest": accepted_version.get("record_digest") if accepted_version else outcome.get("record_digest") or request.get("record_digest"),
    }


def list_execution_history(run_id: str) -> list[dict[str, Any]]:
    _require_synthetic_run(run_id)
    return [
        _aggregate_execution(run_id, str(record["execution_id"]), request=record)
        for record in _execution_request_records(run_id)
    ]


def get_live_proof_status(run_id: str) -> dict[str, Any]:
    _require_synthetic_run(run_id)
    availability = describe_live_availability()
    history = list_execution_history(run_id)
    latest = history[-1] if history else None
    authenticated_readiness = availability["authenticated_execution_readiness"]
    if latest and latest["result_category"] == "provider_output_received":
        authenticated_readiness = "ready"
    elif latest and latest["result_category"] == "provider_unavailable":
        authenticated_readiness = "unavailable"
    versions = report_versions.list_report_versions(run_id).get("items") or []
    version_summaries = [
        {
            "version_id": item.get("version_id"),
            "version_number": item.get("version_number"),
            "generation_mode": item.get("generation_mode") or "deterministic",
            "display_label": item.get("display_label") or item.get("title"),
            "content_sha256": item.get("content_sha256"),
            "provider_id": item.get("provider_id"),
            "execution_record_id": item.get("execution_record_id"),
        }
        for item in versions
    ]
    return {
        **availability,
        "run_id": run_id,
        "authenticated_execution_readiness": authenticated_readiness,
        "one_live_call_budget": "consumed" if any(item["logical_invocation_count"] for item in history) else "unused",
        "history": history,
        "latest_execution": latest,
        "report_versions": version_summaries,
    }


def get_candidate_preview(run_id: str, execution_id: str) -> dict[str, Any]:
    execution = _aggregate_execution(run_id, execution_id)
    if not execution["candidate_available"] or execution["state"] not in {"VALIDATED_PENDING_USER_ACCEPTANCE", "ACCEPTED"}:
        raise PrototypeLiveProviderError("prototype_validated_candidate_unavailable", status_code=404)
    candidate = _read_record(_execution_dir(run_id, execution_id) / "candidate.json")
    if not candidate:
        raise PrototypeLiveProviderError("prototype_validated_candidate_unavailable", status_code=404)
    candidate.pop("record_digest", None)
    return {
        "run_id": run_id,
        "execution_id": execution_id,
        "state": execution["state"],
        "markdown": _candidate_markdown(candidate),
        "grounded_claim_count": execution["grounded_claim_count"],
        "referenced_evidence_count": execution["referenced_evidence_count"],
        "accepted": execution["accepted"],
    }


def _find_version_for_execution(run_id: str, execution_id: str) -> dict[str, Any] | None:
    versions = report_versions.list_report_versions(run_id).get("items") or []
    return next((item for item in versions if item.get("execution_record_id") == execution_id), None)


def accept_candidate(*, run_id: str, execution_id: str, confirmed: bool) -> dict[str, Any]:
    if not confirmed:
        raise PrototypeLiveProviderError("prototype_provider_acceptance_confirmation_required", status_code=400)
    execution = _aggregate_execution(run_id, execution_id)
    if execution["state"] == "REJECTED":
        raise PrototypeLiveProviderError("prototype_rejected_candidate_cannot_be_accepted")
    if execution["state"] == "ACCEPTED":
        return execution
    if execution["state"] != "VALIDATED_PENDING_USER_ACCEPTANCE":
        raise PrototypeLiveProviderError("prototype_validated_candidate_unavailable")

    directory = _execution_dir(run_id, execution_id)
    candidate = _read_record(directory / "candidate.json")
    if not candidate:
        raise PrototypeLiveProviderError("prototype_validated_candidate_unavailable")
    candidate.pop("record_digest", None)
    candidate_bytes = _canonical_bytes(candidate)
    accepted_digest = hashlib.sha256(candidate_bytes).hexdigest()
    bundle = _build_request_bundle(run_id)
    if bundle["baseline_sha256"] != execution["baseline_report_sha256"]:
        raise PrototypeLiveProviderError("prototype_deterministic_baseline_changed")

    decision_path = directory / "user-decision.json"
    decision = _read_record(decision_path)
    if decision is None:
        decision = _write_exclusive_json(
            decision_path,
            {
                "schema_version": SCHEMA_VERSION,
                "execution_id": execution_id,
                "decision": "ACCEPT",
                "explicit_user_confirmation": True,
                "accepted_output_digest": accepted_digest,
                "decided_at": _iso(_utc_now()),
                "reasons": [],
            },
        )
    elif decision.get("decision") != "ACCEPT":
        raise PrototypeLiveProviderError("prototype_rejected_candidate_cannot_be_accepted")

    existing_version = _find_version_for_execution(run_id, execution_id)
    if existing_version is None:
        markdown = _candidate_markdown(candidate)
        created = report_versions.record_completed_report_version(
            run_id,
            source=PROVIDER_SOURCE,
            source_artifact_path=str(directory / "candidate.json"),
            content=markdown,
            generation_metadata={
                "generation_mode": "provider_assisted",
                "provider_id": PROVIDER_ID,
                "execution_record_id": execution_id,
                "accepted_output_digest": accepted_digest,
                "evidence_references": list(candidate["referenced_evidence_ids"]),
                "baseline_version_id": execution["baseline_version_id"],
                "acceptance_record_digest": decision["record_digest"],
                "display_label": "Provider-assisted",
            },
        )
        if created is None:
            raise PrototypeLiveProviderError("prototype_provider_report_version_unavailable")
        existing_version = created

    completion_path = directory / "accepted-version.json"
    if not completion_path.exists():
        _write_exclusive_json(
            completion_path,
            {
                "schema_version": SCHEMA_VERSION,
                "execution_id": execution_id,
                "decision_record_digest": decision["record_digest"],
                "accepted_output_digest": accepted_digest,
                "report_version_id": existing_version["version_id"],
                "baseline_version_id": execution["baseline_version_id"],
                "accepted_at": _iso(_utc_now()),
                "trusted_persistence_count": 1,
            },
        )
    return _aggregate_execution(run_id, execution_id)


def reject_candidate(*, run_id: str, execution_id: str, confirmed: bool) -> dict[str, Any]:
    if not confirmed:
        raise PrototypeLiveProviderError("prototype_provider_rejection_confirmation_required", status_code=400)
    execution = _aggregate_execution(run_id, execution_id)
    if execution["state"] == "ACCEPTED":
        raise PrototypeLiveProviderError("prototype_accepted_candidate_cannot_be_rejected")
    if execution["state"] == "REJECTED":
        return execution
    if execution["state"] != "VALIDATED_PENDING_USER_ACCEPTANCE":
        raise PrototypeLiveProviderError("prototype_validated_candidate_unavailable")
    decision_path = _execution_dir(run_id, execution_id) / "user-decision.json"
    if not decision_path.exists():
        _write_exclusive_json(
            decision_path,
            {
                "schema_version": SCHEMA_VERSION,
                "execution_id": execution_id,
                "decision": "REJECT",
                "explicit_user_confirmation": True,
                "accepted_output_digest": None,
                "decided_at": _iso(_utc_now()),
                "reasons": ["CODEX_CANDIDATE_REJECTED"],
            },
        )
    return _aggregate_execution(run_id, execution_id)


def preview_report_version(run_id: str, version_id: str) -> dict[str, Any]:
    detail = report_versions.get_report_version_detail(run_id, version_id)
    version = detail["version"]
    return {
        "run_id": run_id,
        "version_id": version_id,
        "generation_mode": version.get("generation_mode") or "deterministic",
        "display_label": version.get("display_label") or version.get("title"),
        "content": detail.get("content"),
        "provider_id": version.get("provider_id"),
        "execution_record_id": version.get("execution_record_id"),
        "accepted_output_digest": version.get("accepted_output_digest"),
        "evidence_references": list(version.get("evidence_references") or []),
    }


def export_report_version(run_id: str, version_id: str) -> dict[str, Any]:
    return exports.export_report_version_markdown(run_id, version_id)


def compare_accepted_to_baseline(run_id: str, execution_id: str) -> dict[str, Any]:
    execution = _aggregate_execution(run_id, execution_id)
    if not execution["accepted"]:
        raise PrototypeLiveProviderError("prototype_accepted_report_version_required")
    return report_versions.diff_report_versions(
        run_id,
        from_version_id=str(execution["baseline_version_id"]),
        to_version_id=str(execution["resulting_report_version_id"]),
    )
