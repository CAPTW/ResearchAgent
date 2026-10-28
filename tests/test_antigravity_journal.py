"""Antigravity Journal fixtures use Python children, never an installed CLI."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import importlib
import json
from pathlib import Path
import tempfile

import pytest

from app.domain import journal_execution
from app.domain.journal_exchange import preserved_json, sha
from app.domain.temporal_memory import digest
from app.services.codex_cli_report_draft import CliExecResult
from app.services.providers.antigravity_prepared_review_process import CapturedAttempt
from app.services.providers.base import ProviderError, ProviderOutputError, ProviderReportedFailure, ProviderRequest
from tests import test_antigravity_prepared_review as paper
from tests.test_journal_review_packet import frozen_job, proposal, NOW


def modules():
    return (importlib.import_module("app.services.providers.antigravity_journal"),
            importlib.import_module("app.services.providers.antigravity_journal_plan"))


def contract(*, selected=True, **changes):
    job = frozen_job(selected=selected)
    spec = journal_execution.Prepare(**(dict(request_id="antigravity-journal", expected_input_digest=job["input_digest"],
        provider_name="antigravity_cli", model="synthetic-antigravity-model") | changes))
    return journal_execution.prepare(job, spec, "owner", NOW)


def events(frozen):
    value = paper.events(paper.contract())
    value[0]["init"].update(model=frozen["specification"]["model"], json_schema=frozen["provider_request"]["schema"])
    payload = proposal(frozen["packet"])
    raw = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    value[-1]["result"].update(response=raw, structured_output=payload, json_schema=frozen["provider_request"]["schema"])
    value[2]["step_update"]["text_delta"] = raw[:15]
    value[3]["step_update"]["text_delta"] = raw[15:]
    return value


def runtime_at(root, frozen, mode="success", *, stream=None):
    value = events(frozen)
    if mode == "failed":
        value = value[:2] + [value[-1]]
        value[-1]["result"].update(status="ERROR", response="", error="private diagnostic")
        value[-1]["result"].pop("structured_output")
    elif mode == "citation":
        value[-1]["result"]["structured_output"]["sections"][0]["entries"][0]["citation_ids"] = ["invented"]
        value[-1]["result"]["response"] = preserved_json(value[-1]["result"]["structured_output"])
    elif mode == "missing":
        value.pop()
    stream = "\n".join(preserved_json(item) for item in value) + "\n" if stream is None else stream
    return paper.runtime_at(root, paper.contract(), mode=mode, stream=stream)


def test_antigravity_journal_uses_frozen_request_and_separate_journal_validation(tmp_path):
    adapter, plan = modules()
    frozen = contract()
    provider = adapter.AntigravityJournalProvider(frozen, runtime=runtime_at(tmp_path / "cli", frozen), timeout_seconds=30)
    response = provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert response.provider_name == "antigravity_cli"
    assert response.raw_text == events(frozen)[-1]["result"]["response"]
    assert journal_execution.result(frozen, response)["proposal"] == proposal(frozen["packet"])
    assert provider.manifest()["schema_version"] == "ANTIGRAVITY_JOURNAL_CLI_V1"
    assert provider.binding.adapter_id == plan.ADAPTER_ID == "xrml-antigravity-frozen-journal"
    assert (tmp_path / "cli" / "launches.log").read_text().splitlines() == ["launch"]


ATTEMPT = str(Path(tempfile.gettempdir()) / "synthetic-antigravity-journal" / "antigravity-review-fixture")


def stream_text(value, *, cwd=ATTEMPT):
    value = deepcopy(value)
    value[0]["init"]["cwd"] = cwd
    return "\n".join(preserved_json(item) for item in value) + "\n"


class StubRuntime:
    def __init__(self, raw, *, code=0, timed_out=False, last_message="", cwd=ATTEMPT):
        self.result = CapturedAttempt(CliExecResult(code, raw, "private stderr", timed_out, last_message), cwd)
        self.calls = []
        self.checks = 0

    def identity(self):
        return {key: "a" * 64 for key in ("launcher_sha256", "environment_sha256", "workspace_sha256")}

    def verify(self):
        self.checks += 1

    def run(self, stdin, schema, **settings):
        self.verify()
        self.calls.append((stdin, schema, settings))
        return self.result


def stub_provider(*, frozen=None, raw=None, code=0, timed_out=False, last_message="", timeout=30):
    adapter, _ = modules()
    frozen = frozen or contract()
    runtime = StubRuntime(stream_text(events(frozen)) if raw is None else raw, code=code,
                          timed_out=timed_out, last_message=last_message)
    return adapter.AntigravityJournalProvider(frozen, runtime=runtime, timeout_seconds=timeout), runtime, frozen


@pytest.mark.parametrize("selected", [False, True])
def test_fake_process_binds_exact_profile_input_and_result_response(tmp_path, selected):
    adapter, _ = modules()
    frozen = contract(selected=selected)
    value = events(frozen)
    value[-1]["result"]["structured_output"]["title"] = " e\u0301 한글🙂\u0085\u2028\u2029 preserved "
    raw = json.dumps(value[-1]["result"]["structured_output"], ensure_ascii=False, indent=3) + " \n"
    value[-1]["result"]["response"] = raw
    # Step telemetry is deliberately different from the authoritative response.
    value[2]["step_update"]["text_delta"] = "intermediate telemetry"
    value[3]["step_update"]["text_delta"] = "not the saved Journal"
    root = tmp_path / "cli"
    runtime = runtime_at(root, frozen, stream="\n".join(preserved_json(item) for item in value) + "\n")
    provider = adapter.AntigravityJournalProvider(frozen, runtime=runtime, timeout_seconds=30)
    response = provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    stdin = (root / "stdin.ndjson").read_bytes().decode("utf-8")
    request_json = preserved_json(frozen["provider_request"])
    assert stdin == preserved_json(dict(event="user", message=dict(content=request_json))) + "\n"
    args = json.loads((root / "argv.json").read_text())
    for flag, expected in (("--model", frozen["specification"]["model"]), ("--input-format", "stream-json"),
                           ("--output-format", "stream-json"), ("--agent", "xrml-paper-review-v1"),
                           ("--effort", "low"), ("--print-timeout", "30s")):
        assert args[args.index(flag) + 1] == expected
    assert "--sandbox" in args and "--dangerously-skip-permissions" not in args and "-p" not in args
    agent = (root / "agent.md").read_bytes().decode("utf-8")
    schema = (root / "schema.json").read_bytes().decode("utf-8")
    assert schema == preserved_json(frozen["provider_request"]["schema"])
    assert agent.endswith(frozen["provider_request"]["instructions"] + "\n")
    assert "description: Review only the supplied paper request." in agent
    for restriction in ("tools: []", "commandExecutionPolicy: off", "inheritCustomizations: false", "excludeDefaultComponents: true"):
        assert restriction in agent
    assert response.raw_text == raw and response.raw_text != (root / "stdout.ndjson").read_bytes().decode("utf-8")
    assert response.model == "not_reported" and response.response_id == "fixture-session"
    assert response.usage == dict(input_tokens=4, output_tokens=7, total_tokens=11)
    saved = journal_execution.result(frozen, response)
    assert saved["raw_json"] == raw and saved["proposal"]["title"] == value[-1]["result"]["structured_output"]["title"]
    assert "raw_stdout" not in saved and saved["validation"]["semantic_support"] == "NOT_ASSESSED"
    assert saved["validation"]["independent_live_proof"] == "NOT_VERIFIED"
    manifest = provider.manifest()
    assert manifest["contract_sha256"] == sha(preserved_json(frozen)) and manifest["contract_digest"] == digest(frozen)
    assert manifest["request_sha256"] == sha(request_json) != manifest["stdin_sha256"] == sha(stdin)
    assert manifest["stdin_bytes"] == len(stdin.encode("utf-8"))
    assert manifest["schema_sha256"] == sha(schema) and manifest["agent_sha256"] == sha(agent)
    assert manifest["system_prompt_sha256"] == sha(frozen["provider_request"]["instructions"])
    assert manifest["runtime_identity"] == runtime.identity()
    assert manifest["fixed_environment"] == json.loads((root / "policy-env.json").read_text())
    assert manifest["agent_profile"] == "EXISTING_PAPER_PROFILE_WITH_FROZEN_JOURNAL_INSTRUCTIONS"
    assert manifest["max_journal_output_bytes"] == 262144
    assert manifest["raw_journal_encoding"] == "RESULT_RESPONSE_JSON_UTF8_V1"
    assert manifest["journal_result_storage"] == "RESULT_RESPONSE_ONLY" and manifest["raw_envelope_retained"] is False
    for field in ("output_token_limit_enforced", "temperature_enforced", "cost_limit_enforced", "automatic_retry",
                  "xrml_process_relaunch", "remote_call_count_enforced", "output_rejection_prevents_side_effects"):
        assert manifest[field] is False
    for field in ("managed_hooks", "global_config_and_mcp"):
        assert manifest[field] == "NOT_ISOLATED_NOT_LIVE_VERIFIED"
    assert manifest["windows_sandbox"] == "REQUESTED_NOT_LIVE_VERIFIED"
    assert manifest["billing_route"] == manifest["live_execution"] == "NOT_VERIFIED"
    assert manifest["model_identity"] == "REQUESTED_NOT_INDEPENDENTLY_VERIFIED"
    assert provider.binding.command_shape_sha256 == digest(manifest)
    assert (root / "launches.log").read_text().splitlines() == ["launch"]
    assert list((root / "attempts").iterdir()) == []
    if selected:
        assert "e\u0301 한글🙂" in stdin


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
    assert json.loads(runtime.calls[0][0])["message"]["content"] == preserved_json(original["provider_request"])
    assert "--unsafe" not in provider.manifest()["argv_shape"]
    assert provider.manifest()["runtime_identity"]["launcher_sha256"] == "a" * 64
    assert provider.is_configured() and provider.describe_status()["live_call_enabled"] is False
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
    runtime = StubRuntime("")
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


@pytest.mark.parametrize("change", ["packet", "citation", "schema", "blank-title", "blank-entry", "extra", "missing-output", "response-mismatch"])
def test_known_completed_output_requires_journal_semantics(change):
    frozen = contract()
    data = events(frozen)
    result = data[-1]["result"]
    payload = result["structured_output"]
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
    elif change == "extra":
        payload["extra"] = True
    elif change == "missing-output":
        result.pop("structured_output")
    if change != "response-mismatch":
        result["response"] = preserved_json(payload)
    else:
        result["response"] = "{}"
    provider, runtime, _ = stub_provider(frozen=frozen, raw=stream_text(data))
    with pytest.raises(ProviderOutputError, match="journal_antigravity_output_invalid"):
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert len(runtime.calls) == 1


@pytest.mark.parametrize("extra", [0, 1])
def test_effective_response_utf8_byte_limit_is_256k_not_journal_512k(extra):
    frozen = contract()
    value = events(frozen)
    value[-1]["result"]["structured_output"]["title"] = "한글🙂 e\u0301"
    raw = preserved_json(value[-1]["result"]["structured_output"])
    raw += " " * (262144 + extra - len(raw.encode("utf-8")))
    value[-1]["result"]["response"] = raw
    provider, runtime, _ = stub_provider(frozen=frozen, raw=stream_text(value))
    assert len(raw.encode("utf-8")) == provider.manifest()["max_journal_output_bytes"] + extra
    if extra:
        with pytest.raises(ProviderOutputError):
            provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    else:
        response = provider.generate_json(ProviderRequest(**frozen["provider_request"]))
        assert journal_execution.result(frozen, response)["raw_json"] == raw
    assert len(runtime.calls) == 1


@pytest.mark.parametrize("change", ["init-model", "init-agent", "init-cwd", "init-tools", "init-permission", "init-schema",
    "step-identity", "step-tool", "step-subagent", "duplicate-step", "missing-init", "missing-terminal", "duplicate-terminal",
    "result-identity", "result-schema", "result-turns", "result-running", "unfinished-step", "nonzero-success",
    "duplicate-key", "nonfinite", "timeout", "last-message", "unknown-event"])
def test_stream_identity_or_uncertain_completion_is_unknown(change):
    frozen = contract()
    value, code = events(frozen), 0
    value[0]["init"]["cwd"] = ATTEMPT
    if change.startswith("init-"):
        key = {"schema": "json_schema", "permission": "permission_mode"}.get(change[5:], change[5:])
        value[0]["init"][key] = [] if key == "json_schema" else ["read_file"] if key == "tools" else "wrong"
    elif change == "step-identity":
        value[3]["step_update"]["conversation_id"] = "foreign"
    elif change in {"step-tool", "step-subagent"}:
        value[3]["step_update"]["step_type"] = change[5:]
    elif change == "duplicate-step":
        value.insert(4, deepcopy(value[3]))
    elif change == "missing-init":
        value.pop(0)
    elif change == "missing-terminal":
        value.pop()
    elif change == "duplicate-terminal":
        value.append(deepcopy(value[-1]))
    elif change == "result-identity":
        value[-1]["result"]["conversation_id"] = "foreign"
    elif change == "result-schema":
        value[-1]["result"]["json_schema"] = {}
    elif change == "result-turns":
        value[-1]["result"]["num_turns"] = 2
    elif change == "result-running":
        value[-1]["result"]["status"] = "RUNNING"
    elif change == "unfinished-step":
        value[3]["step_update"]["state"] = "ACTIVE"
    elif change == "nonzero-success":
        code = 1
    elif change == "unknown-event":
        value.insert(1, dict(event="new_kind"))
    raw = "\n".join(preserved_json(item) for item in value) + "\n"
    if change == "duplicate-key":
        raw = raw.replace('"num_turns":1', '"num_turns":1,"num_turns":1')
    elif change == "nonfinite":
        raw = raw.replace('"num_turns":1', '"num_turns":NaN')
    provider, runtime, _ = stub_provider(frozen=frozen, raw=raw, code=code,
        timed_out=change == "timeout", last_message="unexpected" if change == "last-message" else "")
    with pytest.raises(ProviderError, match="unknown") as failure:
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert type(failure.value) is ProviderError and "private" not in str(failure.value)
    assert len(runtime.calls) == 1


def test_valid_success_without_optional_step_telemetry():
    frozen = contract()
    value = events(frozen)
    provider, _, _ = stub_provider(frozen=frozen, raw=stream_text([value[0], value[-1]]))
    response = provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert response.raw_text == value[-1]["result"]["response"]


@pytest.mark.parametrize("status,known", [("ERROR", "failed"), ("CANCELED", "cancelled"), ("INTERRUPTED", "cancelled")])
def test_normal_error_exit_has_known_terminal_outcome(status, known):
    frozen = contract()
    value = events(frozen)
    value[-1]["result"].update(status=status, response="", error="private diagnostic")
    value[-1]["result"].pop("structured_output")
    provider, runtime, _ = stub_provider(frozen=frozen, raw=stream_text(value), code=1)
    with pytest.raises(ProviderReportedFailure) as failure:
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert failure.value.status == known and "private" not in str(failure.value)
    assert len(runtime.calls) == 1


@pytest.mark.parametrize("code", [-9, 2, 7, 255, 3221225477, True, 0.0, None])
@pytest.mark.parametrize("status", ["ERROR", "CANCELED", "INTERRUPTED"])
def test_abnormal_process_exit_keeps_error_envelope_outcome_unknown(code, status):
    frozen = contract()
    value = events(frozen)
    value[-1]["result"].update(status=status, response="", error="private diagnostic")
    value[-1]["result"].pop("structured_output")
    provider, runtime, _ = stub_provider(frozen=frozen, raw=stream_text(value), code=code)
    with pytest.raises(ProviderError, match="unknown") as failure:
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert type(failure.value) is ProviderError and "private" not in str(failure.value)
    assert len(runtime.calls) == 1


@pytest.mark.parametrize("change", ["launcher", "workspace"])
def test_runtime_drift_is_denied_before_any_child(tmp_path, change):
    adapter, _ = modules()
    frozen = contract()
    root = tmp_path / "cli"
    runtime = runtime_at(root, frozen)
    provider = adapter.AntigravityJournalProvider(frozen, runtime=runtime)
    if change == "launcher":
        script = root / "fake_antigravity.py"
        script.write_text(script.read_text(encoding="utf-8") + "\n# drift", encoding="utf-8")
    else:
        (root / "attempts").write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="runtime_changed"):
        provider.validate_local_runtime()
    with pytest.raises(ValueError, match="runtime_changed"):
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert not (root / "launches.log").exists()


@pytest.mark.parametrize("mode,category", [("failed", "failed"), ("citation", "output"), ("missing", "unknown"), ("flood", "unknown")])
def test_fake_process_failures_never_retry_and_clean_attempt_workspace(tmp_path, mode, category):
    adapter, _ = modules()
    frozen = contract()
    root = tmp_path / "cli"
    provider = adapter.AntigravityJournalProvider(frozen, runtime=runtime_at(root, frozen, mode=mode), timeout_seconds=30)
    with pytest.raises(ProviderError) as failure:
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    if category == "failed":
        assert isinstance(failure.value, ProviderReportedFailure) and failure.value.status == "failed"
    elif category == "output":
        assert isinstance(failure.value, ProviderOutputError)
    else:
        assert type(failure.value) is ProviderError and "unknown" in str(failure.value)
    assert (root / "launches.log").read_text().splitlines() == ["launch"]
    assert list((root / "attempts").iterdir()) == []


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
