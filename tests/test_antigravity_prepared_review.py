"""Fake child processes only; no Antigravity binary, account or live qualification."""
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
from app.services.providers.base import ProviderError, ProviderOutputError, ProviderReportedFailure, ProviderRequest
from tests import test_prepared_review as prior


def modules():
    return tuple(importlib.import_module("app.services.providers." + name) for name in (
        "antigravity_prepared_review", "antigravity_prepared_review_plan", "antigravity_prepared_review_process"))


def contract():
    return prepared_review.prepare(prior.packet(), provider_name="antigravity_cli", model="fixture-model")


def events(frozen):
    payload = prior.response(frozen).payload
    raw = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    usage = dict(input_tokens=4, output_tokens=7, thinking_tokens=2, cache_read_tokens=0, total_tokens=11)
    return [
        dict(event="init", conversation_id="fixture-session", init=dict(cwd="{attempt}", tools=[],
            permission_mode="request-review", model=frozen["model"], agent="xrml-paper-review-v1",
            json_schema=frozen["provider_request"]["schema"])),
        dict(event="step_update", step_update=dict(conversation_id="fixture-session", step_index=0,
            state="DONE", step_type="user_input")),
        dict(event="step_update", step_update=dict(conversation_id="fixture-session", step_index=2,
            state="ACTIVE", step_type="agent_response", text_delta=raw[:15])),
        dict(event="step_update", step_update=dict(conversation_id="fixture-session", step_index=2,
            state="DONE", step_type="agent_response", text_delta=raw[15:], duration_seconds=.1, usage=usage)),
        dict(event="step_update", step_update=dict(conversation_id="fixture-session", step_index=3,
            state="DONE", step_type="checkpoint", duration_seconds=.01)),
        dict(event="result", result=dict(conversation_id="fixture-session", status="SUCCESS", response=raw,
            duration_seconds=.2, num_turns=1, structured_output=payload,
            json_schema=frozen["provider_request"]["schema"], usage=usage)),
    ]


def runtime_at(root, frozen, mode="success", *, stream=None):
    _, _, process = modules()
    root.mkdir()
    data = events(frozen)
    if mode == "failed":
        data = data[:2] + [data[-1]]
        data[-1]["result"].update(status="ERROR", response="", error="private diagnostic")
        data[-1]["result"].pop("structured_output")
    elif mode == "citation":
        data[-1]["result"]["structured_output"]["cited_evidence_span_ids"] = ["invented"]
        raw = preserved_json(data[-1]["result"]["structured_output"])
        data[-1]["result"]["response"] = raw
        data[2]["step_update"]["text_delta"] = ""
        data[3]["step_update"]["text_delta"] = raw
    elif mode == "missing":
        data.pop()
    stream = "\n".join(preserved_json(item) for item in data) + "\n" if stream is None else stream
    (root / "stream-template.ndjson").write_text(stream, encoding="utf-8", newline="")
    script = root / "fake_antigravity.py"
    script.write_text('''import json,os,sys,time
from pathlib import Path
root=Path(__file__).parent
stdin=sys.stdin.read()
(root/'stdin.ndjson').write_text(stdin,encoding='utf-8',newline='')
(root/'argv.json').write_text(json.dumps(sys.argv[1:]),encoding='utf-8')
(root/'policy-env.json').write_text(json.dumps({'AGY_CLI_DISABLE_AUTO_UPDATE':os.environ.get('AGY_CLI_DISABLE_AUTO_UPDATE')}),encoding='utf-8')
(root/'agent.md').write_bytes((Path.cwd()/'.agents'/'agents'/'xrml-paper-review-v1.md').read_bytes())
(root/'schema.json').write_bytes(Path(sys.argv[sys.argv.index('--json-schema')+1]).read_bytes())
with (root/'launches.log').open('a') as marker: marker.write('launch\\n')
mode=MODE
if mode=='timeout': time.sleep(30)
if mode=='flood': print('x'*1100000); sys.exit(0)
if mode=='stderr': sys.stderr.write('private diagnostic'); sys.exit(7)
raw=(root/'stream-template.ndjson').read_bytes().decode('utf-8')
raw=raw.replace('{attempt}',str(Path.cwd()).replace('\\\\','\\\\\\\\'))
(root/'stdout.ndjson').write_text(raw,encoding='utf-8',newline='')
sys.stdout.buffer.write(raw.encode('utf-8'))
sys.exit(1 if mode in {'failed','error'} else 7 if mode=='nonzero' else 0)
'''.replace("MODE", repr(mode)), encoding="utf-8")
    environment = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
    environment.update(TEMP=str(root), TMP=str(root), PYTHONIOENCODING="utf-8")
    return process.AntigravityRuntime((str(Path(sys.executable).resolve()), str(script)), environment, root / "attempts")


def provider_at(root, *, mode="success", stream=None, timeout=30):
    adapter, _, _ = modules()
    frozen = contract()
    runtime = runtime_at(root, frozen, mode=mode, stream=stream)
    return adapter.AntigravityPreparedReviewProvider(frozen, runtime=runtime, timeout_seconds=timeout), frozen


def test_exact_single_input_and_stream_response_provenance(tmp_path):
    _, plan, _ = modules()
    provider, frozen = provider_at(tmp_path / "cli")
    record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    root = tmp_path / "cli"
    stdin = (root / "stdin.ndjson").read_text(encoding="utf-8")
    assert stdin == preserved_json(dict(event="user", message=dict(content=preserved_json(frozen["provider_request"])))) + "\n"
    args = json.loads((root / "argv.json").read_text())
    assert args[args.index("--input-format") + 1] == "stream-json"
    assert args[args.index("--output-format") + 1] == "stream-json"
    assert args[args.index("--model") + 1] == "fixture-model"
    assert args[args.index("--agent") + 1] == "xrml-paper-review-v1"
    assert args[args.index("--effort") + 1] == "low"
    assert "--sandbox" in args and "--dangerously-skip-permissions" not in args and "-p" not in args
    assert json.loads((root / "schema.json").read_text()) == frozen["provider_request"]["schema"]
    agent = (root / "agent.md").read_text(encoding="utf-8")
    assert "tools: []" in agent and "inheritCustomizations: false" in agent and "commandExecutionPolicy: off" in agent
    assert frozen["provider_request"]["instructions"] in agent
    assert json.loads((root / "policy-env.json").read_text()) == {"AGY_CLI_DISABLE_AUTO_UPDATE": "true"}
    marker = record["validation"]["execution"]
    assert marker["raw_stdout"] == (root / "stdout.ndjson").read_bytes().decode("utf-8")
    assert marker["stdout_sha256"] == prepared_review.sha(marker["raw_stdout"])
    response = record["validation"]["prepared_execution"]["response"]
    assert response["raw_text"] == events(frozen)[-1]["result"]["response"]
    assert response["payload"] == events(frozen)[-1]["result"]["structured_output"]
    assert response["usage"] == dict(input_tokens=4, output_tokens=7, total_tokens=11)
    assert response["model"] == "not_reported" and response["response_id"] == "fixture-session"
    assert marker["authority"] == "NOT_ESTABLISHED" and marker["live_execution"] == "NOT_VERIFIED"
    assert record["validation"]["prepared_execution"]["full_document_review"] == "NOT_ESTABLISHED"
    plan.validate_transport(record, frozen["packet"])
    manifest = provider.manifest()
    assert manifest["managed_hooks"] == "NOT_ISOLATED_NOT_LIVE_VERIFIED"
    assert manifest["windows_sandbox"] == "REQUESTED_NOT_LIVE_VERIFIED"
    assert manifest["remote_call_count_enforced"] is False
    assert manifest["output_token_limit_enforced"] is False
    assert manifest["automatic_retry"] is False
    manifest["model"] = "changed"
    assert provider.manifest()["model"] == "fixture-model"
    assert provider.describe_status()["live_call_enabled"] is False
    assert (root / "launches.log").read_text().splitlines() == ["launch"]
    assert list((root / "attempts").iterdir()) == []


@pytest.mark.parametrize("change", [
    "init-model", "init-schema", "init-agent", "init-tools", "init-permission", "init-cwd", "init-extra",
    "step-identity", "step-tool", "step-subagent", "step-index", "step-state", "step-duplicate", "step-after-done",
    "step-extra", "missing-init", "missing-terminal", "duplicate-terminal", "duplicate-init", "unknown-event",
    "result-identity", "result-schema", "result-turns", "result-bool-turns", "result-error", "result-running",
    "result-waiting", "result-invalid", "result-extra", "negative-usage", "bool-usage", "nan", "duplicate-key",
    "nonzero-success", "unfinished-step", "two-inputs", "result-after-terminal",
])
def test_stream_activity_and_identity_contradictions_are_unknown(tmp_path, change):
    completion = importlib.import_module("app.services.providers.antigravity_prepared_review_completion")
    frozen = contract()
    data = events(frozen)
    init, step, result = data[0]["init"], data[3]["step_update"], data[-1]["result"]
    code = 0
    if change.startswith("init-"):
        key = change.removeprefix("init-")
        key = {"schema": "json_schema", "permission": "permission_mode"}.get(key, key)
        init[key] = {"tools": ["view_file"], "json_schema": {}, "cwd": str(tmp_path / "other"),
                     "permission_mode": "always-proceed"}.get(key, "different")
    elif change == "step-identity":
        step["conversation_id"] = "other-session"
    elif change == "step-tool":
        step["step_type"] = "tool"
    elif change == "step-subagent":
        step["subagent_info"] = {"subagents": []}
    elif change == "step-index":
        step["step_index"] = True
    elif change == "step-state":
        step["state"] = "UNKNOWN"
    elif change in {"step-duplicate", "step-after-done"}:
        data.insert(4, deepcopy(data[3]))
        if change == "step-after-done":
            data[4]["step_update"]["state"] = "ACTIVE"
    elif change == "step-extra":
        step["tool_info"] = {}
    elif change == "missing-init":
        data.pop(0)
    elif change == "missing-terminal":
        data.pop()
    elif change == "duplicate-terminal":
        data.append(deepcopy(data[-1]))
    elif change == "duplicate-init":
        data.insert(1, deepcopy(data[0]))
    elif change == "unknown-event":
        data.insert(1, {"event": "future"})
    elif change == "result-identity":
        result["conversation_id"] = "other-session"
    elif change == "result-schema":
        result["json_schema"] = {}
    elif change in {"result-turns", "result-bool-turns"}:
        result["num_turns"] = 2 if change == "result-turns" else True
    elif change == "result-error":
        result["error"] = "private diagnostic"
    elif change in {"result-running", "result-waiting", "result-invalid"}:
        result["status"] = change.removeprefix("result-").upper()
    elif change == "result-extra":
        result["tool_calls"] = []
    elif change in {"negative-usage", "bool-usage"}:
        result["usage"]["input_tokens"] = -1 if change == "negative-usage" else True
    elif change == "nonzero-success":
        code = 1
    elif change == "unfinished-step":
        step["state"] = "ACTIVE"
    elif change == "two-inputs":
        data[4]["step_update"]["step_type"] = "user_input"
    elif change == "result-after-terminal":
        data.append(deepcopy(data[4]))
    raw = "\n".join(preserved_json(item) for item in data).replace("{attempt}", str(tmp_path).replace("\\", "\\\\"))
    if change == "nan":
        raw = raw.replace('"duration_seconds":0.2', '"duration_seconds":NaN')
    elif change == "duplicate-key":
        raw = raw.replace('"status":"SUCCESS"', '"status":"SUCCESS","status":"SUCCESS"')
    with pytest.raises(ProviderError, match="unknown") as error:
        completion.read_completion(raw, code, model=frozen["model"], schema=frozen["provider_request"]["schema"], attempt_cwd=str(tmp_path))
    assert type(error.value) is ProviderError and "private diagnostic" not in str(error.value)


@pytest.mark.parametrize("status,category", [("ERROR", "failed"), ("CANCELED", "cancelled"), ("INTERRUPTED", "cancelled")])
@pytest.mark.parametrize("contradiction", [None, "zero-exit", "output", "nonempty-response", "missing-error", "wrong-identity"])
def test_known_terminal_failures_require_consistent_bound_envelope(tmp_path, status, category, contradiction):
    completion = importlib.import_module("app.services.providers.antigravity_prepared_review_completion")
    frozen = contract()
    data = events(frozen)
    data = data[:2] + [data[-1]]
    final = data[-1]["result"]
    final.update(status=status, response="", error="private diagnostic")
    final.pop("structured_output")
    code = 1
    if contradiction == "zero-exit":
        code = 0
    elif contradiction == "output":
        final["structured_output"] = {}
    elif contradiction == "nonempty-response":
        final["response"] = "unexpected response"
    elif contradiction == "missing-error":
        final.pop("error")
    elif contradiction == "wrong-identity":
        final["conversation_id"] = "other-session"
    raw = "\n".join(preserved_json(item) for item in data).replace("{attempt}", str(tmp_path).replace("\\", "\\\\"))
    with pytest.raises(ProviderError) as error:
        completion.read_completion(raw, code, model=frozen["model"], schema=frozen["provider_request"]["schema"], attempt_cwd=str(tmp_path))
    if contradiction is None:
        assert isinstance(error.value, ProviderReportedFailure) and error.value.status == category
    else:
        assert type(error.value) is ProviderError
    assert "private diagnostic" not in str(error.value)


@pytest.mark.parametrize("mode,category", [("citation", "output"), ("failed", "failed"), ("missing", "unknown"),
                                         ("nonzero", "unknown"), ("stderr", "unknown"), ("flood", "unknown"), ("timeout", "unknown")])
def test_fake_process_failures_cleanup_without_relaunch(tmp_path, mode, category):
    provider, frozen = provider_at(tmp_path / "cli", mode=mode, timeout=.2 if mode == "timeout" else 30)
    with pytest.raises(ProviderError) as error:
        provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    if category == "output":
        assert isinstance(error.value, ProviderOutputError)
    elif category == "failed":
        assert isinstance(error.value, ProviderReportedFailure) and error.value.status == "failed"
    else:
        assert type(error.value) is ProviderError and "unknown" in str(error.value)
    assert (tmp_path / "cli" / "launches.log").read_text().splitlines() == ["launch"]
    assert list((tmp_path / "cli" / "attempts").iterdir()) == []


@pytest.mark.parametrize("change", ["missing-output", "bad-json", "mismatch", "duplicate-response-key", "response-extra-field"])
def test_completed_invalid_output_is_distinguished(tmp_path, change):
    frozen = contract()
    data = events(frozen)
    final = data[-1]["result"]
    if change == "missing-output":
        final.pop("structured_output")
    elif change == "bad-json":
        final["response"] = "not JSON"
    elif change == "mismatch":
        final["structured_output"] = {}
    elif change == "duplicate-response-key":
        final["response"] = '{"repeated":1,"repeated":1}'
    else:
        final["structured_output"]["invented"] = 1
        final["response"] = preserved_json(final["structured_output"])
    provider, frozen = provider_at(tmp_path / "cli", stream="\n".join(preserved_json(item) for item in data))
    with pytest.raises(ProviderOutputError):
        provider.generate_record(ProviderRequest(**frozen["provider_request"]))


def test_unicode_and_noncanonical_step_text_preserved_after_runtime_changes(tmp_path):
    _, plan, _ = modules()
    frozen = contract()
    data = events(frozen)
    data[-1]["result"]["structured_output"]["contribution_summary"] = " e\u0301 한글🙂\u0085\u2028\u2029 preserved "
    data[-1]["result"]["response"] = json.dumps(data[-1]["result"]["structured_output"], ensure_ascii=False, indent=3) + "\n"
    # Intermediate text is not documented as byte-identical to the final schema response.
    data[2]["step_update"]["text_delta"] = "intermediate output"
    provider, frozen = provider_at(tmp_path / "cli", stream="\r\n".join(preserved_json(item) for item in data) + "\r\n")
    record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    assert record["draft"]["contribution_summary"] == data[-1]["result"]["structured_output"]["contribution_summary"]
    assert record["validation"]["prepared_execution"]["response"]["raw_text"] == data[-1]["result"]["response"]
    assert "\r\n" in record["validation"]["execution"]["raw_stdout"]
    assert record["validation"]["execution"]["raw_stdout"] == (tmp_path / "cli" / "stdout.ndjson").read_bytes().decode("utf-8")
    (tmp_path / "cli" / "fake_antigravity.py").write_text("changed launcher", encoding="utf-8")
    plan.validate_transport(record, frozen["packet"])


@pytest.mark.parametrize("change", ["launcher", "workspace", "request"])
def test_preflight_drift_or_changed_request_refuses_before_child(tmp_path, change):
    provider, frozen = provider_at(tmp_path / "cli")
    request = ProviderRequest(**frozen["provider_request"])
    if change == "request":
        request = replace(request, prompt=request.prompt + "!")
    elif change == "launcher":
        script = tmp_path / "cli" / "fake_antigravity.py"
        script.write_text(script.read_text(encoding="utf-8") + "\n# changed", encoding="utf-8")
    else:
        (tmp_path / "cli" / "attempts").write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="adapter_mismatch" if change == "request" else "runtime_changed"):
        provider.generate_record(request)
    assert not (tmp_path / "cli" / "launches.log").exists()


@pytest.mark.parametrize("key", ["GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT",
                              "GOOGLE_GEMINI_BASE_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "HTTPS_PROXY", "NODE_OPTIONS"])
def test_environment_credentials_cloud_and_routing_are_not_accepted(tmp_path, key):
    _, _, process = modules()
    with pytest.raises(ValueError, match="runtime_invalid"):
        process.AntigravityRuntime((str(Path(sys.executable).resolve()),), {key: "fixture"}, tmp_path / "attempts")


def test_profile_cannot_be_replaced_or_fixed_updater_policy_disabled(tmp_path, monkeypatch):
    _, _, process = modules()
    monkeypatch.setenv("HOME", "fixture-existing-profile")
    with pytest.raises(ValueError, match="runtime_invalid"):
        process.AntigravityRuntime((str(Path(sys.executable).resolve()),), {"HOME": "different"}, tmp_path / "attempts")
    with pytest.raises(ValueError, match="runtime_invalid"):
        process.AntigravityRuntime((str(Path(sys.executable).resolve()),), {"AGY_CLI_DISABLE_AUTO_UPDATE": "false"}, tmp_path / "attempts")
    runtime = process.AntigravityRuntime((str(Path(sys.executable).resolve()),), {"HOME": "fixture-existing-profile"}, tmp_path / "attempts")
    monkeypatch.setenv("HOME", "changed-ambient-profile")
    with pytest.raises(ValueError, match="runtime_changed"):
        runtime.verify()
    assert not (tmp_path / "attempts").exists()


@pytest.mark.parametrize("change", ["timeout", "bool-timeout", "provider", "model", "fingerprint", "contract"])
def test_invalid_plan_refuses_before_execution(tmp_path, change):
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
        frozen = prepared_review.prepare(prior.packet(), provider_name="antigravity_cli", model="space rejected")
    elif change == "fingerprint":
        options["runtime_identity"]["workspace_sha256"] = "bad"
    else:
        frozen["provider_request"]["instructions"] = "changed"
    with pytest.raises(ValueError, match="configuration_invalid"):
        plan.build_plan(frozen, **options)
    assert not (tmp_path / "cli" / "launches.log").exists()


@pytest.mark.parametrize("change", ["manifest", "stdout", "stdout-hash", "response-hash", "structured-hash", "cwd", "authority", "extra"])
def test_saved_archive_rejects_rehashed_conflicts_and_tampering(tmp_path, change):
    _, plan, _ = modules()
    provider, frozen = provider_at(tmp_path / "cli")
    record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    bad = deepcopy(record)
    marker = bad["validation"]["execution"]
    if change == "manifest":
        marker["manifest"]["model"] = "another"
        marker["manifest_sha256"] = prepared_review.sha(preserved_json(marker["manifest"]))
    elif change == "stdout":
        marker["raw_stdout"] = marker["raw_stdout"].replace('"num_turns":1', '"num_turns":2')
        marker["stdout_sha256"] = prepared_review.sha(marker["raw_stdout"])
    elif change == "stdout-hash":
        marker["stdout_sha256"] = "0" * 64
    elif change == "response-hash":
        marker["review_sha256"] = "0" * 64
    elif change == "structured-hash":
        marker["structured_output_sha256"] = "0" * 64
    elif change == "cwd":
        marker["attempt_cwd"] = str(tmp_path / "other")
        marker["attempt_cwd_sha256"] = prepared_review.sha(marker["attempt_cwd"])
    elif change == "authority":
        marker["authority"] = "APPROVED"
    else:
        marker["unknown"] = 1
    with pytest.raises(ValueError, match="transport_invalid"):
        plan.validate_transport(bad, frozen["packet"])


@pytest.mark.parametrize("selection", ["none", "agent-only", "checkpoint-only"])
def test_documented_optional_step_telemetry_is_not_required(tmp_path, selection):
    completion = importlib.import_module("app.services.providers.antigravity_prepared_review_completion")
    frozen = contract()
    data = events(frozen)
    middle = [] if selection == "none" else data[2:4] if selection == "agent-only" else data[4:5]
    data = data[:1] + middle + data[-1:]
    raw = "\n".join(preserved_json(item) for item in data).replace("{attempt}", str(tmp_path).replace("\\", "\\\\"))
    response = completion.read_completion(raw, 0, model=frozen["model"], schema=frozen["provider_request"]["schema"], attempt_cwd=str(tmp_path))
    assert response.raw_text == data[-1]["result"]["response"]
    assert response.payload == data[-1]["result"]["structured_output"]


@pytest.mark.parametrize("exit_code", [-1, -1073741819, 3221225477])
def test_abnormal_process_exit_cannot_be_a_known_provider_failure(tmp_path, exit_code):
    completion = importlib.import_module("app.services.providers.antigravity_prepared_review_completion")
    frozen = contract()
    data = events(frozen)
    data = data[:2] + data[-1:]
    data[-1]["result"].update(status="ERROR", response="", error="private diagnostic")
    data[-1]["result"].pop("structured_output")
    raw = "\n".join(preserved_json(item) for item in data).replace("{attempt}", str(tmp_path).replace("\\", "\\\\"))
    with pytest.raises(ProviderError, match="unknown") as error:
        completion.read_completion(raw, exit_code, model=frozen["model"], schema=frozen["provider_request"]["schema"], attempt_cwd=str(tmp_path))
    assert type(error.value) is ProviderError
