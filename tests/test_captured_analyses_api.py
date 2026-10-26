from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api import saved_analyses as api
from app.auth.config import clear_auth_settings_cache
from app.auth.security import require_product_user
from app.auth.security_types import ProductUser
from tests import test_local_anthropic_trial as trials, test_temporal_memory

case = trials.case
seeded = test_temporal_memory.seeded
ORIGIN = 'http://127.0.0.1:8002'
HEADERS = {'Origin': ORIGIN, 'X-XRML-Analysis-Save': '1'}


@pytest.fixture
def client(case, seeded, monkeypatch):
    monkeypatch.setenv('AUTH_MODE', 'disabled')
    monkeypatch.setenv('TRUST_PROXY_HEADERS', '0')
    clear_auth_settings_cache()
    runtime = case[1]()
    runtime.execute(trials.binding(runtime))
    app = FastAPI()
    app.include_router(api.router)
    app.state.runtime_bootstrap_ok = True
    app.state.local_anthropic_trial = runtime
    try:
        with TestClient(app, base_url=ORIGIN, client=('127.0.0.1', 12000)) as http:
            yield http, runtime, app
    finally:
        clear_auth_settings_cache()


def test_app_capture_preview_save_replay_and_binding_change(client, seeded, case):
    http, runtime, app = client
    root = f'/api/topics/{seeded.TOPIC_A_ID}/saved-analyses'
    route = root + '/sources/anthropic-trial'
    preview = http.get(route)
    assert preview.status_code == 200, preview.text
    assert preview.headers['cache-control'] == 'no-store'
    source = preview.json()['source']
    body = dict(expected_binding_sha256=source['binding_sha256'], expected_response_sha256=source['response_sha256'])
    before = runtime.path.read_bytes()
    result = http.post(route, json=body, headers=HEADERS)
    assert result.status_code == 200, result.text
    value = result.json()
    assert http.post(route, json=body, headers=HEADERS).json() == value
    assert http.get(root + '/' + value['analysis_id']).json() == value
    assert http.get(root).json()['total'] == 1
    assert len(case[2]) == 1 and runtime.path.read_bytes() == before
    for extra in ({'approved': True}, {'actor_id': 'forged'}, {'path': 'untrusted'}, {'bundle': {}}):
        assert http.post(route, json={**body, **extra}, headers=HEADERS).status_code == 422
    assert http.post(route, json={**body, 'bundle': value['bundle']}, headers=HEADERS).status_code == 413
    for headers in ({}, {'Origin': ORIGIN}, {'Origin': 'https://other.invalid', 'X-XRML-Analysis-Save': '1'}, {'Origin': ORIGIN, 'X-XRML-Trial-Action': '1'}):
        assert http.post(route, json=body, headers=headers).status_code == 403
    assert http.post(route, json={**body, 'expected_response_sha256': '0' * 64}, headers=HEADERS).status_code == 409
    assert http.get(route.replace(seeded.TOPIC_A_ID, 'missing-topic')).status_code == 404
    # A valid server bundle cannot be forged through the public file-import route.
    assert http.post(root, json=value['bundle']).status_code == 422
    app.state.local_anthropic_trial = case[1](grant={**case[4], 'trial_id': 'new-selection'})
    assert http.post(route, json=body, headers=HEADERS).status_code == 409
    assert runtime.path.read_bytes() == before


def test_local_actor_bootstrap_origin_and_topic_guards_remain_required(client, seeded):
    http, runtime, app = client
    route = f'/api/topics/{seeded.TOPIC_A_ID}/saved-analyses/sources/anthropic-trial'
    source = http.get(route).json()['source']
    body = dict(expected_binding_sha256=source['binding_sha256'], expected_response_sha256=source['response_sha256'])
    for identity, provider in [('another-admin', 'oidc'), ('local-internal-user', 'oidc'), ('other-local', 'disabled')]:
        app.dependency_overrides[require_product_user] = lambda: ProductUser(identity, identity, None, provider, True)
        assert http.get(route).status_code == 403
        assert http.post(route, json=body, headers=HEADERS).status_code == 403
    app.dependency_overrides.clear()
    for headers in ({'Origin': 'https://other.invalid'}, {'Forwarded': 'for=127.0.0.1'}, {'Host': 'other.invalid'}):
        assert http.get(route, headers=headers).status_code == 403
    with TestClient(app, base_url=ORIGIN, client=('203.0.113.8', 12001)) as remote:
        assert remote.get(route).status_code == 403
    app.state.runtime_bootstrap_ok = False
    assert http.get(route).status_code == 503
    app.state.runtime_bootstrap_ok = True
    app.state.local_anthropic_trial = None
    assert http.get(route).json() == dict(available=False, reason='anthropic_trial_disabled')
    assert http.post(route, json=body, headers=HEADERS).status_code == 409
