"""Plan all preserved text by model budget, then prepare explicit units locally.

Section headings are heuristics over normalized text, not a scientific outline.
Every target code point belongs to exactly one unit. Neighbor text is context,
not additional reviewed coverage. No model, tokenizer API or credential IO here.
"""
from contextlib import closing
from copy import deepcopy
from datetime import datetime
import re
from typing import Any

from app.auth.resource_access import ResourceActor
from app.db.bootstrap import connect_metadata_index
from app.domain import prepared_review, review_document_plan as domain, review_preparation as prep
from app.domain.review_context import capture
from app.domain.review_range_input import build_packet
from app.domain.review_token_budget import estimate_input_tokens
from app.repositories import ai_review_drafts, review_document_plans as repository, review_preparations
from app.repositories.files import iso_now_utc
from app.services import model_connections, review_input_history, review_preparation

# Exact model IDs only. Unknown aliases require an explicitly entered budget.
# Official pages checked 2026-10-18; this is capability metadata, not live access.
MODEL_CONTEXTS = {
    ("anthropic", "claude-haiku-5-5"): 1_000_000,
    ("anthropic", "claude-opus-5-5"): 1_000_000,
    ("anthropic", "claude-sonnet-5-5"): 1_000_000,
}
DOCS = "https://platform.claude.com/docs/en/models/haiku-5-5/overview"
# Frozen V1 identities: changing the current connector catalog cannot rewrite a
# saved plan. A new connector identity requires an explicit future plan version.
CONNECTOR_PROVIDERS_V1 = dict(codex_subscription="codex_cli", claude_subscription="claude_code",
    antigravity_subscription="antigravity_cli", grok_subscription="grok_cli", openai_api="openai",
    xai_api="xai", anthropic_api="anthropic")
MAX_UNITS = 256
_HEADING = re.compile(r"(?<!\S)([1-9][0-9]?(?:\.[0-9]+){0,3}\.?)\s+(Abstract|Introduction|Related\s+Work|Background|Methods?|Methodology|Approach|Experiments?|Results?(?:\s+and\s+Discussion)?|Discussion|Conclusions?|Limitations|References|Appendix)\b", re.I)


def identity(topic: str, version: str, actor: str, request: str) -> str:
    return "rplan_" + prep.sha(prep.encoded([domain.SCHEMA, topic, version, actor, request]))


def sections(text: str) -> list[dict[str, Any]]:
    headings = [(0, "본문 시작")]
    for match in _HEADING.finditer(text):
        if match.start() == 0:
            headings[0] = (0, match.group())
        else:
            headings.append((match.start(), match.group()))
    if len(headings) > MAX_UNITS:
        raise ValueError("document_plan_section_limit_exceeded")
    return [dict(label=label, start=start, end=headings[i + 1][0] if i + 1 < len(headings) else len(text))
        for i, (start, label) in enumerate(headings)]


def configuration(topic: str, version: str, actor: ResourceActor) -> dict[str, Any]:
    review_preparation._occurrences(topic, version, actor)
    state = model_connections.read()
    choices = []
    for item in model_connections.CONNECTORS:
        model = state["models"].get(item.connector_id, "")
        context = MODEL_CONTEXTS.get((item.provider_name, model))
        choices.append(dict(connector_id=item.connector_id, display_name=item.display_name, provider_name=item.provider_name,
            model=model, context_tokens=context, budget_source="DOCUMENTED_MODEL_2026_10_18" if context else "USER_REQUIRED",
            docs_url=DOCS if context else item.docs_url))
    return dict(choices=choices, default_connector_id=state["default_connector_id"], estimator=domain.ESTIMATOR,
        max_output_tokens=2200, safety_tokens=8192, execution_started=False)


def _body(plan: dict[str, Any], unit: dict[str, Any], index: int, count: int, digest: str) -> prep.DocumentPrepare:
    text = plan["text"]
    metadata = dict(schema_version="PAPER_ANALYSIS_UNIT_V1", plan_id=plan["plan_id"], plan_sha256=digest,
        unit_index=index, unit_count=count, label=unit["label"], document_chars=len(text), outline=plan["outline"],
        context_before=text[max(0, unit["start"] - 256):unit["start"]],
        context_after=text[unit["end"]:unit["end"] + 256], budget=plan["budget"])
    return prep.DocumentPrepare(request_id=f'{plan["plan_id"]}:{index}', run_id=plan["source"]["run_id"],
        paper_id=plan["source"]["paper_id"], expected_source_digest=plan["source"]["source_digest"],
        start=unit["start"], end=unit["end"], analysis_unit=metadata)


def _record(plan: dict[str, Any], unit: dict[str, Any], index: int, count: int, digest: str) -> dict[str, Any]:
    return prep.build(plan["topic_id"], plan["paper_version_id"], plan["actor_id"], _body(plan, unit, index, count, digest),
        plan["source"], plan["text"], plan["created_at"], context=plan["context"])


def _estimate(plan: dict[str, Any], unit: dict[str, Any], index=255, count=256, digest="0" * 64) -> int:
    budget = plan["budget"]
    contract = prepared_review.prepare(build_packet(_record(plan, unit, index, count, digest)),
        provider_name=budget["provider_name"], model=budget["model"], max_output_tokens=budget["max_output_tokens"])
    if budget["provider_name"] == "anthropic":
        # Reuse the exact V2 wire builder, including nested JSON escaping and
        # system-schema duplication. This only serializes; it reads no key.
        from app.services.providers import anthropic_prepared_review as adapter
        try:
            adapter.build_plan(contract, timeout_seconds=60, profile=adapter.MANIFEST_SCHEMA_V2)
        except ValueError as exc:
            if str(exc) == "paper_anthropic_configuration_invalid":
                raise ValueError("document_plan_wire_limit") from None
            raise
    return estimate_input_tokens(contract["provider_request"])


def _fits(plan: dict[str, Any], unit: dict[str, Any]) -> bool:
    try:
        return _estimate(plan, unit) + 64 <= plan["budget"]["input_tokens"]
    except ValueError as exc:
        if str(exc) not in {"prepared_review_request_invalid", "document_plan_wire_limit"}:
            raise
        return False


def _split(plan: dict[str, Any]) -> list[dict[str, Any]]:
    whole = dict(start=0, end=len(plan["text"]), label="전체 추출 본문")
    if _fits(plan, whole):
        return [whole]
    units = []
    text = plan["text"]
    for section in plan["outline"]:
        start = section["start"]
        while start < section["end"]:
            unit = dict(start=start, end=section["end"], label=section["label"])
            if not _fits(plan, unit):
                low, high = start, section["end"]
                while low < high:
                    mid = (low + high + 1) // 2
                    if _fits(plan, {**unit, "end": mid}):
                        low = mid
                    else:
                        high = mid - 1
                if low == start:
                    raise ValueError("document_plan_budget_too_small")
                # Prefer sentence/word boundaries, retaining the delimiter in the
                # target. Oversize words fall back to code points without loss.
                window = text[max(start, low - 800):low]
                sentence = max(window.rfind(". "), window.rfind("。"), window.rfind("? "), window.rfind("! "))
                boundary = sentence + 1 if sentence >= 0 else window.rfind(" ") + 1
                unit["end"] = max(start, low - 800) + boundary if boundary > 0 else low
                # Changing the following context can alter JSON byte usage.
                if not _fits(plan, unit):
                    unit["end"] = low
            units.append(unit)
            if len(units) > MAX_UNITS:
                raise ValueError("document_plan_unit_limit_exceeded")
            start = unit["end"]
    return units


def _new(topic: str, version: str, actor: str, body: domain.Plan, profile: dict[str, Any], paper: dict[str, Any]) -> dict[str, Any]:
    connector = model_connections.BY_ID.get(body.connector_id)
    if connector is None or not connector.adapter_available or body.connector_id not in CONNECTOR_PROVIDERS_V1:
        raise ValueError("document_plan_connector_invalid")
    documented = MODEL_CONTEXTS.get((connector.provider_name, body.model))
    if documented and body.context_tokens > documented:
        raise ValueError("document_plan_budget_invalid")
    source, text = review_preparation._observe_selected(topic, version, body.run_id, body.paper_id, body.expected_source_digest)
    if source["source_digest"] != body.expected_source_digest:
        raise ValueError("review_preparation_source_changed")
    plan: dict[str, Any] = dict(schema_version=domain.SCHEMA, plan_id=identity(topic, version, actor, body.request_id),
        topic_id=topic, paper_version_id=version, actor_id=actor, created_at=iso_now_utc(), request=body.model_dump(),
        source=source, text=text, text_sha256=prep.sha(text), context=capture(profile, paper),
        budget=domain.budget(body, connector.provider_name),
        budget_source="DOCUMENTED_MODEL_2026_10_18" if documented else "USER_CONFIGURED_NOT_VERIFIED",
        model_capability=dict(documented_context_tokens=documented, observed_at="2026-10-18" if documented else None,
            docs_url=DOCS if documented else None, live_access="NOT_VERIFIED"),
        outline=sections(text), section_detection="NUMBERED_HEADING_HEURISTIC_V1",
        full_document_review="NOT_ESTABLISHED", scientific_support="NOT_ASSESSED")
    plan["units"] = _split(plan)
    for index, unit in enumerate(plan["units"]):
        unit["index"] = index
        unit["estimated_input_tokens"] = _estimate(plan, unit, index, len(plan["units"]))
    plan["mode"] = "WHOLE_EXTRACTED_TEXT" if len(plan["units"]) == 1 else "SECTION_UNITS"
    plan["plan_sha256"] = prep.sha(prep.encoded(plan))
    return validate(plan)


def validate(value: dict[str, Any]) -> dict[str, Any]:
    try:
        if set(value) != {"schema_version", "plan_id", "topic_id", "paper_version_id", "actor_id", "created_at", "request",
                "source", "text", "text_sha256", "context", "budget", "budget_source", "model_capability", "outline", "section_detection",
                "full_document_review", "scientific_support", "units", "mode", "plan_sha256"}:
            raise ValueError()
        body = domain.Plan.model_validate(value["request"])
        capability = value["model_capability"]
        if (not isinstance(capability, dict) or set(capability) != {"documented_context_tokens", "observed_at", "docs_url", "live_access"}
                or capability["live_access"] != "NOT_VERIFIED"):
            raise ValueError()
        documented = capability["documented_context_tokens"]
        if documented is None:
            if capability["observed_at"] is not None or capability["docs_url"] is not None:
                raise ValueError()
        elif (type(documented) is not int or not 8192 <= documented <= 2_000_000
                or capability["observed_at"] != "2026-10-18" or capability["docs_url"] != DOCS):
            raise ValueError()
        if (value["schema_version"] != domain.SCHEMA
                or value["plan_id"] != identity(value["topic_id"], value["paper_version_id"], value["actor_id"], body.request_id)
                or datetime.fromisoformat(value["created_at"]).tzinfo is None
                or value["text_sha256"] != prep.sha(value["text"]) or len(value["text"]) != value["source"]["source_chars"]
                or value["plan_sha256"] != prep.sha(prep.encoded({k: v for k, v in value.items() if k != "plan_sha256"}))
                or value["full_document_review"] != "NOT_ESTABLISHED" or value["scientific_support"] != "NOT_ASSESSED"
                or value["section_detection"] != "NUMBERED_HEADING_HEURISTIC_V1" or value["outline"] != sections(value["text"])
                or not 1 <= len(value["units"]) <= MAX_UNITS
                or value["mode"] != ("WHOLE_EXTRACTED_TEXT" if len(value["units"]) == 1 else "SECTION_UNITS")
                or value["budget"] != domain.budget(body, value["budget"]["provider_name"])
                or CONNECTOR_PROVIDERS_V1.get(body.connector_id) != value["budget"]["provider_name"]
                or value["budget_source"] != ("DOCUMENTED_MODEL_2026_10_18" if documented else "USER_CONFIGURED_NOT_VERIFIED")
                or (documented and body.context_tokens > documented)
                or value["source"]["run_id"] != body.run_id or value["source"]["paper_id"] != body.paper_id
                or value["source"]["source_digest"] != body.expected_source_digest):
            raise ValueError()
        cursor = 0
        for i, unit in enumerate(value["units"]):
            if (set(unit) != {"start", "end", "label", "index", "estimated_input_tokens"}
                    or type(unit["index"]) is not int or unit["index"] != i or unit["start"] != cursor
                    or not cursor < unit["end"] <= len(value["text"])
                    or unit["estimated_input_tokens"] != _estimate(value, unit, i, len(value["units"]), value["plan_sha256"])):
                raise ValueError()
            cursor = unit["end"]
        if cursor != len(value["text"]):
            raise ValueError()
        return deepcopy(value)
    except (TypeError, ValueError, KeyError, AttributeError, RecursionError):
        raise ValueError("document_plan_integrity_invalid") from None


def _actor(actor: ResourceActor) -> str:
    return actor.subject or actor.actor_type


def create(topic: str, version: str, body: domain.Plan, actor: ResourceActor) -> dict[str, Any]:
    profile, paper = review_input_history.visible_paper(topic, version, actor)
    if (body.run_id, body.paper_id) not in paper["_reviewOccurrenceKeys"]:
        raise KeyError("document_plan_not_found")
    plan = repository.save(topic, version, _actor(actor), body, lambda: _new(topic, version, _actor(actor), body, profile, paper))
    return summary(plan, review_preparation._occurrences(topic, version, actor))


def _load(topic: str, version: str, identity: str, actor: ResourceActor) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    occurrences = review_preparation._occurrences(topic, version, actor)
    plan = repository.detail(topic, version, _actor(actor), identity)
    if (plan["source"]["run_id"], plan["source"]["paper_id"]) not in occurrences:
        raise KeyError("document_plan_not_found")
    return plan, occurrences


def detail(topic: str, version: str, identity: str, actor: ResourceActor) -> dict[str, Any]:
    return summary(*_load(topic, version, identity, actor))


def listing(topic: str, version: str, actor: ResourceActor, page: int) -> dict[str, Any]:
    occurrences = review_preparation._occurrences(topic, version, actor)
    value = repository.listing(topic, version, _actor(actor), page)
    value["items"] = [summary(p, occurrences) for p in value["items"]
        if (p["source"]["run_id"], p["source"]["paper_id"]) in occurrences]
    return value


def prepare_unit(topic: str, version: str, identity: str, index: int, actor: ResourceActor) -> dict[str, Any]:
    plan, occurrences = _load(topic, version, identity, actor)
    if type(index) is not int or not 0 <= index < len(plan["units"]):
        raise KeyError("document_plan_unit_not_found")
    body = _body(plan, plan["units"][index], index, len(plan["units"]), plan["plan_sha256"])
    return review_preparations.save(topic, version, _actor(actor), body, occurrences,
        lambda: (plan["source"], plan["text"]), context_factory=lambda: plan["context"])


def summary(plan: dict[str, Any], occurrences: list[tuple[str, str]]) -> dict[str, Any]:
    public = {k: deepcopy(v) for k, v in plan.items() if k not in {"text", "context", "request"}}
    public["request_id"] = plan["request"]["request_id"]
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        for index, unit in enumerate(public["units"]):
            body = _body(plan, unit, index, len(plan["units"]), plan["plan_sha256"])
            pid = prep.identity(plan["topic_id"], plan["paper_version_id"], plan["actor_id"], body.request_id)
            unit.update(preparation_id=None, expected_preparation_id=pid, receipts=[], state="PLANNED_NOT_EXECUTED")
            try:
                saved = review_preparations.detail_in(c, plan["topic_id"], plan["paper_version_id"], pid, occurrences)
            except KeyError:
                continue
            if (saved["selection"]["text"] != plan["text"][unit["start"]:unit["end"]]
                    or saved.get("analysis_unit") != body.analysis_unit):
                raise ValueError("document_plan_integrity_invalid")
            unit.update(preparation_id=pid, state="PREPARED_NOT_EXECUTED")
            if ai_review_drafts._history_exists(c):
                rows = c.execute("SELECT * FROM ai_review_input_history WHERE topic_id=? AND json_extract(packet_json,'$.source_preparation.preparation_id')=? ORDER BY recorded_at,receipt_id LIMIT 201",
                    (plan["topic_id"], pid)).fetchall()
                if len(rows) > 200:
                    raise ValueError("document_plan_receipt_limit_exceeded")
                for row in rows:
                    receipt = ai_review_drafts._row_to_receipt(row)
                    if receipt["packet"] != build_packet(saved):
                        raise ValueError("document_plan_integrity_invalid")
                    validated = "prepared_execution" in receipt["validation"]
                    unit["receipts"].append(dict(receipt_id=receipt["receipt_id"], input_origin=receipt["input_origin"],
                        validated_response=validated, status=receipt["status"]))
                if unit["receipts"]:
                    unit["state"] = "SAVED_INPUT_OR_RESULT"
    return public
