"""Explicit fake child processes only; no Claude binary, login or live qualification."""
from copy import deepcopy
from dataclasses import replace
import importlib
import json
import os
from pathlib import Path
import sys

import pytest

from app.domain import prepared_review
from app.domain.journal_exchange import preserved_json
from app.services.providers.base import ProviderError, ProviderOutputError, ProviderRefusalError, ProviderReportedFailure, ProviderRequest
from tests import test_prepared_review as prior


def modules():
    return tuple(importlib.import_module("app.services.providers." + name) for name in (
        "claude_prepared_review", "claude_prepared_review_plan", "claude_prepared_review_process"))


def contract():
    return prepared_review.prepare(prior.packet(), provider_name="claude_code", model="fixture-model")


def envelope(frozen):
    # Official SDK ResultMessage/message_parser wire fields, not a Codex event.
    return dict(type="result", subtype="success", is_error=False, duration_ms=10,
        duration_api_ms=9, num_turns=1, session_id="fixture-session", result="",
        structured_output=prior.response(frozen).payload, total_cost_usd=0.0,
        usage=dict(input_tokens=4, output_tokens=7), permission_denials=[],
        stop_reason="end_turn", terminal_reason="completed")


def runtime_at(root, frozen, mode="success", *, raw=None):
    _, _, process = modules()
    root.mkdir()
    value = envelope(frozen)
    if mode == "failed":
        value.update(is_error=True, subtype="error_during_execution", terminal_reason="api_error", errors=["private diagnostic"])
        value.pop("structured_output")
    elif mode == "citation":
        value["structured_output"]["cited_evidence_span_ids"] = ["invented"]
    elif mode == "missing":
        value.pop("type")
    raw = json.dumps(value, ensure_ascii=False, indent=2) if raw is None else raw
    (root / "envelope.json").write_text(raw, encoding="utf-8", newline="")
    script = root / "fake_claude.py"
    script.write_text('''import json,os,sys,time
from pathlib import Path
root=Path(__file__).parent
stdin=sys.stdin.read()
(root/'stdin.json').write_text(stdin,encoding='utf-8',newline='')
(root/'argv.json').write_text(json.dumps(sys.argv[1:]),encoding='utf-8')
(root/'policy-env.json').write_text(json.dumps({key:os.environ.get(key) for key in ['DISABLE_UPDATES','CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC']}),encoding='utf-8')
with (root/'launches.log').open('a') as marker: marker.write('launch\\n')
mode=MODE
if mode=='timeout': time.sleep(30)
if mode=='flood': print('x'*1100000); sys.exit(0)
if mode=='stderr': sys.stderr.write('private diagnostic'); sys.exit(7)
raw=(root/'envelope.json').read_text(encoding='utf-8')
sys.stdout.buffer.write(raw.encode('utf-8'))
sys.exit(1 if mode in {'error','failed'} else 7 if mode=='nonzero' else 0)
'''.replace("MODE", repr(mode)), encoding="utf-8")
    config = root / "empty-config"
    config.mkdir()
    environment = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
    environment.update(CLAUDE_CONFIG_DIR=str(config), TEMP=str(root), TMP=str(root), PYTHONIOENCODING="utf-8")
    return process.ClaudeRuntime((str(Path(sys.executable).resolve()), str(script)), environment, root / "attempts")


def provider_at(root, *, mode="success", raw=None, timeout=30):
    adapter, _, _ = modules()
    frozen = contract()
    runtime = runtime_at(root, frozen, mode=mode, raw=raw)
    return adapter.ClaudePreparedReviewProvider(frozen, runtime=runtime, timeout_seconds=timeout), frozen


def test_fake_process_freezes_exact_request_and_separate_envelope_review_provenance(tmp_path):
    adapter, plan, _ = modules()
    provider, frozen = provider_at(tmp_path / "cli")
    record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    assert (tmp_path / "cli" / "stdin.json").read_text(encoding="utf-8") == preserved_json(frozen["provider_request"])
    args = json.loads((tmp_path / "cli" / "argv.json").read_text(encoding="utf-8"))
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--setting-sources") + 1] == ""
    assert args[args.index("--disallowedTools") + 1] == "mcp__*"
    assert json.loads(args[args.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert "--strict-mcp-config" in args and "--safe-mode" in args and "--bare" not in args
    assert args[args.index("--system-prompt") + 1] == frozen["provider_request"]["instructions"]
    assert json.loads(args[args.index("--json-schema") + 1]) == frozen["provider_request"]["schema"]
    marker = record["validation"]["execution"]
    raw = (tmp_path / "cli" / "envelope.json").read_text(encoding="utf-8")
    assert marker["raw_envelope"] == raw
    assert marker["envelope_sha256"] == prepared_review.sha(raw)
    response = record["validation"]["prepared_execution"]["response"]
    assert marker["review_sha256"] == prepared_review.sha(response["raw_text"])
    assert marker["review_encoding"] == "EXTRACTED_CANONICAL_JSON_V1"
    assert response["payload"] == json.loads(raw)["structured_output"]
    assert response["model"] == "not_reported" and response["response_id"] == "fixture-session"
    assert response["usage"] == dict(input_tokens=4, output_tokens=7)
    assert marker["authority"] == "NOT_ESTABLISHED" and marker["live_execution"] == "NOT_VERIFIED"
    assert record["validation"]["prepared_execution"]["full_document_review"] == "NOT_ESTABLISHED"
    plan.validate_transport(record, frozen["packet"])
    manifest = provider.manifest()
    assert manifest["managed_hooks"] == "NOT_ISOLATED_NOT_LIVE_VERIFIED"
    assert manifest["internal_retries"] == "CLI_MANAGED_NOT_LIVE_VERIFIED"
    assert manifest["automatic_retry"] is False and manifest["remote_call_count_enforced"] is False
    assert manifest["output_token_limit_enforced"] is False
    manifest["model"] = "changed"
    assert provider.manifest()["model"] == "fixture-model"
    assert provider.describe_status()["live_call_enabled"] is False
    assert adapter.ClaudePreparedReviewProvider.__name__ in type(provider).__name__
    assert (tmp_path / "cli" / "launches.log").read_text().splitlines() == ["launch"]
    assert list((tmp_path / "cli" / "attempts").iterdir()) == []


def test_child_disables_updates_and_nonessential_traffic_with_frozen_policy(tmp_path):
    provider, frozen = provider_at(tmp_path / "cli")
    provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    expected = {"DISABLE_UPDATES": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    assert json.loads((tmp_path / "cli" / "policy-env.json").read_text()) == expected
    assert provider.manifest()["fixed_environment"] == expected


@pytest.mark.parametrize("key", ["DISABLE_UPDATES", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"])
def test_fixed_process_policy_cannot_be_disabled_by_caller(tmp_path, key):
    _, _, process = modules()
    config = tmp_path / "config"
    config.mkdir()
    with pytest.raises(ValueError, match="runtime_invalid"):
        process.ClaudeRuntime((str(Path(sys.executable).resolve()),), {"CLAUDE_CONFIG_DIR": str(config), key: "0"}, tmp_path / "attempts")
    assert not (tmp_path / "attempts").exists()


@pytest.mark.parametrize("change,category", [
    ("citation", "output"), ("missing-output", "output"), ("extra-output", "output"),
    ("duplicate", "unknown"), ("nan", "unknown"), ("extra-envelope", "unknown"),
    ("unknown-subtype", "unknown"), ("wrong-type", "unknown"), ("permission", "unknown"),
    ("injected-origin", "unknown"), ("max-turns", "incomplete"), ("api-error", "failed"),
    ("schema-retries", "incomplete"), ("cancelled", "cancelled"), ("refused", "refused"),
    ("max-output", "incomplete"), ("tool-usage", "unknown"), ("error-success-contradiction", "unknown"),
])
def test_terminal_and_malformed_results_never_relaunch(tmp_path, change, category):
    frozen = contract()
    data = envelope(frozen)
    mode = "success"
    if change == "citation":
        data["structured_output"]["cited_evidence_span_ids"] = ["invented"]
    elif change == "missing-output":
        del data["structured_output"]
    elif change == "extra-output":
        data["structured_output"]["other"] = 1
    elif change == "extra-envelope":
        data["unrecognized_semantics"] = True
    elif change == "unknown-subtype":
        data["subtype"] = "new-success"
    elif change == "wrong-type":
        data["is_error"] = 0
    elif change == "permission":
        data["permission_denials"] = [{"tool_name": "Read"}]
    elif change == "injected-origin":
        data["origin"] = {"kind": "task-notification"}
    elif change == "tool-usage":
        data["usage"]["server_tool_use"] = {"web_search_requests": 1}
    elif change == "error-success-contradiction":
        data["errors"] = ["private diagnostic"]
    elif change in {"refused", "max-output"}:
        data["stop_reason"] = "refusal" if change == "refused" else "max_tokens"
        data.pop("structured_output")
    elif category in {"failed", "incomplete", "cancelled"}:
        mode = "error"
        data.update(is_error=True, errors=["private diagnostic"])
        data.pop("structured_output")
        data["subtype"] = {"max-turns": "error_max_turns", "schema-retries": "error_max_structured_output_retries",
                           "cancelled": "error_during_execution"}.get(change, "success")
        data["terminal_reason"] = {"api-error": "api_error", "cancelled": "aborted_streaming", "max-turns": "max_turns"}.get(change)
    raw = json.dumps(data, ensure_ascii=False)
    if change == "duplicate":
        raw = raw[:-1] + ',"subtype":"success"}'
    elif change == "nan":
        raw = raw.replace('"duration_ms": 10', '"duration_ms": NaN')
    provider, frozen = provider_at(tmp_path / "cli", mode=mode, raw=raw)
    with pytest.raises(ProviderError) as failure:
        provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    if category == "output":
        assert isinstance(failure.value, ProviderOutputError)
    elif category == "refused":
        assert isinstance(failure.value, ProviderRefusalError)
    elif category != "unknown":
        assert isinstance(failure.value, ProviderReportedFailure)
        assert failure.value.status == category
    else:
        assert type(failure.value) is ProviderError
    assert "private diagnostic" not in str(failure.value)
    assert (tmp_path / "cli" / "launches.log").read_text().splitlines() == ["launch"]
    assert list((tmp_path / "cli" / "attempts").iterdir()) == []


@pytest.mark.parametrize("change", ["cancel-success", "cancel-nonerror", "cancel-zero", "failed-zero", "failed-output"])
def test_contradictory_terminal_envelope_is_unknown(change):
    completion = importlib.import_module("app.services.providers.claude_prepared_review_completion")
    data = envelope(contract())
    data.update(subtype="error_during_execution", is_error=True, terminal_reason="aborted_streaming")
    data.pop("structured_output")
    code = 1
    if change == "cancel-success":
        data["subtype"] = "success"
    elif change == "cancel-nonerror":
        data["is_error"] = False
    elif change == "cancel-zero":
        code = 0
    else:
        data["terminal_reason"] = "api_error"
        if change == "failed-zero":
            code = 0
        else:
            data["structured_output"] = prior.response(contract()).payload
    with pytest.raises(ProviderError) as failure:
        completion.read_completion(preserved_json(data), code)
    assert type(failure.value) is ProviderError
    assert "unknown" in str(failure.value)


@pytest.mark.parametrize("mode", ["timeout", "flood", "nonzero", "stderr"])
def test_unknown_process_results_are_bounded_and_not_relaunched(tmp_path, mode):
    provider, frozen = provider_at(tmp_path / "cli", mode=mode, timeout=.2 if mode == "timeout" else 30)
    with pytest.raises(ProviderError, match="unknown"):
        provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    assert (tmp_path / "cli" / "launches.log").read_text().splitlines() == ["launch"]
    assert list((tmp_path / "cli" / "attempts").iterdir()) == []


@pytest.mark.parametrize("change", ["launcher", "config-dir", "workspace"])
def test_changed_request_launcher_or_config_dir_refuses_before_child(tmp_path, change):
    provider, frozen = provider_at(tmp_path / "cli")
    request = ProviderRequest(**frozen["provider_request"])
    with pytest.raises(ValueError, match="adapter_mismatch"):
        provider.generate_json(replace(request, prompt=request.prompt + "!"))
    if change == "launcher":
        script = tmp_path / "cli" / "fake_claude.py"
        script.write_text(script.read_text(encoding="utf-8") + "\n# changed", encoding="utf-8")
    elif change == "config-dir":
        config = tmp_path / "cli" / "empty-config"
        config.rename(tmp_path / "cli" / "old-config")
        config.mkdir()
    else:
        (tmp_path / "cli" / "attempts").write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="runtime_changed"):
        provider.validate_local_runtime()
    assert not (tmp_path / "cli" / "launches.log").exists()


@pytest.mark.parametrize("change", ["timeout", "bool-timeout", "provider", "model", "fingerprint", "contract"])
def test_invalid_plan_cannot_change_frozen_execution(tmp_path, change):
    _, plan, _ = modules()
    frozen = contract()
    runtime = runtime_at(tmp_path / "cli", frozen)
    options = dict(runtime_identity=runtime.identity(), timeout_seconds=30)
    if change == "timeout":
        options["timeout_seconds"] = float("inf")
    elif change == "bool-timeout":
        options["timeout_seconds"] = True
    elif change == "provider":
        frozen = prior.contract()
    elif change == "model":
        frozen = prepared_review.prepare(prior.packet(), provider_name="claude_code", model="space rejected")
    elif change == "fingerprint":
        options["runtime_identity"]["config_dir_sha256"] = "bad"
    else:
        frozen["provider_request"]["instructions"] = "changed"
    with pytest.raises(ValueError, match="configuration_invalid"):
        plan.build_plan(frozen, **options)
    assert not (tmp_path / "cli" / "launches.log").exists()


def test_unicode_output_archive_reconstructs_without_runtime_or_credentials(tmp_path):
    _, plan, _ = modules()
    frozen = contract()
    data = envelope(frozen)
    data["structured_output"]["contribution_summary"] = " e\u0301 한글🙂\u0085\u2028\u2029 preserved "
    raw = json.dumps(data, ensure_ascii=False, indent=3)
    provider, frozen = provider_at(tmp_path / "cli", raw=raw)
    record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    assert record["draft"]["contribution_summary"] == data["structured_output"]["contribution_summary"]
    # A historical reader only needs saved bytes, even after local runtime drift.
    config = tmp_path / "cli" / "empty-config"
    config.rename(tmp_path / "cli" / "preserved-config")
    (tmp_path / "cli" / "fake_claude.py").write_text("changed launcher", encoding="utf-8")
    plan.validate_transport(record, frozen["packet"])


@pytest.mark.parametrize("key", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "HTTPS_PROXY", "NODE_OPTIONS"])
def test_environment_routing_and_direct_tokens_are_not_accepted(tmp_path, key):
    _, _, process = modules()
    config = tmp_path / "config"
    config.mkdir()
    with pytest.raises(ValueError, match="runtime_invalid"):
        process.ClaudeRuntime((str(Path(sys.executable).resolve()),), {"CLAUDE_CONFIG_DIR": str(config), key: "fixture"}, tmp_path / "attempts")


@pytest.mark.parametrize("change", ["manifest", "envelope", "envelope_hash", "review_hash", "authority", "extra"])
def test_transport_reader_refuses_archive_mutation(tmp_path, change):
    _, plan, _ = modules()
    provider, frozen = provider_at(tmp_path / "cli")
    record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    bad = deepcopy(record)
    marker = bad["validation"]["execution"]
    if change == "manifest":
        marker["manifest"]["model"] = "another"
        marker["manifest_sha256"] = prepared_review.sha(preserved_json(marker["manifest"]))
    elif change == "envelope":
        data = json.loads(marker["raw_envelope"])
        data["structured_output"]["contribution_summary"] += " changed"
        marker["raw_envelope"] = preserved_json(data)
        marker["envelope_sha256"] = prepared_review.sha(marker["raw_envelope"])
    elif change == "envelope_hash":
        marker["envelope_sha256"] = "0" * 64
    elif change == "review_hash":
        marker["review_sha256"] = "0" * 64
    elif change == "authority":
        marker["authority"] = "APPROVED"
    else:
        marker["unknown"] = 1
    with pytest.raises(ValueError, match="transport_invalid"):
        plan.validate_transport(bad, frozen["packet"])


def test_windows_crash_error_envelope_is_not_confirmed_paper_failure():
    completion = importlib.import_module("app.services.providers.claude_prepared_review_completion")
    data = envelope(contract())
    data.update(is_error=True, subtype="error_during_execution", terminal_reason="api_error", errors=["private diagnostic"])
    data.pop("structured_output")
    with pytest.raises(ProviderError, match="unknown") as failure:
        completion.read_completion(preserved_json(data), 3221225477)
    assert type(failure.value) is ProviderError and "private" not in str(failure.value)


@pytest.mark.parametrize("code", [-9, 2, 7, 3221225477, True, 0.0, None])
@pytest.mark.parametrize("terminal", ["failed", "cancelled", "incomplete"])
def test_abnormal_exit_cannot_confirm_paper_terminal_outcome(code, terminal):
    completion = importlib.import_module("app.services.providers.claude_prepared_review_completion")
    data = envelope(contract())
    data.update(is_error=True, errors=["private diagnostic"],
        subtype="error_max_turns" if terminal == "incomplete" else "error_during_execution",
        terminal_reason={"failed": "api_error", "cancelled": "aborted_streaming", "incomplete": "max_turns"}[terminal])
    data.pop("structured_output")
    with pytest.raises(ProviderError, match="unknown") as failure:
        completion.read_completion(preserved_json(data), code)
    assert type(failure.value) is ProviderError and "private" not in str(failure.value)


def test_normal_error_exit_one_retains_known_provider_failure():
    completion = importlib.import_module("app.services.providers.claude_prepared_review_completion")
    data = envelope(contract())
    data.update(is_error=True, subtype="error_during_execution", terminal_reason="api_error", errors=["private diagnostic"])
    data.pop("structured_output")
    with pytest.raises(ProviderReportedFailure) as failure:
        completion.read_completion(preserved_json(data), 1)
    assert failure.value.status == "failed" and "private" not in str(failure.value)
