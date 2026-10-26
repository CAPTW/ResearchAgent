from contextlib import closing
import json

from app.api import saved_analyses as api
from app.auth.resource_access import ResourceActor
from app.db.bootstrap import connect_metadata_index
from app.services import topic_profiles
from tests.test_saved_analyses import bundle
from tests import test_saved_analyses_api as fixtures

http, seeded = fixtures.http, fixtures.seeded


def seed(http, seeded):
    root = f'/api/topics/{seeded.TOPIC_A_ID}/saved-analyses'
    archive = http.post(root, json=bundle()).json()
    root += '/' + archive['analysis_id']
    archive = http.post(root + '/decisions', json=dict(request_id='checked', expected_previous_id=None, state='reviewed', note='Excerpt checked')).json()
    return root + '/journals', archive


def test_private_app_path_selection_candidate_review_and_export(http, seeded):
    root, archive = seed(http, seeded)
    choices = http.get(root + '/components')
    assert choices.status_code == 200, choices.text
    assert choices.json()['items'][0]['component_id'] == '/contribution_summary'
    command = dict(expected_decision_id=archive['latest_decision']['decision_id'], component_ids=['/contribution_summary'])
    response = http.post(root, json=command)
    assert response.status_code == 200, response.text
    candidate = response.json()
    assert http.post(root, json=command).json() == candidate
    assert http.get(root).json()['total'] == 1
    url = root + '/' + candidate['candidate_id']
    assert http.get(url).json() == candidate
    history = http.get(url + '/reviews').json()
    command = dict(request_id='adopt', expected_previous_id=None, expected_candidate_digest=history['candidate_digest'], state='accepted', note='Excerpt only', entries=[dict(entry_id=history['entries'][0]['entry_id'], assessment='limited', note='No whole paper claim')])
    accepted = http.post(url + '/reviews', json=command)
    assert accepted.status_code == 200, accepted.text
    review = accepted.json()['review']
    export = http.get(url + '/reviews/' + review['review_id'] + '/export?format=json')
    assert export.status_code == 200, export.text
    assert export.headers['cache-control'] == 'no-store'
    assert archive['bundle']['response_sha256'] in export.json()['content']
    assert http.post(root, json={'approval': True}).status_code == 422
    assert http.post(url + '/reviews', json={**command, 'request_id': 'different'}).status_code == 409
    assert http.post(url + '/reviews', content=json.dumps({**command, 'note': '\ud800'}), headers={'Content-Type': 'application/json'}).status_code == 422
    assert http.get(url.replace(seeded.TOPIC_A_ID, seeded.TOPIC_B_ID)).status_code == 404


def test_all_journal_reads_and_writes_retain_owner_boundary(http, seeded, monkeypatch):
    root, archive = seed(http, seeded)
    command = dict(expected_decision_id=archive['latest_decision']['decision_id'], component_ids=['/contribution_summary'])
    candidate = http.post(root, json=command).json()
    url = root + '/' + candidate['candidate_id']
    history = http.get(url + '/reviews').json()
    review_command = dict(request_id='real-review', expected_previous_id=None, expected_candidate_digest=history['candidate_digest'], state='accepted', note='', entries=[dict(entry_id=history['entries'][0]['entry_id'], assessment='supported', note='')])
    first = http.post(url + '/reviews', json=review_command).json()['review']
    second = http.post(url + '/reviews', json={**review_command, 'request_id': 'next', 'expected_previous_id': first['review_id']}).json()['review']
    monkeypatch.setattr(topic_profiles, 'SYSTEM_TEMPLATE_PROFILE_IDS', topic_profiles.SYSTEM_TEMPLATE_PROFILE_IDS | {seeded.TOPIC_A_ID})
    with closing(connect_metadata_index(read_only=True)) as c:
        before = tuple(c.iterdump())
    for actor in (ResourceActor.oidc_user('outsider'), ResourceActor.oidc_user('other-admin', is_admin=True)):
        monkeypatch.setattr(api, 'resource_actor_for_user', lambda _: actor)
        review_url = url + '/reviews/' + second['review_id']
        for target in (root, root + '/components', url, url + '/reviews', review_url, review_url + '/export?format=json', review_url + '/comparison?from_review_id=' + first['review_id']):
            assert http.get(target).status_code == 404, target
        assert http.post(root, json=command).status_code == 404
        assert http.post(url + '/reviews', json=review_command).status_code == 404
    with closing(connect_metadata_index(read_only=True)) as c:
        assert tuple(c.iterdump()) == before
