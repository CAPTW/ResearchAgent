"""Explicit compatibility probes use disposable fake executables, never Codex."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

from app.services import codex_cli_compatibility as service
from app.services.codex_cli_compatibility import CompatibilityReport, requirements_digest
from app.services.providers.codex_journal_plan import CONFIG, argv
from app.services.providers.codex_journal_process import CliRuntime


def test_contract_is_available_without_invoking_a_client():
    assert CompatibilityReport.model_fields["schema_version"].default == "CODEX_CLI_COMPATIBILITY_V1"
    assert len(requirements_digest()) == 64


def fixture_config(root: Path, mode: str = "success") -> tuple[Path, dict]:
    root.mkdir()
    script = root / "fake_cli.py"
    config_path = root / "preview.json"
    flags = " ".join(sorted({part for part in argv(("launcher",), "cwd", "model", "low")
                             if part.startswith("-") and part != "-"}))
    script.write_text('''import json,os,sys,time
from pathlib import Path
root=Path(__file__).parent
args=sys.argv[1:]
mode=MODE
with (root/'calls.jsonl').open('a',encoding='utf-8') as log:
 log.write(json.dumps(dict(args=args,env=dict(os.environ),cwd=str(Path.cwd()),stdin=sys.stdin.read()))+'\\n')
if args==['--version']:
 if mode=='version_error': sys.exit(7)
 if mode=='runtime_drift':
  with Path(__file__).open('a') as changed: changed.write('\\n# drift')
 if mode=='config_drift':
  with (root/'preview.json').open('a') as changed: changed.write(' ')
 print('unknown private version' if mode=='invalid_version' else 'codex-cli 0.162.0-alpha.2')
elif args==['exec','--help']:
 if mode=='help_error': sys.exit(8)
 print(FLAGS.replace('--strict-config','--strict-config-extra') if mode=='missing_flags' else FLAGS)
elif args[:2]==['features','list']:
 if mode=='timeout': time.sleep(20)
 if mode=='stdout_flood': sys.stdout.write('x'*2000000);sys.stdout.flush();time.sleep(20)
 if mode=='stderr_flood': sys.stderr.write('private'*20000);sys.stderr.flush();time.sleep(20)
 if mode=='rejected': print('PRIVATE CONFIGURATION ERROR',file=sys.stderr);sys.exit(2)
 if mode=='descendant':
  import subprocess
  subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)'])
 print('shell_tool stable false')
else: raise AssertionError('unexpected command')
'''.replace("MODE", repr(mode)).replace("FLAGS", repr(flags)), encoding="utf-8")
    home = root / "original-home"
    home.mkdir()
    (home / "auth.json").write_text("private original credentials must not be read", encoding="utf-8")
    (home / "config.toml").write_text("private original configuration must not be read", encoding="utf-8")
    config = dict(schema_version="CODEX_JOURNAL_PREVIEW_CONFIG_V1", launcher=[sys.executable, str(script)],
        environment=dict(CODEX_HOME=str(home), PATH="private original path", HOME=str(home),
                         **{key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}),
        workspace=str(root / "original-workspace"), reasoning_effort="low", timeout_seconds=30.0)
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path, config


def calls(path: Path) -> list[dict]:
    log = path.parent / "calls.jsonl"
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


def test_success_observation_exact_commands_isolation_and_cleanup(tmp_path, monkeypatch):
    path, config = fixture_config(tmp_path / "fake")
    scratch = tmp_path / "scratch"
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "HTTPS_PROXY", "NODE_OPTIONS", "PYTHONPATH"):
        monkeypatch.setenv(key, "must-not-be-inherited")
    real_open = Path.open
    def guarded_open(self, *args, **kwargs):
        if self.is_relative_to(Path(config["environment"]["CODEX_HOME"])):
            pytest.fail("Original profile contents must not be opened")
        return real_open(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guarded_open)
    runtime = CliRuntime(tuple(config["launcher"]), config["environment"], Path(config["workspace"]))
    result = service.inspect(path, scratch)
    assert result.status == "CONFIGURATION_OBSERVED" and result.reason is None
    assert result.cli_version == "codex-cli 0.162.0-alpha.2"
    assert result.runtime_identity.model_dump() == runtime.identity()
    assert result.requirements_sha256 == requirements_digest()
    assert result.execution_allowed is False and result.model_invoked is False
    assert result.checked_at.tzinfo is not None
    assert len(result.checks) == 3 and all(check.exit_code == 0 for check in result.checks)
    recorded = calls(path)
    assert [call["args"] for call in recorded] == [
        ["--version"], ["exec", "--help"], ["features", "list", *[part for setting in CONFIG for part in ("-c", setting)]]]
    assert len({call["env"]["CODEX_HOME"] for call in recorded}) == 3
    for call in recorded:
        assert call["stdin"] == ""
        assert Path(call["cwd"]).is_relative_to(scratch)
        assert set(call["env"]) <= {"SYSTEMROOT", "WINDIR", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "CODEX_HOME", "TEMP", "TMP"}
        for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "CODEX_HOME", "TEMP", "TMP"):
            assert Path(call["env"][key]).is_relative_to(scratch)
    assert list(scratch.iterdir()) == [] and not Path(config["workspace"]).exists()
    assert str(tmp_path) not in result.model_dump_json() and "private" not in result.model_dump_json().lower()
    assert CompatibilityReport.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError):
        result.status = "INCOMPLETE"
    with pytest.raises(ValidationError):
        result.runtime_identity.launcher_sha256 = "0" * 64


@pytest.mark.parametrize("mode,status,reason,count", [
    ("rejected", "CONFIGURATION_REJECTED", "config_rejected", 3),
    ("missing_flags", "INCOMPLETE", "required_flags_missing", 2),
    ("version_error", "INCOMPLETE", "probe_failed", 1),
    ("invalid_version", "INCOMPLETE", "invalid_version", 1),
    ("help_error", "INCOMPLETE", "probe_failed", 2),
    ("timeout", "INCOMPLETE", "probe_failed", 3),
    ("stdout_flood", "INCOMPLETE", "probe_failed", 3),
    ("stderr_flood", "INCOMPLETE", "probe_failed", 3),
])
def test_failures_are_bounded_redacted_and_never_retried(tmp_path, monkeypatch, mode, status, reason, count):
    path, _ = fixture_config(tmp_path / "fake", mode)
    scratch = tmp_path / "scratch"
    monkeypatch.setattr(service, "PROBE_TIMEOUT_SECONDS", 0.5)
    result = service.inspect(path, scratch)
    assert result.status == status and result.reason == reason
    assert len(calls(path)) == count
    assert result.execution_allowed is False and result.model_invoked is False
    assert "PRIVATE" not in result.model_dump_json() and "private" not in result.model_dump_json()
    assert list(scratch.iterdir()) == []
    assert len(result.checks) == (count - 1 if mode in {"timeout", "stdout_flood", "stderr_flood"} else count)


@pytest.mark.parametrize("mode", ["config_drift", "runtime_drift"])
def test_drift_refuses_report_and_cleans_owned_children(tmp_path, mode):
    path, _ = fixture_config(tmp_path / "fake", mode)
    scratch = tmp_path / "scratch"
    with pytest.raises(ValueError, match="changed"):
        service.inspect(path, scratch)
    assert len(calls(path)) == 1 and list(scratch.iterdir()) == []


def test_descendant_does_not_keep_pipes_or_temporary_profile_open(tmp_path, monkeypatch):
    path, _ = fixture_config(tmp_path / "fake", "descendant")
    monkeypatch.setattr(service, "PROBE_TIMEOUT_SECONDS", 1.0)
    scratch = tmp_path / "scratch"
    result = service.inspect(path, scratch)
    assert result.status == "CONFIGURATION_OBSERVED"
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize("kind", ["repo", "home", "workspace", "existing", "relative", "ancestor"])
def test_unsafe_scratch_is_refused_before_process(tmp_path, kind):
    path, config = fixture_config(tmp_path / "fake")
    scratch = {
        "repo": service._REPO / "never-created-compatibility-probe",
        "home": Path(config["environment"]["CODEX_HOME"]) / "scratch",
        "workspace": Path(config["workspace"]) / "scratch",
        "existing": tmp_path / "existing", "relative": Path("relative"), "ancestor": tmp_path,
    }[kind]
    if kind == "existing":
        scratch.mkdir()
    with pytest.raises(ValueError):
        service.inspect(path, scratch)
    assert calls(path) == []


def test_launcher_inside_original_profile_is_refused_without_read(tmp_path, monkeypatch):
    path, config = fixture_config(tmp_path / "fake")
    config["launcher"][1] = str(Path(config["environment"]["CODEX_HOME"]) / "auth.json")
    path.write_text(json.dumps(config), encoding="utf-8")
    real_open = Path.open
    def guarded(self, *args, **kwargs):
        if self == Path(config["launcher"][1]):
            pytest.fail("Launcher inside profile must be rejected before hashing")
        return real_open(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guarded)
    with pytest.raises(ValueError, match="configuration_invalid"):
        service.inspect(path, tmp_path / "scratch")
    assert calls(path) == []


def test_report_exclusive_write_and_runtime_binding(tmp_path):
    path, _ = fixture_config(tmp_path / "fake")
    scratch = tmp_path / "scratch"
    result = service.inspect(path, scratch)
    output = tmp_path / "report.json"
    service.write_report(output, result, config_path=path, scratch_root=scratch)
    original = output.read_bytes()
    assert CompatibilityReport.model_validate_json(original) == result
    with pytest.raises(ValueError, match="output_invalid"):
        service.write_report(output, result, config_path=path, scratch_root=scratch)
    assert output.read_bytes() == original
    script = path.parent / "fake_cli.py"
    script.write_text(script.read_text(encoding="utf-8") + "\n# changed", encoding="utf-8")
    with pytest.raises(ValueError, match="runtime_changed"):
        service.write_report(tmp_path / "changed.json", result, config_path=path, scratch_root=scratch)
    assert not (tmp_path / "changed.json").exists()


@pytest.mark.parametrize("kind", ["repo", "home", "workspace", "scratch", "relative", "config"])
def test_output_never_uses_protected_or_temporary_paths(tmp_path, kind):
    path, config = fixture_config(tmp_path / "fake")
    scratch = tmp_path / "scratch"
    output = {
        "repo": service._REPO / "never-created-compatibility-report.json",
        "home": Path(config["environment"]["CODEX_HOME"]) / "report.json",
        "workspace": Path(config["workspace"]) / "report.json", "scratch": scratch / "report.json",
        "relative": Path("relative.json"), "config": path,
    }[kind]
    with pytest.raises(ValueError):
        service.validate_output_path(output, config_path=path, scratch_root=scratch)
    assert calls(path) == []


def test_symlink_ancestors_are_rejected_without_following(tmp_path, monkeypatch):
    path, _ = fixture_config(tmp_path / "fake")
    alias = tmp_path / "alias"
    real = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: self == alias or real(self))
    with pytest.raises(ValueError, match="path_invalid"):
        service.inspect(path, alias / "scratch")
    with pytest.raises(ValueError, match="path_invalid"):
        service.validate_output_path(alias / "report.json", config_path=path, scratch_root=tmp_path / "scratch")
    assert calls(path) == []


@pytest.fixture
def observed_report():
    return dict(schema_version="CODEX_CLI_COMPATIBILITY_V1", profile="CODEX_JOURNAL_CLI_V1",
        requirements_sha256=requirements_digest(), runtime_identity=dict(launcher_sha256="a" * 64,
        environment_sha256="b" * 64, workspace_sha256="c" * 64), checked_at=datetime.now(timezone.utc).isoformat(),
        cli_version="codex-cli 0.162.0-alpha.2", status="CONFIGURATION_OBSERVED", reason=None,
        checks=[dict(name=name, exit_code=0, stdout_sha256="d" * 64) for name in ("version", "exec_help", "configuration")],
        execution_allowed=False, model_invoked=False)


@pytest.mark.parametrize("field,value", [
    ("execution_allowed", True), ("model_invoked", True), ("status", "COMPATIBLE"),
    ("execution_allowed", 0), ("model_invoked", 0),
    ("reason", "config_rejected"), ("cli_version", None), ("cli_version", "codex-cli 1.2.3\nprivate"),
    ("checked_at", "2026-10-07T00:00:00"), ("profile", "CODEX_JOURNAL_CLI_V2"),
    ("extra", "not allowed"), ("requirements_sha256", "a" * 63),
    ("requirements_sha256", "a" * 64 + "\n"), ("cli_version", "codex-cli 1.2.3\n"),
])
def test_report_rejects_false_positive_or_unsafe_fields(observed_report, field, value):
    value_doc = deepcopy(observed_report)
    value_doc[field] = value
    with pytest.raises(ValidationError):
        CompatibilityReport.model_validate_json(json.dumps(value_doc))


@pytest.mark.parametrize("change", ["nonzero", "missing", "duplicate", "order", "bool_code", "extra", "extra_identity"])
def test_report_rejects_contradictory_checks(observed_report, change):
    if change == "nonzero":
        observed_report["checks"][2]["exit_code"] = 2
    elif change == "missing":
        observed_report["checks"].pop()
    elif change == "duplicate":
        observed_report["checks"][2]["name"] = "version"
    elif change == "order":
        observed_report["checks"].reverse()
    elif change == "bool_code":
        observed_report["checks"][0]["exit_code"] = False
    elif change == "extra":
        observed_report["checks"][0]["private_stderr"] = "private"
    else:
        observed_report["runtime_identity"]["path"] = "private"
    with pytest.raises(ValidationError):
        CompatibilityReport.model_validate_json(json.dumps(observed_report))


def test_wrapper_only_runs_explicit_non_agent_commands_and_refuses_overwrite(tmp_path):
    path, _ = fixture_config(tmp_path / "fake", "rejected")
    output = tmp_path / "report.json"
    command = [sys.executable, str(service._REPO / "tools" / "check_codex_compatibility.py"),
               "--config", str(path), "--scratch-root", str(tmp_path / "scratch"), "--output", str(output)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    assert result.returncode == 1 and result.stdout.strip() == "CONFIGURATION_REJECTED", result.stderr
    saved = output.read_bytes()
    repeat = subprocess.run(command, capture_output=True, text=True, timeout=20)
    assert repeat.returncode == 2 and repeat.stderr.strip() == "codex_compatibility_check_failed"
    assert saved == output.read_bytes() and len(calls(path)) == 3
