from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from app.services import operator_audit


def test_env_override_redirects_write_to_temp(tmp_path, monkeypatch) -> None:
    target = tmp_path / "audit" / "operator-audit.jsonl"
    # No in-process attr override -> env takes effect.
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", operator_audit._DEFAULT_AUDIT_LOG_PATH)
    monkeypatch.setenv(operator_audit.OPERATOR_AUDIT_PATH_ENV, str(target))

    assert operator_audit._audit_log_path() == target

    event = operator_audit.record_event(
        action_type="view_dashboard",
        decision="allowed",
        operator_id="pytest",
        reason_codes=["synthetic_isolation_check"],
    )

    assert target.exists()
    lines = [line for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["actionType"] == "view_dashboard"
    assert record["reasonCodes"] == ["synthetic_isolation_check"]
    assert record["auditEventId"] == event["auditEventId"]
    # The resolved write target is never the production default under isolation.
    assert operator_audit._audit_log_path() != operator_audit._DEFAULT_AUDIT_LOG_PATH


def test_temp_record_redacts_secret_like_metadata(tmp_path, monkeypatch) -> None:
    target = tmp_path / "audit" / "operator-audit.jsonl"
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", operator_audit._DEFAULT_AUDIT_LOG_PATH)
    monkeypatch.setenv(operator_audit.OPERATOR_AUDIT_PATH_ENV, str(target))

    operator_audit.record_event(
        action_type="view_dashboard",
        decision="denied",
        operator_id="pytest",
        metadata={"api_key": "sk-should-be-redacted", "note": "benign"},
    )

    raw = target.read_text(encoding="utf-8")
    assert "sk-should-be-redacted" not in raw
    assert "<redacted>" in raw


def test_production_default_when_override_absent(monkeypatch) -> None:
    # No in-process attr override and no env override -> production default, WITHOUT writing.
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", operator_audit._DEFAULT_AUDIT_LOG_PATH)
    monkeypatch.delenv(operator_audit.OPERATOR_AUDIT_PATH_ENV, raising=False)
    assert operator_audit._audit_log_path() == operator_audit._DEFAULT_AUDIT_LOG_PATH


def test_in_process_attr_override_wins_over_env(tmp_path, monkeypatch) -> None:
    attr_path = tmp_path / "attr" / "operator-audit.jsonl"
    env_path = tmp_path / "env" / "operator-audit.jsonl"
    monkeypatch.setenv(operator_audit.OPERATOR_AUDIT_PATH_ENV, str(env_path))
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", attr_path)
    # An explicit in-process override (used by existing operator-audit tests) still wins.
    assert operator_audit._audit_log_path() == attr_path


def test_subprocess_inherits_operator_audit_path(tmp_path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    child_target = tmp_path / "child" / "operator-audit.jsonl"
    code = (
        "from app.services import operator_audit; "
        "operator_audit.record_event(action_type='audit_replay_check', decision='allowed', operator_id='pytest')"
    )
    env = dict(os.environ)
    env[operator_audit.OPERATOR_AUDIT_PATH_ENV] = str(child_target)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert child_target.exists()
    lines = [line for line in child_target.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["actionType"] == "audit_replay_check"
