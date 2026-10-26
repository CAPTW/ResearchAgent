"""Journal CLI contract tests. Only explicit fake executables, never Codex/login."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from threading import Barrier

import pytest

from app.repositories import journal_executions as repo
from app.services.providers.base import ProviderError, ProviderOutputError, ProviderReportedFailure, ProviderRequest
from app.services.providers.codex_journal import CodexJournalProvider, journal_dispatcher, read_result
from app.services.providers.codex_journal_process import CliRuntime
from app.services.provider_output_class_proof import compute_provider_proof_id
from tests import test_journal_execution as prior, test_journal_dispatch as dispatch

source = prior.source
completed = prior.completed


@pytest.fixture
def execution(completed):
    return prior.prepare(completed, provider_name="codex_cli", model="offline-requested-model")[0]


def runtime_at(root: Path, mode: str = "success") -> CliRuntime:
    root.mkdir(parents=True, exist_ok=True)
    script = root / "fake_cli.py"
    script.write_text('''import json,sys,time
from pathlib import Path
args=sys.argv[1:]
request=json.loads(sys.stdin.read())
packet=json.loads(request['prompt'])
root=Path(__file__).parent
with (root/'launches.log').open('a') as marker: marker.write('launch\\n')
mode=MODE
if mode=='held':
 (root/'entered').touch(); deadline=time.monotonic()+10
 while not (root/'release').exists() and time.monotonic()<deadline: time.sleep(.01)
 assert (root/'release').exists()
if mode=='timeout': time.sleep(5)
if mode=='flood':
 sys.stdout.write('x'*2000000); sys.stdout.flush(); time.sleep(5)
if mode=='stderr_flood':
 sys.stderr.write('x'*40000); sys.stderr.flush(); time.sleep(5)
if mode=='final_flood':
 Path(args[args.index('--output-last-message')+1]).write_text('x'*600000); time.sleep(5)
proposal=dict(schema_version='JOURNAL_PROPOSAL_V1',packet_id=packet['packet_id'],title='Offline CLI e\\u0301',sections=[dict(kind='limitations',entries=[dict(kind='observation',text='Synthetic process test only.',citation_ids=[packet['references'][0]['citation_id']])])])
if mode=='unicode': proposal['title']='Offline CLI \\u0085\\u2028\\u2029 e\\u0301'
text=json.dumps(proposal,ensure_ascii=False)
Path(args[args.index('--output-last-message')+1]).write_text(text,encoding='utf-8')
print(json.dumps(dict(type='thread.started',thread_id='offline-thread')))
print(json.dumps(dict(type='turn.started')))
print(json.dumps(dict(type='item.completed',item=dict(type='agent_message',text=text)),ensure_ascii=mode!='unicode'))
if mode=='malformed_failure': print(json.dumps(dict(type='turn.failed')))
elif mode!='missing_completion': print(json.dumps(dict(type='turn.completed',usage=dict(input_tokens=10,output_tokens=5))))
if mode=='nonzero': sys.exit(7)
'''.replace('MODE', repr(mode)), encoding="utf-8")
    home = root / "empty-home"
    home.mkdir(exist_ok=True)
    env = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR") if name in os.environ}
    env.update(CODEX_HOME=str(home), TEMP=str(root), TMP=str(root), PYTHONIOENCODING="utf-8")
    return CliRuntime((sys.executable, str(script)), env, root / "attempts")


def adapter(execution, root, mode="success", *, version="1", **changes):
    provider = CodexJournalProvider
    if version == "2":
        from app.services.providers.codex_journal_v2 import CodexJournalV2Provider
        provider = CodexJournalV2Provider
    elif version != "1":
        raise ValueError("unsupported fixture profile")
    return provider(execution["contract"], runtime=runtime_at(root, mode),
                                **(dict(reasoning_effort="low", timeout_seconds=30) | changes))


def approval(execution, provider):
    old = dispatch.grant(execution)
    identity = replace(old.proof.identity, surface_id=provider.binding.surface_id, adapter_id=provider.binding.adapter_id,
                       adapter_version=provider.binding.adapter_version, command_shape_sha256=provider.binding.command_shape_sha256)
    return old.model_copy(update={"proof": replace(old.proof, identity=identity, proof_id=compute_provider_proof_id(identity))})


def wait_terminal(execution):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        value = repo.detail(*prior.scope(execution))
        if value["current"]["state"] not in {"READY", "LEASED", "RUNNING"}:
            return value
        time.sleep(.01)
    pytest.fail("Fake CLI worker did not terminate")


def test_default_denial_consumes_no_attempt_or_process(execution, tmp_path):
    provider = adapter(execution, tmp_path / "cli")
    controller = journal_dispatcher(provider, clock=lambda: prior.NOW)
    with pytest.raises(ValueError, match="authority_unavailable"):
        controller.dispatch(*prior.scope(execution), dispatch.body(), "owner")
    assert repo.detail(*prior.scope(execution)) == execution
    assert not (tmp_path / "cli" / "attempts").exists()
    assert controller.stop()


def test_workspace_file_refuses_before_journal_reservation_and_allows_explicit_recovery(execution, tmp_path):
    root = tmp_path / "cli"
    provider = adapter(execution, root)
    blocker = root / "attempts"
    blocker.write_bytes(b"preserve blocking file")
    grants = []
    def issuer(*_):
        grants.append("requested")
        return approval(execution, provider)
    controller = journal_dispatcher(provider, authority=issuer, clock=lambda: prior.NOW)
    try:
        try:
            controller.dispatch(*prior.scope(execution), dispatch.body(), "owner")
        except ValueError as error:
            assert str(error) == "journal_codex_runtime_changed"
        else:
            current = wait_terminal(execution)["current"]
            pytest.fail(f"workspace admitted: {current['state']}, unknown={current['outcome_unknown']}")
        assert repo.detail(*prior.scope(execution)) == execution
        assert grants == []
        assert not (root / "launches.log").exists()
        assert blocker.read_bytes() == b"preserve blocking file"
        blocker.rename(root / "preserved-blocker")
        controller.dispatch(*prior.scope(execution), dispatch.body(), "owner")
        saved = wait_terminal(execution)
        assert saved["current"]["state"] == "SUCCEEDED"
        assert saved["current"]["attempt"] == 1
        assert saved["contract"] == execution["contract"]
        assert grants == ["requested"]
        assert (root / "launches.log").read_text().splitlines() == ["launch"]
    finally:
        assert controller.stop()


def test_workspace_change_after_journal_preflight_stays_unknown_without_automatic_retry(execution, tmp_path, monkeypatch):
    root = tmp_path / "cli"
    provider = adapter(execution, root)
    original = CodexJournalProvider.validate_local_runtime
    def changed_after_check(self):
        original(self)
        (root / "attempts").write_bytes(b"late blocking file")
    monkeypatch.setattr(CodexJournalProvider, "validate_local_runtime", changed_after_check)
    controller = journal_dispatcher(provider, authority=lambda *_: approval(execution, provider), clock=lambda: prior.NOW)
    try:
        controller.dispatch(*prior.scope(execution), dispatch.body(), "owner")
        saved = wait_terminal(execution)
        assert saved["current"]["state"] == "INTERRUPTED" and saved["current"]["outcome_unknown"]
        assert saved["current"]["attempt"] == 1
        assert controller.dispatch(*prior.scope(execution), dispatch.body(), "owner") == saved
        assert not (root / "launches.log").exists()
    finally:
        assert controller.stop()


@pytest.mark.parametrize("mode", ["success", "unicode"])
def test_fake_process_dispatch_persists_exact_binding_and_replays_without_process(execution, tmp_path, mode):
    provider = adapter(execution, tmp_path / "cli", mode)
    manifest = provider.manifest()
    assert manifest["schema_version"] == "CODEX_JOURNAL_CLI_V1"
    assert manifest["output_token_limit_enforced"] is False
    controller = journal_dispatcher(provider, authority=lambda *_: approval(execution, provider), clock=lambda: prior.NOW)
    try:
        controller.dispatch(*prior.scope(execution), dispatch.body(), "owner")
        saved = wait_terminal(execution)
        assert saved["current"]["state"] == "SUCCEEDED", saved
        assert saved["events"][0]["adapter_manifest"] == manifest
        result = saved["current"]["result"]
        assert result["proposal"]["title"].endswith("e\u0301")
        if mode == "unicode":
            assert result["proposal"]["title"] == "Offline CLI \u0085\u2028\u2029 e\u0301"
            assert "\u0085\u2028\u2029" in result["raw_json"]
        assert result["raw_sha256"] == hashlib.sha256(result["raw_json"].encode("utf-8")).hexdigest()
        assert result["provider_reported"] == dict(provider_name="codex_cli", model="not_reported", response_id="offline-thread", usage=dict(input_tokens=10, output_tokens=5))
        assert result["validation"]["independent_live_proof"] == "NOT_VERIFIED"
        assert controller.dispatch(*prior.scope(execution), dispatch.body(), "owner") == saved
        assert list((tmp_path / "cli" / "attempts").iterdir()) == []
        from app.domain.temporal_memory import digest
        from app.repositories import journal_candidates, journal_reviews
        from tests.test_journal_reviews import body as review_body
        candidate, created = journal_candidates.save_execution(*prior.scope(saved), digest(result), "reviewer")
        assert created and journal_reviews.history(*prior.scope(saved)[:2], candidate["candidate_id"])["latest"] is None
        review, created = journal_reviews.save(*prior.scope(saved)[:2], candidate["candidate_id"], review_body(candidate), "reviewer")
        assert created
        exported = journal_reviews.export(*prior.scope(saved)[:2], candidate["candidate_id"], review["review_id"], "json")
        assert json.loads(exported["content"])["candidate"] == candidate
        assert candidate["execution"]["contract"]["execution_id"] == saved["contract"]["execution_id"]
        assert repo.detail(*prior.scope(saved))["events"][0]["adapter_manifest"] == manifest
        assert repo.detail(*prior.scope(saved)) == saved
        assert (tmp_path / "cli" / "launches.log").read_text().splitlines() == ["launch"]
    finally:
        assert controller.stop()


@pytest.mark.parametrize("mode", ["timeout", "flood", "stderr_flood", "final_flood", "missing_completion", "nonzero", "malformed_failure"])
def test_unconfirmed_process_outcome_is_not_a_candidate_or_retried(execution, tmp_path, mode):
    provider = adapter(execution, tmp_path / "cli", mode, timeout_seconds=0.5 if mode == "timeout" else 3)
    controller = journal_dispatcher(provider, authority=lambda *_: approval(execution, provider), clock=lambda: prior.NOW)
    try:
        controller.dispatch(*prior.scope(execution), dispatch.body(), "owner")
        saved = wait_terminal(execution)
        assert saved["current"]["state"] == "INTERRUPTED", saved
        assert saved["current"]["outcome_unknown"] and saved["current"]["result"] is None
        assert controller.dispatch(*prior.scope(execution), dispatch.body(), "owner") == saved
    finally:
        assert controller.stop()


def test_caller_mutation_and_changed_executable_are_rejected_before_launch(execution, tmp_path):
    provider = adapter(execution, tmp_path / "cli")
    changed = deepcopy(execution["contract"]["provider_request"])
    changed["prompt"] += "different input"
    with pytest.raises(ValueError, match="adapter_mismatch"):
        provider.generate_json(ProviderRequest(**changed))
    (tmp_path / "cli" / "fake_cli.py").write_text("raise AssertionError('must not execute')", encoding="utf-8")
    with pytest.raises(ValueError, match="runtime_changed"):
        provider.generate_json(ProviderRequest(**execution["contract"]["provider_request"]))
    assert not (tmp_path / "cli" / "attempts").exists()


def test_changed_home_is_rejected_before_journal_process(execution, tmp_path, monkeypatch):
    from unittest.mock import Mock
    from app.services.providers import codex_journal_process as process

    provider = adapter(execution, tmp_path / "cli")
    home = tmp_path / "cli" / "empty-home"
    home.rename(tmp_path / "cli" / "preserved-home")
    home.write_text("synthetic replacement", encoding="utf-8")
    capture = Mock(return_value=None)
    monkeypatch.setattr(process, "capture", capture)
    with pytest.raises(ValueError, match="^journal_codex_runtime_changed$"):
        provider.generate_json(ProviderRequest(**execution["contract"]["provider_request"]))
    capture.assert_not_called()
    assert not (tmp_path / "cli" / "attempts").exists()


def stream(final="{}", extra=()):
    return "\n".join(json.dumps(row) for row in [
        dict(type="thread.started", thread_id="fake-thread"), dict(type="turn.started"),
        *extra, dict(type="item.completed", item=dict(type="agent_message", text=final)),
        dict(type="turn.completed"),
    ])


@pytest.mark.parametrize("extra", [
    [dict(type="turn.started")],
    [dict(type="turn.completed")],
    [dict(type="item.completed", item=dict(type="command_execution", command="unexpected"))],
    [dict(type="error", message="untrusted private diagnostic")],
    [dict(type="turn.failed"), dict(type="turn.started")],
])
def test_ambiguous_events_do_not_become_success_or_known_failure(extra):
    with pytest.raises(ProviderError, match="completion_unconfirmed"):
        read_result(stream(extra=extra), "{}", 0)


def test_explicit_failure_and_completed_invalid_output_are_distinct():
    failed = '\n'.join(json.dumps(row) for row in [dict(type="thread.started", thread_id="t"),
        dict(type="turn.started"), dict(type="turn.failed", error=dict(message="private"))])
    with pytest.raises(ProviderReportedFailure):
        read_result(failed, "", 0)
    with pytest.raises(ProviderOutputError):
        read_result(stream(), "{}", 0)
    for malformed in ['{"type":"turn.started","type":"turn.completed"}', '{"value":NaN}', 'not json']:
        with pytest.raises(ProviderError, match="completion_unconfirmed"):
            read_result(malformed, "{}", 0)
    with pytest.raises(ProviderError, match="completion_unconfirmed"):
        read_result(stream(), '{"changed":true}', 0)


def test_malformed_failure_is_not_a_known_terminal_result():
    failed = '\n'.join(json.dumps(row) for row in [dict(type="thread.started"),
        dict(type="turn.started"), dict(type="turn.failed")])
    with pytest.raises(ProviderError, match="completion_unconfirmed"):
        read_result(failed, "", 0)


@pytest.mark.parametrize("terminal", [
    dict(type="turn.failed"),
    dict(type="turn.failed", error=None),
    dict(type="turn.failed", error="private diagnostic"),
    dict(type="turn.failed", error=[]),
    dict(type="turn.failed", error={}),
    dict(type="turn.failed", error=dict(message=None)),
    dict(type="turn.failed", error=dict(message=1)),
    dict(type="turn.failed", error=dict(message=True)),
    dict(type="turn.failed", error=dict(message={"private": "diagnostic"})),
])
def test_malformed_terminal_error_is_unknown_with_valid_thread(terminal):
    failed = '\n'.join(json.dumps(row) for row in [
        dict(type="thread.started", thread_id="valid-thread"), dict(type="turn.started"), terminal,
    ])
    with pytest.raises(ProviderError, match="^journal_codex_completion_unconfirmed$") as failure:
        read_result(failed, "", 0)
    assert not isinstance(failure.value, ProviderReportedFailure)


@pytest.mark.parametrize("message", ["", "private diagnostic: 비공개 e\u0301🙂"])
def test_valid_terminal_error_is_known_failure_without_copying_message(message):
    failed = '\n'.join(json.dumps(row) for row in [
        dict(type="thread.started", thread_id="valid-thread"), dict(type="turn.started"),
        dict(type="turn.failed", error=dict(message=message)),
    ])
    with pytest.raises(ProviderReportedFailure) as failure:
        read_result(failed, "", 0)
    assert failure.value.status == "failed"
    assert "private diagnostic" not in str(failure.value)
    assert "비공개" not in str(failure.value)


def test_broken_stdin_is_unknown_even_when_child_exits_zero(tmp_path):
    from app.services.providers.codex_journal_process import capture
    with pytest.raises(ProviderError, match="process_outcome_unknown"):
        capture([sys.executable, "-c", "import sys;sys.stdin.close();print('early exit')"],
                "x" * 2_000_000, tmp_path, dict(os.environ), 3)


def test_thread_start_failure_reaps_owned_child(tmp_path, monkeypatch):
    from app.services.providers import codex_journal_process as process
    real_popen = process.subprocess.Popen
    children = []
    def start(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child
    def unavailable(_):
        raise RuntimeError("no thread")
    monkeypatch.setattr(process.subprocess, "Popen", start)
    monkeypatch.setattr(process.threading.Thread, "start", unavailable)
    try:
        with pytest.raises(ProviderError, match="process_outcome_unknown"):
            process.capture([sys.executable, "-c", "import time;time.sleep(15)"], "{}", tmp_path, dict(os.environ), 1)
        assert children and children[0].poll() is not None
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


def test_runtime_rechecks_workspace_target(tmp_path):
    runtime = runtime_at(tmp_path / "cli")
    runtime.workspace.mkdir()
    target = tmp_path / "other"
    target.mkdir()
    # Reproduce the resolved-path change without requiring OS symlink privilege.
    from unittest.mock import patch
    original = Path.resolve
    with patch.object(Path, "resolve", lambda self, **kwargs: target if self == runtime.workspace else original(self, **kwargs)):
        with pytest.raises(ValueError, match="runtime_changed"):
            runtime.verify()


def test_parent_exit_does_not_leave_pipe_holding_descendant(tmp_path):
    import signal
    from app.services.providers.codex_journal_process import capture
    pid_file = tmp_path / "owned-child.pid"
    program = "import subprocess,sys;from pathlib import Path;sys.stdin.read();p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)']);Path('owned-child.pid').write_text(str(p.pid));print('done')"
    try:
        result = capture([sys.executable, "-c", program], "{}", tmp_path, dict(os.environ), 3)
        assert result.exit_code == 0 and result.stdout.strip() == "done"
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGTERM)
            except OSError:
                pass


@pytest.mark.parametrize("version", ["1", "2"])
def test_six_duplicate_requests_across_dispatchers_spawn_once(execution, tmp_path, version):
    provider = adapter(execution, tmp_path / "cli", version=version)
    controllers = [journal_dispatcher(provider, authority=lambda *_: approval(execution, provider), clock=lambda: prior.NOW) for _ in range(2)]
    gate = Barrier(6)
    def send(index):
        gate.wait()
        return controllers[index % 2].dispatch(*prior.scope(execution), dispatch.body(), "owner")
    try:
        with ThreadPoolExecutor(6) as pool:
            list(pool.map(send, range(6)))
        saved = wait_terminal(execution)
        assert saved["current"]["state"] == "SUCCEEDED"
        assert sum("dispatch" in event for event in saved["events"]) == 1
    finally:
        assert all([controller.stop() for controller in controllers])
    # Restarted controller answers the same lost-response replay from storage.
    fresh = journal_dispatcher(provider, clock=lambda: prior.NOW)
    assert fresh.dispatch(*prior.scope(execution), dispatch.body(), "owner") == saved
    assert fresh.stop()
    assert (tmp_path / "cli" / "launches.log").read_text().splitlines() == ["launch"]


@pytest.mark.parametrize("action", ["cancel", "shutdown"])
@pytest.mark.parametrize("version", ["1", "2"])
def test_late_cli_result_is_fenced_after_control(execution, tmp_path, action, version):
    from tests.test_journal_execution_controls import command
    root = tmp_path / "cli"
    provider = adapter(execution, root, "held", version=version)
    controller = journal_dispatcher(provider, authority=lambda *_: approval(execution, provider), clock=lambda: prior.NOW,
                                    shutdown_grace_seconds=.01)
    try:
        controller.dispatch(*prior.scope(execution), dispatch.body(), "owner")
        deadline = time.monotonic() + 5
        while not (root / "entered").exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert (root / "entered").exists()
        if action == "cancel":
            command(execution, sequence=2)
        else:
            assert not controller.stop()
        saved = repo.detail(*prior.scope(execution))
        assert saved["current"]["state"] == ("CANCELLED" if action == "cancel" else "INTERRUPTED")
        assert saved["current"]["outcome_unknown"]
    finally:
        (root / "release").touch()
        for worker in controller._workers:
            assert worker.wait(5)
        assert controller.stop()
    assert repo.detail(*prior.scope(execution)) == saved
    assert saved["current"]["result"] is None
    assert (root / "launches.log").read_text().splitlines() == ["launch"]
    assert list((root / "attempts").iterdir()) == []


@pytest.mark.parametrize("mode", ["missing", "changed", "extra"])
def test_cli_receipt_manifest_is_rechecked_even_after_digest_rewrite(execution, tmp_path, mode):
    from app.db.bootstrap import connect_metadata_index
    from app.domain.temporal_memory import digest
    from app.domain.journal_exchange import preserved_json
    from app.repositories import journal_dispatch as admission
    provider = adapter(execution, tmp_path / "cli")
    _, claim = admission.admit(*prior.scope(execution), dispatch.body(), "owner", approval(execution, provider), provider.binding,
                               now=prior.NOW, adapter_manifest=provider.manifest())
    with closing(connect_metadata_index()) as connection, connection:
        event = json.loads(connection.execute("SELECT payload_json FROM journal_execution_events WHERE execution_id=?", (claim.execution_id,)).fetchone()[0])
        if mode == "missing":
            del event["adapter_manifest"]
        else:
            event["adapter_manifest"]["stdin_sha256" if mode == "changed" else "extra"] = "0" * 64
        connection.execute("DROP TRIGGER journal_execution_events_no_update")
        connection.execute("UPDATE journal_execution_events SET payload_json=?,payload_digest=? WHERE execution_id=?",
                           (preserved_json(event), digest(event), claim.execution_id))
    with pytest.raises(ValueError, match="integrity_invalid"):
        repo.detail(*prior.scope(execution))


@pytest.mark.parametrize("version", ["1", "2"])
def test_changed_plan_needs_new_authority_and_failed_admission_rolls_back(execution, tmp_path, monkeypatch, version):
    from app.repositories import journal_dispatch as admission
    provider = adapter(execution, tmp_path / "cli", version=version)
    changed = type(provider)(execution["contract"], runtime=provider._runtime, reasoning_effort="high", timeout_seconds=30)
    controller = journal_dispatcher(changed, authority=lambda *_: approval(execution, provider), clock=lambda: prior.NOW)
    try:
        with pytest.raises(ValueError, match="proof_rejected"):
            controller.dispatch(*prior.scope(execution), dispatch.body(), "owner")
        assert repo.detail(*prior.scope(execution)) == execution
    finally:
        assert controller.stop()
    def fail(_):
        raise RuntimeError("rollback fixture")
    with monkeypatch.context() as patch:
        patch.setattr(repo, "checkpoint", fail)
        with pytest.raises(RuntimeError, match="rollback fixture"):
            admission.admit(*prior.scope(execution), dispatch.body(), "owner", approval(execution, provider), provider.binding,
                            now=prior.NOW, adapter_manifest=provider.manifest())
    assert repo.detail(*prior.scope(execution)) == execution
    assert not (tmp_path / "cli" / "launches.log").exists()


def test_plan_is_detached_canonical_and_rejects_unbounded_or_ambient_runtime(execution, tmp_path):
    from app.services.providers.codex_journal_plan import build_plan
    runtime = runtime_at(tmp_path / "cli")
    first = build_plan(execution["contract"], runtime_identity=runtime.identity(), reasoning_effort="low", timeout_seconds=30)
    reordered = json.loads(json.dumps(execution["contract"], sort_keys=True))
    assert build_plan(reordered, runtime_identity=runtime.identity(), reasoning_effort="low", timeout_seconds=30) == first
    for timeout in [0, 121, float("inf"), float("nan"), True]:
        with pytest.raises(ValueError, match="configuration_invalid"):
            build_plan(reordered, runtime_identity=runtime.identity(), reasoning_effort="low", timeout_seconds=timeout)
    for env in [{}, dict(runtime.environment, NODE_OPTIONS="unexpected"), dict(runtime.environment, OPENAI_API_KEY="fake")]:
        with pytest.raises(ValueError, match="runtime_invalid"):
            CliRuntime(runtime.launcher, env, runtime.workspace)
    with pytest.raises(ValueError, match="runtime_invalid"):
        CliRuntime(("codex.cmd",), dict(runtime.environment), runtime.workspace)
