"""Disposable backup/restore of real local history, with a fresh-process readback."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from app.auth.resource_access import resource_actor_for_user
from app.db import bootstrap
from app.domain import journal_execution
from app.domain.review_preparation import Prepare as PrepareReviewRange
from app.domain.review_offline import Execute as ExecuteOfflineReview
from app.domain.journal_exchange import preserved_json
from app.domain.temporal_memory import digest
from app.repositories import ai_review_drafts, journal_candidates, journal_executions
from app.repositories import journal_authority, journal_reviews, journal_working_copy, local_synthesis, paper_reviews
from app.repositories.files import safe_filename
from app.repositories.runs import build_run_paths
from app.services import ai_provider, ai_review, paper_bank, paper_review, review_preparation, review_offline
from app.services.providers.base import ProviderResult
from tests.test_ai_review_provider import _review
from tests.test_journal_review_packet import proposal
from tests.test_journal_reviews import body as review_body
from tests.test_journal_working_copy import body as working_body
from tests import test_journal_authority as authority_tests, test_temporal_memory


seeded = test_temporal_memory.seeded


_REOPEN = r'''
import faulthandler
faulthandler.dump_traceback_later(15, repeat=True)
import hashlib,json,os,sys
from pathlib import Path
from urllib.parse import quote
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.auth.security import require_product_user
from app.db import bootstrap
from app.main import create_app
from app.repositories import ai_review_drafts,journal_candidates,journal_executions,journal_reviews,local_synthesis
from app.repositories import journal_authority,journal_working_copy
from app.repositories.artifact_refs import resolve_artifact_reference
from app.services import paper_review
from app.services.providers import describe_provider_status,get_active_provider

expected=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
root=Path(os.environ['XRML_DATA_DIR']).resolve()
assert root == bootstrap.STORAGE_ROOT.parent.resolve()
assert not Path(expected['original_root']).exists()
topic,job_id,candidate_id,review_id,execution_id = (
    expected['topic'],expected['job']['job_id'],expected['candidate']['candidate_id'],
    expected['review']['review_id'],expected['execution']['contract']['execution_id'])
args=(topic,job_id,candidate_id)
assert ai_review_drafts.get_input_receipt(topic,expected['receipt']['receipt_id']) == expected['receipt']
assert ai_review_drafts.get_input_receipt(topic,expected['offline_receipt']['receipt_id']) == expected['offline_receipt']
assert local_synthesis.get(topic,job_id) == expected['job']
assert journal_executions.detail(topic,job_id,execution_id) == expected['execution']
unknown_id=expected['unknown_execution']['contract']['execution_id']
authorization_id=expected['authority']['record']['grant']['authorization_id']
assert journal_executions.detail(topic,job_id,unknown_id) == expected['unknown_execution']
assert journal_authority.detail(topic,job_id,unknown_id,authorization_id) == expected['authority']
assert journal_working_copy.latest(topic,job_id,'local-internal-user') == expected['working_copy']
assert journal_candidates.detail(*args) == expected['candidate']
assert journal_reviews.detail(*args,review_id) == expected['review']
assert journal_reviews.export(*args,review_id,'json') == expected['export_json']
assert journal_reviews.export(*args,review_id,'markdown') == expected['export_markdown']
source=expected['source']
resolved=resolve_artifact_reference(source['reference'],run_id=expected['receipt']['run_id'])
assert resolved.is_safe and resolved.exists
actual_path=Path(resolved.resolved_path).resolve()
assert actual_path.is_relative_to(root) and actual_path.relative_to(root).as_posix() == source['relative_path']
assert actual_path.read_bytes().hex() == source['bytes_hex']
assert hashlib.sha256(actual_path.read_bytes()).hexdigest() == source['sha256']
reopened=paper_review.read_local_full_text_evidence(
    run_id=expected['receipt']['run_id'],paper_id=expected['receipt']['paper_id'],max_text_chars=24000)
assert reopened['full_text_artifact_path'] == source['reference']
assert reopened['extraction_provenance'] == expected['receipt']['packet']['source_extraction']

# Restoration does not issue an execution grant. Calls below use the real app
# routes, with absent opt-in flags, disabled scheduler/executor and no Provider.
assert 'XRML_JOURNAL_OFFLINE_ENABLED' not in os.environ
assert get_active_provider() is None
assert describe_provider_status()['provider_mode'] == 'disabled'
with patch('app.services.journal_execution.execute',side_effect=AssertionError('unexpected execution')) as execute, \
     patch('app.services.journal_offline.SyntheticJournalProvider.generate_json',side_effect=AssertionError('unexpected provider')) as generate:
    app=create_app()
    app.dependency_overrides[require_product_user]=lambda:None
    with TestClient(app) as client:
        base=f'/api/topics/{quote(topic,safe="")}/local-synthesis/{job_id}'
        receipt_url=f'/api/topics/{quote(topic,safe="")}/paper-bank/{quote(expected["version_id"],safe="")}/review-inputs/{expected["receipt"]["receipt_id"]}'
        response=client.get(receipt_url)
        assert response.status_code == 200,response.text
        assert response.json()['packet'] == expected['receipt']['packet']
        assert response.json()['draft'] == expected['receipt']['draft']
        preparation_url=receipt_url.split('/review-inputs/')[0]+'/review-preparations/'+expected['preparation']['preparation_id']
        response=client.get(preparation_url)
        assert response.status_code == 200 and response.json() == expected['preparation'],response.text
        response=client.get(preparation_url+'/offline-review')
        assert response.status_code == 200,response.text
        assert response.json()['enabled'] is False and response.json()['latest'] == expected['offline_result']
        response=client.get(receipt_url.rsplit('/',1)[0]+'/'+expected['offline_receipt']['receipt_id'])
        assert response.status_code == 200 and response.json()['packet'] == expected['offline_receipt']['packet'],response.text
        assert app.state.review_offline_runtime is None
        response=client.get(base)
        assert response.status_code == 200 and response.json() == expected['job'],response.text
        response=client.get(base+'/journal-offline')
        assert response.status_code == 200,response.text
        assert response.json()['enabled'] is False and response.json()['reason'] == 'journal_offline_disabled'
        assert app.state.journal_offline_runtime is None
        assert app.state.topic_schedule_worker.snapshot()['enabled'] is False
        assert app.state.run_executor_worker.snapshot()['enabled'] is False
        response=client.get(base+'/journal-working-copy')
        assert response.status_code == 200 and response.json() == expected['working_copy'],response.text
        response=client.get(base+'/journal-executions/'+unknown_id)
        assert response.status_code == 200 and response.json() == expected['unknown_execution'],response.text
        response=client.get(base+'/journal-executions/'+unknown_id+'/authority',params={
            'authorization_id':authorization_id,'expected_record_digest':expected['authority_digest']})
        assert response.status_code == 200 and response.json() == expected['authority'],response.text
        execution_url=base+'/journal-executions/'+execution_id
        response=client.post(execution_url+'/offline-approvals',json={'request_id':'restore-denied','expected_sequence':0})
        assert response.status_code == 409 and response.json()['detail'] == 'journal_offline_disabled',response.text
        response=client.post(execution_url+'/offline-dispatch',json={
            'request_id':'restore-denied','expected_sequence':0,'authorization_id':'not-a-grant','acknowledge_unknown':False})
        assert response.status_code == 409 and response.json()['detail'] == 'journal_offline_disabled',response.text
        response=client.get(base+'/journal-candidates/'+candidate_id+'/reviews/'+review_id+'/export?format=json')
        assert response.status_code == 200 and response.json() == expected['export_json'],response.text
    execute.assert_not_called()
    generate.assert_not_called()
assert journal_executions.detail(topic,job_id,execution_id) == expected['execution']
assert journal_executions.detail(topic,job_id,unknown_id) == expected['unknown_execution']
assert journal_authority.detail(topic,job_id,unknown_id,authorization_id) == expected['authority']
assert journal_working_copy.latest(topic,job_id,'local-internal-user') == expected['working_copy']
assert not Path(expected['original_root']).exists()
print(json.dumps({'repository_readback':True,'api_readback':True,'relocated_source_bytes':True,
                 'execution_disabled':True,'working_copy_preserved':True,'revoked_unknown_not_restarted':True}))
'''


def test_restored_review_and_accepted_journal_reopen_without_original_root(seeded, tmp_path):
    backup = importlib.import_module("app.services.local_data_backup")
    source_root = bootstrap.STORAGE_ROOT.parent.resolve()
    assert source_root.is_relative_to(tmp_path.resolve())
    topic = seeded.TOPIC_A_ID
    item = paper_bank.list_topic_papers(topic, page_size=100)["items"][0]
    occurrence = item["compatibility"]["occurrences"][0]
    run_id, paper_id = occurrence["runId"], occurrence["paperId"]
    source_path = build_run_paths(run_id).raw_dir / (safe_filename(paper_id) + ".txt")
    source_bytes = ("Preserved e\u0301 한글🙂.\r\nSecond synthetic line.\r\n" * 5).encode("utf-8")
    source_path.write_bytes(source_bytes)
    source = _review(1, topic_id=topic, run_id=run_id, paper_id=paper_id, title=item["title"])
    source.update(paper_review.read_local_full_text_evidence(run_id=run_id, paper_id=paper_id))
    paper_reviews.upsert_review(source)
    actor = resource_actor_for_user(None)
    observed = next(row for row in review_preparation.sources(topic, item["paperVersionId"], actor)["sources"]
                    if (row["run_id"], row["paper_id"]) == (run_id, paper_id))
    preparation = review_preparation.prepare(topic, item["paperVersionId"], PrepareReviewRange(
        request_id="backup-prepared-range", run_id=run_id, paper_id=paper_id,
        expected_source_digest=observed["source_digest"], start=20, end=40,
    ), actor)
    offline_capability = review_offline.status(None, topic, item["paperVersionId"], preparation["preparation_id"], actor)
    offline_result = review_offline.execute(review_offline.OfflineReviewRuntime(), topic, item["paperVersionId"],
        preparation["preparation_id"], ExecuteOfflineReview(request_id="backup-offline-review",
            expected_preparation_digest=offline_capability["preparation_digest"]), actor)
    offline_receipt = ai_review_drafts.get_input_receipt(topic, offline_result["receipt_id"])
    packet = ai_review._packet_from_review(source, topic_id=topic, topic_name="Backup fixture", max_chars=120)
    response = ai_provider.ProviderResponse(
        status="generated", provider_name="stub", paper_id=paper_id, run_id="backup-fixture",
        parsed_draft=ai_review.generate_fixture_ai_review_draft(packet),
    )
    response = ai_provider._validate_response(packet, response)
    stored = ai_provider._store_accepted_draft(packet, response, provider_mode="stub", provider_origin="stub")
    receipt = ai_review_drafts.get_input_receipt(topic, stored["validation"]["input_receipt_id"])
    assert receipt is not None and receipt["input_origin"] == "FIXTURE_SYNTHETIC"
    assert receipt["packet"]["source_extraction"]["artifact"]["sha256"] == hashlib.sha256(source_bytes).hexdigest()
    item = next(p for p in paper_bank.list_topic_papers(topic, page_size=100)["items"] if p["paperVersionId"] == item["paperVersionId"])
    selection = [dict(
        paper_version_id=item["paperVersionId"], metadata_digest=item["metadataDigest"],
        review_input=dict(receipt_id=receipt["receipt_id"], component_ids=["/selected_text_excerpts/0/text"]),
    )]
    job, created = local_synthesis.create(topic, selection, "backup-fixture", resource_actor_for_user(None))
    assert created
    for step in range(job["total_steps"]):
        job, advanced = local_synthesis.advance(topic, job["job_id"], step)
        assert advanced

    # Store an explicitly synthetic response through the execution repository;
    # no adapter, CLI, account or network Provider is called to build this data.
    now = datetime(2026, 9, 28, tzinfo=timezone.utc)
    execution, _ = journal_executions.prepare(topic, job["job_id"], journal_execution.Prepare(
        request_id="backup-fixture-execution", expected_input_digest=job["input_digest"],
        provider_name="test", model="synthetic-backup-fixture",
    ), "fixture-owner", now=now)
    execution_id = execution["contract"]["execution_id"]
    claim = journal_executions.claim(topic, job["job_id"], execution_id, expected_attempt=0, now=now)
    assert journal_executions.start(claim, now=now) is not None
    payload = proposal(execution["contract"]["packet"])
    result = journal_execution.result(execution["contract"], ProviderResult(
        provider_name="test", model="synthetic-backup-fixture", response_id="synthetic-backup-response",
        payload=payload, raw_text=preserved_json(payload),
    ))
    assert journal_executions.finish(claim, result, now=now)
    execution = journal_executions.detail(topic, job["job_id"], execution_id)
    candidate, created = journal_candidates.save_execution(topic, job["job_id"], execution_id, digest(result), "fixture-owner")
    assert created and candidate["coverage"]["review_input_excerpt_citations"] == 1
    body = review_body(candidate)
    for entry in body.entries:
        entry.assessment = "limited"
        entry.note = "Synthetic backup fixture; source binding only, not scientific validation."
    accepted, created = journal_reviews.save(topic, job["job_id"], candidate["candidate_id"], body, "fixture-reviewer")
    assert created and accepted["state"] == "accepted"

    working = working_body(job)
    working.content.entries[0].text = "  Unfinished e\u0301 한글🙂\n"
    working.content.entries[0].citation_ids = [candidate["citations"][0]["citation_id"]]
    saved_copy, created = journal_working_copy.save(topic, job["job_id"], working, "local-internal-user")
    assert created and saved_copy["copy"]["content"] == working.content.model_dump()

    # The authority helpers issue only synthetic test records. Mark a stored
    # attempt as started, then revoke its authority without calling any adapter.
    # Its UNKNOWN outcome must remain an explicit unresolved history on restore.
    uncertain, _ = journal_executions.prepare(topic, job["job_id"], journal_execution.Prepare(
        request_id="backup-unknown-fixture", expected_input_digest=job["input_digest"],
        provider_name="test", model="offline-model",
    ), "owner", now=authority_tests.NOW)
    record, created = authority_tests.register(uncertain)
    assert created
    _, uncertain_claim = authority_tests.admit(uncertain)
    assert uncertain_claim is not None
    assert journal_executions.start(uncertain_claim, now=authority_tests.NOW) is not None
    revocation, created = authority_tests.revoke(uncertain)
    assert created
    unknown_scope = authority_tests.scope(uncertain)
    uncertain = journal_executions.detail(*unknown_scope)
    assert uncertain["current"]["state"] == "INTERRUPTED" and uncertain["current"]["outcome_unknown"] is True
    saved_authority = journal_authority.detail(*unknown_scope, record["grant"]["authorization_id"])
    assert saved_authority == dict(record=record, revocation=revocation)
    args = (topic, job["job_id"], candidate["candidate_id"], accepted["review_id"])
    expected = dict(
        original_root=str(source_root), topic=topic, version_id=item["paperVersionId"], receipt=receipt,
        preparation=preparation,
        offline_result=offline_result, offline_receipt=offline_receipt,
        job=job, execution=execution, candidate=candidate, review=accepted,
        working_copy=saved_copy, unknown_execution=uncertain, authority=saved_authority, authority_digest=digest(record),
        export_json=journal_reviews.export(*args, "json"),
        export_markdown=journal_reviews.export(*args, "markdown"),
        source=dict(reference=source["full_text_artifact_path"], relative_path=source_path.relative_to(source_root).as_posix(),
                    bytes_hex=source_bytes.hex(), sha256=hashlib.sha256(source_bytes).hexdigest()),
    )
    assert expected["source"]["reference"].startswith("run:")
    # This fixture has no running app. Close/checkpoint its own stores before
    # asking the stopped-source API to take a consistent copy.
    for database in source_root.glob("*.sqlite3"):
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
            connection.execute("PRAGMA journal_mode=DELETE")
    bundle, restored = tmp_path / "bundle", tmp_path / "restored"
    manifest = backup.create_backup(source_root, bundle, source_stopped=True)
    assert backup.verify_backup(bundle) == manifest
    assert backup.restore_backup(bundle, restored) == manifest
    assert (restored / expected["source"]["relative_path"]).read_bytes() == source_bytes
    assert backup.verify_backup(bundle) == manifest

    # Rename only our checked, disposable source. The fresh process must locate
    # preserved references under the restored root, with no original fallback.
    parked = (tmp_path / "original-parked").resolve()
    assert parked.is_relative_to(tmp_path.resolve()) and not parked.exists()
    source_root.rename(parked)
    assert not source_root.exists()
    expected_path = tmp_path / "expected-readback.json"
    expected_path.write_text(json.dumps(expected, ensure_ascii=False), encoding="utf-8")
    env = dict(os.environ, XRML_DATA_DIR=str(restored), AUTH_MODE="disabled", XRML_RUN_EXECUTOR_MODE="disabled",
               TOPIC_SCHEDULER_ENABLED="0", TOPIC_SCHEDULER_MODE="disabled", XRML_PROVIDER="disabled",
               XRML_ALLOW_PROVIDER_RESEARCH_OUTPUT="0", XRML_PROTOTYPE_REAL_CORPUS_NETWORK="0",
               OPENAI_API_KEY="", ANTHROPIC_API_KEY="", GEMINI_API_KEY="", PYTHONUTF8="1")
    for key in ("XRML_JOURNAL_OFFLINE_ENABLED", "XRML_JOURNAL_OFFLINE_DATA_ROOT", "XRML_REVIEW_OFFLINE_ENABLED", "XRML_REVIEW_OFFLINE_DATA_ROOT"):
        env.pop(key, None)
    try:
        result = subprocess.run([sys.executable, "-c", _REOPEN, str(expected_path)],
                                cwd=Path(__file__).resolve().parents[1], env=env,
                                capture_output=True, text=True, encoding="utf-8", timeout=45)
    except subprocess.TimeoutExpired as exc:
        # Preserve startup/readback traces on failure as well as on success.
        for name, output in (("stdout", exc.stdout), ("stderr", exc.stderr)):
            raw = output.encode("utf-8") if isinstance(output, str) else output or b""
            (tmp_path / f"fresh-process.{name}.log").write_bytes(raw)
        raise
    (tmp_path / "fresh-process.stdout.log").write_text(result.stdout, encoding="utf-8")
    (tmp_path / "fresh-process.stderr.log").write_text(result.stderr, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == dict(repository_readback=True, api_readback=True,
                                           relocated_source_bytes=True, execution_disabled=True,
                                           working_copy_preserved=True, revoked_unknown_not_restarted=True)
    assert not source_root.exists()
