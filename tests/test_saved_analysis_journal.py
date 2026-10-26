"""Private saved analysis -> frozen candidate -> explicit reviewed export, offline."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import importlib
from threading import Barrier

import pytest

from app.db import bootstrap
from app.domain import journal_review, saved_analysis
from app.repositories import saved_analyses
from tests.test_saved_analyses import bundle, state, store  # noqa: F401


def modules():
    return importlib.import_module('app.domain.saved_analysis_journal'), importlib.import_module('app.repositories.saved_analysis_journals')


def reviewed():
    saved = saved_analyses.save('destination', 'owner', bundle(unsupported=True))
    return saved_analyses.decide('destination', saved['analysis_id'], 'owner', saved_analysis.Decision(
        request_id='checked', expected_previous_id=None, state='reviewed', note='Excerpt only; not scientific acceptance.'))


def setup_candidate():
    domain, repo = modules()
    archive = reviewed()
    command = domain.Create(expected_decision_id=archive['latest_decision']['decision_id'], component_ids=['/contribution_summary', '/limitations/0'])
    candidate = repo.create('destination', archive['analysis_id'], 'owner', command)
    return domain, repo, archive, command, candidate


def review_command(candidate, *, previous=None, state='accepted', request='review-1'):
    from app.domain.temporal_memory import digest
    return journal_review.Save(request_id=request, expected_previous_id=previous, expected_candidate_digest=digest(candidate),
        state=state, note='Human judgment on this excerpt only.', entries=[dict(entry_id=e['entry_id'], assessment='limited', note='Partial excerpt; no full paper assessment.') for e in journal_review.entries(candidate)])


def test_candidate_preserves_interpretation_and_exact_frozen_evidence_without_fake_source():
    domain, repo, archive, command, candidate = setup_candidate()
    assert repo.create('destination', archive['analysis_id'], 'owner', command) == candidate
    assert repo.detail('destination', archive['analysis_id'], 'owner', candidate['candidate_id']) == candidate
    assert candidate['source']['receipt'] == archive['receipt']
    assert candidate['source']['decision'] == archive['latest_decision']
    assert candidate['source']['response_sha256'] == archive['bundle']['response_sha256']
    assert candidate['origin'] == 'SAVED_ANALYSIS_DERIVATION'
    assert candidate['proposal']['sections'][0]['entries'][0]['kind'] == 'interpretation'
    assert candidate['proposal']['sections'][0]['entries'][0]['text'] == archive['receipt']['draft']['contribution_summary']
    assert candidate['citations'][0]['text'] == archive['receipt']['packet']['evidence_spans'][0]['text']
    location = candidate['citations'][0]['locator']
    assert location['evidence_span']['char_start'] == archive['receipt']['packet']['evidence_spans'][0]['char_start']
    assert location['prepared_selection'] == archive['receipt']['packet']['source_preparation']['selection']
    assert location['prepared_source'] == archive['receipt']['packet']['source_preparation']['source']
    assert 'job_id' not in candidate and 'source_snapshot_id' not in candidate['packet']
    assert 'paper_version_id' not in candidate['citations'][0]
    assert candidate['validation']['sentence_support'] == 'NOT_ASSESSED'
    assert repo.listing('destination', archive['analysis_id'], 'owner')['total'] == 1
    with closing(bootstrap.connect_metadata_index(read_only=True)) as c:
        for table in ('journal_candidates', 'local_synthesis_jobs', 'ai_review_input_history'):
            exists = c.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone()
            assert not exists or c.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0


def test_selection_is_explicit_and_stale_or_unreviewed_capture_cannot_create():
    domain, repo = modules()
    archive = saved_analyses.save('destination', 'owner', bundle())
    before = state()
    body = domain.Create(expected_decision_id='decision_' + 'f'*64, component_ids=['/contribution_summary'])
    with pytest.raises(ValueError, match='source_changed'):
        repo.create('destination', archive['analysis_id'], 'owner', body)
    assert state() == before
    archive = reviewed()
    for ids in (['/absent'], ['/contribution_summary', '/contribution_summary']):
        before = state()
        with pytest.raises(ValueError):
            repo.create('destination', archive['analysis_id'], 'owner', domain.Create(expected_decision_id=archive['latest_decision']['decision_id'], component_ids=ids))
        assert state() == before


def test_explicit_review_acceptance_export_and_reopen_preserve_bytes():
    _, repo, archive, _, candidate = setup_candidate()
    args = ('destination', archive['analysis_id'], 'owner', candidate['candidate_id'])
    history = repo.history(*args)
    assert history['latest'] is None
    with pytest.raises(ValueError, match='not_accepted'):
        repo.export(*args, 'missing', 'json')
    bad = review_command(candidate)
    bad.entries[0].assessment = 'not_assessed'
    with pytest.raises(ValueError, match='incomplete'):
        repo.save_review(*args, bad)
    review, created = repo.save_review(*args, review_command(candidate))
    assert created and repo.save_review(*args, review_command(candidate)) == (review, False)
    result = repo.export(*args, review['review_id'], 'json')
    assert 'e\u0301' in result['content'] and 'SAVED_ANALYSIS_DERIVATION' in result['content']
    assert '새 모델 호출' in review['report']['content']
    assert repo.review_detail(*args, review['review_id']) == review
    assert repo.export(*args, review['review_id'], 'json') == result
    held, _ = repo.save_review(*args, review_command(candidate, previous=review['review_id'], state='held', request='hold'))
    assert repo.compare(*args, held['review_id'], review['review_id'])['overall_changes']['state']
    with pytest.raises(ValueError, match='not_accepted'):
        repo.export(*args, review['review_id'], 'markdown')


def test_source_review_change_blocks_new_journal_writes_and_export_but_not_history():
    _, repo, archive, command, candidate = setup_candidate()
    args = ('destination', archive['analysis_id'], 'owner', candidate['candidate_id'])
    review, _ = repo.save_review(*args, review_command(candidate))
    saved_analyses.decide('destination', archive['analysis_id'], 'owner', saved_analysis.Decision(
        request_id='hold-source', expected_previous_id=archive['latest_decision']['decision_id'], state='rejected', note='Recheck'))
    assert repo.create('destination', archive['analysis_id'], 'owner', command) == candidate  # lost-response readback
    assert repo.review_detail(*args, review['review_id']) == review
    for operation in (lambda: repo.export(*args, review['review_id'], 'json'), lambda: repo.save_review(*args, review_command(candidate, previous=review['review_id'], request='new'))):
        with pytest.raises(ValueError, match='source_changed'):
            operation()
    assert repo.save_review(*args, review_command(candidate)) == (review, False)  # old receipt, no new write


def test_private_owner_topic_and_replay_conflicts():
    _, repo, archive, command, candidate = setup_candidate()
    args = ('destination', archive['analysis_id'], 'owner', candidate['candidate_id'])
    review, _ = repo.save_review(*args, review_command(candidate))
    for topic, owner in [('other', 'owner'), ('destination', 'other-admin')]:
        before = state()
        with pytest.raises(KeyError):
            repo.create(topic, archive['analysis_id'], owner, command)
        with pytest.raises(KeyError):
            repo.export(topic, archive['analysis_id'], owner, candidate['candidate_id'], review['review_id'], 'json')
        assert state() == before
    with pytest.raises(ValueError, match='request_conflict'):
        repo.save_review(*args, review_command(candidate).model_copy(update={'note': 'different'}))
    with pytest.raises(ValueError, match='review_conflict'):
        repo.save_review(*args, review_command(candidate, request='stale'))


@pytest.mark.parametrize('phase', ['candidate_saved', 'review_saved'])
def test_failures_rollback_no_partial_candidate_or_acceptance(monkeypatch, phase):
    _, repo, archive, command, candidate = setup_candidate()
    def fail(actual):
        if phase == actual:
            raise RuntimeError('injected')
    monkeypatch.setattr(repo, 'checkpoint', fail)
    before = state()
    with pytest.raises(RuntimeError, match='injected'):
        if phase == 'candidate_saved':
            repo.create('destination', archive['analysis_id'], 'owner', command.model_copy(update={'component_ids': ['/method_summary']}))
        else:
            repo.save_review('destination', archive['analysis_id'], 'owner', candidate['candidate_id'], review_command(candidate))
    assert state() == before


def test_real_concurrent_duplicate_create_and_competing_acceptance():
    domain, repo = modules()
    archive = reviewed()
    command = domain.Create(expected_decision_id=archive['latest_decision']['decision_id'], component_ids=['/contribution_summary'])
    gate = Barrier(4)
    def create(_):
        gate.wait()
        return repo.create('destination', archive['analysis_id'], 'owner', command)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(create, range(4)))
    candidate = results[0]
    assert results == [candidate] * 4
    assert repo.listing('destination', archive['analysis_id'], 'owner')['total'] == 1
    gate = Barrier(4)
    def accept(i):
        gate.wait()
        try:
            return repo.save_review('destination', archive['analysis_id'], 'owner', candidate['candidate_id'], review_command(candidate, request=f'concurrent-{i}'))
        except ValueError as exc:
            assert str(exc) == 'journal_review_conflict'
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(v is not None for v in pool.map(accept, range(4))) == 1
    assert repo.history('destination', archive['analysis_id'], 'owner', candidate['candidate_id'])['total'] == 1


def test_review_unicode_replay_and_sql_immutability_and_corruption():
    _, repo, archive, _, candidate = setup_candidate()
    args = ('destination', archive['analysis_id'], 'owner', candidate['candidate_id'])
    command = review_command(candidate).model_copy(update={'note': 'e\u0301'})
    first, _ = repo.save_review(*args, command)
    assert first['note'] == 'e\u0301'
    with pytest.raises(ValueError, match='request_conflict'):
        repo.save_review(*args, command.model_copy(update={'note': 'é'}))
    with closing(bootstrap.connect_metadata_index()) as c, c:
        for table in ('saved_analysis_journals', 'saved_analysis_journal_reviews'):
            with pytest.raises(Exception, match='immutable'):
                c.execute(f'UPDATE {table} SET payload_json=?', ('{}',))
        c.execute('DROP TRIGGER saved_analysis_journal_reviews_no_update')
        c.execute('UPDATE saved_analysis_journal_reviews SET payload_json=?', ('{}',))
    with pytest.raises(ValueError, match='integrity_invalid'):
        repo.export(*args, first['review_id'], 'json')


def test_unsupported_component_keeps_its_original_label_and_status_in_review_and_export():
    domain, repo = modules()
    archive = reviewed()
    candidate = repo.create('destination', archive['analysis_id'], 'owner', domain.Create(
        expected_decision_id=archive['latest_decision']['decision_id'], component_ids=['/unsupported_claims/0']))
    entry = journal_review.entries(candidate)[0]
    assert entry['source_component']['support'] == 'NOT_ESTABLISHED'
    assert entry['source_component']['component_id'] == '/unsupported_claims/0'
    review, _ = repo.save_review('destination', archive['analysis_id'], 'owner', candidate['candidate_id'], review_command(candidate))
    assert '근거가 확인되지 않은 주장' in review['report']['content']
    assert candidate['source']['receipt']['status'] == 'needs_review'


def test_saved_manuscripts_remain_readable_after_renderer_changes(monkeypatch):
    from app.domain import journal_exchange
    _, repo, archive, _, candidate = setup_candidate()
    args = ('destination', archive['analysis_id'], 'owner', candidate['candidate_id'])
    review, _ = repo.save_review(*args, review_command(candidate))
    exported = repo.export(*args, review['review_id'], 'markdown')
    def changed_renderer(*args):
        raise AssertionError('historical prose must not be rerendered')
    monkeypatch.setattr(journal_exchange, 'manuscript_lines', changed_renderer)
    monkeypatch.setattr(journal_review, 'report', changed_renderer)
    assert repo.detail(*args) == candidate
    assert repo.review_detail(*args, review['review_id']) == review
    assert repo.export(*args, review['review_id'], 'markdown') == exported
