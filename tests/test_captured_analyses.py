"""Server-held successful captures can be archived without spending another attempt."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import importlib
from threading import Barrier

import httpx
import pytest

from app.domain.review_preparation import encoded, sha
from app.domain import prepared_review
from app.services.providers.anthropic_messages import parse_response
from app.repositories import saved_analyses
from tests import test_local_anthropic_trial as trial_tests, test_saved_analyses as archive_tests

case = trial_tests.case
store = archive_tests.store


def subject():
    return importlib.import_module('app.services.captured_analyses')


def forbidden(*a, **kw):
    pytest.fail('Archival must not invoke execution, mutation or credentials')


@pytest.mark.parametrize('options', [{}, {'read_timeout_seconds': 300},
    {'read_timeout_seconds': 300, 'schema_compiler': 'ANTHROPIC_STRUCTURED_OUTPUT_V2'}])
def test_exact_trial_capture_is_saved_without_comparison_repackaging(case, monkeypatch, options):
    module, build, calls, raw, grant, contract = case
    if 'schema_compiler' in options:
        options = dict(options, schema_compiler=module.SCHEMA_COMPILER_V2)
    runtime = build(**options)
    runtime.execute(trial_tests.binding(runtime))
    original = runtime.path.read_bytes()
    # Historical copies stay available after authorization expires and keys change.
    reopened = build(**options, resolve_key=forbidden, clock=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc))
    monkeypatch.setattr(reopened, 'execute', forbidden)
    monkeypatch.setattr(reopened, '_mutate', forbidden)
    api = subject()
    preview = api.preview(reopened, 'local-internal-user')
    source = preview['source']
    command = api.Save(expected_binding_sha256=source['binding_sha256'], expected_response_sha256=source['response_sha256'])
    saved = api.save(reopened, 'topic', 'local-internal-user', command)
    assert api.save(reopened, 'topic', 'local-internal-user', command) == saved
    assert runtime.path.read_bytes() == original and len(calls) == 1
    assert saved['bundle']['plan']['contract'] == contract
    assert saved['bundle']['request_body_sha256'] == sha(calls[0].content.decode())
    assert saved['bundle']['response_sha256'] == sha(raw.decode())
    assert saved['bundle']['plan']['grant'] == grant
    assert saved['bundle']['events'] == runtime._read()['events']
    assert saved['bundle']['captured_at'] == runtime._read()['events'][1]['at']
    assert saved['receipt']['input_origin'] == 'FIXTURE_SYNTHETIC_CAPTURE'
    original_result = prepared_review.result(contract, parse_response(raw, max_output_bytes=prepared_review.MAX_OUTPUT_BYTES))
    assert saved['receipt']['status'] == original_result['status']
    assert saved['receipt']['draft'] == original_result['draft'] and saved['latest_decision'] is None
    assert saved['receipt']['validation']['prepared_execution']['human_adoption'] == 'NOT_ESTABLISHED'
    assert saved_analyses.detail('topic', saved['analysis_id'], 'local-internal-user') == saved
    with pytest.raises(ValueError, match='saved_analysis_bundle_invalid'):
        saved_analyses.save('topic', 'local-internal-user', saved['bundle'])
    with pytest.raises(KeyError):
        saved_analyses.detail('topic', saved['analysis_id'], 'outsider')
    assert saved_analyses.listing('topic', 'local-internal-user')['total'] == 1


@pytest.mark.parametrize('mode', ['prepared', 'cancelled', 'invalid', 'unknown'])
def test_ineligible_trial_does_not_become_a_saved_analysis(case, mode):
    _, build, _, _, _, _ = case
    runtime = build(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b'{}')))
    if mode == 'cancelled':
        runtime.cancel(trial_tests.binding(runtime))
    elif mode in {'invalid', 'unknown'}:
        if mode == 'invalid':
            from tests.test_anthropic_messages import envelope
            runtime = build(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=envelope('{}'))))
        runtime.execute(trial_tests.binding(runtime))
    before = archive_tests.state()
    assert subject().preview(runtime, 'local-internal-user') == dict(available=False, reason='anthropic_trial_analysis_unavailable')
    with pytest.raises(ValueError, match='analysis_unavailable'):
        runtime.archive_capture('local-internal-user')
    assert archive_tests.state() == before


def test_changed_runtime_response_or_actor_cannot_store_wrong_capture(case):
    _, build, _, _, _, _ = case
    runtime = build()
    runtime.execute(trial_tests.binding(runtime))
    api = subject()
    source = api.preview(runtime, 'local-internal-user')['source']
    before = archive_tests.state()
    for field in ('expected_binding_sha256', 'expected_response_sha256'):
        body = dict(expected_binding_sha256=source['binding_sha256'], expected_response_sha256=source['response_sha256'])
        body[field] = '0' * 64
        with pytest.raises(ValueError, match='changed'):
            api.save(runtime, 'topic', 'local-internal-user', api.Save(**body))
    with pytest.raises(PermissionError):
        api.preview(runtime, 'another-admin')
    assert archive_tests.state() == before


def test_semantic_ledger_corruption_is_rejected_even_with_rehashed_events(case):
    _, build, _, _, _, _ = case
    runtime = build()
    runtime.execute(trial_tests.binding(runtime))
    bundle = runtime.archive_capture('local-internal-user')
    from app.domain.saved_analysis import validate_bundle
    for field, replacement in [('draft', {}), ('actual_model', 'invented'), ('http_status', True)]:
        tampered = deepcopy(bundle)
        last = tampered['events'][-1]
        last['changes'][field] = replacement
        last['sha256'] = sha(encoded({k: v for k, v in last.items() if k != 'sha256'}))
        with pytest.raises(ValueError):
            validate_bundle(tampered)
    changed = deepcopy(bundle)
    changed['plan']['manifest']['http_timeouts'] = dict(read=1)
    with pytest.raises(ValueError):
        validate_bundle(changed)


def test_concurrent_archive_and_rollback_preserve_trial_and_private_store(case, monkeypatch):
    _, build, calls, _, _, _ = case
    runtime = build()
    runtime.execute(trial_tests.binding(runtime))
    before = runtime.path.read_bytes()
    api = subject()
    source = api.preview(runtime, 'local-internal-user')['source']
    command = api.Save(expected_binding_sha256=source['binding_sha256'], expected_response_sha256=source['response_sha256'])
    barrier = Barrier(4)
    def save(_):
        barrier.wait()
        return api.save(runtime, 'topic', 'local-internal-user', command)
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(save, range(4)))
    assert records == [records[0]] * 4
    assert saved_analyses.listing('topic', 'local-internal-user')['total'] == 1
    database = archive_tests.state()
    def fail(phase):
        if phase == 'archive_saved':
            raise RuntimeError('injected')
    monkeypatch.setattr(saved_analyses, 'checkpoint', fail)
    with pytest.raises(RuntimeError, match='injected'):
        api.save(runtime, 'other-topic', 'local-internal-user', command)
    assert archive_tests.state() == database
    assert runtime.path.read_bytes() == before and len(calls) == 1


def test_large_native_capture_keeps_original_bytes_without_manual_import_limit(case):
    from base64 import b64decode
    _, build, _, raw, _, _ = case
    padded = b' ' * 2_100_000 + raw
    runtime = build(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=padded)))
    runtime.execute(trial_tests.binding(runtime))
    api = subject()
    source = api.preview(runtime, 'local-internal-user')['source']
    value = api.save(runtime, 'topic', 'local-internal-user', api.Save(
        expected_binding_sha256=source['binding_sha256'], expected_response_sha256=source['response_sha256']))
    capture = next(event['changes']['capture_b64'] for event in value['bundle']['events'] if 'capture_b64' in event['changes'])
    assert b64decode(capture, validate=True) == padded
    assert value['bundle']['response_sha256'] == sha(padded.decode())
