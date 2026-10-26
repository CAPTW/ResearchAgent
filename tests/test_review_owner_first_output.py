"""First-output authority is a separate synthetic decision, never a fabricated proof."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest

from app.auth.resource_access import ResourceActor
from app.domain import review_authority as authority, review_execution
from app.domain.review_preparation import encoded, sha
from app.services.review_approval import PaperApprovalRuntime
from app.services.review_dispatch import PaperDispatchRuntime
from app.services.providers import anthropic_prepared_review as adapter
from tests import test_anthropic_prepared_review_v2 as plans, test_review_authority as legacy
from tests import test_review_anthropic_native as native

range_source = native.range_source
seeded = native.seeded
native_case = native.native_case
DECISION = "d" * 64
NOW = legacy.NOW


def contract():
    value = plans.execution(surface="NATIVE_HTTP")
    value["transport"] = adapter.build_plan(value["prepared"], timeout_seconds=60,
        profile=adapter.MANIFEST_SCHEMA_V2, execution_surface="NATIVE_HTTP", credential_revision=7)[2]
    return value


def first_record(value, *, now=NOW, decision=DECISION):
    manifest = value["transport"]
    return dict(schema_version="PAPER_REVIEW_OWNER_FIRST_OUTPUT_AUTHORITY_RECORD_V1",
        grant=dict(schema_version="PAPER_REVIEW_OWNER_FIRST_OUTPUT_GRANT_V1", authorization_kind="OWNER_FIRST_OUTPUT",
            authorization_id="first_" + sha(encoded(value)), actor_id=value["actor_id"],
            contract_sha256=sha(encoded(value)), execution_id=value["execution_id"],
            input_origin="PROVIDER_RESPONSE", policy_version="synthetic-policy-e\u0301",
            granted_at=now.isoformat().replace("+00:00", "Z"),
            expires_at=(now + timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
            max_attempts=1, reserved_output_tokens=value["prepared"]["provider_request"]["max_output_tokens"],
            decision_sha256=decision, request_sha256=value["prepared"]["request_sha256"],
            manifest_sha256=sha(encoded(manifest)), credential_binding=deepcopy(manifest.get("credential_binding"))),
        issuer_id="synthetic-native-issuer", decision_ref="synthetic-first-output-only",
        registered_at=now.isoformat().replace("+00:00", "Z"), expected_sequence=0)


def activate(c, *, decisions=frozenset({DECISION}), credential_path=None):
    runtime = native.service().AnthropicNativeRuntime(model=native.MODEL, credential_revision=7,
        trusted_issuers=native.TRUSTED, source=lambda value: deepcopy(c["offered"].get(value["execution_id"])),
        owner_first_output_decisions=decisions, credential_path=credential_path)
    c["runtime"].stop()
    c["runtime"] = runtime
    c["app"].state.paper_anthropic_native_runtime = runtime
    return runtime


def offer(c, saved):
    record = first_record(saved["contract"], now=datetime.now(timezone.utc))
    c["offered"][saved["contract"]["execution_id"]] = record
    return record


def approve(c, saved, url):
    record = offer(c, saved)
    preview = c["http"].get(url + "/anthropic-native-approval")
    assert preview.status_code == 200, preview.text
    assert preview.json()["offer"]["authorization_kind"] == "OWNER_FIRST_OUTPUT"
    assert preview.json()["offer"]["decision_sha256"] == DECISION
    body = dict(request_id="first-approve", expected_sequence=0, expected_contract_sha256=saved["contract_sha256"],
        expected_authority_sha256=sha(encoded(record)))
    response = c["http"].post(url + "/anthropic-native-approval", json=body, headers=native.HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["authority"]["authorization_kind"] == "OWNER_FIRST_OUTPUT"
    assert response.json()["authority"]["decision_sha256"] == DECISION
    return response.json(), body


def test_first_output_record_roundtrip_without_proof_or_prior_verification():
    value = contract()
    stored = first_record(value)
    parsed = authority.read_record(stored, value)
    assert parsed.model_dump(mode="json") == stored
    assert not hasattr(parsed.grant, "proof")
    assert authority.reference_from(stored).record_sha256 == sha(encoded(stored))
    authority.validate_grant(parsed.grant, value, NOW + timedelta(seconds=1))
    assert "EXACT_OUTPUT_CLASS_VERIFIED" not in encoded(stored)


@pytest.mark.parametrize("field,new", [("actor_id", "other"), ("contract_sha256", "0" * 64),
    ("execution_id", "other"), ("request_sha256", "0" * 64), ("manifest_sha256", "0" * 64),
    ("decision_sha256", "invalid"), ("reserved_output_tokens", 2199), ("max_attempts", True),
    ("max_attempts", 2), ("input_origin", "FIXTURE_SYNTHETIC"), ("authorization_kind", "VERIFIED"),
    ("proof", {}), ("credential_binding", None),
    ("credential_binding", {"connector_id": "anthropic_api", "revision": True, "storage": "LOCAL_DPAPI"}),
    ("credential_binding", {"connector_id": "anthropic_api", "revision": 8, "storage": "LOCAL_DPAPI"})])
def test_first_output_rejects_changed_binding_and_forged_verification(field, new):
    value = contract()
    stored = first_record(value)
    stored["grant"][field] = new
    with pytest.raises(ValueError, match="authority_integrity_invalid"):
        authority.read_record(stored, value)


@pytest.mark.parametrize("change", ["unbound", "mock", "v1", "codex", "changed_body", "different_actor", "different_request"])
def test_first_output_is_only_for_exact_bound_native_anthropic_v2(change):
    value = contract()
    stored = first_record(value)
    if change == "unbound":
        value["transport"].pop("credential_binding")
    elif change == "mock":
        value = plans.execution(surface="MOCK_TRANSPORT")
    elif change == "v1":
        value["transport"] = adapter.build_plan(value["prepared"], timeout_seconds=60)[2]
    elif change == "codex":
        value = legacy.contract()
    elif change == "changed_body":
        value["transport"]["body_sha256"] = "0" * 64
    elif change == "different_actor":
        value["actor_id"] = "other"
    else:
        value["execution_id"] = "other"
    stored["grant"]["contract_sha256"] = sha(encoded(value))
    with pytest.raises(ValueError, match="authority_integrity_invalid"):
        authority.read_record(stored, value)


@pytest.mark.parametrize("delta", [0, 301])
def test_first_output_rejects_invalid_expiry_window(delta):
    value = contract()
    stored = first_record(value)
    stored["grant"]["expires_at"] = (NOW + timedelta(seconds=delta)).isoformat().replace("+00:00", "Z")
    with pytest.raises(ValueError, match="authority_integrity_invalid"):
        authority.read_record(stored, value)


def test_first_output_historical_read_does_not_renew_and_expiry_is_exclusive():
    value = contract()
    stored = first_record(value)
    parsed = authority.read_record(stored, value)
    with pytest.raises(ValueError, match="authority_expired"):
        authority.validate_grant(parsed.grant, value, NOW + timedelta(minutes=2))
    assert authority.read_record(stored, value).model_dump(mode="json") == stored


def test_generic_approval_default_rejects_first_output_even_with_trusted_issuer():
    value = contract()
    stored = first_record(value, now=datetime.now(timezone.utc))
    runtime = PaperApprovalRuntime(source=lambda _: stored, approvers=frozenset({value["actor_id"]}), trusted_issuers=native.TRUSTED)
    actor = ResourceActor.oidc_user(value["actor_id"])
    with pytest.raises(ValueError, match="paper_approval_first_output_not_allowed"):
        runtime.record(actor, dict(contract=value, current=dict(state="READY", sequence=0)))


def test_native_first_output_requires_explicit_server_decision_allowlist(native_case):
    c = native_case
    saved, url, _, _ = native.ready(c)
    offer(c, saved)
    response = c["http"].get(url + "/anthropic-native-approval")
    assert response.status_code == 200, response.text
    assert response.json()["approval_allowed"] is False
    assert response.json()["reason"] == "paper_approval_first_output_not_allowed"
    assert c["resolutions"] == c["calls"] == []


def test_first_output_app_dispatch_once_then_source_free_replay_and_revocation(native_case):
    c = native_case
    runtime = activate(c)
    try:
        saved, url, prepare_body, _ = native.ready(c)
        _, approval_body = approve(c, saved, url)
        body = approval_body | dict(request_id="first-dispatch", expected_sequence=1)
        response = c["http"].post(url + "/anthropic-native-dispatch", json=body, headers=native.HEADERS)
        assert response.status_code == 200, response.text
        runtime.wait_idle(15)
        final = c["http"].get(url).json()
        assert final["current"]["state"] == "RESULT_SAVED", final["current"]
        assert len(c["resolutions"]) == len(c["calls"]) == 1
        c["offered"].clear()
        c["state"].update(stored=False, revision=8)
        c["app"].state.paper_anthropic_native_runtime = None
        for endpoint, command in ((c["root"] + "/anthropic-native-requests", prepare_body),
                (url + "/anthropic-native-approval", approval_body), (url + "/anthropic-native-dispatch", body)):
            replay = c["http"].post(endpoint, json=command, headers=native.HEADERS)
            assert replay.status_code == 200, replay.text
            assert replay.json()["events"] == final["events"]
        revoke = body | dict(request_id="first-revoke", expected_sequence=final["current"]["sequence"])
        revoked = c["http"].post(url + "/anthropic-native-revocation", json=revoke, headers=native.HEADERS)
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["authority"]["validity"] == "REVOKED"
        assert revoked.json()["events"] == final["events"]
        assert len(c["resolutions"]) == len(c["calls"]) == 1
    finally:
        runtime.stop()


def test_stored_first_output_does_not_enable_different_runtime_or_generic_dispatch(native_case):
    c = native_case
    runtime = activate(c)
    try:
        saved, url, _, _ = native.ready(c)
        approved, body = approve(c, saved, url)
        runtime = activate(c, decisions=frozenset({"e" * 64}))
        response = c["http"].get(url + "/anthropic-native-status")
        assert response.status_code == 200 and response.json()["enabled"] is False
        assert response.json()["reason"] == "paper_approval_first_output_not_allowed"
        body |= dict(request_id="first-dispatch", expected_sequence=1)
        assert c["http"].post(url + "/anthropic-native-dispatch", json=body, headers=native.HEADERS).status_code == 409
        value = saved["contract"]
        source = c["preparation"]["source"]
        scope = review_execution.Scope(value["topic_id"], value["paper_version_id"], value["preparation_id"],
            value["actor_id"], ((source["run_id"], source["paper_id"]),))
        # A permissive generic policy is not explicit first-output authorization.
        generic = PaperDispatchRuntime(provider=runtime.provider_for(value), stored_authority_policy=lambda _: None)
        with pytest.raises(ValueError, match="paper_approval_first_output_not_allowed"):
            generic.dispatch(scope, value["execution_id"], review_execution.Dispatch(request_id="generic", expected_sequence=1))
        generic.stop()
        assert c["http"].get(url).json()["events"] == approved["events"]
        assert c["resolutions"] == c["calls"] == []
    finally:
        runtime.stop()


def test_explicit_trusted_vault_path_is_pinned_and_never_returned(native_case, monkeypatch, tmp_path):
    c = native_case
    monkeypatch.setattr(native.model_credentials, "path", lambda: tmp_path / "unused-vault")
    runtime = activate(c, credential_path=c["vault"])
    try:
        saved, url, _, preview = native.ready(c)
        _, body = approve(c, saved, url)
        body |= dict(request_id="first-dispatch", expected_sequence=1)
        response = c["http"].post(url + "/anthropic-native-dispatch", json=body, headers=native.HEADERS)
        assert response.status_code == 200, response.text
        runtime.wait_idle(15)
        assert c["http"].get(url).json()["current"]["state"] == "RESULT_SAVED"
        assert len(c["resolutions"]) == len(c["calls"]) == 1
        assert str(c["vault"]) not in encoded(preview)
    finally:
        runtime.stop()


@pytest.mark.parametrize("changes", [dict(credential_path="D:/not-a-Path"), dict(credential_path=Path("relative.json")),
    dict(owner_first_output_decisions={DECISION}), dict(owner_first_output_decisions=frozenset({"bad"})),
    dict(owner_first_output_decisions=frozenset({True}))])
def test_native_first_output_server_configuration_is_strict(changes):
    with pytest.raises(ValueError, match="configuration_invalid"):
        native.service().AnthropicNativeRuntime(model=native.MODEL, credential_revision=7, trusted_issuers=native.TRUSTED, **changes)


@pytest.mark.parametrize("marker", [dict(authorization_kind="OWNER_FIRST_OUTPUT"), dict(decision_sha256=DECISION),
    dict(authorization_kind="UNKNOWN", decision_sha256=DECISION),
    dict(authorization_kind="OWNER_FIRST_OUTPUT", decision_sha256=True)])
def test_stored_authority_kind_pair_is_strict(marker):
    runtime = native.service().AnthropicNativeRuntime(model=native.MODEL, credential_revision=7,
        trusted_issuers=native.TRUSTED, owner_first_output_decisions=frozenset({DECISION}))
    try:
        with pytest.raises(ValueError, match="first_output_not_allowed"):
            runtime.check_authority(dict(authority=dict(issuer_id="synthetic-native-issuer",
                policy_version="synthetic-policy-e\u0301", validity="WITHIN_VALIDITY_WINDOW", **marker)))
    finally:
        runtime.stop()


def test_generic_resolver_cannot_register_first_output_without_explicit_allowlist(native_case):
    c = native_case
    saved, url, _, _ = native.ready(c)
    value, source = saved["contract"], c["preparation"]["source"]
    scope = review_execution.Scope(value["topic_id"], value["paper_version_id"], value["preparation_id"],
        value["actor_id"], ((source["run_id"], source["paper_id"]),))
    record = first_record(value, now=datetime.now(timezone.utc))
    generic = PaperDispatchRuntime(resolver=lambda *_: record, provider=c["runtime"].provider_for(value))
    try:
        with pytest.raises(ValueError, match="first_output_not_allowed"):
            generic.approve(scope, value["execution_id"], review_execution.Control(request_id="generic", expected_sequence=0, action="approve"))
        assert c["http"].get(url).json()["current"]["state"] == "READY"
        assert c["resolutions"] == c["calls"] == []
    finally:
        generic.stop()


def test_same_first_output_decision_cannot_rotate_authorization_or_request_after_revoke(native_case):
    c = native_case
    runtime = activate(c)
    try:
        saved, url, prepare_body, _ = native.ready(c)
        _, body = approve(c, saved, url)
        revoke = body | dict(request_id="first-revoke", expected_sequence=1)
        response = c["http"].post(url + "/anthropic-native-revocation", json=revoke, headers=native.HEADERS)
        assert response.status_code == 200, response.text
        second = c["http"].post(c["root"] + "/anthropic-native-requests",
            json=prepare_body | dict(request_id="rotated-request"), headers=native.HEADERS)
        assert second.status_code == 200, second.text
        saved_second = second.json()
        second_url = c["root"] + "/" + saved_second["contract"]["execution_id"]
        record = offer(c, saved_second)
        command = body | dict(request_id="rotated-approval", expected_contract_sha256=saved_second["contract_sha256"],
            expected_authority_sha256=sha(encoded(record)))
        response = c["http"].post(second_url + "/anthropic-native-approval", json=command, headers=native.HEADERS)
        assert response.status_code == 409 and response.json()["detail"] == "paper_review_authority_conflict"
        assert c["http"].get(second_url).json()["current"]["state"] == "READY"
        assert c["http"].get(url).json()["authority"]["validity"] == "REVOKED"
        assert c["resolutions"] == c["calls"] == []
    finally:
        runtime.stop()


def test_first_output_expired_offer_and_browser_flags_cannot_enable(native_case):
    c = native_case
    runtime = activate(c)
    try:
        saved, url, _, _ = native.ready(c)
        record = first_record(saved["contract"], now=datetime.now(timezone.utc) - timedelta(minutes=3))
        c["offered"][saved["contract"]["execution_id"]] = record
        response = c["http"].get(url + "/anthropic-native-approval")
        assert response.status_code == 200 and response.json()["reason"] == "paper_review_authority_expired"
        body = dict(request_id="forged", expected_sequence=0, expected_contract_sha256=saved["contract_sha256"],
            expected_authority_sha256=sha(encoded(record)))
        for extra in (dict(authorization_kind="OWNER_FIRST_OUTPUT"), dict(decision_sha256=DECISION),
                dict(owner_first_output_decisions=[DECISION]), dict(credential_path="D:/private"), dict(proof={})):
            response = c["http"].post(url + "/anthropic-native-approval", json=body | extra, headers=native.HEADERS)
            assert response.status_code == 422, response.text
        assert c["http"].get(url).json()["current"]["state"] == "READY"
        assert c["resolutions"] == c["calls"] == []
    finally:
        runtime.stop()


def test_concurrent_first_output_registration_consumes_decision_once(native_case):
    c = native_case
    runtime = activate(c)
    try:
        saved, url, prepare_body, _ = native.ready(c)
        second = c["http"].post(c["root"] + "/anthropic-native-requests",
            json=prepare_body | dict(request_id="parallel-request"), headers=native.HEADERS)
        assert second.status_code == 200, second.text
        items = [(saved, url), (second.json(), c["root"] + "/" + second.json()["contract"]["execution_id"])]
        commands = []
        for item, endpoint in items:
            record = offer(c, item)
            commands.append((endpoint, dict(request_id="parallel-approve", expected_sequence=0,
                expected_contract_sha256=item["contract_sha256"], expected_authority_sha256=sha(encoded(record)))))
        barrier = Barrier(2)
        def submit(item):
            barrier.wait(timeout=10)
            endpoint, body = item
            return c["http"].post(endpoint + "/anthropic-native-approval", json=body, headers=native.HEADERS)
        with ThreadPoolExecutor(max_workers=2) as workers:
            responses = list(workers.map(submit, commands))
        assert sorted(item.status_code for item in responses) == [200, 409]
        assert next(item for item in responses if item.status_code == 409).json()["detail"] == "paper_review_authority_conflict"
        states = [c["http"].get(endpoint).json()["current"]["state"] for _, endpoint in items]
        assert sorted(states) == ["APPROVED", "READY"]
        assert c["resolutions"] == c["calls"] == []
    finally:
        runtime.stop()
