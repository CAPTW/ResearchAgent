"""Claude Journal uses Python fixtures only; no CLI login, account or inference."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import importlib
import json

import pytest

from app.domain import journal_execution
from app.domain.journal_exchange import preserved_json
from app.domain.temporal_memory import digest
from app.services.codex_cli_report_draft import CliExecResult
from app.services.providers.base import (ProviderError, ProviderOutputError, ProviderRefusalError,
                                        ProviderReportedFailure, ProviderRequest)
from tests import test_claude_prepared_review as paper
from tests.test_journal_review_packet import frozen_job, proposal, NOW


def modules():
    return (importlib.import_module("app.services.providers.claude_journal"),
            importlib.import_module("app.services.providers.claude_journal_plan"))


def contract(*, selected=True, **changes):
    job = frozen_job(selected=selected)
    spec = journal_execution.Prepare(**(dict(request_id="claude-journal", expected_input_digest=job["input_digest"],
        provider_name="claude_code", model="synthetic-claude-model") | changes))
    return journal_execution.prepare(job, spec, "owner", NOW)


def envelope(frozen):
    value = paper.envelope(paper.contract())
    value["structured_output"] = proposal(frozen["packet"])
    return value


def runtime_at(root, frozen, *, mode="success", raw=None):
    value = envelope(frozen)
    if mode == "failed":
        value.update(is_error=True, subtype="error_during_execution", terminal_reason="api_error", errors=["private diagnostic"])
        value.pop("structured_output")
    elif mode == "citation":
        value["structured_output"]["sections"][0]["entries"][0]["citation_ids"] = ["invented"]
    elif mode == "missing":
        value.pop("type")
    raw = json.dumps(value, ensure_ascii=False, indent=2) if raw is None else raw
    return paper.runtime_at(root, paper.contract(), mode=mode, raw=raw)


def test_claude_journal_uses_frozen_request_and_separate_journal_validation(tmp_path):
    adapter, plan = modules()
    frozen = contract()
    provider = adapter.ClaudeJournalProvider(frozen, runtime=runtime_at(tmp_path / "cli", frozen), timeout_seconds=30)
    response = provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert response.provider_name == "claude_code"
    assert response.raw_text == preserved_json(proposal(frozen["packet"]))
    assert journal_execution.result(frozen, response)["proposal"] == proposal(frozen["packet"])
    assert provider.manifest()["schema_version"] == "CLAUDE_JOURNAL_CLI_V1"
    assert provider.binding.adapter_id == plan.ADAPTER_ID == "xrml-claude-frozen-journal"
    assert (tmp_path / "cli" / "launches.log").read_text().splitlines() == ["launch"]


class StubRuntime:
    """Envelope tests avoid additional process launches; never a live runtime."""

    def __init__(self, raw, *, code=0, timed_out=False, last_message=""):
        self.result = CliExecResult(code, raw, "private stderr", timed_out, last_message)
        self.calls = []
        self.checks = 0

    def identity(self):
        return {key: "a" * 64 for key in ("launcher_sha256", "environment_sha256", "workspace_sha256", "config_dir_sha256")}

    def verify(self):
        self.checks += 1

    def run(self, stdin, schema, **settings):
        self.verify()
        self.calls.append((stdin, schema, settings))
        return self.result


def stub_provider(*, frozen=None, raw=None, code=0, timed_out=False, last_message="", timeout=30):
    adapter, _ = modules()
    frozen = frozen or contract()
    runtime = StubRuntime(preserved_json(envelope(frozen)) if raw is None else raw,
                          code=code, timed_out=timed_out, last_message=last_message)
    return adapter.ClaudeJournalProvider(frozen, runtime=runtime, timeout_seconds=timeout), runtime, frozen


@pytest.mark.parametrize("selected", [False, True])
def test_real_fake_process_uses_exact_cli_profile_and_extracted_json_provenance(tmp_path, selected):
    adapter, _ = modules()
    frozen = contract(selected=selected)
    value = envelope(frozen)
    value["structured_output"]["title"] = " e\u0301 한글🙂\u0085\u2028\u2029 preserved "
    raw_envelope = json.dumps(value, ensure_ascii=False, indent=3)
    root = tmp_path / "cli"
    runtime = runtime_at(root, frozen, raw=raw_envelope)
    provider = adapter.ClaudeJournalProvider(frozen, runtime=runtime, timeout_seconds=30)
    response = provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    stdin = (root / "stdin.json").read_bytes().decode("utf-8")
    args = json.loads((root / "argv.json").read_text(encoding="utf-8"))
    assert stdin == preserved_json(frozen["provider_request"])
    assert args[args.index("--model") + 1] == frozen["specification"]["model"]
    assert args[args.index("--system-prompt") + 1] == frozen["provider_request"]["instructions"]
    assert args[args.index("--json-schema") + 1] == preserved_json(frozen["provider_request"]["schema"])
    assert args[args.index("--tools") + 1] == "" and args[args.index("--setting-sources") + 1] == ""
    assert args[args.index("--disallowedTools") + 1] == "mcp__*"
    assert json.loads(args[args.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert {"--safe-mode", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence", "--no-chrome"} <= set(args)
    assert {"--bare", "--dangerously-skip-permissions", "--resume", "--continue"}.isdisjoint(args)
    assert response.raw_text == preserved_json(value["structured_output"]) and response.raw_text != raw_envelope
    assert response.model == "not_reported" and response.response_id == "fixture-session"
    assert response.usage == dict(input_tokens=4, output_tokens=7)
    saved = journal_execution.result(frozen, response)
    assert saved["raw_json"] == response.raw_text
    assert saved["proposal"]["title"] == value["structured_output"]["title"]
    assert "raw_envelope" not in saved and "total_cost_usd" not in str(saved)
    assert saved["validation"]["semantic_support"] == "NOT_ASSESSED"
    assert saved["validation"]["independent_live_proof"] == "NOT_VERIFIED"
    manifest = provider.manifest()
    assert manifest["request_sha256"] == manifest["stdin_sha256"] == journal_execution.exchange.sha(stdin)
    assert manifest["stdin_bytes"] == len(stdin.encode("utf-8"))
    assert manifest["schema_sha256"] == journal_execution.exchange.sha(args[args.index("--json-schema") + 1])
    assert manifest["system_prompt_sha256"] == journal_execution.exchange.sha(frozen["provider_request"]["instructions"])
    assert manifest["runtime_identity"] == runtime.identity()
    assert manifest["fixed_environment"] == json.loads((root / "policy-env.json").read_text())
    assert manifest["contract_sha256"] == journal_execution.exchange.sha(preserved_json(frozen))
    assert manifest["raw_journal_encoding"] == "EXTRACTED_CANONICAL_JSON_V1"
    assert manifest["raw_envelope_retained"] is False
    assert manifest["journal_result_storage"] == "EXTRACTED_STRUCTURED_OUTPUT_ONLY"
    for field in ("output_token_limit_enforced", "temperature_enforced", "cost_limit_enforced", "remote_call_count_enforced", "automatic_retry"):
        assert manifest[field] is False
    assert manifest["managed_hooks"] == "NOT_ISOLATED_NOT_LIVE_VERIFIED"
    assert manifest["model_identity"] == "REQUESTED_NOT_INDEPENDENTLY_VERIFIED"
    assert manifest["billing_route"] == manifest["live_execution"] == "NOT_VERIFIED"
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
    assert runtime.calls[0][0] == preserved_json(original["provider_request"])
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


@pytest.mark.parametrize("change", ["provider", "model", "schema", "packet-schema", "prompt", "execution", "packet", "tokens", "canonical-instructions", "fingerprint", "timeout", "bool-timeout"])
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
        settings["runtime_identity"]["config_dir_sha256"] = "invalid"
    else:
        settings["timeout_seconds"] = True if change == "bool-timeout" else float("inf")
    with pytest.raises(ValueError, match="configuration_invalid"):
        plan.build_plan(frozen, **settings)
    assert runtime.calls == []


@pytest.mark.parametrize("field", ["binding", "model", "contract", "request"])
def test_binding_is_exact_and_new_limits_require_new_binding(field):
    provider, runtime, frozen = stub_provider()
    bound = provider.binding
    model = provider.model
    if field == "binding":
        bound = bound.model_copy(update={"adapter_id": "paper-adapter"})
    elif field == "model":
        model = "other"
    elif field == "contract":
        frozen["actor_id"] = "other"
    else:
        frozen["provider_request"]["metadata"]["execution_id"] = "other"
    with pytest.raises(ValueError, match="adapter_mismatch"):
        provider.validate_journal_binding(frozen, model, bound)
    assert runtime.calls == []
    original, _, _ = stub_provider()
    changed, _, _ = stub_provider(timeout=31)
    assert original.binding != changed.binding


@pytest.mark.parametrize("change", ["packet", "citation", "schema", "blank-title", "blank-entry", "extra", "missing-output", "oversized"])
def test_known_completed_output_requires_journal_semantics(change):
    frozen = contract()
    data = envelope(frozen)
    payload = data["structured_output"]
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
        data.pop("structured_output")
    else:
        payload["title"] = "x" * 512_001
    provider, runtime, _ = stub_provider(frozen=frozen, raw=preserved_json(data))
    with pytest.raises(ProviderOutputError, match="journal_claude_output_invalid"):
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert len(runtime.calls) == 1


@pytest.mark.parametrize("case,category", [("failed", "failed"), ("max-turns", "incomplete"), ("schema-retry", "incomplete"),
    ("cancelled", "cancelled"), ("refusal", "refused"), ("max-output", "incomplete"), ("nonzero", "unknown"),
    ("duplicate", "unknown"), ("nonfinite", "unknown"), ("missing-terminal", "unknown"), ("extra-envelope", "unknown"),
    ("permissions", "unknown"), ("tools", "unknown"), ("conflicting-cancel", "unknown"), ("timeout", "unknown"), ("last-message", "unknown")])
def test_shared_terminal_grammar_keeps_known_and_unknown_distinct(case, category):
    frozen = contract()
    data, code = envelope(frozen), 0
    if case in {"failed", "max-turns", "schema-retry", "cancelled"}:
        code = 1
        data.update(is_error=True, errors=["private provider diagnostic"])
        data.pop("structured_output")
        data["subtype"] = {"max-turns": "error_max_turns", "schema-retry": "error_max_structured_output_retries"}.get(case, "error_during_execution")
        data["terminal_reason"] = {"max-turns": "max_turns", "schema-retry": "max_structured_output_retries", "cancelled": "aborted_streaming"}.get(case, "api_error")
    elif case in {"refusal", "max-output"}:
        data["stop_reason"] = "refusal" if case == "refusal" else "max_tokens"
        data.pop("structured_output")
    elif case == "nonzero":
        code = 7
    elif case == "missing-terminal":
        data.pop("type")
    elif case == "extra-envelope":
        data["unknown"] = True
    elif case == "permissions":
        data["permission_denials"] = [{"tool_name": "Read"}]
    elif case == "tools":
        data["usage"]["server_tool_use"] = {"web_search_requests": 1}
    elif case == "conflicting-cancel":
        data["terminal_reason"] = "aborted_streaming"
    raw = preserved_json(data)
    if case == "duplicate":
        raw = raw[:-1] + ',"subtype":"success"}'
    elif case == "nonfinite":
        raw = raw.replace('"duration_ms":10', '"duration_ms":NaN')
    provider, runtime, _ = stub_provider(frozen=frozen, raw=raw, code=code,
        timed_out=case == "timeout", last_message="unexpected" if case == "last-message" else "")
    with pytest.raises(ProviderError) as failure:
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    if category == "refused":
        assert isinstance(failure.value, ProviderRefusalError)
    elif category == "unknown":
        assert type(failure.value) is ProviderError and "unknown" in str(failure.value)
    else:
        assert isinstance(failure.value, ProviderReportedFailure) and failure.value.status == category
    assert "private" not in str(failure.value) and len(runtime.calls) == 1


@pytest.mark.parametrize("change", ["launcher", "config-dir", "workspace"])
def test_existing_runtime_guard_rejects_local_drift_before_any_child(tmp_path, change):
    adapter, _ = modules()
    frozen = contract()
    root = tmp_path / "cli"
    runtime = runtime_at(root, frozen)
    provider = adapter.ClaudeJournalProvider(frozen, runtime=runtime)
    if change == "launcher":
        script = root / "fake_claude.py"
        script.write_text(script.read_text(encoding="utf-8") + "\n# drift", encoding="utf-8")
    elif change == "config-dir":
        (root / "empty-config").rename(root / "preserved-config")
        (root / "empty-config").mkdir()
    else:
        (root / "attempts").write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="runtime_changed"):
        provider.validate_local_runtime()
    with pytest.raises(ValueError, match="runtime_changed"):
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert not (root / "launches.log").exists()


@pytest.mark.parametrize("mode,category", [("failed", "failed"), ("citation", "output"), ("missing", "unknown"), ("flood", "unknown")])
def test_fake_process_errors_do_not_retry_or_leave_attempt_files(tmp_path, mode, category):
    adapter, _ = modules()
    frozen = contract()
    root = tmp_path / "cli"
    provider = adapter.ClaudeJournalProvider(frozen, runtime=runtime_at(root, frozen, mode=mode), timeout_seconds=30)
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


def test_default_dispatcher_never_uses_explicit_runtime_without_authority(monkeypatch):
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


def test_windows_crash_with_error_envelope_remains_unknown():
    frozen = contract()
    value = envelope(frozen)
    value.update(is_error=True, subtype="error_during_execution", terminal_reason="api_error", errors=["private diagnostic"])
    value.pop("structured_output")
    provider, runtime, _ = stub_provider(frozen=frozen, raw=preserved_json(value), code=3221225477)
    with pytest.raises(ProviderError, match="unknown") as failure:
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert type(failure.value) is ProviderError
    assert len(runtime.calls) == 1 and "private" not in str(failure.value)


@pytest.mark.parametrize("code", [-9, 2, 7, 3221225477, True, 0.0, None])
@pytest.mark.parametrize("terminal", ["failed", "cancelled", "incomplete"])
def test_abnormal_process_code_cannot_turn_terminal_prose_into_known_outcome(code, terminal):
    frozen = contract()
    value = envelope(frozen)
    value.update(is_error=True, errors=["private diagnostic"],
        subtype="error_max_turns" if terminal == "incomplete" else "error_during_execution",
        terminal_reason={"failed": "api_error", "cancelled": "aborted_streaming", "incomplete": "max_turns"}[terminal])
    value.pop("structured_output")
    provider, runtime, _ = stub_provider(frozen=frozen, raw=preserved_json(value), code=code)
    with pytest.raises(ProviderError, match="unknown") as failure:
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    assert type(failure.value) is ProviderError
    assert len(runtime.calls) == 1 and "private" not in str(failure.value)
