from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.domain.review_input import enrich_packet, validate_packet
from app.repositories import ai_review_drafts, paper_reviews
from app.schemas.ai_review import AIReviewDraft, AIReviewPacket
from app.services import topic_profiles

PACKET_VERSION = "ai-review-packet-v2"
REQUIRED_DRAFT_FIELDS = {
    "paper_id",
    "contribution_summary",
    "method_summary",
    "evidence_summary",
    "relevance_to_topic",
    "novelty_signal",
    "practical_implications",
    "confidence",
    "review_level",
}


def _review_identity(review: dict[str, Any]) -> str:
    return str(review.get("paper_id") or "").strip().lower()


def _review_rank_key(review: dict[str, Any]) -> tuple[float, float, str, str]:
    return (
        float(review.get("relevance_score") or 0),
        float(review.get("evidence_strength") or 0),
        str(review.get("published_at") or ""),
        str(review.get("reviewed_at") or ""),
    )


def _dedupe_reviews(reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for review in reviews:
        key = _review_identity(review)
        if not key:
            continue
        existing = selected.get(key)
        if existing is None or _review_rank_key(review) > _review_rank_key(existing):
            selected[key] = review
    return list(selected.values())


def _span_id(span: dict[str, Any], index: int) -> str:
    for key in ("span_id", "id", "label"):
        value = str(span.get(key) or "").strip()
        if value:
            return value
    return f"span-{index}"


def _normalized_spans(review: dict[str, Any]) -> list[dict[str, Any]]:
    spans = review.get("evidence_spans") if isinstance(review.get("evidence_spans"), list) else []
    normalized: list[dict[str, Any]] = []
    for index, span in enumerate(spans, start=1):
        if not isinstance(span, dict):
            continue
        text = str(span.get("text") or "").strip()
        if not text:
            continue
        normalized.append(
            {
                "span_id": _span_id(span, index),
                "label": str(span.get("label") or "").strip() or None,
                "text": text,
                "source_path": str(span.get("source_path") or "").strip() or None,
                "page_number": span.get("page_number"),
                "char_start": span.get("char_start"),
                "char_end": span.get("char_end"),
            }
        )
    return normalized


def _bounded_excerpts(review: dict[str, Any], *, max_chars: int, review_level: str) -> list[dict[str, Any]]:
    remaining = max(1, int(max_chars))
    excerpts: list[dict[str, Any]] = []
    candidates = []
    if review_level == "full_text_grounded":
        candidates.append(("text_excerpt", str(review.get("text_excerpt") or "")))
        for span in _normalized_spans(review):
            candidates.append((f"evidence_span:{span['span_id']}", span["text"]))
    else:
        candidates.append(("abstract", str(review.get("abstract_text") or "")))
    for source, text in candidates:
        text = " ".join(str(text or "").split()).strip()
        if not text or remaining <= 0:
            continue
        used = text[:remaining]
        excerpts.append({"source": source, "text": used, "char_count": len(used)})
        remaining -= len(used)
    return excerpts


def _provider_output_contract(review_level: str) -> dict[str, Any]:
    return {
        "contract_version": "ai-review-provider-output-v1",
        "accepted_output_formats": ["json", "jsonl"],
        "validation_gate": "m4_grounded_validation",
        "required_output_fields": [
            "paper_id",
            "source_text_sha256",
            "contribution_summary",
            "method_summary",
            "evidence_summary",
            "limitations",
            "relevance_to_topic",
            "novelty_signal",
            "practical_implications",
            "cited_evidence_span_ids",
            "confidence",
            "review_level",
        ],
        "required_preserved_fields": ["paper_id", "source_text_sha256"],
        "full_text_grounded_requires": ["matching source_text_sha256", "at least one cited evidence span ID"],
        "review_level": review_level,
        "instructions": [
            "Return one AI review draft object as JSON or one JSON object per line.",
            "Preserve paper_id and source_text_sha256 exactly from the packet.",
            "Full-text grounded drafts must cite evidence span IDs from evidence_spans.",
            "Do not invent citations, papers, venues, evidence spans, hashes, or claims not supported by the packet.",
            "Surface unsupported claims in unsupported_claims instead of presenting them as grounded findings.",
        ],
    }


def _packet_from_review(
    review: dict[str, Any],
    *,
    topic_id: str,
    topic_name: str,
    max_chars: int,
) -> dict[str, Any] | None:
    status = str(review.get("full_text_status") or "unavailable")
    spans = _normalized_spans(review)
    source_hash = str(review.get("source_text_sha256") or "").strip()
    if status == "reviewed":
        if not source_hash or not spans:
            return None
        review_level = "full_text_grounded"
    elif status == "abstract_only":
        review_level = "abstract_only"
    else:
        return None
    payload = {
        "packet_version": PACKET_VERSION,
        "paper_id": str(review.get("paper_id") or "").strip(),
        "title": str(review.get("title") or "Untitled paper").strip() or "Untitled paper",
        "authors": [str(author) for author in review.get("authors") or [] if str(author).strip()],
        "published_at": review.get("published_at"),
        "topic_id": topic_id,
        "topic_name": topic_name,
        "relevance_score": float(review.get("relevance_score") or 0),
        "abstract": review.get("abstract_text"),
        "full_text_status": status,
        "review_level": review_level,
        "source_text_sha256": source_hash or None,
        "extraction_quality": {
            "text_char_count": int(review.get("text_char_count") or 0),
            "evidence_span_count": len(spans),
            "review_source_type": review.get("review_source_type") or "abstract",
            "full_text_artifact_path": review.get("full_text_artifact_path"),
        },
        "evidence_spans": spans if review_level == "full_text_grounded" else [],
        "selected_text_excerpts": _bounded_excerpts(review, max_chars=max_chars, review_level=review_level),
        "existing_review": {
            "method_summary": review.get("method_summary"),
            "key_findings": review.get("key_findings") or [],
            "limitations": review.get("limitations") or [],
            "why_it_matters": review.get("why_it_matters"),
            "topic_fit_rationale": review.get("topic_fit_rationale"),
            "review_model": review.get("review_model"),
        },
        "citation_metadata": {
            "source_provider": review.get("source_provider"),
            "full_text_artifact_path": review.get("full_text_artifact_path"),
        },
        "provider_output_contract": _provider_output_contract(review_level),
    }
    return enrich_packet(review, AIReviewPacket.model_validate(payload).model_dump())


def build_ai_review_packets(
    topic_id: str,
    *,
    limit: int = 10,
    max_chars: int = 12000,
    include_abstract_only: bool = False,
) -> dict[str, Any]:
    profile = topic_profiles.get_topic_profile(str(topic_id or "").strip())
    resolved_topic_id = str(profile.get("profile_id") or topic_id).strip()
    topic_name = str(profile.get("title") or profile.get("topic") or resolved_topic_id)
    reviews = _dedupe_reviews(paper_reviews.list_reviews(topic_id=resolved_topic_id, limit=100_000))
    reviews.sort(
        key=lambda review: (
            float(review.get("relevance_score") or 0),
            float(review.get("evidence_strength") or 0),
            str(review.get("published_at") or ""),
            str(review.get("paper_id") or ""),
        ),
        reverse=True,
    )
    packets: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for review in reviews:
        status = str(review.get("full_text_status") or "unavailable")
        if status == "abstract_only" and not include_abstract_only:
            skipped.append({"paper_id": review.get("paper_id"), "reason": "abstract_only_excluded"})
            continue
        packet = _packet_from_review(review, topic_id=resolved_topic_id, topic_name=topic_name, max_chars=max_chars)
        if packet is None:
            skipped.append({"paper_id": review.get("paper_id"), "reason": "not_packet_eligible"})
            continue
        packets.append(packet)
        if len(packets) >= max(0, int(limit)):
            break
    return {
        "topic_id": resolved_topic_id,
        "packet_version": PACKET_VERSION,
        "packet_count": len(packets),
        "full_text_grounded_packet_count": sum(1 for packet in packets if packet["review_level"] == "full_text_grounded"),
        "abstract_only_packet_count": sum(1 for packet in packets if packet["review_level"] == "abstract_only"),
        "max_chars": int(max_chars),
        "packets": packets,
        "skipped": skipped,
    }


def validate_ai_review_draft(packet: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
    reason_codes: list[str] = []
    warnings: list[str] = []
    if not isinstance(draft, dict):
        return {
            "accepted": False,
            "status": "rejected",
            "reason_codes": ["malformed_json"],
            "validation_warnings": [],
            "normalized_draft": None,
        }
    missing = sorted(field for field in REQUIRED_DRAFT_FIELDS if field not in draft or draft.get(field) in (None, "", []))
    if missing:
        reason_codes.append("missing_required_field")
        warnings.append(f"missing fields: {', '.join(missing)}")
    try:
        normalized = AIReviewDraft.model_validate(draft).model_dump()
    except ValidationError as exc:
        normalized = None
        if "missing_required_field" not in reason_codes:
            reason_codes.append("malformed_json")
        warnings.append(str(exc))

    if normalized is not None:
        if normalized["paper_id"] != packet.get("paper_id"):
            reason_codes.append("paper_id_mismatch")
        if normalized.get("source_text_sha256") != packet.get("source_text_sha256"):
            reason_codes.append("source_text_sha256_mismatch")
        if not 0 <= float(normalized.get("confidence") or 0) <= 1:
            reason_codes.append("confidence_out_of_range")
        packet_level = str(packet.get("review_level") or "")
        draft_level = str(normalized.get("review_level") or "")
        if draft_level == "full_text_grounded":
            if packet_level != "full_text_grounded":
                reason_codes.append("abstract_only_full_text_claim")
            cited = [str(value) for value in normalized.get("cited_evidence_span_ids") or [] if str(value).strip()]
            if not cited:
                reason_codes.append("missing_cited_evidence_span_ids")
            packet_span_ids = {str(span.get("span_id")) for span in packet.get("evidence_spans") or [] if span.get("span_id")}
            unknown = sorted(span_id for span_id in cited if span_id not in packet_span_ids)
            if unknown:
                reason_codes.append("unknown_cited_evidence_span_id")
                warnings.append(f"unknown cited evidence span IDs: {', '.join(unknown)}")
        elif draft_level == "abstract_only":
            if packet_level == "full_text_grounded" and normalized.get("cited_evidence_span_ids"):
                warnings.append("abstract-only draft includes evidence span IDs; spans will not be treated as grounded claims")
        else:
            reason_codes.append("invalid_review_level")
        if normalized.get("unsupported_claims"):
            reason_codes.append("unsupported_claims_present")

    hard_failures = {
        "missing_required_field",
        "malformed_json",
        "paper_id_mismatch",
        "source_text_sha256_mismatch",
        "confidence_out_of_range",
        "abstract_only_full_text_claim",
        "missing_cited_evidence_span_ids",
        "unknown_cited_evidence_span_id",
        "invalid_review_level",
    }
    if any(reason in hard_failures for reason in reason_codes):
        status = "rejected"
    elif "unsupported_claims_present" in reason_codes:
        status = "needs_review"
    else:
        status = "accepted"
    return {
        "accepted": status == "accepted",
        "status": status,
        "reason_codes": sorted(set(reason_codes)),
        "validation_warnings": warnings,
        "normalized_draft": normalized if status in {"accepted", "needs_review"} else None,
    }


def generate_fixture_ai_review_draft(packet: dict[str, Any], *, unsupported_claim: bool = False) -> dict[str, Any]:
    span_ids = [str(span.get("span_id")) for span in packet.get("evidence_spans") or [] if span.get("span_id")]
    review_level = str(packet.get("review_level") or "abstract_only")
    return {
        "paper_id": packet["paper_id"],
        "source_text_sha256": packet.get("source_text_sha256"),
        "contribution_summary": f"Fixture review for {packet['title']} summarizes the validated contribution.",
        "method_summary": "Fixture method summary based on provided packet evidence.",
        "evidence_summary": "Fixture evidence summary cites bounded packet evidence spans." if span_ids else "Fixture abstract-only evidence summary.",
        "limitations": ["Fixture draft remains deterministic and requires human review before publication."],
        "relevance_to_topic": f"Relevant to {packet.get('topic_name') or packet.get('topic_id')}.",
        "novelty_signal": "moderate",
        "practical_implications": "Useful for evidence-backed weekly synthesis.",
        "follow_up_questions": ["Which evaluation setting is most relevant next week?"],
        "recommended_tags": ["LLM", "RAG"],
        "cited_evidence_span_ids": span_ids[:2] if review_level == "full_text_grounded" else [],
        "confidence": 0.78,
        "unsupported_claims": ["unsupported fixture claim"] if unsupported_claim else [],
        "review_level": review_level,
    }


def write_ai_review_packets(
    topic_id: str,
    output_path: str | Path,
    *,
    limit: int,
    max_chars: int,
    dry_run: bool = False,
) -> dict[str, Any]:
    payload = build_ai_review_packets(topic_id, limit=limit, max_chars=max_chars)
    path = Path(output_path)
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "mode": "ai_review_packet_export",
        "topic_id": payload["topic_id"],
        "dry_run": bool(dry_run),
        "output_path": str(path),
        "written": not dry_run,
        "packet_summary": {key: value for key, value in payload.items() if key != "packets"},
    }


def _drafts_from_path(path: str | Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        drafts = payload.get("drafts")
        if isinstance(drafts, list):
            return [item for item in drafts if isinstance(item, dict)]
    return []


def validate_and_import_ai_review_drafts(
    topic_id: str,
    draft_path: str | Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    packet_payload = build_ai_review_packets(topic_id, limit=100_000, max_chars=24000, include_abstract_only=True)
    packet_by_paper = {packet["paper_id"]: packet for packet in packet_payload["packets"]}
    results: list[dict[str, Any]] = []
    accepted = rejected = needs_review = stored = 0
    for draft in _drafts_from_path(draft_path):
        packet = packet_by_paper.get(str(draft.get("paper_id") or ""))
        if packet is None:
            validation = {
                "accepted": False,
                "status": "rejected",
                "reason_codes": ["packet_not_found"],
                "validation_warnings": [],
                "normalized_draft": None,
            }
        else:
            validation = validate_ai_review_draft(packet, draft)
        status = validation["status"]
        accepted += 1 if status == "accepted" else 0
        rejected += 1 if status == "rejected" else 0
        needs_review += 1 if status == "needs_review" else 0
        stored_record = None
        if status == "accepted" and not dry_run:
            if packet is None:
                raise ValueError("packet_not_found")
            normalized = validation["normalized_draft"] or {}
            # Manual import rebuilds a packet from current local evidence; it cannot
            # attest that an external author or model actually received this input.
            input_record: dict[str, Any] = (
                {"input_packet": packet, "input_origin": "MANUAL_IMPORT_UNVERIFIED"}
                if packet.get("packet_version") == PACKET_VERSION
                else {}
            )
            stored_record = ai_review_drafts.upsert_draft(
                {
                    "topic_id": packet["topic_id"],
                    "run_id": _run_id_for_packet(packet, topic_id),
                    "paper_id": packet["paper_id"],
                    "source_text_sha256": str(normalized.get("source_text_sha256") or ""),
                    "review_level": str(normalized.get("review_level") or "abstract_only"),
                    "status": "accepted",
                    "draft": normalized,
                    "validation": validation,
                },
                **input_record,
            )
            stored += 1
        results.append(
            {
                "paper_id": draft.get("paper_id"),
                "status": status,
                "reason_codes": validation.get("reason_codes", []),
                "stored": stored_record is not None,
            }
        )
    return {
        "mode": "ai_review_draft_import",
        "topic_id": packet_payload["topic_id"],
        "dry_run": bool(dry_run),
        "input_path": str(draft_path),
        "draft_count": len(results),
        "accepted_draft_count": accepted,
        "rejected_draft_count": rejected,
        "needs_review_count": needs_review,
        "stored_draft_count": stored,
        "results": results,
    }


def _run_id_for_packet(packet: dict[str, Any], topic_id: str) -> str:
    if packet.get("packet_version") in {PACKET_VERSION, "ai-review-packet-v3", "ai-review-packet-v4", "ai-review-packet-v5"}:
        return str(validate_packet(packet)["source"]["run_id"])
    existing_run = packet.get("existing_review", {}).get("run_id")
    if existing_run:
        return str(existing_run)
    reviews = paper_reviews.list_reviews(topic_id=topic_id, paper_ids=[packet["paper_id"]], limit=1)
    return str(reviews[0].get("run_id") or "") if reviews else ""
