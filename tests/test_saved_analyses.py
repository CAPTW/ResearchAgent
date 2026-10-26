"""Saved external captures remain immutable and never mint execution authority."""
from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import hashlib
import importlib
import json
from threading import Barrier

import pytest

from app.db import bootstrap
from app.services.anthropic_output_comparison import prepare
from tests.test_prepared_review import packet, response
from tests.test_anthropic_messages import envelope
from app.domain import prepared_review


def bundle(*, unsupported=False):
    contract = prepared_review.prepare(packet(), provider_name='anthropic', model='claude-haiku-5-5', max_output_tokens=2200)
    plan = prepare(contract)
    raw = json.dumps(envelope(response(contract, unsupported=unsupported).raw_text, model='claude-haiku-5-5')).encode()
    return dict(schema_version='SAVED_ANTHROPIC_ANALYSIS_V1', plan=plan, arm='structured',
        response_b64=b64encode(raw).decode(), response_sha256=hashlib.sha256(raw).hexdigest(),
        request_body_sha256=plan['arms']['structured']['body_sha256'], http_status=200,
        captured_at='2026-10-11T06:22:38+00:00')


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setenv('XRML_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(bootstrap, 'STORAGE_ROOT', tmp_path / 'runs')
    bootstrap.connect_metadata_index().close()


def modules():
    return importlib.import_module('app.domain.saved_analysis'), importlib.import_module('app.repositories.saved_analyses')


def state():
    with closing(bootstrap.connect_metadata_index(read_only=True)) as c:
        return tuple(c.iterdump())


def test_archive_preserves_original_scope_and_receipt_without_latest_cache():
    domain, repo = modules()
    original = bundle()
    saved = repo.save('destination-topic', 'owner', original)
    reopened = repo.detail('destination-topic', saved['analysis_id'])
    assert reopened == saved
    assert saved['bundle'] == original
    assert saved['receipt']['packet'] == original['plan']['contract']['packet']
    assert saved['receipt']['topic_id'] != saved['topic_id']
    assert saved['receipt']['input_origin'] == 'MANUAL_IMPORT_UNVERIFIED'
    assert saved['latest_decision'] is None and saved['decisions'] == []
    assert saved['receipt']['validation']['prepared_execution']['human_adoption'] == 'NOT_ESTABLISHED'
    with closing(bootstrap.connect_metadata_index(read_only=True)) as c:
        assert c.execute('SELECT count(*) FROM ai_review_drafts').fetchone()[0] == 0
        exists = c.execute("SELECT 1 FROM sqlite_master WHERE name='ai_review_input_history'").fetchone()
        assert exists is None or c.execute('SELECT count(*) FROM ai_review_input_history').fetchone()[0] == 0
    assert domain.validate_bundle(original)[1]['draft'] == saved['receipt']['draft']


@pytest.mark.parametrize('change', ['body', 'raw_hash', 'fenced', 'wrong_paper', 'http', 'arm', 'extra'])
def test_invalid_import_cannot_write_partial_receipt(change):
    _, repo = modules()
    value = bundle()
    if change == 'body':
        value['request_body_sha256'] = 'f' * 64
    elif change == 'raw_hash':
        value['response_sha256'] = 'f' * 64
    elif change in {'fenced', 'wrong_paper'}:
        contract = value['plan']['contract']
        text = response(contract).raw_text
        if change == 'fenced':
            text = '```json\n' + text + '\n```'
        else:
            payload = json.loads(text)
            payload['paper_id'] = 'wrong'
            text = json.dumps(payload)
        raw = json.dumps(envelope(text)).encode()
        value.update(response_b64=b64encode(raw).decode(), response_sha256=hashlib.sha256(raw).hexdigest())
    elif change == 'http':
        value['http_status'] = 400
    elif change == 'arm':
        value['arm'] = 'text'
    else:
        value['live_verified'] = True
    before = state()
    with pytest.raises(ValueError):
        repo.save('destination-topic', 'owner', value)
    assert state() == before


def test_duplicate_concurrent_import_is_one_record_and_receipt():
    _, repo = modules()
    value = bundle()
    gate = Barrier(4)
    def save(_):
        gate.wait()
        return repo.save('topic', 'owner', deepcopy(value))
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(save, range(4)))
    assert len({row['analysis_id'] for row in records}) == 1
    assert len(repo.listing('topic')['items']) == 1
    assert repo.listing('other')['items'] == []
    with pytest.raises(KeyError):
        repo.detail('other', records[0]['analysis_id'])


def test_receipt_and_archive_rollback_together(monkeypatch):
    _, repo = modules()
    before = state()
    def fail(phase):
        if phase == 'receipt_saved':
            raise RuntimeError('injected')
    monkeypatch.setattr(repo, 'checkpoint', fail)
    with pytest.raises(RuntimeError, match='injected'):
        repo.save('topic', 'owner', bundle())
    assert state() == before


def test_decisions_replay_conflict_and_restart_keep_original_analysis():
    domain, repo = modules()
    saved = repo.save('topic', 'owner', bundle())
    command = domain.Decision(request_id='review-request-1', expected_previous_id=None, state='reviewed', note='Evidence checked; numerical results absent.')
    first = repo.decide('topic', saved['analysis_id'], 'owner', command)
    assert repo.decide('topic', saved['analysis_id'], 'owner', command) == first
    with pytest.raises(ValueError, match='conflict'):
        repo.decide('topic', saved['analysis_id'], 'owner', command.model_copy(update={'note': 'Changed replay'}))
    with pytest.raises(ValueError, match='conflict'):
        repo.decide('topic', saved['analysis_id'], 'owner', command.model_copy(update={'request_id': 'stale-request'}))
    next_command = domain.Decision(request_id='review-request-2', expected_previous_id=first['latest_decision']['decision_id'], state='needs_review', note='Recheck details')
    second = repo.decide('topic', saved['analysis_id'], 'owner', next_command)
    assert second['bundle'] == saved['bundle'] and second['receipt'] == saved['receipt']
    assert [entry['state'] for entry in second['decisions']] == ['reviewed', 'needs_review']
    assert repo.detail('topic', saved['analysis_id']) == second
    assert repo.decide('topic', saved['analysis_id'], 'owner', command) == second


def test_sql_immutability_and_corrupt_archive_fail_closed():
    _, repo = modules()
    saved = repo.save('topic', 'owner', bundle())
    with closing(bootstrap.connect_metadata_index()) as c, c:
        with pytest.raises(Exception, match='immutable'):
            c.execute('UPDATE saved_analysis_captures SET payload_json=?', ('{}',))
        c.execute('DROP TRIGGER saved_analysis_no_update')
        c.execute('UPDATE saved_analysis_captures SET payload_json=?', ('{}',))
    with pytest.raises(ValueError, match='integrity'):
        repo.detail('topic', saved['analysis_id'])


def test_replay_preserves_unicode_bytes_and_rejects_normalization_changes():
    domain, repo = modules()
    saved = repo.save('topic', 'owner', bundle())
    command = domain.Decision(request_id='unicode', expected_previous_id=None, state='reviewed', note='e\u0301')
    first = repo.decide('topic', saved['analysis_id'], 'owner', command)
    assert first['latest_decision']['note'] == 'e\u0301'
    with pytest.raises(ValueError, match='conflict'):
        repo.decide('topic', saved['analysis_id'], 'owner', command.model_copy(update={'note': 'é'}))


def test_competing_human_reviews_have_one_winner_without_partial_history():
    domain, repo = modules()
    saved = repo.save('topic', 'owner', bundle())
    barrier = Barrier(4)
    def review(index):
        barrier.wait()
        try:
            return repo.decide('topic', saved['analysis_id'], 'owner', domain.Decision(
                request_id=f'concurrent-{index}', expected_previous_id=None, state='reviewed', note=str(index)))
        except ValueError as exc:
            assert str(exc) == 'saved_analysis_review_conflict'
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(review, range(4)))
    assert sum(value is not None for value in results) == 1
    assert len(repo.detail('topic', saved['analysis_id'])['decisions']) == 1


def test_archive_does_not_mutate_existing_source_topic_history():
    from app.repositories import ai_review_drafts
    domain, repo = modules()
    value = bundle()
    frozen, record = domain.validate_bundle(value)
    with closing(bootstrap.connect_metadata_index()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        receipt = ai_review_drafts.append_input_receipt_in(c, record, input_packet=frozen['plan']['contract']['packet'], input_origin='FIXTURE_SYNTHETIC')
        before = tuple(c.execute('SELECT * FROM ai_review_input_history').fetchall())
    saved = repo.save('unrelated-destination', 'different-owner', value)
    assert saved['receipt']['topic_id'] == receipt['topic_id']
    with closing(bootstrap.connect_metadata_index(read_only=True)) as c:
        assert tuple(c.execute('SELECT * FROM ai_review_input_history').fetchall()) == before
