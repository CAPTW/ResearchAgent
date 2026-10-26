"""Stored review text -> local Journal selection, without another model call."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import importlib
import json
from threading import Barrier
import unicodedata

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api import local_synthesis as api
from app.auth.resource_access import ResourceActor, resource_actor_for_user
from app.auth.security import require_product_user
from app.db.bootstrap import connect_metadata_index
from app.domain import journal_exchange as exchange, journal_review
from app.domain.review_input import enrich_packet
from app.domain.temporal_memory import digest
from app.repositories import ai_review_drafts, journal_candidates, journal_reviews, local_synthesis
from tests.test_ai_review_input_history import input_and_record
from tests.test_synthesis_review_inputs import finish, select, source as source


TEXT = 'e\u0301 원문🙂 preserved evidence'


def module():
    return importlib.import_module('app.domain.review_input_journal')


def make_job(catalog, *, selected=True, cited=True, request='derive', text='  e\u0301 분석🙂  '):
    packet, record = input_and_record(run='saved-run', paper='paper-v0', text=TEXT)
    packet.pop('input_scope')
    packet['evidence_spans'] = [dict(span_id='span-1', text=TEXT, char_start=0, char_end=len(TEXT)),
                              dict(span_id='unselected-span', text=TEXT[:2], char_start=0, char_end=2)]
    packet = enrich_packet(dict(run_id='saved-run', paper_id='paper-v0', text_excerpt=TEXT,
        source_text_sha256=packet['source_text_sha256'], evidence_spans=packet['evidence_spans']), packet)
    record.update(status='needs_review', draft=dict(
        contribution_summary=text, method_summary='Method interpretation', evidence_summary='Evidence interpretation',
        relevance_to_topic='Relevance interpretation', novelty_signal='Novelty not established', practical_implications='Possible use',
        limitations=['Excerpt only'], unsupported_claims=['Unsupported original claim'], follow_up_questions=['What remains unknown?'],
        cited_evidence_span_ids=['span-1', 'unselected-span'] if cited else ['unselected-span']))
    record['validation'].update(status='needs_review', reason_codes=['unsupported_claims_present'])
    stored = ai_review_drafts.upsert_draft(record, input_packet=packet, input_origin='FIXTURE_SYNTHETIC')
    receipt = ai_review_drafts.get_input_receipt('topic', stored['validation']['input_receipt_id'])
    catalog[0]['_reviewOccurrenceKeys'] = [('saved-run', 'paper-v0')]
    selection = select(catalog)
    if selected:
        selection[0]['review_input'] = dict(receipt_id=receipt['receipt_id'], component_ids=['/evidence_spans/0/text'])
    job = finish(local_synthesis.create('topic', selection, request, resource_actor_for_user(None))[0])
    return job, receipt


@pytest.fixture
def complete(source):
    return make_job(source)


def command(job, receipt, ids=None):
    return module().Create(expected_packet_id=exchange.packet(job)['packet_id'], receipt_id=receipt['receipt_id'],
        component_ids=ids or ['/contribution_summary', '/unsupported_claims/0', '/follow_up_questions/0'])


def test_api_lists_frozen_analysis_components_and_creates_candidate(complete, monkeypatch):
    job, receipt = complete
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_product_user] = lambda: None
    monkeypatch.setattr(api.topic_profiles, 'get_topic_profile', lambda *a, **kw: {'title': 'Fixture topic'})
    root = f"/api/topics/topic/local-synthesis/{job['job_id']}"
    with TestClient(app) as client:
        response = client.get(root + '/journal-review-inputs')
        assert response.status_code == 200, response.text
        preview = response.json()
        assert preview['packet_id'] == exchange.packet(job)['packet_id']
        item = preview['inputs'][0]
        assert item['receipt_id'] == receipt['receipt_id'] and item['validation_status'] == 'needs_review'
        assert item['components'][0]['text'] == receipt['draft']['contribution_summary']
        body = dict(expected_packet_id=preview['packet_id'], receipt_id=item['receipt_id'], component_ids=['/contribution_summary'])
        response = client.post(root + '/journal-review-input-candidates', json=body)
        assert response.status_code == 201, response.text
        assert response.json()['created'] is True
        candidate = response.json()['candidate']
        assert candidate['source']['receipt'] == receipt and candidate['review_state'] == 'UNREVIEWED'
        assert client.post(root + '/journal-review-input-candidates', json=body).json() == dict(candidate=candidate, created=False)


def test_selection_preserves_exact_text_full_receipt_and_only_selected_cited_span(complete):
    job, receipt = complete
    body = command(job, receipt)
    candidate, created = journal_candidates.save_review_input('topic', job['job_id'], body, 'owner')
    assert created and candidate['schema_version'] == 'REVIEW_INPUT_JOURNAL_CANDIDATE_V1'
    assert candidate['origin'] == 'PRESERVED_REVIEW_DERIVATION'
    assert candidate['packet'] == exchange.packet(job)
    assert candidate['source']['receipt'] == receipt
    assert candidate['source']['receipt_sha256'] == exchange.sha(exchange.preserved_json(receipt))
    assert candidate['source']['model'] is None and candidate['source']['raw_text_sha256'] is None
    assert candidate['source']['selected_input_component_ids'] == ['/evidence_spans/0/text']
    assert candidate['source']['validation_status'] == 'needs_review'
    assert candidate['source']['receipt']['validation']['reason_codes'] == ['unsupported_claims_present']
    assert [ref['source_evidence_id'] for ref in candidate['citations']] == ['span-1']
    assert all(ref['kind'] == 'review_input_excerpt' for ref in candidate['citations'])
    entries = journal_review.entries(candidate)
    assert entries[0]['text'] == receipt['draft']['contribution_summary']
    assert entries[0]['text'] != unicodedata.normalize('NFC', entries[0]['text'])
    assert entries[1]['source_component']['support'] == 'NOT_ESTABLISHED'
    assert candidate['validation']['semantic_support'] == 'NOT_ASSESSED'
    assert candidate['validation']['provider_execution'] == 'NOT_VERIFIED'
    assert journal_reviews.history('topic', job['job_id'], candidate['candidate_id'])['latest'] is None


def test_api_requires_topic_update_and_rejects_foreign_job_or_forged_text(complete, monkeypatch):
    job, receipt = complete
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_product_user] = lambda: None
    monkeypatch.setattr(api.topic_profiles, 'get_topic_profile', lambda *a, **kw: {'profile_id': 'default-xrml'})
    monkeypatch.setattr(api, 'resource_actor_for_user', lambda _: ResourceActor.oidc_user('public-reader'))
    root = f"/api/topics/topic/local-synthesis/{job['job_id']}"
    body = command(job, receipt).model_dump()
    with TestClient(app) as client:
        assert client.get(root + '/journal-review-inputs').status_code == 404
        assert client.post(root + '/journal-review-input-candidates', json=body).status_code == 404
        assert journal_candidates.listing('topic', job['job_id'])['total'] == 0
        monkeypatch.setattr(api, 'resource_actor_for_user', lambda _: ResourceActor.oidc_user('admin', is_admin=True))
        assert client.get(root + '/journal-review-inputs').status_code == 200
        assert client.post(root + '/journal-review-input-candidates', json=body | dict(text='Forged prose')).status_code == 422
        wrong_packet = body | dict(expected_packet_id='jpacket_' + '0' * 64)
        assert client.post(root + '/journal-review-input-candidates', json=wrong_packet).status_code == 409
        assert client.get(root.replace('/topic/', '/foreign/') + '/journal-review-inputs').status_code == 404
        assert journal_candidates.listing('topic', job['job_id'])['total'] == 0


def test_reordered_duplicate_selection_has_one_identity_and_frozen_readback(complete, source):
    job, receipt = complete
    body = command(job, receipt)
    first, _ = journal_candidates.save_review_input('topic', job['job_id'], body, 'owner')
    source.clear()
    second, created = journal_candidates.save_review_input('topic', job['job_id'],
        body.model_copy(update=dict(component_ids=list(reversed(body.component_ids)))), 'another-topic-editor')
    assert not created and second == first
    assert journal_candidates.detail('topic', job['job_id'], first['candidate_id']) == first
    assert journal_candidates.listing('topic', job['job_id'])['items'] == [dict(candidate_id=first['candidate_id'],
        title=first['proposal']['title'], origin=first['origin'], preserved_at=first['preserved_at'])]


@pytest.mark.parametrize('change', ['packet', 'receipt', 'duplicate', 'missing', 'blank', 'long', 'citations'])
def test_invalid_selection_does_not_save(source, change):
    job, receipt = make_job(source, cited=change != 'citations', text=' ' if change == 'blank' else 'x' * 3001 if change == 'long' else 'Valid')
    body = command(job, receipt, ['/contribution_summary'])
    if change == 'packet':
        body = body.model_copy(update=dict(expected_packet_id='jpacket_' + '0' * 64))
    elif change == 'receipt':
        body = body.model_copy(update=dict(receipt_id='aireceipt_' + '0' * 64))
    elif change == 'duplicate':
        body = body.model_copy(update=dict(component_ids=body.component_ids * 2))
    elif change == 'missing':
        body = body.model_copy(update=dict(component_ids=['/summary']))
    with pytest.raises(ValueError, match='journal_packet_mismatch' if change == 'packet' else 'review_input_journal_selection_invalid'):
        journal_candidates.save_review_input('topic', job['job_id'], body, 'owner')
    assert journal_candidates.listing('topic', job['job_id'])['total'] == 0


def test_concurrent_save_and_rollback_retry_preserve_one_immutable_candidate(complete, monkeypatch):
    job, receipt = complete
    body = command(job, receipt)
    def fail(_):
        raise RuntimeError('fixture before candidate commit')
    monkeypatch.setattr(journal_candidates, 'checkpoint', fail)
    with pytest.raises(RuntimeError, match='fixture before candidate commit'):
        journal_candidates.save_review_input('topic', job['job_id'], body, 'owner')
    assert journal_candidates.listing('topic', job['job_id'])['total'] == 0
    monkeypatch.setattr(journal_candidates, 'checkpoint', lambda _: None)
    gate = Barrier(4)
    def save(_):
        gate.wait()
        return journal_candidates.save_review_input('topic', job['job_id'], body, 'owner')
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(save, range(4)))
    assert sum(created for _, created in results) == 1
    assert all(value == results[0][0] for value, _ in results)
    assert journal_candidates.listing('topic', job['job_id'])['total'] == 1


@pytest.mark.parametrize('change', ['source', 'text', 'citation', 'support', 'selection', 'origin', 'hash', 'packet', 'coverage'])
def test_rehashed_candidate_corruption_fails_closed(complete, change):
    job, receipt = complete
    value = journal_candidates.save_review_input('topic', job['job_id'], command(job, receipt), 'owner')[0]
    damaged = deepcopy(value)
    if change == 'source':
        damaged['source']['receipt']['draft']['contribution_summary'] += 'changed'
    elif change == 'text':
        damaged['proposal']['sections'][0]['entries'][0]['text'] = unicodedata.normalize('NFC', damaged['proposal']['sections'][0]['entries'][0]['text'])
    elif change == 'citation':
        damaged['citations'][0]['text'] += 'changed'
    elif change == 'support':
        damaged['component_bindings'][1]['support'] = 'SUPPORTED'
    elif change == 'selection':
        damaged['selected_component_ids'] = ['/method_summary']
    elif change == 'origin':
        damaged['origin'] = 'PROVIDER_RESPONSE'
    elif change == 'hash':
        damaged['source']['raw_text_sha256'] = '0' * 64
    elif change == 'packet':
        damaged['packet']['review_inputs'] = []
    else:
        damaged['coverage']['cited_papers'] = 999
    with closing(connect_metadata_index()) as connection, connection:
        connection.execute('DROP TRIGGER journal_candidates_no_update')
        connection.execute('UPDATE journal_candidates SET payload_json=?,payload_digest=?',
            (exchange.preserved_json(damaged), digest(damaged)))
    with pytest.raises(ValueError, match='journal_candidate_integrity_invalid'):
        journal_candidates.detail('topic', job['job_id'], value['candidate_id'])


def test_human_review_and_export_keep_analysis_qualifications_and_full_receipt(complete):
    job, receipt = complete
    candidate = journal_candidates.save_review_input('topic', job['job_id'], command(job, receipt), 'owner')[0]
    body = journal_review.Save(request_id='review', expected_candidate_digest=digest(candidate), state='accepted',
        entries=[dict(entry_id=e['entry_id'], assessment='not_assessed', note='') for e in journal_review.entries(candidate)])
    with pytest.raises(ValueError, match='journal_review_incomplete'):
        journal_reviews.save('topic', job['job_id'], candidate['candidate_id'], body, 'owner')
    body.entries = [journal_review.EntryReview(entry_id=e.entry_id, assessment='limited', note='Limited excerpt context only') for e in body.entries]
    saved, _ = journal_reviews.save('topic', job['job_id'], candidate['candidate_id'], body, 'owner')
    exported = json.loads(journal_reviews.export('topic', job['job_id'], candidate['candidate_id'], saved['review_id'], 'json')['content'])
    assert exported['candidate']['source']['receipt'] == receipt
    report = saved['report']['content']
    assert 'needs_review' in report and 'unsupported_claims_present' in report
    assert 'NOT_ESTABLISHED' in report and '새 Provider 호출' in report
    assert receipt['receipt_id'] in report
    assert journal_candidates.detail('topic', job['job_id'], candidate['candidate_id'])['review_state'] == 'UNREVIEWED'


def test_prepared_v2_response_request_hashes_and_exact_raw_text_are_preserved():
    from app.services.providers.base import ProviderRequest
    from tests.test_anthropic_prepared_review_v2 import provider_for
    from tests.test_journal_review_packet import frozen_job, NOW
    provider, prepared, calls = provider_for()
    record = provider.generate_record(ProviderRequest(**prepared['provider_request']))
    assert len(calls) == 1  # Existing MockTransport fixture only; no account/network.
    content = ai_review_drafts._receipt_content(ai_review_drafts._normalize_record(record), prepared['packet'], 'FIXTURE_SYNTHETIC')
    receipt = dict(receipt_id=ai_review_drafts._receipt_id(content), recorded_at=NOW, **content)
    job = frozen_job(selected=False)
    job['inputs']['schema_version'] = 'LOCAL_EVIDENCE_SYNTHESIS_V2'
    paper = job['inputs']['papers'][0]
    paper['review_input'] = dict(schema_version='FROZEN_REVIEW_INPUT_V1', receipt=receipt,
        selected_component_ids=[component['json_pointer'] for component in prepared['packet']['input_scope']['components']
            if component['json_pointer'].startswith('/evidence_spans/')])
    job['input_digest'] = digest(job['inputs'])
    preview = module().inputs(job)
    body = module().Create(expected_packet_id=preview['packet_id'], receipt_id=receipt['receipt_id'],
        component_ids=['/contribution_summary'])
    candidate = module().assemble(job, body, 'owner', NOW)
    proof = record['validation']['prepared_execution']
    assert candidate['source']['model'] == prepared['model']
    assert candidate['source']['request_sha256'] == prepared['request_sha256']
    assert candidate['source']['raw_text_sha256'] == proof['response']['raw_text_sha256']
    assert candidate['source']['receipt']['validation']['prepared_execution']['response']['raw_text'] == proof['response']['raw_text']
    assert candidate['source']['receipt']['validation']['execution']['manifest']['schema_version'] == 'ANTHROPIC_PREPARED_REVIEW_WIRE_V2'
    module().validate(candidate, job)
    assert len(calls) == 1


def test_no_review_input_has_empty_preview_and_cannot_derive(source):
    job, receipt = make_job(source, selected=False)
    assert module().inputs(job) == dict(packet_id=exchange.packet(job)['packet_id'], inputs=[])
    with pytest.raises(ValueError, match='review_input_journal_selection_invalid'):
        journal_candidates.save_review_input('topic', job['job_id'], command(job, receipt), 'owner')


def test_saved_candidate_does_not_rerender_markdown_on_read(complete, monkeypatch):
    job, receipt = complete
    candidate = journal_candidates.save_review_input('topic', job['job_id'], command(job, receipt), 'owner')[0]
    monkeypatch.setattr(exchange, 'manuscript_lines', lambda *_: pytest.fail('Do not rerender preserved prose'))
    monkeypatch.setattr(module(), 'provenance_lines', lambda *_: pytest.fail('Do not rerender preserved cover'))
    assert journal_candidates.detail('topic', job['job_id'], candidate['candidate_id']) == candidate
