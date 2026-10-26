"""Offline comparison preparation, never a grant or live Provider experiment."""
from copy import deepcopy
import importlib
import json

import httpx
import pytest

from app.domain import prepared_review
from app.domain.journal_exchange import preserved_json, sha
from app.services.providers import anthropic_messages as messages
from app.services.providers.base import ProviderOutputError
from tests.test_anthropic_messages import envelope
from tests.test_anthropic_prepared_review import contract
from tests.test_prepared_review import response


def module():
    return importlib.import_module("app.services.anthropic_output_comparison")


def test_plain_text_capture_preserves_unicode_but_json_adapter_stays_strict():
    text = " e\u0301 한글🙂 source observation, not a complete paper review. \n"
    raw = preserved_json(envelope(text)).encode("utf-8")
    parsed = messages.parse_text_response(raw)
    assert parsed.text == text and not hasattr(parsed, "payload")
    assert parsed.model == "reported-offline-model" and parsed.usage["input_tokens"] == 5
    with pytest.raises(ProviderOutputError):
        messages.parse_response(raw)


def test_only_output_format_differs_and_schema_is_available_to_both(monkeypatch):
    def no_http(*args, **kwargs):
        raise AssertionError("offline preparation cannot open an HTTP client")
    monkeypatch.setattr(httpx, "Client", no_http)
    frozen = contract(model="claude-haiku-5-5")
    before = deepcopy(frozen)
    plan = module().prepare(frozen)
    text = plan["arms"]["text"]["body"]
    structured = plan["arms"]["structured"]["body"]
    assert frozen == before
    assert text == {key: value for key, value in structured.items() if key != "output_config"}
    assert text["messages"] == [{"role": "user", "content": frozen["provider_request"]["prompt"]}]
    assert text["system"].startswith(frozen["provider_request"]["instructions"] + "\n")
    assert preserved_json(frozen["provider_request"]["schema"]) in text["system"]
    assert " e\u0301 한글🙂 tail " in preserved_json(text)
    assert text["model"] == "claude-haiku-5-5" and text["max_tokens"] == 2200
    assert text["tools"] == [] and text["stream"] is False
    assert not {"thinking", "temperature", "metadata", "output_config"} & text.keys()
    assert plan["state"] == "PREPARED_NOT_AUTHORIZED"
    assert plan["authority"] == "NOT_ESTABLISHED"
    assert plan["policy"]["automatic_retry"] is False
    assert plan["policy"]["http_timeouts"] == {"connect": 10, "read": 300, "write": 30, "pool": 10}
    assert plan["policy"]["proposed_max_attempts"] == {"text": 1, "structured": 1}
    assert module().validate_plan(plan) == plan
    for arm in plan["arms"].values():
        assert arm["body_sha256"] == sha(preserved_json(arm["body"]))
        assert arm["body_bytes"] == len(preserved_json(arm["body"]).encode("utf-8"))


@pytest.mark.parametrize("change", ["model", "prompt", "schema", "timeout", "authority", "retry", "extra"])
def test_rehashed_plan_changes_do_not_become_valid(change):
    plan = module().prepare(contract())
    if change in {"model", "prompt", "schema"}:
        body = plan["arms"]["text"]["body"]
        body[{"model": "model", "prompt": "system", "schema": "output_config"}[change]] = "changed"
    elif change == "timeout":
        plan["policy"]["http_timeouts"]["read"] = 600
    elif change == "authority":
        plan["authority"] = "APPROVED"
    elif change == "retry":
        plan["policy"]["automatic_retry"] = True
    else:
        plan["unexpected"] = True
    plan["plan_sha256"] = sha(preserved_json({key: value for key, value in plan.items() if key != "plan_sha256"}))
    with pytest.raises(ValueError, match="comparison_plan_invalid"):
        module().validate_plan(plan)


def test_tampered_frozen_contract_is_rejected():
    frozen = contract()
    frozen["provider_request"]["instructions"] = "changed"
    with pytest.raises(ValueError):
        module().prepare(frozen)


@pytest.mark.parametrize("arm", ["text", "structured"])
@pytest.mark.parametrize("kind,expected", [("text", "REVIEW_JSON_INVALID"), ("valid", "CONTRACT_VALID"),
    ("binding", "CONTRACT_INVALID"), ("refusal", "NOT_EVALUATED"), ("incomplete", "NOT_EVALUATED"),
    ("http", "NOT_EVALUATED"), ("malformed", "NOT_EVALUATED")])
def test_capture_assessment_keeps_transport_text_contract_and_authority_separate(arm, kind, expected):
    frozen = contract()
    plan = module().prepare(frozen)
    draft = response(frozen).payload
    if kind == "binding":
        draft.update(paper_id="x", source_text_sha256="x", cited_evidence_span_ids=[""])
    body = envelope("Some ordinary text." if kind == "text" else preserved_json(draft))
    if kind in {"refusal", "incomplete"}:
        body["stop_reason"] = "refusal" if kind == "refusal" else "max_tokens"
    raw = b"not a response" if kind == "malformed" else preserved_json(body).encode("utf-8")
    assessment = module().assess_capture(plan, arm=arm, http_status=400 if kind == "http" else 200, raw=raw,
        capture_origin="SYNTHETIC")
    assert assessment["review_status"] == expected
    assert assessment["raw_response_sha256"] == sha(raw.decode("utf-8"))
    assert assessment["planned_request_body_sha256"] == plan["arms"][arm]["body_sha256"]
    assert assessment["request_response_binding"] == "NOT_VERIFIED_OFFLINE_CAPTURE"
    assert assessment["scientific_support"] == "NOT_ASSESSED" and assessment["human_adoption"] == "NOT_ESTABLISHED"
    if kind in {"text", "valid", "binding"}:
        assert assessment["transport_status"] == "FINAL_TEXT_CAPTURED"
        assert assessment["reported_model"] == "reported-offline-model"
        assert assessment["usage"]["output_tokens"] == 4
        assert assessment["final_text"] == body["content"][0]["text"]
    if kind == "binding":
        assert assessment["reason_codes"] == ["PAPER_ID_MISMATCH", "SOURCE_HASH_MISMATCH", "MISSING_CITATIONS"]
    if kind == "refusal":
        assert assessment["transport_status"] == "REFUSED"
    if kind == "incomplete":
        assert assessment["transport_status"] == "INCOMPLETE"


def test_export_is_inspectable_fresh_only_and_has_no_grant(tmp_path):
    frozen = contract()
    plan = module().prepare(frozen)
    output = tmp_path / "prepared"
    module().write_bundle(plan, output)
    assert json.loads((output / "plan.json").read_bytes()) == plan
    assert json.loads((output / "contract.json").read_bytes()) == frozen
    for arm in ("text", "structured"):
        wire = (output / f"{arm}-request.json").read_bytes()
        assert wire == preserved_json(plan["arms"][arm]["body"]).encode("utf-8")
    preview = (output / "PREVIEW.md").read_text(encoding="utf-8")
    assert "PREPARED_NOT_AUTHORIZED" in preview and "NOT_RUN" in preview
    assert frozen["model"] in preview and plan["plan_sha256"] in preview
    before = {file.name: file.read_bytes() for file in output.iterdir()}
    with pytest.raises(FileExistsError):
        module().write_bundle(plan, output)
    assert {file.name: file.read_bytes() for file in output.iterdir()} == before


def test_plan_is_not_accepted_as_existing_contract_or_trial_grant():
    plan = module().prepare(contract())
    with pytest.raises(ValueError):
        prepared_review.validate_contract(plan)
    from app.services.local_anthropic_trial import Grant
    with pytest.raises(ValueError):
        Grant.model_validate(plan)
