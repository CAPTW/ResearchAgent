"""Pure V2 Messages plans, MockTransport calls and immutable history checks."""
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest

from app.domain import prepared_review, review_authority, review_transport_execution
from app.domain.journal_exchange import preserved_json, sha
from app.repositories import ai_review_drafts
from app.services import anthropic_output_comparison
from app.services.provider_output_class_proof import compute_provider_proof_id
from app.services.providers import anthropic_prepared_review as adapter
from app.services.providers.base import ProviderError, ProviderOutputError, ProviderRequest
from tests.test_anthropic_messages import envelope
from tests.test_anthropic_prepared_review import contract
from tests import test_prepared_review as prior, test_review_authority as authority

V2 = "ANTHROPIC_PREPARED_REVIEW_WIRE_V2"
MARKER_V2 = "PREPARED_REVIEW_ANTHROPIC_HTTP_TRANSPORT_V2"
# Captured from untouched main 87a3000 before V2 implementation, timeout 60.
V1_GOLDEN = {
    prepared_review.LEGACY_SCHEMA: (
        "ba947acf7cd5b09c23d518ac13cd74216bf10bb16dcc37825561345adae36e62",
        "8675db25f5f881405228beb289613122da392dad11f346e8a176823f6a89dc09",
        "1ffb5c5234d392e2bee20219e1141c2235783000bb5576b4926d57fb58a4ce1b"),
    prepared_review.SCHEMA_V2: (
        "7cbeccb39671e17c7372b356aa23b851651aea64db63965dbf3aee5e38f32256",
        "a404c44418a4af7b746161f248ecedc68215515d904bb441fc8cd2835035908f",
        "405c1c09eb10fe7143f0baf24d60c781ded477669b2f2c0163db50b440438ab4"),
    prepared_review.SCHEMA: (
        "dd5fe7bd7328ae763c55d44f2c6c019ef4aca83daae076b29cf6907402e88ed0",
        "8fbdd6328f8f7c4eae986a9598320bea9c3095b746bca90c302349a5b40d26df",
        "56fd0a147c3590710e3e4fd07e093fbc554c76028dfe0d407b704bede273f30c"),
}


def plan(frozen=None, *, surface="MOCK_TRANSPORT", **options):
    return adapter.build_plan(contract() if frozen is None else frozen, timeout_seconds=60,
        profile=V2, execution_surface=surface, **options)


def provider_for(frozen=None, handler=None, **options):
    frozen = contract() if frozen is None else frozen
    calls = []

    def respond(request):
        calls.append(request)
        return handler(request) if handler else httpx.Response(200, json=envelope(prior.response(frozen).raw_text))

    provider = adapter.AnthropicPreparedReviewProvider(frozen, **(dict(api_key="synthetic-key",
        timeout_seconds=60, profile=V2, execution_surface="MOCK_TRANSPORT", transport=httpx.MockTransport(respond)) | options))
    return provider, frozen, calls


def execution(frozen=None, *, surface="MOCK_TRANSPORT"):
    frozen = contract() if frozen is None else frozen
    value = authority.contract()
    value.update(prepared=frozen, transport=plan(frozen, surface=surface)[2], mode="TRUSTED_PAPER_HTTP",
        specification=dict(provider_name="anthropic", model=frozen["model"], max_output_tokens=frozen["provider_request"]["max_output_tokens"]),
        input_origin="FIXTURE_SYNTHETIC" if surface == "MOCK_TRANSPORT" else "PROVIDER_RESPONSE")
    return value


def grant(value):
    approved = authority.grant(value)
    identity = replace(approved.proof.identity, surface_id="api")
    proof = replace(approved.proof, identity=identity, proof_id=compute_provider_proof_id(identity))
    return approved.model_copy(update={"proof": proof})


def receipt(record, packet, origin="FIXTURE_SYNTHETIC"):
    return ai_review_drafts._receipt_content(ai_review_drafts._normalize_record(record), packet, origin)


@pytest.mark.parametrize("version", list(V1_GOLDEN))
def test_v1_request_body_and_manifest_keep_committed_golden_hashes(version):
    request, body, manifest = adapter.build_plan(contract(schema_version=version), timeout_seconds=60)
    assert (sha(request), sha(body.decode("utf-8")), sha(preserved_json(manifest))) == V1_GOLDEN[version]
    assert "execution_surface" not in manifest and "shared_schema_context" not in manifest


@pytest.mark.parametrize("version", list(V1_GOLDEN))
def test_v2_exact_comparison_body_unicode_and_roundtrip_all_prepared_versions(version):
    frozen = contract(schema_version=version)
    before = deepcopy(frozen)
    request, body, manifest = plan(frozen)
    expected = anthropic_output_comparison.prepare(frozen)["arms"]["structured"]
    assert body == preserved_json(expected["body"]).encode("utf-8")
    assert manifest["body_sha256"] == expected["body_sha256"] and manifest["body_bytes"] == expected["body_bytes"]
    assert request == preserved_json(frozen["provider_request"]) and frozen == before
    assert " e\u0301 한글🙂 tail " in body.decode("utf-8")
    assert manifest["schema_version"] == V2 and manifest["adapter_version"] == "2"
    assert manifest["schema_compiler"] == "ANTHROPIC_JSON_SCHEMA_V2"
    assert manifest["execution_surface"] == "MOCK_TRANSPORT"
    assert manifest["shared_schema_context"] == "ORIGINAL_LOCAL_SCHEMA_IN_SYSTEM"
    assert manifest["effective_system_sha256"] == sha(expected["body"]["system"])
    assert manifest["timeout_seconds"] == 60.0 and manifest["timeout_semantics"] == "PER_OPERATION_INACTIVITY"
    assert manifest["raw_envelope_retained"] is False and manifest["live_execution"] == "NOT_VERIFIED"
    provider, _, calls = provider_for(frozen)
    record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    assert len(calls) == 1 and calls[0].content == body
    marker = record["validation"]["execution"]
    assert marker["schema_version"] == MARKER_V2 and marker["adapter_version"] == "2"
    assert marker["manifest"] == manifest and marker["authority"] == "NOT_ESTABLISHED"
    assert record["validation"]["prepared_execution"]["response"]["raw_text"] == prior.response(frozen).raw_text
    adapter.validate_transport(record, frozen["packet"])
    review_transport_execution.validate_record(execution(frozen), record)
    saved = receipt(record, frozen["packet"])
    assert saved["input_origin"] == "FIXTURE_SYNTHETIC"
    assert saved["validation"]["execution"] == marker


def test_profile_constants_and_surface_bindings_are_explicit_and_pure():
    assert adapter.MANIFEST_SCHEMA_V2 == V2 and adapter.TRANSPORT_SCHEMA_V2 == MARKER_V2
    mock = plan()[2]
    native = plan(surface="NATIVE_HTTP")[2]
    assert mock["body_sha256"] == native["body_sha256"]
    assert mock != native and sha(preserved_json(mock)) != sha(preserved_json(native))
    native_provider = adapter.AnthropicPreparedReviewProvider(contract(), api_key="synthetic-key", timeout_seconds=60,
        profile=V2, execution_surface="NATIVE_HTTP")
    assert native_provider.manifest() == native
    assert native_provider.describe_status()["live_call_enabled"] is False


@pytest.mark.parametrize("surface,transport", [("MOCK_TRANSPORT", None), ("NATIVE_HTTP", httpx.MockTransport(lambda _: None)),
    ("unrecognized", None)])
def test_v2_runtime_cannot_mislabel_mock_or_network_surface(surface, transport):
    with pytest.raises(ValueError, match="configuration_invalid"):
        adapter.AnthropicPreparedReviewProvider(contract(), api_key="synthetic-key", timeout_seconds=60,
            profile=V2, execution_surface=surface, transport=transport)


@pytest.mark.parametrize("profile,surface", [(adapter.MANIFEST_SCHEMA, "MOCK_TRANSPORT"), (V2, "other"), ("unknown", "NATIVE_HTTP")])
def test_unknown_profile_surface_and_v1_extra_surface_rejected(profile, surface):
    with pytest.raises(ValueError, match="configuration_invalid"):
        adapter.build_plan(contract(), timeout_seconds=60, profile=profile, execution_surface=surface)


@pytest.mark.parametrize("timeout", [0, 121, True, float("inf")])
def test_v2_does_not_extend_ordinary_timeout_contract(timeout):
    with pytest.raises(ValueError, match="configuration_invalid"):
        adapter.build_plan(contract(), timeout_seconds=timeout, profile=V2, execution_surface="MOCK_TRANSPORT")


@pytest.mark.parametrize("change", ["request", "schema", "source"])
def test_v2_corrupted_original_input_is_rejected_before_call(change):
    frozen = contract()
    if change == "source":
        frozen["packet"]["source_text_sha256"] = "0" * 64
    elif change == "schema":
        frozen["provider_request"]["schema"]["properties"]["confidence"]["maximum"] = 2
    else:
        frozen["provider_request"]["prompt"] += " altered"
    with pytest.raises(ValueError):
        plan(frozen)


@pytest.mark.parametrize("field,value", [("schema_compiler", "ANTHROPIC_JSON_SCHEMA_V1"), ("body_sha256", "0" * 64),
    ("wire_schema_sha256", "0" * 64), ("effective_system_sha256", "0" * 64), ("shared_schema_context", "NONE"),
    ("adapter_version", "1"), ("schema_version", adapter.MANIFEST_SCHEMA), ("execution_surface", "OTHER")])
def test_v2_rehashed_corrupt_manifest_rejected_by_transport_and_receipt(field, value):
    provider, frozen, _ = provider_for()
    record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    marker = record["validation"]["execution"]
    marker["manifest"][field] = value
    marker["manifest_sha256"] = sha(preserved_json(marker["manifest"]))
    with pytest.raises(ValueError, match="transport_invalid"):
        adapter.validate_transport(record, frozen["packet"])
    with pytest.raises(ValueError):
        receipt(record, frozen["packet"])


@pytest.mark.parametrize("field,value", [("adapter_id", "unknown"), ("schema_version", "unknown"), ("adapter_version", "1")])
def test_v2_receipt_recognizes_manifest_even_when_marker_identity_tampered(field, value):
    provider, frozen, _ = provider_for()
    record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    marker = record["validation"]["execution"]
    marker[field] = value
    if field == "adapter_id":
        marker["schema_version"] = "unknown"
    with pytest.raises(ValueError, match="transport_invalid"):
        receipt(record, frozen["packet"])


@pytest.mark.parametrize("surface", ["MOCK_TRANSPORT", "NATIVE_HTTP"])
def test_v2_grant_records_preserve_exact_profile_surface_and_mode(surface):
    value = execution(surface=surface)
    approved = grant(value)
    review_authority.validate_grant(approved, value, authority.NOW)
    saved = authority.record(value, grant=approved)
    assert review_authority.read_record(saved, value).grant == approved
    for changed in ("surface", "profile", "mode"):
        other = deepcopy(value)
        if changed == "surface":
            target = "NATIVE_HTTP" if surface == "MOCK_TRANSPORT" else "MOCK_TRANSPORT"
            other["transport"] = plan(surface=target)[2]
            other["input_origin"] = "PROVIDER_RESPONSE" if target == "NATIVE_HTTP" else "FIXTURE_SYNTHETIC"
        elif changed == "profile":
            other["transport"] = adapter.build_plan(other["prepared"], timeout_seconds=60)[2]
        else:
            other["mode"] = "TRUSTED_PAPER_CLI"
        rehashed = approved.model_copy(update={"contract_sha256": sha(preserved_json(other)), "input_origin": other["input_origin"]})
        with pytest.raises(ValueError):
            review_authority.validate_grant(rehashed, other, authority.NOW)


def test_mock_output_cannot_be_relabelled_as_real_provider_receipt():
    provider, frozen, _ = provider_for()
    record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    with pytest.raises(ValueError):
        receipt(record, frozen["packet"], "PROVIDER_RESPONSE")
    value = execution()
    value["input_origin"] = "PROVIDER_RESPONSE"
    with pytest.raises(ValueError):
        review_authority.validate_grant(grant(value), value, authority.NOW)


@pytest.mark.parametrize("mode", ["citation", "confidence", "timeout", "redirect"])
def test_v2_local_validation_and_uncertain_results_keep_single_attempt(mode):
    frozen = contract()
    draft = prior.response(frozen).payload
    if mode == "citation":
        draft["cited_evidence_span_ids"] = ["invented"]
    elif mode == "confidence":
        draft["confidence"] = 1.1

    def respond(request):
        if mode == "timeout":
            raise httpx.ReadTimeout("private diagnostic", request=request)
        return httpx.Response(307 if mode == "redirect" else 200, json=envelope(preserved_json(draft)),
            headers={"location": "https://never-follow.invalid"})

    provider, _, calls = provider_for(frozen, respond)
    with pytest.raises(ProviderError) as error:
        provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    assert len(calls) == 1 and "private" not in str(error.value)
    if mode in {"citation", "confidence"}:
        assert isinstance(error.value, ProviderOutputError)
    else:
        assert type(error.value) is ProviderError and "outcome_unknown" in str(error.value)


def test_request_mismatch_cannot_send_even_when_manifest_is_detached():
    provider, frozen, calls = provider_for()
    provider.manifest()["execution_surface"] = "NATIVE_HTTP"
    request = ProviderRequest(**frozen["provider_request"])
    with pytest.raises(ValueError, match="adapter_mismatch"):
        provider.generate_record(replace(request, prompt=request.prompt + "other"))
    assert calls == [] and "synthetic-key" not in repr(provider)


@pytest.mark.parametrize("extra", [0, 1])
def test_v2_retains_exact_256k_output_cap(extra):
    frozen = contract()
    raw = prior.response(frozen).raw_text
    raw += " " * (prepared_review.MAX_OUTPUT_BYTES + extra - len(raw.encode("utf-8")))
    provider, _, calls = provider_for(frozen, lambda _: httpx.Response(200, json=envelope(raw)))
    if extra:
        with pytest.raises(ProviderOutputError):
            provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    else:
        record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
        assert record["validation"]["prepared_execution"]["response"]["raw_text"] == raw
    assert len(calls) == 1


def test_native_credential_revision_binds_manifest_not_body_and_roundtrips():
    frozen = contract()
    old_request, old_body, old_manifest = plan(frozen, surface="NATIVE_HTTP")
    request, body, manifest = plan(frozen, surface="NATIVE_HTTP", credential_revision=7)
    assert (request, body) == (old_request, old_body)
    assert manifest == old_manifest | {"credential_binding": {"connector_id": "anthropic_api", "revision": 7, "storage": "LOCAL_DPAPI"}}
    assert sha(preserved_json(manifest)) != sha(preserved_json(old_manifest))
    assert review_transport_execution.build_manifest(frozen, manifest) == manifest
    response = prior.response(frozen)
    response.provider_name = "anthropic"
    record = adapter.transport_record(frozen, response, manifest)
    adapter.validate_transport(record, frozen["packet"])
    changed = deepcopy(manifest)
    changed["credential_binding"]["storage"] = "ENVIRONMENT"
    with pytest.raises(ValueError):
        review_transport_execution.build_manifest(frozen, changed)
    changed = deepcopy(manifest)
    changed["credential_binding"]["extra"] = True
    with pytest.raises(ValueError):
        review_transport_execution.build_manifest(frozen, changed)


@pytest.mark.parametrize("revision", [None, 0, -1, True, 1.0, "1"])
def test_native_credential_revision_is_strict_positive_integer(revision):
    with pytest.raises(ValueError, match="configuration_invalid"):
        plan(surface="NATIVE_HTTP", credential_revision=revision)


@pytest.mark.parametrize("profile,surface", [(adapter.MANIFEST_SCHEMA, "NATIVE_HTTP"), (V2, "MOCK_TRANSPORT")])
def test_stored_credential_binding_is_only_native_v2(profile, surface):
    with pytest.raises(ValueError, match="configuration_invalid"):
        adapter.build_plan(contract(), timeout_seconds=60, profile=profile, execution_surface=surface, credential_revision=1)


def test_rehashed_outer_grant_cannot_change_pinned_credential_revision():
    value = execution(surface="NATIVE_HTTP")
    value["transport"] = plan(surface="NATIVE_HTTP", credential_revision=7)[2]
    approved = grant(value)
    review_authority.validate_grant(approved, value, authority.NOW)
    value["transport"] = plan(surface="NATIVE_HTTP", credential_revision=8)[2]
    changed = approved.model_copy(update={"contract_sha256": sha(preserved_json(value))})
    with pytest.raises(ValueError, match="authority_proof_rejected"):
        review_authority.validate_grant(changed, value, authority.NOW)


@pytest.mark.parametrize("options", [dict(), dict(profile=V2), dict(profile=V2, execution_surface="MOCK_TRANSPORT", credential_revision=7),
    dict(profile=V2, execution_surface="NATIVE_HTTP", credential_revision=7, before_send=False)])
def test_before_send_guard_requires_bound_native_profile_and_callable(options):
    args = dict(api_key="synthetic-key", timeout_seconds=60, before_send=lambda: None) | options
    with pytest.raises(ValueError, match="configuration_invalid"):
        adapter.AnthropicPreparedReviewProvider(contract(), **args)
