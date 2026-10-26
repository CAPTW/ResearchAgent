"""Only local fake ACP children; never invokes Grok or reads account files."""
from copy import deepcopy
from dataclasses import replace
import importlib
import json
import os
import sys

import pytest

from app.domain import prepared_review
from app.domain.journal_exchange import preserved_json
from app.services.providers.base import ProviderError, ProviderOutputError, ProviderRefusalError, ProviderReportedFailure, ProviderRequest
from tests import test_prepared_review as prior


def modules():
    return tuple(importlib.import_module("app.services.providers." + name) for name in (
        "grok_prepared_review", "grok_prepared_review_plan", "grok_prepared_review_process"))


def test_explicit_request_bound_grok_adapter_exists():
    adapter, plan, process = modules()
    assert adapter.GrokPreparedReviewProvider
    assert process.GrokRuntime
    assert plan.SURFACE == "grok_cli"
    assert plan.MANIFEST_SCHEMA == "GROK_PREPARED_REVIEW_ACP_V1"


def contract():
    return prepared_review.prepare(prior.packet(), provider_name="grok_cli", model="fixture-model")


def runtime_at(root, frozen, mode="success", *, output=None):
    _, _, process = modules()
    root.mkdir()
    payload = prior.response(frozen).payload
    if mode == "citation":
        payload["cited_evidence_span_ids"] = ["invented"]
    raw = json.dumps(payload, ensure_ascii=False, indent=2) + "\n" if output is None else output
    (root / "output.txt").write_text(raw, encoding="utf-8", newline="")
    script = root / "fake_grok.py"
    script.write_text('''import json,os,sys,time
from pathlib import Path
root=Path(__file__).parent
mode=MODE
target_model=TARGET_MODEL
with (root/'launches.log').open('a') as marker: marker.write('launch\\n')
(root/'argv.json').write_text(json.dumps(sys.argv[1:]),encoding='utf-8')
(root/'agent.md').write_bytes(Path(sys.argv[sys.argv.index('--agent-profile')+1]).read_bytes())
(root/'policy-env.json').write_text(json.dumps({k:v for k,v in os.environ.items() if k.startswith('GROK_')}),encoding='utf-8')
def send(value):
    raw=json.dumps(value,ensure_ascii=False,separators=(',',':'))+'\\n'
    with (root/'stdout.ndjson').open('ab') as marker: marker.write(raw.encode('utf-8'))
    sys.stdout.buffer.write(raw.encode('utf-8')); sys.stdout.buffer.flush()
def receive():
    raw=sys.stdin.buffer.readline()
    if not raw: sys.exit(0)
    with (root/'stdin.ndjson').open('ab') as marker: marker.write(raw)
    return json.loads(raw)
def reply(request,result): send({'jsonrpc':'2.0','id':request['id'],'result':result})
def options(model): return [{'id':'model','name':'Model','category':'model','type':'select','currentValue':model,'options':[{'value':target_model,'name':'Fixture'},{'value':'other-model','name':'Other'}]}]
r=receive()
if mode=='timeout-init': time.sleep(30)
if mode=='flood': sys.stdout.write('x'*1100000);sys.stdout.flush();time.sleep(30)
reply(r,{'protocolVersion':2 if mode=='protocol' else 1,'agentCapabilities':{},'agentInfo':{'name':'grok-fixture','version':'1'},'authMethods':[{'id':'other' if mode=='auth' else 'cached_token','name':'Cached'}]})
r=receive(); reply(r,{})
r=receive(); (root/'session-new.json').write_text(json.dumps(r),encoding='utf-8')
send({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'fixture-session','_meta':{'eventId':'startup-1'},'update':{'sessionUpdate':'available_commands_update','availableCommands':[{'name':'help','description':'Help'}],'_meta':{'tools':['read_file']}}}})
send({'jsonrpc':'2.0','method':'_x.ai/mcp_initialized','params':{'sessionId':'fixture-session','mcpToolCount':0,'elapsedMs':0}})
reply(r,{'sessionId':'fixture-session','configOptions':options('other-model'),'models':{'currentModelId':'other-model','availableModels':[{'modelId':target_model,'name':'Fixture'},{'modelId':'other-model','name':'Other'}]},'_meta':{'currentWorkingDirectory':r['params']['cwd'],'codebaseIndexed':False}})
r=receive(); (root/'model-request.json').write_text(json.dumps(r),encoding='utf-8')
reply(r,{'configOptions':options('other-model' if mode=='model' else target_model)})
r=receive(); (root/'prompt.json').write_text(json.dumps(r,ensure_ascii=False),encoding='utf-8')
if mode=='timeout': time.sleep(30)
if mode=='missing': sys.exit(0)
if mode=='truncated': sys.stdout.buffer.write(b'{"jsonrpc":"2.0"');sys.stdout.buffer.flush();sys.exit(0)
if mode=='invalid-utf8': sys.stdout.buffer.write(b'\\xff\\n');sys.stdout.buffer.flush();sys.exit(0)
if mode=='descendant':
    import subprocess
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
    (root/'descendant.pid').write_text(str(child.pid))
if mode=='permission':
    send({'jsonrpc':'2.0','id':'permission-1','method':'session/request_permission','params':{'sessionId':'fixture-session','toolCall':{},'options':[]}})
    denied=receive(); (root/'denied.json').write_text(json.dumps(denied),encoding='utf-8'); time.sleep(30)
if mode=='fs':
    send({'jsonrpc':'2.0','id':'fs-1','method':'fs/read_text_file','params':{'sessionId':'fixture-session','path':'sensitive'}})
    denied=receive(); (root/'denied.json').write_text(json.dumps(denied),encoding='utf-8'); time.sleep(30)
if mode=='failed':
    send({'jsonrpc':'2.0','id':r['id'],'error':{'code':-32603,'message':'private diagnostic'}})
else:
    raw=(root/'output.txt').read_bytes().decode('utf-8')
    for index,text in enumerate([raw[:17],raw[17:]]):
        update={'sessionUpdate':'agent_message_chunk','messageId':'second' if mode=='message-id' and index else 'message-1','content':{'type':'text','text':text}}
        if mode=='tool': update={'sessionUpdate':'tool_call','toolCallId':'call-1','title':'Unexpected','kind':'read','status':'in_progress'}
        send({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'other' if mode=='session' else 'fixture-session','update':update}})
    terminal={'jsonrpc':'2.0','id':99 if mode=='wrong-id' else r['id'],'result':{'stopReason':mode if mode in ['refusal','cancelled','max_tokens','max_turn_requests'] else 'end_turn'}}
    if mode=='duplicate-terminal':
        raw=json.dumps(terminal)+'\\n'+json.dumps(terminal)+'\\n'
        sys.stdout.buffer.write(raw.encode('utf-8'));sys.stdout.buffer.flush()
    else: send(terminal)
time.sleep(30) # persistent ACP must be closed by the host, never quiet-time completion
'''.replace("mode=MODE", "mode=" + repr(mode)).replace("target_model=TARGET_MODEL", "target_model=" + repr(frozen["model"])), encoding="utf-8")
    environment = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
    environment.update(TEMP=str(root), TMP=str(root), PYTHONIOENCODING="utf-8")
    return process.GrokRuntime((sys.executable, str(script)), environment, root / "attempts")


def provider_at(root, *, mode="success", output=None, timeout=30):
    adapter, _, _ = modules()
    frozen = contract()
    runtime = runtime_at(root, frozen, mode=mode, output=output)
    return adapter.GrokPreparedReviewProvider(frozen, runtime=runtime, timeout_seconds=timeout), frozen


def test_interactive_handshake_freezes_request_and_archive_without_rerun(tmp_path):
    _, plan, _ = modules()
    provider, frozen = provider_at(tmp_path / "cli")
    record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    root = tmp_path / "cli"
    requests = [json.loads(row) for row in (root / "stdin.ndjson").read_text(encoding="utf-8").splitlines()]
    assert [item["method"] for item in requests] == ["initialize", "authenticate", "session/new", "session/set_config_option", "session/prompt"]
    assert [item["id"] for item in requests] == [1, 2, 3, 4, 5]
    assert requests[0]["params"]["clientCapabilities"] == {}
    assert requests[1]["params"] == dict(methodId="cached_token", _meta=dict(headless=True))
    assert requests[2]["params"]["mcpServers"] == []
    assert requests[3]["params"] == dict(sessionId="fixture-session", configId="model", value="fixture-model")
    assert requests[4]["params"]["prompt"] == [dict(type="text", text=preserved_json(frozen["provider_request"]))]
    args = json.loads((root / "argv.json").read_text())
    assert args[:2] == ["--no-auto-update", "agent"] and args[-2:] == ["--no-leader", "stdio"]
    assert "--tools" not in args and "--always-approve" not in args
    assert frozen["provider_request"]["instructions"] in (root / "agent.md").read_text(encoding="utf-8")
    assert json.loads((root / "policy-env.json").read_text()) == plan.FIXED_ENV
    marker = record["validation"]["execution"]
    observed = "".join(item["raw_line"] for item in marker["transcript"] if item["direction"] == "server")
    assert observed == (root / "stdout.ndjson").read_bytes().decode("utf-8")
    response = record["validation"]["prepared_execution"]["response"]
    assert response["raw_text"] == (root / "output.txt").read_bytes().decode("utf-8")
    assert response["usage"] == {} and response["model"] == "not_reported"
    assert response["response_id"] == "fixture-session"
    assert marker["review_sha256"] == prepared_review.sha(response["raw_text"])
    assert marker["archive_extent"] == "CAPTURED_ACP_PREFIX_THROUGH_TERMINAL"
    assert marker["authority"] == "NOT_ESTABLISHED" and marker["live_execution"] == "NOT_VERIFIED"
    assert provider.manifest()["billing_route"] == "NOT_VERIFIED"
    assert provider.manifest()["tool_isolation"] == "NOT_ESTABLISHED"
    assert provider.manifest()["remote_call_count_enforced"] is False
    assert provider.describe_status()["live_call_enabled"] is False
    assert (root / "launches.log").read_text().splitlines() == ["launch"]
    assert list((root / "attempts").iterdir()) == []
    (root / "fake_grok.py").write_text("changed launcher", encoding="utf-8")
    plan.validate_transport(record, frozen["packet"])


@pytest.mark.parametrize("mode,category", [("failed", "unknown"), ("citation", "output"), ("missing", "unknown"),
    ("protocol", "unknown"), ("auth", "unknown"), ("model", "unknown"), ("permission", "unknown"), ("fs", "unknown"),
    ("tool", "unknown"), ("session", "unknown"), ("message-id", "unknown"), ("wrong-id", "unknown"),
    ("duplicate-terminal", "unknown"), ("timeout", "unknown"), ("timeout-init", "unknown"), ("flood", "unknown"),
    ("truncated", "unknown"), ("invalid-utf8", "unknown"),
    ("refusal", "refused"), ("cancelled", "cancelled"), ("max_tokens", "incomplete"), ("max_turn_requests", "incomplete")])
def test_fake_child_failures_never_relaunch_or_escape_owned_cleanup(tmp_path, mode, category):
    provider, frozen = provider_at(tmp_path / "cli", mode=mode, timeout=.2 if mode.startswith("timeout") else 30)
    with pytest.raises(ProviderError) as error:
        provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    if category == "output":
        assert isinstance(error.value, ProviderOutputError)
    elif category == "refused":
        assert isinstance(error.value, ProviderRefusalError)
    elif category != "unknown":
        assert isinstance(error.value, ProviderReportedFailure) and error.value.status == category
    else:
        assert type(error.value) is ProviderError and "unknown" in str(error.value)
    assert "private diagnostic" not in str(error.value)
    assert (tmp_path / "cli" / "launches.log").read_text().splitlines() == ["launch"]
    assert list((tmp_path / "cli" / "attempts").iterdir()) == []
    if mode in {"protocol", "auth", "model", "timeout-init", "flood"}:
        assert not (tmp_path / "cli" / "prompt.json").exists()


@pytest.mark.parametrize("raw", ['not JSON', '{"duplicate":1,"duplicate":2}', '{"value":NaN}', '[]'])
def test_known_completed_invalid_review_is_not_unknown(tmp_path, raw):
    provider, frozen = provider_at(tmp_path / "cli", output=raw)
    with pytest.raises(ProviderOutputError):
        provider.generate_record(ProviderRequest(**frozen["provider_request"]))


@pytest.mark.parametrize("key", ["XAI_API_KEY", "GROK_HOME", "GROK_AGENT", "GROK_OIDC_ISSUER", "GROK_CLI_CHAT_PROXY_BASE_URL",
                              "GOOGLE_APPLICATION_CREDENTIALS", "OPENAI_API_KEY", "HTTPS_PROXY", "NODE_OPTIONS"])
def test_credentials_and_routing_cannot_be_injected(tmp_path, key):
    _, _, process = modules()
    with pytest.raises(ValueError, match="runtime_invalid"):
        process.GrokRuntime((sys.executable,), {key: "fixture"}, tmp_path / "attempts")


@pytest.mark.parametrize("change", ["request", "launcher", "workspace"])
def test_drift_is_refused_before_child_start(tmp_path, change):
    provider, frozen = provider_at(tmp_path / "cli")
    request = ProviderRequest(**frozen["provider_request"])
    if change == "request":
        request = replace(request, prompt=request.prompt + "!")
    elif change == "launcher":
        (tmp_path / "cli" / "fake_grok.py").write_text("changed", encoding="utf-8")
    else:
        (tmp_path / "cli" / "attempts").write_text("not directory", encoding="utf-8")
    with pytest.raises(ValueError):
        provider.generate_record(request)
    assert not (tmp_path / "cli" / "launches.log").exists()


def test_unicode_original_review_and_local_archive_reconstruction(tmp_path):
    _, plan, _ = modules()
    payload = prior.response(contract()).payload
    payload["contribution_summary"] = " e\u0301 한글🙂\u0085\u2028\u2029 preserved "
    raw = json.dumps(payload, ensure_ascii=False, indent=3) + "\n"
    provider, frozen = provider_at(tmp_path / "cli", output=raw)
    record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    assert record["draft"]["contribution_summary"] == payload["contribution_summary"]
    assert record["validation"]["prepared_execution"]["response"]["raw_text"] == raw
    plan.validate_transport(record, frozen["packet"])


@pytest.mark.parametrize("change", ["manifest", "request", "response-id", "model", "terminal", "extra-client", "extra-server", "hash", "authority", "extra-marker"])
def test_archive_rejects_rehashed_contradictions(tmp_path, change):
    _, plan, _ = modules()
    provider, frozen = provider_at(tmp_path / "cli")
    record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    marker = deepcopy(record["validation"]["execution"])
    if change == "manifest":
        marker["manifest"]["automatic_retry"] = True
        marker["manifest_sha256"] = prepared_review.sha(preserved_json(marker["manifest"]))
    elif change in {"request", "response-id", "model", "terminal"}:
        if change == "request":
            entry = next(item for item in marker["transcript"] if json.loads(item["raw_line"]).get("method") == "session/prompt")
        else:
            number = {"response-id": 1, "model": 4, "terminal": 5}[change]
            entry = next(item for item in marker["transcript"] if item["direction"] == "server" and json.loads(item["raw_line"]).get("id") == number)
        item = json.loads(entry["raw_line"])
        if change == "request":
            item["params"]["prompt"][0]["text"] += " changed"
        elif change == "response-id":
            item["id"] = 99
        elif change == "model":
            item["result"]["configOptions"][0]["currentValue"] = "other-model"
        else:
            item["result"]["stopReason"] = "refusal"
        entry["raw_line"] = preserved_json(item) + "\n"
        marker["transcript_sha256"] = prepared_review.sha(preserved_json(marker["transcript"]))
    elif change.startswith("extra-") and change != "extra-marker":
        entry = next(item for item in marker["transcript"] if item["direction"] == "client") if change == "extra-client" else marker["transcript"][-1]
        marker["transcript"].append(deepcopy(entry))
    elif change == "hash":
        marker["review_sha256"] = "0" * 64
    elif change == "authority":
        marker["authority"] = "APPROVED"
    else:
        marker["extra"] = True
    record["validation"]["execution"] = marker
    with pytest.raises(ValueError, match="transport_invalid"):
        plan.validate_transport(record, frozen["packet"])


def _session_machine(tmp_path):
    protocol = importlib.import_module("app.services.providers.grok_prepared_review_protocol")
    frozen = contract()
    machine = protocol.Conversation(preserved_json(frozen["provider_request"]), frozen["model"], str(tmp_path))
    for response in [dict(protocolVersion=1, agentCapabilities={}, authMethods=[dict(id="cached_token", name="Cached")]), {}]:
        request = machine.next_request
        machine.sent(protocol.line(request))
        machine.received(protocol.line(dict(jsonrpc="2.0", id=request["id"], result=response)))
    return protocol, machine


def test_official_session_models_and_metadata_do_not_hide_model_binding(tmp_path):
    protocol, machine = _session_machine(tmp_path)
    machine.sent(protocol.line(machine.next_request))
    options = [dict(id="model", name="Model", category="model", type="select", currentValue="fixture-model",
                    options=[dict(value="fixture-model", name="Fixture")])]
    machine.received(protocol.line(dict(jsonrpc="2.0", id=3, result=dict(sessionId="fixture-session", configOptions=options,
        models=dict(currentModelId="fixture-model", availableModels=[dict(modelId="fixture-model", name="Fixture")]),
        _meta={"x.ai": {"fixture": True}}))))
    assert machine.next_request["method"] == "session/set_config_option"
    assert machine.next_request["params"]["value"] == "fixture-model"


def test_correlated_rpc_error_still_has_unknown_remote_outcome(tmp_path):
    protocol, machine = _session_machine(tmp_path)
    options = [dict(id="model", name="Model", type="select", currentValue="fixture-model",
                    options=[dict(value="fixture-model", name="Fixture")])]
    for result in [dict(sessionId="fixture-session", configOptions=options), dict(configOptions=options)]:
        request = machine.next_request
        machine.sent(protocol.line(request))
        machine.received(protocol.line(dict(jsonrpc="2.0", id=request["id"], result=result)))
    machine.sent(protocol.line(machine.next_request))
    machine.received(protocol.line(dict(jsonrpc="2.0", id=5, error=dict(code=-32603, message="private diagnostic"))))
    with pytest.raises(ProviderError, match="unknown") as failure:
        machine.response()
    assert type(failure.value) is ProviderError


def test_startup_commands_before_session_reply_are_bound_before_prompt(tmp_path):
    protocol, machine = _session_machine(tmp_path)
    machine.sent(protocol.line(machine.next_request))
    machine.received(protocol.line(dict(jsonrpc="2.0", method="session/update", params=dict(sessionId="fixture-session",
        update=dict(sessionUpdate="available_commands_update", availableCommands=[dict(name="help", description="Help")],
                    _meta=dict(tools=["read_file"]))))))
    assert machine.next_request is None
    options = [dict(id="model", name="Model", type="select", currentValue="fixture-model",
                    options=[dict(value="fixture-model", name="Fixture")])]
    machine.received(protocol.line(dict(jsonrpc="2.0", id=3, result=dict(sessionId="fixture-session", configOptions=options))))
    assert machine.next_request["method"] == "session/set_config_option"


@pytest.mark.parametrize("change", ["model", "cwd", "startup-session", "metadata-array", "metadata-large", "tools"])
def test_official_metadata_cannot_override_session_model_or_scope(tmp_path, change):
    protocol, machine = _session_machine(tmp_path)
    machine.sent(protocol.line(machine.next_request))
    if change in {"startup-session", "tools"}:
        update = dict(sessionUpdate="available_commands_update", availableCommands=[])
        if change == "tools":
            update = dict(sessionUpdate="tool_call", toolCallId="call")
        machine.received(protocol.line(dict(jsonrpc="2.0", method="session/update",
            params=dict(sessionId="other-session" if change == "startup-session" else "fixture-session", update=update))))
    result = dict(sessionId="fixture-session", configOptions=[dict(id="model", name="Model", type="select",
        currentValue="fixture-model", options=[dict(value="fixture-model", name="Fixture")])],
        models=dict(currentModelId="other-model" if change == "model" else "fixture-model",
                    availableModels=[dict(modelId="fixture-model", name="Fixture")]),
        _meta=dict(currentWorkingDirectory=str(tmp_path / "other" if change == "cwd" else tmp_path)))
    if change == "metadata-array":
        result["_meta"] = []
    elif change == "metadata-large":
        result["_meta"] = dict(large="x" * 64001)
    with pytest.raises(ValueError):
        machine.received(protocol.line(dict(jsonrpc="2.0", id=3, result=result)))
    assert machine.next_request is None


@pytest.mark.parametrize("raw", ['{"jsonrpc":"2.0","id":1,"id":1,"result":{}}\n',
    '{"jsonrpc":"2.0","id":1,"result":{"x":NaN}}\n', '{"jsonrpc":"2.0"}',
    '{"jsonrpc":"2.0"}\r\n', '{"jsonrpc":"2.0"}\n{}\n'])
def test_ambiguous_or_incomplete_wire_frames_are_rejected(raw):
    protocol = importlib.import_module("app.services.providers.grok_prepared_review_protocol")
    with pytest.raises(ValueError):
        protocol.parse_line(raw)


@pytest.mark.parametrize("change", ["prefix", "scope", "tools", "bool-tools", "elapsed", "extra"])
def test_mcp_zero_startup_metadata_is_a_narrow_allowance(tmp_path, change):
    protocol, machine = _session_machine(tmp_path)
    machine.sent(protocol.line(machine.next_request))
    value = dict(jsonrpc="2.0", method="_x.ai/mcp_initialized", params=dict(
        sessionId="fixture-session", mcpToolCount=0, elapsedMs=0))
    if change == "prefix":
        value["method"] = "x.ai/mcp_initialized"
    elif change == "scope":
        value["params"]["sessionId"] = "other-session"
    elif change in {"tools", "bool-tools"}:
        value["params"]["mcpToolCount"] = 1 if change == "tools" else False
    elif change == "elapsed":
        value["params"]["elapsedMs"] = -1
    else:
        value["params"]["authority"] = "approved"
    machine.received(protocol.line(value))
    options = [dict(id="model", name="Model", type="select", currentValue="fixture-model",
                    options=[dict(value="fixture-model", name="Fixture")])]
    with pytest.raises(ValueError):
        machine.received(protocol.line(dict(jsonrpc="2.0", id=3, result=dict(sessionId="fixture-session", configOptions=options))))


def test_fake_helpers_bind_nonfixture_requested_model(tmp_path):
    adapter, plan, _ = modules()
    frozen = prepared_review.prepare(prior.packet(), provider_name="grok_cli", model="selected-model-2")
    runtime = runtime_at(tmp_path / "cli", frozen)
    provider = adapter.GrokPreparedReviewProvider(frozen, runtime=runtime)
    record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    model_request = json.loads((tmp_path / "cli" / "model-request.json").read_text())
    assert model_request["params"]["value"] == frozen["model"]
    assert provider.manifest()["model"] == frozen["model"]
    plan.validate_transport(record, frozen["packet"])


@pytest.mark.skipif(os.name != "nt", reason="Windows Job descendant cleanup contract")
def test_terminal_cleanup_closes_own_hanging_descendant(tmp_path):
    import ctypes
    from ctypes import wintypes

    provider, frozen = provider_at(tmp_path / "cli", mode="descendant")
    provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    pid = int((tmp_path / "cli" / "descendant.pid").read_text())
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000 | 0x0001, False, pid)
    if not handle:
        assert ctypes.get_last_error() == 87  # Process is already gone.
    else:
        try:
            state = kernel.WaitForSingleObject(handle, 0)
            if state != 0:
                kernel.TerminateProcess(handle, 1)  # Only this fixture's owned PID.
                kernel.WaitForSingleObject(handle, 5000)
            assert state == 0, "host returned before its descendant exited"
        finally:
            kernel.CloseHandle(handle)
    assert list((tmp_path / "cli" / "attempts").iterdir()) == []
