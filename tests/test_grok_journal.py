"""Grok Journal fixtures use owned Python ACP children, never an installed CLI."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import importlib
import json

import pytest

from app.domain import journal_execution
from app.domain.journal_exchange import preserved_json, sha
from app.domain.temporal_memory import digest
from app.services.providers.base import ProviderError, ProviderOutputError, ProviderRefusalError, ProviderReportedFailure, ProviderRequest
from app.services.providers.grok_prepared_review_process import CapturedConversation
from tests import test_grok_prepared_review as paper
from tests.test_journal_review_packet import frozen_job, proposal, NOW


def modules():
    return (importlib.import_module("app.services.providers.grok_journal"),
            importlib.import_module("app.services.providers.grok_journal_plan"))


def test_explicit_request_bound_grok_journal_adapter_exists():
    adapter, plan = modules()
    assert adapter.GrokJournalProvider
    assert plan.SURFACE == "grok_cli"
    assert plan.MANIFEST_SCHEMA == "GROK_JOURNAL_ACP_V1"


def contract(*, selected=True, **changes):
    job = frozen_job(selected=selected)
    spec = journal_execution.Prepare(**(dict(request_id="grok-journal", expected_input_digest=job["input_digest"],
        provider_name="grok_cli", model="synthetic-grok-model") | changes))
    return journal_execution.prepare(job, spec, "owner", NOW)


def runtime_at(root, frozen, mode="success", *, output=None):
    payload = proposal(frozen["packet"])
    if mode == "citation":
        payload["sections"][0]["entries"][0]["citation_ids"] = ["invented"]
        mode = "success"
    raw = json.dumps(payload, ensure_ascii=False, indent=2) + "\n" if output is None else output
    # The existing fake child needs only a model and fixture packet, while its
    # output is this Journal proposal. No paper contract enters the adapter.
    fixture = paper.contract()
    fixture["model"] = frozen["specification"]["model"]
    return paper.runtime_at(root, fixture, mode=mode, output=raw)


ATTEMPT = "D:/synthetic-grok-journal/grok-review-fixture"


def transcript(frozen, *, raw=None, stop="end_turn"):
    model = frozen["specification"]["model"]
    raw = json.dumps(proposal(frozen["packet"]), ensure_ascii=False, indent=2) + "\n" if raw is None else raw
    options = [dict(id="model", name="Model", category="model", type="select", currentValue=model,
                    options=[dict(value=model, name="Fixture")])]
    value = []
    requests = [("initialize", dict(protocolVersion=1, clientCapabilities={}, clientInfo=dict(name="xrml-prepared-paper", version="1")),
                 dict(protocolVersion=1, agentCapabilities={}, authMethods=[dict(id="cached_token", name="Cached")])),
                ("authenticate", dict(methodId="cached_token", _meta=dict(headless=True)), {}),
                ("session/new", dict(cwd=ATTEMPT, mcpServers=[]), dict(sessionId="fixture-session", configOptions=options)),
                ("session/set_config_option", dict(sessionId="fixture-session", configId="model", value=model), dict(configOptions=options)),
                ("session/prompt", dict(sessionId="fixture-session", prompt=[dict(type="text", text=preserved_json(frozen["provider_request"]))]), dict(stopReason=stop))]
    for index, (method, params, result) in enumerate(requests, 1):
        value.append(dict(direction="client", raw_line=preserved_json(dict(jsonrpc="2.0", id=index, method=method, params=params)) + "\n"))
        if index == 5:
            for part in (raw[:17], raw[17:]):
                value.append(dict(direction="server", raw_line=preserved_json(dict(jsonrpc="2.0", method="session/update", params=dict(
                    sessionId="fixture-session", update=dict(sessionUpdate="agent_message_chunk", messageId="message-1",
                    content=dict(type="text", text=part))))) + "\n"))
        value.append(dict(direction="server", raw_line=preserved_json(dict(jsonrpc="2.0", id=index, result=result)) + "\n"))
    return value


class StubRuntime:
    def __init__(self, value):
        self.result = CapturedConversation(value, ATTEMPT)
        self.calls = []
        self.checks = 0

    def identity(self):
        return {key: "a" * 64 for key in ("launcher_sha256", "environment_sha256", "workspace_sha256")}

    def verify(self):
        self.checks += 1

    def run(self, request_text, **settings):
        self.verify()
        self.calls.append((request_text, settings))
        return self.result


def stub_provider(*, frozen=None, value=None, timeout=30):
    adapter, _ = modules()
    frozen = frozen or contract()
    runtime = StubRuntime(transcript(frozen) if value is None else value)
    return adapter.GrokJournalProvider(frozen, runtime=runtime, timeout_seconds=timeout), runtime, frozen


@pytest.mark.parametrize("selected", [False, True])
def test_fake_process_binds_exact_acp_request_profile_and_journal_response(tmp_path, selected):
    adapter, plan = modules()
    frozen = contract(selected=selected)
    payload = proposal(frozen["packet"])
    payload["title"] = " e\u0301 한글🙂\u0085\u2028\u2029 preserved "
    raw = json.dumps(payload, ensure_ascii=False, indent=3) + " \n"
    root = tmp_path / "cli"
    runtime = runtime_at(root, frozen, output=raw)
    provider = adapter.GrokJournalProvider(frozen, runtime=runtime, timeout_seconds=30)
    response = provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    requests = [json.loads(line) for line in (root / "stdin.ndjson").read_text(encoding="utf-8").splitlines()]
    assert [item["method"] for item in requests] == ["initialize", "authenticate", "session/new", "session/set_config_option", "session/prompt"]
    assert [item["id"] for item in requests] == [1, 2, 3, 4, 5]
    assert requests[0]["params"]["clientCapabilities"] == {}
    assert requests[0]["params"]["clientInfo"] == dict(name="xrml-prepared-paper", version="1")
    assert requests[1]["params"] == dict(methodId="cached_token", _meta=dict(headless=True))
    assert requests[2]["params"]["mcpServers"] == []
    assert requests[3]["params"] == dict(sessionId="fixture-session", configId="model", value=provider.model)
    request_text = preserved_json(frozen["provider_request"])
    assert requests[4]["params"]["prompt"] == [dict(type="text", text=request_text)]
    args = json.loads((root / "argv.json").read_text())
    assert args[:2] == ["--no-auto-update", "agent"] and args[-2:] == ["--no-leader", "stdio"]
    assert args[args.index("--model") + 1] == provider.model
    assert "--tools" not in args and "--always-approve" not in args
    agent = (root / "agent.md").read_bytes().decode("utf-8")
    assert agent.endswith(frozen["provider_request"]["instructions"] + "\n")
    assert "description: Review only the supplied paper request." in agent and "tools:" not in agent
    assert response.raw_text == raw and response.raw_text != (root / "stdout.ndjson").read_bytes().decode("utf-8")
    assert response.provider_name == "grok_cli" and response.model == "not_reported"
    assert response.response_id == "fixture-session" and response.usage == {}
    saved = journal_execution.result(frozen, response)
    assert saved["raw_json"] == raw and saved["proposal"] == payload
    assert "transcript" not in saved and saved["validation"]["semantic_support"] == "NOT_ASSESSED"
    assert saved["validation"]["independent_live_proof"] == "NOT_VERIFIED"
    manifest = provider.manifest()
    assert manifest["schema_version"] == plan.MANIFEST_SCHEMA and manifest["provider_name"] == plan.SURFACE
    assert manifest["contract_digest"] == digest(frozen) and manifest["contract_sha256"] == sha(preserved_json(frozen))
    assert manifest["request_sha256"] == manifest["prompt_sha256"] == sha(request_text)
    assert manifest["prompt_bytes"] == len(request_text.encode("utf-8"))
    assert manifest["schema_sha256"] == sha(preserved_json(frozen["provider_request"]["schema"]))
    assert manifest["agent_sha256"] == sha(agent)
    assert manifest["runtime_identity"] == runtime.identity()
    assert manifest["fixed_environment"] == json.loads((root / "policy-env.json").read_text())
    assert manifest["agent_profile"] == "EXISTING_PAPER_PROFILE_WITH_FROZEN_JOURNAL_INSTRUCTIONS"
    assert manifest["max_journal_output_bytes"] == 262144
    assert manifest["raw_journal_encoding"] == "ACP_AGENT_MESSAGE_TEXT_UTF8_V1"
    assert manifest["journal_result_storage"] == "AGENT_MESSAGE_TEXT_ONLY" and manifest["raw_envelope_retained"] is False
    assert "archive_extent" not in manifest
    assert manifest["prompt_count"] == manifest["protocol_version"] == 1 and manifest["max_events"] == 4096
    assert manifest["token_usage"] == "NOT_REPORTED_BY_SUPPORTED_ACP_PROFILE"
    assert manifest["authentication"] == "OFFICIAL_CLI_CACHED_TOKEN_ONLY_NOT_VERIFIED"
    assert manifest["client_info"] == requests[0]["params"]["clientInfo"]
    for field in ("output_token_limit_enforced", "temperature_enforced", "cost_limit_enforced", "automatic_retry",
                  "xrml_process_relaunch", "remote_call_count_enforced", "output_rejection_prevents_side_effects"):
        assert manifest[field] is False
    for field in ("managed_hooks", "global_config_and_mcp"):
        assert manifest[field] == "NOT_ISOLATED_NOT_LIVE_VERIFIED"
    assert manifest["tool_isolation"] == manifest["physical_remote_cancellation"] == "NOT_ESTABLISHED"
    assert manifest["session_retention"] == "CLI_MANAGED_PROFILE_STORAGE_NOT_ISOLATED"
    assert manifest["billing_route"] == manifest["live_execution"] == "NOT_VERIFIED"
    assert manifest["model_identity"] == "REQUESTED_NOT_INDEPENDENTLY_VERIFIED"
    assert provider.binding.command_shape_sha256 == digest(manifest)
    assert provider.binding.adapter_id == "xrml-grok-frozen-journal"
    assert (root / "launches.log").read_text().splitlines() == ["launch"]
    assert list((root / "attempts").iterdir()) == []
    if selected:
        assert "e\u0301 한글🙂" in request_text


def test_input_and_manifest_are_detached_from_callers():
    frozen = contract()
    provider, runtime, _ = stub_provider(frozen=frozen)
    original = deepcopy(frozen)
    manifest = provider.manifest()
    manifest["runtime_identity"]["launcher_sha256"] = "0" * 64
    manifest["argv_shape"].append("--unsafe")
    frozen["provider_request"]["prompt"] = "caller changed"
    response = provider.generate_json(ProviderRequest(**original["provider_request"]))
    assert journal_execution.result(original, response)["proposal"] == proposal(original["packet"])
    assert runtime.calls[0][0] == preserved_json(original["provider_request"])
    assert "--unsafe" not in provider.manifest()["argv_shape"]
    assert provider.manifest()["runtime_identity"]["launcher_sha256"] == "a" * 64
    assert provider.is_configured() and provider.describe_status()["live_call_enabled"] is False
    assert provider.describe_status()["authority"] == "NOT_ESTABLISHED"
    assert "private" not in repr(provider)
    with pytest.raises(FrozenInstanceError):
        provider.model = "changed"


@pytest.mark.parametrize("change", ["prompt", "schema", "instructions", "metadata", "schema_name", "max_output_tokens", "temperature"])
def test_request_mutation_is_rejected_before_runtime(change):
    provider, runtime, frozen = stub_provider()
    request = deepcopy(frozen["provider_request"])
    request[change] = {} if change in {"schema", "metadata"} else 256 if change == "max_output_tokens" else 1 if change == "temperature" else "changed"
    with pytest.raises(ValueError, match="adapter_mismatch"):
        provider.generate_json(ProviderRequest(**request))
    assert runtime.calls == [] and runtime.checks == 0


@pytest.mark.parametrize("change", ["provider", "model", "schema", "packet-schema", "prompt", "execution", "packet", "tokens",
    "canonical-instructions", "fingerprint", "extra-fingerprint", "timeout", "bool-timeout", "activation", "topic", "job", "input-digest"])
def test_invalid_contract_or_runtime_plan_cannot_construct_adapter(change):
    _, plan = modules()
    frozen = contract()
    runtime = StubRuntime([])
    settings = dict(runtime_identity=runtime.identity(), timeout_seconds=30)
    if change == "provider":
        frozen["specification"]["provider_name"] = "codex_cli"
    elif change == "model":
        frozen["specification"]["model"] = "--flag is not model"
    elif change == "schema":
        frozen["provider_request"]["schema"] = {}
    elif change == "packet-schema":
        frozen["packet"]["schema_version"] = "unsupported"
    elif change == "prompt":
        frozen["provider_request"]["prompt"] += "changed"
    elif change == "execution":
        frozen["execution_id"] = "jexec_" + "0" * 64
    elif change == "packet":
        frozen["packet"]["packet_id"] = "jpacket_" + "0" * 64
    elif change == "tokens":
        frozen["provider_request"]["max_output_tokens"] += 1
    elif change == "canonical-instructions":
        packet = frozen["packet"]
        packet["instructions"].append("Changed instructions")
        packet["packet_id"] = "jpacket_" + digest({key: value for key, value in packet.items() if key != "packet_id"})
        frozen["provider_request"].update(instructions="\n".join(packet["instructions"]), prompt=journal_execution.exchange.provider_prompt(packet))
        frozen["provider_request"]["metadata"]["packet_id"] = packet["packet_id"]
    elif change == "fingerprint":
        settings["runtime_identity"]["launcher_sha256"] = "invalid"
    elif change == "extra-fingerprint":
        settings["runtime_identity"]["config_dir_sha256"] = "a" * 64
    elif change == "activation":
        frozen["activation"] = "LIVE"
    elif change in {"topic", "job"}:
        frozen[change + "_id"] = "other"
    elif change == "input-digest":
        frozen["packet"]["input_digest"] = "0" * 64
    else:
        settings["timeout_seconds"] = True if change == "bool-timeout" else float("inf")
    with pytest.raises(ValueError, match="configuration_invalid"):
        plan.build_plan(frozen, **settings)
    assert runtime.calls == []


@pytest.mark.parametrize("field", ["binding", "model", "contract", "request", "runtime"])
def test_binding_is_exact_and_new_limits_require_new_binding(field):
    provider, runtime, frozen = stub_provider()
    bound, model = provider.binding, provider.model
    if field == "binding":
        bound = bound.model_copy(update={"adapter_id": "paper-adapter"})
    elif field == "model":
        model = "other"
    elif field == "contract":
        frozen["actor_id"] = "other"
    elif field == "runtime":
        bound = bound.model_copy(update={"command_shape_sha256": "b" * 64})
    else:
        frozen["provider_request"]["metadata"]["execution_id"] = "other"
    with pytest.raises(ValueError, match="adapter_mismatch"):
        provider.validate_journal_binding(frozen, model, bound)
    assert runtime.calls == []
    original, _, _ = stub_provider()
    changed, _, _ = stub_provider(timeout=31)
    assert original.binding != changed.binding


@pytest.mark.parametrize("change", ["packet", "citation", "schema", "blank-title", "blank-entry", "extra"])
def test_known_completed_output_requires_journal_semantics(change):
    frozen = contract()
    payload = proposal(frozen["packet"])
    if change == "packet":
        payload["packet_id"] = "jpacket_" + "0" * 64
    elif change == "citation":
        payload["sections"][0]["entries"][0]["citation_ids"] = ["foreign"]
    elif change == "schema":
        payload["schema_version"] = "PREPARED_PAPER_REVIEW"
    elif change == "blank-title":
        payload["title"] = " \t"
    elif change == "blank-entry":
        payload["sections"][0]["entries"][0]["text"] = "\n "
    else:
        payload["extra"] = True
    provider, runtime, _ = stub_provider(frozen=frozen, value=transcript(frozen, raw=preserved_json(payload)))
    with pytest.raises(ProviderOutputError, match="journal_grok_output_invalid"):
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert len(runtime.calls) == 1


@pytest.mark.parametrize("raw", ["not JSON", '{"duplicate":1,"duplicate":2}', '{"value":NaN}', '[]'])
def test_completed_invalid_json_is_known_invalid_output(raw):
    frozen = contract()
    provider, runtime, _ = stub_provider(frozen=frozen, value=transcript(frozen, raw=raw))
    with pytest.raises(ProviderOutputError, match="journal_grok_output_invalid"):
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert len(runtime.calls) == 1


@pytest.mark.parametrize("extra", [0, 1])
def test_response_cap_is_256k_and_overflow_before_terminal_remains_unknown(extra):
    frozen = contract()
    payload = proposal(frozen["packet"])
    payload["title"] = "한글🙂 e\u0301"
    raw = preserved_json(payload)
    raw += " " * (262144 + extra - len(raw.encode("utf-8")))
    provider, runtime, _ = stub_provider(frozen=frozen, value=transcript(frozen, raw=raw))
    assert len(raw.encode("utf-8")) == provider.manifest()["max_journal_output_bytes"] + extra
    if extra:
        with pytest.raises(ProviderError, match="unknown") as error:
            provider.generate_json(ProviderRequest(**frozen["provider_request"]))
        assert type(error.value) is ProviderError
    else:
        response = provider.generate_json(ProviderRequest(**frozen["provider_request"]))
        assert journal_execution.result(frozen, response)["raw_json"] == raw
    assert len(runtime.calls) == 1


@pytest.mark.parametrize("change", ["request", "auth", "model", "session", "message-id", "tool", "terminal-id",
    "missing-terminal", "extra-client", "extra-server", "truncated", "duplicate-key", "wrong-cwd", "rpc-error"])
def test_reused_acp_validation_does_not_accept_unbound_or_ambiguous_transcript(change):
    frozen = contract()
    value = transcript(frozen)
    index = {"request": 8, "auth": 2, "model": 7, "session": 9, "message-id": 10, "tool": 9,
             "terminal-id": 11, "duplicate-key": 11, "wrong-cwd": 4, "rpc-error": 11}.get(change)
    if index is not None:
        item = json.loads(value[index]["raw_line"])
        if change == "request":
            item["params"]["prompt"][0]["text"] += "changed"
        elif change == "auth":
            item["params"]["methodId"] = "api_key"
        elif change == "model":
            item["result"]["configOptions"][0]["currentValue"] = "other"
        elif change == "session":
            item["params"]["sessionId"] = "foreign"
        elif change == "message-id":
            item["params"]["update"]["messageId"] = "other"
        elif change == "tool":
            item["params"]["update"] = dict(sessionUpdate="tool_call", toolCallId="call")
        elif change == "terminal-id":
            item["id"] = 99
        elif change == "wrong-cwd":
            item["params"]["cwd"] = "D:/other"
        elif change == "rpc-error":
            item.pop("result")
            item["error"] = dict(code=-32603, message="private diagnostic")
        value[index]["raw_line"] = preserved_json(item) + "\n"
        if change == "duplicate-key":
            value[index]["raw_line"] = value[index]["raw_line"].replace('"id":5', '"id":5,"id":5')
    elif change == "missing-terminal":
        value.pop()
    elif change == "extra-client":
        value.append(deepcopy(value[8]))
    elif change == "extra-server":
        value.append(deepcopy(value[-1]))
    else:
        value[-1]["raw_line"] = value[-1]["raw_line"].rstrip("\n")
    provider, runtime, _ = stub_provider(frozen=frozen, value=value)
    with pytest.raises(ProviderError, match="journal_grok_process_outcome_unknown") as error:
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert type(error.value) is ProviderError and "private" not in str(error.value)
    assert len(runtime.calls) == 1


def test_acp_thought_and_usage_notifications_are_not_saved_as_journal_or_token_usage():
    frozen = contract()
    value = transcript(frozen)
    for update in [dict(sessionUpdate="agent_thought_chunk", content=dict(type="text", text="private reasoning")),
                   dict(sessionUpdate="usage_update", used=900, size=10000, cost=dict(amount=1.2, currency="USD"))]:
        value.insert(9, dict(direction="server", raw_line=preserved_json(dict(jsonrpc="2.0", method="session/update",
            params=dict(sessionId="fixture-session", update=update))) + "\n"))
    provider, _, _ = stub_provider(frozen=frozen, value=value)
    response = provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert response.raw_text == json.dumps(proposal(frozen["packet"]), ensure_ascii=False, indent=2) + "\n"
    assert response.usage == {} and "private reasoning" not in preserved_json(journal_execution.result(frozen, response))


@pytest.mark.parametrize("mode,category", [("failed", "unknown"), ("citation", "output"), ("missing", "unknown"),
    ("protocol", "unknown"), ("auth", "unknown"), ("model", "unknown"), ("permission", "unknown"), ("fs", "unknown"),
    ("tool", "unknown"), ("session", "unknown"), ("message-id", "unknown"), ("wrong-id", "unknown"),
    ("duplicate-terminal", "unknown"), ("timeout", "unknown"), ("timeout-init", "unknown"), ("flood", "unknown"),
    ("truncated", "unknown"), ("invalid-utf8", "unknown"),
    ("refusal", "refused"), ("cancelled", "cancelled"), ("max_tokens", "incomplete"), ("max_turn_requests", "incomplete")])
def test_fake_child_failure_never_relaunches_or_escapes_owned_cleanup(tmp_path, mode, category):
    adapter, _ = modules()
    frozen = contract()
    root = tmp_path / "cli"
    provider = adapter.GrokJournalProvider(frozen, runtime=runtime_at(root, frozen, mode=mode),
        timeout_seconds=.2 if mode.startswith("timeout") else 30)
    with pytest.raises(ProviderError) as error:
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    if category == "output":
        assert isinstance(error.value, ProviderOutputError)
    elif category == "refused":
        assert isinstance(error.value, ProviderRefusalError)
    elif category != "unknown":
        assert isinstance(error.value, ProviderReportedFailure) and error.value.status == category
    else:
        assert type(error.value) is ProviderError and "unknown" in str(error.value)
    assert "private diagnostic" not in str(error.value)
    assert (root / "launches.log").read_text().splitlines() == ["launch"]
    assert list((root / "attempts").iterdir()) == []
    if mode in {"protocol", "auth", "model", "timeout-init", "flood"}:
        assert not (root / "prompt.json").exists()


@pytest.mark.parametrize("change", ["launcher", "workspace"])
def test_runtime_drift_is_denied_before_any_child(tmp_path, change):
    adapter, _ = modules()
    frozen = contract()
    root = tmp_path / "cli"
    provider = adapter.GrokJournalProvider(frozen, runtime=runtime_at(root, frozen))
    if change == "launcher":
        script = root / "fake_grok.py"
        script.write_text(script.read_text(encoding="utf-8") + "\n# drift", encoding="utf-8")
    else:
        (root / "attempts").write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="runtime_changed"):
        provider.validate_local_runtime()
    with pytest.raises(ValueError, match="runtime_changed"):
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert not (root / "launches.log").exists()


def test_default_dispatcher_does_not_launch_without_authority(monkeypatch):
    from app.domain.journal_dispatch import Dispatch
    from app.services import journal_dispatch
    adapter, _ = modules()
    provider, runtime, frozen = stub_provider()
    monkeypatch.setattr(journal_dispatch.admission, "replay", lambda *_: None)
    monkeypatch.setattr(journal_dispatch.repository, "detail", lambda *_: dict(contract=frozen))
    monkeypatch.setattr(journal_dispatch.admission, "admit", lambda *args, **kwargs: pytest.fail("No authority"))
    controller = adapter.journal_dispatcher(provider)
    try:
        with pytest.raises(ValueError, match="authority_unavailable"):
            controller.dispatch(frozen["topic_id"], frozen["job_id"], frozen["execution_id"],
                                Dispatch(request_id="send", expected_sequence=0), "owner")
        assert runtime.calls == [] and controller.snapshot()["workers"] == []
    finally:
        assert controller.stop()
