"""Fail-closed Codex SDK provider adapter.

The default provider remains blocked.  Prototype v0.2 adds one deliberately
narrow, owner-authorized entry point for a typed report-draft turn.  That entry
point never reads credentials, never changes model configuration, uses an
ephemeral read-only SDK thread, and never retries.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import importlib
import importlib.util
import json
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from app.repositories.files import iso_now_utc


PROVIDER_ID = "codex"
PROVIDER_FAMILY_ID = "codex"
PROFILE_ID = "codex_sdk_python"
EXECUTION_SURFACE_TYPE = "sdk"
ADAPTER_KIND = "sdk_adapter_scaffold"
BACKEND_ROLE = "primary_ai_backend_candidate"
PROVIDER_AUTH_SCOPE = "provider_auth_ai_backend"
SDK_PACKAGE_NAME = "openai-codex"
SDK_IMPORT_NAME = "openai_codex"
SDK_ALTERNATE_IMPORT_NAMES: tuple[str, ...] = ("codex",)
SDK_IMPORT_NAME_CANDIDATES: tuple[str, ...] = (SDK_IMPORT_NAME, *SDK_ALTERNATE_IMPORT_NAMES)
CODEX_SDK_DEPENDENCY_GROUP = "codex-sdk"
CODEX_SDK_LOCK_UPDATE_COMMAND_CANDIDATE = "uv lock"
CODEX_SDK_INSTALL_COMMAND_CANDIDATES: tuple[str, ...] = (
    "pip install openai-codex",
    'python -m pip install "openai-codex"',
    'python -m pip install -e ".[codex-sdk]"',
    'uv pip install "openai-codex"',
    "uv sync --extra codex-sdk",
)
CODEX_SDK_DEPENDENCY_READINESS_STATES: tuple[str, ...] = (
    "dependency_declared",
    "dependency_installed_unknown_not_checked",
    "dependency_missing_observed_from_prior_live_attempt",
    "dependency_install_required",
    "dependency_lock_update_required",
    "sdk_import_deferred",
    "provider_auth_unverified",
    "tiny_proof_missing",
    "output_class_proof_missing",
)
AUTH_ENV_NAME_CANDIDATES: tuple[str, ...] = (
    "CODEX_ACCESS_TOKEN",
    "CODEX_API_KEY",
    "XRML_CODEX_SDK_PROVIDER_MANAGED_AUTH_APPROVED",
)
SUPPORTED_TASK_KINDS: tuple[str, ...] = (
    "tiny_sentinel",
    "paper_review",
    "weekly_report",
    "topic_recommendation",
)
PRODUCT_OUTPUT_CLASS_MAP: dict[str, str] = {
    "paper_review": "research_output",
    "weekly_report": "report_draft",
    "research_output": "research_output",
    "report_draft": "report_draft",
}
SUPPORTED_OUTPUT_CLASS_CANDIDATES: tuple[str, ...] = ("research_output", "report_draft")
PROOF_MODE = "tiny_live"
TINY_PROOF_KIND = "sdk_sentinel_prompt"
ARTIFACT_KIND = "provider_tiny_proof"
PROOF_STATUS_CANDIDATE = "live_verified_tiny"
PROMPT_CLASS = "sentinel_only"
INPUT_SIZE_CLASS = "tiny"
OUTPUT_CLASS_ARTIFACT_KIND = "provider_output_class_proof"
OUTPUT_CLASS_PROOF_MODE = "output_class_live"
PAPER_REVIEW_PRODUCT_OUTPUT_CLASS = "paper_review"
PAPER_REVIEW_INTERNAL_OUTPUT_CLASS = "research_output"
PAPER_REVIEW_REQUIRED_PROOF_STATUS = "research_output_verified_tiny"
PAPER_REVIEW_PROMPT_CLASS = "paper_review_json_only"
WEEKLY_REPORT_PRODUCT_OUTPUT_CLASS = "weekly_report"
WEEKLY_REPORT_INTERNAL_OUTPUT_CLASS = "report_draft"
WEEKLY_REPORT_REQUIRED_PROOF_STATUS = "report_draft_verified_tiny"
WEEKLY_REPORT_PROMPT_CLASS = "weekly_report_json_only"
OUTPUT_CLASS_ALLOWED_ARTIFACT_PREFIX = "output/provider-output-class-proof/"
PAPER_REVIEW_JSON_FIELDS: tuple[str, ...] = (
    "output_class",
    "internal_output_class",
    "provider_family_id",
    "surface_id",
    "topic_id",
    "topic_name",
    "paper_id",
    "paper_title",
    "evidence_span_refs_used",
    "concise_review_summary",
    "methodology_or_core_approach",
    "key_findings",
    "limitations",
    "relevance_to_topic",
    "follow_up_topic_suggestions",
    "confidence_or_caveats",
    "no_chain_of_thought",
)
WEEKLY_REPORT_JSON_FIELDS: tuple[str, ...] = (
    "output_class",
    "internal_output_class",
    "provider_family_id",
    "surface_id",
    "topic_id",
    "topic_name",
    "report_scope",
    "source_provider_review_ids",
    "source_paper_ids",
    "evidence_span_refs_used",
    "proof_artifact_refs_used",
    "weekly_report_title",
    "executive_summary",
    "reviewed_papers_summary",
    "cross_paper_synthesis",
    "methodology_or_scope_note",
    "limitations",
    "recommended_next_research_topics",
    "confidence_or_caveats",
    "no_chain_of_thought",
)

SENTINEL_OBJECT: dict[str, Any] = {
    "provider_family_id": PROVIDER_FAMILY_ID,
    "surface_id": PROFILE_ID,
    "sentinel": "xrml-provider-tiny-proof",
    "no_chain_of_thought": True,
}
SENTINEL_PROMPT = (
    "Return exactly this JSON object and nothing else: "
    '{"provider_family_id":"codex","surface_id":"codex_sdk_python",'
    '"sentinel":"xrml-provider-tiny-proof","no_chain_of_thought":true}'
)

AUTH_STRATEGY_PROVIDER_MANAGED = "codex_sdk_provider_managed_auth_external"
AUTH_STRATEGY_ACCESS_TOKEN = "codex_sdk_access_token_external"
AUTH_STRATEGY_API_KEY = "codex_sdk_api_key_external"
AUTH_STRATEGY_CHATGPT_OAUTH = "codex_sdk_chatgpt_oauth_external"
AUTH_STRATEGY_UNKNOWN_BLOCKED = "codex_sdk_unknown_auth_blocked"
AUTH_STRATEGY_IDS: tuple[str, ...] = (
    AUTH_STRATEGY_PROVIDER_MANAGED,
    AUTH_STRATEGY_ACCESS_TOKEN,
    AUTH_STRATEGY_API_KEY,
    AUTH_STRATEGY_CHATGPT_OAUTH,
    AUTH_STRATEGY_UNKNOWN_BLOCKED,
)

BASE_REASON_CODES: tuple[str, ...] = (
    "codex_sdk_adapter_scaffolded",
    "blocked_pending_owner_live_approval",
    "blocked_pending_provider_auth_surface_proof",
    "output_class_proof_required",
    "sdk_import_deferred_until_future_execution_path",
    "provider_auth_is_not_app_oauth_oidc",
    "public_mcp_connector_not_in_scope",
)

SECRET_VALUE_MARKERS: tuple[str, ...] = (
    "Bearer ",
    "Authorization:",
    "BEGIN ",
    "sk-",
    "ya29.",
    "access_token=",
    "refresh_token=",
    "client_secret=",
    "api_key=",
    "C:\\",
    "C:/Users/",
    "/Users/",
    "/home/",
    ".codex/",
    "auth.json",
    "session.json",
    "browser/cookies",
    "browser/session",
    "keychain",
    "credential-store",
)
CHAIN_OF_THOUGHT_MARKERS: tuple[str, ...] = (
    "chain-of-thought",
    "chain_of_thought",
    "reasoning",
    "thoughts",
)
RESEARCH_CONTENT_MARKERS: tuple[str, ...] = (
    "paper_id",
    "paper_review",
    "research_output",
    "report_draft",
    "weekly_report",
    "abstract",
    "citation",
)
ALLOWED_ARTIFACT_PREFIX = "output/provider-tiny-proof/"


def _env_present(env: Mapping[str, str] | None, name: str) -> bool:
    source = os.environ if env is None else env
    return bool(str(source.get(name, "")).strip())


def _env_presence(env: Mapping[str, str] | None, names: tuple[str, ...]) -> dict[str, bool]:
    return {name: _env_present(env, name) for name in names}


def _contract_output_class(output_class: str | None) -> str | None:
    if output_class is None:
        return None
    return PRODUCT_OUTPUT_CLASS_MAP.get(str(output_class or "").strip())


def _provider_orchestration() -> Any:
    from app.services import provider_orchestration

    return provider_orchestration


def _gate(output_class: str | None) -> Any:
    po = _provider_orchestration()
    contract_output_class = _contract_output_class(output_class) or "research_output"
    return po.evaluate_provider_output_gate(
        provider_id=PROVIDER_ID,
        output_class=contract_output_class,
        current_proof_level="none",
        proof_present=False,
        accepted=False,
        profile_status="configured_not_verified",
    )


def _auth_label_from_presence(presence: Mapping[str, bool]) -> str:
    if presence.get("CODEX_ACCESS_TOKEN"):
        return AUTH_STRATEGY_ACCESS_TOKEN
    if presence.get("CODEX_API_KEY"):
        return AUTH_STRATEGY_API_KEY
    if presence.get("XRML_CODEX_SDK_PROVIDER_MANAGED_AUTH_APPROVED"):
        return AUTH_STRATEGY_PROVIDER_MANAGED
    return AUTH_STRATEGY_UNKNOWN_BLOCKED


def _auth_label_for_live_attempt(env: Mapping[str, str] | None) -> tuple[str, dict[str, bool]]:
    presence = _env_presence(env, AUTH_ENV_NAME_CANDIDATES)
    if presence.get("CODEX_ACCESS_TOKEN"):
        return AUTH_STRATEGY_ACCESS_TOKEN, presence
    if presence.get("CODEX_API_KEY"):
        return AUTH_STRATEGY_API_KEY, presence
    return AUTH_STRATEGY_PROVIDER_MANAGED, presence


def _has_secret_like_value(payload: Any) -> bool:
    def _walk(node: Any) -> bool:
        if isinstance(node, dict):
            return any(_walk(value) for value in node.values())
        if isinstance(node, (list, tuple)):
            return any(_walk(value) for value in node)
        text = str(node or "")
        return any(marker.lower() in text.lower() for marker in SECRET_VALUE_MARKERS)

    return _walk(payload)


def _has_research_or_report_content(value: str) -> bool:
    lowered = str(value or "").lower()
    return any(marker in lowered for marker in RESEARCH_CONTENT_MARKERS)


def validate_codex_sdk_sentinel_output(output: str | Mapping[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    if isinstance(output, Mapping):
        text = json.dumps(dict(output), separators=(",", ":"), sort_keys=True)
        payload: Any = dict(output)
    else:
        text = str(output or "").strip()
        payload = None
    if not text:
        return {"valid": False, "sentinel": None, "reasonCodes": ["empty_output"]}
    if text.startswith("```") or text.endswith("```"):
        reasons.append("markdown_fence_not_allowed")
    if not (text.startswith("{") and text.endswith("}")):
        reasons.append("extra_prose_or_non_json_output")
    if _has_secret_like_value(text):
        reasons.append("secret_like_content_detected")
    if _has_research_or_report_content(text):
        reasons.append("paper_research_or_report_content_not_allowed")
    if payload is None:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return {"valid": False, "sentinel": None, "reasonCodes": list(dict.fromkeys([*reasons, "invalid_json"]))}
    if not isinstance(payload, dict):
        payload = {}
        reasons.append("json_object_required")
    if set(payload) != set(SENTINEL_OBJECT):
        reasons.append("unexpected_or_missing_fields")
    for key, value in SENTINEL_OBJECT.items():
        if payload.get(key) != value:
            reasons.append(f"field_mismatch:{key}")
    unsafe_extra_keys = set(payload) - {"no_chain_of_thought"}
    if any(str(key).lower() in CHAIN_OF_THOUGHT_MARKERS for key in unsafe_extra_keys):
        reasons.append("chain_of_thought_content_not_allowed")
    if payload.get("no_chain_of_thought") is not True:
        reasons.append("chain_of_thought_content_not_allowed")
    valid = not reasons
    return {
        "valid": valid,
        "sentinel": payload if valid else None,
        "reasonCodes": list(dict.fromkeys(reasons)),
    }


def codex_sdk_tiny_proof_output_gate_summary() -> dict[str, Any]:
    po = _provider_orchestration()
    research_gate = po.evaluate_provider_output_gate(
        provider_id=PROVIDER_ID,
        output_class="research_output",
        current_proof_level=PROOF_STATUS_CANDIDATE,
        proof_present=True,
        accepted=True,
        profile_status=PROOF_STATUS_CANDIDATE,
    )
    report_gate = po.evaluate_provider_output_gate(
        provider_id=PROVIDER_ID,
        output_class="report_draft",
        current_proof_level=PROOF_STATUS_CANDIDATE,
        proof_present=True,
        accepted=True,
        profile_status=PROOF_STATUS_CANDIDATE,
    )
    return {
        "proofStatusCandidate": PROOF_STATUS_CANDIDATE,
        "researchOutputAllowed": research_gate.allowed,
        "reportDraftAllowed": report_gate.allowed,
        "paperReviewAndWeeklyReportRemainSeparate": True,
        "apiPipelineSkeletonCanBypassProof": False,
        "providerAuthIsAppOauthOidcProof": False,
        "localMcpIsPublicMcpConnectorProof": False,
        "researchOutputGate": research_gate.to_dict(),
        "reportDraftGate": report_gate.to_dict(),
    }


def _default_artifact_ref(timestamp: str) -> str:
    safe_timestamp = "".join(char if char.isalnum() else "-" for char in timestamp).strip("-")
    return f"{ALLOWED_ARTIFACT_PREFIX}{safe_timestamp}/codex_sdk_python_provider_tiny_proof.json"


def validate_codex_sdk_tiny_proof_artifact_path(path: str | None) -> dict[str, Any]:
    normalized = str(path or "").replace("\\", "/").strip()
    if not normalized:
        return {"valid": False, "pathForPlan": None, "reasonCodes": ["missing_artifact_path"]}
    if ":" in normalized.split("/", 1)[0] or normalized.startswith("/") or normalized.startswith("~"):
        return {"valid": False, "pathForPlan": None, "reasonCodes": ["absolute_or_home_artifact_path_not_allowed"]}
    lowered = normalized.lower()
    if not lowered.startswith(ALLOWED_ARTIFACT_PREFIX):
        return {"valid": False, "pathForPlan": None, "reasonCodes": ["artifact_path_must_be_under_provider_tiny_proof_output"]}
    if not lowered.endswith(".json"):
        return {"valid": False, "pathForPlan": None, "reasonCodes": ["artifact_path_must_end_json"]}
    if any(fragment in lowered for fragment in ("../", "/../", "storage/", "docs/", "tests/", "app/", ".env", ".mcp.json")):
        return {"valid": False, "pathForPlan": None, "reasonCodes": ["artifact_path_hits_blocked_location"]}
    return {"valid": True, "pathForPlan": normalized, "reasonCodes": []}


def _default_output_class_artifact_ref(
    timestamp: str,
    product_output_class: str = PAPER_REVIEW_PRODUCT_OUTPUT_CLASS,
) -> str:
    safe_timestamp = "".join(char if char.isalnum() else "-" for char in timestamp).strip("-")
    filename = (
        "codex_sdk_weekly_report_output_class_live_proof.json"
        if product_output_class == WEEKLY_REPORT_PRODUCT_OUTPUT_CLASS
        else "codex_sdk_paper_review_output_class_live_proof.json"
    )
    return (
        f"{OUTPUT_CLASS_ALLOWED_ARTIFACT_PREFIX}{safe_timestamp}/"
        f"{filename}"
    )


def validate_codex_sdk_output_class_proof_artifact_path(path: str | None) -> dict[str, Any]:
    normalized = str(path or "").replace("\\", "/").strip()
    if not normalized:
        return {"valid": False, "pathForPlan": None, "reasonCodes": ["missing_artifact_path"]}
    if ":" in normalized.split("/", 1)[0] or normalized.startswith("/") or normalized.startswith("~"):
        return {"valid": False, "pathForPlan": None, "reasonCodes": ["absolute_or_home_artifact_path_not_allowed"]}
    lowered = normalized.lower()
    if not lowered.startswith(OUTPUT_CLASS_ALLOWED_ARTIFACT_PREFIX):
        return {"valid": False, "pathForPlan": None, "reasonCodes": ["artifact_path_must_be_under_provider_output_class_proof_output"]}
    if not lowered.endswith(".json"):
        return {"valid": False, "pathForPlan": None, "reasonCodes": ["artifact_path_must_end_json"]}
    blocked_fragments = (
        "../",
        "/../",
        "storage/",
        "docs/",
        "tests/",
        "app/",
        ".env",
        ".mcp.json",
        "release-validation/",
        "playwright/",
        "credential",
        "cache",
        "session",
    )
    if any(fragment in lowered for fragment in blocked_fragments):
        return {"valid": False, "pathForPlan": None, "reasonCodes": ["artifact_path_hits_blocked_location"]}
    return {"valid": True, "pathForPlan": normalized, "reasonCodes": []}


def _required_evidence_text(evidence: Mapping[str, Any], key: str) -> str:
    return str(evidence.get(key) or "").strip()


def _compact_text(value: Any, *, limit: int = 1200) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit].strip()


def _normalize_paper_review_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    normalized = {
        "topic_id": _required_evidence_text(evidence, "topic_id"),
        "topic_name": _required_evidence_text(evidence, "topic_name"),
        "paper_id": _required_evidence_text(evidence, "paper_id"),
        "paper_title": _required_evidence_text(evidence, "paper_title"),
        "evidence_source_ref": _required_evidence_text(evidence, "evidence_source_ref"),
        "evidence_spans": [],
    }
    for key in ("topic_id", "topic_name", "paper_id", "paper_title", "evidence_source_ref"):
        if not normalized[key]:
            reasons.append(f"missing_evidence_field:{key}")
    spans = evidence.get("evidence_spans")
    if not isinstance(spans, (list, tuple)) or not spans:
        reasons.append("missing_evidence_spans")
        spans = []
    normalized_spans: list[dict[str, str]] = []
    for index, span in enumerate(spans):
        if not isinstance(span, Mapping):
            reasons.append(f"invalid_evidence_span:{index}")
            continue
        ref = _compact_text(span.get("ref"), limit=160)
        text = _compact_text(span.get("text"), limit=1200)
        if not ref:
            reasons.append(f"missing_evidence_span_ref:{index}")
        if not text:
            reasons.append(f"missing_evidence_span_text:{index}")
        normalized_spans.append({"ref": ref, "text": text})
    normalized["evidence_spans"] = normalized_spans
    if _has_secret_like_value(normalized):
        reasons.append("secret_like_content_detected")
    if reasons:
        return {"valid": False, "evidence": normalized, "reasonCodes": list(dict.fromkeys(reasons))}
    return {"valid": True, "evidence": normalized, "reasonCodes": []}


def build_codex_sdk_paper_review_prompt(evidence: Mapping[str, Any]) -> str:
    normalized = _normalize_paper_review_evidence(evidence)
    if not normalized["valid"]:
        raise ValueError(",".join(normalized["reasonCodes"]))
    safe_evidence = normalized["evidence"]
    prompt_payload = {
        "task": PAPER_REVIEW_PROMPT_CLASS,
        "provider_family_id": PROVIDER_FAMILY_ID,
        "surface_id": PROFILE_ID,
        "output_class": PAPER_REVIEW_PRODUCT_OUTPUT_CLASS,
        "internal_output_class": PAPER_REVIEW_INTERNAL_OUTPUT_CLASS,
        "instructions": [
            "Return one strict JSON object and nothing else.",
            "Use only the supplied evidence spans.",
            "Cite only supplied evidence span refs.",
            "Do not include markdown, prose outside JSON, hidden reasoning, or chain-of-thought.",
            "Do not generate weekly report or report draft output.",
        ],
        "required_fields": list(PAPER_REVIEW_JSON_FIELDS),
        "evidence": safe_evidence,
    }
    return json.dumps(prompt_payload, ensure_ascii=True, sort_keys=True)


def _walk_text_values(payload: Any) -> list[str]:
    values: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                _walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                _walk(value)
        else:
            values.append(str(node or ""))

    _walk(payload)
    return values


def _contains_chain_of_thought_value(payload: Any) -> bool:
    markers = (
        "chain-of-thought",
        "chain_of_thought",
        "hidden reasoning",
        "internal reasoning",
        "step-by-step reasoning",
        "thought process",
    )
    return any(marker in value.lower() for value in _walk_text_values(payload) for marker in markers)


def validate_codex_sdk_paper_review_output(
    output: str | Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    reasons: list[str] = []
    normalized = _normalize_paper_review_evidence(evidence)
    if not normalized["valid"]:
        return {"valid": False, "paperReview": None, "reasonCodes": normalized["reasonCodes"]}
    expected = normalized["evidence"]
    if isinstance(output, Mapping):
        text = json.dumps(dict(output), separators=(",", ":"), sort_keys=True)
        payload: Any = dict(output)
    else:
        text = str(output or "").strip()
        payload = None
    if not text:
        return {"valid": False, "paperReview": None, "reasonCodes": ["empty_output"]}
    if text.startswith("```") or text.endswith("```"):
        reasons.append("markdown_fence_not_allowed")
    if not (text.startswith("{") and text.endswith("}")):
        reasons.append("extra_prose_or_non_json_output")
    if _has_secret_like_value(text):
        reasons.append("secret_like_content_detected")
    if payload is None:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return {"valid": False, "paperReview": None, "reasonCodes": list(dict.fromkeys([*reasons, "invalid_json"]))}
    if not isinstance(payload, dict):
        payload = {}
        reasons.append("json_object_required")
    expected_fields = set(PAPER_REVIEW_JSON_FIELDS)
    if set(payload) != expected_fields:
        reasons.append("unexpected_or_missing_fields")
    fixed_values = {
        "output_class": PAPER_REVIEW_PRODUCT_OUTPUT_CLASS,
        "internal_output_class": PAPER_REVIEW_INTERNAL_OUTPUT_CLASS,
        "provider_family_id": PROVIDER_FAMILY_ID,
        "surface_id": PROFILE_ID,
        "topic_id": expected["topic_id"],
        "topic_name": expected["topic_name"],
        "paper_id": expected["paper_id"],
        "paper_title": expected["paper_title"],
    }
    for key, value in fixed_values.items():
        if payload.get(key) != value:
            reasons.append(f"field_mismatch:{key}")
    allowed_refs = {span["ref"] for span in expected["evidence_spans"]}
    used_refs = payload.get("evidence_span_refs_used")
    if not isinstance(used_refs, list) or not used_refs:
        reasons.append("evidence_span_refs_used_required")
        used_refs = []
    elif any(str(ref) not in allowed_refs for ref in used_refs):
        reasons.append("unknown_evidence_span_ref_used")
    list_fields = ("key_findings", "limitations", "follow_up_topic_suggestions")
    for key in list_fields:
        value = payload.get(key)
        if not isinstance(value, list) or not [item for item in value if str(item or "").strip()]:
            reasons.append(f"nonempty_list_required:{key}")
    string_fields = (
        "concise_review_summary",
        "methodology_or_core_approach",
        "relevance_to_topic",
        "confidence_or_caveats",
    )
    for key in string_fields:
        if not str(payload.get(key) or "").strip():
            reasons.append(f"nonempty_string_required:{key}")
    if payload.get("no_chain_of_thought") is not True:
        reasons.append("chain_of_thought_content_not_allowed")
    if _contains_chain_of_thought_value(payload):
        reasons.append("chain_of_thought_content_not_allowed")
    serialized_payload = json.dumps(payload, ensure_ascii=True, sort_keys=True)
    if "weekly_report" in serialized_payload or "report_draft" in serialized_payload:
        reasons.append("weekly_report_or_report_draft_output_not_allowed")
    if _has_secret_like_value(payload):
        reasons.append("secret_like_content_detected")
    valid = not reasons
    return {
        "valid": valid,
        "paperReview": payload if valid else None,
        "reasonCodes": list(dict.fromkeys(reasons)),
    }


def codex_sdk_paper_review_output_class_gate_summary(
    *, proof_artifact_ref: str | None = None
) -> dict[str, Any]:
    po = _provider_orchestration()
    research_gate = po.evaluate_provider_output_gate(
        provider_id=PROVIDER_ID,
        output_class=PAPER_REVIEW_INTERNAL_OUTPUT_CLASS,
        current_proof_level=PAPER_REVIEW_REQUIRED_PROOF_STATUS,
        proof_present=True,
        accepted=True,
        profile_status="configured_not_verified",
    )
    report_gate = po.evaluate_provider_output_gate(
        provider_id=PROVIDER_ID,
        output_class="report_draft",
        current_proof_level=PAPER_REVIEW_REQUIRED_PROOF_STATUS,
        proof_present=True,
        accepted=True,
        profile_status="configured_not_verified",
    )
    return {
        "proofMode": OUTPUT_CLASS_PROOF_MODE,
        "productOutputClass": PAPER_REVIEW_PRODUCT_OUTPUT_CLASS,
        "internalOutputClass": PAPER_REVIEW_INTERNAL_OUTPUT_CLASS,
        "proofStatus": PAPER_REVIEW_REQUIRED_PROOF_STATUS,
        "artifactRef": proof_artifact_ref,
        "researchOutputAllowed": research_gate.allowed,
        "reportDraftAllowed": report_gate.allowed,
        "paperReviewAndWeeklyReportRemainSeparate": True,
        "apiPipelineSkeletonCanBypassProof": False,
        "providerAuthIsAppOauthOidcProof": False,
        "localMcpIsPublicMcpConnectorProof": False,
        "researchOutputGate": research_gate.to_dict(),
        "reportDraftGate": report_gate.to_dict(),
    }


def _evidence_spans_for_artifact(evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    spans = []
    for span in evidence.get("evidence_spans") or []:
        text = str(span.get("text") or "")
        spans.append(
            {
                "ref": span.get("ref"),
                "charCount": len(text),
                "redactedExcerpt": text[:900],
            }
        )
    return spans


def build_codex_sdk_paper_review_output_class_proof_artifact(
    *,
    paper_review_output: Mapping[str, Any],
    evidence: Mapping[str, Any],
    auth_strategy_used: str,
    provider_auth_observed: Mapping[str, bool],
    artifact_ref: str,
    timestamp: str,
) -> dict[str, Any]:
    from app.services.provider_output_class_proof import build_output_class_proof

    normalized = _normalize_paper_review_evidence(evidence)
    if not normalized["valid"]:
        raise ValueError(",".join(normalized["reasonCodes"]))
    safe_evidence = normalized["evidence"]
    run_id = f"codex-sdk-paper-review-output-class-proof-{timestamp}"
    proof = build_output_class_proof(
        provider_id=PROVIDER_ID,
        provider_profile_name=PROFILE_ID,
        execution_surface=EXECUTION_SURFACE_TYPE,
        output_class=PAPER_REVIEW_PRODUCT_OUTPUT_CLASS,
        proof_mode=OUTPUT_CLASS_PROOF_MODE,
        topic_id=safe_evidence["topic_id"],
        topic_name=safe_evidence["topic_name"],
        paper_ids=[safe_evidence["paper_id"]],
        full_text_evidence_source=safe_evidence["evidence_source_ref"],
        evidence_span_count=len(safe_evidence["evidence_spans"]),
        artifact_ref=artifact_ref,
        run_id=run_id,
        generated_at=timestamp,
        profile_status=PAPER_REVIEW_REQUIRED_PROOF_STATUS,
        configured=True,
    )
    proof_record = proof.to_dict()
    gates = codex_sdk_paper_review_output_class_gate_summary(proof_artifact_ref=artifact_ref)
    return {
        "artifactKind": OUTPUT_CLASS_ARTIFACT_KIND,
        "providerFamilyId": PROVIDER_FAMILY_ID,
        "providerId": PROVIDER_ID,
        "profileId": PROFILE_ID,
        "surfaceId": PROFILE_ID,
        "executionSurfaceType": EXECUTION_SURFACE_TYPE,
        "adapterKind": ADAPTER_KIND,
        "authStrategyUsed": auth_strategy_used,
        "proofMode": OUTPUT_CLASS_PROOF_MODE,
        "promptClass": PAPER_REVIEW_PROMPT_CLASS,
        "productOutputClass": PAPER_REVIEW_PRODUCT_OUTPUT_CLASS,
        "internalOutputClass": PAPER_REVIEW_INTERNAL_OUTPUT_CLASS,
        "requiredProofMode": OUTPUT_CLASS_PROOF_MODE,
        "requiredProofStatus": PAPER_REVIEW_REQUIRED_PROOF_STATUS,
        "currentProofStatus": PAPER_REVIEW_REQUIRED_PROOF_STATUS,
        "acceptedOutputClassProof": bool(proof.accepted),
        "outputClassProof": bool(proof.accepted),
        "researchOutputAllowed": gates["researchOutputAllowed"],
        "reportDraftAllowed": gates["reportDraftAllowed"],
        "weeklyReportProofStatus": "missing",
        "providerSdkRunCount": 1,
        "providerCommandCount": 0,
        "providerCallCount": 1,
        "topicId": safe_evidence["topic_id"],
        "topicName": safe_evidence["topic_name"],
        "paperId": safe_evidence["paper_id"],
        "paperTitle": safe_evidence["paper_title"],
        "fullTextEvidenceSourceUsed": safe_evidence["evidence_source_ref"],
        "evidenceSourceRef": safe_evidence["evidence_source_ref"],
        "evidenceSpanCount": len(safe_evidence["evidence_spans"]),
        "evidenceSpans": _evidence_spans_for_artifact(safe_evidence),
        "evidenceSpanRefsUsed": list(paper_review_output.get("evidence_span_refs_used") or []),
        "redactedProviderOutput": dict(paper_review_output),
        "artifactRef": artifact_ref,
        "timestamp": timestamp,
        "runId": run_id,
        "sdkSurfaceSummary": {
            "packageName": SDK_PACKAGE_NAME,
            "importNameCandidates": list(SDK_IMPORT_NAME_CANDIDATES),
            "executionSurfaceType": EXECUTION_SURFACE_TYPE,
            "adapterKind": ADAPTER_KIND,
            "manualAppServerStarted": False,
            "codexCliCommandRun": False,
            "workspaceWriteRequested": False,
            "readOnlySandboxRequested": True,
            "secretValuesPrinted": False,
        },
        "providerAuthObserved": {
            "authStrategyUsed": auth_strategy_used,
            "envPresence": dict(provider_auth_observed),
            "providerManagedAuthBlackBoxAllowed": auth_strategy_used == AUTH_STRATEGY_PROVIDER_MANAGED,
            "providerManagedAuthVerifiedByCacheInspection": False,
        },
        "outputClassGateSummary": gates,
        "proofRecordSummary": proof_record,
        "reasonCodes": [
            "provider_output_class_proof_accepted",
            "codex_sdk_single_paper_review_run",
            "paper_review_output_class_verified",
            "weekly_report_not_unlocked",
        ],
        "liveCommandDisabled": False,
        "noSecretInspection": True,
        "noTokenCacheInspection": True,
        "noCredentialPathDisclosure": True,
        "appOauthOidcInScope": False,
        "publicMcpConnectorInScope": False,
        "outputClassProofInScope": True,
        "notInScope": [
            "app_oauth_oidc_not_in_scope",
            "public_mcp_not_in_scope",
            "weekly_report_not_in_scope",
            "report_draft_not_in_scope",
            "production_external_beta_not_in_scope",
            "broad_paper_collection_not_in_scope",
            "db_persistence_not_in_scope",
        ],
        "no_chain_of_thought": True,
        "redactionStatus": "passed",
        "actualEnvFileInspected": False,
        "actualMcpConfigFileInspected": False,
        "credentialCacheSessionAuthFileInspectedByProject": False,
        "productionReady": False,
        "externalBetaReady": False,
        "safetyStatements": [
            "No provider command was run by this project.",
            "No provider auth/cache files were inspected by this project.",
            "This proof does not establish App OAuth/OIDC, public MCP connector, weekly report, production, or external beta readiness.",
        ],
    }


def validate_codex_sdk_paper_review_output_class_proof_artifact_contract(
    artifact: Mapping[str, Any]
) -> dict[str, Any]:
    from app.services.provider_output_class_proof import scan_for_forbidden_content

    reasons: list[str] = []
    required_fixed = {
        "artifactKind": OUTPUT_CLASS_ARTIFACT_KIND,
        "providerFamilyId": PROVIDER_FAMILY_ID,
        "providerId": PROVIDER_ID,
        "profileId": PROFILE_ID,
        "surfaceId": PROFILE_ID,
        "executionSurfaceType": EXECUTION_SURFACE_TYPE,
        "adapterKind": ADAPTER_KIND,
        "proofMode": OUTPUT_CLASS_PROOF_MODE,
        "productOutputClass": PAPER_REVIEW_PRODUCT_OUTPUT_CLASS,
        "internalOutputClass": PAPER_REVIEW_INTERNAL_OUTPUT_CLASS,
        "requiredProofMode": OUTPUT_CLASS_PROOF_MODE,
        "requiredProofStatus": PAPER_REVIEW_REQUIRED_PROOF_STATUS,
        "currentProofStatus": PAPER_REVIEW_REQUIRED_PROOF_STATUS,
        "acceptedOutputClassProof": True,
        "outputClassProof": True,
        "researchOutputAllowed": True,
        "reportDraftAllowed": False,
        "weeklyReportProofStatus": "missing",
        "providerSdkRunCount": 1,
        "providerCommandCount": 0,
        "providerCallCount": 1,
        "appOauthOidcInScope": False,
        "publicMcpConnectorInScope": False,
        "outputClassProofInScope": True,
        "no_chain_of_thought": True,
        "redactionStatus": "passed",
        "productionReady": False,
        "externalBetaReady": False,
    }
    for key, expected in required_fixed.items():
        if artifact.get(key) != expected:
            reasons.append(f"field_mismatch:{key}")
    if artifact.get("authStrategyUsed") not in {
        AUTH_STRATEGY_PROVIDER_MANAGED,
        AUTH_STRATEGY_ACCESS_TOKEN,
        AUTH_STRATEGY_API_KEY,
    }:
        reasons.append("unsafe_or_unknown_auth_strategy_for_accepted_proof")
    evidence = {
        "topic_id": artifact.get("topicId"),
        "topic_name": artifact.get("topicName"),
        "paper_id": artifact.get("paperId"),
        "paper_title": artifact.get("paperTitle"),
        "evidence_source_ref": artifact.get("evidenceSourceRef"),
        "evidence_spans": [
            {"ref": span.get("ref"), "text": span.get("redactedExcerpt")}
            for span in artifact.get("evidenceSpans") or []
            if isinstance(span, Mapping)
        ],
    }
    review = validate_codex_sdk_paper_review_output(artifact.get("redactedProviderOutput") or {}, evidence)
    if not review["valid"]:
        reasons.append("provider_output_not_valid_paper_review_json")
        reasons.extend(str(reason) for reason in review.get("reasonCodes", []))
    proof_record = artifact.get("proofRecordSummary") or {}
    if scan_for_forbidden_content(proof_record):
        reasons.append("proof_record_secret_like_content_detected")
    gates = artifact.get("outputClassGateSummary") or {}
    if gates.get("researchOutputAllowed") is not True or gates.get("reportDraftAllowed") is not False:
        reasons.append("output_class_gate_mismatch")
    if _has_secret_like_value(artifact):
        reasons.append("secret_like_content_detected")
    provider_output_text = json.dumps(artifact.get("redactedProviderOutput") or {}, ensure_ascii=True, sort_keys=True)
    if "weekly_report" in provider_output_text or "report_draft" in provider_output_text:
        reasons.append("weekly_report_or_report_draft_output_not_allowed")
    return {"valid": not reasons, "reasonCodes": list(dict.fromkeys(reasons))}


def _text_list(value: Any, *, limit: int = 320) -> list[str]:
    if not isinstance(value, (list, tuple)):
        value = [value] if str(value or "").strip() else []
    return [
        _compact_text(item, limit=limit)
        for item in value
        if _compact_text(item, limit=limit)
    ]


def _normalize_weekly_report_context(context: Mapping[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    source_reviews = list(context.get("provider_reviews") or [])
    normalized_reviews: list[dict[str, Any]] = []
    for index, review in enumerate(source_reviews):
        if not isinstance(review, Mapping):
            reasons.append(f"invalid_provider_review_context:{index}")
            continue
        normalized = {
            "provider_review_id": _compact_text(review.get("provider_review_id"), limit=180),
            "paper_id": _compact_text(review.get("paper_id"), limit=180),
            "paper_title": _compact_text(review.get("paper_title"), limit=320),
            "concise_review_summary": _compact_text(review.get("concise_review_summary"), limit=900),
            "key_findings": _text_list(review.get("key_findings"), limit=420),
            "limitations": _text_list(review.get("limitations"), limit=420),
            "relevance_to_topic": _compact_text(review.get("relevance_to_topic"), limit=700),
            "confidence_or_caveats": _compact_text(review.get("confidence_or_caveats"), limit=520),
            "provider_family_id": _compact_text(review.get("provider_family_id") or PROVIDER_FAMILY_ID, limit=120),
            "surface_id": _compact_text(review.get("surface_id") or PROFILE_ID, limit=120),
            "output_class": _compact_text(review.get("output_class") or PAPER_REVIEW_PRODUCT_OUTPUT_CLASS, limit=120),
            "internal_output_class": _compact_text(review.get("internal_output_class") or PAPER_REVIEW_INTERNAL_OUTPUT_CLASS, limit=120),
        }
        for key in (
            "provider_review_id",
            "paper_id",
            "paper_title",
            "concise_review_summary",
            "relevance_to_topic",
            "confidence_or_caveats",
        ):
            if not normalized[key]:
                reasons.append(f"missing_provider_review_context_field:{index}:{key}")
        if not normalized["key_findings"]:
            reasons.append(f"missing_provider_review_context_field:{index}:key_findings")
        if not normalized["limitations"]:
            reasons.append(f"missing_provider_review_context_field:{index}:limitations")
        if normalized["provider_family_id"] != PROVIDER_FAMILY_ID:
            reasons.append(f"provider_review_context_wrong_provider:{index}")
        if normalized["surface_id"] != PROFILE_ID:
            reasons.append(f"provider_review_context_wrong_surface:{index}")
        if normalized["output_class"] != PAPER_REVIEW_PRODUCT_OUTPUT_CLASS:
            reasons.append(f"provider_review_context_wrong_output_class:{index}")
        if normalized["internal_output_class"] != PAPER_REVIEW_INTERNAL_OUTPUT_CLASS:
            reasons.append(f"provider_review_context_wrong_internal_output_class:{index}")
        normalized_reviews.append(normalized)

    review_ids = _text_list(context.get("source_provider_review_ids"), limit=180)
    if not review_ids:
        review_ids = [review["provider_review_id"] for review in normalized_reviews if review["provider_review_id"]]
    paper_ids = _text_list(context.get("source_paper_ids"), limit=180)
    if not paper_ids:
        paper_ids = [review["paper_id"] for review in normalized_reviews if review["paper_id"]]
    evidence_refs = _text_list(context.get("evidence_span_refs_used"), limit=240)
    proof_refs = _text_list(context.get("proof_artifact_refs_used"), limit=320)
    safe_context = {
        "topic_id": _required_evidence_text(context, "topic_id"),
        "topic_name": _required_evidence_text(context, "topic_name"),
        "report_scope": _required_evidence_text(context, "report_scope") or "provider_review_context_only",
        "source_provider_review_ids": list(dict.fromkeys(review_ids)),
        "source_paper_ids": list(dict.fromkeys(paper_ids)),
        "evidence_span_refs_used": list(dict.fromkeys(evidence_refs)),
        "proof_artifact_refs_used": list(dict.fromkeys(proof_refs)),
        "provider_reviews": normalized_reviews,
    }
    for key in ("topic_id", "topic_name", "report_scope"):
        if not safe_context[key]:
            reasons.append(f"missing_weekly_report_context_field:{key}")
    for key in (
        "source_provider_review_ids",
        "source_paper_ids",
        "evidence_span_refs_used",
        "proof_artifact_refs_used",
    ):
        if not safe_context[key]:
            reasons.append(f"missing_weekly_report_context_field:{key}")
    if safe_context["report_scope"] != "provider_review_context_only":
        reasons.append("weekly_report_context_scope_must_be_provider_review_context_only")
    review_id_set = {review["provider_review_id"] for review in normalized_reviews}
    paper_id_set = {review["paper_id"] for review in normalized_reviews}
    if not review_id_set.issuperset(safe_context["source_provider_review_ids"]):
        reasons.append("source_provider_review_id_not_in_context")
    if not paper_id_set.issuperset(safe_context["source_paper_ids"]):
        reasons.append("source_paper_id_not_in_context")
    if _has_secret_like_value(safe_context):
        reasons.append("secret_like_content_detected")
    if _contains_chain_of_thought_value(safe_context):
        reasons.append("chain_of_thought_content_not_allowed")
    return {"valid": not reasons, "context": safe_context, "reasonCodes": list(dict.fromkeys(reasons))}


def build_codex_sdk_weekly_report_prompt(report_context: Mapping[str, Any]) -> str:
    normalized = _normalize_weekly_report_context(report_context)
    if not normalized["valid"]:
        raise ValueError(",".join(normalized["reasonCodes"]))
    prompt_payload = {
        "task": WEEKLY_REPORT_PROMPT_CLASS,
        "provider_family_id": PROVIDER_FAMILY_ID,
        "surface_id": PROFILE_ID,
        "output_class": WEEKLY_REPORT_PRODUCT_OUTPUT_CLASS,
        "internal_output_class": WEEKLY_REPORT_INTERNAL_OUTPUT_CLASS,
        "instructions": [
            "Return exactly one strict JSON object and nothing else.",
            "Do not include markdown or prose outside JSON.",
            "Use only the supplied DB-applied provider review context.",
            "Cite only supplied paper, evidence span, provider review, and proof refs.",
            "Do not include hidden reasoning or chain-of-thought.",
            "This is a draft proof only, not a persisted report.",
            "Do not claim the report is persisted, saved, production-ready, released, deployed, externally available, or public.",
            "Do not claim DB writes, report persistence, staging readiness, production readiness, external beta readiness, deployment, release, or public access.",
            "Do not generate new paper_review or research_output output.",
            "Do not use unsupplied sources.",
            "reviewed_papers_summary must be a non-empty array.",
            "Each reviewed_papers_summary item must include paper_id, paper_title, main_takeaway, and evidence_refs.",
            "Each reviewed_papers_summary main_takeaway must be a non-empty string.",
            "Each reviewed_papers_summary evidence_refs must be a non-empty array drawn only from supplied evidence_span_refs_used.",
            "proof_artifact_refs_used must cite the supplied paper_review proof artifact ref.",
            "source_provider_review_ids must include the supplied provider review id.",
            "source_paper_ids must include the supplied paper id.",
        ],
        "reviewed_papers_summary_item_required_fields": [
            "paper_id",
            "paper_title",
            "main_takeaway",
            "evidence_refs",
        ],
        "required_fields": list(WEEKLY_REPORT_JSON_FIELDS),
        "report_context": normalized["context"],
    }
    return json.dumps(prompt_payload, ensure_ascii=True, sort_keys=True)


def _contains_forbidden_weekly_report_claim(payload: Mapping[str, Any]) -> bool:
    positive_claim_patterns = (
        r"\bpersisted\s+report\b",
        r"\bsaved\s+report\b",
        r"\breport\s+has\s+been\s+stored\b",
        r"\bdb\s+has\s+been\s+updated\b",
        r"\bready\s+for\s+production\b",
        r"\bproduction[-\s]+ready\b",
        r"\bexternal\s+beta[-\s]+ready\b",
        r"\breleased\b",
        r"\bdeployed\b",
        r"\bpublicly\s+available\b",
    )
    negation_markers = (
        "not ",
        "no ",
        "never ",
        "without ",
        "does not ",
        "do not ",
        "is not ",
        "are not ",
        "cannot ",
        "can't ",
        "not in scope",
        "out of scope",
        "not persisted",
        "not production-ready",
        "not production ready",
        "not external beta-ready",
        "not external beta ready",
        "not publicly available",
        "not released",
        "not deployed",
    )
    import re

    for value in _walk_text_values(payload):
        lowered = value.lower()
        for pattern in positive_claim_patterns:
            for match in re.finditer(pattern, lowered):
                prefix = lowered[max(0, match.start() - 48) : match.start()]
                segment = lowered[max(0, match.start() - 64) : min(len(lowered), match.end() + 64)]
                if any(marker in prefix or marker in segment for marker in negation_markers):
                    continue
                return True
    return False


def validate_codex_sdk_weekly_report_output(
    output: str | Mapping[str, Any],
    report_context: Mapping[str, Any],
) -> dict[str, Any]:
    reasons: list[str] = []
    normalized = _normalize_weekly_report_context(report_context)
    if not normalized["valid"]:
        return {"valid": False, "weeklyReport": None, "reasonCodes": normalized["reasonCodes"]}
    expected = normalized["context"]
    if isinstance(output, Mapping):
        text = json.dumps(dict(output), separators=(",", ":"), sort_keys=True)
        payload: Any = dict(output)
    else:
        text = str(output or "").strip()
        payload = None
    if not text:
        return {"valid": False, "weeklyReport": None, "reasonCodes": ["empty_output"]}
    if text.startswith("```") or text.endswith("```"):
        reasons.append("markdown_fence_not_allowed")
    if not (text.startswith("{") and text.endswith("}")):
        reasons.append("extra_prose_or_non_json_output")
    if _has_secret_like_value(text):
        reasons.append("secret_like_content_detected")
    if payload is None:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return {"valid": False, "weeklyReport": None, "reasonCodes": list(dict.fromkeys([*reasons, "invalid_json"]))}
    if not isinstance(payload, dict):
        payload = {}
        reasons.append("json_object_required")
    if set(payload) != set(WEEKLY_REPORT_JSON_FIELDS):
        reasons.append("unexpected_or_missing_fields")
    fixed_values = {
        "output_class": WEEKLY_REPORT_PRODUCT_OUTPUT_CLASS,
        "internal_output_class": WEEKLY_REPORT_INTERNAL_OUTPUT_CLASS,
        "provider_family_id": PROVIDER_FAMILY_ID,
        "surface_id": PROFILE_ID,
        "topic_id": expected["topic_id"],
        "topic_name": expected["topic_name"],
        "report_scope": "provider_review_context_only",
    }
    for key, value in fixed_values.items():
        if payload.get(key) != value:
            reasons.append(f"field_mismatch:{key}")

    expected_review_ids = set(expected["source_provider_review_ids"])
    expected_paper_ids = set(expected["source_paper_ids"])
    expected_evidence_refs = set(expected["evidence_span_refs_used"])
    expected_proof_refs = set(expected["proof_artifact_refs_used"])
    list_expectations = {
        "source_provider_review_ids": expected_review_ids,
        "source_paper_ids": expected_paper_ids,
        "proof_artifact_refs_used": expected_proof_refs,
    }
    for key, expected_set in list_expectations.items():
        value = payload.get(key)
        if not isinstance(value, list) or not value:
            reasons.append(f"nonempty_list_required:{key}")
            continue
        if set(str(item) for item in value) != expected_set:
            reasons.append(f"field_mismatch:{key}")
    evidence_used = payload.get("evidence_span_refs_used")
    if not isinstance(evidence_used, list) or not evidence_used:
        reasons.append("evidence_span_refs_used_required")
    elif any(str(ref) not in expected_evidence_refs for ref in evidence_used):
        reasons.append("unknown_evidence_span_ref_used")

    string_fields = (
        "weekly_report_title",
        "executive_summary",
        "cross_paper_synthesis",
        "methodology_or_scope_note",
        "confidence_or_caveats",
    )
    for key in string_fields:
        if not str(payload.get(key) or "").strip():
            reasons.append(f"nonempty_string_required:{key}")
    for key in ("limitations", "recommended_next_research_topics"):
        value = payload.get(key)
        if not isinstance(value, list) or not [item for item in value if str(item or "").strip()]:
            reasons.append(f"nonempty_list_required:{key}")

    reviewed = payload.get("reviewed_papers_summary")
    if not isinstance(reviewed, list) or not reviewed:
        reasons.append("reviewed_papers_summary_required")
    else:
        for index, item in enumerate(reviewed):
            if not isinstance(item, Mapping):
                reasons.append(f"invalid_reviewed_papers_summary_item:{index}")
                continue
            if str(item.get("paper_id") or "") not in expected_paper_ids:
                reasons.append(f"unknown_reviewed_paper_id:{index}")
            if not str(item.get("paper_title") or "").strip():
                reasons.append(f"missing_reviewed_paper_title:{index}")
            if not str(item.get("main_takeaway") or "").strip():
                reasons.append(f"missing_reviewed_paper_takeaway:{index}")
            refs = item.get("evidence_refs")
            if not isinstance(refs, list) or not refs:
                reasons.append(f"missing_reviewed_paper_evidence_refs:{index}")
            elif any(str(ref) not in expected_evidence_refs for ref in refs):
                reasons.append(f"unknown_reviewed_paper_evidence_ref:{index}")

    if payload.get("no_chain_of_thought") is not True:
        reasons.append("chain_of_thought_content_not_allowed")
    if _contains_chain_of_thought_value(payload):
        reasons.append("chain_of_thought_content_not_allowed")
    if _contains_forbidden_weekly_report_claim(payload):
        reasons.append("forbidden_report_persistence_or_readiness_claim")
    if _has_secret_like_value(payload):
        reasons.append("secret_like_content_detected")
    return {
        "valid": not reasons,
        "weeklyReport": payload if not reasons else None,
        "reasonCodes": list(dict.fromkeys(reasons)),
    }


def codex_sdk_weekly_report_output_class_gate_summary(
    *, proof_artifact_ref: str | None = None
) -> dict[str, Any]:
    po = _provider_orchestration()
    report_gate = po.evaluate_provider_output_gate(
        provider_id=PROVIDER_ID,
        output_class=WEEKLY_REPORT_INTERNAL_OUTPUT_CLASS,
        current_proof_level=WEEKLY_REPORT_REQUIRED_PROOF_STATUS,
        proof_present=True,
        accepted=True,
        profile_status="configured_not_verified",
    )
    research_gate = po.evaluate_provider_output_gate(
        provider_id=PROVIDER_ID,
        output_class=PAPER_REVIEW_INTERNAL_OUTPUT_CLASS,
        current_proof_level=WEEKLY_REPORT_REQUIRED_PROOF_STATUS,
        proof_present=True,
        accepted=True,
        profile_status="configured_not_verified",
    )
    return {
        "proofMode": OUTPUT_CLASS_PROOF_MODE,
        "productOutputClass": WEEKLY_REPORT_PRODUCT_OUTPUT_CLASS,
        "internalOutputClass": WEEKLY_REPORT_INTERNAL_OUTPUT_CLASS,
        "proofStatus": WEEKLY_REPORT_REQUIRED_PROOF_STATUS,
        "artifactRef": proof_artifact_ref,
        "researchOutputAllowed": research_gate.allowed,
        "reportDraftAllowed": report_gate.allowed,
        "paperReviewAndWeeklyReportRemainSeparate": True,
        "apiPipelineSkeletonCanBypassProof": False,
        "providerAuthIsAppOauthOidcProof": False,
        "localMcpIsPublicMcpConnectorProof": False,
        "researchOutputGate": research_gate.to_dict(),
        "reportDraftGate": report_gate.to_dict(),
    }


def build_codex_sdk_weekly_report_output_class_proof_artifact(
    *,
    weekly_report_output: Mapping[str, Any],
    report_context: Mapping[str, Any],
    auth_strategy_used: str,
    provider_auth_observed: Mapping[str, bool],
    artifact_ref: str,
    timestamp: str,
) -> dict[str, Any]:
    from app.services.provider_output_class_proof import build_output_class_proof

    normalized = _normalize_weekly_report_context(report_context)
    if not normalized["valid"]:
        raise ValueError(",".join(normalized["reasonCodes"]))
    safe_context = normalized["context"]
    output_validation = validate_codex_sdk_weekly_report_output(weekly_report_output, safe_context)
    if not output_validation["valid"]:
        raise ValueError(",".join(output_validation["reasonCodes"]))
    safe_weekly_report_output = output_validation["weeklyReport"]
    run_id = f"codex-sdk-weekly-report-output-class-proof-{timestamp}"
    proof = build_output_class_proof(
        provider_id=PROVIDER_ID,
        provider_profile_name=PROFILE_ID,
        execution_surface=EXECUTION_SURFACE_TYPE,
        output_class=WEEKLY_REPORT_PRODUCT_OUTPUT_CLASS,
        proof_mode=OUTPUT_CLASS_PROOF_MODE,
        topic_id=safe_context["topic_id"],
        topic_name=safe_context["topic_name"],
        paper_ids=safe_context["source_paper_ids"],
        full_text_evidence_source="provider_review_context_only",
        evidence_span_count=len(safe_context["evidence_span_refs_used"]),
        artifact_ref=artifact_ref,
        run_id=run_id,
        generated_at=timestamp,
        profile_status=WEEKLY_REPORT_REQUIRED_PROOF_STATUS,
        configured=True,
    )
    proof_record = proof.to_dict()
    gates = codex_sdk_weekly_report_output_class_gate_summary(proof_artifact_ref=artifact_ref)
    return {
        "artifactKind": OUTPUT_CLASS_ARTIFACT_KIND,
        "providerFamilyId": PROVIDER_FAMILY_ID,
        "providerId": PROVIDER_ID,
        "profileId": PROFILE_ID,
        "surfaceId": PROFILE_ID,
        "executionSurfaceType": EXECUTION_SURFACE_TYPE,
        "adapterKind": ADAPTER_KIND,
        "authStrategyUsed": auth_strategy_used,
        "proofMode": OUTPUT_CLASS_PROOF_MODE,
        "promptClass": WEEKLY_REPORT_PROMPT_CLASS,
        "productOutputClass": WEEKLY_REPORT_PRODUCT_OUTPUT_CLASS,
        "internalOutputClass": WEEKLY_REPORT_INTERNAL_OUTPUT_CLASS,
        "requiredProofMode": OUTPUT_CLASS_PROOF_MODE,
        "requiredProofStatus": WEEKLY_REPORT_REQUIRED_PROOF_STATUS,
        "currentProofStatus": WEEKLY_REPORT_REQUIRED_PROOF_STATUS,
        "acceptedOutputClassProof": bool(proof.accepted),
        "outputClassProof": bool(proof.accepted),
        "researchOutputAllowed": gates["researchOutputAllowed"],
        "reportDraftAllowed": gates["reportDraftAllowed"],
        "paperReviewProofStatus": "separate_output_class_proof_required",
        "providerSdkRunCount": 1,
        "providerCommandCount": 0,
        "providerCallCount": 1,
        "topicId": safe_context["topic_id"],
        "topicName": safe_context["topic_name"],
        "reportScope": safe_context["report_scope"],
        "sourceProviderReviewIds": list(safe_context["source_provider_review_ids"]),
        "sourcePaperIds": list(safe_context["source_paper_ids"]),
        "sourceProviderReviews": list(safe_context["provider_reviews"]),
        "evidenceSpanCount": len(safe_context["evidence_span_refs_used"]),
        "evidenceSpanRefsUsed": list(safe_weekly_report_output.get("evidence_span_refs_used") or []),
        "proofArtifactRefsUsed": list(safe_weekly_report_output.get("proof_artifact_refs_used") or []),
        "redactedProviderOutput": dict(safe_weekly_report_output),
        "artifactRef": artifact_ref,
        "timestamp": timestamp,
        "runId": run_id,
        "sdkSurfaceSummary": {
            "packageName": SDK_PACKAGE_NAME,
            "importNameCandidates": list(SDK_IMPORT_NAME_CANDIDATES),
            "executionSurfaceType": EXECUTION_SURFACE_TYPE,
            "adapterKind": ADAPTER_KIND,
            "manualAppServerStarted": False,
            "codexCliCommandRun": False,
            "workspaceWriteRequested": False,
            "readOnlySandboxRequested": True,
            "secretValuesPrinted": False,
        },
        "providerAuthObserved": {
            "authStrategyUsed": auth_strategy_used,
            "envPresence": dict(provider_auth_observed),
            "providerManagedAuthBlackBoxAllowed": auth_strategy_used == AUTH_STRATEGY_PROVIDER_MANAGED,
            "providerManagedAuthVerifiedByCacheInspection": False,
        },
        "outputClassGateSummary": gates,
        "proofRecordSummary": proof_record,
        "reasonCodes": [
            "provider_output_class_proof_accepted",
            "codex_sdk_single_weekly_report_run",
            "weekly_report_output_class_verified",
            "paper_review_not_unlocked_by_weekly_report",
            "db_report_persistence_not_in_scope",
        ],
        "liveCommandDisabled": False,
        "noSecretInspection": True,
        "noTokenCacheInspection": True,
        "noCredentialPathDisclosure": True,
        "appOauthOidcInScope": False,
        "publicMcpConnectorInScope": False,
        "outputClassProofInScope": True,
        "reportPersistenceInScope": False,
        "dbWriteInScope": False,
        "notInScope": [
            "app_oauth_oidc_not_in_scope",
            "public_mcp_not_in_scope",
            "paper_review_not_in_scope_for_this_run",
            "db_report_persistence_not_in_scope",
            "production_external_beta_not_in_scope",
        ],
        "no_chain_of_thought": True,
        "redactionStatus": "passed",
        "actualEnvFileInspected": False,
        "actualMcpConfigFileInspected": False,
        "credentialCacheSessionAuthFileInspectedByProject": False,
        "productionReady": False,
        "externalBetaReady": False,
        "safetyStatements": [
            "No provider command was run by this project.",
            "No provider auth/cache files were inspected by this project.",
            "This proof does not establish App OAuth/OIDC, public MCP connector, DB report persistence, production, or external beta readiness.",
            "This is not persisted report generation.",
        ],
    }


def validate_codex_sdk_weekly_report_output_class_proof_artifact_contract(
    artifact: Mapping[str, Any]
) -> dict[str, Any]:
    from app.services.provider_output_class_proof import scan_for_forbidden_content

    reasons: list[str] = []
    required_fixed = {
        "artifactKind": OUTPUT_CLASS_ARTIFACT_KIND,
        "providerFamilyId": PROVIDER_FAMILY_ID,
        "providerId": PROVIDER_ID,
        "profileId": PROFILE_ID,
        "surfaceId": PROFILE_ID,
        "executionSurfaceType": EXECUTION_SURFACE_TYPE,
        "adapterKind": ADAPTER_KIND,
        "proofMode": OUTPUT_CLASS_PROOF_MODE,
        "productOutputClass": WEEKLY_REPORT_PRODUCT_OUTPUT_CLASS,
        "internalOutputClass": WEEKLY_REPORT_INTERNAL_OUTPUT_CLASS,
        "requiredProofMode": OUTPUT_CLASS_PROOF_MODE,
        "requiredProofStatus": WEEKLY_REPORT_REQUIRED_PROOF_STATUS,
        "currentProofStatus": WEEKLY_REPORT_REQUIRED_PROOF_STATUS,
        "acceptedOutputClassProof": True,
        "outputClassProof": True,
        "researchOutputAllowed": False,
        "reportDraftAllowed": True,
        "providerSdkRunCount": 1,
        "providerCommandCount": 0,
        "providerCallCount": 1,
        "appOauthOidcInScope": False,
        "publicMcpConnectorInScope": False,
        "outputClassProofInScope": True,
        "reportPersistenceInScope": False,
        "dbWriteInScope": False,
        "no_chain_of_thought": True,
        "redactionStatus": "passed",
        "productionReady": False,
        "externalBetaReady": False,
    }
    for key, expected in required_fixed.items():
        if artifact.get(key) != expected:
            reasons.append(f"field_mismatch:{key}")
    if artifact.get("authStrategyUsed") not in {
        AUTH_STRATEGY_PROVIDER_MANAGED,
        AUTH_STRATEGY_ACCESS_TOKEN,
        AUTH_STRATEGY_API_KEY,
    }:
        reasons.append("unsafe_or_unknown_auth_strategy_for_accepted_proof")
    context = {
        "topic_id": artifact.get("topicId"),
        "topic_name": artifact.get("topicName"),
        "report_scope": artifact.get("reportScope"),
        "source_provider_review_ids": artifact.get("sourceProviderReviewIds"),
        "source_paper_ids": artifact.get("sourcePaperIds"),
        "evidence_span_refs_used": artifact.get("evidenceSpanRefsUsed"),
        "proof_artifact_refs_used": artifact.get("proofArtifactRefsUsed"),
        "provider_reviews": artifact.get("sourceProviderReviews"),
    }
    validation = validate_codex_sdk_weekly_report_output(
        artifact.get("redactedProviderOutput") or {},
        context,
    )
    if not validation["valid"]:
        reasons.append("provider_output_not_valid_weekly_report_json")
        reasons.extend(str(reason) for reason in validation.get("reasonCodes", []))
    proof_record = artifact.get("proofRecordSummary") or {}
    if scan_for_forbidden_content(proof_record):
        reasons.append("proof_record_secret_like_content_detected")
    gates = artifact.get("outputClassGateSummary") or {}
    if gates.get("reportDraftAllowed") is not True or gates.get("researchOutputAllowed") is not False:
        reasons.append("output_class_gate_mismatch")
    if _has_secret_like_value(artifact):
        reasons.append("secret_like_content_detected")
    if _contains_chain_of_thought_value(artifact):
        reasons.append("chain_of_thought_content_not_allowed")
    return {"valid": not reasons, "reasonCodes": list(dict.fromkeys(reasons))}


@dataclass(frozen=True)
class CodexSDKProviderConfig:
    provider_id: str = PROVIDER_ID
    profile_id: str = PROFILE_ID
    provider_family_id: str = PROVIDER_FAMILY_ID
    execution_surface_type: str = EXECUTION_SURFACE_TYPE
    adapter_kind: str = ADAPTER_KIND
    backend_role: str = BACKEND_ROLE
    provider_auth_scope: str = PROVIDER_AUTH_SCOPE
    app_oauth_oidc_in_scope: bool = False
    public_mcp_connector_in_scope: bool = False
    output_class_proof_in_scope: bool = False
    no_secret_inspection: bool = True
    no_token_cache_inspection: bool = True
    no_credential_path_disclosure: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "providerId": self.provider_id,
            "profileId": self.profile_id,
            "providerFamilyId": self.provider_family_id,
            "executionSurfaceType": self.execution_surface_type,
            "adapterKind": self.adapter_kind,
            "backendRole": self.backend_role,
            "providerAuthScope": self.provider_auth_scope,
            "appOauthOidcInScope": self.app_oauth_oidc_in_scope,
            "publicMcpConnectorInScope": self.public_mcp_connector_in_scope,
            "outputClassProofInScope": self.output_class_proof_in_scope,
            "noSecretInspection": self.no_secret_inspection,
            "noTokenCacheInspection": self.no_token_cache_inspection,
            "noCredentialPathDisclosure": self.no_credential_path_disclosure,
        }


@dataclass(frozen=True)
class CodexSDKReadiness:
    config: CodexSDKProviderConfig
    dependency_status: dict[str, Any]
    auth_strategy_status: dict[str, Any]
    reason_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = self.config.to_dict()
        payload.update(
            {
                "configured": False,
                "liveReady": False,
                "mayRunNow": False,
                "wouldImportSdk": False,
                "wouldStartAppServer": False,
                "wouldExecuteProvider": False,
                "wouldCallNetwork": False,
                "requiresOwnerApproval": True,
                "requiresProcessEnvOnly": True,
                "dependencyStatus": dict(self.dependency_status),
                "authStrategyStatus": dict(self.auth_strategy_status),
                "authEnvNames": list(AUTH_ENV_NAME_CANDIDATES),
                "authEnvPresence": dict(self.auth_strategy_status["envPresence"]),
                "selectedAuthStrategyLabel": self.auth_strategy_status["selectedAuthStrategyLabel"],
                "providerManagedAuthRepresented": True,
                "providerManagedAuthVerified": False,
                "appOauthOidcSatisfied": False,
                "publicMcpConnectorProofSatisfied": False,
                "outputClassCandidates": list(SUPPORTED_OUTPUT_CLASS_CANDIDATES),
                "provenOutputClasses": [],
                "researchOutputAllowed": False,
                "reportDraftAllowed": False,
                "reasonCodes": list(dict.fromkeys(self.reason_codes)),
            }
        )
        return payload


@dataclass(frozen=True)
class CodexSDKExecutionPlan:
    task_kind: str
    output_class: str | None
    live: bool
    reason_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        gate = _gate(self.output_class)
        known_task = self.task_kind in SUPPORTED_TASK_KINDS
        live_call_count = 1 if self.live and known_task else 0
        return {
            "providerFamilyId": PROVIDER_FAMILY_ID,
            "surfaceId": PROFILE_ID,
            "profileId": PROFILE_ID,
            "providerId": PROVIDER_ID,
            "executionSurfaceType": EXECUTION_SURFACE_TYPE,
            "adapterKind": ADAPTER_KIND,
            "backendRole": BACKEND_ROLE,
            "taskKind": self.task_kind,
            "knownTaskKind": known_task,
            "outputClass": _contract_output_class(self.output_class),
            "liveRequested": self.live,
            "wouldImportSdk": False,
            "wouldStartAppServer": False,
            "wouldExecuteProvider": False,
            "wouldCallNetwork": False,
            "mayRunNow": False,
            "requiresOwnerApproval": True,
            "requiresProcessEnvOnly": True,
            "providerCallCountPlanned": live_call_count,
            "outputClassProof": False,
            "researchOutputAllowed": False,
            "reportDraftAllowed": False,
            "cannotUnlockOutputClass": True,
            "requiredProofLevel": gate.required_proof_level,
            "blockedReason": gate.blocked_reason,
            "appOauthOidcInScope": False,
            "publicMcpConnectorInScope": False,
            "noSecretInspection": True,
            "noTokenCacheInspection": True,
            "noCredentialPathDisclosure": True,
            "reasonCodes": list(dict.fromkeys(self.reason_codes)),
        }


class CodexSDKBlocked(RuntimeError):
    """Raised when code attempts to use the non-live Codex SDK scaffold."""


def describe_codex_sdk_dependency_status(*, allow_find_spec: bool = False) -> dict[str, Any]:
    """Return dependency metadata without importing the SDK.

    ``allow_find_spec`` exists only for a future owner-approved diagnostics path.
    The default path intentionally marks install state unknown and does not probe
    module specs, because availability is not execution proof.
    """
    base: dict[str, Any] = {
        "packageName": SDK_PACKAGE_NAME,
        "importName": SDK_IMPORT_NAME,
        "alternateImportNames": list(SDK_ALTERNATE_IMPORT_NAMES),
        "importNameCandidates": list(SDK_IMPORT_NAME_CANDIDATES),
        "dependencyGroup": CODEX_SDK_DEPENDENCY_GROUP,
        "dependencyDeclared": True,
        "dependencyDeclaredInProjectOptionalDependencies": True,
        "installedKnown": None,
        "dependencyInstalledKnown": None,
        "dependencyInstalledStatus": "dependency_installed_unknown_not_checked",
        "dependencyMissingObservedFromPriorLiveAttempt": True,
        "dependencyInstallRequired": True,
        "dependencyLockUpdateRequired": True,
        "lockUpdateRequired": True,
        "lockUpdateCommandCandidate": CODEX_SDK_LOCK_UPDATE_COMMAND_CANDIDATE,
        "installCommandCandidates": list(CODEX_SDK_INSTALL_COMMAND_CANDIDATES),
        "importCheckAllowedNow": False,
        "wouldImportSdk": False,
        "wouldStartAppServer": False,
        "wouldExecuteProvider": False,
        "defaultBlocked": True,
        "readinessStates": list(CODEX_SDK_DEPENDENCY_READINESS_STATES),
        "status": "dependency_declared_install_required_sdk_import_deferred",
        "reasonCodes": [
            "codex_sdk_dependency_declared",
            "codex_sdk_dependency_not_imported",
            "sdk_import_not_attempted",
            "unknown_do_not_import",
            "blocked_pending_dependency_install",
            "blocked_pending_owner_live_approval",
            "output_class_proof_required",
            "sdk_availability_cannot_unlock_output_class",
        ],
    }
    if not allow_find_spec:
        return base
    installed = any(importlib.util.find_spec(name) is not None for name in SDK_IMPORT_NAME_CANDIDATES)
    payload = {
        **base,
        "installedKnown": installed,
        "dependencyInstalledKnown": installed,
        "dependencyInstalledStatus": "dependency_spec_found_nonlive" if installed else "dependency_not_found_nonlive",
        "status": "installed_spec_found_nonlive" if installed else "not_found_nonlive",
        "importCheckAllowedNow": True,
        "wouldImportSdk": False,
        "reasonCodes": [
            *base["reasonCodes"],
            "find_spec_only_no_sdk_import",
            "sdk_availability_cannot_unlock_output_class",
        ],
    }
    return payload


def describe_codex_sdk_dependency_install_runbook() -> dict[str, Any]:
    dependency = describe_codex_sdk_dependency_status()
    return {
        "title": "Codex SDK dependency install runbook",
        "ownerRunOnly": True,
        "nonLiveInstallOnly": True,
        "packageName": SDK_PACKAGE_NAME,
        "importName": SDK_IMPORT_NAME,
        "dependencyGroup": CODEX_SDK_DEPENDENCY_GROUP,
        "installCommandCandidates": list(CODEX_SDK_INSTALL_COMMAND_CANDIDATES),
        "lockUpdateRequired": dependency["lockUpdateRequired"],
        "lockUpdateCommandCandidate": CODEX_SDK_LOCK_UPDATE_COMMAND_CANDIDATE,
        "postInstallNonLiveTests": [
            "pytest -q tests/test_codex_sdk_provider_scaffold.py",
            "pytest -q tests/test_provider_backend_scaffold.py",
        ],
        "futureLivePrompt": "CODEX_SDK_OWNER_APPROVED_ONE_CALL_TINY_PROOF_LIVE",
        "safetyNotes": [
            "Install only inside an isolated project environment.",
            "Do not paste secrets into chat.",
            "Do not inspect Codex auth, cache, session, or CODEX_HOME files.",
            "Do not run a live proof during dependency installation.",
        ],
        "doesNotProve": [
            "provider_auth",
            "provider_tiny_proof",
            "output_class_proof",
            "app_oauth_oidc",
            "public_mcp_connector",
            "production_readiness",
        ],
        "wouldImportSdk": False,
        "wouldStartAppServer": False,
        "wouldExecuteProvider": False,
        "defaultBlocked": True,
        "reasonCodes": [
            "codex_sdk_dependency_declared",
            "codex_sdk_dependency_not_imported",
            "blocked_pending_dependency_install",
            "blocked_pending_owner_live_approval",
            "output_class_proof_required",
        ],
    }


def render_codex_sdk_dependency_install_runbook() -> str:
    runbook = describe_codex_sdk_dependency_install_runbook()
    lines = [
        "Codex SDK dependency install runbook (future owner-run, non-live install only)",
        f"Package: {runbook['packageName']}",
        f"Import: {runbook['importName']}",
        f"Optional extra: {runbook['dependencyGroup']}",
        "Install in an isolated project environment only.",
        "Do not paste secrets into chat.",
        "Do not inspect Codex auth/cache/session files or CODEX_HOME contents.",
        "Do not run live proof during install.",
        "Future install command candidates:",
        *[f"- {command}" for command in runbook["installCommandCandidates"]],
        f"Future lock refresh candidate: {runbook['lockUpdateCommandCandidate']}",
        "After install, run non-live tests first:",
        *[f"- {command}" for command in runbook["postInstallNonLiveTests"]],
        f"Then use future prompt: {runbook['futureLivePrompt']}",
        "Installing the SDK does not prove provider auth, tiny proof, output-class proof, App OAuth/OIDC, public MCP, or production readiness.",
    ]
    return "\n".join(lines)


def build_codex_sdk_tiny_proof_artifact(
    *,
    sentinel_output: Mapping[str, Any],
    auth_strategy_used: str,
    provider_auth_observed: Mapping[str, bool],
    artifact_ref: str,
    timestamp: str,
) -> dict[str, Any]:
    return {
        "artifactKind": ARTIFACT_KIND,
        "providerFamilyId": PROVIDER_FAMILY_ID,
        "providerId": PROVIDER_ID,
        "profileId": PROFILE_ID,
        "surfaceId": PROFILE_ID,
        "executionSurfaceType": EXECUTION_SURFACE_TYPE,
        "adapterKind": ADAPTER_KIND,
        "authStrategyUsed": auth_strategy_used,
        "proofMode": PROOF_MODE,
        "tinyProofKind": TINY_PROOF_KIND,
        "promptClass": PROMPT_CLASS,
        "inputSizeClass": INPUT_SIZE_CLASS,
        "providerSdkRunCount": 1,
        "providerCommandCount": 0,
        "providerCallCount": 1,
        "acceptedTinyProof": True,
        "outputClassProof": False,
        "researchOutputAllowed": False,
        "reportDraftAllowed": False,
        "cannotUnlockOutputClass": True,
        "proofStatusCandidate": PROOF_STATUS_CANDIDATE,
        "providerOutput": dict(sentinel_output),
        "artifactRef": artifact_ref,
        "timestamp": timestamp,
        "runId": f"codex-sdk-tiny-proof-{timestamp}",
        "sdkSurfaceSummary": {
            "packageName": SDK_PACKAGE_NAME,
            "importNameCandidates": list(SDK_IMPORT_NAME_CANDIDATES),
            "executionSurfaceType": EXECUTION_SURFACE_TYPE,
            "adapterKind": ADAPTER_KIND,
            "manualAppServerStarted": False,
            "codexCliCommandRun": False,
            "workspaceWriteRequested": False,
            "readOnlySandboxRequested": True,
            "secretValuesPrinted": False,
        },
        "providerAuthObserved": {
            "authStrategyUsed": auth_strategy_used,
            "envPresence": dict(provider_auth_observed),
            "providerManagedAuthBlackBoxAllowed": auth_strategy_used == AUTH_STRATEGY_PROVIDER_MANAGED,
            "providerManagedAuthVerifiedByCacheInspection": False,
        },
        "outputClassGateSummary": codex_sdk_tiny_proof_output_gate_summary(),
        "reasonCodes": [
            "provider_tiny_proof_accepted",
            "codex_sdk_single_sentinel_run",
            "tiny_proof_not_output_class_proof",
            "tiny_proof_cannot_unlock_output_class",
        ],
        "noSecretInspection": True,
        "noTokenCacheInspection": True,
        "noCredentialPathDisclosure": True,
        "appOauthOidcInScope": False,
        "publicMcpConnectorInScope": False,
        "outputClassProofInScope": False,
        "notInScope": [
            "app_oauth_oidc_not_in_scope",
            "public_mcp_not_in_scope",
            "output_class_proof_not_in_scope",
            "paper_review_not_in_scope",
            "weekly_report_not_in_scope",
        ],
        "no_chain_of_thought": True,
        "redactionStatus": "passed",
        "safetyStatements": [
            "No credential, cache, session, or auth file was inspected by this project.",
            "No actual env file or MCP config file was inspected by this project.",
            "This provider_tiny_proof is not production or external beta readiness.",
        ],
    }


def validate_codex_sdk_tiny_proof_artifact_contract(artifact: Mapping[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    required_fixed = {
        "artifactKind": ARTIFACT_KIND,
        "providerFamilyId": PROVIDER_FAMILY_ID,
        "providerId": PROVIDER_ID,
        "profileId": PROFILE_ID,
        "surfaceId": PROFILE_ID,
        "executionSurfaceType": EXECUTION_SURFACE_TYPE,
        "adapterKind": ADAPTER_KIND,
        "proofMode": PROOF_MODE,
        "tinyProofKind": TINY_PROOF_KIND,
        "providerSdkRunCount": 1,
        "providerCommandCount": 0,
        "providerCallCount": 1,
        "acceptedTinyProof": True,
        "outputClassProof": False,
        "researchOutputAllowed": False,
        "reportDraftAllowed": False,
        "cannotUnlockOutputClass": True,
        "proofStatusCandidate": PROOF_STATUS_CANDIDATE,
        "appOauthOidcInScope": False,
        "publicMcpConnectorInScope": False,
        "outputClassProofInScope": False,
        "no_chain_of_thought": True,
        "redactionStatus": "passed",
    }
    for key, expected in required_fixed.items():
        if artifact.get(key) != expected:
            reasons.append(f"field_mismatch:{key}")
    sentinel = validate_codex_sdk_sentinel_output(artifact.get("providerOutput") or {})
    if not sentinel["valid"]:
        reasons.append("provider_output_not_valid_sentinel_json")
    if artifact.get("authStrategyUsed") not in {
        AUTH_STRATEGY_PROVIDER_MANAGED,
        AUTH_STRATEGY_ACCESS_TOKEN,
        AUTH_STRATEGY_API_KEY,
    }:
        reasons.append("unsafe_or_unknown_auth_strategy_for_accepted_proof")
    if _has_secret_like_value(artifact):
        reasons.append("secret_like_content_detected")
    gates = codex_sdk_tiny_proof_output_gate_summary()
    if gates["researchOutputAllowed"] or gates["reportDraftAllowed"]:
        reasons.append("tiny_proof_cross_unlock_risk")
    return {"valid": not reasons, "reasonCodes": list(dict.fromkeys(reasons))}


CodexSDKRunner = Callable[[str], str | Mapping[str, Any]]


class CodexSDKImportUnavailable(RuntimeError):
    pass


class CodexSDKSentinelRunFailed(RuntimeError):
    pass


PrototypeCodexSDKRunner = Callable[[str, Mapping[str, Any], str], Awaitable[Any]]


def _read_only_sandbox_value(sandbox_class: Any) -> Any:
    if sandbox_class is None:
        return None
    for name in ("read_only", "READ_ONLY", "ReadOnly"):
        value = getattr(sandbox_class, name, None)
        if value is not None:
            return value
    return None


def _extract_final_response(result: Any) -> str:
    for attr in ("final_response", "finalResponse", "output_text", "text", "content"):
        value = getattr(result, attr, None)
        if value:
            return str(value)
    if isinstance(result, Mapping):
        for key in ("final_response", "finalResponse", "output_text", "text", "content"):
            value = result.get(key)
            if value:
                return str(value)
    return str(result or "")


def _default_codex_sdk_runner(prompt: str) -> str:
    last_import_error: Exception | None = None
    sdk_module: Any | None = None
    for import_name in ("openai_codex", "codex"):
        try:
            sdk_module = importlib.import_module(import_name)
            break
        except ImportError as exc:
            last_import_error = exc
    if sdk_module is None:
        raise CodexSDKImportUnavailable("Codex SDK import is not available.") from last_import_error
    codex_class = getattr(sdk_module, "Codex", None)
    if codex_class is None:
        raise CodexSDKImportUnavailable("Codex SDK Codex class is not available.")
    sandbox = _read_only_sandbox_value(getattr(sdk_module, "Sandbox", None))
    try:
        with codex_class() as codex_client:
            if sandbox is None:
                thread = codex_client.thread_start()
            else:
                thread = codex_client.thread_start(sandbox=sandbox)
            result = thread.run(prompt)
    except Exception as exc:  # noqa: BLE001 - SDK exception taxonomy is not part of this scaffold.
        raise CodexSDKSentinelRunFailed(exc.__class__.__name__) from exc
    return _extract_final_response(result)


async def _default_prototype_report_draft_runner(
    prompt: str,
    output_schema: Mapping[str, Any],
    cwd: str,
) -> dict[str, Any]:
    """Run one ephemeral, read-only Codex SDK turn for Prototype report drafting."""

    last_import_error: Exception | None = None
    sdk_module: Any | None = None
    for import_name in ("openai_codex", "codex"):
        try:
            sdk_module = importlib.import_module(import_name)
            break
        except ImportError as exc:
            last_import_error = exc
    if sdk_module is None:
        raise CodexSDKImportUnavailable("Codex SDK import is not available.") from last_import_error

    async_codex_class = getattr(sdk_module, "AsyncCodex", None)
    if async_codex_class is None:
        raise CodexSDKImportUnavailable("Codex SDK AsyncCodex class is not available.")
    sandbox_class = getattr(sdk_module, "Sandbox", None)
    approval_class = getattr(sdk_module, "ApprovalMode", None)
    sandbox = _read_only_sandbox_value(sandbox_class)
    approval_mode = getattr(approval_class, "deny_all", None) if approval_class is not None else None

    try:
        async with async_codex_class() as codex_client:
            thread_kwargs: dict[str, Any] = {
                "cwd": cwd,
                "ephemeral": True,
            }
            if sandbox is not None:
                thread_kwargs["sandbox"] = sandbox
            if approval_mode is not None:
                thread_kwargs["approval_mode"] = approval_mode
            thread = await codex_client.thread_start(**thread_kwargs)
            run_kwargs: dict[str, Any] = {
                "cwd": cwd,
                "output_schema": dict(output_schema),
            }
            if sandbox is not None:
                run_kwargs["sandbox"] = sandbox
            if approval_mode is not None:
                run_kwargs["approval_mode"] = approval_mode
            result = await thread.run(prompt, **run_kwargs)
    except Exception as exc:  # noqa: BLE001 - SDK exception details must stay private.
        raise CodexSDKSentinelRunFailed(exc.__class__.__name__) from exc

    return {
        "raw_output": _extract_final_response(result),
        "duration_ms": getattr(result, "duration_ms", None),
        "model_backend_identifier": None,
    }


def _stop_reason_from_exception(exc: Exception) -> str:
    if isinstance(exc, CodexSDKImportUnavailable):
        return "CODEX_SDK_IMPORT_NOT_AVAILABLE"
    text = f"{exc.__class__.__name__} {exc}".lower()
    if "browser" in text:
        return "WOULD_REQUIRE_BROWSER_LOGIN"
    if "device" in text:
        return "WOULD_REQUIRE_DEVICE_FLOW"
    if "interactive" in text or "login" in text:
        return "WOULD_REQUIRE_INTERACTIVE_LOGIN"
    if "auth" in text or "unauthorized" in text or "credential" in text:
        return "CODEX_SDK_PROVIDER_AUTH_NOT_AVAILABLE"
    return "CODEX_SDK_SENTINEL_RUN_FAILED"


async def run_codex_sdk_prototype_report_draft_once(
    *,
    prompt: str,
    output_schema: Mapping[str, Any],
    cwd: str,
    timeout_seconds: int,
    sdk_runner: PrototypeCodexSDKRunner | None = None,
) -> dict[str, Any]:
    """Execute one bounded Prototype report-draft turn without retrying.

    The caller owns activation, quarantine, validation, acceptance, and
    persistence.  Raw output is returned only to that in-process caller.
    """

    if not prompt.strip() or len(prompt.encode("utf-8")) > 65_536:
        return {
            "status": "stopped",
            "result_category": "request_bounds_rejected",
            "provider_call_count": 0,
            "reason_category": "PROTOTYPE_REPORT_DRAFT_REQUEST_INVALID",
        }
    if timeout_seconds < 1 or timeout_seconds > 300:
        return {
            "status": "stopped",
            "result_category": "request_bounds_rejected",
            "provider_call_count": 0,
            "reason_category": "PROTOTYPE_REPORT_DRAFT_TIMEOUT_INVALID",
        }

    runner = sdk_runner or _default_prototype_report_draft_runner
    try:
        result = await asyncio.wait_for(
            runner(prompt, output_schema, cwd),
            timeout=float(timeout_seconds),
        )
    except TimeoutError:
        return {
            "status": "timeout",
            "result_category": "provider_timeout",
            "provider_call_count": 1,
            "reason_category": "CODEX_SDK_PROTOTYPE_REPORT_DRAFT_TIMEOUT",
        }
    except Exception as exc:  # noqa: BLE001 - never expose raw SDK errors.
        reason = _stop_reason_from_exception(exc)
        return {
            "status": "unavailable" if reason in {
                "CODEX_SDK_IMPORT_NOT_AVAILABLE",
                "CODEX_SDK_PROVIDER_AUTH_NOT_AVAILABLE",
                "WOULD_REQUIRE_BROWSER_LOGIN",
                "WOULD_REQUIRE_DEVICE_FLOW",
                "WOULD_REQUIRE_INTERACTIVE_LOGIN",
            } else "failed",
            "result_category": "provider_unavailable" if reason != "CODEX_SDK_SENTINEL_RUN_FAILED" else "provider_failed",
            "provider_call_count": 0 if reason == "CODEX_SDK_IMPORT_NOT_AVAILABLE" else 1,
            "reason_category": reason,
        }

    if isinstance(result, Mapping):
        raw_output = str(result.get("raw_output") or "")
        duration_ms = result.get("duration_ms")
        model_backend_identifier = result.get("model_backend_identifier")
    else:
        raw_output = str(result or "")
        duration_ms = None
        model_backend_identifier = None
    return {
        "status": "completed",
        "result_category": "provider_output_received",
        "provider_call_count": 1,
        "raw_output": raw_output,
        "duration_ms": duration_ms,
        "model_backend_identifier": model_backend_identifier,
        "reason_category": None,
    }


def run_codex_sdk_tiny_sentinel_proof(
    *,
    env: Mapping[str, str] | None = None,
    sdk_runner: CodexSDKRunner | None = None,
    artifact_path: str | None = None,
    write_artifact: bool = True,
) -> dict[str, Any]:
    timestamp = iso_now_utc()
    artifact_ref = artifact_path or _default_artifact_ref(timestamp)
    path_policy = validate_codex_sdk_tiny_proof_artifact_path(artifact_ref)
    auth_label, auth_observed = _auth_label_for_live_attempt(env)
    if not path_policy["valid"]:
        return {
            "status": "stopped",
            "stopReason": "CODEX_SDK_SENTINEL_RUN_FAILED",
            "providerSdkRunCount": 0,
            "providerCommandCount": 0,
            "artifactPath": None,
            "artifact": None,
            "reasonCodes": path_policy["reasonCodes"],
        }
    runner = sdk_runner or _default_codex_sdk_runner
    try:
        raw_output = runner(SENTINEL_PROMPT)
    except Exception as exc:  # noqa: BLE001 - do not store raw SDK logs or messages.
        stop_reason = _stop_reason_from_exception(exc)
        return {
            "status": "stopped",
            "stopReason": stop_reason,
            "providerSdkRunCount": 0,
            "providerCommandCount": 0,
            "artifactPath": None,
            "artifact": None,
            "reasonCodes": [stop_reason],
            "errorClass": exc.__class__.__name__,
        }
    validation = validate_codex_sdk_sentinel_output(raw_output)
    if not validation["valid"]:
        stop_reason = (
            "SECRET_LIKE_CONTENT_DETECTED"
            if "secret_like_content_detected" in validation["reasonCodes"]
            else "CODEX_SDK_OUTPUT_NOT_VALID_SENTINEL_JSON"
        )
        return {
            "status": "stopped",
            "stopReason": stop_reason,
            "providerSdkRunCount": 1,
            "providerCommandCount": 0,
            "artifactPath": None,
            "artifact": None,
            "reasonCodes": validation["reasonCodes"],
        }
    artifact = build_codex_sdk_tiny_proof_artifact(
        sentinel_output=validation["sentinel"],
        auth_strategy_used=auth_label,
        provider_auth_observed=auth_observed,
        artifact_ref=str(path_policy["pathForPlan"]),
        timestamp=timestamp,
    )
    artifact_validation = validate_codex_sdk_tiny_proof_artifact_contract(artifact)
    if not artifact_validation["valid"]:
        stop_reason = (
            "SECRET_LIKE_CONTENT_DETECTED"
            if "secret_like_content_detected" in artifact_validation["reasonCodes"]
            else "OUTPUT_CLASS_GATE_CROSS_UNLOCK_RISK"
            if "tiny_proof_cross_unlock_risk" in artifact_validation["reasonCodes"]
            else "CODEX_SDK_OUTPUT_NOT_VALID_SENTINEL_JSON"
        )
        return {
            "status": "stopped",
            "stopReason": stop_reason,
            "providerSdkRunCount": 1,
            "providerCommandCount": 0,
            "artifactPath": None,
            "artifact": None,
            "reasonCodes": artifact_validation["reasonCodes"],
        }
    if write_artifact:
        target = Path(str(path_policy["pathForPlan"]))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "status": "accepted_tiny_proof",
        "stopReason": None,
        "providerSdkRunCount": 1,
        "providerCommandCount": 0,
        "providerCallCount": 1,
        "authStrategyUsed": auth_label,
        "artifactPath": str(path_policy["pathForPlan"]),
        "artifact": artifact,
        "sentinelValidation": {"valid": True, "reasonCodes": []},
        "artifactValidation": artifact_validation,
        "outputClassGateSummary": codex_sdk_tiny_proof_output_gate_summary(),
        "reasonCodes": ["provider_tiny_proof_accepted", "output_class_proof_not_in_scope"],
    }


def run_codex_sdk_paper_review_output_class_proof(
    *,
    evidence: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
    sdk_runner: CodexSDKRunner | None = None,
    artifact_path: str | None = None,
    write_artifact: bool = True,
) -> dict[str, Any]:
    timestamp = iso_now_utc()
    artifact_ref = artifact_path or _default_output_class_artifact_ref(timestamp)
    path_policy = validate_codex_sdk_output_class_proof_artifact_path(artifact_ref)
    auth_label, auth_observed = _auth_label_for_live_attempt(env)
    if not path_policy["valid"]:
        return {
            "status": "stopped",
            "stopReason": "CODEX_SDK_PAPER_REVIEW_RUN_FAILED",
            "providerSdkRunCount": 0,
            "providerCommandCount": 0,
            "providerCallCount": 0,
            "artifactPath": None,
            "artifact": None,
            "reasonCodes": path_policy["reasonCodes"],
        }
    normalized = _normalize_paper_review_evidence(evidence)
    if not normalized["valid"]:
        stop_reason = (
            "SECRET_LIKE_CONTENT_DETECTED"
            if "secret_like_content_detected" in normalized["reasonCodes"]
            else "LOCAL_EVIDENCE_PRECONDITION_FAILED"
        )
        return {
            "status": "stopped",
            "stopReason": stop_reason,
            "providerSdkRunCount": 0,
            "providerCommandCount": 0,
            "providerCallCount": 0,
            "artifactPath": None,
            "artifact": None,
            "reasonCodes": normalized["reasonCodes"],
        }
    prompt = build_codex_sdk_paper_review_prompt(normalized["evidence"])
    runner = sdk_runner or _default_codex_sdk_runner
    try:
        raw_output = runner(prompt)
    except Exception as exc:  # noqa: BLE001 - do not store raw SDK logs or messages.
        stop_reason = _stop_reason_from_exception(exc)
        if stop_reason == "CODEX_SDK_SENTINEL_RUN_FAILED":
            stop_reason = "CODEX_SDK_PAPER_REVIEW_RUN_FAILED"
        return {
            "status": "stopped",
            "stopReason": stop_reason,
            "providerSdkRunCount": 0,
            "providerCommandCount": 0,
            "providerCallCount": 0,
            "artifactPath": None,
            "artifact": None,
            "reasonCodes": [stop_reason],
            "errorClass": exc.__class__.__name__,
        }
    validation = validate_codex_sdk_paper_review_output(raw_output, normalized["evidence"])
    if not validation["valid"]:
        stop_reason = (
            "SECRET_LIKE_CONTENT_DETECTED"
            if "secret_like_content_detected" in validation["reasonCodes"]
            else "CODEX_SDK_OUTPUT_NOT_VALID_PAPER_REVIEW_JSON"
        )
        return {
            "status": "stopped",
            "stopReason": stop_reason,
            "providerSdkRunCount": 1,
            "providerCommandCount": 0,
            "providerCallCount": 1,
            "artifactPath": None,
            "artifact": None,
            "outputJsonValidation": {"valid": False, "reasonCodes": validation["reasonCodes"]},
            "reasonCodes": validation["reasonCodes"],
        }
    artifact = build_codex_sdk_paper_review_output_class_proof_artifact(
        paper_review_output=validation["paperReview"],
        evidence=normalized["evidence"],
        auth_strategy_used=auth_label,
        provider_auth_observed=auth_observed,
        artifact_ref=str(path_policy["pathForPlan"]),
        timestamp=timestamp,
    )
    artifact_validation = validate_codex_sdk_paper_review_output_class_proof_artifact_contract(artifact)
    if not artifact_validation["valid"]:
        reasons = artifact_validation["reasonCodes"]
        stop_reason = (
            "SECRET_LIKE_CONTENT_DETECTED"
            if "secret_like_content_detected" in reasons or "proof_record_secret_like_content_detected" in reasons
            else "OUTPUT_CLASS_GATE_CROSS_UNLOCK_RISK"
            if "output_class_gate_mismatch" in reasons
            else "OUTPUT_CLASS_PROOF_ARTIFACT_INVALID"
        )
        return {
            "status": "stopped",
            "stopReason": stop_reason,
            "providerSdkRunCount": 1,
            "providerCommandCount": 0,
            "providerCallCount": 1,
            "artifactPath": None,
            "artifact": None,
            "outputJsonValidation": {"valid": True, "reasonCodes": []},
            "artifactValidation": artifact_validation,
            "reasonCodes": reasons,
        }
    if write_artifact:
        target = Path(str(path_policy["pathForPlan"]))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "status": "accepted_output_class_proof",
        "stopReason": None,
        "providerSdkRunCount": 1,
        "providerCommandCount": 0,
        "providerCallCount": 1,
        "authStrategyUsed": auth_label,
        "artifactPath": str(path_policy["pathForPlan"]),
        "artifact": artifact,
        "outputJsonValidation": {"valid": True, "reasonCodes": []},
        "artifactValidation": artifact_validation,
        "outputClassGateSummary": artifact["outputClassGateSummary"],
        "reasonCodes": [
            "provider_output_class_proof_accepted",
            "paper_review_output_class_verified",
            "weekly_report_not_unlocked",
        ],
    }


def run_codex_sdk_weekly_report_output_class_proof(
    *,
    report_context: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
    sdk_runner: CodexSDKRunner | None = None,
    artifact_path: str | None = None,
    write_artifact: bool = True,
) -> dict[str, Any]:
    timestamp = iso_now_utc()
    artifact_ref = artifact_path or _default_output_class_artifact_ref(
        timestamp,
        WEEKLY_REPORT_PRODUCT_OUTPUT_CLASS,
    )
    path_policy = validate_codex_sdk_output_class_proof_artifact_path(artifact_ref)
    auth_label, auth_observed = _auth_label_for_live_attempt(env)
    if not path_policy["valid"]:
        return {
            "status": "stopped",
            "stopReason": "CODEX_SDK_WEEKLY_REPORT_RUN_FAILED",
            "providerSdkRunCount": 0,
            "providerCommandCount": 0,
            "providerCallCount": 0,
            "artifactPath": None,
            "artifact": None,
            "reasonCodes": path_policy["reasonCodes"],
        }
    normalized = _normalize_weekly_report_context(report_context)
    if not normalized["valid"]:
        stop_reason = (
            "SECRET_LIKE_CONTENT_DETECTED"
            if "secret_like_content_detected" in normalized["reasonCodes"]
            else "NO_SAFE_REPORT_CONTEXT_AVAILABLE"
        )
        return {
            "status": "stopped",
            "stopReason": stop_reason,
            "providerSdkRunCount": 0,
            "providerCommandCount": 0,
            "providerCallCount": 0,
            "artifactPath": None,
            "artifact": None,
            "reasonCodes": normalized["reasonCodes"],
        }
    prompt = build_codex_sdk_weekly_report_prompt(normalized["context"])
    runner = sdk_runner or _default_codex_sdk_runner
    try:
        raw_output = runner(prompt)
    except Exception as exc:  # noqa: BLE001 - do not store raw SDK logs or messages.
        stop_reason = _stop_reason_from_exception(exc)
        if stop_reason == "CODEX_SDK_SENTINEL_RUN_FAILED":
            stop_reason = "CODEX_SDK_WEEKLY_REPORT_RUN_FAILED"
        return {
            "status": "stopped",
            "stopReason": stop_reason,
            "providerSdkRunCount": 0,
            "providerCommandCount": 0,
            "providerCallCount": 0,
            "artifactPath": None,
            "artifact": None,
            "reasonCodes": [stop_reason],
            "errorClass": exc.__class__.__name__,
        }
    validation = validate_codex_sdk_weekly_report_output(raw_output, normalized["context"])
    if not validation["valid"]:
        stop_reason = (
            "SECRET_LIKE_CONTENT_DETECTED"
            if "secret_like_content_detected" in validation["reasonCodes"]
            else "CODEX_SDK_OUTPUT_NOT_VALID_WEEKLY_REPORT_JSON"
        )
        return {
            "status": "stopped",
            "stopReason": stop_reason,
            "providerSdkRunCount": 1,
            "providerCommandCount": 0,
            "providerCallCount": 1,
            "artifactPath": None,
            "artifact": None,
            "outputJsonValidation": {"valid": False, "reasonCodes": validation["reasonCodes"]},
            "reasonCodes": validation["reasonCodes"],
        }
    artifact = build_codex_sdk_weekly_report_output_class_proof_artifact(
        weekly_report_output=validation["weeklyReport"],
        report_context=normalized["context"],
        auth_strategy_used=auth_label,
        provider_auth_observed=auth_observed,
        artifact_ref=str(path_policy["pathForPlan"]),
        timestamp=timestamp,
    )
    artifact_validation = validate_codex_sdk_weekly_report_output_class_proof_artifact_contract(artifact)
    if not artifact_validation["valid"]:
        reasons = artifact_validation["reasonCodes"]
        stop_reason = (
            "SECRET_LIKE_CONTENT_DETECTED"
            if "secret_like_content_detected" in reasons or "proof_record_secret_like_content_detected" in reasons
            else "OUTPUT_CLASS_GATE_CROSS_UNLOCK_RISK"
            if "output_class_gate_mismatch" in reasons
            else "OUTPUT_CLASS_PROOF_ARTIFACT_INVALID"
        )
        return {
            "status": "stopped",
            "stopReason": stop_reason,
            "providerSdkRunCount": 1,
            "providerCommandCount": 0,
            "providerCallCount": 1,
            "artifactPath": None,
            "artifact": None,
            "outputJsonValidation": {"valid": True, "reasonCodes": []},
            "artifactValidation": artifact_validation,
            "reasonCodes": reasons,
        }
    if write_artifact:
        target = Path(str(path_policy["pathForPlan"]))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "status": "accepted_output_class_proof",
        "stopReason": None,
        "providerSdkRunCount": 1,
        "providerCommandCount": 0,
        "providerCallCount": 1,
        "authStrategyUsed": auth_label,
        "artifactPath": str(path_policy["pathForPlan"]),
        "artifact": artifact,
        "outputJsonValidation": {"valid": True, "reasonCodes": []},
        "artifactValidation": artifact_validation,
        "outputClassGateSummary": artifact["outputClassGateSummary"],
        "reasonCodes": [
            "provider_output_class_proof_accepted",
            "weekly_report_output_class_verified",
            "paper_review_not_unlocked_by_weekly_report",
        ],
    }


def describe_codex_sdk_auth_strategies(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    presence = _env_presence(env, AUTH_ENV_NAME_CANDIDATES)
    selected = _auth_label_from_presence(presence)
    return {
        "providerAuthScope": PROVIDER_AUTH_SCOPE,
        "providerManagedAuthRepresented": True,
        "providerManagedAuthVerified": False,
        "selectedAuthStrategyLabel": selected,
        "authStrategyIds": list(AUTH_STRATEGY_IDS),
        "envNames": list(AUTH_ENV_NAME_CANDIDATES),
        "envPresence": presence,
        "appOauthOidcSatisfied": False,
        "publicMcpConnectorProofSatisfied": False,
        "tinyProofSatisfied": False,
        "outputClassProofSatisfied": False,
        "noSecretInspection": True,
        "noTokenCacheInspection": True,
        "noCredentialPathDisclosure": True,
        "reasonCodes": [
            "provider_managed_auth_strategy_represented_unverified",
            "provider_auth_is_not_app_oauth_oidc",
            "public_mcp_connector_not_in_scope",
            "provider_auth_presence_cannot_unlock_output_class",
        ],
    }


def describe_codex_sdk_readiness(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    readiness = CodexSDKReadiness(
        config=CodexSDKProviderConfig(),
        dependency_status=describe_codex_sdk_dependency_status(),
        auth_strategy_status=describe_codex_sdk_auth_strategies(env),
        reason_codes=BASE_REASON_CODES,
    )
    return readiness.to_dict()


def build_codex_sdk_execution_plan(
    task_kind: str,
    output_class: str | None = None,
    *,
    live: bool = False,
) -> dict[str, Any]:
    reasons = [
        *BASE_REASON_CODES,
        "non_live_execution_plan_only",
        "provider_call_not_executed",
    ]
    if task_kind == "tiny_sentinel":
        reasons.extend(["tiny_proof_not_output_class_proof", "tiny_proof_cannot_unlock_output_class"])
    if live:
        reasons.append("future_live_call_requires_separate_owner_approval")
    if task_kind not in SUPPORTED_TASK_KINDS:
        reasons.append("unknown_task_kind")
    return CodexSDKExecutionPlan(
        task_kind=task_kind,
        output_class=output_class,
        live=live,
        reason_codes=tuple(reasons),
    ).to_dict()


class CodexSDKProvider:
    provider_id = PROVIDER_ID
    profile_id = PROFILE_ID
    provider_family_id = PROVIDER_FAMILY_ID
    execution_surface_type = EXECUTION_SURFACE_TYPE
    adapter_kind = ADAPTER_KIND

    def __init__(self, config: CodexSDKProviderConfig | None = None) -> None:
        self.config = config or CodexSDKProviderConfig()

    def is_configured(self) -> bool:
        return False

    def describe_status(self, env: Mapping[str, str] | None = None) -> dict[str, Any]:
        return describe_codex_sdk_readiness(env)

    def run_tiny_sentinel_proof(
        self,
        *,
        env: Mapping[str, str] | None = None,
        sdk_runner: CodexSDKRunner | None = None,
        artifact_path: str | None = None,
        write_artifact: bool = True,
    ) -> dict[str, Any]:
        return run_codex_sdk_tiny_sentinel_proof(
            env=env,
            sdk_runner=sdk_runner,
            artifact_path=artifact_path,
            write_artifact=write_artifact,
        )

    def run_paper_review_output_class_proof(
        self,
        *,
        evidence: Mapping[str, Any],
        env: Mapping[str, str] | None = None,
        sdk_runner: CodexSDKRunner | None = None,
        artifact_path: str | None = None,
        write_artifact: bool = True,
    ) -> dict[str, Any]:
        return run_codex_sdk_paper_review_output_class_proof(
            evidence=evidence,
            env=env,
            sdk_runner=sdk_runner,
            artifact_path=artifact_path,
            write_artifact=write_artifact,
        )

    def run_weekly_report_output_class_proof(
        self,
        *,
        report_context: Mapping[str, Any],
        env: Mapping[str, str] | None = None,
        sdk_runner: CodexSDKRunner | None = None,
        artifact_path: str | None = None,
        write_artifact: bool = True,
    ) -> dict[str, Any]:
        return run_codex_sdk_weekly_report_output_class_proof(
            report_context=report_context,
            env=env,
            sdk_runner=sdk_runner,
            artifact_path=artifact_path,
            write_artifact=write_artifact,
        )

    async def run_prototype_report_draft_once(
        self,
        *,
        prompt: str,
        output_schema: Mapping[str, Any],
        cwd: str,
        timeout_seconds: int,
        sdk_runner: PrototypeCodexSDKRunner | None = None,
    ) -> dict[str, Any]:
        return await run_codex_sdk_prototype_report_draft_once(
            prompt=prompt,
            output_schema=output_schema,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            sdk_runner=sdk_runner,
        )

    def generate_json(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise CodexSDKBlocked("Codex SDK provider scaffold is non-live and fail-closed.")
