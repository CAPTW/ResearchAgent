"""Actual app paths use synthetic Messages only, with native authority disabled."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import importlib
from threading import Barrier, Event

import pytest

from app.db import bootstrap
from app.domain.review_preparation import encoded, sha
from app.repositories import review_executions
from tests import test_review_offline_api as previous
from tests.test_review_transport_offline import dump

range_source = previous.range_source
seeded = previous.seeded


def service():
    return importlib.import_module('app.services.review_anthropic_execution')


def enable(monkeypatch):
    previous.enable(monkeypatch)
    monkeypatch.setenv('XRML_PAPER_ANTHROPIC_OFFLINE_ENABLED', '1')
    monkeypatch.setenv('XRML_PAPER_ANTHROPIC_OFFLINE_DATA_ROOT', str(bootstrap.STORAGE_ROOT.parent))


def receipt_count():
    with closing(bootstrap.connect_metadata_index(read_only=True)) as connection:
        # First publication creates this table in its transaction; rollback
        # must leave either no table or an existing table with zero receipts.
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_review_input_history'").fetchone():
            return 0
        return connection.execute('SELECT COUNT(*) FROM ai_review_input_history').fetchone()[0]


def prepare(http, source, request='anthropic-fixture'):
    preparation, root = previous.prepared(http, source)
    base = root + '/transport-executions'
    preview = http.get(base + '/anthropic-preview')
    assert preview.status_code == 200, preview.text
    value = preview.json()['preview']
    command = dict(request_id=request, expected_preparation_digest=value['preparation_digest'],
                   expected_preview_sha256=value['preview_sha256'], acknowledge_unknown=False)
    response = http.post(base + '/anthropic-requests', json=command)
    assert response.status_code == 200, response.text
    return preparation, base, command, response.json(), value


def approve(http, base, ready):
    url = base + '/' + ready['contract']['execution_id']
    command = dict(request_id='approve', expected_sequence=0, expected_contract_sha256=ready['contract_sha256'])
    response = http.post(url + '/anthropic-offline-approval', json=command)
    assert response.status_code == 200, response.text
    return url, command, response.json()


def test_default_deny_and_browser_fields_cannot_enable_or_authorize(range_source, monkeypatch):
    monkeypatch.delenv('XRML_PAPER_ANTHROPIC_OFFLINE_ENABLED', raising=False)
    with previous.client() as http:
        saved, root = previous.prepared(http, range_source)
        base = root + '/transport-executions'
        before = dump()
        preview = http.get(base + '/anthropic-preview')
        assert preview.status_code == 200, preview.text
        assert preview.json() == dict(enabled=False, mode='ANTHROPIC_STRUCTURED_OFFLINE',
                                      reason='paper_anthropic_offline_disabled', preview=None)
        body = dict(request_id='denied', expected_preparation_digest=sha(encoded(saved)), expected_preview_sha256='a' * 64)
        assert http.post(base + '/anthropic-requests', json=body).status_code == 409
        for extra in ({'approved': True}, {'proof': {}}, {'model': 'live'}, {'api_key': 'never-used'}, {'execution_surface': 'NATIVE_HTTP'}):
            assert http.post(base + '/anthropic-requests', json=body | extra).status_code == 422
        assert dump() == before


def test_normal_prepared_range_executes_once_and_retains_exact_input_and_synthetic_coverage(range_source, monkeypatch):
    enable(monkeypatch)
    calls = []
    handler = service()._synthetic_response
    def record(*args):
        calls.append(1)
        return handler(*args)
    monkeypatch.setattr(service(), '_synthetic_response', record)
    with previous.client() as http:
        preparation, base, command, ready, preview = prepare(http, range_source)
        assert ready['current']['state'] == 'READY' and ready['authority'] is None
        assert ready['contract']['prepared'] == preview['prepared']
        assert ready['contract']['transport'] == preview['transport']
        assert http.get(base).json()['capabilities']['dispatch'] is False
        url, approved_command, approved = approve(http, base, ready)
        assert approved['current']['state'] == 'APPROVED'
        runtime = http.app.state.paper_anthropic_offline_runtime
        dispatched = dict(request_id='dispatch', expected_sequence=1)
        barrier = Barrier(4)
        def send(_):
            barrier.wait()
            return http.post(url + '/anthropic-offline-dispatch', json=dispatched)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(send, range(4)))
        assert all(result.status_code == 200 for result in results)
        runtime.wait_idle(30)
        assert len(calls) == 1
        saved = http.get(url).json()
        assert [item['state'] for item in saved['events']] == ['READY', 'APPROVED', 'LEASED', 'RUNNING', 'RESULT_RECEIVED', 'RESULT_SAVED']
        receipt_url = base.split('/review-preparations')[0] + '/review-inputs/' + saved['current']['receipt_id']
        receipt = http.get(receipt_url).json()
        assert receipt['input_origin'] == 'FIXTURE_SYNTHETIC'
        assert receipt['packet'] == preview['prepared']['packet']
        assert receipt['packet']['evidence_spans'][0]['text'] == preparation['selection']['text']
        assert receipt['validation']['execution']['manifest'] == preview['transport']
        assert receipt['validation']['prepared_execution']['full_document_review'] == 'NOT_ESTABLISHED'
        coverage_url = base.removesuffix('/transport-executions') + '/coverage'
        response = http.get(coverage_url)
        assert response.status_code == 200, response.text
        coverage = response.json()
        origins = {row['input_origin']: row for row in coverage['inputs']}
        assert origins['FIXTURE_SYNTHETIC']['covered_chars'] == preparation['selection']['chars']
        assert origins['PROVIDER_RESPONSE']['covered_chars'] == 0
        assert http.post(base + '/anthropic-requests', json=command).json() == saved
        assert http.post(url + '/anthropic-offline-approval', json=approved_command).json() == saved
        assert http.post(url + '/anthropic-offline-dispatch', json=dispatched).json() == saved
        range_source[2].unlink()
        assert receipt_count() == 1
    monkeypatch.setenv('XRML_PAPER_ANTHROPIC_OFFLINE_ENABLED', '0')
    with previous.client() as http:
        assert not http.get(base + '/anthropic-preview').json()['enabled']
        assert http.get(url).json() == saved and http.get(receipt_url).json() == receipt
        assert http.get(coverage_url).json() == coverage
        assert http.post(base + '/anthropic-requests', json=command).json() == saved
        assert http.post(url + '/anthropic-offline-dispatch', json=dispatched).json() == saved
        assert http.post(base + '/anthropic-requests', json=command | {'request_id': 'new'}).status_code == 409


def test_changed_preview_hash_and_fabricated_approval_have_no_storage_effect(range_source, monkeypatch):
    enable(monkeypatch)
    with previous.client() as http:
        _, base, body, ready, _ = prepare(http, range_source)
        url = base + '/' + ready['contract']['execution_id']
        before = dump()
        assert http.post(base + '/anthropic-requests', json=body | {'expected_preview_sha256': '0' * 64}).status_code == 409
        command = dict(request_id='approve', expected_sequence=0, expected_contract_sha256='0' * 64)
        assert http.post(url + '/anthropic-offline-approval', json=command).status_code == 409
        command['expected_contract_sha256'] = ready['contract_sha256']
        assert http.post(url + '/anthropic-offline-approval', json=command | {'proof': {}}).status_code == 422
        assert http.post(url + '/anthropic-offline-dispatch', json=dict(request_id='early', expected_sequence=0)).status_code == 409
        assert dump() == before


@pytest.mark.parametrize('action', ['cancel', 'revoke', 'shutdown'])
def test_running_result_is_fenced_without_publishing(range_source, monkeypatch, action):
    enable(monkeypatch)
    started, release = Event(), Event()
    handler = service()._synthetic_response
    def blocked(*args):
        started.set()
        assert release.wait(15)
        return handler(*args)
    monkeypatch.setattr(service(), '_synthetic_response', blocked)
    with previous.client() as http:
        _, base, _, ready, _ = prepare(http, range_source)
        url, _, _ = approve(http, base, ready)
        runtime = http.app.state.paper_anthropic_offline_runtime
        try:
            assert http.post(url + '/anthropic-offline-dispatch', json=dict(request_id='dispatch', expected_sequence=1)).status_code == 200
            assert started.wait(10)
            if action == 'shutdown':
                runtime.stop()
            else:
                body = dict(request_id=action, expected_sequence=3)
                if action == 'cancel':
                    body['action'] = action
                endpoint = '/anthropic-offline-revocation' if action == 'revoke' else '/controls'
                assert http.post(url + endpoint, json=body).status_code == 200
        finally:
            release.set()
            runtime.wait_idle(30)
        saved = http.get(url).json()
        assert saved['current']['state'] == dict(cancel='CANCELLED', revoke='REVOKED', shutdown='INTERRUPTED')[action]
        assert saved['current']['outcome_unknown'] is True
        assert saved['current']['receipt_id'] is None
        assert receipt_count() == 0


def test_received_publication_rolls_back_and_recovers_without_resend(range_source, monkeypatch):
    enable(monkeypatch)
    calls = []
    handler = service()._synthetic_response
    def record(*args):
        calls.append(1)
        return handler(*args)
    def fail(phase):
        if phase == 'after_receipt':
            raise RuntimeError('synthetic_publication_failure')
    monkeypatch.setattr(service(), '_synthetic_response', record)
    monkeypatch.setattr(review_executions, 'checkpoint', fail)
    with previous.client() as http:
        _, base, _, ready, _ = prepare(http, range_source)
        url, _, _ = approve(http, base, ready)
        dispatch = dict(request_id='dispatch', expected_sequence=1)
        assert http.post(url + '/anthropic-offline-dispatch', json=dispatch).status_code == 200
        http.app.state.paper_anthropic_offline_runtime.wait_idle(30)
        assert http.get(url).json()['current']['state'] == 'RESULT_RECEIVED'
        assert receipt_count() == 0
    monkeypatch.setattr(review_executions, 'checkpoint', lambda _: None)
    with previous.client() as http:
        # Result publication uses only the stored response, even after restart.
        body = dict(request_id='save', expected_sequence=4, action='save_result')
        response = http.post(url + '/controls', json=body)
        assert response.status_code == 200, response.text
        saved = response.json()
        assert saved['current']['state'] == 'RESULT_SAVED'
        assert http.post(url + '/controls', json=body).json() == saved
        assert http.post(url + '/anthropic-offline-dispatch', json=dispatch).json() == saved
        assert len(calls) == 1
        revoke = dict(request_id='revoke-completed', expected_sequence=5)
        revoked = http.post(url + '/anthropic-offline-revocation', json=revoke).json()
        assert revoked['current'] == saved['current'] and revoked['authority']['validity'] == 'REVOKED'
        assert http.post(url + '/anthropic-offline-revocation', json=revoke).json() == revoked
    monkeypatch.setenv('XRML_PAPER_ANTHROPIC_OFFLINE_ENABLED', '0')
    with previous.client() as http:
        before = dump()
        assert http.post(url + '/anthropic-offline-revocation', json=revoke).json() == revoked
        assert http.post(url + '/anthropic-offline-revocation', json=revoke | {'request_id': 'new-revoke'}).status_code == 409
        assert dump() == before


def test_unknown_result_is_not_retried_and_needs_new_acknowledgement(range_source, monkeypatch):
    enable(monkeypatch)
    calls = []
    def fail(*args):
        calls.append(1)
        raise RuntimeError('synthetic_worker_failure')
    monkeypatch.setattr(service(), '_synthetic_response', fail)
    with previous.client() as http:
        _, base, original, ready, _ = prepare(http, range_source)
        url, _, _ = approve(http, base, ready)
        dispatch = dict(request_id='dispatch', expected_sequence=1)
        assert http.post(url + '/anthropic-offline-dispatch', json=dispatch).status_code == 200
        http.app.state.paper_anthropic_offline_runtime.wait_idle(30)
        failed = http.get(url).json()
        assert failed['current']['state'] == 'INTERRUPTED' and failed['current']['outcome_unknown'] is True
        assert http.post(url + '/anthropic-offline-dispatch', json=dispatch).json() == failed
        assert len(calls) == 1
        assert http.get(base).json()['outcome_unknown_pending'] is True
        body = original | {'request_id': 'new-attempt'}
        assert http.post(base + '/anthropic-requests', json=body).status_code == 409
        response = http.post(base + '/anthropic-requests', json=body | {'acknowledge_unknown': True})
        assert response.status_code == 200, response.text
        assert response.json()['current']['state'] == 'READY' and response.json()['authority'] is None
        assert len(calls) == 1


def test_dedicated_root_and_disabled_external_runtime_are_required(range_source, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setenv('XRML_PAPER_ANTHROPIC_OFFLINE_DATA_ROOT', str(bootstrap.STORAGE_ROOT / 'wrong'))
    with pytest.raises(ValueError, match='paper_anthropic_offline_configuration_invalid'):
        service().build_runtime()
    enable(monkeypatch)
    monkeypatch.setenv('TOPIC_SCHEDULER_ENABLED', '1')
    with pytest.raises(ValueError, match='paper_anthropic_offline_configuration_invalid'):
        service().build_runtime()


def test_competing_apps_replay_one_immutable_approval(range_source, monkeypatch):
    enable(monkeypatch)
    barrier = Barrier(2)
    issuer = service()._issue
    def competing(*args):
        value = issuer(*args)
        barrier.wait(timeout=15)
        return value
    monkeypatch.setattr(service(), '_issue', competing)
    with previous.client() as first, previous.client() as second:
        _, base, _, ready, _ = prepare(first, range_source)
        url = base + '/' + ready['contract']['execution_id'] + '/anthropic-offline-approval'
        body = dict(request_id='competing', expected_sequence=0, expected_contract_sha256=ready['contract_sha256'])
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda http: http.post(url, json=body), [first, second]))
        assert [result.status_code for result in results] == [200, 200], [result.text for result in results]
        assert results[0].json() == results[1].json()
        assert first.post(url, json=body).json() == results[0].json()
        assert len(results[0].json()['events']) == 2


def test_read_only_system_template_actor_cannot_mutate_but_admin_can(range_source, monkeypatch):
    from app.api import review_execution as api
    from app.auth.resource_access import ResourceActor
    from app.services import topic_profiles
    enable(monkeypatch)
    with previous.client() as http:
        _, base, original, ready, _ = prepare(http, range_source)
        url = base + '/' + ready['contract']['execution_id']
        monkeypatch.setattr(topic_profiles, 'SYSTEM_TEMPLATE_PROFILE_IDS', topic_profiles.SYSTEM_TEMPLATE_PROFILE_IDS | {range_source[0]})
        monkeypatch.setattr(api, 'resource_actor_for_user', lambda _: ResourceActor.oidc_user('outsider'))
        before = dump()
        assert http.get(base + '/anthropic-preview').status_code == 404
        assert http.post(base + '/anthropic-requests', json=original | {'request_id': 'other'}).status_code == 404
        approval = dict(request_id='approve', expected_sequence=0, expected_contract_sha256=ready['contract_sha256'])
        assert http.post(url + '/anthropic-offline-approval', json=approval).status_code == 404
        assert dump() == before
        monkeypatch.setattr(api, 'resource_actor_for_user', lambda _: ResourceActor.oidc_user('local-internal-user', is_admin=True))
        assert http.post(url + '/anthropic-offline-approval', json=approval).status_code == 200


def test_expired_approval_denies_dispatch_without_renewal(range_source, monkeypatch):
    from app.repositories import review_authorities
    enable(monkeypatch)
    with previous.client() as http:
        _, base, _, ready, _ = prepare(http, range_source)
        url, body, approved = approve(http, base, ready)
        expiry = approved['authority']['expires_at']
        monkeypatch.setattr(review_authorities, 'iso_now_utc', lambda: expiry)
        before = dump()
        response = http.post(url + '/anthropic-offline-dispatch', json=dict(request_id='expired', expected_sequence=1))
        assert response.status_code == 409, response.text
        assert dump() == before
        replay = http.post(url + '/anthropic-offline-approval', json=body).json()
        assert replay['events'] == approved['events']
        assert replay['authority']['expires_at'] == expiry


def test_native_target_and_other_actor_cannot_use_synthetic_issuer(range_source, monkeypatch):
    from app.auth.resource_access import ResourceActor
    from app.api import review_execution as api
    enable(monkeypatch)
    with previous.client() as http:
        _, base, _, ready, _ = prepare(http, range_source)
        changed = deepcopy(ready['contract'])
        changed['transport']['execution_surface'] = 'NATIVE_HTTP'
        with pytest.raises(ValueError, match='transport_mismatch'):
            service()._issue(changed, service().domain.Control(request_id='bad', expected_sequence=0, action='approve'))
        url = base + '/' + ready['contract']['execution_id']
        before = dump()
        monkeypatch.setattr(api, 'resource_actor_for_user', lambda _: ResourceActor.oidc_user('outsider'))
        assert http.get(base + '/anthropic-preview').status_code == 404
        assert http.post(url + '/anthropic-offline-approval', json=dict(request_id='bad', expected_sequence=0,
            expected_contract_sha256=ready['contract_sha256'])).status_code == 404
        assert dump() == before
