"""Paper CLI boundary: explicit fake Python processes, never Codex or login."""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import hashlib
import importlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from app.domain import journal_review_input, prepared_review
from app.domain.journal_exchange import preserved_json
from app.repositories import ai_review_drafts as receipts
from app.services.providers.base import ProviderError, ProviderOutputError, ProviderReportedFailure, ProviderRequest
from app.services.providers.codex_journal_process import CliRuntime
from tests import test_prepared_review as prior


def adapter_module():
    return importlib.import_module("app.services.providers.codex_prepared_review")


def plan_module():
    return importlib.import_module("app.services.providers.codex_prepared_review_plan")


def contract():
    return prepared_review.prepare(prior.packet(), provider_name="codex_cli", model="offline-requested-model")


def runtime_at(root: Path, frozen, mode="success"):
    root.mkdir()
    output = prior.response(frozen).payload
    if mode == "citation":
        output["cited_evidence_span_ids"] = ["invented"]
    if mode in {"abstract-empty", "abstract-cited"}:
        output["review_level"] = "abstract_only"
        if mode == "abstract-empty":
            output["cited_evidence_span_ids"] = []
    if mode == "extra":
        output["extra"] = "must fail"
    if mode == "journal":
        output = dict(schema_version="JOURNAL_PROPOSAL_V1", title="wrong domain")
    (root / "output.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    script = root / "fake_cli.py"
    script.write_text('''import json,sys
from pathlib import Path
root=Path(__file__).parent
args=sys.argv[1:]
stdin=sys.stdin.read()
(root/'stdin.json').write_text(stdin,encoding='utf-8',newline='')
(root/'schema.json').write_bytes(Path(args[args.index('--output-schema')+1]).read_bytes())
with (root/'launches.log').open('a') as marker: marker.write('launch\\n')
text=(root/'output.json').read_text(encoding='utf-8')
mode=MODE
if mode=='duplicate': text=text[:-1]+',"paper_id":"paper"}'
Path(args[args.index('--output-last-message')+1]).write_text(text,encoding='utf-8',newline='')
events=[dict(type='thread.started',thread_id='fake-paper-thread'),dict(type='turn.started')]
if mode=='tool': events.append(dict(type='item.completed',item=dict(type='command_execution')))
events.append(dict(type='item.completed',item=dict(type='agent_message',text=text if mode!='mismatch' else 'different')))
if mode=='failed': events.append(dict(type='turn.failed',error=dict(message='private diagnostic')))
elif mode=='malformed_failure': events.append(dict(type='turn.failed'))
elif mode!='missing': events.append(dict(type='turn.completed',usage=dict(input_tokens=4,output_tokens=7,total_tokens=11)))
for event in events: print(json.dumps(event,ensure_ascii=False))
if mode=='nonzero': sys.exit(7)
'''.replace("MODE", repr(mode)), encoding="utf-8")
    home = root / "empty-home"
    home.mkdir()
    environment = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR") if name in os.environ}
    environment.update(CODEX_HOME=str(home), TEMP=str(root), TMP=str(root), PYTHONIOENCODING="utf-8")
    return CliRuntime((sys.executable, str(script)), environment, root / "attempts")


def provider_at(root, mode="success"):
    frozen = contract()
    runtime = runtime_at(root, frozen, mode)
    return adapter_module().CodexPreparedReviewProvider(frozen, runtime=runtime, reasoning_effort="low", timeout_seconds=30), frozen


@pytest.mark.parametrize("separator", ["", "\u0085", "\u2028", "\u2029"])
def test_fake_cli_preserves_request_schema_response_and_restarted_receipt(tmp_path, separator):
    provider, frozen = provider_at(tmp_path / "cli")
    output_path = tmp_path / "cli" / "output.json"
    output = json.loads(output_path.read_text(encoding="utf-8"))
    expected_summary = f" e\u0301 한글🙂 output{separator} end " if separator else " e\u0301 한글🙂 output "
    output["contribution_summary"] = expected_summary
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8", newline="")
    assert not (tmp_path / "cli" / "launches.log").exists()
    assert provider.describe_status()["live_call_enabled"] is False
    request = ProviderRequest(**frozen["provider_request"])
    record = provider.generate_record(request)
    manifest = provider.manifest()
    assert (tmp_path / "cli" / "stdin.json").read_text(encoding="utf-8") == preserved_json(frozen["provider_request"])
    schema_raw = (tmp_path / "cli" / "schema.json").read_text(encoding="utf-8")
    assert prepared_review.sha(schema_raw) == manifest["schema_sha256"]
    schema = json.loads(schema_raw)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(prepared_review.PreparedReviewDraft.model_fields)
    proof = record["validation"]["prepared_execution"]
    assert proof["contract"] == frozen
    assert proof["response"]["model"] == "not_reported"
    assert proof["response"]["response_id"] == "fake-paper-thread"
    assert proof["response"]["usage"] == dict(input_tokens=4, output_tokens=7, total_tokens=11)
    assert proof["response"]["raw_text"] == (tmp_path / "cli" / "output.json").read_text(encoding="utf-8")
    assert record["draft"]["contribution_summary"] == expected_summary
    assert proof["response"]["raw_text_sha256"] == hashlib.sha256(proof["response"]["raw_text"].encode("utf-8")).hexdigest()
    assert proof["scientific_support"] == "NOT_ASSESSED"
    assert proof["full_document_review"] == "NOT_ESTABLISHED"
    marker = record["validation"]["execution"]
    assert marker["manifest"] == manifest
    assert marker["authority"] == "NOT_ESTABLISHED"
    assert marker["live_execution"] == "NOT_VERIFIED"
    # Existing append-only store, no mutable latest-draft cache or app authority.
    database = tmp_path / "receipts.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        saved = receipts.append_input_receipt_in(connection, record, input_packet=frozen["packet"], input_origin="FIXTURE_SYNTHETIC")
        assert receipts.append_input_receipt_in(connection, record, input_packet=frozen["packet"], input_origin="FIXTURE_SYNTHETIC") == saved
        assert connection.execute("SELECT COUNT(*) FROM ai_review_input_history").fetchone()[0] == 1
        selected = journal_review_input.freeze(connection, "topic",
            dict(receipt_id=saved["receipt_id"], component_ids=["/evidence_spans/0/text"]), [("source-run", "paper")])
        paper = dict(paper_version_id="version", canonical_paper_id="paper", title="Fixture paper", source_identity={},
                     metadata_digest="a" * 64, review_state="partial", rights={"state": "unknown"}, review_input=selected)
        references = journal_review_input.references(paper)
        attachment = journal_review_input.attachment(paper)
        assert references[0]["text"] == frozen["packet"]["evidence_spans"][0]["text"]
        assert references[0]["review_receipt_id"] == saved["receipt_id"]
        assert attachment["input_origin"] == "FIXTURE_SYNTHETIC"
        assert attachment["review_result"] == record["draft"]
        summary = attachment["validation"]["prepared_execution"]
        assert summary["requested"]["provider_name"] == "codex_cli"
        assert summary["reported"]["model"] == "not_reported"
        assert summary["full_document_review"] == "NOT_ESTABLISHED"
        assert "contract" not in summary and "response" not in summary
        assert attachment["validation"]["execution"] == marker
    program = """import json,sqlite3,sys
from app.repositories.ai_review_drafts import get_input_receipt_in
with sqlite3.connect(sys.argv[1]) as db:
 db.row_factory=sqlite3.Row
 value=get_input_receipt_in(db,'topic',sys.argv[2])
 print(json.dumps(value,ensure_ascii=True))
"""
    restarted = subprocess.run([sys.executable, "-c", program, str(database), saved["receipt_id"]], capture_output=True, text=True, check=True, timeout=60)
    assert json.loads(restarted.stdout) == saved
    assert (tmp_path / "cli" / "launches.log").read_text().splitlines() == ["launch"]
    assert list((tmp_path / "cli" / "attempts").iterdir()) == []


@pytest.mark.parametrize("mode,category", [("citation", "output"), ("extra", "output"), ("journal", "output"),
    ("abstract-empty", "output"), ("abstract-cited", "output"),
    ("duplicate", "output"), ("tool", "unknown"), ("missing", "unknown"), ("mismatch", "unknown"),
    ("malformed_failure", "unknown"), ("nonzero", "unknown"), ("failed", "failed")])
def test_invalid_or_uncertain_cli_results_never_retry(tmp_path, mode, category):
    provider, frozen = provider_at(tmp_path / "cli", mode)
    with pytest.raises(ProviderError) as failure:
        provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    if category == "output":
        assert isinstance(failure.value, ProviderOutputError)
    elif category == "failed":
        assert isinstance(failure.value, ProviderReportedFailure)
        assert failure.value.status == "failed"
    else:
        assert type(failure.value) is ProviderError
        assert "unknown" in str(failure.value) or "unconfirmed" in str(failure.value)
    assert "private diagnostic" not in str(failure.value)
    assert (tmp_path / "cli" / "launches.log").read_text().splitlines() == ["launch"]
    assert list((tmp_path / "cli" / "attempts").iterdir()) == []


def test_request_and_launcher_mutations_rejected_before_process(tmp_path):
    provider, frozen = provider_at(tmp_path / "cli")
    request = ProviderRequest(**frozen["provider_request"])
    with pytest.raises(ValueError, match="adapter_mismatch"):
        provider.generate_json(replace(request, prompt=request.prompt + " "))
    script = tmp_path / "cli" / "fake_cli.py"
    script.write_text(script.read_text(encoding="utf-8") + "\n# changed launcher", encoding="utf-8")
    with pytest.raises(ValueError, match="runtime_changed"):
        provider.generate_json(request)
    assert not (tmp_path / "cli" / "launches.log").exists()


def test_changed_home_is_rejected_before_paper_process(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from app.services.providers import codex_journal_process as process

    provider, frozen = provider_at(tmp_path / "cli")
    home = tmp_path / "cli" / "empty-home"
    home.rename(tmp_path / "cli" / "preserved-home")
    home.write_text("synthetic replacement", encoding="utf-8")
    capture = Mock(return_value=None)
    monkeypatch.setattr(process, "capture", capture)
    with pytest.raises(ValueError, match="^journal_codex_runtime_changed$"):
        provider.generate_json(ProviderRequest(**frozen["provider_request"]))
    capture.assert_not_called()
    assert not (tmp_path / "cli" / "attempts").exists()


def test_plan_is_exact_detached_and_cannot_issue_authority(tmp_path):
    provider, frozen = provider_at(tmp_path / "cli")
    manifest = provider.manifest()
    assert manifest["schema_version"] == "CODEX_PREPARED_REVIEW_CLI_V1"
    assert manifest["contract_sha256"] == prepared_review.sha(preserved_json(frozen))
    assert manifest["request_sha256"] == frozen["request_sha256"]
    assert manifest["automatic_retry"] is False
    assert manifest["output_token_limit_enforced"] is False
    assert manifest["temperature_enforced"] is False
    assert manifest["tool_isolation"] == "REQUESTED_NOT_LIVE_VERIFIED"
    manifest["model"] = "changed"
    frozen["packet"]["evidence_spans"][0]["text"] = "changed"
    assert provider.manifest()["model"] == "offline-requested-model"
    assert not (tmp_path / "cli" / "attempts").exists()


@pytest.mark.parametrize("change", ["timeout", "bool_timeout", "effort", "runtime", "provider", "model", "contract"])
def test_invalid_plan_settings_fail_closed(tmp_path, change):
    frozen = contract()
    runtime = runtime_at(tmp_path / "cli", frozen)
    options = dict(runtime_identity=runtime.identity(), reasoning_effort="low", timeout_seconds=30)
    if change == "timeout":
        options["timeout_seconds"] = float("inf")
    elif change == "bool_timeout":
        options["timeout_seconds"] = True
    elif change == "effort":
        options["reasoning_effort"] = "invented"
    elif change == "runtime":
        options["runtime_identity"] = {}
    elif change == "provider":
        frozen = prior.contract()
    elif change == "model":
        frozen = prepared_review.prepare(prior.packet(), provider_name="codex_cli", model="space not allowed")
    else:
        frozen["provider_request"]["instructions"] = "changed"
    with pytest.raises(ValueError, match="configuration_invalid"):
        plan_module().build_plan(frozen, **options)


@pytest.mark.parametrize("change", ["manifest", "manifest_hash", "response_hash", "authority", "extra", "id_only", "schema_only"])
def test_transport_archive_tampering_is_refused_before_insert(tmp_path, change):
    provider, frozen = provider_at(tmp_path / "cli")
    record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    bad = deepcopy(record)
    marker = bad["validation"]["execution"]
    if change == "manifest":
        marker["manifest"]["stdin_sha256"] = "0" * 64
        marker["manifest_sha256"] = prepared_review.sha(preserved_json(marker["manifest"]))
    elif change == "manifest_hash":
        marker["manifest_sha256"] = "0" * 64
    elif change == "response_hash":
        marker["response_sha256"] = "0" * 64
    elif change == "authority":
        marker["authority"] = "APPROVED"
    elif change == "id_only":
        del marker["schema_version"]
    elif change == "schema_only":
        del marker["adapter_id"]
    else:
        marker["extra"] = True
    with closing(sqlite3.connect(tmp_path / "rejected.sqlite3")) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="transport_invalid"):
            receipts.append_input_receipt_in(connection, bad, input_packet=frozen["packet"], input_origin="FIXTURE_SYNTHETIC")
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []


def test_corrupt_saved_transport_is_refused_even_with_recomputed_receipt_id(tmp_path):
    provider, frozen = provider_at(tmp_path / "cli")
    record = provider.generate_record(ProviderRequest(**frozen["provider_request"]))
    with closing(sqlite3.connect(tmp_path / "corrupt.sqlite3")) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        saved = receipts.append_input_receipt_in(connection, record, input_packet=frozen["packet"], input_origin="FIXTURE_SYNTHETIC")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE ai_review_input_history SET status='accepted'")
        bad = deepcopy(saved)
        bad["validation"]["execution"]["manifest"]["schema_sha256"] = "0" * 64
        bad["validation"]["execution"]["manifest_sha256"] = prepared_review.sha(preserved_json(bad["validation"]["execution"]["manifest"]))
        bad.pop("receipt_id")
        bad.pop("recorded_at")
        rewritten_id = receipts._receipt_id(bad)
        # Deliberate corruption confined to this disposable SQLite fixture.
        connection.execute("DROP TRIGGER ai_review_input_no_update")
        connection.execute("UPDATE ai_review_input_history SET receipt_id=?,validation_json=?",
                           (rewritten_id, preserved_json(bad["validation"])))
        with pytest.raises(ValueError, match="receipt_corrupt"):
            receipts.get_input_receipt_in(connection, "topic", rewritten_id)
