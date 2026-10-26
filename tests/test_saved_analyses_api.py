from contextlib import closing
import sqlite3

from fastapi.testclient import TestClient
import pytest

from app.api import saved_analyses as api
from app.auth.resource_access import ResourceActor
from app.auth.security import require_product_user
from app.db.bootstrap import connect_metadata_index
from app.main import create_app
from app.services import topic_profiles
from tests.test_saved_analyses import bundle
from tests import test_temporal_memory

seeded = test_temporal_memory.seeded


@pytest.fixture
def http(seeded):
    app = create_app()
    app.dependency_overrides[require_product_user] = lambda: None
    with TestClient(app) as client:
        yield client


def test_app_import_read_decide_scope_and_invalid_body(http, seeded):
    root = f'/api/topics/{seeded.TOPIC_A_ID}/saved-analyses'
    assert http.get(root).json()['items'] == []
    saved = http.post(root, json=bundle())
    assert saved.status_code == 200, saved.text
    assert saved.headers['cache-control'] == 'no-store'
    value = saved.json()
    url = root + '/' + value['analysis_id']
    assert http.get(url).json() == value
    assert http.get(root).json()['total'] == 1
    assert http.get(url.replace(seeded.TOPIC_A_ID, seeded.TOPIC_B_ID)).status_code == 404
    body = dict(request_id='one', expected_previous_id=None, state='reviewed', note='Read preserved evidence')
    reviewed = http.post(url + '/decisions', json=body)
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()['latest_decision']['note'] == body['note']
    assert http.post(url + '/decisions', json=body).json() == reviewed.json()
    assert http.post(url + '/decisions', json={**body, 'request_id': 'two'}).status_code == 409
    for invalid in ({}, {'approval': True}, [], 'not a bundle'):
        assert http.post(root, json=invalid).status_code == 422
    assert http.post(url + '/decisions', json={**body, 'actor_id': 'forged'}).status_code == 422
    import json
    for field in ('note', 'expected_previous_id'):
        assert http.post(url + '/decisions', content=json.dumps({**body, field: '\ud800'}), headers={'Content-Type': 'application/json'}).status_code == 422
    assert http.post(root, content=b'x' * 2_000_001).status_code == 413


def test_public_read_cannot_import_or_read_private_archive(http, seeded, monkeypatch):
    root = f'/api/topics/{seeded.TOPIC_A_ID}/saved-analyses'
    saved = http.post(root, json=bundle()).json()
    url = root + '/' + saved['analysis_id']
    monkeypatch.setattr(topic_profiles, 'SYSTEM_TEMPLATE_PROFILE_IDS', topic_profiles.SYSTEM_TEMPLATE_PROFILE_IDS | {seeded.TOPIC_A_ID})
    monkeypatch.setattr(api, 'resource_actor_for_user', lambda _: ResourceActor.oidc_user('outsider'))
    for target in (root, url):
        assert http.get(target).status_code == 404
    assert http.post(root, json=bundle()).status_code == 404
    assert http.post(url + '/decisions', json=dict(request_id='deny', expected_previous_id=None, state='reviewed', note='')).status_code == 404
    # Even another administrator must not see another actor's saved capture.
    monkeypatch.setattr(api, 'resource_actor_for_user', lambda _: ResourceActor.oidc_user('another-admin', is_admin=True))
    assert http.get(root).json()['items'] == []
    assert http.get(url).status_code == 404
    with closing(connect_metadata_index(read_only=True)) as c:
        assert c.execute('SELECT count(*) FROM saved_analysis_captures').fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM saved_analysis_decisions').fetchone()[0] == 0


def test_failure_is_not_empty_success_or_private_error(http, seeded, monkeypatch):
    def broken(*a, **kw):
        raise sqlite3.OperationalError('private path and text')
    monkeypatch.setattr(api.store, 'listing', broken)
    response = http.get(f'/api/topics/{seeded.TOPIC_A_ID}/saved-analyses')
    assert response.status_code == 503
    assert response.json() == {'detail': 'saved_analysis_unavailable'}
